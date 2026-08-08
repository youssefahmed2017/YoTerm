"""Small shared helpers: logging setup and human-readable formatting.

Logs go to *stderr* on purpose. stdout is the video channel — once playback
starts it carries the raw `YT;img` escape stream to the terminal, so nothing
else may be written there or it would corrupt the picture.
"""

import logging
import sys

log = logging.getLogger("yoterm_vids")


def setup_logging(verbose=False):
    """Route logs to stderr so stdout stays a clean image pipe."""
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s  %(message)s"))
    log.handlers.clear()
    log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False
    return log


def human_bytes(n):
    """1048576 -> '1.0 MiB'."""
    if n is None:
        return "?"
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < step:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} PiB"


def human_duration(seconds):
    """73.5 -> '1:13.5'; 3661 -> '1:01:01'."""
    if seconds is None:
        return "?"
    seconds = float(seconds)
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    frac = seconds - int(seconds)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}{f'{frac:.1f}'[1:] if frac else ''}"
