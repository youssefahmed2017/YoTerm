"""app_glfw.py — a PySide6-free prototype shell for YoTerm.

Same terminal model (term.py) and the same ModernGL rendering approach as
app.py, but the window, tabs, settings panel and input all come from GLFW +
Dear ImGui instead of Qt. That swaps a ~370 MB PySide6 dependency for a few MB
(glfw + pyimgui + PyOpenGL).

This is a PROTOTYPE that lives ALONGSIDE app.py — app.py is untouched and stays
the reference build. Run it with:

    python app_glfw.py

Scope of this prototype: real tabs, a real shell per tab, the core terminal
render (backgrounds, glyphs, cursor), an in-app settings overlay, font zoom,
scrollback, and clipboard. The gradient/image passes (YT sequences) use the
exact same ModernGL programs as app.py and port over unchanged; they're left
out here only to keep the prototype focused on proving the framework swap.
"""

# PyOpenGL (which pyimgui's renderer uses) error-checks after every GL call and
# trips over the error flag ModernGL legitimately leaves set. Turning its
# per-call checking off is the standard fix for sharing one context between the
# two — must happen before OpenGL.GL is imported anywhere.
import OpenGL

OpenGL.ERROR_CHECKING = False
OpenGL.ERROR_LOGGING = False

import math
import os
import queue
import sys
import threading
import time
from array import array
from dataclasses import fields

import glfw
import imgui
import moderngl
from imgui.integrations.glfw import GlfwRenderer
from PIL import Image, ImageDraw
from winpty import PtyProcess

from tools import RectangleBuilder, DynamicAtlas, PALETTE
import config as config_module
from config import YTConfig, SHELLS, CURSOR_STYLES
from term import Terminal

# ---- constants (mirrored from app.py so this file stands alone) -------------
CONFIG = YTConfig()
_FONT_META = {f.name: f.metadata for f in fields(YTConfig)}["font_size"]
MIN_FONT_PX = _FONT_META.get("min", 8)
MAX_FONT_PX = _FONT_META.get("max", 72)

SUPERSAMPLE = 3
GUTTER = 2
DIM_FACTOR = 0.55
CURSOR_COLOR = (0.90, 0.92, 0.98)
CURSOR_THICK_PX = 2.0
CURSOR_BLINK_PERIOD = 1.2  # seconds for one on->off->on cycle
CURSOR_BLINK_DELAY = 0.5  # stay solid this long after activity
HEADER = 40  # tab-strip height (matches app.py HEADER_H)
WHEEL_LINES = 3


