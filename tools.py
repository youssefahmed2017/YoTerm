import math
from array import array
from bisect import bisect_right
from PIL import Image, ImageFont, ImageDraw

# Unicode display-width table. These ranges are kept identical to the standard
# wcwidth tables (and to cozy_tui/_width.py) so YoTerm and any app it hosts
# agree cell-for-cell — a single disagreement shifts a column and cascades into
# overlapping output.
_ZERO_WIDTH = [
    (0x0300, 0x036F),
    (0x0483, 0x0489),
    (0x0591, 0x05BD),
    (0x05BF, 0x05BF),
    (0x0610, 0x061A),
    (0x064B, 0x065F),
    (0x0670, 0x0670),
    (0x06D6, 0x06DC),
    (0x06DF, 0x06E4),
    (0x0711, 0x0711),
    (0x0730, 0x074A),
    (0x07A6, 0x07B0),
    (0x0900, 0x0903),
    (0x093A, 0x094F),
    (0x0951, 0x0957),
    (0x0E31, 0x0E31),
    (0x0E34, 0x0E3A),
    (0x0EB1, 0x0EB1),
    (0x0EB4, 0x0EBC),
    (0x1AB0, 0x1AFF),
    (0x1DC0, 0x1DFF),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x20D0, 0x20FF),
    (0xFE00, 0xFE0F),
    (0xFE20, 0xFE2F),
    (0xFEFF, 0xFEFF),
]
_WIDE = [
    (0x1100, 0x115F),
    (0x2329, 0x232A),
    (0x2E80, 0x303E),
    (0x3041, 0x33FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xA000, 0xA4CF),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
    (0xFE10, 0xFE19),
    (0xFE30, 0xFE6F),
    (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6),
    (0x1F300, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x20000, 0x3FFFD),
]


def _flatten(ranges):
    edges = []
    for start, end in ranges:
        edges.append(start)
        edges.append(end + 1)
    return edges


_ZERO_EDGES = _flatten(_ZERO_WIDTH)
_WIDE_EDGES = _flatten(_WIDE)


def char_width(ch):
    """Terminal cell width of a character: 0 (combining/zero-width), 1, or 2."""
    cp = ord(ch)
    if cp == 0:
        return 0
    if cp < 32 or 0x7F <= cp < 0xA0:  # C0/C1 control chars
        return 0
    if cp < 0x300:  # all ASCII/Latin
        return 1
    if bisect_right(_ZERO_EDGES, cp) & 1:
        return 0
    if bisect_right(_WIDE_EDGES, cp) & 1:
        return 2
    return 1


# --- Font atlas layout -------------------------------------------------------
# We pack printable ASCII into a uniform grid: ATLAS_COLS glyphs per row,
# each glyph living in its own square cell. cell_uv() and build_font_atlas()
# must agree on these numbers.
ATLAS_FIRST = 32  # first ASCII code we render (space)
ATLAS_LAST = 126  # last  ASCII code we render (~)
ATLAS_COLS = 16  # glyphs per atlas row

CONSOLAS = r"C:\Windows\Fonts\consola.ttf"

# Consolas style variants, indexed by (bold?1:0) + (italic?2:0).
STYLE_FONTS = [
    r"C:\Windows\Fonts\consola.ttf",  # 0 regular
    r"C:\Windows\Fonts\consolab.ttf",  # 1 bold
    r"C:\Windows\Fonts\consolai.ttf",  # 2 italic
    r"C:\Windows\Fonts\consolaz.ttf",  # 3 bold italic
]

# Foreground colors as RGB floats (0..1), keyed by the names term.py
# stores in each cell's `fg`. The shader multiplies glyph coverage by
# this, so the same white atlas renders in any color.
PALETTE = {
    "default": (0.95, 0.95, 0.95),
    "black": (0.00, 0.00, 0.00),
    "red": (0.91, 0.36, 0.36),
    "green": (0.44, 0.78, 0.44),
    "yellow": (0.90, 0.80, 0.36),
    "blue": (0.40, 0.56, 0.95),
    "magenta": (0.82, 0.47, 0.86),
    "cyan": (0.40, 0.80, 0.85),
    "white": (0.95, 0.95, 0.95),
    # Bright variants (SGR 90-97 / 100-107).
    "bright_black": (0.50, 0.50, 0.50),
    "bright_red": (1.00, 0.50, 0.50),
    "bright_green": (0.55, 0.95, 0.55),
    "bright_yellow": (1.00, 0.95, 0.50),
    "bright_blue": (0.55, 0.70, 1.00),
    "bright_magenta": (0.95, 0.60, 1.00),
    "bright_cyan": (0.55, 0.95, 1.00),
    "bright_white": (1.00, 1.00, 1.00),
}


