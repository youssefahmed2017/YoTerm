"""Command-line entry point for YoTerm-Vids.

`yoterm-vids <file>` prints info; `yoterm-vids <file> --play` (or the shortcut
`yoterm-vids demo`) streams the video into the terminal as YT;img frames. The
diagnostic modes (--decode/--schedule/--process) exercise single pipeline
stages.
"""

import argparse
import os
import sys

from . import __version__
from .utils import setup_logging, log, human_bytes, human_duration

# The bundled sample used by `yoterm-vids demo`, sitting at the project root
# (one level up from this package directory).
DEMO_VIDEO = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cat.mp4")


def build_parser():
    p = argparse.ArgumentParser(
        prog="yoterm-vids",
        description="Play videos inside YoTerm (GPU-sampled YT;img frames).",
    )
    p.add_argument("file", help="path to a video file, or 'demo' for the sample")
    p.add_argument(
        "--play",
        action="store_true",
        help="stream the video into the terminal (implied by the 'demo' shortcut)",
    )
    p.add_argument(
        "--loop", action="store_true", help="restart at the end (--play / --native)"
    )
    p.add_argument(
        "--native",
        action="store_true",
        help="emit a YT;vid sequence so YoTerm plays it itself (spacebar to pause)",
    )
    p.add_argument(
        "--decode",
        action="store_true",
        help="decode every frame and print its index + timestamp (no display)",
    )
    p.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="with --decode, stop after N frames",
    )
    p.add_argument(
        "--schedule",
        action="store_true",
        help="pace frames in real time and report timing accuracy (no display)",
    )
    p.add_argument(
        "--work",
        type=float,
        default=0.0,
        metavar="MS",
        help="simulate MS of per-frame processing (to exercise frame-skipping)",
    )
    p.add_argument(
        "--process",
        action="store_true",
        help="RGB-convert + resize every frame; report throughput (no display)",
    )
    p.add_argument(
        "--size",
        metavar="WxH",
        default="320x240",
        help="target pixel box for --process (default 320x240)",
    )
    p.add_argument(
        "--mode",
        choices=("contain", "fill", "cover"),
        default="contain",
        help="scaling mode for --process (default contain)",
    )
    p.add_argument(
        "--quality",
        choices=("nearest", "fast", "smooth"),
        default="smooth",
        help="resize filter for --process (default smooth)",
    )
    p.add_argument(
        "--out",
        metavar="PATH",
        help="with --process, save the middle frame here for inspection",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument(
        "--version", action="version", version=f"yoterm-vids {__version__}"
    )
    return p


def _print_info(info):
    """Human-readable summary of a probed video, to stderr's sibling: stdout.

    (Nothing is playing yet, so stdout is free for the info block. Once playback
    exists it will carry the image stream instead.)"""
    fps = f"{info.avg_fps:.3f}".rstrip("0").rstrip(".") if info.avg_fps else "?"
    lines = [
        f"file       {info.path}",
        f"size       {human_bytes(info.file_bytes)}",
        f"format     {info.container_format}",
        f"codec      {info.codec}  ({info.pix_fmt})",
        f"resolution {info.width} x {info.height}",
        f"duration   {human_duration(info.duration)}",
        f"fps        {fps}",
        f"frames     {info.frame_count if info.frame_count else '?'}",
        f"bitrate    {f'{info.bit_rate / 1000:.0f} kbps' if info.bit_rate else '?'}",
    ]
    print("\n".join(lines))


def _decode_frames(path, limit):
    """M1 demo: walk the decoder and print each frame's index + timestamp.

    Confirms open → metadata → frame decode → timestamps → clean EOF end to end.
    """
    import time

    from .decoder import Decoder

    with Decoder(path) as dec:
        count = 0
        started = time.perf_counter()
        for frame in dec.frames():
            print(f"Frame {frame.index:<5d} t={frame.pts:7.3f}s")
            count += 1
            if limit and count >= limit:
                break
        elapsed = time.perf_counter() - started
    rate = count / elapsed if elapsed else 0.0
    log.info(
        "decoded %d frame%s in %.2fs (%.0f fps)%s",
        count,
        "" if count == 1 else "s",
        elapsed,
        rate,
        "" if limit and count >= limit else " — reached EOF",
    )
    return 0


def _schedule_frames(path, work_ms, verbose):
    """M2 demo: run the scheduler over the decoder in real time.

    Prints a sparse trace of when each frame was actually presented vs. its
    intended timestamp, then a summary showing total wall time (should match the
    video's duration) and worst-case lateness (should stay tiny). `--work`
    injects fake per-frame load to make frame-skipping kick in.
    """
    from .decoder import Decoder
    from .scheduler import Scheduler, Clock, perf

    work = work_ms / 1000.0
    with Decoder(path) as dec:
        duration = dec.info.duration
        sched = Scheduler(dec.info.avg_fps)
        clock = Clock()
        last_print = [-1.0]

        def present(frame):
            if work > 0:
                spin_end = perf() + work  # simulate processing without yielding
                while perf() < spin_end:
                    pass
            wall = clock.now()
            if verbose or wall - last_print[0] >= 0.5:
                last_print[0] = wall
                print(
                    f"t={frame.pts:6.3f}s  shown@{wall:6.3f}s  "
                    f"drift={ (wall - frame.pts) * 1000:+5.0f}ms"
                )

        stats = sched.run(dec.frames(), present, clock)

    log.info(
        "shown=%d skipped=%d late=%d  max_late=%.1fms",
        stats.shown, stats.skipped, stats.late, stats.max_late * 1000,
    )
    if duration:
        log.info(
            "wall=%.3fs  video=%.3fs  end-to-end drift=%+.0fms",
            stats.wall, duration, (stats.wall - duration) * 1000,
        )
    return 0


def _parse_size(text):
    w, _, h = text.lower().partition("x")
    return int(w), int(h)


def _process_frames(path, size, mode, quality, out):
    """M3 demo: convert + resize every frame; report dimensions and throughput.

    Saves the middle frame to `out` (if given) so the RGB conversion and
    aspect-preserving fit can be eyeballed.
    """
    import time

    from .decoder import Decoder
    from .resize import FrameProcessor

    box = _parse_size(size)
    proc = FrameProcessor(box, mode=mode, quality=quality)

    with Decoder(path) as dec:
        total = dec.info.frame_count
        mid = total // 2 if total else None
        first_dims = None
        count = 0
        started = time.perf_counter()
        for frame in dec.frames():
            img = proc.process(frame.av_frame)
            if first_dims is None:
                first_dims = img.size
            if out and frame.index == mid:
                img.save(out)
                log.info("saved frame %d to %s", frame.index, out)
            count += 1
        elapsed = time.perf_counter() - started

    log.info(
        "processed %d frames  box=%dx%d mode=%s -> output=%dx%d",
        count, box[0], box[1], mode, first_dims[0], first_dims[1],
    )
    log.info(
        "throughput %.0f frames/s (%.2fms per frame)",
        count / elapsed if elapsed else 0.0,
        elapsed / count * 1000 if count else 0.0,
    )
    return 0


def _play_video(path, loop):
    """Stream `path` into the terminal as YT;img frames (the CLI display path).

    Sizes the video to fill the terminal, letterboxed to keep its aspect, and
    streams frames until the video ends or Ctrl+C. The sink always tears down the
    alternate screen on exit, so an interrupt can't leave the terminal wedged.
    """
    import shutil

    from .decoder import probe
    from .player import Player
    from .renderer import EscapeSink

    info = probe(path)  # validate before touching the screen (fail cleanly)

    cell = (8, 16)  # assumed cell pixels — only affects target sharpness/cells
    cols, lines = shutil.get_terminal_size((80, 24))
    # Fill the screen but leave the last row free, so the image's block
    # placement never scrolls the bottom of the screen. Cap the pixel box so a
    # maximised window doesn't push huge frames through per-frame JPEG decode +
    # texture upload on the UI thread; the GPU upscales the capped frame to the
    # cell box smoothly anyway.
    MAX_W, MAX_H = 960, 540
    box = (min(cols * cell[0], MAX_W), min(max(1, lines - 1) * cell[1], MAX_H))

    sink = EscapeSink(sys.stdout, cell_px=cell, quality=75, fullscreen=True)
    player = Player(path, sink, box, mode="contain", quality="fast", loop=loop)

    log.info("playing %s  %dx%d  %.1fs  (Ctrl+C to quit)",
             os.path.basename(path), info.width, info.height, info.duration or 0)
    try:
        player.play()
    except KeyboardInterrupt:
        player.stop()  # sink teardown already ran via play()'s finally
        log.info("stopped")
    return 0


def _native_video(path, loop):
    """Emit a YT;vid sequence so YoTerm decodes and plays it natively (frames go
    straight to the GPU; spacebar pauses). A no-op on any other terminal."""
    import shutil

    from .decoder import probe
    from .protocols import yt_video

    probe(path)  # validate; a bad path shouldn't emit a doomed sequence
    cols, lines = shutil.get_terminal_size((80, 24))
    sys.stdout.write(
        yt_video(os.path.abspath(path), cols=cols, rows=max(1, lines - 1), loop=loop)
    )
    sys.stdout.flush()
    log.info("sent YT;vid for %s (spacebar pauses inside YoTerm)",
             os.path.basename(path))
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    # 'demo' is a shortcut: play the bundled sample.
    is_demo = args.file == "demo"
    path = DEMO_VIDEO if is_demo else args.file

    # Imported here so `--help` / `--version` work even without PyAV installed.
    from .decoder import probe

    try:
        if args.decode:
            return _decode_frames(path, args.limit)
        if args.schedule:
            return _schedule_frames(path, args.work, args.verbose)
        if args.process:
            return _process_frames(
                path, args.size, args.mode, args.quality, args.out
            )
        if args.native:
            return _native_video(path, args.loop)
        if args.play or is_demo:
            return _play_video(path, args.loop)
        info = probe(path)
    except FileNotFoundError:
        log.error("no such file: %s", path)
        return 2
    except Exception as exc:  # unreadable / no video stream / corrupt
        log.error("cannot open %s: %s", path, exc)
        return 1

    _print_info(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
