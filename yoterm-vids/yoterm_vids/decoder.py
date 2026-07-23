"""Video decoding via PyAV.

`probe()` is a one-shot metadata read. `Decoder` holds a container open and
streams decoded frames, each tagged with a presentation timestamp (seconds from
the start), and stops cleanly at end-of-file. Milestone 2 schedules these
against a clock; Milestone 3 converts the raw frames to RGB.
"""

import os
from dataclasses import dataclass
from fractions import Fraction

import av

from .utils import log


@dataclass
class VideoInfo:
    path: str
    file_bytes: int
    container_format: str
    duration: float | None        # seconds
    width: int
    height: int
    codec: str
    pix_fmt: str | None
    avg_fps: float | None
    frame_count: int | None       # None if the container doesn't report it
    bit_rate: int | None          # bits/sec


@dataclass
class Frame:
    """One decoded frame, still in the codec's native pixel format.

    `pts` is the presentation time in seconds from the start of the stream — the
    scheduler (M2) waits until the clock reaches it; the resizer (M3) turns
    `av_frame` into RGB. `index` is a simple monotonic counter for logging.
    """

    index: int
    pts: float
    av_frame: av.VideoFrame


def _fps(stream):
    """A stream's frame rate as a float, preferring the averaged rate."""
    rate = stream.average_rate or stream.base_rate or stream.guessed_rate
    return float(rate) if rate else None


def _video_stream(container):
    stream = next((s for s in container.streams if s.type == "video"), None)
    if stream is None:
        raise ValueError("no video stream found")
    return stream


def _read_info(container, stream, path, file_bytes):
    """Assemble a VideoInfo from an already-open container + video stream."""
    cc = stream.codec_context
    duration = None
    if stream.duration is not None and stream.time_base:
        duration = float(stream.duration * stream.time_base)
    elif container.duration is not None:
        duration = container.duration / av.time_base

    fps = _fps(stream)
    frame_count = stream.frames or None
    if not frame_count and duration and fps:
        frame_count = round(duration * fps)

    return VideoInfo(
        path=path,
        file_bytes=file_bytes,
        container_format=container.format.name,
        duration=duration,
        width=cc.width,
        height=cc.height,
        codec=cc.name,
        pix_fmt=cc.pix_fmt,
        avg_fps=fps,
        frame_count=frame_count,
        bit_rate=stream.bit_rate or container.bit_rate or None,
    )


def probe(path):
    """Open `path`, return a VideoInfo, and close again.

    Raises FileNotFoundError / ValueError / av.FFmpegError on an unreadable file
    so the CLI can report a clean message rather than a traceback.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    file_bytes = os.path.getsize(path)
    with av.open(path) as container:
        stream = _video_stream(container)
        info = _read_info(container, stream, path, file_bytes)
    log.debug("probed %s: %sx%s %s", path, info.width, info.height, info.codec)
    return info


class Decoder:
    """Streams decoded video frames from a file.

    Use as a context manager so the container is always closed:

        with Decoder(path) as dec:
            for frame in dec.frames():
                ...
    """

    def __init__(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.path = path
        self._container = av.open(path)
        try:
            self._stream = _video_stream(self._container)
        except Exception:
            self._container.close()
            raise
        # Let FFmpeg use every core it can; frames still arrive in order.
        self._stream.thread_type = "AUTO"
        self.info = _read_info(
            self._container, self._stream, path, os.path.getsize(path)
        )
        self._time_base = self._stream.time_base or Fraction(1, 1000)
        self._fps = self.info.avg_fps or 25.0

    def _pts_seconds(self, frame, index):
        """Frame presentation time in seconds, with sensible fallbacks.

        Prefer the container's own timestamp (`frame.time`); if a frame carries
        no pts (rare, but legal), synthesize one from the index and frame rate so
        the timeline never stalls or goes backwards.
        """
        if frame.time is not None:
            return float(frame.time)
        if frame.pts is not None:
            return float(frame.pts * self._time_base)
        return index / self._fps

    def frames(self):
        """Yield Frame objects in presentation order until EOF.

        PyAV's `decode()` iterator flushes the decoder and terminates at
        end-of-file on its own, so EOF is simply the generator running out —
        no sentinel to check for.
        """
        index = 0
        for av_frame in self._container.decode(self._stream):
            yield Frame(index, self._pts_seconds(av_frame, index), av_frame)
            index += 1
        log.debug("decoded %d frames from %s", index, self.path)

    def close(self):
        if self._container is not None:
            self._container.close()
            self._container = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
