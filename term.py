# YoTerm: A friendly, Terminal Emulator

import re
import time
import unicodedata

from tools import char_width
from ytseq import make_gradient
from ytimg import load_image, fit_cells, video_size, ImagePlacement
from ytzone import Zone, apply_style, geometry_from

# A run of "ordinary" bytes inside an OSC payload — everything that isn't a C0
# control (which is where OSC termination / abort decisions live). Used to
# bulk-swallow long base64 image/video payloads instead of one char at a time.
_OSC_RUN = re.compile(r"[^\x00-\x1f]+")

# Categories that combine onto a preceding glyph rather than occupying a cell:
# non-spacing (Mn), enclosing (Me), and spacing-combining (Mc) marks. Other
# zero-width codepoints (ZWSP, ZWJ, BOM — category Cf) are formatting and are
# dropped rather than attached to anything.
_MARK_CATEGORIES = ("Mn", "Me", "Mc")

# Cap on marks per cell, so a "Zalgo" run of stacked diacritics can't grow a
# cell's string without bound.
MAX_MARKS = 8

# Cap on an OSC payload, so a sequence that never terminates can't buffer
# output forever. Big enough to carry a base64 image/video frame through
# `YT;img;data:` (a single 720p JPEG frame is tens of KB), small enough that a
# runaway unterminated sequence still can't eat unbounded memory.
MAX_OSC = 8 * 1024 * 1024

# DEC Special Graphics (ESC ( 0). Old TUIs draw boxes by switching G0 to this
# and sending plain ASCII, so 'q' is a horizontal line, 'x' a vertical one, and
# so on. Without the mapping those programs print 'qqqqq' where a rule belongs.
DEC_GRAPHICS = {
    "_": " ",
    "`": "◆",
    "a": "▒",
    "b": "␉",
    "c": "␌",
    "d": "␍",
    "e": "␊",
    "f": "°",
    "g": "±",
    "h": "␤",
    "i": "␋",
    "j": "┘",
    "k": "┐",
    "l": "┌",
    "m": "└",
    "n": "┼",
    "o": "⎺",
    "p": "⎻",
    "q": "─",
    "r": "⎼",
    "s": "⎽",
    "t": "├",
    "u": "┤",
    "v": "┴",
    "w": "┬",
    "x": "│",
    "y": "≤",
    "z": "≥",
    "{": "π",
    "|": "≠",
    "}": "£",
    "~": "·",
}

# SGR color codes -> color names (matching tools.PALETTE keys).
SGR_FG = {
    30: "black",
    31: "red",
    32: "green",
    33: "yellow",
    34: "blue",
    35: "magenta",
    36: "cyan",
    37: "white",
    39: "default",
}
SGR_BG = {
    40: "black",
    41: "red",
    42: "green",
    43: "yellow",
    44: "blue",
    45: "magenta",
    46: "cyan",
    47: "white",
    49: "default",
}
SGR_FG_BRIGHT = {
    90: "bright_black",
    91: "bright_red",
    92: "bright_green",
    93: "bright_yellow",
    94: "bright_blue",
    95: "bright_magenta",
    96: "bright_cyan",
    97: "bright_white",
}
SGR_BG_BRIGHT = {
    100: "bright_black",
    101: "bright_red",
    102: "bright_green",
    103: "bright_yellow",
    104: "bright_blue",
    105: "bright_magenta",
    106: "bright_cyan",
    107: "bright_white",
}

# The 16 standard xterm colors (RGB 0-255) for palette indices 0-15.
_XTERM_16 = [
    (0, 0, 0),
    (128, 0, 0),
    (0, 128, 0),
    (128, 128, 0),
    (0, 0, 128),
    (128, 0, 128),
    (0, 128, 128),
    (192, 192, 192),
    (128, 128, 128),
    (255, 0, 0),
    (0, 255, 0),
    (255, 255, 0),
    (0, 0, 255),
    (255, 0, 255),
    (0, 255, 255),
    (255, 255, 255),
]
_CUBE = (0, 95, 135, 175, 215, 255)  # 6x6x6 color-cube levels