def _hex(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def make_logo_image(size=64):
    """The YoTerm mark as an RGBA image: a rounded blue tile with a '>_' prompt,
    matching app.py's make_logo. Used for the GLFW window icon."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=size * 0.22, fill=(70, 120, 242, 255)
    )
    w = max(2, int(size * 0.09))
    white = (255, 255, 255, 255)
    d.line(
        [
            (size * 0.27, size * 0.33),
            (size * 0.45, size * 0.51),
            (size * 0.27, size * 0.69),
        ],
        fill=white,
        width=w,
        joint="curve",
    )
    d.line(
        [(size * 0.54, size * 0.69), (size * 0.75, size * 0.69)], fill=white, width=w
    )
    return img


# The exact PySide6 chrome palette (app.py), so the two builds look identical.
UI_BG = _hex("#0f0f14")  # terminal surface + the selected tab
UI_STRIP = _hex("#17171f")  # the tab strip behind the tabs
UI_TAB_HOVER = _hex("#22222e")
UI_TEXT = _hex("#9a9aa8")  # inactive tab text
UI_TEXT_HOVER = _hex("#d6d6e2")
UI_TEXT_ACTIVE = _hex("#ffffff")
UI_ACCENT = _hex("#5a7fe0")  # 2px top border on the selected tab
UI_CLOSE_HOVER = _hex("#c4404a")
UI_DIALOG_BG = _hex("#14141b")
UI_HINT = _hex("#7a7a88")

BG_COLOR = UI_BG  # terminal clear colour == the selected-tab fill

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
UI_FONT_PX = 18.0  # Dear ImGui chrome font size

VERTEX_SHADER = """
#version 330
in vec2 in_corner;
in vec2 in_pos;
in vec2 in_size;
in vec3 in_color;
in vec2 in_uv0;
in vec2 in_uv1;
in float in_mode;
out vec3 v_color;
out vec2 v_uv;
out float v_mode;
void main() {
    gl_Position = vec4(in_pos + in_corner * in_size, 0, 1);
    v_uv = mix(in_uv0, in_uv1, in_corner);
    v_color = in_color;
    v_mode = in_mode;
}
"""

FRAGMENT_SHADER = """
#version 330
uniform sampler2D tex;
in vec3 v_color;
in vec2 v_uv;
in float v_mode;
out vec4 color;
void main() {
    vec4 texel = texture(tex, v_uv);
    if (v_mode > 0.5) color = texel;
    else color = vec4(v_color, texel.a);
}
"""

# Per-vertex gradient text (YT;gradient): each glyph corner carries its own
# colour, sampled from the run's ramp, so the colour is smooth within a glyph.
GRAD_VERTEX_SHADER = """
#version 330
in vec2 in_vpos;
in vec2 in_vuv;
in vec3 in_vcol;
out vec2 v_uv;
out vec3 v_col;
void main() { gl_Position = vec4(in_vpos, 0, 1); v_uv = in_vuv; v_col = in_vcol; }
"""
GRAD_FRAGMENT_SHADER = """
#version 330
uniform sampler2D tex;
in vec2 v_uv;
in vec3 v_col;
out vec4 color;
void main() { color = vec4(v_col, texture(tex, v_uv).a); }
"""
_GRAD_FLOATS_PER_VERT = 7

# Textured quad for images (YT;img), sampled from unit 1 so the glyph atlas on
# unit 0 is undisturbed.
IMAGE_VERTEX_SHADER = """
#version 330
in vec2 in_pos;
in vec2 in_uv;
out vec2 v_uv;
void main() { gl_Position = vec4(in_pos, 0, 1); v_uv = in_uv; }
"""
IMAGE_FRAGMENT_SHADER = """
#version 330
uniform sampler2D img;
in vec2 v_uv;
out vec4 color;
void main() { color = texture(img, v_uv); }
"""


# ---- shared glyph atlas + shell environment (from app.py, Qt-free) ----------
_SHARED_ATLAS = None
_SHARED_ATLAS_PX = None


def shared_atlas(font_px):
    global _SHARED_ATLAS, _SHARED_ATLAS_PX
    if _SHARED_ATLAS is None or _SHARED_ATLAS_PX != font_px:
        _SHARED_ATLAS = DynamicAtlas(px=font_px * SUPERSAMPLE, pad=GUTTER * SUPERSAMPLE)
        _SHARED_ATLAS_PX = font_px
    return _SHARED_ATLAS


def registry_path():
    if sys.platform != "win32":
        return []
    import winreg

    entries = []
    for root, key in (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(root, key) as handle:
                value, kind = winreg.QueryValueEx(handle, "Path")
        except OSError:
            continue
        if kind == winreg.REG_EXPAND_SZ:
            value = os.path.expandvars(value)
        entries.extend(p for p in value.split(os.pathsep) if p)
    return entries


def shell_env():
    env = dict(os.environ)
    inherited = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    seen = {p.lower().rstrip("\\") for p in inherited}
    extra = [
        p
        for p in registry_path()
        if p.lower().rstrip("\\") not in seen and not seen.add(p.lower().rstrip("\\"))
    ]
    if extra:
        env["PATH"] = os.pathsep.join(inherited + extra)
    return env


# ---- one shell + terminal behind one tab ------------------------------------
class Session:
    def __init__(self, cols, rows, cell_px):
        self.term = Terminal(cols, rows)
        self.term.cursor.shape = CONFIG.shape()
        self.term.cell_px = cell_px
        self.out_queue = queue.Queue()
        self.alive = True
        self.title = CONFIG.shell
        try:
            self.pty = PtyProcess.spawn(
                [CONFIG.shell], dimensions=(rows, cols), env=shell_env()
            )
            threading.Thread(target=self._reader, daemon=True).start()
        except Exception as exc:
            self.pty = None
            self.term.write(
                "\x1b[31mYoTerm: couldn't start %s\x1b[0m\r\n  %s\r\n"
                % (CONFIG.shell, exc),
                end="",
            )

    def _reader(self):
        try:
            while True:
                data = self.pty.read(4096)
                if data:
                    self.out_queue.put(data)
        except EOFError:
            pass
        finally:
            self.out_queue.put(None)

    def drain(self):
        """Feed queued shell output into the model. Returns True if anything
        changed (so the frame is worth rebuilding)."""
        changed = False
        while True:
            try:
                data = self.out_queue.get_nowait()
            except queue.Empty:
                break
            if data is None:
                self.alive = False
                break
            self.term.write(data, end="")
            changed = True
        if self.term.responses:
            for reply in self.term.responses:
                self.write(reply)
            self.term.responses.clear()
        if self.term.title:
            self.title = self.term.title
        return changed

    def write(self, data):
        try:
            if self.pty and self.pty.isalive():
                self.pty.write(data)
        except (EOFError, OSError):
            pass

    def send(self, data):
        self.term.scroll_to_bottom()
        self.write(data)

    def resize(self, cols, rows, cell_px):
        self.term.cell_px = cell_px
        self.term.resize(cols, rows)
        try:
            if self.pty and self.pty.isalive():
                self.pty.setwinsize(rows, cols)
        except (EOFError, OSError):
            pass

    def close(self):
        try:
            if self.pty and self.pty.isalive():
                self.pty.terminate(force=True)
        except (EOFError, OSError):
            pass


# ---- special-key -> escape sequences ----------------------------------------
def _arrow(term, letter):
    return ("\x1bO" if term.cursor_keys_app else "\x1b[") + letter


def key_to_bytes(term, key, mods):
    ctrl = bool(mods & glfw.MOD_CONTROL)
    special = {
        glfw.KEY_ENTER: "\r",
        glfw.KEY_KP_ENTER: "\r",
        glfw.KEY_BACKSPACE: "\x7f",
        glfw.KEY_TAB: "\t",
        glfw.KEY_ESCAPE: "\x1b",
        glfw.KEY_UP: _arrow(term, "A"),
        glfw.KEY_DOWN: _arrow(term, "B"),
        glfw.KEY_RIGHT: _arrow(term, "C"),
        glfw.KEY_LEFT: _arrow(term, "D"),
        glfw.KEY_HOME: _arrow(term, "H"),
        glfw.KEY_END: _arrow(term, "F"),
        glfw.KEY_DELETE: "\x1b[3~",
    }
    if key in special:
        return special[key]
    if ctrl and glfw.KEY_A <= key <= glfw.KEY_Z:
        return chr((key - glfw.KEY_A) + 1)  # Ctrl+A..Z -> 0x01..0x1A
    return None


# ---- the app -----------------------------------------------------------------
class YoTermGlfw:
    def __init__(self):
        if not glfw.init():
            raise SystemExit("GLFW init failed")
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        if os.environ.get("YOTERM_HEADLESS"):
            glfw.window_hint(glfw.VISIBLE, glfw.FALSE)  # offscreen for tests
        self.window = glfw.create_window(1000, 640, "YoTerm", None, None)
        if not self.window:
            glfw.terminate()
            raise SystemExit("GLFW window creation failed")
        glfw.make_context_current(self.window)
        glfw.swap_interval(1)
        try:  # the YoTerm mark as the taskbar / titlebar icon
            glfw.set_window_icon(
                self.window, 2, [make_logo_image(64), make_logo_image(32)]
            )
        except Exception:
            pass

        self.ctx = moderngl.create_context()
        self.font_px = CONFIG.font_size
        self.atlas = shared_atlas(self.font_px)
        self.cell_w = max(1, round(self.atlas.glyph_w / SUPERSAMPLE))
        self.cell_h = max(1, round(self.atlas.glyph_h / SUPERSAMPLE))
        self._atlas_cursor = 0
        self._color_cache = {}
        self._start = time.monotonic()  # for animated (cycling) gradients
        self._cursor_active = self._start  # caret holds solid, then blinks

        self.program = self.ctx.program(
            vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER
        )
        self.program["tex"] = 0
        self.texture = self.ctx.texture(
            (self.atlas.width, self.atlas.height), 4, self.atlas.image.tobytes()
        )
        self.texture.build_mipmaps()
        self.texture.anisotropy = self.ctx.max_anisotropy
        self.quad_vbo = self.ctx.buffer(RectangleBuilder.CORNERS)
        self.vbo = self.ctx.buffer(reserve=4_000_000)
        self.vao = self.ctx.vertex_array(
            self.program,
            [
                (self.quad_vbo, "2f", "in_corner"),
                (
                    self.vbo,
                    "2f 2f 3f 2f 2f 1f/i",
                    "in_pos",
                    "in_size",
                    "in_color",
                    "in_uv0",
                    "in_uv1",
                    "in_mode",
                ),
            ],
        )

        # YT;gradient — per-vertex coloured glyphs (own program + buffer).
        self.grad_program = self.ctx.program(
            vertex_shader=GRAD_VERTEX_SHADER, fragment_shader=GRAD_FRAGMENT_SHADER
        )
        self.grad_program["tex"] = 0
        self.grad_vbo = self.ctx.buffer(reserve=64_000)
        self.grad_vao = self.ctx.vertex_array(
            self.grad_program,
            [(self.grad_vbo, "2f 2f 3f", "in_vpos", "in_vuv", "in_vcol")],
        )
        self._grad_glyphs = []
        self._grad_bbox = {}
        self._grad_specs = {}

        # YT;img — one textured quad per image, cached by placement identity.
        self.img_program = self.ctx.program(
            vertex_shader=IMAGE_VERTEX_SHADER, fragment_shader=IMAGE_FRAGMENT_SHADER
        )
        self.img_program["img"] = 1
        self.img_vbo = self.ctx.buffer(reserve=6 * 4 * 4)
        self.img_vao = self.ctx.vertex_array(
            self.img_program, [(self.img_vbo, "2f 2f", "in_pos", "in_uv")]
        )
        self._img_textures = {}

        imgui.create_context()
        self._apply_theme()
        self._load_ui_font()  # before GlfwRenderer, which bakes the atlas
        self.impl = GlfwRenderer(self.window, attach_callbacks=False)
        glfw.set_key_callback(self.window, self._key_cb)
        glfw.set_char_callback(self.window, self._char_cb)
        glfw.set_scroll_callback(self.window, self._scroll_cb)
        glfw.set_window_size_callback(self.window, lambda *_: None)

        self._open_settings = False  # request to open the settings modal
        self._open_menu = False  # request to open the ⌄ dropdown menu
        self._open_about = False  # request to open the About modal
        self._menu_x = 0.0  # x of the dropdown, under the ⌄ button
        self.sessions = []
        self.active = 0
        self._region()  # sets self.cols/self.rows for the first tab
        self.new_tab()

    # -- geometry ------------------------------------------------------------
    def _scale(self):
        ww, _ = glfw.get_window_size(self.window)
        fw, _ = glfw.get_framebuffer_size(self.window)
        return (fw / ww) if ww else 1.0

    def _region(self):
        """Logical size of the terminal area (below the tab strip) + grid."""
        ww, wh = glfw.get_window_size(self.window)
        self.region_w = max(1, ww)
        self.region_h = max(1, wh - HEADER)
        self.cols = max(1, self.region_w // self.cell_w)
        self.rows = max(1, self.region_h // self.cell_h)
        return self.cols, self.rows

    def cur(self):
        return self.sessions[self.active] if self.sessions else None

    # -- tabs ----------------------------------------------------------------
    def new_tab(self):
        cols, rows = self._region()
        self.sessions.append(Session(cols, rows, (self.cell_w, self.cell_h)))
        self.active = len(self.sessions) - 1

    def close_tab(self, i):
        s = self.sessions.pop(i)
        s.close()
        if not self.sessions:
            glfw.set_window_should_close(self.window, True)
            return
        self.active = min(self.active, len(self.sessions) - 1)

    def _load_ui_font(self):
        """Make the chrome use JetBrains Mono instead of imgui's built-in bitmap
        font (ProggyClean), which is what read as 'retro'. Cover Latin plus the
        General-Punctuation dashes/quotes so em-dashes render, not as '?'."""
        io = imgui.get_io()
        path = os.path.join(FONT_DIR, "JetBrainsMono-Regular.ttf")
        if not os.path.exists(path):
            return
        try:
            ranges = imgui.core.GlyphRanges([0x20, 0xFF, 0x2010, 0x2027, 0])
        except Exception:
            ranges = None
        io.fonts.clear()
        try:
            if ranges is not None:
                io.fonts.add_font_from_file_ttf(path, UI_FONT_PX, glyph_ranges=ranges)
            else:
                io.fonts.add_font_from_file_ttf(path, UI_FONT_PX)
        except Exception:
            io.fonts.add_font_default()  # never leave the atlas empty

    # -- imgui theme (dark, matching the PySide6 dialog styling) -------------
    def _apply_theme(self):
        st = imgui.get_style()
        st.window_border_size = 0.0
        st.frame_border_size = 0.0
        st.window_rounding = 6.0
        st.frame_rounding = 4.0
        st.grab_rounding = 4.0
        st.popup_rounding = 6.0
        st.tab_rounding = 7.0
        st.frame_padding = (8, 5)
        st.item_spacing = (8, 8)
        cols = st.colors

        def setc(name, rgb, a=1.0):
            idx = getattr(imgui, name, None)
            if idx is not None:
                cols[idx] = (rgb[0], rgb[1], rgb[2], a)

        setc("COLOR_TEXT", UI_TEXT_HOVER)
        setc("COLOR_TEXT_DISABLED", UI_HINT)
        setc("COLOR_WINDOW_BACKGROUND", UI_DIALOG_BG)
        setc("COLOR_POPUP_BACKGROUND", UI_DIALOG_BG)
        setc("COLOR_BORDER", _hex("#33333f"))
        setc("COLOR_FRAME_BACKGROUND", _hex("#1e1e28"))
        setc("COLOR_FRAME_BACKGROUND_HOVERED", _hex("#2a2a38"))
        setc("COLOR_FRAME_BACKGROUND_ACTIVE", _hex("#30303f"))
        setc("COLOR_BUTTON", _hex("#24242e"))
        setc("COLOR_BUTTON_HOVERED", _hex("#2c2c3a"))
        setc("COLOR_BUTTON_ACTIVE", _hex("#34343f"))
        setc("COLOR_HEADER", UI_ACCENT)
        setc("COLOR_HEADER_HOVERED", _hex("#2c2c3a"))
        setc("COLOR_HEADER_ACTIVE", UI_ACCENT)
        setc("COLOR_CHECK_MARK", UI_ACCENT)
        setc("COLOR_SLIDER_GRAB", UI_ACCENT)
        setc("COLOR_SLIDER_GRAB_ACTIVE", _hex("#6b8ce8"))
        setc("COLOR_SEPARATOR", _hex("#2a2a36"))
        setc("COLOR_TITLE_BACKGROUND", UI_STRIP)
        setc("COLOR_TITLE_BACKGROUND_ACTIVE", UI_STRIP)
        setc("COLOR_SCROLLBAR_BACKGROUND", UI_STRIP)
        setc("COLOR_MODAL_WINDOW_DIM_BACKGROUND", (0.0, 0.0, 0.0), 0.55)

    # -- input ---------------------------------------------------------------
    def _key_cb(self, window, key, scancode, action, mods):
        self.impl.keyboard_callback(window, key, scancode, action, mods)
        if imgui.get_io().want_capture_keyboard:
            return
        if action not in (glfw.PRESS, glfw.REPEAT):
            return
        self._cursor_active = time.monotonic()  # typing holds the caret solid
        ctrl = bool(mods & glfw.MOD_CONTROL)
        shift = bool(mods & glfw.MOD_SHIFT)
        s = self.cur()
        if s is None:
            return
        # app-level shortcuts
        if ctrl and key == glfw.KEY_COMMA:
            self._open_settings = True
            return
        if ctrl and shift and key == glfw.KEY_T:
            self.new_tab()
            return
        if ctrl and shift and key == glfw.KEY_W:
            self.close_tab(self.active)
            return
        if ctrl and key in (glfw.KEY_EQUAL, glfw.KEY_KP_ADD):
            self.set_font(self.font_px + 1)
            return
        if ctrl and key in (glfw.KEY_MINUS, glfw.KEY_KP_SUBTRACT):
            self.set_font(self.font_px - 1)
            return
        if ctrl and key == glfw.KEY_0:
            self.set_font(CONFIG.font_size)
            return
        if ctrl and shift and key == glfw.KEY_C:
            sel = None  # selection not implemented in the prototype
            return
        if ctrl and shift and key == glfw.KEY_V:
            text = glfw.get_clipboard_string(self.window)
            if text:
                s.send(text.decode() if isinstance(text, bytes) else text)
            return
        if key == glfw.KEY_PAGE_UP:
            s.term.scroll_up(max(1, self.rows - 1))
            return
        if key == glfw.KEY_PAGE_DOWN:
            s.term.scroll_down(max(1, self.rows - 1))
            return
        seq = key_to_bytes(s.term, key, mods)
        if seq is not None:
            s.send(seq)

    def _char_cb(self, window, codepoint):
        self.impl.char_callback(window, codepoint)
        if imgui.get_io().want_capture_keyboard:
            return
        # Ctrl/Alt combos are handled in _key_cb; ignore their char events.
        io = imgui.get_io()
        if io.key_ctrl or io.key_alt:
            return
        s = self.cur()
        if s is not None and codepoint >= 32:
            s.send(chr(codepoint))

    def _scroll_cb(self, window, xoff, yoff):
        self.impl.scroll_callback(window, xoff, yoff)
        if imgui.get_io().want_capture_mouse:
            return
        s = self.cur()
        if s is None:
            return
        if s.term.alt_screen:  # pagers: wheel -> arrows
            s.send(_arrow(s.term, "A" if yoff > 0 else "B") * WHEEL_LINES)
        elif yoff > 0:
            s.term.scroll_up(WHEEL_LINES)
        else:
            s.term.scroll_down(WHEEL_LINES)

    # -- settings ------------------------------------------------------------
    def set_font(self, px):
        px = max(MIN_FONT_PX, min(MAX_FONT_PX, int(px)))
        if px == self.font_px:
            return
        self.font_px = px
        self.atlas = shared_atlas(px)
        self.cell_w = max(1, round(self.atlas.glyph_w / SUPERSAMPLE))
        self.cell_h = max(1, round(self.atlas.glyph_h / SUPERSAMPLE))
        self.texture.release()
        self.texture = self.ctx.texture(
            (self.atlas.width, self.atlas.height), 4, self.atlas.image.tobytes()
        )
        self.texture.build_mipmaps()
        self.texture.anisotropy = self.ctx.max_anisotropy
        self._atlas_cursor = len(self.atlas.written)
        self._color_cache.clear()
        self._resize_all()

    def _resize_all(self):
        cols, rows = self._region()
        for s in self.sessions:
            s.resize(cols, rows, (self.cell_w, self.cell_h))

    # -- colour resolution (from app.py) ------------------------------------
    def _colors_for(self, cell):
        invert = self.cur().term.reverse_video
        key = (cell.fg, cell.bg, cell.reverse, cell.dim, invert)
        hit = self._color_cache.get(key)
        if hit is None:
            fg = (
                cell.fg
                if isinstance(cell.fg, tuple)
                else PALETTE.get(cell.fg, PALETTE["default"])
            )
            if cell.bg == "default":
                bg = None
            elif isinstance(cell.bg, tuple):
                bg = cell.bg
            else:
                bg = PALETTE.get(cell.bg, BG_COLOR)
            if cell.reverse:
                fg, bg = (BG_COLOR if bg is None else bg), fg
            if cell.dim:
                fg = (fg[0] * DIM_FACTOR, fg[1] * DIM_FACTOR, fg[2] * DIM_FACTOR)
            hit = (fg, bg)
            if invert:
                f, b = hit
                hit = ((BG_COLOR if b is None else b), f)
            if len(self._color_cache) > 4096:
                self._color_cache.clear()
            self._color_cache[key] = hit
        return hit

    def _grid_tables(self):
        cols, rows = self.cols, self.rows
        sx = self.cell_w / self.region_w * 2.0
        sy = self.cell_h / self.region_h * 2.0
        xs = [c * sx - 1.0 for c in range(cols + 1)]
        ys = [1.0 - (r + 1) * sy for r in range(rows + 1)]
        return xs, ys, sx, sy

    def _build(self, term):
        """Backgrounds + glyphs + cursor for one terminal, as instance data."""
        bg = RectangleBuilder()
        glyphs = RectangleBuilder()
        su0, sv0, su1, sv1 = self.atlas.solid_uv()
        xs, ys, rw, rh = self._grid_tables()
        atlas_uv, colors_for = self.atlas.cell_uv, self._colors_for
        ul_h, st_h, st_off = rh * 0.08, rh * 0.08, rh * 0.45
        width = self.cols
        grad_glyphs, grad_bbox, grad_specs = [], {}, {}

        for y, row in enumerate(term.visible_lines()):
            if y >= self.rows:
                break
            ry = ys[y]
            for x, cell in enumerate(row):
                if x >= width:
                    break
                char = cell.char
                if (
                    char == " "
                    and cell.bg == "default"
                    and not cell.reverse
                    and not cell.underline
                    and not cell.strike
                ):
                    continue
                fg, bgc = colors_for(cell)
                rx = xs[x]
                if bgc is not None:
                    bg.add(rx, ry, rw, rh, bgc, su0, sv0, su1, sv1)
                if cell.width == 0:
                    continue
                if char != " " and not cell.conceal:
                    u0, v0, u1, v1, is_color = atlas_uv(
                        char, bold=cell.bold, italic=cell.italic
                    )
                    gw = rw * 2 if cell.width == 2 else rw
                    if cell.grad is not None and not is_color:
                        gid = id(cell.grad)
                        grad_specs[gid] = cell.grad
                        x1p, y1p = rx + gw, ry + rh
                        bb = grad_bbox.get(gid)
                        if bb is None:
                            grad_bbox[gid] = [rx, ry, x1p, y1p]
                        else:
                            if rx < bb[0]:
                                bb[0] = rx
                            if ry < bb[1]:
                                bb[1] = ry
                            if x1p > bb[2]:
                                bb[2] = x1p
                            if y1p > bb[3]:
                                bb[3] = y1p
                        grad_glyphs.append((gid, rx, ry, gw, rh, u0, v0, u1, v1))
                    else:
                        glyphs.add(
                            rx, ry, gw, rh, fg, u0, v0, u1, v1, 1.0 if is_color else 0.0
                        )
                if cell.underline:
                    glyphs.add(rx, ry, rw, ul_h, fg, su0, sv0, su1, sv1)
                if cell.strike:
                    glyphs.add(rx, ry + st_off, rw, st_h, fg, su0, sv0, su1, sv1)

        self._add_cursor(glyphs, term, xs, ys, rw, rh, su0, sv0, su1, sv1)
        bg.extend(glyphs)
        self._grad_glyphs = grad_glyphs
        self._grad_bbox = grad_bbox
        self._grad_specs = grad_specs
        return bg

    def _cursor_solid(self, term):
        """Caret on/off: solid while you're active, then blink after a pause —
        unless DECSCUSR asked for a steady caret or blinking is off."""
        cur = term.cursor
        if not cur.blink:  # DECSCUSR steady shapes
            return True
        since = time.monotonic() - self._cursor_active
        if since < CURSOR_BLINK_DELAY:  # hold solid right after activity
            return True
        return int(since / (CURSOR_BLINK_PERIOD / 2.0)) % 2 == 0

    def _add_cursor(self, b, term, xs, ys, rw, rh, su0, sv0, su1, sv1):
        cur = term.cursor
        if not CONFIG.cursor or not cur.visible or term.scroll_offset != 0:
            return
        if cur.x >= self.cols or cur.y >= self.rows:
            return
        if not self._cursor_solid(term):
            return
        rx, ry = xs[cur.x], ys[cur.y]
        thick_x = CURSOR_THICK_PX / self.region_w * 2.0
        thick_y = CURSOR_THICK_PX / self.region_h * 2.0
        shape = cur.shape
        if shape == "block":
            b.add(rx, ry, rw, rh, CURSOR_COLOR, su0, sv0, su1, sv1)
        elif shape == "underline":
            b.add(rx, ry, rw, thick_y, CURSOR_COLOR, su0, sv0, su1, sv1)
        else:  # bar
            b.add(rx, ry, thick_x, rh, CURSOR_COLOR, su0, sv0, su1, sv1)

    # -- frame ---------------------------------------------------------------
    def _render_terminal(self):
        s = self.cur()
        if s is None:
            return
        fw, fh = glfw.get_framebuffer_size(self.window)
        scale = self._scale()
        header_px = int(round(HEADER * scale))
        vp_h = max(1, fh - header_px)
        self.ctx.viewport = (0, 0, fw, vp_h)

        # upload glyphs rasterized since our last frame
        regions, self._atlas_cursor = self.atlas.dirty_since(self._atlas_cursor)
        if regions:
            for x, y, w, h, rgba in regions:
                self.texture.write(rgba, viewport=(x, y, w, h))
            self.texture.build_mipmaps()

        self.texture.use(0)
        data = self._build(s.term)  # also fills self._grad_glyphs / bbox / specs
        if data.count:
            buf = data.buffer()
            need = len(buf) * 4
            if need > self.vbo.size:
                self.vbo.orphan(need)
            else:
                self.vbo.orphan()
            self.vbo.write(buf)
            self.vao.render(moderngl.TRIANGLES, vertices=6, instances=data.count)

        # YT images sit over their reserved cells; gradient text on top.
        self._render_images(s.term)
        self._render_gradients()

    def _build_grad_vertices(self):
        glyphs = self._grad_glyphs
        if not glyphs:
            return None, 0
        elapsed = time.monotonic() - self._start
        info = {}
        for gid, (minx, miny, maxx, maxy) in self._grad_bbox.items():
            grad = self._grad_specs[gid]
            rad = math.radians(grad.angle)
            ax, ay = math.cos(rad), -math.sin(rad)
            projs = (
                minx * ax + miny * ay,
                maxx * ax + miny * ay,
                maxx * ax + maxy * ay,
                minx * ax + maxy * ay,
            )
            pmin, pmax = min(projs), max(projs)
            info[gid] = (
                grad,
                ax,
                ay,
                pmin,
                (pmax - pmin) or 1.0,
                grad.cycle,
                elapsed * grad.speed if grad.cycle else 0.0,
            )
        data = []
        for gid, rx, ry, gw, rh, u0, v0, u1, v1 in glyphs:
            grad, ax, ay, pmin, span, cycle, phase = info[gid]
            color_at = grad.color_at

            def col(px, py):
                t = (px * ax + py * ay - pmin) / span
                if cycle:
                    m = (t + phase) % 2.0
                    t = 2.0 - m if m > 1.0 else m
                return color_at(0.0 if t < 0 else 1.0 if t > 1 else t)

            x1, y1 = rx + gw, ry + rh
            c0, c1, c2, c3 = col(rx, ry), col(x1, ry), col(x1, y1), col(rx, y1)
            data.extend(
                (
                    rx,
                    ry,
                    u0,
                    v0,
                    *c0,
                    x1,
                    ry,
                    u1,
                    v0,
                    *c1,
                    x1,
                    y1,
                    u1,
                    v1,
                    *c2,
                    rx,
                    ry,
                    u0,
                    v0,
                    *c0,
                    x1,
                    y1,
                    u1,
                    v1,
                    *c2,
                    rx,
                    y1,
                    u0,
                    v1,
                    *c3,
                )
            )
        return array("f", data), len(glyphs) * 6

    def _render_gradients(self):
        if not self._grad_glyphs:
            return
        verts, nverts = self._build_grad_vertices()
        if not nverts:
            return
        need = nverts * _GRAD_FLOATS_PER_VERT * 4
        if need > self.grad_vbo.size:
            self.grad_vbo.orphan(need)
        else:
            self.grad_vbo.orphan()
        self.grad_vbo.write(verts)
        self.grad_vao.render(moderngl.TRIANGLES, vertices=nverts)

    @staticmethod
    def _contain(l, r, t, b, win_w, win_h, iw, ih):
        box_w = (r - l) * win_w / 2.0
        box_h = (t - b) * win_h / 2.0
        if box_w <= 0 or box_h <= 0:
            return l, r, t, b
        img_a, box_a = iw / ih, box_w / box_h
        if img_a > box_a:
            frac = box_a / img_a
            mid, half = (t + b) / 2.0, (t - b) * frac / 2.0
            return l, r, mid + half, mid - half
        frac = img_a / box_a
        mid, half = (l + r) / 2.0, (r - l) * frac / 2.0
        return mid - half, mid + half, t, b

    def _render_images(self, term):
        live = {id(im) for im in term.images}
        for key in list(self._img_textures):
            if key not in live:
                self._img_textures.pop(key).release()
        drawable = [im for im in term.images if im.alt == term.alt_screen]
        if not drawable:
            return
        top_abs = term.first_line_no + len(term.scrollback) - term.scroll_offset
        sx = self.cell_w / self.region_w * 2.0
        sy = self.cell_h / self.region_h * 2.0
        for im in drawable:
            row_top = im.top_line - top_abs
            if row_top + im.rows <= 0 or row_top >= term.height:
                continue
            tex = self._img_textures.get(id(im))
            if tex is None:
                tex = self.ctx.texture((im.iw, im.ih), 4, im.rgba)
                tex.build_mipmaps()
                tex.anisotropy = self.ctx.max_anisotropy
                self._img_textures[id(im)] = tex
            bl = im.left * sx - 1.0
            br = (im.left + im.cols) * sx - 1.0
            bt = 1.0 - row_top * sy
            bb = 1.0 - (row_top + im.rows) * sy
            if im.fit == "contain":
                l, r, t, b = self._contain(
                    bl, br, bt, bb, self.region_w, self.region_h, im.iw, im.ih
                )
            else:
                l, r, t, b = bl, br, bt, bb
            quad = array(
                "f",
                (
                    l,
                    b,
                    0.0,
                    1.0,
                    r,
                    b,
                    1.0,
                    1.0,
                    r,
                    t,
                    1.0,
                    0.0,
                    l,
                    b,
                    0.0,
                    1.0,
                    r,
                    t,
                    1.0,
                    0.0,
                    l,
                    t,
                    0.0,
                    0.0,
                ),
            )
            tex.use(1)
            self.img_vbo.orphan()
            self.img_vbo.write(quad)
            self.img_vao.render(moderngl.TRIANGLES, vertices=6)

    # -- chrome (Windows-Terminal / VS Code style, matching app.py) ----------
    @staticmethod
    def _u32(rgb, a=1.0):
        return imgui.get_color_u32_rgba(rgb[0], rgb[1], rgb[2], a)

    @staticmethod
    def _ellipsize(text, max_w):
        if imgui.calc_text_size(text).x <= max_w:
            return text
        while text and imgui.calc_text_size(text + "…").x > max_w:
            text = text[:-1]
        return (text + "…") if text else "…"

    def _render_chrome(self):
        self.impl.process_inputs()
        imgui.new_frame()
        ww, wh = glfw.get_window_size(self.window)

        # The tab strip: a borderless window filling the top, painted UI_STRIP.
        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(ww, HEADER)
        imgui.push_style_var(imgui.STYLE_WINDOW_PADDING, (0, 0))
        imgui.push_style_var(imgui.STYLE_WINDOW_ROUNDING, 0.0)
        imgui.push_style_color(imgui.COLOR_WINDOW_BACKGROUND, *UI_STRIP)
        flags = (
            imgui.WINDOW_NO_TITLE_BAR
            | imgui.WINDOW_NO_RESIZE
            | imgui.WINDOW_NO_MOVE
            | imgui.WINDOW_NO_SCROLLBAR
            | imgui.WINDOW_NO_SAVED_SETTINGS
            | imgui.WINDOW_NO_BRING_TO_FRONT_ON_FOCUS
        )
        imgui.begin("##strip", flags=flags)
        self._draw_tabs(ww)
        imgui.end()
        imgui.pop_style_color(1)
        imgui.pop_style_var(2)

        self._main_menu()
        self._settings_modal(ww, wh)
        self._about_modal(ww, wh)

        imgui.render()
        fw, fh = glfw.get_framebuffer_size(self.window)
        self.ctx.viewport = (0, 0, fw, fh)
        self.impl.render(imgui.get_draw_data())

    def _draw_tabs(self, ww):
        dl = imgui.get_window_draw_list()
        wx, wy = imgui.get_window_position()
        top_only = imgui.DRAW_ROUND_CORNERS_TOP
        tab_top, tab_bot = wy + 5.0, wy + HEADER
        h = tab_bot - tab_top
        rounding, pad_l, pad_r, close_sz, gap = 7.0, 13.0, 8.0, 13.0, 3.0
        # App logo at the far left (the rounded shell tile), like app.py.
        logo = HEADER - 16.0
        self._draw_logo(dl, wx + 9.0, wy + (HEADER - logo) * 0.5, logo)
        right_limit = wx + ww - 20.0
        x = wx + 9.0 + logo + 9.0
        close_req = None

        for i, s in enumerate(list(self.sessions)):
            active = i == self.active
            label = s.title or "shell"
            tw = imgui.calc_text_size(label).x
            tab_w = max(150.0, min(240.0, pad_l + tw + 10 + close_sz + pad_r))
            if x + tab_w > right_limit and i > 0:
                break  # (prototype: no tab scrolling yet)
            x0, x1 = x, x + tab_w

            # hit areas first, so we can read hover/click before painting
            imgui.set_cursor_screen_pos((x0, tab_top))
            imgui.invisible_button("tab%d" % i, tab_w - close_sz - 6, h)
            if imgui.is_item_clicked():
                self.active = i
                active = True
            tab_hover = imgui.is_item_hovered()
            ccx = x1 - pad_r - close_sz * 0.5
            imgui.set_cursor_screen_pos((ccx - close_sz * 0.5 - 3, tab_top + 6))
            imgui.invisible_button("x%d" % i, close_sz + 8, h - 12)
            close_hover = imgui.is_item_hovered()
            if imgui.is_item_clicked():
                close_req = i

            if active:
                dl.add_rect_filled(
                    x0, tab_top, x1, tab_bot + 2, self._u32(UI_BG), rounding, top_only
                )
                dl.add_line(
                    x0 + rounding,
                    tab_top + 1.0,
                    x1 - rounding,
                    tab_top + 1.0,
                    self._u32(UI_ACCENT),
                    2.0,
                )
                tcol = UI_TEXT_ACTIVE
            elif tab_hover:
                dl.add_rect_filled(
                    x0,
                    tab_top,
                    x1,
                    tab_bot + 2,
                    self._u32(UI_TAB_HOVER),
                    rounding,
                    top_only,
                )
                tcol = UI_TEXT_HOVER
            else:
                tcol = UI_TEXT

            sz = imgui.calc_text_size(label)
            avail = tab_w - pad_l - close_sz - pad_r - 8
            dl.add_text(
                x0 + pad_l,
                tab_top + (h - sz.y) * 0.5,
                self._u32(tcol),
                self._ellipsize(label, avail),
            )

            cy = tab_top + h * 0.5
            if close_hover:
                dl.add_rect_filled(
                    ccx - 8, cy - 8, ccx + 8, cy + 8, self._u32(UI_CLOSE_HOVER), 4.0
                )
                xcol = self._u32(UI_TEXT_ACTIVE)
            else:
                xcol = self._u32(UI_TEXT_HOVER if active else UI_TEXT)
            r = 3.1
            dl.add_line(ccx - r, cy - r, ccx + r, cy + r, xcol, 1.4)
            dl.add_line(ccx - r, cy + r, ccx + r, cy - r, xcol, 1.4)
            x = x1 + gap

        # "+" new-tab button
        pc = self._u32(UI_TEXT_HOVER)
        pw = 28.0
        imgui.set_cursor_screen_pos((x, tab_top))
        imgui.invisible_button("newtab", pw, h)
        if imgui.is_item_clicked():
            self.new_tab()
        if imgui.is_item_hovered():
            dl.add_rect_filled(
                x, tab_top + 4, x + pw, tab_bot - 4, self._u32(UI_TAB_HOVER), 5.0
            )
        pcx, pcy = x + pw * 0.5, tab_top + h * 0.5
        dl.add_line(pcx - 5, pcy, pcx + 5, pcy, pc, 1.6)
        dl.add_line(pcx, pcy - 5, pcx, pcy + 5, pc, 1.6)
        x += pw + 2

        # "⌄" dropdown menu button
        vw = 26.0
        imgui.set_cursor_screen_pos((x, tab_top))
        imgui.invisible_button("menubtn", vw, h)
        if imgui.is_item_clicked():
            self._open_menu = True
            self._menu_x = x
        if imgui.is_item_hovered():
            dl.add_rect_filled(
                x, tab_top + 4, x + vw, tab_bot - 4, self._u32(UI_TAB_HOVER), 5.0
            )
        vcx, vcy = x + vw * 0.5, tab_top + h * 0.5 - 1
        dl.add_line(vcx - 4, vcy - 2, vcx, vcy + 2, pc, 1.6)
        dl.add_line(vcx, vcy + 2, vcx + 4, vcy - 2, pc, 1.6)

        if close_req is not None:
            self.close_tab(close_req)

    def _draw_logo(self, dl, x, y, size):
        """The YoTerm mark: a rounded blue tile with a '>_' shell prompt,
        matching app.py's make_logo (a solid tint stands in for the gradient —
        it's imperceptible at this size)."""
        r = size * 0.22
        dl.add_rect_filled(
            x,
            y,
            x + size,
            y + size,
            imgui.get_color_u32_rgba(0.36, 0.47, 0.95, 1.0),
            r,
            imgui.DRAW_ROUND_CORNERS_ALL,
        )
        white = imgui.get_color_u32_rgba(1.0, 1.0, 1.0, 1.0)
        th = max(1.5, size * 0.09)
        dl.add_line(
            x + size * 0.27,
            y + size * 0.33,
            x + size * 0.45,
            y + size * 0.51,
            white,
            th,
        )
        dl.add_line(
            x + size * 0.45,
            y + size * 0.51,
            x + size * 0.27,
            y + size * 0.69,
            white,
            th,
        )
        dl.add_line(
            x + size * 0.54,
            y + size * 0.69,
            x + size * 0.75,
            y + size * 0.69,
            white,
            th,
        )

    def _main_menu(self):
        """The ⌄ dropdown — the same items and shortcuts as app.py's menu."""
        if self._open_menu:
            imgui.open_popup("mainmenu")
            self._open_menu = False
        imgui.set_next_window_position(self._menu_x, HEADER - 2)
        imgui.push_style_var(imgui.STYLE_WINDOW_PADDING, (6, 6))
        if imgui.begin_popup("mainmenu"):
            if imgui.menu_item("New Tab", "Ctrl+Shift+T")[0]:
                self.new_tab()
            if imgui.menu_item("Close Tab", "Ctrl+Shift+W")[0]:
                self.close_tab(self.active)
            imgui.separator()
            if imgui.menu_item("Settings...", "Ctrl+,")[0]:
                self._open_settings = True
            if imgui.menu_item("Edit settings file...")[0]:
                self._edit_config()
            imgui.separator()
            if imgui.menu_item("Zoom in", "Ctrl+=")[0]:
                self.set_font(self.font_px + 1)
            if imgui.menu_item("Zoom out", "Ctrl+-")[0]:
                self.set_font(self.font_px - 1)
            if imgui.menu_item("Reset zoom", "Ctrl+0")[0]:
                self.set_font(CONFIG.font_size)
            imgui.separator()
            if imgui.menu_item("About YoTerm")[0]:
                self._open_about = True
            imgui.end_popup()
        imgui.pop_style_var(1)

    def _edit_config(self):
        path = config_module.config_path()
        try:
            if not os.path.exists(path):
                config_module.save(CONFIG, path)
            os.startfile(path)  # Windows: open in the default editor
        except Exception:
            pass

    def _about_modal(self, ww, wh):
        appearing = getattr(imgui, "APPEARING", getattr(imgui, "FIRST_USE_EVER", 0))
        if self._open_about:
            imgui.open_popup("About YoTerm")
            self._open_about = False
        imgui.set_next_window_position(ww * 0.5, wh * 0.5, appearing, 0.5, 0.5)
        imgui.push_style_var(imgui.STYLE_WINDOW_PADDING, (20, 18))
        visible, _ = imgui.begin_popup_modal(
            "About YoTerm", True, flags=imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE
        )
        if visible:
            imgui.text("YoTerm")
            imgui.text_colored("A from-scratch GPU terminal for Windows", *UI_HINT)
            imgui.spacing()
            imgui.text_colored("GLFW + Dear ImGui prototype shell", *UI_HINT)
            imgui.separator()
            if imgui.button("Close", 100):
                imgui.close_current_popup()
            imgui.end_popup()
        imgui.pop_style_var(1)

    def _settings_modal(self, ww, wh):
        appearing = getattr(imgui, "APPEARING", getattr(imgui, "FIRST_USE_EVER", 0))
        if self._open_settings:
            imgui.open_popup("Settings")
            self._open_settings = False
        imgui.set_next_window_size(380, 0)
        imgui.set_next_window_position(ww * 0.5, wh * 0.5, appearing, 0.5, 0.5)

        imgui.push_style_var(imgui.STYLE_WINDOW_PADDING, (18, 16))
        imgui.push_style_var(imgui.STYLE_FRAME_ROUNDING, 4.0)
        imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (8, 8))
        imgui.push_style_color(imgui.COLOR_POPUP_BACKGROUND, *UI_DIALOG_BG)
        imgui.push_style_color(imgui.COLOR_TITLE_BACKGROUND_ACTIVE, *UI_STRIP)

        visible, _ = imgui.begin_popup_modal(
            "Settings", True, flags=imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE
        )
        if visible:
            changed = self._settings_body()
            imgui.separator()
            imgui.push_style_color(imgui.COLOR_BUTTON, *UI_ACCENT)
            imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, *_hex("#6b8ce8"))
            imgui.push_style_color(imgui.COLOR_BUTTON_ACTIVE, *UI_ACCENT)
            if imgui.button("Save", 96):
                config_module.save(CONFIG)
            imgui.pop_style_color(3)
            imgui.same_line()
            if imgui.button("Close", 96):
                imgui.close_current_popup()
            imgui.end_popup()
            if changed:
                self._color_cache.clear()

        imgui.pop_style_color(2)
        imgui.pop_style_var(3)

    def _settings_body(self):
        """Generated from the YTConfig dataclass fields, like app.py's dialog."""
        changed = False
        for spec in fields(CONFIG):
            meta = spec.metadata
            label = meta.get("label", spec.name)
            value = getattr(CONFIG, spec.name)
            choices = meta.get("choices")
            imgui.push_id(spec.name)
            if choices:
                idx = list(choices).index(value) if value in choices else 0
                ch, idx = imgui.combo(label, idx, list(choices))
                if ch:
                    setattr(CONFIG, spec.name, list(choices)[idx])
                    self._apply_setting(spec.name)
                    changed = True
            elif isinstance(value, bool):
                ch, v = imgui.checkbox(label, value)
                if ch:
                    setattr(CONFIG, spec.name, v)
                    self._apply_setting(spec.name)
                    changed = True
            elif isinstance(value, int):
                ch, v = imgui.slider_int(
                    label, value, meta.get("min", 1), meta.get("max", 100)
                )
                if ch:
                    setattr(CONFIG, spec.name, v)
                    self._apply_setting(spec.name)
                    changed = True
            help_text = meta.get("help")
            if help_text:
                imgui.push_style_color(imgui.COLOR_TEXT, *UI_HINT)
                imgui.text_wrapped(help_text)
                imgui.pop_style_color(1)
            imgui.spacing()
            imgui.pop_id()
        return changed

    def _apply_setting(self, name):
        if name == "cursor_style":
            for s in self.sessions:
                s.term.cursor.shape = CONFIG.shape()
        elif name == "font_size":
            self.set_font(CONFIG.font_size)

    def run(self):
        prev_size = None
        while not glfw.window_should_close(self.window):
            glfw.poll_events()
            size = glfw.get_window_size(self.window)
            if size != prev_size:
                prev_size = size
                self._resize_all()

            for s in list(self.sessions):
                if s.drain():
                    self._cursor_active = time.monotonic()  # output holds it solid
                if not s.alive:
                    self.close_tab(self.sessions.index(s))

            # Re-detect the default framebuffer every frame at its CURRENT size.
            # moderngl caches the screen size from context creation and never
            # sees a window resize, so without this the whole surface clips to
            # the startup size — text vanishes the moment you maximize.
            fw, fh = glfw.get_framebuffer_size(self.window)
            self.ctx.viewport = (0, 0, fw, fh)
            self.ctx.detect_framebuffer().use()
            self.ctx.clear(*BG_COLOR)
            self.ctx.enable(moderngl.BLEND)
            self.ctx.blend_func = (
                moderngl.SRC_ALPHA,
                moderngl.ONE_MINUS_SRC_ALPHA,
                moderngl.ONE,
                moderngl.ONE_MINUS_SRC_ALPHA,
            )
            self._render_terminal()
            self._render_chrome()
            glfw.swap_buffers(self.window)

        for s in self.sessions:
            s.close()
        glfw.terminate()


def main():
    global CONFIG
    CONFIG, problem = config_module.load()
    if problem:
        sys.stderr.write("YoTerm config problem: %s\n" % problem)
    YoTermGlfw().run()


if __name__ == "__main__":
    main()
