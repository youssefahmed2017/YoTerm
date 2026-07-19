"""Zones — styled rectangular regions for YoTerm's `ESC ] YT ; zone` sequence.

A zone is just a rectangle with rendering properties. YoTerm never asks whether
it's a button, a card or a dialog; an application describes geometry and style,
and the GPU draws it. That's what keeps the protocol from ever needing a
`YT;button` or `YT;window`.

See docs/zones.md for the full design. This module is deliberately
shell-agnostic: it holds the model and the parsing, nothing that touches GL.
"""

from ytseq import make_gradient, parse_color

# Styling fields carry their full definition from Phase 1 even though the
# renderer only draws some of them yet. Freezing the protocol early means an
# application can send `radius:8` before rounded corners land and simply not see
# them, rather than getting a parse error it has to code around later.
_DEFAULTS = {
    "z": 0,
    "radius": 0.0,
    "bg": None,
    "border": None,
    "border_w": 0.0,
    "gradient": None,
    "angle": 0.0,
    "shadow": 0.0,
    "opacity": 1.0,
    # Phase 4: cells whose row/col fall inside this zone are drawn as their
    # own scissored (and, for a rounded zone, shader-discarded) batch instead
    # of the normal full-screen one, so overflowing text is cut to the zone's
    # shape. See docs/zones.md.
    "clip": False,
}


class Zone:
    """One rectangular region, pinned to the grid.

    `top_line` is an *absolute* line number (Terminal.first_line_no based) like
    an image placement, not a screen row, so the zone scrolls with the text it
    belongs to and is dropped once that line falls out of scrollback.
    """

    __slots__ = ("id", "top_line", "x", "w", "h", "alt") + tuple(_DEFAULTS)

    def __init__(self, zone_id, top_line, x, w, h, alt=False):
        self.id = zone_id
        self.top_line = top_line
        self.x = x
        self.w = w
        self.h = h
        self.alt = alt
        for name, value in _DEFAULTS.items():
            setattr(self, name, value)


def _int(text, default=None):
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _float(text, default=None):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _truthy(text):
    return text.strip().lower() in ("on", "true", "1", "yes")


def _color(text):
    """A colour field, where the literal 'none' clears it."""
    if text is None or text.strip().lower() in ("none", "off", ""):
        return None
    return parse_color(text)


def apply_style(zone, opts):
    """Update a zone's styling from parsed `key:value` options.

    Only keys actually present are touched — that's what makes `update` a patch
    rather than a replace, and what lets an animation loop send just the field
    it's changing each frame.
    """
    if "z" in opts:
        zone.z = _int(opts["z"], zone.z)
    if "radius" in opts:
        zone.radius = max(0.0, _float(opts["radius"], zone.radius))
    if "bg" in opts:
        zone.bg = _color(opts["bg"])
    if "border" in opts:
        zone.border = _color(opts["border"])
    for key in ("border_w", "bw"):
        if key in opts:
            zone.border_w = max(0.0, _float(opts[key], zone.border_w))
    if "opacity" in opts:
        zone.opacity = min(1.0, max(0.0, _float(opts["opacity"], zone.opacity)))
    if "shadow" in opts:
        zone.shadow = max(0.0, _float(opts["shadow"], zone.shadow))
    if "clip" in opts:
        zone.clip = _truthy(opts["clip"])
    if "angle" in opts:
        zone.angle = _float(opts["angle"], zone.angle)
    if "gradient" in opts:
        # Stops are comma-separated: ';' already separates protocol fields.
        raw = opts["gradient"].strip()
        if raw.lower() in ("none", "off", ""):
            zone.gradient = None
        else:
            stops = [s for s in raw.split(",") if s.strip()]
            zone.gradient = make_gradient(stops, {"angle": str(zone.angle)})


def geometry_from(opts, zone=None):
    """Pull x/w/h (and the raw row y) out of options, falling back to a zone's
    current values. Returns (x, y_or_None, w, h)."""
    x = _int(opts.get("x"), zone.x if zone else 0)
    w = _int(opts.get("w"), zone.w if zone else 1)
    h = _int(opts.get("h"), zone.h if zone else 1)
    y = _int(opts.get("y"))  # None means "leave the anchor where it is"
    return max(0, x), y, max(1, w), max(1, h)
