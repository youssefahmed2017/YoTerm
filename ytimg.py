"""Decode and size images for YoTerm's `ESC ] YT ; img` sequence.

Kept apart from term.py (which parses the byte stream) and app.py (which uploads
to the GPU) so the Pillow dependency and the sizing math live in one place. A
placement is described in *cells* — the terminal is a grid — but the sizing has
to respect the image's pixel aspect ratio, so it needs to know how many pixels a
cell is, which the app supplies via Terminal.cell_px.
"""

import base64
import io
import math

try:
    from PIL import Image
except ImportError:  # the terminal still runs; images just no-op
    Image = None

# Cap the texture we ever upload, so one stray 8000px screenshot can't hand the
# GPU a giant allocation. Aspect is preserved when clamping.
MAX_DIM = 2048


def load_image(path=None, data=None):
    """Decode a source into (rgba_bytes, width, height), or None on any failure.

    Failure is always silent-None rather than an exception: a bad path or a
    truncated base64 blob must never take the terminal down — the sequence just
    draws nothing, exactly as it would on a terminal that doesn't support it.
    """
    if Image is None:
        return None
    try:
        if data is not None:
            image = Image.open(io.BytesIO(base64.b64decode(data)))
        elif path is not None:
            image = Image.open(path)
        else:
            return None
        image = image.convert("RGBA")
    except Exception:
        return None

    w, h = image.size
    if w <= 0 or h <= 0:
        return None
    scale = min(1.0, MAX_DIM / max(w, h))
    if scale < 1.0:
        w, h = max(1, int(w * scale)), max(1, int(h * scale))
        image = image.resize((w, h), Image.LANCZOS)
    return image.tobytes(), w, h


def _to_px(value, per_cell):
    """A size field -> pixels. '240px' is literal; a bare '24' is 24 cells."""
    value = value.strip().lower()
    try:
        if value.endswith("px"):
            return float(value[:-2])
        return float(value) * per_cell
    except ValueError:
        return None


def _int(value):
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return None


def fit_cells(iw, ih, cw, ch, opts, max_cols, max_rows):
    """Work out the (cols, rows) box an image should occupy.

    Priority: an explicit cols+rows wins; otherwise a given width or height sets
    one side and the image's aspect ratio sets the other; otherwise the image's
    native pixel size is used. The result is clamped into (max_cols, max_rows)
    with the aspect preserved, so an image can never reserve more grid than
    there is.
    """
    cols = _int(opts.get("cols"))
    rows = _int(opts.get("rows"))
    if cols and rows:
        box_w, box_h = cols * cw, rows * ch
    else:
        w_px = _to_px(opts["w"], cw) if "w" in opts else None
        h_px = _to_px(opts["h"], ch) if "h" in opts else None
        aspect = iw / ih
        if w_px:
            box_w, box_h = w_px, w_px / aspect
        elif h_px:
            box_h, box_w = h_px, h_px * aspect
        elif cols:
            box_w, box_h = cols * cw, (cols * cw) / aspect
        elif rows:
            box_h, box_w = rows * ch, (rows * ch) * aspect
        else:
            box_w, box_h = float(iw), float(ih)
        cols = max(1, math.ceil(box_w / cw))
        rows = max(1, math.ceil(box_h / ch))

    # Clamp into the grid, keeping aspect.
    if cols > max_cols:
        rows = max(1, round(rows * max_cols / cols))
        cols = max_cols
    if rows > max_rows:
        cols = max(1, round(cols * max_rows / rows))
        rows = max_rows
    return cols, rows


class ImagePlacement:
    """One image pinned to the grid.

    `top_line` is an *absolute* line number (Terminal.first_line_no based), not a
    screen row, so the image scrolls with its text and is dropped once that line
    falls out of scrollback. The raw RGBA rides along for the app to upload; the
    app caches the GPU texture by this object's identity.
    """

    __slots__ = (
        "id",
        "top_line",
        "left",
        "cols",
        "rows",
        "rgba",
        "iw",
        "ih",
        "alt",
        "fit",
        "rev",
    )

    def __init__(
        self, img_id, top_line, left, cols, rows, rgba, iw, ih, alt, fit="contain"
    ):
        self.id = img_id
        self.top_line = top_line
        self.left = left
        self.cols = cols
        self.rows = rows
        self.rgba = rgba
        self.iw = iw
        self.ih = ih
        self.alt = alt  # placed on the alternate screen?
        self.fit = fit  # 'contain' (letterbox) or 'fill' (stretch to the box)
        # Bumped whenever `rgba` is swapped in place (a video frame updating its
        # placement). The renderer re-uploads the texture when this changes, even
        # though the placement object stays the same identity.
        self.rev = 0
