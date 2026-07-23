"""The playback engine: decode → process → schedule → sink.

`Player` ties the four stages together and adds transport control (pause, resume,
stop, loop, seek) on top of the pausable Clock. It's deliberately sink-agnostic
and thread-friendly: `play()` runs the loop (typically on a worker thread) while
`pause()`/`seek()`/`stop()` are called from another thread — YoTerm's UI thread,
or the CLI's keyboard reader.

Seeking works by re-anchoring, not by tracking absolute wall-clock: a seek posts
a target position, the frame loop breaks, the decoder repositions, and the
scheduler runs again with its clock anchored to the first post-seek frame — the
same pts-anchoring that keeps startup and pauses drift-free.
"""

import threading

from .decoder import Decoder, probe
from .resize import FrameProcessor
from .scheduler import Scheduler, Clock
from .utils import log


class Player:
    def __init__(self, path, sink, box, mode="contain", quality="smooth",
                 loop=False):
        self.path = path
        self.sink = sink
        self.box = (int(box[0]), int(box[1]))
        self.mode = mode
        self.quality = quality
        self.loop = loop

        self.clock = Clock()
        self.stats = None
        self._interval = 1.0 / 25.0  # frame interval; set once the decoder opens
        self._position = 0.0         # pts of the last shown frame (for rel. seek)
        try:
            self._duration = probe(path).duration or 0.0
        except Exception:
            self._duration = 0.0     # unknown; percentage seeks clamp to 0

        # One Condition guards the transport state and parks the playback loop
        # while paused — no lost wakeups, and zero CPU when idle (cond.wait
        # releases the lock and blocks rather than polling).
        self._cond = threading.Condition()
        self._paused = False
        self._stop = threading.Event()      # end playback for good
        self._step = threading.Event()      # advance one frame while paused
        self._pending = None                # a seek target (seconds), or None

    # --- transport ---------------------------------------------------------
    @property
    def paused(self):
        return self._paused

    @property
    def position(self):
        """Playback position in seconds (pts of the last shown frame)."""
        return self._position

    @property
    def duration(self):
        return self._duration

    def pause(self):
        with self._cond:
            if not self._paused:
                self.clock.pause()
                self._paused = True  # no notify: we *want* the loop to park

    def resume(self):
        with self._cond:
            if self._paused:
                self.clock.resume()
                self._paused = False
                self._cond.notify_all()

    def toggle(self):
        self.resume() if self._paused else self.pause()

    def stop(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()  # wake the loop if parked in a pause

    def seek(self, seconds):
        """Jump to an absolute position (seconds), clamped to the video. Resumes
        if paused, so the target frame is shown rather than sat behind a pause."""
        target = max(0.0, seconds)
        if self._duration:
            target = min(target, self._duration)
        with self._cond:
            self._pending = target
            if self._paused:  # un-pause so the loop applies the seek and plays
                self.clock.resume()
                self._paused = False
            self._cond.notify_all()

    def seek_relative(self, delta):
        """Jump `delta` seconds from the current position (negative = back)."""
        self.seek(self._position + delta)

    def seek_percent(self, fraction):
        """Jump to a fraction (0..1) of the total duration."""
        self.seek(self._duration * max(0.0, min(1.0, fraction)))

    def restart(self):
        """Replay from the beginning — just a seek to 0 (also un-pauses)."""
        self.seek(0.0)

    def step(self):
        """Advance exactly one frame, staying paused. No-op unless paused."""
        with self._cond:
            if self._paused:
                self._step.set()
                self._cond.notify_all()

    def _should_break(self):
        """End the current frame-loop pass on stop, or when a seek is pending."""
        return self._stop.is_set() or self._pending is not None

    def _take_pending(self):
        with self._cond:
            target, self._pending = self._pending, None
            return target

    def _wait_if_paused(self):
        """Park the playback loop while paused (clock already frozen); let a
        single frame through on a step request, then park again."""
        with self._cond:
            while (self._paused and not self._stop.is_set()
                   and self._pending is None):
                if self._step.is_set():
                    self._step.clear()
                    # Nudge the clock so the held frame's deadline has arrived:
                    # it presents right away instead of sleeping, then we re-park.
                    self.clock.advance(self._interval)
                    return
                self._cond.wait()

    # --- playback ----------------------------------------------------------
    def play(self):
        """Run to completion (or until stop()); returns the final Stats.

        The decoder stays open for the whole session: each pass either plays to
        EOF, loops, or repositions to a pending seek and replays from there. The
        sink is always closed — even on error — so a fullscreen/alt-screen sink
        can never leave the terminal wedged.
        """
        self.sink.open(self.box)
        try:
            with Decoder(self.path) as dec:
                self._duration = dec.info.duration or self._duration
                proc = FrameProcessor(self.box, self.mode, self.quality)
                sched = Scheduler(dec.info.avg_fps)
                self._interval = sched.interval

                def present(frame):
                    self._position = frame.pts
                    self.sink.show(proc.process(frame.av_frame), frame.pts)

                while not self._stop.is_set():
                    target = self._take_pending()
                    if target is not None:
                        dec.seek(target)
                    self.clock.reset()
                    self.stats = sched.run(
                        dec.frames(), present, clock=self.clock,
                        should_stop=self._should_break,
                        pause_gate=self._wait_if_paused,
                    )
                    if self._stop.is_set():
                        break
                    if self._pending is not None:
                        continue  # a seek/restart landed: reposition and replay
                    if self.loop:
                        with self._cond:
                            self._pending = 0.0  # natural EOF: loop to the start
                        continue
                    break
        finally:
            self.sink.close()
        return self.stats
