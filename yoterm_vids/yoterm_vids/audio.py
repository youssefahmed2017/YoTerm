"""Audio decode + A/V sync (M8).

Video is synced to *audio*, not the wall clock: audio plays at the sound card's
own fixed rate, and the video scheduler paces to how much audio has actually
been played. A late video frame is invisible; an audio underrun is not, so the
audio device is the master and the picture follows it.

Three pieces, all sink-agnostic (the actual device output is injected so the
engine keeps no dependency on any audio backend / Qt):

  * ``AudioDecoder`` — decodes a file's audio off its OWN container (so it never
    contends with the video ``Decoder``) and resamples it to one fixed device
    format, yielding raw PCM chunks.
  * ``AudioSink`` — the interface a device output implements (YoTerm supplies a
    QtMultimedia one). ``played_seconds()`` is the master clock.
  * ``AudioClock`` — adapts a sink's played position to the exact surface
    ``scheduler.Scheduler`` expects, so video pacing needs no other change.
"""

from fractions import Fraction

import av


class AudioDecoder:
    """Streams a file's audio as PCM at a fixed device format.

    Opens its own container (like the scrub-preview worker does) so audio decode
    never blocks or seeks the video path. Output is interleaved ``s16`` by
    default at the source's own sample rate, which every sound device accepts.
    """

    def __init__(self, path, rate=None, layout="stereo", fmt="s16"):
        self._container = av.open(path)
        self._stream = next(
            (s for s in self._container.streams if s.type == "audio"), None
        )
        if self._stream is None:
            self._container.close()
            raise ValueError("no audio stream")
        self._stream.thread_type = "AUTO"
        self.rate = int(rate or self._stream.rate or 44100)
        self.layout = layout
        self.fmt = fmt
        self.channels = 2 if layout == "stereo" else 1
        self._tb = self._stream.time_base or Fraction(1, 1000)
        self._resampler = self._new_resampler()

    def _new_resampler(self):
        return av.AudioResampler(format=self.fmt, layout=self.layout, rate=self.rate)

    def frames(self):
        """Yield ``(pcm_bytes, pts_seconds)`` for each resampled chunk to EOF.

        ``pts`` is best-effort (the master clock is the *sink*, not this); it's
        handy for logging and lining up a seek. Bytes are interleaved samples in
        ``fmt`` — ready to hand straight to a device.
        """
        for frame in self._container.decode(self._stream):
            pts = (
                float(frame.pts * frame.time_base)
                if frame.pts is not None and frame.time_base
                else 0.0
            )
            for r in self._resampler.resample(frame):
                yield r.to_ndarray().tobytes(), pts

    def seek(self, seconds):
        """Reposition to ``seconds`` (keyframe-backward), flushing the resampler
        so no pre-seek samples leak into the first post-seek chunk."""
        ts = int(max(0.0, seconds) / self._tb)
        self._container.seek(ts, stream=self._stream, backward=True, any_frame=False)
        self._resampler = self._new_resampler()

    def close(self):
        try:
            self._container.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class AudioSink:
    """Interface for a device audio output. The engine feeds PCM and reads the
    played position; a concrete sink (e.g. YoTerm's QtMultimedia one) does the
    real output. All methods are optional no-ops here so a partial sink still
    works and so this doubles as documentation.
    """

    def open(self, rate, channels):
        """Prepare the device for interleaved s16 at ``rate``/``channels``."""

    def write(self, pcm):
        """Enqueue interleaved-s16 PCM bytes (may block on backpressure)."""

    def played_seconds(self):
        """Seconds of audio the device has actually played — the master clock."""
        return 0.0

    def pause(self):
        """Freeze playback (the played clock should stop advancing)."""

    def resume(self):
        """Resume playback."""

    def flush(self):
        """Drop everything buffered-but-unplayed (used on seek)."""

    def set_muted(self, muted):
        """Silence/unsilence output without changing the played clock."""

    def close(self):
        """Stop and release the device."""


class AudioClock:
    """A master clock backed by a sink's played position.

    Presents the same surface ``scheduler.Scheduler`` uses on ``scheduler.Clock``
    (``now``/``pause``/``resume``/``reset``/``advance``), so the scheduler paces
    video against real audio playback without any change of its own. ``now()`` is
    audio-seconds since the last anchor, exactly like the wall clock is
    wall-seconds since its anchor — the timeline just comes from the sound card.
    """

    def __init__(self, sink):
        self._sink = sink
        self._baseline = sink.played_seconds()
        self._paused_at = None

    def now(self):
        if self._paused_at is not None:
            return self._paused_at - self._baseline
        return self._sink.played_seconds() - self._baseline

    def pause(self):
        if self._paused_at is None:
            self._paused_at = self._sink.played_seconds()

    def resume(self):
        # Shift the anchor by however far the sink's counter moved while paused,
        # so now() picks up exactly where it froze whether or not the device kept
        # counting (it shouldn't, but this is robust either way).
        if self._paused_at is not None:
            self._baseline += self._sink.played_seconds() - self._paused_at
            self._paused_at = None

    @property
    def paused(self):
        return self._paused_at is not None

    def advance(self, dt):
        """Jump playback time forward by ``dt`` (frame-step while paused)."""
        self._baseline -= dt

    def reset(self):
        """Re-anchor to the current played position (loop/restart/seek)."""
        self._baseline = self._sink.played_seconds()
        self._paused_at = None
