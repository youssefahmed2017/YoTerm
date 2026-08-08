"""Frame sinks — where a processed frame goes to be shown.

The player is sink-agnostic: it decodes, paces and resizes, then hands each RGB
frame to a sink. That keeps the two very different display paths behind one
interface:

  * EscapeSink   — JPEG-encode + base64 + emit a `YT;img` sequence to a stream.
                   Drives the `yoterm-vids` CLI (and any YT-capable terminal).
  * CallbackSink — just call a function with (image, pts). YoTerm's native
                   `YT;vid` uses this to push pixels straight into its renderer.
"""

import base64
import io

from . import protocols
from .utils import log


def encode_jpeg(image, quality=80):
    """RGB PIL image -> JPEG bytes. JPEG (not PNG) because per-frame size drives
    both the base64 payload and encode time, and video tolerates its artefacts."""
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


class FrameSink:
    """Interface: open() once, show() per frame, close() once (always)."""

    def open(self, box):
        pass

    def show(self, image, pts):
        raise NotImplementedError

    def close(self):
        pass


class CallbackSink(FrameSink):
    """Forward each frame to a plain callable — the native/in-process path.

    `on_show(frame, pts)` receives whatever the requested `pixel_format` yields:
    "rgb" (default) hands over an RGB PIL image; "rgba" hands over a
    ``(rgba_bytes, w, h)`` tuple straight from swscale, which YoTerm's native
    player uploads to a GPU texture with no PIL/convert step. `on_open`/
    `on_close` are optional hooks.
    """

    def __init__(self, on_show, on_open=None, on_close=None, pixel_format="rgb"):
        self._on_show = on_show
        self._on_open = on_open
        self._on_close = on_close
        self.pixel_format = pixel_format

    def open(self, box):
        if self._on_open:
            self._on_open(box)

    def show(self, image, pts):
        self._on_show(image, pts)

    def close(self):
        if self._on_close:
            self._on_close()


class EscapeSink(FrameSink):
    """Emit frames as `YT;img` escape sequences to a text stream (stdout).

    Note: YoTerm caps OSC payloads at 1 KB, so a full JPEG frame won't fit
    through its parser — this sink targets other YT-capable terminals / tests
    and the visual-inspection path, not real YoTerm (which plays video natively).
    """

    def __init__(self, stream, cell_px=(8, 16), img_id=1, quality=80, fullscreen=True):
        self._stream = stream
        self._cw, self._ch = cell_px
        self._id = img_id
        self._quality = quality
        self._fullscreen = fullscreen
        self._cols = self._rows = 0

    def open(self, box):
        if self._fullscreen:
            self._write(protocols.enter_fullscreen())
            self._write(protocols.clear_screen())
        self._flush()
        log.debug(
            "EscapeSink open, box=%dx%d px, cell=%dx%d",
            box[0],
            box[1],
            self._cw,
            self._ch,
        )

    def show(self, image, pts):
        # Cell size from the ACTUAL image dimensions (already fitted to the box
        # at the video's aspect ratio), so fit:fill maps 1:1 and never stretches.
        cols = max(1, round(image.width / self._cw))
        rows = max(1, round(image.height / self._ch))
        data = base64.b64encode(encode_jpeg(image, self._quality)).decode("ascii")
        self._write(protocols.esc_home())
        self._write(protocols.yt_image(data, cols, rows, self._id))
        self._flush()  # push each frame out immediately so playback animates

    def close(self):
        self._write(protocols.yt_image_delete(self._id))
        if self._fullscreen:
            self._write(protocols.leave_fullscreen())
        self._flush()

    def _write(self, text):
        self._stream.write(text)

    def _flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass
