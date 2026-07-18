"""YoTerm's own OSC sequences (the `ESC ] YT ; ... ST` namespace).

These are features a GPU terminal can do honestly that a cell-stepped ANSI
terminal can only fake: true per-vertex gradients, real images. This module
holds the *parsing* and the colour math, so term.py (which parses the byte
stream) and app.py (which renders) share one definition and can't drift.

Any terminal that isn't YoTerm swallows an unknown OSC whole, so a gradient
degrades to plain text and an image to nothing — never to escape-code garbage
on the screen. That graceful fallback is the reason these live in OSC.
"""

# The 16 standard xterm colours, so a stop can be written as a plain SGR code
# (31, 93, ...) the way you'd write it in an ordinary colour escape. Kept local
# rather than imported from term.py to avoid a circular import.
_SGR16 = {
    30: (0, 0, 0),
    31: (128, 0, 0),
    32: (0, 128, 0),
    33: (128, 128, 0),
    34: (0, 0, 128),
    35: (128, 0, 128),
    36: (0, 128, 128),
    37: (192, 192, 192),
    90: (128, 128, 128),
    91: (255, 0, 0),
    92: (0, 255, 0),
    93: (255, 255, 0),
    94: (0, 0, 255),
    95: (255, 0, 255),
    96: (0, 255, 255),
    97: (255, 255, 255),
}

# A handful of friendly names, so `gradient;red;orange` reads the way it means.
_NAMES = {
    "black": (0, 0, 0),
    "red": (222, 56, 43),
    "green": (57, 181, 74),
    "yellow": (255, 199, 6),
    "blue": (0, 111, 184),
    "magenta": (118, 38, 113),
    "cyan": (44, 181, 233),
    "white": (204, 204, 204),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "orange": (255, 128, 0),
    "purple": (150, 0, 190),
    "pink": (255, 105, 180),
    "teal": (0, 128, 128),
    "lime": (0, 255, 0),
}


def parse_color(text):
    """A gradient stop -> (r, g, b) floats in 0..1, or None if unparseable.

    Accepts `#rgb`, `#rrggbb`, an SGR colour code (`31`, `93`), or a name.
    """
    text = text.strip().lower()
    if not text:
        return None
    if text.startswith("#"):
        h = text[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            try:
                return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
            except ValueError:
                return None
        return None
    if text.isdigit():
        rgb = _SGR16.get(int(text))
        return tuple(c / 255.0 for c in rgb) if rgb else None
    rgb = _NAMES.get(text)
    return tuple(c / 255.0 for c in rgb) if rgb else None


def _truthy(v):
    return v.strip().lower() in ("on", "true", "1", "yes")


class GradientRun:
    """A parsed `YT;gradient` state. Cells written while it's active share one
    of these objects, so the renderer can group them and interpolate a single
    smooth ramp across the whole run's bounding box — not per cell."""

    __slots__ = ("stops", "angle", "cycle", "speed", "target")

    def __init__(self, stops, angle=0.0, cycle=False, speed=0.5, target="fg"):
        # stops: sorted list of (pos_0_1, (r, g, b)).
        self.stops = stops
        self.angle = angle
        self.cycle = cycle
        self.speed = speed
        self.target = target

    def color_at(self, t):
        """Sample the ramp at t in 0..1 (piecewise-linear between stops)."""
        stops = self.stops
        if t <= stops[0][0]:
            return stops[0][1]
        if t >= stops[-1][0]:
            return stops[-1][1]
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                f = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
                return (
                    c0[0] + (c1[0] - c0[0]) * f,
                    c0[1] + (c1[1] - c0[1]) * f,
                    c0[2] + (c1[2] - c0[2]) * f,
                )
        return stops[-1][1]


def make_gradient(stop_texts, opts):
    """Build a GradientRun from raw stop strings and a key:value options dict,
    or return None if there aren't enough usable colours."""
    colors = []
    for raw in stop_texts:
        pos = None
        if "@" in raw:
            raw, _, ptxt = raw.partition("@")
            try:
                pos = float(ptxt)
            except ValueError:
                pos = None
        rgb = parse_color(raw)
        if rgb is not None:
            colors.append((pos, rgb))
    if not colors:
        return None
    if len(colors) == 1:
        colors.append(colors[0])  # a single colour is just a flat "gradient"

    # Fill in any positions the caller didn't pin, spreading them evenly.
    n = len(colors)
    stops = []
    for i, (pos, rgb) in enumerate(colors):
        if pos is None:
            pos = i / (n - 1)
        stops.append((max(0.0, min(1.0, pos)), rgb))
    stops.sort(key=lambda s: s[0])

    try:
        angle = float(opts.get("angle", 0.0))
    except ValueError:
        angle = 0.0
    try:
        speed = float(opts.get("speed", 0.5))
    except ValueError:
        speed = 0.5
    target = opts.get("target", "fg").strip().lower()
    if target not in ("fg", "bg"):
        target = "fg"
    return GradientRun(
        stops,
        angle=angle,
        cycle=_truthy(opts.get("cycle", "")),
        speed=speed,
        target=target,
    )