def _atlas_slots(first=ATLAS_FIRST, last=ATLAS_LAST):
    # One slot per printable glyph, plus one extra fully-opaque "solid"
    # cell used to paint cell backgrounds through the same shader.
    return (last - first + 1) + 1


def _solid_index(first=ATLAS_FIRST, last=ATLAS_LAST):
    return last - first + 1  # the slot right after the last glyph


def _atlas_rows(cols=ATLAS_COLS, first=ATLAS_FIRST, last=ATLAS_LAST):
    slots = _atlas_slots(first, last)
    return (slots + cols - 1) // cols


def _slot_uv(index, cols, rows):
    """(u0, v0, u1, v1) for a grid slot. v0 is the bottom edge, v1 the top;
    the atlas is uploaded un-flipped so GL's v=0 is the image's top row."""
    col = index % cols
    row = index // cols
    u0 = col / cols
    u1 = (col + 1) / cols
    v1 = row / rows  # top edge
    v0 = (row + 1) / rows  # bottom edge
    return (u0, v0, u1, v1)


def build_font_atlas(
    font_path=CONSOLAS, px=48, cols=ATLAS_COLS, first=ATLAS_FIRST, last=ATLAS_LAST
):
    """Render every glyph from `first`..`last` into a grid and return
    (atlas_image, cell_w, cell_h).

    Each glyph is anchored to a *shared baseline and left origin* (not
    centered), and every cell is the font's advance width x line height,
    so characters line up like a real monospace terminal. The glyph shape
    lives in the alpha channel (RGB is solid white), which the shader samples.

    cell_w / cell_h are the pixel size of one glyph cell; the caller uses
    them to size the terminal grid so glyphs aren't stretched."""
    font = ImageFont.truetype(font_path, px)

    ascent, descent = font.getmetrics()
    cell_h = ascent + descent  # full line height
    cell_w = math.ceil(font.getlength("M"))  # monospace advance width

    rows = _atlas_rows(cols, first, last)
    img = Image.new("RGBA", (cols * cell_w, rows * cell_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    for i in range(last - first + 1):
        char = chr(first + i)
        col = i % cols
        row = i // cols

        # anchor="la": pen at the cell's (left edge, ascender line), so
        # every glyph shares a baseline and left origin.
        draw.text(
            (col * cell_w, row * cell_h),
            char,
            font=font,
            fill=(255, 255, 255, 255),
            anchor="la",
        )

    # Fill the solid cell fully opaque so backgrounds can sample alpha=1.
    si = _solid_index(first, last)
    scol = si % cols
    srow = si // cols
    draw.rectangle(
        [
            scol * cell_w,
            srow * cell_h,
            (scol + 1) * cell_w - 1,
            (srow + 1) * cell_h - 1,
        ],
        fill=(255, 255, 255, 255),
    )

    return img, cell_w, cell_h


def cell_uv(char, cols=ATLAS_COLS, first=ATLAS_FIRST, last=ATLAS_LAST):
    """Return (u0, v0, u1, v1) for `char`'s glyph cell in the atlas."""
    code = ord(char)
    if code < first or code > last:
        code = first  # unknown glyph -> blank (space)

    i = code - first
    return _slot_uv(i, cols, _atlas_rows(cols, first, last))


def solid_uv(cols=ATLAS_COLS, first=ATLAS_FIRST, last=ATLAS_LAST):
    """A degenerate UV: all four corners point at the *center* of the solid
    opaque cell. Sampling one texel (never an edge) keeps backgrounds fully
    opaque with no bleed from neighboring transparent cells under mipmaps."""
    si = _solid_index(first, last)
    rows = _atlas_rows(cols, first, last)
    cu = (si % cols + 0.5) / cols
    cv = (si // cols + 0.5) / rows
    return (cu, cv, cu, cv)


class FontAtlas:
    """A padded, supersampled, multi-style glyph atlas.

    Renders four Consolas variants (regular / bold / italic / bold-italic)
    into one texture as stacked bands, so bold+italic are real font glyphs
    (not faked). `px` is rendered larger than the on-screen cell and the GPU
    downsamples for crisp anti-aliasing. Each glyph sits in a slot with a
    transparent `pad` gutter so sampling can't bleed a neighbor into a cell.

    Attributes you'll use:
        image             the RGBA PIL image to upload as a texture
        width, height     atlas size in pixels
        glyph_w, glyph_h  one glyph's content size in atlas pixels
                          (divide by your supersample factor for on-screen size)
    """

    N_STYLES = 4  # regular, bold, italic, bold-italic

    def __init__(
        self, px=96, pad=6, cols=ATLAS_COLS, first=ATLAS_FIRST, last=ATLAS_LAST
    ):
        self.cols = cols
        self.first = first
        self.last = last

        fonts = [ImageFont.truetype(p, px) for p in STYLE_FONTS]
        ascent, descent = fonts[0].getmetrics()
        self.glyph_w = math.ceil(fonts[0].getlength("M"))  # monospace advance
        self.glyph_h = ascent + descent  # full line height

        # Slot = glyph content + a transparent gutter on the right/bottom.
        self.pitch_w = self.glyph_w + pad
        self.pitch_h = self.glyph_h + pad

        n = last - first + 1
        self.rows_per_style = (n + cols - 1) // cols
        # One extra band row holds the solid opaque cell.
        self.solid_row = self.N_STYLES * self.rows_per_style
        self.rows = self.solid_row + 1

        self.width = cols * self.pitch_w
        self.height = self.rows * self.pitch_h

        img = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        for style, font in enumerate(fonts):
            base_row = style * self.rows_per_style
            for i in range(n):
                col = i % cols
                row = base_row + i // cols
                draw.text(
                    (col * self.pitch_w, row * self.pitch_h),
                    chr(first + i),
                    font=font,
                    fill=(255, 255, 255, 255),
                    anchor="la",  # shared baseline + left origin
                )

        # Solid opaque cell (for backgrounds / cursor / underline / strike).
        y0 = self.solid_row * self.pitch_h
        draw.rectangle(
            [0, y0, self.glyph_w - 1, y0 + self.glyph_h - 1], fill=(255, 255, 255, 255)
        )

        self.image = img

    def _content_uv(self, col, row):
        x0 = col * self.pitch_w
        y0 = row * self.pitch_h
        u0 = x0 / self.width
        u1 = (x0 + self.glyph_w) / self.width
        v1 = y0 / self.height  # top edge
        v0 = (y0 + self.glyph_h) / self.height  # bottom edge
        return (u0, v0, u1, v1)

    def cell_uv(self, char, bold=False, italic=False):
        """(u0, v0, u1, v1) for a character's glyph, in the requested style."""
        code = ord(char)
        if code < self.first or code > self.last:
            code = self.first  # unknown glyph -> blank (space)
        i = code - self.first
        style = (1 if bold else 0) + (2 if italic else 0)
        col = i % self.cols
        row = style * self.rows_per_style + i // self.cols
        return self._content_uv(col, row)

    def solid_uv(self):
        """Degenerate UV at the center of the solid cell: sampling one opaque
        texel keeps fills solid with no edge bleed."""
        cu = (self.glyph_w * 0.5) / self.width
        cv = (self.solid_row * self.pitch_h + self.glyph_h * 0.5) / self.height
        return (cu, cv, cu, cv)


# Monochrome fallback fonts (regular weight), tried in order for glyphs
# Consolas lacks. This is how broad Unicode coverage is achieved — a fallback
# chain — since no single font covers everything.
# Fallback chain, tried in order: the first font covering a codepoint wins, so
# script-appropriate faces come first and broad catch-alls last. Loaded lazily,
# so a long tail costs nothing until something actually needs it. Missing files
# are skipped harmlessly.
_FALLBACK_PATHS = [
    r"C:\Windows\Fonts\segoeui.ttf",  # Segoe UI (broad Latin/Cyrillic/Greek)
    r"C:\Windows\Fonts\seguisym.ttf",  # Segoe UI Symbol (arrows, box, symbols)
    r"C:\Windows\Fonts\msgothic.ttc",  # CJK (Japanese)
    r"C:\Windows\Fonts\malgun.ttf",  # CJK (Korean)
    r"C:\Windows\Fonts\msyh.ttc",  # CJK (Simplified Chinese)
    r"C:\Windows\Fonts\simsunb.ttf",  # SimSun-ExtB: rare CJK (U+20000+)
    r"C:\Windows\Fonts\Nirmala.ttc",  # Nirmala UI: Indic scripts
    r"C:\Windows\Fonts\leelawui.ttf",  # Leelawadee UI: Thai / Lao / Khmer
    r"C:\Windows\Fonts\ebrima.ttf",  # Ebrima: African scripts (Ethiopic, N'Ko)
    r"C:\Windows\Fonts\gadugi.ttf",  # Gadugi: Cherokee, Canadian Aboriginal
    r"C:\Windows\Fonts\msyi.ttf",  # Microsoft Yi Baiti: Yi syllables
    r"C:\Windows\Fonts\simpbdo.ttf",  # Simplified Arabic
    r"C:\Windows\Fonts\seguihis.ttf",  # Segoe UI Historic: cuneiform, hieroglyphs
    r"C:\Windows\Fonts\SansSerifCollection.ttf",  # last-ditch Unicode catch-all
]

# The OS color emoji font, rendered with its own colors. Only SMP pictographs
# use it; BMP dingbats (✔ ⚠ ★ …) stay as monochrome text so their width and
# look match how a hosted app draws them.
_EMOJI_PATH = r"C:\Windows\Fonts\seguiemj.ttf"


def _is_emoji(cp):
    return cp >= 0x1F000


def _font_coverage(path):
    """Set of Unicode codepoints a font file provides glyphs for."""
    from fontTools.ttLib import TTFont

    try:
        kw = {"fontNumber": 0} if path.lower().endswith(".ttc") else {}
        with TTFont(path, lazy=True, **kw) as font:
            cov = set()
            for table in font["cmap"].tables:
                cov.update(table.cmap.keys())
            return cov
    except Exception:
        return set()


class DynamicAtlas:
    """A glyph atlas that rasterizes characters on demand into a fixed-size
    texture grid, so any Unicode character the fonts cover can be drawn (not
    just a preloaded ASCII range).

    Glyphs are keyed by (codepoint, style) and cached to a slot. New glyphs are
    drawn into a CPU image and their slot marked dirty; the app uploads dirty
    slots to the GPU each frame. Glyphs Consolas lacks fall back to other fonts.

    Same public surface as FontAtlas: image / width / height / glyph_w /
    glyph_h / cell_uv(char, bold, italic) / solid_uv(). Plus take_dirty()."""

    def __init__(self, px=96, pad=6, grid_cols=64, grid_rows=48):
        self.px = px
        self.fonts = [ImageFont.truetype(p, px) for p in STYLE_FONTS]
        # Per-style coverage: the bold/italic Consolas variants are MISSING some
        # glyphs the regular has (e.g. heavy box-drawing ┃ ━ ╭), so we must
        # check the actual style's coverage, not assume it matches regular.
        self._style_cov = [_font_coverage(p) for p in STYLE_FONTS]

        ascent, descent = self.fonts[0].getmetrics()
        # Every glyph, from any font, sits on *this* baseline. Fallback faces
        # have taller ascenders than Consolas, so anchoring each to its own
        # ascender drops them below the text and clips them at the cell edge.
        self.ascent = ascent
        self.descent = descent
        self.glyph_w = math.ceil(self.fonts[0].getlength("M"))
        self.glyph_h = ascent + descent
        self.pitch_w = self.glyph_w + pad
        self.pitch_h = self.glyph_h + pad
        self._scaled = {}  # (path, size) -> shrunk fallback font

        self.grid_cols = grid_cols
        self.grid_rows = grid_rows
        self.capacity = grid_cols * grid_rows
        self.width = grid_cols * self.pitch_w
        self.height = grid_rows * self.pitch_h

        self.image = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        self._draw = ImageDraw.Draw(self.image)

        self.cache = {}  # (codepoint, style) -> slot
        # Slots are write-once (rasterized on a cache miss, never evicted), so
        # this append-only log lets *several* consumers each track their own
        # upload position — which is what lets tabs share one atlas.
        self.written = []  # slots written, in order
        self.color_slots = set()  # slots holding full-color glyphs (emoji)

        # Reserved slots: 0 = solid opaque (backgrounds/cursor), 1 = blank.
        self.solid_slot = 0
        self.blank_slot = 1
        x0, y0 = self._origin(self.solid_slot)
        self._draw.rectangle(
            [x0, y0, x0 + self.glyph_w - 1, y0 + self.glyph_h - 1],
            fill=(255, 255, 255, 255),
        )
        self.written.append(self.solid_slot)
        self.next_slot = 2

        # Lazily-loaded fallback fonts: list of (ImageFont, coverage, is_color).
        self._fallbacks = []
        self._fallback_next = 0
        self._emoji_font = None
        self._emoji_cov = None

    def _origin(self, slot):
        return (slot % self.grid_cols) * self.pitch_w, (
            slot // self.grid_cols
        ) * self.pitch_h

    def _load_emoji(self):
        if self._emoji_cov is None:
            self._emoji_cov = _font_coverage(_EMOJI_PATH)
            try:
                # Color fonts open at a size their bitmap strikes support; PIL
                # then scales. 109 is a Segoe UI Emoji strike.
                self._emoji_font = ImageFont.truetype(_EMOJI_PATH, 109)
            except OSError:
                self._emoji_cov = set()
        return self._emoji_font

    def _font_for(self, char, style):
        """Return (font, is_color) for a character or grapheme cluster; the
        base character picks the font. is_color is True for the OS color emoji
        font (Segoe UI Emoji), whose glyphs carry their own colors."""
        cp = ord(char[0])
        # Pictographic emoji -> OS color emoji font first (before monochrome
        # symbol fonts that would otherwise claim them).
        if _is_emoji(cp):
            font = self._load_emoji()
            if font is not None and cp in self._emoji_cov:
                return font, True
        if cp in self._style_cov[style]:
            return self.fonts[style], False  # the styled variant has it
        if cp in self._style_cov[0]:
            return self.fonts[0], False  # regular Consolas has it (box glyphs etc.)
        for font, cov in self._fallbacks:
            if cp in cov:
                return font, False
        # Load more monochrome fallback fonts until one covers this codepoint.
        while self._fallback_next < len(_FALLBACK_PATHS):
            path = _FALLBACK_PATHS[self._fallback_next]
            self._fallback_next += 1
            cov = _font_coverage(path)
            try:
                font = ImageFont.truetype(path, self.px)
            except OSError:
                continue
            self._fallbacks.append((font, cov))
            if cp in cov:
                return font, False
        return self.fonts[style], False  # give up -> tofu / .notdef

    def _scaled_font(self, font, scale):
        """A copy of `font` shrunk by `scale`, cached per (path, size)."""
        path = str(getattr(font, "path", ""))
        size = max(8, int(self.px * scale))
        key = (path, size)
        cached = self._scaled.get(key)
        if cached is None:
            try:
                cached = ImageFont.truetype(path, size)
            except OSError:
                cached = font
            self._scaled[key] = cached
        return cached

    def _fit_fallback(self, char, font, gw):
        """Shrink a fallback font if this glyph's ink would spill out of the
        cell. CJK faces draw to fill their own em box, which is taller than
        Consolas's ascent, so baseline-aligning them would clip the top. Only
        the glyphs that actually overflow shrink; the rest are untouched."""
        bb = font.getbbox(char, anchor="ls")  # y is relative to the baseline
        if not bb:
            return font
        scale = 1.0
        above, below, width = -bb[1], bb[3], bb[2] - bb[0]
        if above > self.ascent:
            scale = min(scale, self.ascent / above)
        if below > self.descent:
            scale = min(scale, self.descent / below)
        if width > gw:  # would bleed into the next slot
            scale = min(scale, gw / width)
        if scale >= 0.999:
            return font
        return self._scaled_font(font, scale)

    def _draw_marks(self, marks, style, x0, y0, gw):
        """Overlay combining marks onto the base glyph.

        There's no shaping engine here (Pillow ships without Raqm on Windows),
        and fonts give combining marks a *full* advance — so drawing the
        cluster as a string lays the mark down beside the base as its own
        spacing glyph, the classic stray-accent bug. Instead place each mark
        ourselves: horizontally centred on the cell, keeping the vertical
        position the font already gives it relative to the baseline.
        """
        pad = self.glyph_w * 2  # room for marks that overhang the cell
        for mark in marks:
            font, is_color = self._font_for(mark, style)
            if is_color:
                continue
            tmp = Image.new("L", (gw + 2 * pad, self.pitch_h + 2 * pad), 0)
            ImageDraw.Draw(tmp).text(
                (pad, pad + self.ascent), mark, font=font, fill=255, anchor="ls"
            )
            bb = tmp.getbbox()
            if not bb:
                continue
            ink = tmp.crop(bb)
            tx = x0 + max(0, (gw - ink.width) // 2)
            ty = y0 + (bb[1] - pad)
            # Never let a mark spill into a neighbouring slot.
            tx = max(x0, min(tx, x0 + gw - ink.width))
            ty = max(y0, min(ty, y0 + self.glyph_h - ink.height))
            layer = Image.new("RGBA", ink.size, (255, 255, 255, 255))
            self.image.paste(layer, (tx, ty), ink)

    def _rasterize(self, char, style, slot, wslots):
        x0, y0 = self._origin(slot)
        region_w = self.pitch_w * wslots
        self._draw.rectangle(
            [x0, y0, x0 + region_w - 1, y0 + self.pitch_h - 1], fill=(0, 0, 0, 0)
        )
        base, marks = char[0], char[1:]
        font, is_color = self._font_for(base, style)
        gw = self.glyph_w * wslots
        if is_color:
            # Emoji carry their own colors; render, trim, fit to the glyph box.
            tmp = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
            ImageDraw.Draw(tmp).text((4, 4), char, font=font, embedded_color=True)
            bbox = tmp.getbbox()
            if bbox:
                tmp = tmp.crop(bbox)
            # Fit inside the cell *without distorting*: emoji are square, and
            # stretching them to the cell's aspect makes them look fat.
            s = min(gw / tmp.width, self.glyph_h / tmp.height)
            size = (max(1, round(tmp.width * s)), max(1, round(tmp.height * s)))
            tmp = tmp.resize(size, Image.LANCZOS)
            self.image.paste(
                tmp, (x0 + (gw - size[0]) // 2, y0 + (self.glyph_h - size[1]) // 2)
            )
            self.color_slots.add(slot)
        else:
            baseline = y0 + self.ascent
            if font in self.fonts:
                # Primary monospace: the cell is built from its metrics, so it
                # lands on the baseline as-is (identical to the old anchor="la").
                self._draw.text(
                    (x0, baseline),
                    base,
                    font=font,
                    fill=(255, 255, 255, 255),
                    anchor="ls",
                )
            else:
                font = self._fit_fallback(base, font, gw)
                bb = font.getbbox(base, anchor="ls")
                # Centre it: fallback faces are proportional, so left-aligning
                # them in a monospace cell looks lopsided.
                dx = (gw - (bb[2] - bb[0])) / 2.0 - bb[0] if bb else 0.0
                self._draw.text(
                    (x0 + dx, baseline),
                    base,
                    font=font,
                    fill=(255, 255, 255, 255),
                    anchor="ls",
                )
            if marks:
                self._draw_marks(marks, style, x0, y0, gw)
        self.written.append(slot)
        if wslots == 2:
            self.written.append(slot + 1)
        return is_color

    def _content_uv(self, slot, wslots=1):
        x0, y0 = self._origin(slot)
        gw = self.glyph_w * wslots
        return (
            x0 / self.width,
            (y0 + self.glyph_h) / self.height,
            (x0 + gw) / self.width,
            y0 / self.height,
        )

    def cell_uv(self, char, bold=False, italic=False):
        """Return (u0, v0, u1, v1, is_color) for a character's glyph. `char`
        may be a grapheme cluster (base + combining marks), which is cached
        and rasterized as a single glyph."""
        style = (1 if bold else 0) + (2 if italic else 0)
        key = (char, style)
        entry = self.cache.get(key)
        if entry is None:
            wslots = 2 if char_width(char[0]) == 2 else 1
            # Keep a wide pair within one atlas row so its UVs stay contiguous.
            if wslots == 2 and self.next_slot % self.grid_cols == self.grid_cols - 1:
                self.next_slot += 1
            if self.next_slot + wslots - 1 >= self.capacity:
                return self._content_uv(self.blank_slot) + (False,)  # atlas full
            slot = self.next_slot
            self.next_slot += wslots
            is_color = self._rasterize(char, style, slot, wslots)
            entry = (slot, wslots, is_color)
            self.cache[key] = entry
        slot, wslots, is_color = entry
        return self._content_uv(slot, wslots) + (is_color,)

    def solid_uv(self):
        x0, y0 = self._origin(self.solid_slot)
        cu = (x0 + self.glyph_w * 0.5) / self.width
        cv = (y0 + self.glyph_h * 0.5) / self.height
        return (cu, cv, cu, cv)

    def dirty_since(self, cursor):
        """Return ([(x, y, w, h, rgba_bytes), ...], new_cursor) for slots
        written since `cursor`. Non-destructive: each GL context keeps its own
        cursor, so tabs sharing this atlas all get every new glyph."""
        regions = []
        for slot in self.written[cursor:]:
            x0, y0 = self._origin(slot)
            crop = self.image.crop((x0, y0, x0 + self.pitch_w, y0 + self.pitch_h))
            regions.append((x0, y0, self.pitch_w, self.pitch_h, crop.tobytes()))
        return regions, len(self.written)


class RectangleBuilder:
    """Accumulates one *instance* per quad; the GPU expands each into two
    triangles from a shared unit quad.

    Writing 12 floats per quad instead of 6 vertices x 8 floats is ~8x less
    Python work per frame and 4x less data to upload — the difference between a
    full-screen rebuild fitting in a frame budget and blowing straight through
    it. `mode` 0 = alpha-keyed glyph tinted by `color`; 1 = use the texture's
    own RGBA (color emoji).
    """

    FLOATS_PER_QUAD = 12

    # The unit quad every instance is expanded from: two triangles, corners in
    # (0,0)..(1,1). The vertex shader maps corner -> position and -> UV.
    CORNERS = array("f", [0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 1.0])

    def __init__(self):
        # A plain list is measurably faster to append to than array('f'); it's
        # converted once, at upload.
        self.data = []

    def add(self, x, y, w, h, color, u0, v0, u1, v1, mode=0.0):
        r, g, b = color
        self.data.extend((x, y, w, h, r, g, b, u0, v0, u1, v1, mode))

    def extend(self, other):
        """Append another builder's quads, preserving draw order."""
        self.data.extend(other.data)

    @property
    def count(self):
        """Number of quads (= instances to draw)."""
        return len(self.data) // self.FLOATS_PER_QUAD

    def buffer(self):
        return array("f", self.data)


def cell_rect(x, y, cols, rows):
    cell_w = 2.0 / cols
    cell_h = 2.0 / rows

    left = -1.0 + x * cell_w
    top = 1.0 - y * cell_h

    return (
        left,
        top - cell_h,
        cell_w,
        cell_h,
    )


def cell_rect_px(x, y, cw, ch, win_w, win_h):
    """NDC rect for grid cell (x, y) using exact pixel cell sizes, anchored
    top-left. The grid is pixel-exact (glyphs keep their true aspect, no
    stretch); leftover pixels become a right/bottom margin instead of the
    cells being clamped to fill the window."""
    px_left = x * cw
    px_top = y * ch

    ndc_left = px_left / win_w * 2.0 - 1.0
    ndc_top = 1.0 - px_top / win_h * 2.0
    ndc_w = cw / win_w * 2.0
    ndc_h = ch / win_h * 2.0

    return (ndc_left, ndc_top - ndc_h, ndc_w, ndc_h)
