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
import time

from .audio import AudioClock, AudioDecoder
from .decoder import Decoder, has_audio, probe
from .resize import FrameProcessor
from .scheduler import Scheduler, Clock
from .utils import log

# How much decoded audio to keep queued in the sink. Enough to ride out decode
# jitter without adding perceptible A/V latency or a laggy response to seek.
_AUDIO_BUFFER_TARGET = 0.25


class Player:
    def __init__(
        self,
        path,
        sink,
        box,
        mode="contain",
        quality="smooth",
        loop=False,
        audio_sink=None,
    ):
        self.path = path
        self.sink = sink
        self.box = (int(box[0]), int(box[1]))
        self.mode = mode
        self.quality = quality
        self.loop = loop
        # Optional device audio output (an audio.AudioSink). When present and the
        # file has audio, playback becomes audio-master: video paces to it.
        self.audio_sink = audio_sink

        self.clock = Clock()
        self.stats = None
        self._interval = 1.0 / 25.0  # frame interval; set once the decoder opens
        self._position = 0.0  # pts of the last shown frame (for rel. seek)
        try:
            self._duration = probe(path).duration or 0.0
        except Exception:
            self._duration = 0.0  # unknown; percentage seeks clamp to 0

        # One Condition guards the transport state and parks the playback loop
        # while paused — no lost wakeups, and zero CPU when idle (cond.wait
        # releases the lock and blocks rather than polling).
        self._cond = threading.Condition()
        self._paused = False
        self._stop = threading.Event()  # end playback for good
        self._step = threading.Event()  # advance one frame while paused
        self._pending = None  # a seek target (seconds), or None

        # Audio feed coordination (only used when audio is active). The video
        # loop drives every reposition (seek + loop) by bumping _audio_epoch;
        # the feed thread follows.
        self._audio_on = False
        self._audio_thread = None
        self._audio_target = 0.0
        self._audio_epoch = 0
        self._pending_box = None  # a resize target box, applied next frame

    def resize(self, box):
        """Change the output box mid-playback (e.g. fullscreen). The next frame
        is decoded/scaled at the new size."""
        self._pending_box = (max(1, int(box[0])), max(1, int(box[1])))

    def _audio_reposition(self, seconds):
        """Tell the audio feed thread to (re)start decoding at `seconds`."""
        with self._cond:
            self._audio_target = seconds
            self._audio_epoch += 1
            self._cond.notify_all()

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
                if self._audio_on:
                    # Suspend the device: its played clock freezes (so the video
                    # clock, which reads it, freezes too) and the feed thread
                    # parks on backpressure once the buffer stops draining.
                    self.audio_sink.pause()

    def resume(self):
        with self._cond:
            if self._paused:
                self.clock.resume()
                self._paused = False
                if self._audio_on:
                    self.audio_sink.resume()
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
                if self._audio_on:
                    self.audio_sink.resume()
            self._cond.notify_all()
        if self._audio_on:
            self._audio_reposition(target)  # feed thread flushes + repositions

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
            while self._paused and not self._stop.is_set() and self._pending is None:
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
        # Audio-master when we have a sink and the file actually has sound: the
        # video clock becomes the audio device's played position, and a feed
        # thread keeps the device fed. Otherwise the wall Clock drives, exactly
        # as before (video-only files and the CLI are unchanged).
        self._audio_on = self.audio_sink is not None and has_audio(self.path)
        if self._audio_on:
            self.clock = AudioClock(self.audio_sink)
            self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
            self._audio_thread.start()
        try:
            with Decoder(self.path) as dec:
                self._duration = dec.info.duration or self._duration
                # The sink picks the pixel form it wants (GPU wants raw RGBA,
                # the CLI wants an RGB PIL image to JPEG-encode); default RGB.
                out = getattr(self.sink, "pixel_format", "rgb")
                proc = [FrameProcessor(self.box, self.mode, self.quality, out=out)]
                sched = Scheduler(dec.info.avg_fps)
                self._interval = sched.interval

                def present(frame):
                    # A resize (e.g. entering fullscreen) rebuilds the processor
                    # so subsequent frames are decoded/scaled at the new box --
                    # crisp, not an upscaled small frame.
                    box = self._pending_box
                    if box is not None:
                        self._pending_box = None
                        self.box = box
                        proc[0] = FrameProcessor(box, self.mode, self.quality, out=out)
                    self._position = frame.pts
                    self.sink.show(proc[0].process(frame.av_frame), frame.pts)

                while not self._stop.is_set():
                    target = self._take_pending()
                    if target is not None:
                        dec.seek(target)
                        if self._audio_on:
                            self._audio_reposition(target)  # keep audio in step
                    self.clock.reset()
                    self.stats = sched.run(
                        dec.frames(),
                        present,
                        clock=self.clock,
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
            # Playback is over: stop the audio feed and release its device.
            self._stop.set()
            with self._cond:
                self._cond.notify_all()
            if self._audio_thread is not None:
                self._audio_thread.join(timeout=2.0)
            if self._audio_on:
                try:
                    self.audio_sink.close()
                except Exception:
                    pass
            self.sink.close()
        return self.stats

    def _audio_loop(self):
        """Feed the audio device off its own decoder, following the video loop's
        repositions (seek/loop) and idling on backpressure (which is also how it
        parks while paused: the device is suspended, so its buffer never drains).
        """
        try:
            dec = AudioDecoder(self.path)
        except Exception as exc:  # no audio / unreadable: silently play muted
            log.debug("audio feed disabled: %s", exc)
            self._audio_on = False
            return
        self.audio_sink.open(dec.rate, dec.channels)
        served = self._audio_epoch
        frames = dec.frames()
        try:
            while not self._stop.is_set():
                with self._cond:
                    epoch, target = self._audio_epoch, self._audio_target
                if epoch != served:  # a seek or loop landed
                    dec.seek(target)
                    self.audio_sink.flush()
                    frames = dec.frames()
                    served = epoch
                    continue
                if self.audio_sink.buffered_seconds() >= _AUDIO_BUFFER_TARGET:
                    with self._cond:
                        self._cond.wait(0.01)  # buffer full (or paused): idle
                    continue
                chunk = next(frames, None)
                if chunk is None:  # audio EOF: wait for the video loop to loop
                    with self._cond:
                        self._cond.wait(0.03)
                    continue
                self.audio_sink.write(chunk[0])
        finally:
            dec.close()
