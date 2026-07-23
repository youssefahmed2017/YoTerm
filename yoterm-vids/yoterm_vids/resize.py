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

# Down-/up-sampling filters, cheapest → sharpest. Video downscaling looks fine
# with BILINEAR and it's markedly faster than LANCZOS per frame; LANCZOS is
# offered for stills / slow playback where sharpness wins.
_FILTERS = {
    "nearest": Image.NEAREST,
    "fast": Image.BILINEAR,
    "smooth": Image.LANCZOS,
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
    """Converts av frames to RGB PIL images sized for a fixed pixel box."""

    def __init__(self, box, mode="contain", quality="smooth"):
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")
        self.box_w, self.box_h = int(box[0]), int(box[1])
        self.mode = mode
        self.filter = _FILTERS.get(quality, Image.LANCZOS)

    def process(self, av_frame):
        """Decoded frame → RGB PIL.Image sized for the box.

        `to_ndarray(format="rgb24")` runs libswscale to do the YUV→RGB
        conversion in C; the resize is Pillow so we get an explicit filter
        choice. (Fusing both into one swscale call is a Milestone 7 optimisation.)
        """
        arr = av_frame.to_ndarray(format="rgb24")
        ih, iw = arr.shape[0], arr.shape[1]
        img = Image.fromarray(arr, "RGB")

        w, h, crop = target_dims(iw, ih, self.box_w, self.box_h, self.mode)
        if (w, h) != (iw, ih):
            img = img.resize((w, h), self.filter)
        if crop:  # cover: trim the overflow so we land exactly on the box
            left = (w - self.box_w) // 2
            top = (h - self.box_h) // 2
            img = img.crop((left, top, left + self.box_w, top + self.box_h))
        return img
