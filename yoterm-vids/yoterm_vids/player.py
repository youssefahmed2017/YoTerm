"""The playback engine: decode → process → schedule → sink.

`Player` ties the four stages together and adds transport control (pause, resume,
stop, loop) on top of the pausable Clock. It's deliberately sink-agnostic and
thread-friendly: `play()` runs the loop (typically on a worker thread) while
`pause()`/`toggle()`/`stop()` are called from another thread — YoTerm's UI
thread, or the CLI's keyboard reader.
"""

import threading

from .decoder import Decoder
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
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._resumed = threading.Event()
        self._resumed.set()  # start un-paused

    # --- transport ---------------------------------------------------------
    @property
    def paused(self):
        return not self._resumed.is_set()

    def pause(self):
        with self._lock:
            if self._resumed.is_set():
                self.clock.pause()
                self._resumed.clear()

    def resume(self):
        with self._lock:
            if not self._resumed.is_set():
                self.clock.resume()
                self._resumed.set()

    def toggle(self):
        self.resume() if self.paused else self.pause()

    def stop(self):
        self._stop.set()
        self._resumed.set()  # wake the loop if it's parked in a pause

    def _wait_if_paused(self):
        """Block the playback loop while paused (clock already frozen).

        An *untimed* wait: a paused player parks on the event and consumes zero
        CPU until resume() or stop() sets it — no polling, so pausing a video
        genuinely idles the machine rather than spinning a wakeup loop.
        """
        while self.paused and not self._stop.is_set():
            self._resumed.wait()

    # --- playback ----------------------------------------------------------
    def play(self):
        """Run to completion (or until stop()); returns the final Stats.

        Opens the sink once, replays the decode→sink pipeline each loop pass,
        and always closes the sink — even on error — so a fullscreen/alt-screen
        sink can never leave the terminal wedged.
        """
        self.sink.open(self.box)
        try:
            while not self._stop.is_set():
                self.clock.reset()
                self._play_once()
                if not self.loop:
                    break
        finally:
            self.sink.close()
        return self.stats

    def _play_once(self):
        with Decoder(self.path) as dec:
            proc = FrameProcessor(self.box, self.mode, self.quality)
            sched = Scheduler(dec.info.avg_fps)

            def present(frame):
                image = proc.process(frame.av_frame)
                self.sink.show(image, frame.pts)

            self.stats = sched.run(
                dec.frames(),
                present,
                clock=self.clock,
                should_stop=self._stop.is_set,
                pause_gate=self._wait_if_paused,
            )
        log.debug("play_once done: %s", self.stats)
