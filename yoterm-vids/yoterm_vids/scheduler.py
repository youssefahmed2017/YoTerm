"""Frame scheduling — produce frames at the video's intended speed.

The design is deliberately drift-free: every frame's deadline is computed as
`clock start + frame.pts`, always from the frame's own presentation timestamp,
never by adding up per-frame sleeps. Rounding error in any one sleep therefore
can't accumulate — a frame 3 ms late doesn't push the next one 3 ms late too.

When the pipeline falls behind (slow decode, a busy machine), frames whose
moment has already passed by more than one interval are *dropped* rather than
shown late, so playback catches back up to the wall clock instead of drifting
into slow-motion.
"""

import time
from dataclasses import dataclass

from .utils import log

perf = time.perf_counter

# How long a tail to busy-spin at the end of a sleep for pacing tightness.
# Kept small on purpose: spinning burns CPU (and battery), and because deadlines
# are absolute, a fraction of a millisecond of sleep jitter self-corrects on the
# next frame instead of accumulating — so we don't need to spin much to stay
# visually locked to the clock. 0.5 ms tail ≈ 1% of a core at 24 fps.
SPIN_MARGIN = 0.0005


def precise_sleep(seconds):
    """Sleep `seconds` accurately without needlessly burning CPU.

    Python 3.11's `time.sleep` already uses a high-resolution timer on Windows
    (~0.5–1 ms), so we sleep for all but a tiny tail and only briefly spin to
    absorb the OS scheduler's overshoot. Keeping the spin short is a deliberate
    battery choice — see SPIN_MARGIN.
    """
    if seconds <= 0:
        return
    end = perf() + seconds
    coarse = seconds - SPIN_MARGIN
    if coarse > 0:
        time.sleep(coarse)
    while perf() < end:
        pass


class Clock:
    """A monotonic, pausable playback clock, anchored when created.

    `now()` is seconds of playback elapsed, *excluding* any paused time — so a
    frame scheduled for pts=3.0 still shows at playback-time 3.0 no matter how
    long the video sat paused. That's what keeps the timeline drift-free across
    pauses (and, later, seeks) instead of the pause bleeding into every deadline.
    """

    def __init__(self):
        self._start = perf()
        self._paused_at = None

    def now(self):
        if self._paused_at is not None:
            return self._paused_at - self._start
        return perf() - self._start

    def pause(self):
        if self._paused_at is None:
            self._paused_at = perf()

    def resume(self):
        if self._paused_at is not None:
            # Shift the anchor forward by however long we were paused, so `now()`
            # picks up exactly where it froze.
            self._start += perf() - self._paused_at
            self._paused_at = None

    @property
    def paused(self):
        return self._paused_at is not None

    def reset(self):
        """Restart the clock from zero (used when playback loops/restarts)."""
        self._start = perf()
        self._paused_at = None


@dataclass
class Stats:
    shown: int = 0
    skipped: int = 0        # dropped because they were already stale
    late: int = 0           # shown, but past their deadline
    max_late: float = 0.0   # worst lateness among shown frames (seconds)
    wall: float = 0.0       # total wall-clock time the run took (seconds)


class Scheduler:
    """Paces a stream of frames against a Clock, calling `present` on each.

    `present(frame)` does the actual work (print, or emit a YT;img). The
    scheduler only decides *when* — or whether — to call it.
    """

    def __init__(self, fps, catch_up=True, max_consecutive_skips=None):
        self.interval = 1.0 / fps if fps else 1.0 / 25.0
        self.catch_up = catch_up
        # A safety valve: even under sustained overload, force a frame through
        # after this many drops so the picture never fully freezes. None = drop
        # as many stale frames as it takes (correct for brief hiccups).
        self.max_consecutive_skips = max_consecutive_skips

    def run(self, frames, present, clock=None, should_stop=None, pause_gate=None):
        """Drive `frames` through `present` in real time; return Stats.

        `should_stop()` (optional) is polled each frame so a caller can quit
        mid-playback. `pause_gate()` (optional) is called before each frame and
        should block while playback is paused — it's responsible for pausing the
        clock so paused time doesn't count against later deadlines.
        """
        clock = clock or Clock()
        stats = Stats()
        consecutive = 0
        first = True
        pts0 = 0.0  # pts that maps to clock time 0 (first frame, or seek target)

        for frame in frames:
            if should_stop is not None and should_stop():
                break
            if pause_gate is not None:
                pause_gate()
                if should_stop is not None and should_stop():
                    break  # a stop that arrived while we were paused

            if first:
                # Show the first frame immediately, THEN start the clock from
                # that moment. Container opening, first-frame decode and one-time
                # warmup (numpy/Pillow init) all happen during this first
                # present; anchoring the clock afterwards keeps that cost out of
                # the timeline so it can't make the next frames "late" and get
                # them dropped. pts0 also lets a stream that starts mid-timeline
                # (after a seek) pace correctly.
                present(frame)
                stats.shown += 1
                clock.reset()
                pts0 = frame.pts
                first = False
                continue

            target = frame.pts - pts0
            now = clock.now()
            lateness = now - target

            # Too late to be worth showing, and allowed to drop: skip it so the
            # timeline catches up instead of sliding into slow motion.
            can_skip = self.max_consecutive_skips is None or (
                consecutive < self.max_consecutive_skips
            )
            if self.catch_up and lateness > self.interval and can_skip:
                stats.skipped += 1
                consecutive += 1
                continue
            consecutive = 0

            if now < frame.pts:
                precise_sleep(frame.pts - now)
                lateness = clock.now() - frame.pts

            present(frame)
            stats.shown += 1
            if lateness > 0.001:
                stats.late += 1
                stats.max_late = max(stats.max_late, lateness)

        stats.wall = clock.now()
        log.debug(
            "scheduled: shown=%d skipped=%d late=%d max_late=%.1fms wall=%.3fs",
            stats.shown, stats.skipped, stats.late, stats.max_late * 1000,
            stats.wall,
        )
        return stats