def color_256(n):
    """Map an xterm 256-color index to an (r, g, b) float tuple in 0..1."""
    n = max(0, min(255, n))
    if n < 16:
        r, g, b = _XTERM_16[n]
    elif n < 232:
        c = n - 16
        r, g, b = _CUBE[c // 36], _CUBE[(c // 6) % 6], _CUBE[c % 6]
    else:
        v = 8 + (n - 232) * 10  # grayscale ramp
        r = g = b = v
    return (r / 255.0, g / 255.0, b / 255.0)


class Cell:
    __slots__ = (
        "char",
        "fg",
        "bg",
        "bold",
        "dim",
        "italic",
        "underline",
        "reverse",
        "strike",
        "conceal",
        "blink",
        "protected",
        "grad",
        "href",
        "width",
    )

    def __init__(
        self,
        char=" ",
        fg="default",
        bg="default",
        bold=False,
        dim=False,
        italic=False,
        underline=False,
        reverse=False,
        strike=False,
        conceal=False,
        blink=False,
        protected=False,
        grad=None,
        href=None,
        width=1,
    ):
        self.char = char
        self.fg = fg
        self.bg = bg
        self.bold = bold
        self.dim = dim
        self.italic = italic
        self.underline = underline
        self.reverse = reverse
        self.strike = strike
        self.conceal = conceal
        self.blink = blink
        # DECSCA: a protected cell survives selective erase (DECSED/DECSEL) but
        # is still cleared by the ordinary ED/EL and by RIS.
        self.protected = protected
        # YoTerm gradient run (ESC ] YT ; gradient), or None. Cells in one run
        # share the same object, so the renderer can span one ramp across them.
        self.grad = grad
        # OSC 8 hyperlink target, or None.
        self.href = href
        # 1 = normal, 2 = leading half of a wide glyph, 0 = trailing spacer.
        self.width = width

    def __repr__(self):
        return f"Cell({self.char!r}, fg={self.fg!r}, bg={self.bg!r})"


class Cursor:
    def __init__(self):
        self.x = 0
        self.y = 0

        self.visible = True
        self.blink = True
        self.shape = "bar"  # bar, block, underline


class Renderer:
    def __init__(self, terminal):
        self.terminal = terminal

    def draw(self):
        rows = []

        blink_on = not self.terminal.cursor.blink or int(time.time() * 2) % 2 == 0

        for y, row in enumerate(self.terminal.screen):
            chars = []

            for x, cell in enumerate(row):
                if (
                    blink_on
                    and self.terminal.cursor.visible
                    and x == self.terminal.cursor.x
                    and y == self.terminal.cursor.y
                ):
                    if self.terminal.cursor.shape == "block":
                        chars.append("█")
                    elif self.terminal.cursor.shape in ("bar", "vertical"):
                        chars.append("│")
                    elif self.terminal.cursor.shape == "underline":
                        chars.append("_")
                else:
                    chars.append(cell.char)

            rows.append("".join(chars))

        return "\n".join(rows)


class Terminal:
    TAB_SIZE = 8

    MAX_SCROLLBACK = 2000

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.cursor = Cursor()
        self.screen = [[Cell() for _ in range(self.width)] for _ in range(self.height)]
        self.escape = False
        self.escape_buffer = ""
        # Deferred auto-wrap: set when a glyph lands in the last column, so a
        # following CR/LF doesn't add a blank line ("extra newline" bug).
        self.wrap_pending = False
        self.current_fg = "default"
        self.current_bg = "default"
        self.current_bold = False
        self.current_dim = False
        self.current_italic = False
        self.current_underline = False
        self.current_reverse = False
        self.current_strike = False
        self.current_conceal = False
        self.current_blink = False  # SGR 5
        # DECSCA (ESC [ Ps " q): whether new glyphs are erase-protected. Unlike
        # the SGR attributes above, this is NOT cleared by SGR 0 — only by
        # another DECSCA, a soft reset (DECSTR) or RIS.
        self.current_protected = False
        # YoTerm gradient (ESC ] YT ; gradient) applied to following glyphs, or
        # None. SGR 0 clears it, so it behaves like a rendition attribute.
        self.current_grad = None
        # OSC 8 hyperlink target applied to following glyphs, or None. Unlike
        # the SGR attributes above, this is NOT part of "graphic rendition" in
        # any real terminal -- SGR 0 doesn't end a link, only another OSC 8
        # with an empty URI does (or a full reset).
        self.current_href = None

        # Saved cursor position (ESC 7 / ESC 8, ESC [ s / ESC [ u).
        # DECSC (ESC 7) stashes the whole drawing state; SCOSC (ESC [ s), the
        # ANSI.SYS variant, only stashes the position. They get separate slots
        # so a prompt that brackets colored output in ESC[s/ESC[u doesn't have
        # its colors restored out from under it.
        self.saved_cursor = None  # DECSC: position + SGR + origin mode
        self.saved_pos = None  # SCOSC: position only

        # Horizontal tab stops (HTS / TBC), as 0-based columns. A fresh
        # terminal has one every TAB_SIZE columns.
        self.tab_stops = set(range(self.TAB_SIZE, width, self.TAB_SIZE))

        # Auto-wrap (DECAWM, ?7). Off means a glyph in the last column
        # overwrites it instead of moving to the next line.
        self.autowrap = True

        # The last glyph written, for REP (ESC [ n b).
        self.last_graphic = None

        # Lines that have scrolled off the top, oldest first.
        self.scrollback = []
        # How many lines the view is scrolled up from the live bottom (0 = live).
        self.scroll_offset = 0
        # Absolute line number of scrollback[0] — i.e. how many lines have been
        # trimmed away for good. It gives every line a number that survives
        # scrolling and trimming, which is how a placed image (YT;img) stays
        # pinned to its text. Screen row r is line first_line_no+len(scrollback)+r.
        self.first_line_no = 0

        # Placed images (YT;img), and pixels-per-cell (set by the app, which is
        # the only side that knows it) so image sizing can respect aspect ratio.
        self.images = []
        self.cell_px = (8, 16)
        self._next_img_id = 1

        # YT;vid requests the app hasn't picked up yet. The model can't decode or
        # spawn threads, so it just reserves a placement (an image slot) and
        # records the request; the app drains this, starts a decoder thread, and
        # streams frames back into that placement. See _yt_video.
        self.video_requests = []

        # Zones (YT;zone): styled rectangles, keyed by the caller's id so an
        # `update` is a cheap patch. Their ids are a separate namespace from
        # images'. See docs/zones.md.
        self.zones = {}

        # Scrolling region (DECSTBM), inclusive 0-based row bounds.
        self.scroll_top = 0
        self.scroll_bottom = height - 1
        # Origin mode (DECOM, ?6): cursor positioning relative to the region.
        self.origin_mode = False

        # Alternate screen buffer (used by full-screen TUIs).
        self.alt_screen = False
        self.primary_screen = None
        self.primary_cursor = (0, 0)

        # VT100 modes.
        self.insert_mode = False  # IRM (4): insert rather than overwrite
        self.newline_mode = False  # LNM (20): LF also does a CR
        self.cursor_keys_app = False  # DECCKM (?1): arrows send ESC O A
        self.keypad_app = False  # DECKPAM / DECKPNM
        self.reverse_video = False  # DECSCNM (?5): whole screen inverted
        self.column_mode_132 = False  # DECCOLM (?3)
        # Accepted so DECRQM can answer honestly, but inert: these describe CRT
        # hardware a GPU renderer has no equivalent for.
        self.smooth_scroll = False  # DECSCLM (?4)
        self.autorepeat = True  # DECARM (?8)
        self.interlace = False  # DECINLM (?9)

        # Character sets. The VT220 has four designatable slots, G0-G3, and
        # decides which one GL (the graphic-left, 0x20-0x7E range) draws from.
        # 'B' = ASCII, '0' = DEC Special Graphics, 'A' = UK national set.
        #   ESC ( ) * +  designate G0 G1 G2 G3
        #   SI / SO      lock G0 / G1 into GL   (LS0 / LS1)
        #   ESC n / o    lock G2 / G3 into GL   (LS2 / LS3)
        #   ESC N / O    shift G2 / G3 for ONE glyph (SS2 / SS3)
        self.charsets = ["B", "B", "B", "B"]
        self.charset_gl = 0
        # When a single shift is pending, the next graphic glyph draws from this
        # slot (2 or 3) and then GL reverts on its own.
        self.single_shift = None
        # DECBKM (?67): backspace key sends BS (set) rather than DEL (reset).
        self.backarrow_bs = False
        # DECSCL (ESC [ Pl " p): conformance level a program asked for. We
        # answer DA as a VT100 regardless, so this is just remembered, not acted
        # on — the point is that the request is consumed, not left on screen.
        self.conformance_level = 62  # VT200 series

        # BEL rings this; the app decides whether that's a sound or a flash.
        self.bell_count = 0

        # Input modes the app toggles via DEC private modes.
        self.mouse_mode = 0  # 0 off, or 1000/1002/1003 tracking level
        self.mouse_sgr = False  # 1006: SGR extended mouse coordinates
        self.bracketed_paste = False  # 2004

        # Window/tab title, as set by OSC 0 / OSC 2. The app polls this.
        self.title = ""

        # Replies the terminal owes the shell (e.g. DSR cursor-position report).
        # The app drains this and writes it back to the PTY.
        self.responses = []

    def _blank_row(self):
        return [Cell() for _ in range(self.width)]

    def index(self):
        """IND (ESC D): move down one row, keeping the column. At the bottom
        margin the region scrolls up instead."""
        self.wrap_pending = False
        if self.cursor.y == self.scroll_bottom:
            self._scroll_region_up(1)  # at bottom margin -> scroll
        else:
            self.cursor.y = min(self.cursor.y + 1, self.height - 1)

    def reverse_index(self):
        """RI (ESC M): move up one row, keeping the column. At the top margin
        the region scrolls *down*, opening a blank line — this is how pagers
        and full-screen apps scroll backwards."""
        self.wrap_pending = False
        if self.cursor.y == self.scroll_top:
            self._scroll_region_down(1)
        else:
            self.cursor.y = max(self.cursor.y - 1, 0)

    def next_line(self):
        """NEL (ESC E): index, then return to column 1."""
        self.index()
        self.cursor.x = 0

    def newline(self):
        self.next_line()

    def _scroll_region_up(self, n=1):
        """Scroll the region [scroll_top, scroll_bottom] up by n lines, adding
        blanks at the bottom. When the region starts at the screen top, the
        removed lines go into scrollback."""
        top, bot = self.scroll_top, self.scroll_bottom
        for _ in range(n):
            line = self.screen.pop(top)
            # Scrollback belongs to the primary screen only, and only when the
            # region starts at the very top.
            if top == 0 and not self.alt_screen:
                self.scrollback.append(line)
                if len(self.scrollback) > self.MAX_SCROLLBACK:
                    self.scrollback.pop(0)
                    self.first_line_no += 1  # a line left history for good
                    self._prune_images()
                    self._prune_zones()
                elif self.scroll_offset > 0:
                    # Keep a scrolled-up view anchored as output streams in.
                    self.scroll_offset = min(
                        self.scroll_offset + 1, len(self.scrollback)
                    )
            self.screen.insert(bot, self._blank_row())

    def _scroll_region_down(self, n=1):
        """Scroll the region down by n lines, adding blanks at the top."""
        top, bot = self.scroll_top, self.scroll_bottom
        for _ in range(n):
            del self.screen[bot]
            self.screen.insert(top, self._blank_row())

    # ------------------------------------------------------------ scrolling

    def scroll_up(self, n=1):
        if self.alt_screen:
            return  # alt screen has no scrollback to reveal
        self.scroll_offset = min(self.scroll_offset + n, len(self.scrollback))

    def scroll_down(self, n=1):
        self.scroll_offset = max(self.scroll_offset - n, 0)

    def scroll_to_bottom(self):
        self.scroll_offset = 0

    def visible_lines(self):
        """The `height` rows currently in view (history when scrolled up)."""
        if self.alt_screen or self.scroll_offset <= 0:
            return self.screen

        top = len(self.scrollback) - self.scroll_offset
        lines = []
        for i in range(top, top + self.height):
            if 0 <= i < len(self.scrollback):
                lines.append(self.scrollback[i])
            elif i >= len(self.scrollback):
                lines.append(self.screen[i - len(self.scrollback)])
            else:
                lines.append(self._blank_row())
        return lines

    def visible_top(self):
        """Absolute line index (into scrollback + screen) of the top visible row."""
        return len(self.scrollback) - self.scroll_offset

    def line_at(self, index):
        """Row at an absolute index across scrollback + screen, or None."""
        if index < 0:
            return None
        if index < len(self.scrollback):
            return self.scrollback[index]
        i = index - len(self.scrollback)
        if 0 <= i < len(self.screen):
            return self.screen[i]
        return None

    # ------------------------------------------------------------ resize

    def resize(self, width, height):
        """Reflow to a new grid size. Rows aren't re-wrapped (the shell
        repaints on its next prompt); we just fit the existing content and
        push any overflow into scrollback."""
        width = max(1, width)
        height = max(1, height)
        if width == self.width and height == self.height:
            return

        # Fit each current row to the new width.
        new_screen = []
        for row in self.screen:
            if len(row) >= width:
                new_screen.append(row[:width])
            else:
                new_screen.append(row + [Cell() for _ in range(width - len(row))])

        # Fit the row count.
        if len(new_screen) > height:
            overflow = len(new_screen) - height
            self.scrollback.extend(new_screen[:overflow])
            new_screen = new_screen[overflow:]
        else:
            for _ in range(height - len(new_screen)):
                new_screen.append([Cell() for _ in range(width)])

        self.screen = new_screen
        self.width = width
        self.height = height
        self.cursor.x = min(self.cursor.x, width - 1)
        self.cursor.y = min(self.cursor.y, height - 1)

        # A resize invalidates any scroll region; reset to the full screen.
        self.scroll_top = 0
        self.scroll_bottom = height - 1

        # Keep custom tab stops, extend the defaults over any new width, and
        # drop any that now fall off the right edge.
        self.tab_stops |= set(range(self.TAB_SIZE, width, self.TAB_SIZE))
        self.tab_stops = {s for s in self.tab_stops if s < width}

        # Keep the saved primary buffer usable if a TUI is open during resize.
        if self.primary_screen is not None:
            fitted = []
            for row in self.primary_screen[:height]:
                if len(row) >= width:
                    fitted.append(row[:width])
                else:
                    fitted.append(row + [Cell() for _ in range(width - len(row))])
            while len(fitted) < height:
                fitted.append([Cell() for _ in range(width)])
            self.primary_screen = fitted

        if len(self.scrollback) > self.MAX_SCROLLBACK:
            trimmed = len(self.scrollback) - self.MAX_SCROLLBACK
            del self.scrollback[:trimmed]
            self.first_line_no += trimmed
            self._prune_images()
            self._prune_zones()
        self.scroll_offset = min(self.scroll_offset, len(self.scrollback))

    def carriage(self):
        self.wrap_pending = False
        self.cursor.x = 0

    def backspace(self):
        if self.cursor.x > 0:
            self.cursor.x -= 1

    def _next_tab_stop(self, x):
        """The next tab stop strictly right of column x, else the last column."""
        stops = [s for s in self.tab_stops if s > x]
        # Clamp to the last column. put_char() wraps at the right edge
        # (resetting cursor.x to 0), so a stop past the width would loop
        # forever.
        return min(stops) if stops else self.width - 1

    def _prev_tab_stop(self, x):
        """The nearest tab stop strictly left of column x, else column 0."""
        stops = [s for s in self.tab_stops if s < x]
        return max(stops) if stops else 0

    def tab(self):
        """HT: advance to the next tab stop. Like CHT this only moves the
        cursor — cells it passes over keep their contents. Writing spaces
        instead would erase text a program tabs back over, and would smear
        the current background color across the gap."""
        self.cursor_forward_tab(1)

    def cursor_forward_tab(self, n=1):
        """CHT (ESC [ n I): move forward n tab stops. Unlike HT this only
        moves the cursor — the cells it passes over keep their contents."""
        self.wrap_pending = False
        x = self.cursor.x
        for _ in range(max(1, n)):
            x = self._next_tab_stop(x)
        self.cursor.x = max(0, min(x, self.width - 1))

    def cursor_back_tab(self, n=1):
        """CBT (ESC [ n Z): move back n tab stops, likewise non-destructive."""
        self.wrap_pending = False
        x = self.cursor.x
        for _ in range(max(1, n)):
            x = self._prev_tab_stop(x)
        self.cursor.x = max(0, min(x, self.width - 1))

    def set_tab_stop(self):
        """HTS (ESC H): set a tab stop at the current column."""
        self.tab_stops.add(self.cursor.x)

    def clear_tab_stop(self, mode=0):
        """TBC (ESC [ n g): clear the stop at the cursor (0) or all of them (3)."""
        if mode == 3:
            self.tab_stops.clear()
        elif mode == 0:
            self.tab_stops.discard(self.cursor.x)

    def repeat_last(self, n=1):
        """REP (ESC [ n b): repeat the last glyph written n more times."""
        if self.last_graphic is None:
            return
        char = self.last_graphic
        for _ in range(max(1, n)):
            self.put_char(char)

    def cursor_up(self, n=1):
        """CUU. Starting inside the scroll region, the cursor stops at the top
        margin rather than escaping it — that's what keeps a full-screen app's
        cursor inside the pane it set up. Outside the region it just clamps to
        the screen."""
        self.wrap_pending = False
        limit = self.scroll_top if self.cursor.y >= self.scroll_top else 0
        self.cursor.y = max(limit, self.cursor.y - n)

    def cursor_down(self, n=1):
        """CUD. Mirror of cursor_up: stops at the bottom margin."""
        self.wrap_pending = False
        limit = (
            self.scroll_bottom
            if self.cursor.y <= self.scroll_bottom
            else self.height - 1
        )
        self.cursor.y = min(limit, self.cursor.y + n)

    def cursor_forward(self, n=1):
        self.wrap_pending = False
        self.cursor.x = min(self.width - 1, self.cursor.x + n)

    def cursor_back(self, n=1):
        self.wrap_pending = False
        self.cursor.x = max(0, self.cursor.x - n)

    def get_amount(self, seq):
        amount = seq[1:-1]
        return int(amount) if amount.isdigit() else 1

    def parse_params(self, seq):
        body = seq[1:-1]  # remove '[' and final letter
        if not body:
            return []

        params = []
        for part in body.split(";"):
            if part == "":
                params.append(0)  # empty param defaults to 0
            elif part.isdigit():
                params.append(int(part))
            # anything else (e.g. a '?' private-mode prefix) is skipped
        return params

    def _set_cursor(self, y, x):
        self.wrap_pending = False
        self.cursor.y = max(0, min(self.height - 1, y))
        self.cursor.x = max(0, min(self.width - 1, x))

    def _abs_row(self, row):
        """Convert a 1-based row parameter to an absolute screen row, honoring
        origin mode (relative to and confined within the scroll region)."""
        if self.origin_mode:
            y = self.scroll_top + (row - 1)
            return max(self.scroll_top, min(self.scroll_bottom, y))
        return max(0, min(self.height - 1, row - 1))

    def _set_row_col(self, row, col):
        self.wrap_pending = False
        self.cursor.y = self._abs_row(row)
        self.cursor.x = max(0, min(self.width - 1, col - 1))

    def reset(self):
        """RIS (ESC c): back to power-on state.

        This is what `reset` and `tput reset` send when a program has left the
        terminal in a mess, so it has to put *everything* back — not just the
        screen, but every mode a program could have left flipped.
        """
        self.exit_alt_screen()
        self.screen = [self._blank_row() for _ in range(self.height)]
        self.scrollback.clear()
        self.first_line_no = 0
        self.images = []
        self.video_requests = []
        self.zones = {}
        self.scroll_offset = 0
        self.scroll_top, self.scroll_bottom = 0, self.height - 1
        self.origin_mode = False
        self.autowrap = True
        self.insert_mode = False
        self.newline_mode = False
        self.cursor_keys_app = False
        self.keypad_app = False
        self.reverse_video = False
        self.column_mode_132 = False
        self.smooth_scroll = False
        self.autorepeat = True
        self.interlace = False
        self.mouse_mode = 0
        self.mouse_sgr = False
        self.bracketed_paste = False
        self.charsets = ["B", "B", "B", "B"]
        self.charset_gl = 0
        self.single_shift = None
        self.backarrow_bs = False
        self.current_protected = False
        self.current_href = None
        self.tab_stops = set(range(self.TAB_SIZE, self.width, self.TAB_SIZE))
        self.saved_cursor = None
        self.saved_pos = None
        self.title = ""
        self.wrap_pending = False
        self.last_graphic = None
        self.cursor.visible = True
        self.cursor.blink = True
        self._reset_sgr()
        self._set_cursor(0, 0)

    def _ansi_mode(self, body, on):
        """SM / RM (ESC [ <n> h / l): the non-private ANSI modes."""
        for part in body.split(";"):
            if not part.isdigit():
                continue
            mode = int(part)
            if mode == 4:  # IRM: insert vs replace
                self.insert_mode = on
            elif mode == 20:  # LNM: LF also does a carriage return
                self.newline_mode = on

    def screen_alignment(self):
        """DECALN (ESC # 8): fill the whole screen with 'E'.

        A VT100 service pattern for checking screen geometry. Like xterm, it
        also drops any scroll region and homes the cursor.
        """
        self.scroll_top, self.scroll_bottom = 0, self.height - 1
        self.screen = [
            [Cell(char="E") for _ in range(self.width)] for _ in range(self.height)
        ]
        self._set_cursor(0, 0)

    def report_mode(self, body, private):
        """DECRQM (ESC [ ? Ps $ p): answer whether a mode is currently set.

        Reply is ESC [ ? Ps ; Pm $ y, where Pm is 0 (mode not recognized),
        1 (set) or 2 (reset). Apps use this to check a mode instead of
        assuming, so answering 0 for something we don't have is the honest
        reply — claiming "reset" would imply we know the mode.
        """
        if not body.isdigit():
            return
        mode = int(body)
        state = 0  # not recognized
        if private:
            known = {
                1: lambda: self.cursor_keys_app,
                3: lambda: self.column_mode_132,
                4: lambda: self.smooth_scroll,
                5: lambda: self.reverse_video,
                6: lambda: self.origin_mode,
                7: lambda: self.autowrap,
                8: lambda: self.autorepeat,
                9: lambda: self.interlace,
                25: lambda: self.cursor.visible,
                47: lambda: self.alt_screen,
                1047: lambda: self.alt_screen,
                1049: lambda: self.alt_screen,
                1000: lambda: self.mouse_mode == 1000,
                1002: lambda: self.mouse_mode == 1002,
                1003: lambda: self.mouse_mode == 1003,
                1006: lambda: self.mouse_sgr,
                2004: lambda: self.bracketed_paste,
                66: lambda: self.keypad_app,  # DECNKM
                67: lambda: self.backarrow_bs,  # DECBKM
            }
            if mode in known:
                state = 1 if known[mode]() else 2
        else:
            ansi_known = {
                4: lambda: self.insert_mode,  # IRM
                20: lambda: self.newline_mode,  # LNM
            }
            if mode in ansi_known:
                state = 1 if ansi_known[mode]() else 2
        self.responses.append("\x1b[%s%d;%d$y" % ("?" if private else "", mode, state))

    def set_cursor_style(self, n):
        """DECSCUSR (ESC [ n SP q): let an app pick the caret shape and blink.
        0/1 blinking block, 2 steady block, 3 blinking underline,
        4 steady underline, 5 blinking bar, 6 steady bar."""
        styles = {
            0: ("block", True),
            1: ("block", True),
            2: ("block", False),
            3: ("underline", True),
            4: ("underline", False),
            5: ("bar", True),
            6: ("bar", False),
        }
        if n in styles:
            self.cursor.shape, self.cursor.blink = styles[n]

    # The rendition state DECSC carries along with the cursor. A real VT saves
    # the selective-erase attribute (DECSCA) here too, so it rides along with a
    # DECSC/DECRC pair the same way the SGR attributes do.
    _SGR_ATTRS = (
        "current_fg",
        "current_bg",
        "current_bold",
        "current_dim",
        "current_italic",
        "current_underline",
        "current_reverse",
        "current_strike",
        "current_conceal",
        "current_blink",
        "current_protected",
    )

    def save_cursor(self):
        """DECSC (ESC 7).

        A real VT saves more than the position: the graphic rendition and
        origin mode ride along, so a program can stash its whole drawing state,
        go and scribble somewhere else, and put everything back with one DECRC.
        """
        self.saved_cursor = (
            self.cursor.x,
            self.cursor.y,
            self.origin_mode,
            tuple(getattr(self, name) for name in self._SGR_ATTRS),
        )

    def restore_cursor(self):
        """DECRC (ESC 8). With nothing saved, a VT homes the cursor and resets
        attributes and origin mode, so that's what "restore" means here too."""
        if self.saved_cursor is None:
            self._reset_sgr()
            self.origin_mode = False
            self._set_cursor(0, 0)
            return
        x, y, origin_mode, sgr = self.saved_cursor
        for name, value in zip(self._SGR_ATTRS, sgr):
            setattr(self, name, value)
        # Restore origin mode *before* placing the cursor: it decides whether
        # the saved row is confined to the scroll region.
        self.origin_mode = origin_mode
        self._set_cursor(y, x)

    def save_cursor_pos(self):
        """SCOSC (ESC [ s): the ANSI.SYS variant — position only."""
        self.saved_pos = (self.cursor.x, self.cursor.y)

    def restore_cursor_pos(self):
        """SCORC (ESC [ u)."""
        x, y = self.saved_pos if self.saved_pos is not None else (0, 0)
        self._set_cursor(y, x)

    def erase_line(self, mode=0):
        row = self.screen[self.cursor.y]
        if mode == 1:  # start of line -> cursor
            x0, x1 = 0, self.cursor.x + 1
        elif mode == 2:  # whole line
            x0, x1 = 0, self.width
        else:  # 0: cursor -> end of line
            x0, x1 = self.cursor.x, self.width
        for x in range(x0, min(x1, self.width)):
            row[x] = Cell()

    def erase_display(self, mode=0):
        if mode == 1:  # start of screen -> cursor
            for y in range(self.cursor.y):
                self.screen[y] = self._blank_row()
            self.erase_line(1)
        elif mode == 2:  # whole visible screen
            self.screen = [self._blank_row() for _ in range(self.height)]
            self._clear_live_images()
        elif mode == 3:  # screen + scrollback (clear all)
            self.screen = [self._blank_row() for _ in range(self.height)]
            self.first_line_no += len(self.scrollback)  # keep numbering monotonic
            self.scrollback.clear()
            self.scroll_offset = 0
            self.images = [im for im in self.images if im.alt != self.alt_screen]
            self.zones = {
                zid: z for zid, z in self.zones.items() if z.alt != self.alt_screen
            }
        else:  # 0: cursor -> end of screen
            self.erase_line(0)
            for y in range(self.cursor.y + 1, self.height):
                self.screen[y] = self._blank_row()

    def _clear_live_images(self):
        """Remove images and zones sitting on the current live screen (ED 2)."""
        live = self.first_line_no + len(self.scrollback)
        self.images = [
            im for im in self.images if im.alt != self.alt_screen or im.top_line < live
        ]
        self.zones = {
            zid: z
            for zid, z in self.zones.items()
            if z.alt != self.alt_screen or z.top_line < live
        }

    def set_char_protection(self, mode):
        """DECSCA (ESC [ Ps " q): mark following glyphs erase-protected.

        Ps 1 protects; Ps 0 or 2 unprotects. This is deliberately separate from
        SGR — SGR 0 does not clear it — because a form draws its fixed labels
        protected, then lets the user type into the unprotected fields, and a
        stray color reset in the middle mustn't unprotect the labels.
        """
        self.current_protected = mode == 1

    def _erase_unprotected(self, row, x0, x1):
        for x in range(x0, min(x1, self.width)):
            if not row[x].protected:
                row[x] = Cell()

    def selective_erase_line(self, mode=0, y=None):
        """DECSEL (ESC [ ? Ps K): like EL, but leaves protected cells alone."""
        row = self.screen[self.cursor.y if y is None else y]
        if mode == 1:  # start of line -> cursor
            x0, x1 = 0, self.cursor.x + 1
        elif mode == 2:  # whole line
            x0, x1 = 0, self.width
        else:  # 0: cursor -> end of line
            x0, x1 = self.cursor.x, self.width
        self._erase_unprotected(row, x0, x1)

    def selective_erase_display(self, mode=0):
        """DECSED (ESC [ ? Ps J): like ED, but leaves protected cells alone."""
        if mode == 1:  # start of screen -> cursor
            for y in range(self.cursor.y):
                self._erase_unprotected(self.screen[y], 0, self.width)
            self.selective_erase_line(1)
        elif mode in (2, 3):  # whole screen (scrollback isn't protectable)
            for y in range(self.height):
                self._erase_unprotected(self.screen[y], 0, self.width)
        else:  # 0: cursor -> end of screen
            self.selective_erase_line(0)
            for y in range(self.cursor.y + 1, self.height):
                self._erase_unprotected(self.screen[y], 0, self.width)

    def soft_reset(self):
        """DECSTR (ESC [ ! p): soft terminal reset.

        Where RIS wipes everything, a soft reset leaves the screen, scrollback,
        tab stops and title alone and only returns the *modes and rendition* to
        power-on. It's what a program sends to get back to a known state without
        clearing the user's screen. The reset set follows the VT220 manual —
        notably DECAWM ends up OFF, and the DECSC save slot is cleared.
        """
        self.origin_mode = False  # DECOM
        self.insert_mode = False  # IRM
        self.autowrap = False  # DECAWM (per the VT220 manual)
        self.cursor_keys_app = False  # DECCKM
        self.keypad_app = False  # DECNKM
        self.backarrow_bs = False  # DECBKM
        self.cursor.visible = True  # DECTCEM
        self.current_protected = False  # DECSCA
        self.current_href = None  # OSC 8
        self.single_shift = None
        self.scroll_top, self.scroll_bottom = 0, self.height - 1
        self.saved_cursor = None
        self.saved_pos = None
        self.wrap_pending = False
        self._reset_sgr()
        self._set_cursor(0, 0)

    def insert_chars(self, n):
        """ICH: insert n blanks at the cursor, shifting the rest of the line
        right (cells pushed past the edge are lost)."""
        row = self.screen[self.cursor.y]
        x = self.cursor.x
        n = max(0, min(n, self.width - x))
        self.screen[self.cursor.y] = (
            row[:x] + [Cell() for _ in range(n)] + row[x : self.width - n]
        )

    def delete_chars(self, n):
        """DCH: delete n cells at the cursor, shifting the rest left and
        filling the end with blanks."""
        row = self.screen[self.cursor.y]
        x = self.cursor.x
        n = max(0, min(n, self.width - x))
        self.screen[self.cursor.y] = (
            row[:x] + row[x + n : self.width] + [Cell() for _ in range(n)]
        )

    def erase_chars(self, n):
        """ECH: erase n cells at the cursor in place (no shift)."""
        row = self.screen[self.cursor.y]
        for x in range(self.cursor.x, min(self.cursor.x + n, self.width)):
            row[x] = Cell()

    def insert_lines(self, n):
        """IL: insert n blank lines at the cursor row within the scroll region,
        pushing lower lines down (lines past the region bottom are lost)."""
        y = self.cursor.y
        if not (self.scroll_top <= y <= self.scroll_bottom):
            return
        n = max(0, min(n, self.scroll_bottom - y + 1))
        for _ in range(n):
            self.screen.insert(y, self._blank_row())
            del self.screen[self.scroll_bottom + 1]

    def delete_lines(self, n):
        """DL: delete n lines at the cursor row within the scroll region,
        pulling lower lines up and adding blanks at the region bottom."""
        y = self.cursor.y
        if not (self.scroll_top <= y <= self.scroll_bottom):
            return
        n = max(0, min(n, self.scroll_bottom - y + 1))
        for _ in range(n):
            del self.screen[y]
            self.screen.insert(self.scroll_bottom, self._blank_row())

    def set_scroll_region(self, params):
        """DECSTBM: set the top/bottom margins (1-based). Empty resets to the
        full screen. Homes the cursor."""
        top = params[0] if len(params) >= 1 and params[0] > 0 else 1
        bottom = params[1] if len(params) >= 2 and params[1] > 0 else self.height
        top -= 1
        bottom -= 1
        if 0 <= top < bottom < self.height:
            self.scroll_top = top
            self.scroll_bottom = bottom
        else:
            self.scroll_top = 0
            self.scroll_bottom = self.height - 1
        # DECSTBM homes the cursor (region top-left in origin mode).
        self._set_cursor(self.scroll_top if self.origin_mode else 0, 0)

    def _dec_private_mode(self, body, on):
        """Handle DEC private mode set/reset (ESC [ ? <n...> h/l)."""
        for part in body.split(";"):
            if not part.isdigit():
                continue
            mode = int(part)
            if mode == 25:  # DECTCEM: cursor visibility
                self.cursor.visible = on
            elif mode == 1:  # DECCKM: arrows send ESC O A instead of ESC [ A
                self.cursor_keys_app = on
            elif mode == 3:  # DECCOLM: 80/132 columns
                # We don't resize the window for a program, but the spec's
                # other side effects still happen, and apps rely on those.
                self.column_mode_132 = on
                self.scroll_top, self.scroll_bottom = 0, self.height - 1
                self.erase_display(2)
                self._set_cursor(0, 0)
            elif mode == 4:  # DECSCLM: smooth scroll — nothing to do
                self.smooth_scroll = on
            elif mode == 5:  # DECSCNM: reverse the whole screen
                self.reverse_video = on
            elif mode == 8:  # DECARM: auto-repeat is the OS's business
                self.autorepeat = on
            elif mode == 9:  # DECINLM on a VT100 (xterm reuses ?9 for X10 mouse)
                self.interlace = on
            elif mode == 7:  # DECAWM: auto-wrap
                self.autowrap = on
                if not on:
                    self.wrap_pending = False  # drop any deferred wrap
            elif mode == 6:  # DECOM: origin mode
                self.origin_mode = on
                # Setting/resetting DECOM homes the cursor to the origin.
                self._set_cursor(self.scroll_top if on else 0, 0)
            elif mode in (47, 1047, 1049):  # alternate screen buffer
                if on:
                    self.enter_alt_screen()
                else:
                    self.exit_alt_screen()
            elif mode in (1000, 1002, 1003):  # mouse tracking level
                self.mouse_mode = mode if on else 0
            elif mode == 1006:  # SGR extended mouse coords
                self.mouse_sgr = on
            elif mode == 2004:  # bracketed paste
                self.bracketed_paste = on
            elif mode == 66:  # DECNKM: application keypad, as a mode
                self.keypad_app = on
            elif mode == 67:  # DECBKM: backspace sends BS (on) or DEL (off)
                self.backarrow_bs = on

    def enter_alt_screen(self):
        if self.alt_screen:
            return
        self.alt_screen = True
        self.primary_screen = self.screen
        self.primary_cursor = (self.cursor.x, self.cursor.y)
        self.screen = [self._blank_row() for _ in range(self.height)]
        self.scroll_offset = 0
        self.scroll_top = 0
        self.scroll_bottom = self.height - 1
        self.cursor.x = 0
        self.cursor.y = 0

    def exit_alt_screen(self):
        if not self.alt_screen:
            return
        self.alt_screen = False
        self.screen = self.primary_screen
        self.primary_screen = None
        self.cursor.x, self.cursor.y = self.primary_cursor
        self.scroll_offset = 0
        self.scroll_top = 0
        self.scroll_bottom = self.height - 1
        # Images and zones placed on the alt screen are ephemeral, like its text.
        self.images = [im for im in self.images if not im.alt]
        self.zones = {zid: z for zid, z in self.zones.items() if not z.alt}

    def parse_osc(self, body):
        """OSC payload, 'Ps ; Pt'. 0 sets icon name + window title, 2 sets the
        title alone — both are what shells and SSH use to name a tab. 8 is the
        standard hyperlink sequence (ESC ] 8 ; params ; URI ST). 1 (icon name
        only) and the rest (clipboard, palette) are ignored.

        `YT` is YoTerm's own namespace (ESC ] YT ; ...): gradients, images, and
        a capability handshake. A non-YoTerm terminal ignores it entirely, so
        these degrade to plain text rather than to garbage."""
        code, sep, text = body.partition(";")
        if not sep:
            return
        if code in ("0", "2"):
            self.title = text
        elif code == "8":
            self._parse_hyperlink(text)
        elif code == "YT":
            self._parse_yt(text)

    def _parse_hyperlink(self, text):
        """OSC 8: 'params ; URI'. An empty URI ends the current link -- the
        params (e.g. 'id=xyz', for grouping a link that wraps across lines)
        aren't acted on, just consumed so they don't leak into the URI."""
        _params, _, uri = text.partition(";")
        self.current_href = uri if uri else None

    def _parse_yt(self, payload):
        """Dispatch a YoTerm OSC: 'verb ; args...'."""
        verb, _, rest = payload.partition(";")
        verb = verb.strip().lower()
        if verb == "?":  # capability handshake
            self.responses.append("\x1b]YT;version:1;feat:gradient,img,vid\x1b\\")
        elif verb in ("gradient", "grad"):
            self._yt_gradient(rest)
        elif verb == "img":
            self._yt_image(rest)
        elif verb == "vid":
            self._yt_video(rest)
        elif verb == "zone":
            self._yt_zone(rest)

    def _yt_gradient(self, rest):
        """YT;gradient — begin a true gradient over following text, or 'off'.

        Bare fields are color stops; 'key:value' fields are options. Also
        cleared by SGR 0, so it composes with an ordinary attribute reset."""
        fields = [f.strip() for f in rest.split(";") if f.strip()]
        if not fields or fields[0].lower() == "off":
            self.current_grad = None
            return
        stops, opts = [], {}
        for f in fields:
            if ":" in f:
                key, _, value = f.partition(":")
                opts[key.strip().lower()] = value.strip()
            else:
                stops.append(f)
        grad = make_gradient(stops, opts)
        if grad is not None:
            self.current_grad = grad

    def _prune_images(self):
        """Drop images whose whole span has scrolled out of history."""
        floor = self.first_line_no
        self.images = [im for im in self.images if im.top_line + im.rows > floor]

    def _prune_zones(self):
        """Drop zones whose whole span has scrolled out of history."""
        floor = self.first_line_no
        self.zones = {
            zid: z for zid, z in self.zones.items() if z.top_line + z.h > floor
        }

    def _anchor_row(self, y):
        """A screen row -> an absolute line number that survives scrolling."""
        return self.first_line_no + len(self.scrollback) + max(0, y)

    def _yt_zone(self, rest):
        """YT;zone — create / update / move / delete a styled rectangle.

        Geometry is in cells; `y` is resolved once into an absolute line so the
        zone scrolls with its text. Styling is a patch: only the fields actually
        named are touched, which is what makes per-frame animation cheap.
        """
        fields = [f.strip() for f in rest.split(";") if f.strip()]
        opts, action = {}, None
        for field in fields:
            if ":" in field:
                key, _, value = field.partition(":")
                opts[key.strip().lower()] = value.strip()
            else:
                action = field.lower()

        if action == "delete":
            wanted = opts.get("id", "")
            if wanted == "*":
                self.zones = {
                    zid: z for zid, z in self.zones.items() if z.alt != self.alt_screen
                }
            elif wanted.isdigit():
                self.zones.pop(int(wanted), None)
            return

        if not opts.get("id", "").isdigit():
            return  # every other verb needs an id
        zone_id = int(opts["id"])
        zone = self.zones.get(zone_id)

        if action == "create" or zone is None:
            x, y, w, h = geometry_from(opts, None)
            zone = Zone(zone_id, self._anchor_row(y or 0), x, w, h, self.alt_screen)
            self.zones[zone_id] = zone
        else:
            # update / move: only re-anchor when a row was actually given.
            x, y, w, h = geometry_from(opts, zone)
            zone.x, zone.w, zone.h = x, w, h
            if y is not None:
                zone.top_line = self._anchor_row(y)
        apply_style(zone, opts)

    def _yt_image(self, rest):
        """YT;img — place a GPU-sampled image at the cursor, or 'del' one.

        Bare 'del' (optionally 'id:N') removes a placed image; otherwise the
        image is decoded, sized in cells, and pinned to the grid so it scrolls
        with the surrounding text. Block placement (the default) reserves rows
        by moving the cursor below the image; inline:on keeps it on the line and
        advances the cursor past it, so a small icon can sit mid-sentence.
        """
        fields = [f.strip() for f in rest.split(";") if f.strip()]
        opts, action = {}, None
        for f in fields:
            if ":" in f:
                key, _, value = f.partition(":")
                opts[key.strip().lower()] = value.strip()
            else:
                action = f.lower()

        if action == "del":
            wanted = opts.get("id", "")
            if wanted.isdigit():
                self.images = [im for im in self.images if im.id != int(wanted)]
            else:
                self.images = [im for im in self.images if im.alt != self.alt_screen]
            return

        loaded = load_image(path=opts.get("path"), data=opts.get("data"))
        if loaded is None:
            return  # unreadable source: draw nothing, like an old terminal
        rgba, iw, ih = loaded
        cw, ch = self.cell_px
        inline = opts.get("inline", "").lower() in ("on", "true", "1", "yes")

        room_cols = self.width - self.cursor.x
        max_cols = room_cols if inline else self.width
        max_rows = 1 if inline else max(1, self.height)
        cols, rows = fit_cells(iw, ih, cw, ch, opts, max(1, max_cols), max_rows)
        if inline:
            rows = 1

        if opts.get("id", "").isdigit():  # a named image replaces its previous self
            img_id = int(opts["id"])
            self.images = [im for im in self.images if im.id != img_id]
        else:
            img_id, self._next_img_id = self._next_img_id, self._next_img_id + 1

        fit = opts.get("fit", "contain").lower()
        if fit not in ("contain", "fill", "cover"):
            fit = "contain"
        top_line = self.first_line_no + len(self.scrollback) + self.cursor.y
        self.images.append(
            ImagePlacement(
                img_id,
                top_line,
                self.cursor.x,
                cols,
                rows,
                rgba,
                iw,
                ih,
                self.alt_screen,
                fit,
            )
        )

        if inline:
            self.cursor.x = min(self.width - 1, self.cursor.x + cols)
        else:
            # Reserve the image's rows: drop the cursor below it, scrolling the
            # screen (and history) the same way a run of newlines would.
            self.cursor.x = 0
            for _ in range(rows):
                self.index()

    def _yt_video(self, rest):
        """YT;vid — play a video in-place. The model can't decode, so it reserves
        a placement (like an image) and hands the path to the app via
        video_requests; the app decodes on a worker thread and streams frames
        back into this placement. `del` (optionally id:N) stops/removes a video.

            YT;vid ;path:<file> [;cols:C ;rows:R ;id:N ;loop:on]
            YT;vid ;del [;id:N]
        """
        fields = [f.strip() for f in rest.split(";") if f.strip()]
        opts, action = {}, None
        for f in fields:
            if ":" in f:
                key, _, value = f.partition(":")
                opts[key.strip().lower()] = value.strip()
            else:
                action = f.lower()

        if action == "del":
            wanted = opts.get("id", "")
            if wanted.isdigit():
                self.images = [im for im in self.images if im.id != int(wanted)]
            else:
                self.images = [im for im in self.images if im.alt != self.alt_screen]
            return

        path = opts.get("path")
        if not path:
            return

        def _dim(value, default):
            try:
                return max(1, int(float(value)))
            except (TypeError, ValueError):
                return default

        max_cols = min(_dim(opts.get("cols"), max(1, self.width // 2)), self.width)
        max_rows = min(
            _dim(opts.get("rows"), max(1, self.height // 2)), max(1, self.height)
        )
        # Aspect-fit the reserved box to the video's real size (probed cheaply
        # from the file's metadata) so cols/rows act as a *bounding box*, like
        # YT;img: a 16:9 clip in a 48x24 box reserves ~48x13, not 48x24 with big
        # letterbox bands around the picture. Falls back to the raw box if the
        # file can't be probed.
        cw, ch = self.cell_px
        size = video_size(path)
        if size is not None and cw and ch:
            iw, ih = size
            aspect = iw / ih
            box_w, box_h = max_cols * cw, max_rows * ch
            if box_w / box_h > aspect:  # box wider than the video: fit to height
                fit_h, fit_w = box_h, box_h * aspect
            else:  # fit to width
                fit_w, fit_h = box_w, box_w / aspect
            cols = max(1, min(max_cols, round(fit_w / cw)))
            rows = max(1, min(max_rows, round(fit_h / ch)))
        else:
            cols, rows = max_cols, max_rows

        if opts.get("id", "").isdigit():  # a named video replaces its previous self
            img_id = int(opts["id"])
            self.images = [im for im in self.images if im.id != img_id]
        else:
            img_id, self._next_img_id = self._next_img_id, self._next_img_id + 1

        # Block placement starts on a fresh line at column 0: a shell often emits
        # the sequence mid-line (right after its prompt), so this keeps the video
        # from straddling the prompt or landing at whatever column the cursor sat.
        if self.cursor.x > 0:
            self.cursor.x = 0
            self.index()
        # A 1x1 transparent placeholder until the first decoded frame lands; the
        # app then swaps in real pixels (and dimensions) frame by frame.
        top_line = self.first_line_no + len(self.scrollback) + self.cursor.y
        self.images.append(
            ImagePlacement(
                img_id,
                top_line,
                0,
                cols,
                rows,
                b"\x00\x00\x00\x00",
                1,
                1,
                self.alt_screen,
                "contain",
            )
        )
        # Reserve the rows, exactly like a block image.
        self.cursor.x = 0
        for _ in range(rows):
            self.index()

        loop = opts.get("loop", "").lower() in ("on", "true", "1", "yes")
        mute = opts.get("mute", "").lower() in ("on", "true", "1", "yes")
        fs = opts.get("fullscreen", "").lower() in ("on", "true", "1", "yes")
        self.video_requests.append(
            {
                "id": img_id,
                "path": path,
                "cols": cols,
                "rows": rows,
                "loop": loop,
                "mute": mute,
                "fullscreen": fs,
                "alt": self.alt_screen,
            }
        )

    def parse_escape(self, seq):
        """Dispatch a CSI sequence. `seq` includes the leading '[' and the
        final byte, e.g. '[10;20H'."""
        final = seq[-1]

        if final == "m":
            self._apply_sgr(self.parse_params(seq))
            return

        # DEC private modes: ESC [ ? <n> h  (set) / ESC [ ? <n> l  (reset),
        # and ESC [ ? <n> $ p (DECRQM) to ask whether one is set.
        if seq.startswith("[?"):
            if final in "hl":
                self._dec_private_mode(seq[2:-1], final == "h")
            elif final == "p" and seq.endswith("$p"):
                self.report_mode(seq[2:-2], private=True)
            elif final == "J":  # DECSED: selective erase in display
                body = seq[2:-1]
                self.selective_erase_display(int(body) if body.isdigit() else 0)
            elif final == "K":  # DECSEL: selective erase in line
                body = seq[2:-1]
                self.selective_erase_line(int(body) if body.isdigit() else 0)
            return  # other private sequences ignored

        if final == "p" and seq.endswith("$p"):  # DECRQM, ANSI (non-private)
            self.report_mode(seq[1:-2], private=False)
            return

        if final == "p" and seq.endswith("!p"):  # DECSTR: soft reset
            self.soft_reset()
            return

        if final == "p" and seq.endswith('"p'):  # DECSCL: conformance level
            body = seq[1:-2].split(";")[0]
            self.conformance_level = int(body) if body.isdigit() else 62
            return

        if final == "q" and seq.endswith('"q'):  # DECSCA: character protection
            body = seq[1:-2]
            self.set_char_protection(int(body) if body.isdigit() else 0)
            return

        if final in "hl":  # SM / RM: ANSI modes (the private ones went above)
            self._ansi_mode(seq[1:-1], final == "h")
            return

        if final == "q" and seq.endswith(" q"):  # DECSCUSR: cursor style
            body = seq[1:-2]  # 'q' has a ' ' intermediate
            self.set_cursor_style(int(body) if body.isdigit() else 0)
            return

        if final == "c":  # DA: "what kind of terminal are you?"
            if seq.startswith("[>"):  # DA2 (secondary): firmware level
                self.responses.append("\x1b[>0;10;1c")
            elif not seq.startswith("[="):  # DA1 (primary): VT100 + adv. video
                self.responses.append("\x1b[?1;2c")
            return

        params = self.parse_params(seq)
        p0 = params[0] if params else 0

        if final in "Hf":  # CUP / HVP (row;col, 1-based)
            row = params[0] if len(params) >= 1 and params[0] > 0 else 1
            col = params[1] if len(params) >= 2 and params[1] > 0 else 1
            self._set_row_col(row, col)
        elif final == "A":
            self.cursor_up(max(1, p0))
        elif final == "B":
            self.cursor_down(max(1, p0))
        elif final == "C":
            self.cursor_forward(max(1, p0))
        elif final == "D":
            self.cursor_back(max(1, p0))
        elif final == "E":  # cursor next line
            self.cursor_down(max(1, p0))
            self.cursor.x = 0
        elif final == "F":  # cursor previous line
            self.cursor_up(max(1, p0))
            self.cursor.x = 0
        elif final in "G`":  # CHA / HPA (column, 1-based)
            self.wrap_pending = False
            self.cursor.x = max(0, min(self.width - 1, (p0 or 1) - 1))
        elif final == "d":  # VPA (row, 1-based)
            self.wrap_pending = False
            self.cursor.y = self._abs_row(p0 or 1)
        elif final == "J":
            self.erase_display(p0)
        elif final == "K":
            self.erase_line(p0)
        elif final == "@":  # ICH: insert blank chars
            self.insert_chars(max(1, p0))
        elif final == "P":  # DCH: delete chars
            self.delete_chars(max(1, p0))
        elif final == "X":  # ECH: erase chars
            self.erase_chars(max(1, p0))
        elif final == "L":  # IL: insert lines
            self.insert_lines(max(1, p0))
        elif final == "M":  # DL: delete lines
            self.delete_lines(max(1, p0))
        elif final == "S":  # SU: scroll region up
            self._scroll_region_up(max(1, p0))
        elif final == "T":  # SD: scroll region down
            self._scroll_region_down(max(1, p0))
        elif final == "I":  # CHT: tab forward n stops
            self.cursor_forward_tab(max(1, p0))
        elif final == "Z":  # CBT: tab backward n stops
            self.cursor_back_tab(max(1, p0))
        elif final == "b":  # REP: repeat the last glyph
            self.repeat_last(max(1, p0))
        elif final == "g":  # TBC: clear tab stop(s)
            self.clear_tab_stop(p0)
        elif final == "r":  # DECSTBM: set scroll region
            self.set_scroll_region(params)
        elif final == "s":  # SCOSC: save cursor position (ANSI.SYS)
            self.save_cursor_pos()
        elif final == "u":  # SCORC: restore it
            self.restore_cursor_pos()
        elif final == "x":  # DECREQTPARM: report terminal parameters
            # Answer as a VT100 would: 8 bits, no parity, 9600 baud both ways.
            # A request of 0 reports 2, a request of 1 reports 3.
            if p0 in (0, 1):
                self.responses.append("\x1b[%d;1;1;120;120;1;0x" % (p0 + 2))
        elif final == "q":  # DECLL: load the keyboard LEDs — we have no LEDs
            pass
        elif final == "n":  # DSR: device status report
            if p0 == 6:  # report cursor position
                row = self.cursor.y + 1
                if self.origin_mode:  # reported relative to the region
                    row = self.cursor.y - self.scroll_top + 1
                self.responses.append("\x1b[%d;%dR" % (row, self.cursor.x + 1))
            elif p0 == 5:  # report terminal OK
                self.responses.append("\x1b[0n")
        # Remaining CSI finals are not implemented and are safely ignored.

    def _apply_sgr(self, params):
        if not params:
            params = [0]
        i, n = 0, len(params)
        while i < n:
            code = params[i]
            if code in (38, 48):  # extended color (256 / truecolor)
                color, step = None, 1
                if i + 2 < n and params[i + 1] == 5:
                    color, step = color_256(params[i + 2]), 3
                elif i + 4 < n and params[i + 1] == 2:
                    r, g, b = params[i + 2], params[i + 3], params[i + 4]
                    color = (
                        min(255, r) / 255.0,
                        min(255, g) / 255.0,
                        min(255, b) / 255.0,
                    )
                    step = 5
                if color is not None:
                    if code == 38:
                        self.current_fg = color
                    else:
                        self.current_bg = color
                i += step
                continue
            self._sgr_code(code)
            i += 1

    def _sgr_code(self, code):
        if code == 0:
            self._reset_sgr()
        elif code == 1:
            self.current_bold = True
        elif code == 2:
            self.current_dim = True
        elif code == 3:
            self.current_italic = True
        elif code == 4:
            self.current_underline = True
        elif code == 5:  # SGR 5: blink
            self.current_blink = True
        elif code == 7:
            self.current_reverse = True
        elif code == 8:
            self.current_conceal = True
        elif code == 9:
            self.current_strike = True
        elif code == 22:
            self.current_bold = False
            self.current_dim = False
        elif code == 23:
            self.current_italic = False
        elif code == 24:
            self.current_underline = False
        elif code == 25:  # SGR 25: steady (blink off)
            self.current_blink = False
        elif code == 27:
            self.current_reverse = False
        elif code == 28:
            self.current_conceal = False
        elif code == 29:
            self.current_strike = False
        elif code in SGR_FG:
            self.current_fg = SGR_FG[code]
        elif code in SGR_BG:
            self.current_bg = SGR_BG[code]
        elif code in SGR_FG_BRIGHT:
            self.current_fg = SGR_FG_BRIGHT[code]
        elif code in SGR_BG_BRIGHT:
            self.current_bg = SGR_BG_BRIGHT[code]

    def _reset_sgr(self):
        self.current_fg = "default"
        self.current_bg = "default"
        self.current_bold = False
        self.current_dim = False
        self.current_italic = False
        self.current_underline = False
        self.current_reverse = False
        self.current_strike = False
        self.current_conceal = False
        self.current_blink = False
        self.current_grad = None

    def _end_escape(self):
        self.escape = False
        self.escape_buffer = ""

    def consume_escape(self, char):
        """Feed one char into an in-progress escape sequence. Handles the
        sequence families a real shell (ConPTY) actually emits:

          CSI  ESC [ ... <letter>        cursor moves, SGR, erase, ...
          OSC  ESC ] ... (BEL | ESC \\)  window title, etc. (ignored)
          ESC (X / ESC )X               charset select (ignored)
          ESC x                         other 2-byte sequences (ignored)
        """
        self.escape_buffer += char
        buf = self.escape_buffer
        kind = buf[0]

        if kind == "[":  # CSI: ends on a final byte 0x40-0x7E
            # buf[0] is the '[' intro; the final byte is a *later* char.
            if len(buf) >= 2 and 0x40 <= ord(char) <= 0x7E:
                # Leave escape state *before* dispatching: a handler that
                # writes glyphs (REP) re-enters put_char, and would otherwise
                # have its output swallowed back into the escape buffer.
                self._end_escape()
                self.parse_escape(buf)
            elif ord(char) < 0x20:  # stray control char -> abort
                self._end_escape()
                self.put_char(char)

        elif kind == "]":  # OSC: ESC ] Ps ; Pt (BEL | ST)
            if char == "\x07":  # BEL terminates
                self.parse_osc(buf[1:-1])
                self._end_escape()
            elif char == "\\" and buf.endswith("\x1b\\"):  # ST (ESC \) terminates
                self.parse_osc(buf[1:-2])
                self._end_escape()
            elif char == "\x1b":
                pass  # may be ST's first byte
            elif buf[-2:-1] == "\x1b" or ord(char) < 0x20:
                # An ESC not followed by '\', or any other C0: the OSC was never
                # terminated. Abort so it can't swallow the rest of the output,
                # and let the stray bytes act normally.
                self._end_escape()
                if buf[-2:-1] == "\x1b":
                    self.put_char("\x1b")
                self.put_char(char)
            elif len(buf) > MAX_OSC:  # runaway: give up
                self._end_escape()

        elif kind in "()*+":  # SCS: ESC ( ) * + designate G0 G1 G2 G3
            if len(buf) >= 2:
                self._end_escape()
                self.charsets["()*+".index(kind)] = char

        elif kind == "#":  # ESC # <n>: DEC line attributes / screen tests
            if len(buf) >= 2:
                self._end_escape()
                if char == "8":  # DECALN
                    self.screen_alignment()
                # 3/4 DECDHL, 5 DECSWL, 6 DECDWL: double-height/width lines.
                # Accepted and ignored — we render every line single-width —
                # but they must still be *consumed*, or the digit lands on the
                # screen as a stray glyph.

        else:  # other 2-byte ESC sequence
            self._end_escape()  # end first, as above
            if char == "7":  # DECSC: save cursor
                self.save_cursor()
            elif char == "8":  # DECRC: restore cursor
                self.restore_cursor()
            elif char == "D":  # IND: index
                self.index()
            elif char == "M":  # RI: reverse index
                self.reverse_index()
            elif char == "E":  # NEL: next line
                self.next_line()
            elif char == "H":  # HTS: set a tab stop here
                self.set_tab_stop()
            elif char == "=":  # DECKPAM: keypad application mode
                self.keypad_app = True
            elif char == ">":  # DECKPNM: keypad numeric mode
                self.keypad_app = False
            elif char == "Z":  # DECID: identify, same answer as DA1
                self.responses.append("\x1b[?1;2c")
            elif char == "c":  # RIS: reset to initial state
                self.reset()
            elif char == "n":  # LS2: lock G2 into GL
                self.charset_gl = 2
            elif char == "o":  # LS3: lock G3 into GL
                self.charset_gl = 3
            elif char == "N":  # SS2: next glyph from G2
                self.single_shift = 2
            elif char == "O":  # SS3: next glyph from G3
                self.single_shift = 3

    def put_char(self, char):
        # CAN/SUB abandon whatever escape sequence is in flight. Checked before
        # the escape branch precisely because that's what they're for.
        if char in ("\x18", "\x1a"):
            self._end_escape()
            return

        if self.escape:
            self.consume_escape(char)
            return

        if char == "\x1b":
            self.escape = True
            self.escape_buffer = ""
            return

        # LF, VT and FF all index. With LNM off (the default) they keep the
        # column, which is why a bare '\n' doesn't return to column 1 — the
        # shell sends CRLF, or sets LNM if it wants otherwise.
        if char in ("\n", "\x0b", "\x0c"):
            self.wrap_pending = False
            if self.newline_mode:
                self.next_line()
            else:
                self.index()
            return

        if char == "\r":
            self.wrap_pending = False
            self.carriage()
            return

        if char == "\b":
            self.wrap_pending = False
            self.backspace()
            return

        if char == "\t":
            self.wrap_pending = False
            self.tab()
            return

        if char == "\x07":  # BEL
            self.bell_count += 1
            return

        if char == "\x0e":  # SO: map G1 into GL
            self.charset_gl = 1
            return

        if char == "\x0f":  # SI: back to G0
            self.charset_gl = 0
            return

        # Ignore any other control char so it doesn't get written as a glyph
        # and wrongly advance the cursor.
        if ord(char) < 32:
            return

        # A pending single shift (SS2/SS3) picks G2/G3 for this one glyph, then
        # GL reverts on its own; otherwise use whatever's locked into GL.
        if self.single_shift is not None:
            gl = self.single_shift
            self.single_shift = None
        else:
            gl = self.charset_gl
        charset = self.charsets[gl]
        if charset == "0":
            # DEC Special Graphics: plain ASCII becomes line-drawing.
            char = DEC_GRAPHICS.get(char, char)
        elif charset == "A" and char == "#":
            char = "£"  # UK national set: '#' is where the pound sign lives

        w = char_width(char)
        if w == 0:
            # A combining mark has no cell of its own: it belongs to the glyph
            # already on screen. Anything else zero-width is formatting.
            if unicodedata.category(char) in _MARK_CATEGORIES:
                self._add_combining(char)
            return

        # Deferred auto-wrap: a glyph written in the last column set a pending
        # flag rather than wrapping immediately. Now that another glyph is
        # arriving, perform the wrap first.
        if self.wrap_pending:
            self.wrap_pending = False
            self.newline()

        # A wide glyph needs two columns; wrap if only one remains.
        if w == 2 and self.cursor.x >= self.width - 1 and self.autowrap:
            self.newline()

        # Defensive: a bad cursor position (e.g. from an out-of-range CUP)
        # must never index out of bounds.
        self.cursor.x = max(0, min(self.width - 1, self.cursor.x))
        self.cursor.y = max(0, min(self.height - 1, self.cursor.y))

        x, y = self.cursor.x, self.cursor.y
        if self.insert_mode:
            self.insert_chars(w)  # IRM: shove the rest of the line right
        self.screen[y][x] = self._new_cell(char, width=w)
        if w == 2 and x + 1 < self.width:
            # Trailing spacer: same background, no glyph of its own.
            self.screen[y][x + 1] = self._new_cell(" ", width=0)
        self.last_graphic = char  # REP repeats this

        nx = x + w
        if nx >= self.width:
            self.cursor.x = self.width - 1
            # With auto-wrap on, defer the wrap until the next glyph. With it
            # off, the cursor just sticks here and later glyphs overwrite.
            self.wrap_pending = self.autowrap
        else:
            self.cursor.x = nx

    def _add_combining(self, mark):
        """Attach a combining mark to the glyph the cursor just left.

        The mark doesn't advance the cursor and must not trigger a pending
        wrap — it modifies a cell that's already been written.
        """
        y = self.cursor.y
        # After a normal glyph the cursor has moved past it; but a glyph in the
        # last column parks the cursor *on* itself (deferred wrap).
        x = self.cursor.x if self.wrap_pending else self.cursor.x - 1
        # Step back over the trailing spacer of a wide glyph to reach its base.
        if x > 0 and self.screen[y][x].width == 0:
            x -= 1
        if x < 0:
            return  # nothing on this line to attach to

        cell = self.screen[y][x]
        if len(cell.char) > MAX_MARKS:
            return
        # Prefer a precomposed form: "e" + U+0301 becomes "é", which the font
        # draws as one glyph. Only what NFC can't compose needs mark stacking.
        cell.char = unicodedata.normalize("NFC", cell.char + mark)

    def _new_cell(self, char, width=1):
        return Cell(
            char=char,
            fg=self.current_fg,
            bg=self.current_bg,
            bold=self.current_bold,
            dim=self.current_dim,
            italic=self.current_italic,
            underline=self.current_underline,
            reverse=self.current_reverse,
            strike=self.current_strike,
            conceal=self.current_conceal,
            blink=self.current_blink,
            protected=self.current_protected,
            grad=self.current_grad,
            href=self.current_href,
            width=width,
        )

    def write(self, data, end: str = "\n"):
        self._feed(data)
        self._feed(end)

    def _feed(self, data):
        """Consume a run of input. Ordinary characters go through put_char one by
        one (each may place a glyph); but while an OSC payload is accumulating,
        the long run of non-control bytes — a base64 image or video frame — is
        swallowed in a single slice. Feeding those char-by-char is O(n) Python
        calls on the UI thread and is what made large `YT;img` frames hang it."""
        i = 0
        n = len(data)
        while i < n:
            # Only bulk-consume when mid-OSC AND not sitting on a pending ESC:
            # the ST terminator is `ESC \`, and `\` isn't a control byte, so a
            # buffer ending in ESC must fall through to consume_escape or the
            # fast-path would swallow the `\` and the OSC would never terminate.
            if (
                self.escape
                and self.escape_buffer[:1] == "]"
                and not self.escape_buffer.endswith("\x1b")
            ):
                m = _OSC_RUN.match(data, i)
                if m is not None:
                    self.escape_buffer += m.group()
                    if len(self.escape_buffer) > MAX_OSC:  # runaway: give up
                        self._end_escape()
                    i = m.end()
                    continue
            self.put_char(data[i])
            i += 1
