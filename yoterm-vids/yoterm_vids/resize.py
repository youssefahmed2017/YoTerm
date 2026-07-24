"""Turn a decoded frame into display-ready RGB pixels.

Two jobs: convert from the codec's native pixel format (usually YUV) to RGB, and
scale to the pixel box the video will occupy on screen. Scaling here matters for
*bandwidth*, not just looks — YoTerm samples the image on the GPU, so sending it
much larger than its on-screen size just bloats the base64 payload and slows
encoding for no visible gain.

Aspect ratio is preserved by default (`contain`); `fill` stretches and `cover`
crops, matching the vocabulary YoTerm's own `YT;img;fit:` uses.
"""

from PIL import Image
from av.video.reformatter import Interpolation

# Down-/up-sampling filters, cheapest → sharpest, mapped onto libswscale's own
# scalers (see FrameProcessor.process): video downscaling looks fine with
# BILINEAR and it's markedly cheaper than LANCZOS per frame; LANCZOS is offered
# for stills / slow playback where sharpness wins.
_FILTERS = {
    "nearest": Interpolation.POINT,
    "fast": Interpolation.BILINEAR,
    "smooth": Interpolation.LANCZOS,
}
_MODES = ("contain", "fill", "cover")


def target_dims(iw, ih, box_w, box_h, mode):
    """(scaled_w, scaled_h, crop_to_box?) for fitting iw×ih into the box.

    contain — largest size that fits inside the box, aspect kept (≤ box).
    fill    — exactly the box, aspect ignored (stretched).
    cover   — smallest size that covers the box, aspect kept, then centre-cropped.
    """
    if mode == "fill":
        return box_w, box_h, False
    if mode == "cover":
        scale = max(box_w / iw, box_h / ih)
    else:  # contain
        scale = min(box_w / iw, box_h / ih)
    w = max(1, round(iw * scale))
    h = max(1, round(ih * scale))
    return w, h, (mode == "cover")


class FrameProcessor:
    """Converts av frames to display-ready pixels sized for a fixed pixel box.

    `out` picks the output form for the two display paths, so each gets exactly
    the bytes it needs with no extra copy:
      "rgb"  -> an RGB PIL.Image (the CLI's EscapeSink JPEG-encodes it).
      "rgba" -> a ``(rgba_bytes, w, h)`` tuple straight from swscale, ready to
                upload as a GPU texture -- YoTerm's native path, no PIL at all.
    """

    def __init__(self, box, mode="contain", quality="smooth", out="rgb"):
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")
        self.box_w, self.box_h = int(box[0]), int(box[1])
        self.mode = mode
        self.interp = _FILTERS.get(quality, Interpolation.LANCZOS)
        self.out = out
        self._fmt = "rgba" if out == "rgba" else "rgb24"

    def process(self, av_frame):
        """Decoded frame → display-ready pixels (see `out`).

        The pixel-format conversion *and* the scale-to-box happen in a single
        libswscale pass (``av_frame.reformat``), rather than converting at full
        source resolution and then resizing again in Pillow. That fuses two
        passes into one and skips allocating a full-resolution buffer — for a
        1280×720 source into a ~672-wide box it's several times less CPU per
        frame, which is most of the per-frame cost. Downscaling on the GPU still
        does the final sub-pixel sampling, so swscale's scaler is plenty here.
        """
        iw, ih = av_frame.width, av_frame.height
        w, h, crop = target_dims(iw, ih, self.box_w, self.box_h, self.mode)
        if (w, h) != (iw, ih):
            conv = av_frame.reformat(width=w, height=h, format=self._fmt,
                                     interpolation=self.interp)
        else:  # already the right size: convert only
            conv = av_frame.reformat(format=self._fmt)
        arr = conv.to_ndarray()  # (h, w, 3) for rgb24, (h, w, 4) for rgba
        if crop:  # cover: trim the overflow so we land exactly on the box
            left = (w - self.box_w) // 2
            top = (h - self.box_h) // 2
            arr = arr[top:top + self.box_h, left:left + self.box_w]
        if self.out == "rgba":
            return arr.tobytes(), arr.shape[1], arr.shape[0]
        return Image.fromarray(arr, "RGB")
