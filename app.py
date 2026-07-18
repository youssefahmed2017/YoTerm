# YoTerm: PySide6 (Qt) application shell hosting the ModernGL terminal renderer.
#
#   Qt (window / menus / tabs / clipboard / input)
#     └── TerminalWidget (QOpenGLWidget)
#           └── ModernGL renderer  ->  Terminal model  ->  pywinpty  ->  shell
#
# The renderer core (tools.FontAtlas / shader / term.Terminal) is unchanged;
# only the app shell moved from moderngl_window to Qt.

import math
import os
import sys
import time
import queue
import threading

import moderngl
from array import array
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from winpty import PtyProcess

from dataclasses import fields

from tools import RectangleBuilder, cell_rect_px, DynamicAtlas, PALETTE
from term import Terminal
import config as config_module
from config import YTConfig, config_path

# Live settings. main() replaces this with whatever the user's config says;
# everything reads it through the module global so a settings change applies
# without rebuilding anything.
CONFIG = YTConfig()

_FONT_META = {f.name: f.metadata for f in fields(YTConfig)}["font_size"]
MIN_FONT_PX = _FONT_META.get("min", 8)
MAX_FONT_PX = _FONT_META.get("max", 72)

FONT_PX = 24  # on-screen glyph size (line-height feel)
SUPERSAMPLE = 3  # render the atlas this much larger, then downsample
GUTTER = 2  # transparent px between atlas cells (anti-bleed)

BG_COLOR = (0.06, 0.06, 0.08)  # terminal background / clear color
SELECTION_COLOR = (0.20, 0.30, 0.52)  # highlight behind selected cells
DIM_FACTOR = 0.55  # brightness multiplier for SGR 2 (dim)

# Cursor. A caret reads as "real" when it keeps a constant weight regardless of
# font size, stays solid while you're working, and only blinks once you pause.
CURSOR_COLOR = (0.90, 0.92, 0.98)  # caret color when fully on
CURSOR_THICK_PX = 2.0  # bar/underline weight in *logical* px
CURSOR_BLINK_PERIOD = 1.2  # seconds for one on->off->on cycle
CURSOR_BLINK_DELAY = 0.5  # stay solid this long after activity
CURSOR_UNFOCUSED_ALPHA = 0.40  # dimmed caret when the window is inactive

TEXT_BLINK_PERIOD = 1.0  # seconds for one on->off->on cycle of SGR 5 text

# --- Chrome ------------------------------------------------------------------
# Windows Terminal's tab styling -- rounded top corners, the selected tab
# merging into the terminal surface -- under the native window frame. Colours
# and accent are our own.
UI_BG = "#0f0f14"  # == BG_COLOR: terminal surface + selected tab
UI_STRIP = "#17171f"  # the title bar / tab strip behind the tabs
UI_TAB_HOVER = "#22222e"
UI_TEXT = "#9a9aa8"
UI_TEXT_ACTIVE = "#ffffff"
UI_ACCENT = "#5a7fe0"
UI_CLOSE_HOVER = "#c4404a"

HEADER_H = 40  # tab strip height (Windows Terminal sits near this)

STYLE_SHEET = f"""
QMainWindow, QStackedWidget {{ background: {UI_BG}; }}
QWidget#header {{ background: {UI_STRIP}; }}

QTabBar {{ background: transparent; qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: transparent;
    color: {UI_TEXT};
    border: 0;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 0px 8px 0px 12px;
    margin: 4px 1px 0px 1px;
    height: {HEADER_H - 4}px;
    min-width: 130px;
    max-width: 250px;
}}
QTabBar::tab:hover {{ background: {UI_TAB_HOVER}; color: #d6d6e2; }}
QTabBar::tab:selected {{
    background: {UI_BG};
    color: {UI_TEXT_ACTIVE};
    border-top: 2px solid {UI_ACCENT};
}}
QTabBar::scroller {{ width: 18px; }}

QToolButton#tabClose {{
    background: transparent; border: 0; border-radius: 4px;
    color: {UI_TEXT}; font-size: 11px; padding: 0;
}}
QToolButton#tabClose:hover {{ background: {UI_CLOSE_HOVER}; color: white; }}

QToolButton#strip {{
    background: transparent; border: 0; border-radius: 4px;
    color: #d6d6e2; font-size: 14px;
    padding: 0; margin: 5px 2px 3px 2px;
    min-width: 32px; min-height: {HEADER_H - 8}px;
}}
QToolButton#strip:hover {{ background: {UI_TAB_HOVER}; color: {UI_TEXT_ACTIVE}; }}
QToolButton#strip::menu-indicator {{ image: none; width: 0; }}

QMenu {{
    background: #1c1c26; color: #e6e6ee;
    border: 1px solid #33333f; padding: 4px;
}}
QMenu::item {{ padding: 5px 24px 5px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {UI_ACCENT}; color: white; }}
QMenu::separator {{ height: 1px; background: #33333f; margin: 4px 8px; }}

QToolTip {{
    background: #1e1e26; color: #e6e6ee;
    border: 1px solid #33333f; padding: 4px;
}}

QDialog {{ background: #14141b; }}
QDialog QLabel {{ color: #d6d6e2; }}
QLabel#hint {{ color: #7a7a88; }}
QCheckBox {{ color: #d6d6e2; spacing: 6px; }}
QComboBox, QSpinBox, QLineEdit {{
    background: #1e1e28; color: #e6e6ee;
    border: 1px solid #33333f; border-radius: 4px;
    padding: 4px 6px; min-height: 20px;
}}
QComboBox:hover, QSpinBox:hover, QLineEdit:hover {{ border-color: #454557; }}
/* Only widen the arrow area. Restyling drop-down wholesale drops the arrow
   Qt draws for us, and a combo with no arrow reads as a text field. */
QComboBox::drop-down {{ width: 20px; border: 0; }}
QComboBox QAbstractItemView {{
    background: #1e1e28; color: #e6e6ee;
    border: 1px solid #33333f;
    selection-background-color: {UI_ACCENT}; selection-color: white;
}}
QPushButton {{
    background: #24242e; color: #e6e6ee;
    border: 1px solid #33333f; border-radius: 4px;
    padding: 5px 14px; min-width: 72px;
}}
QPushButton:hover {{ background: #2c2c3a; }}
QPushButton:default {{
    background: {UI_ACCENT}; border-color: {UI_ACCENT}; color: white;
}}
QPushButton:default:hover {{ background: #6b8ce8; }}
"""


def make_logo(size=64):
    """The YoTerm mark: a rounded tile with a shell prompt drawn on it.

    Generated at runtime, so there's no binary asset to ship or keep in sync,
    and it stays crisp at whatever size the OS asks for.
    """
    pm = QtGui.QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)

    grad = QtGui.QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QtGui.QColor("#6c8bff"))
    grad.setColorAt(1.0, QtGui.QColor("#3b5bcc"))
    p.setBrush(QtGui.QBrush(grad))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QtCore.QRectF(0, 0, size, size), size * 0.22, size * 0.22)

    pen = QtGui.QPen(QtGui.QColor("#ffffff"))
    pen.setWidthF(max(1.4, size * 0.08))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawPolyline(
        QtGui.QPolygonF(
            [  # the '>' chevron
                QtCore.QPointF(size * 0.27, size * 0.33),
                QtCore.QPointF(size * 0.45, size * 0.51),
                QtCore.QPointF(size * 0.27, size * 0.69),
            ]
        )
    )
    p.drawLine(
        QtCore.QPointF(size * 0.54, size * 0.69),  # the '_' cursor
        QtCore.QPointF(size * 0.75, size * 0.69),
    )
    p.end()
    return pm


def app_icon():
    icon = QtGui.QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(make_logo(s))
    return icon


_ARROW_PATH = None


def combo_arrow_qss():
    """Style rule giving combo boxes their dropdown arrow back.

    Styling QComboBox at all makes Qt stop drawing its own arrow, and a combo
    with no arrow reads as a plain text field. QSS can only take an image from
    a URL, so paint one once and point at it. Built lazily: a QPixmap before
    QApplication exists is invalid.
    """
    global _ARROW_PATH
    if _ARROW_PATH is None:
        import tempfile
        pm = QtGui.QPixmap(16, 16)
        pm.fill(Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor(UI_TEXT))
        pen.setWidthF(1.5)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QtGui.QPolygonF([
            QtCore.QPointF(4.5, 6.5),
            QtCore.QPointF(8.0, 10.0),
            QtCore.QPointF(11.5, 6.5),
        ]))
        p.end()
        path = os.path.join(tempfile.gettempdir(), "yoterm_combo_arrow.png")
        pm.save(path, "PNG")
        _ARROW_PATH = path.replace("\\", "/")   # QSS urls want forward slashes
    return ("QComboBox::down-arrow { image: url(%s); width: 16px; height: 16px; }"
            % _ARROW_PATH)


def enable_dark_titlebar(widget):
    """Ask Windows to paint the native title bar dark, so it doesn't sit as a
    bright strip above a dark terminal. Cosmetic: a no-op anywhere else."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(widget.winId())),
            ctypes.c_int(DWMWA_USE_IMMERSIVE_DARK_MODE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        pass


def registry_path():
    """The PATH Windows *currently* has on record, read fresh from the registry.

    A process inherits PATH from its parent at launch and never sees later
    changes. So a terminal started from a long-running parent (an IDE, say)
    hands the shell whatever PATH that parent had at *its* start — install a
    tool with winget and even a brand new tab still can't find it, because the
    staleness is in the parent, not in us. Windows keeps the real value here.
    """
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
            value = os.path.expandvars(value)  # %SystemRoot% and friends
        entries.extend(p for p in value.split(os.pathsep) if p)
    return entries


def shell_env():
    """The environment to hand a new shell: ours, with any PATH entries the
    registry knows about but we didn't inherit appended.

    Appended, not prepended: entries we inherited must keep priority, or a
    venv's Scripts directory would lose to the system copy and activating a
    virtualenv before launching YoTerm would silently stop working.
    """
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


_SHARED_ATLAS = None
_SHARED_ATLAS_PX = None


def shared_atlas(font_px):
    """The one glyph atlas, shared by every tab.

    It's ~45 MB of PIL image plus several parsed fallback fonts, so building
    one per tab would be wasteful and slow — and caching one per zoom level
    would be worse, so a size change *replaces* it rather than keeping both.
    That's also why zoom applies to every tab at once.

    Slots are write-once, so tabs can share it as long as each GL context
    tracks its own upload cursor (see DynamicAtlas.dirty_since).
    """
    global _SHARED_ATLAS, _SHARED_ATLAS_PX
    if _SHARED_ATLAS is None or _SHARED_ATLAS_PX != font_px:
        _SHARED_ATLAS = DynamicAtlas(px=font_px * SUPERSAMPLE, pad=GUTTER * SUPERSAMPLE)
        _SHARED_ATLAS_PX = font_px
    return _SHARED_ATLAS


def _lerp(a, b, t):
    """Blend two RGB tuples."""
    return (
        a[0] + (b[0] - a[0]) * t,
        a[1] + (b[1] - a[1]) * t,
        a[2] + (b[2] - a[2]) * t,
    )


VERTEX_SHADER = """
#version 330
// One instance per quad: `in_corner` walks the shared unit quad, everything
// else is per-instance. The GPU does the expansion so Python doesn't have to
// write out six vertices for every cell.
in vec2 in_corner;      // per-vertex: (0,0)..(1,1)
in vec2 in_pos;         // per-instance: bottom-left in NDC
in vec2 in_size;        // per-instance: width/height in NDC
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
    if (v_mode > 0.5) {
        color = texel;                   // color glyph (emoji): own RGBA
    } else {
        color = vec4(v_color, texel.a);  // alpha-keyed glyph tinted by cell
    }
}
"""

# A second, non-instanced program just for YoTerm gradient text. Each glyph is
# a real four-corner quad whose corners carry their OWN colour, sampled from
# the gradient ramp at each corner's position along the run. The rasteriser
# then interpolates across the two triangles, so the colour changes *within*
# each character — genuinely sub-cell smooth, not the per-cell stepping an
# ordinary terminal is stuck with. Gradient text is rare, so paying full
# per-vertex geometry for it (instead of the fast instanced path) costs nothing
# that matters.
GRAD_VERTEX_SHADER = """
#version 330
in vec2 in_vpos;   // NDC position of this corner
in vec2 in_vuv;    // atlas UV for this corner
in vec3 in_vcol;   // gradient colour sampled at this corner
out vec2 v_uv;
out vec3 v_col;
void main() {
    gl_Position = vec4(in_vpos, 0, 1);
    v_uv = in_vuv;
    v_col = in_vcol;
}
"""

GRAD_FRAGMENT_SHADER = """
#version 330
uniform sampler2D tex;
in vec2 v_uv;
in vec3 v_col;
out vec4 color;
void main() {
    // Gradient text is alpha-keyed like ordinary glyphs: the atlas alpha is the
    // coverage, the per-corner colour is the ink.
    color = vec4(v_col, texture(tex, v_uv).a);
}
"""

# 7 floats per vertex (pos.xy, uv.xy, col.rgb), 6 vertices per glyph.
_GRAD_FLOATS_PER_VERT = 7


# A third program for YoTerm images (YT;img): a plain textured quad sampled
# smoothly from the image's own texture — no half-block / sextant fakery. Each
# image carries its own texture, so they're drawn one quad at a time.
IMAGE_VERTEX_SHADER = """
#version 330
in vec2 in_pos;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    gl_Position = vec4(in_pos, 0, 1);
    v_uv = in_uv;
}
"""

IMAGE_FRAGMENT_SHADER = """
#version 330
uniform sampler2D img;
in vec2 v_uv;
out vec4 color;
void main() {
    color = texture(img, v_uv);   // straight RGBA, blended over the cells
}
"""

# Zones (YT;zone): styled rectangles, one instance each, expanded from the same
# shared unit quad as the glyph pass. Hundreds of zones must cost one draw call,
# not hundreds -- an app redrawing a screen of cards every frame depends on it.
#
# Phase 2: a signed-distance-field rounded rect, computed per fragment, so the
# CPU never rasterises a corner -- it uploads a rectangle, a radius, and some
# colours, same as Phase 1. Gradient fill reads a small shared ramp texture
# (one row per zone-with-a-gradient) rather than packing colour stops into
# vertex attributes: that scales to any number of stops without bumping into
# the ~16 vertex-attribute limit, and it's the same "texture, not per-pixel
# CPU work" spirit as the rest of the renderer. See docs/zones.md.
ZONE_RAMP_W = 128   # texels per gradient ramp (resolution along the ramp)
ZONE_RAMP_H = 64    # rows: max zones with a *distinct* gradient drawn per frame

ZONE_VERTEX_SHADER = """
#version 330
in vec2 in_corner;        // per-vertex: the shared unit quad, (0,0)..(1,1)
in vec2 in_pos;           // per-instance: bottom-left in NDC
in vec2 in_size;          // per-instance: width/height in NDC
in vec4 in_color;         // fill colour (rgb; a already carries opacity)
in float in_radius;       // corner radius, logical px
in vec4 in_border_color;
in float in_border_width; // logical px; 0 = no border
in float in_angle;        // gradient angle, degrees (0 = left->right)
in float in_ramp_row;     // row in the ramp texture, or < 0 for a solid fill

uniform vec2 u_win;       // widget size, logical px -- converts NDC to px

out vec2 v_local;         // fragment position relative to the zone centre, px
out vec2 v_half;          // zone half-size, px
flat out vec4 v_color;
flat out float v_radius;
flat out vec4 v_border_color;
flat out float v_border_width;
flat out float v_angle;
flat out float v_ramp_row;

void main() {
    gl_Position = vec4(in_pos + in_corner * in_size, 0, 1);
    // NDC spans [-1, 1] == u_win pixels, so 1 NDC unit == u_win/2 px.
    v_half = in_size * u_win * 0.25;
    v_local = (in_corner - vec2(0.5)) * in_size * u_win * 0.5;
    v_color = in_color;
    v_radius = in_radius;
    v_border_color = in_border_color;
    v_border_width = in_border_width;
    v_angle = in_angle;
    v_ramp_row = in_ramp_row;
}
"""

ZONE_FRAGMENT_SHADER = """
#version 330
uniform sampler2D ramp_tex;

in vec2 v_local;
in vec2 v_half;
flat in vec4 v_color;
flat in float v_radius;
flat in vec4 v_border_color;
flat in float v_border_width;
flat in float v_angle;
flat in float v_ramp_row;
out vec4 color;

// Inigo Quilez's rounded-box SDF: negative inside, positive outside, in the
// same units as p/b (here, logical px relative to the box centre).
float sdRoundBox(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}

void main() {
    float r = min(v_radius, min(v_half.x, v_half.y));
    float d = sdRoundBox(v_local, v_half, r);
    // ~1px antialiased edge; independent of zoom since everything is in px.
    float shape_alpha = 1.0 - smoothstep(-1.0, 1.0, d);
    if (shape_alpha <= 0.0) discard;

    vec4 fill = v_color;
    if (v_ramp_row >= 0.0) {
        vec2 ax = vec2(cos(radians(v_angle)), -sin(radians(v_angle)));
        vec2 c0 = vec2(-v_half.x, -v_half.y), c1 = vec2(v_half.x, -v_half.y);
        vec2 c2 = vec2(v_half.x, v_half.y), c3 = vec2(-v_half.x, v_half.y);
        float p0 = dot(c0, ax), p1 = dot(c1, ax);
        float p2 = dot(c2, ax), p3 = dot(c3, ax);
        float pmin = min(min(p0, p1), min(p2, p3));
        float pmax = max(max(p0, p1), max(p2, p3));
        float t = (dot(v_local, ax) - pmin) / max(pmax - pmin, 0.0001);
        fill = texture(ramp_tex, vec2(clamp(t, 0.0, 1.0), (v_ramp_row + 0.5) / %(ramp_h)d.0));
    }

    float fill_mask = v_border_width > 0.0
        ? 1.0 - smoothstep(-1.0, 1.0, d + v_border_width)
        : 1.0;
    vec3 rgb = mix(v_border_color.rgb, fill.rgb, fill_mask);
    float a = mix(v_border_color.a, fill.a, fill_mask) * shape_alpha;
    color = vec4(rgb, a);
}
""" % {"ramp_h": ZONE_RAMP_H}

# 16 floats per zone instance: pos.xy, size.xy, color.rgba, radius, border.rgba,
# border_width, angle, ramp_row.
_ZONE_FLOATS = 16


class TerminalWidget(QOpenGLWidget):
    """A GL-rendered terminal surface driving a live shell over a PTY."""

    titleChanged = QtCore.Signal(str)  # OSC 0/2 set the window/tab title
    exited = QtCore.Signal()  # the shell died; the tab should close

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)

        # Atlas can be built before we have a GL context (it's just a PIL image).
        self.font_px = CONFIG.font_size
        self.atlas = shared_atlas(self.font_px)
        self.cell_w = max(1, round(self.atlas.glyph_w / SUPERSAMPLE))
        self.cell_h = max(1, round(self.atlas.glyph_h / SUPERSAMPLE))

        self.ctx = None
        self.texture = None
        self.term = None
        self.pty = None
        self.win_w = self.win_h = 1
        self._start = time.monotonic()
        # Cursor liveness: the caret holds solid while it's moving or you're
        # typing, and only starts blinking after a pause.
        self._cursor_pos = None
        self._cursor_active = self._start
        self._shell_exited = False
        self._last_title = None  # last title pushed to the tab
        self._last_bell = 0
        self._last_reverse = False
        self._has_blink = False   # is any SGR-5 text actually on screen?
        self._last_blink_on = True
        # YoTerm gradient text (ESC ] YT ; gradient). Geometry is collected when
        # the screen is rebuilt; the per-corner colours are recomputed every
        # frame so a `cycle:on` gradient can animate without a full rebuild.
        self._grad_glyphs = []    # (grad_id, rx, ry, gw, rh, u0, v0, u1, v1)
        self._grad_bbox = {}      # grad_id -> [minx, miny, maxx, maxy] in NDC
        self._grad_specs = {}     # grad_id -> GradientRun
        self._has_cycle = False   # is any animated gradient on screen?
        # YoTerm images (ESC ] YT ; img). One GPU texture per placement, cached
        # by the placement's identity and released when it's gone.
        self._img_textures = {}   # id(placement) -> moderngl.Texture
        self._atlas_cursor = 0  # our position in the shared atlas's log
        self.out_queue = queue.Queue()

        # Damage tracking. Rebuilding the whole screen's geometry costs more
        # than a frame budget on a big window, so only do it when something
        # actually changed rather than 60 times a second forever.
        self._dirty = True
        self._last_cursor_state = None
        self._color_cache = {}  # (fg, bg, reverse, dim) -> (fg_rgb, bg_rgb)
        self._static_data = None  # cached screen geometry (everything but the caret)
        self._static_quads = 0

        # Text selection, stored as absolute (buffer_line, col) so it stays
        # pinned to content while scrolling. None = no selection.
        self.sel_anchor = None
        self.sel_focus = None
        self._selecting = False
        self._last_move_cell = None  # last cell a motion event was reported for

    # ------------------------------------------------------------ GL lifecycle

    def initializeGL(self):
        # Adopt the GL context Qt created for this widget.
        self.ctx = moderngl.create_context()

        self.program = self.ctx.program(
            vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER
        )
        self.program["tex"] = 0

        self.texture = self.ctx.texture(
            (self.atlas.width, self.atlas.height), 4, self.atlas.image.tobytes()
        )
        self.texture.build_mipmaps()
        self.texture.anisotropy = self.ctx.max_anisotropy
        # We just uploaded the atlas as it stands (a later tab inherits glyphs
        # earlier tabs rasterized), so start from the end of its write log.
        self._atlas_cursor = len(self.atlas.written)

        # The unit quad every instance expands from, uploaded once.
        self.quad_vbo = self.ctx.buffer(RectangleBuilder.CORNERS)
        # Persistent instance buffer, re-filled each frame (no per-frame alloc).
        self.vbo = self.ctx.buffer(reserve=4_000_000)
        self.vao = self.ctx.vertex_array(
            self.program,
            [
                (self.quad_vbo, "2f", "in_corner"),
                # '/i' = advance once per instance rather than per vertex.
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

        # Gradient text: its own program + a dynamic per-vertex buffer.
        self.grad_program = self.ctx.program(
            vertex_shader=GRAD_VERTEX_SHADER,
            fragment_shader=GRAD_FRAGMENT_SHADER,
        )
        self.grad_program["tex"] = 0
        self.grad_vbo = self.ctx.buffer(reserve=64_000)
        self.grad_vao = self.ctx.vertex_array(
            self.grad_program,
            [(self.grad_vbo, "2f 2f 3f", "in_vpos", "in_vuv", "in_vcol")],
        )

        # Images: their own program + a one-quad buffer (drawn per image, since
        # each has its own texture). Sampled from texture unit 1 so it never
        # disturbs the glyph atlas bound on unit 0.
        self.img_program = self.ctx.program(
            vertex_shader=IMAGE_VERTEX_SHADER,
            fragment_shader=IMAGE_FRAGMENT_SHADER,
        )
        self.img_program["img"] = 1
        self.img_vbo = self.ctx.buffer(reserve=6 * 4 * 4)  # 6 verts * 4f
        self.img_vao = self.ctx.vertex_array(
            self.img_program,
            [(self.img_vbo, "2f 2f", "in_pos", "in_uv")],
        )

        # Zones: instanced, sharing the unit quad with the glyph pass.
        self.zone_program = self.ctx.program(
            vertex_shader=ZONE_VERTEX_SHADER,
            fragment_shader=ZONE_FRAGMENT_SHADER,
        )
        self.zone_program["ramp_tex"] = 2   # unit 0 = glyph atlas, 1 = images
        self.zone_vbo = self.ctx.buffer(reserve=64_000)
        self.zone_vao = self.ctx.vertex_array(
            self.zone_program,
            [(self.quad_vbo, "2f", "in_corner"),
             (self.zone_vbo, "2f 2f 4f 1f 4f 1f 1f 1f/i",
              "in_pos", "in_size", "in_color", "in_radius",
              "in_border_color", "in_border_width", "in_angle", "in_ramp_row")],
        )
        # Shared gradient-ramp atlas: one row per zone-with-a-gradient drawn
        # this frame, rebuilt only when at least one is on screen.
        self.zone_ramp = self.ctx.texture(
            (ZONE_RAMP_W, ZONE_RAMP_H), 4,
            b"\x00" * (ZONE_RAMP_W * ZONE_RAMP_H * 4))
        self.zone_ramp.filter = (moderngl.LINEAR, moderngl.LINEAR)

        # Terminal grid + shell. Grid is sized in *logical* pixels so text
        # keeps a consistent visual size across DPIs.
        self.win_w, self.win_h = self.width(), self.height()
        cols = max(1, self.win_w // self.cell_w)
        rows = max(1, self.win_h // self.cell_h)
        self.term = Terminal(cols, rows)
        self.term.cursor.shape = CONFIG.shape()   # apps can still override it
        self.term.cell_px = (self.cell_w, self.cell_h)  # image sizing needs it

        try:
            self.pty = PtyProcess.spawn([CONFIG.shell], dimensions=(rows, cols),
                                        env=shell_env())
        except Exception as exc:
            # A configured shell that isn't installed shouldn't take the tab
            # down with a traceback — say so on the screen instead.
            self.pty = None
            self.term.write(
                "\x1b[31mYoTerm: couldn't start %s\x1b[0m\r\n  %s\r\n\r\n"
                "Pick another shell in Settings (Ctrl+,), then open a new tab.\r\n"
                % (CONFIG.shell, exc), end="")

        if self.pty is not None:
            threading.Thread(target=self._read_loop, daemon=True).start()

        # Drive redraws (also advances the blinking cursor).
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)  # ~60 fps

    def resizeGL(self, w, h):
        self._resync_grid()
        self._invalidate()

    def _resync_grid(self):
        """Keep the terminal grid + PTY matched to the widget's logical size."""
        if self.term is None:
            return
        cols = max(1, self.width() // self.cell_w)
        rows = max(1, self.height() // self.cell_h)
        if cols != self.term.width or rows != self.term.height:
            self.term.resize(cols, rows)
            self._clear_selection()  # abs line coords change after reflow
            try:
                if self.pty is not None:
                    self.pty.setwinsize(rows, cols)  # let the shell reflow
            except (EOFError, OSError):
                pass

    def paintGL(self):
        self._resync_grid()

        # cell_rect_px works in logical px (ratios); the GL viewport must be in
        # physical px so we fill the whole (HiDPI) framebuffer.
        self.win_w, self.win_h = self.width(), self.height()
        dpr = self.devicePixelRatioF()
        pw = max(1, int(round(self.width() * dpr)))
        ph = max(1, int(round(self.height() * dpr)))

        # QOpenGLWidget renders into its OWN framebuffer, not FBO 0.
        self.ctx.detect_framebuffer(self.defaultFramebufferObject()).use()
        self.ctx.viewport = (0, 0, pw, ph)

        # DECSCNM inverts the screen, so the bare background flips too — a cell
        # with no background of its own must land on the inverted surface.
        base = PALETTE["default"] if self.term.reverse_video else BG_COLOR
        self.ctx.clear(*base, 1.0)
        self.ctx.enable(moderngl.BLEND)
        # RGB blends normally; alpha is kept at 1 so the framebuffer stays fully
        # opaque and QOpenGLWidget doesn't composite it over the white widget.
        self.ctx.blend_func = (
            moderngl.SRC_ALPHA,
            moderngl.ONE_MINUS_SRC_ALPHA,
            moderngl.ONE,
            moderngl.ONE_MINUS_SRC_ALPHA,
        )
        self.texture.use()

        # Backgrounds behind selection behind glyphs behind the cursor: the
        # draw order is just the order quads appear in the buffer.
        #
        # The screen's geometry is cached and rebuilt only when it changes; the
        # caret is one quad appended after it. So a blinking caret costs a
        # 48-byte write rather than rebuilding thousands of cells 30x a second.
        rewrite = self._dirty or self._static_data is None
        if rewrite:
            body = RectangleBuilder()
            glyphs = RectangleBuilder()
            self._add_cells(body, glyphs)  # may rasterize new glyphs on demand
            self._add_selection(body)
            body.extend(glyphs)
            self._static_data = body.buffer()
            self._static_quads = body.count
            self._dirty = False

        cursor = RectangleBuilder()
        self._add_cursor(cursor)
        self._last_cursor_state = self._cursor_state()

        # Upload glyphs this context hasn't seen yet, then refresh mipmaps once.
        # The cursor is per-widget: another tab may have rasterized them into
        # the shared atlas, and each GL context needs its own copy uploaded.
        regions, self._atlas_cursor = self.atlas.dirty_since(self._atlas_cursor)
        if regions:
            for x, y, w, h, rgba in regions:
                self.texture.write(rgba, viewport=(x, y, w, h))
            self.texture.build_mipmaps()

        # Zones at z<=0 sit behind the text layer (a button's background), so
        # they draw before the glyph batch. z>=1 goes on top, further down.
        self._render_zones(above=False)

        total = self._static_quads + cursor.count
        if total:
            stride = RectangleBuilder.FLOATS_PER_QUAD * 4   # bytes per quad
            needed = total * stride
            if needed > self.vbo.size:
                self.vbo.orphan(needed)   # grow; the old contents go with it
                rewrite = True
            elif rewrite:
                self.vbo.orphan()      # discard first; avoids stalling on the GPU
            if rewrite:
                self.vbo.write(self._static_data)
            if cursor.count:
                self.vbo.write(cursor.buffer(),
                               offset=self._static_quads * stride)
            self.vao.render(moderngl.TRIANGLES, vertices=6, instances=total)

        # Images sit over their (blank, reserved) cells; gradient text draws on
        # top of the instanced pass. Both are their own programs.
        self._render_images()
        self._render_gradients()
        self._render_zones(above=True)   # overlays: modals, tooltips, menus

    # ------------------------------------------------------------ PTY I/O

    def _read_loop(self):
        try:
            while True:
                data = self.pty.read(4096)
                if data:
                    self.out_queue.put(data)
        except EOFError:
            pass
        finally:
            self.out_queue.put(None)

    def _tick(self):
        if self.term is not None:
            # Keep the model's pixels-per-cell current: images placed this tick
            # size themselves against it, and the font can change mid-session.
            self.term.cell_px = (self.cell_w, self.cell_h)
        while True:
            try:
                data = self.out_queue.get_nowait()
            except queue.Empty:
                break
            if data is None:
                self._shell_exited = True
                break
            self.term.write(data, end="")
            self._dirty = True

        if self._shell_exited:
            self._timer.stop()
            self.exited.emit()  # the window closes the tab, not itself
            return

        # Send any replies the terminal owes the shell (e.g. DSR reports).
        if self.term and self.term.responses:
            for reply in self.term.responses:
                self._write_pty(reply)
            self.term.responses.clear()

        # BEL. The terminal just counts them; ringing it is the app's call.
        if self.term.bell_count != self._last_bell:
            self._last_bell = self.term.bell_count
            QtWidgets.QApplication.beep()

        # DECSCNM inverts every cell, and the colour cache is keyed on it, so
        # a flip has to invalidate the cached geometry.
        if self.term.reverse_video != self._last_reverse:
            self._last_reverse = self.term.reverse_video
            self._invalidate()

        # Blinking text lives in the cached geometry, so driving it means
        # rebuilding — but only while there's blinking text to drive.
        if self._has_blink and CONFIG.text_blink:
            phase = self._text_blink_on()
            if phase != self._last_blink_on:
                self._last_blink_on = phase
                self._invalidate()

        # OSC 0/2 named this session: push it to the tab.
        if self.term.title != self._last_title:
            self._last_title = self.term.title
            self.titleChanged.emit(self.term.title)

        # Any-motion tracking (?1003, used for hover) needs bare move events,
        # which Qt only delivers with mouse tracking enabled.
        want_tracking = self.term.mouse_mode == 1003
        if want_tracking != self.hasMouseTracking():
            self.setMouseTracking(want_tracking)

        # Only repaint when something actually changed: the screen, the caret,
        # or an animated gradient. Note update(), *not* _invalidate(): a cycling
        # gradient only needs new per-corner colours (recomputed in paintGL),
        # not a full geometry rebuild, so marking the screen dirty here would
        # throw away the cached geometry every single frame for nothing.
        if (self._dirty or self._has_cycle
                or self._cursor_state() != self._last_cursor_state):
            self.update()

    def _write_pty(self, data):
        """Write raw bytes to the shell with no local side effects."""
        try:
            if self.pty and self.pty.isalive():
                self.pty.write(data)
        except (EOFError, OSError):
            pass

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._cursor_active = time.monotonic()  # start solid, then blink
        self._invalidate()  # focus changes the caret

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._invalidate()

    def _send(self, data):
        """Send user input: jump to the live view, drop the selection, write."""
        self._invalidate()  # scroll-to-bottom and the dropped selection redraw
        # Typing holds the caret solid even when the shell doesn't echo it
        # back (password prompts), where cursor movement alone wouldn't.
        self._cursor_active = time.monotonic()
        if self.term:
            self.term.scroll_to_bottom()
        self._clear_selection()  # typing deselects
        self._write_pty(data)

    def shutdown(self):
        try:
            if self.pty and self.pty.isalive():
                self.pty.terminate(force=True)
        except (EOFError, OSError):
            pass

    # ------------------------------------------------------------ Drawing

    def _rect(self, x, y):
        return cell_rect_px(x, y, self.cell_w, self.cell_h, self.win_w, self.win_h)

    @staticmethod
    def _resolve_colors(cell):
        """Return (fg_rgb, bg_rgb) for a cell, applying reverse + dim.
        fg/bg may be a color name (PALETTE) or an (r,g,b) tuple (256/truecolor).
        bg_rgb is None when the cell uses the default (transparent) background."""
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
            # Swap, resolving the default sides to their concrete colors.
            fg, bg = (BG_COLOR if bg is None else bg), fg
        if cell.dim:
            fg = (fg[0] * DIM_FACTOR, fg[1] * DIM_FACTOR, fg[2] * DIM_FACTOR)
        return fg, bg

    def _colors_for(self, cell):
        """_resolve_colors, memoized. It was the single hottest thing in the
        frame (called once per cell per pass); a screen only ever uses a
        handful of distinct colour combinations, so cache on those.

        DECSCNM is part of the key, not applied after: it inverts every cell,
        so a cached entry from before the flip would be wrong."""
        invert = self.term.reverse_video
        key = (cell.fg, cell.bg, cell.reverse, cell.dim, invert)
        hit = self._color_cache.get(key)
        if hit is None:
            hit = self._resolve_colors(cell)
            if invert:   # DECSCNM (?5): the whole screen reads inverted
                fg, bg = hit
                hit = ((BG_COLOR if bg is None else bg), fg)
            if len(self._color_cache) > 4096:
                self._color_cache.clear()  # truecolor content could grow it
            self._color_cache[key] = hit
        return hit

    def _grid_tables(self):
        """NDC edges per column and per row.

        cell_rect_px() recomputed these for every cell of every pass, but a
        cell's x only depends on its column and its y only on its row."""
        cols, rows = self.term.width, self.term.height
        sx, sy = self.cell_w / self.win_w * 2.0, self.cell_h / self.win_h * 2.0
        xs = [c * sx - 1.0 for c in range(cols + 1)]
        ys = [1.0 - (r + 1) * sy for r in range(rows + 1)]
        return xs, ys, sx, sy

    def _add_cells(self, bg_builder, glyph_builder):
        """One pass over the grid emitting both background and glyph quads.

        Backgrounds must all sit behind all glyphs, so they go to separate
        builders that the caller concatenates in order — that keeps the draw
        order while resolving each cell's colours and geometry only once.
        """
        su0, sv0, su1, sv1 = self.atlas.solid_uv()
        xs, ys, rw, rh = self._grid_tables()
        width = self.term.width
        colors_for, atlas_uv = self._colors_for, self.atlas.cell_uv
        add_bg, add_glyph = bg_builder.add, glyph_builder.add
        ul_h, st_h, st_off = rh * 0.08, rh * 0.08, rh * 0.45

        # SGR 5 only animates if the user asked for it: blink is ancient and
        # most terminals render it steadily. Off, it also costs nothing — no
        # blinking cells means no repaints to drive them.
        blink_enabled = CONFIG.text_blink
        blink_on = (not blink_enabled) or self._text_blink_on()
        has_blink = False

        # Gradient glyphs are pulled out of the fast instanced path and drawn
        # per-vertex instead. Collect their geometry and each run's bounding box
        # here; the colours are filled in per frame in _build_grad_vertices.
        grad_glyphs, grad_bbox, grad_specs = [], {}, {}
        has_cycle = False

        for y, row in enumerate(self.term.visible_lines()):
            ry = ys[y]
            for x, cell in enumerate(row):
                if x >= width:
                    break
                char = cell.char
                # Fast path: a plain empty cell contributes nothing at all.
                if (
                    char == " "
                    and cell.bg == "default"
                    and not cell.reverse
                    and not cell.underline
                    and not cell.strike
                ):
                    continue

                fg, bg = colors_for(cell)
                rx = xs[x]
                if bg is not None:
                    add_bg(rx, ry, rw, rh, bg, su0, sv0, su1, sv1)
                if cell.width == 0:
                    continue  # trailing half of a wide glyph: no glyph here

                if cell.blink and blink_enabled:
                    has_blink = True

                if char != " " and not cell.conceal and (blink_on or not cell.blink):
                    u0, v0, u1, v1, is_color = atlas_uv(
                        char, bold=cell.bold, italic=cell.italic
                    )
                    # Wide glyph spans two cells; color emoji use their own RGBA.
                    gw = rw * 2 if cell.width == 2 else rw
                    if cell.grad is not None and not is_color:
                        # Route to the gradient batch and grow this run's bbox.
                        gid = id(cell.grad)
                        grad_specs[gid] = cell.grad
                        x1, y1 = rx + gw, ry + rh
                        bb = grad_bbox.get(gid)
                        if bb is None:
                            grad_bbox[gid] = [rx, ry, x1, y1]
                        else:
                            if rx < bb[0]: bb[0] = rx
                            if ry < bb[1]: bb[1] = ry
                            if x1 > bb[2]: bb[2] = x1
                            if y1 > bb[3]: bb[3] = y1
                        grad_glyphs.append((gid, rx, ry, gw, rh, u0, v0, u1, v1))
                        if cell.grad.cycle:
                            has_cycle = True
                    else:
                        add_glyph(rx, ry, gw, rh, fg, u0, v0, u1, v1,
                                  1.0 if is_color else 0.0)

                # Underline / strikethrough are thin solid bars (they apply to
                # spaces too, e.g. an underlined blank), and blink with the
                # cell they belong to.
                if blink_on or not cell.blink:
                    if cell.underline:
                        add_glyph(rx, ry, rw, ul_h, fg, su0, sv0, su1, sv1)
                    if cell.strike:
                        add_glyph(rx, ry + st_off, rw, st_h, fg, su0, sv0, su1, sv1)

        self._has_blink = has_blink   # drives repaints only while it's True
        self._grad_glyphs = grad_glyphs
        self._grad_bbox = grad_bbox
        self._grad_specs = grad_specs
        self._has_cycle = has_cycle   # drives repaints while a gradient cycles

    def _build_grad_vertices(self):
        """Per-vertex colours for the collected gradient glyphs at the current
        time. Each corner samples the run's ramp at its projected position, so
        the colour is continuous across the whole run and within each glyph."""
        glyphs = self._grad_glyphs
        if not glyphs:
            return None, 0

        elapsed = time.monotonic() - self._start

        # Per run: the gradient axis, and the min/max projection over its bbox,
        # which set the 0..1 range every corner is normalised against.
        info = {}
        for gid, (minx, miny, maxx, maxy) in self._grad_bbox.items():
            grad = self._grad_specs[gid]
            rad = math.radians(grad.angle)
            ax, ay = math.cos(rad), -math.sin(rad)  # 0deg -> right, 90deg -> down
            projs = (minx * ax + miny * ay, maxx * ax + miny * ay,
                     maxx * ax + maxy * ay, minx * ax + maxy * ay)
            pmin, pmax = min(projs), max(projs)
            span = (pmax - pmin) or 1.0
            phase = elapsed * grad.speed if grad.cycle else 0.0
            info[gid] = (grad, ax, ay, pmin, span, grad.cycle, phase)

        data = []
        for gid, rx, ry, gw, rh, u0, v0, u1, v1 in glyphs:
            grad, ax, ay, pmin, span, cycle, phase = info[gid]
            color_at = grad.color_at

            def col(px, py):
                t = (px * ax + py * ay - pmin) / span
                if cycle:
                    m = (t + phase) % 2.0      # ping-pong so the loop is seamless
                    t = 2.0 - m if m > 1.0 else m
                if t < 0.0:
                    t = 0.0
                elif t > 1.0:
                    t = 1.0
                return color_at(t)

            x1, y1 = rx + gw, ry + rh
            r0, g0, b0 = col(rx, ry)    # bottom-left
            r1, g1, b1 = col(x1, ry)    # bottom-right
            r2, g2, b2 = col(x1, y1)    # top-right
            r3, g3, b3 = col(rx, y1)    # top-left
            # Two triangles, winding matching the instanced unit quad.
            data.extend((
                rx, ry, u0, v0, r0, g0, b0,
                x1, ry, u1, v0, r1, g1, b1,
                x1, y1, u1, v1, r2, g2, b2,
                rx, ry, u0, v0, r0, g0, b0,
                x1, y1, u1, v1, r2, g2, b2,
                rx, y1, u0, v1, r3, g3, b3,
            ))
        return array("f", data), len(glyphs) * 6

    def _render_gradients(self):
        """Draw the gradient glyphs on top of the instanced pass."""
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
        """Shrink the cell box (NDC edges) to the image's aspect ratio, centred.
        This is `fit:contain` — the image never distorts, it letterboxes."""
        box_px_w = (r - l) * win_w / 2.0
        box_px_h = (t - b) * win_h / 2.0
        if box_px_w <= 0 or box_px_h <= 0:
            return l, r, t, b
        img_a, box_a = iw / ih, box_px_w / box_px_h
        if img_a > box_a:          # image relatively wider: pillarbox top/bottom
            frac = box_a / img_a
            mid, half = (t + b) / 2.0, (t - b) * frac / 2.0
            return l, r, mid + half, mid - half
        frac = img_a / box_a       # image relatively taller: bars left/right
        mid, half = (l + r) / 2.0, (r - l) * frac / 2.0
        return mid - half, mid + half, t, b

    def _build_zone_ramp(self, zones_with_gradient):
        """One row per gradient, sampled across ZONE_RAMP_W texels. Returns
        {id(zone): row}. Rebuilding this is cheap (<=32KB) so it happens
        fresh each frame zones-with-gradients are on screen -- simpler than
        cache invalidation, and correct for an animating (cycle:on) ramp."""
        rows = zones_with_gradient[:ZONE_RAMP_H]
        pixels = bytearray(ZONE_RAMP_W * ZONE_RAMP_H * 4)
        row_of = {}
        for row, z in enumerate(rows):
            color_at = z.gradient.color_at
            base = row * ZONE_RAMP_W * 4
            for i in range(ZONE_RAMP_W):
                r, g, b = color_at(i / (ZONE_RAMP_W - 1))
                o = base + i * 4
                pixels[o] = int(max(0, min(1, r)) * 255)
                pixels[o + 1] = int(max(0, min(1, g)) * 255)
                pixels[o + 2] = int(max(0, min(1, b)) * 255)
                pixels[o + 3] = 255
            row_of[id(z)] = row
        self.zone_ramp.write(bytes(pixels))
        return row_of

    def _render_zones(self, above):
        """Draw zones (YT;zone) as one instanced batch: a signed-distance
        rounded rect per zone, radius/border/gradient all fragment-shader work.

        `above` picks the layer: text lives at z=0 and wins ties, so zones with
        z<=0 draw *before* the glyph pass and z>=1 draw after it (modals,
        tooltips, overlays). See docs/zones.md.
        """
        term = self.term
        if not term.zones:
            return
        alt = term.alt_screen
        wanted = [z for z in term.zones.values()
                  if z.alt == alt and (z.z >= 1) == above
                  and (z.bg is not None or z.gradient is not None)]
        if not wanted:
            return
        wanted.sort(key=lambda z: z.z)   # lower z first within the layer

        top_abs = term.first_line_no + len(term.scrollback) - term.scroll_offset
        sx = self.cell_w / self.win_w * 2.0
        sy = self.cell_h / self.win_h * 2.0

        visible = []
        for z in wanted:
            row_top = z.top_line - top_abs
            if row_top + z.h <= 0 or row_top >= term.height:
                continue                      # scrolled out of view
            visible.append((z, row_top))
        if not visible:
            return

        gradients = [z for z, _ in visible if z.gradient is not None]
        row_of = self._build_zone_ramp(gradients) if gradients else {}

        data = []
        for z, row_top in visible:
            left = z.x * sx - 1.0
            right = (z.x + z.w) * sx - 1.0
            top = 1.0 - row_top * sy
            bottom = 1.0 - (row_top + z.h) * sy
            fr, fg, fb = z.bg if z.bg is not None else (0.0, 0.0, 0.0)
            if z.border is not None:
                br, bg_, bb = z.border
            else:
                br, bg_, bb = 0.0, 0.0, 0.0
            ramp_row = row_of.get(id(z), -1)
            data.extend((
                left, bottom, right - left, top - bottom,      # pos, size
                fr, fg, fb, z.opacity,                          # fill colour
                z.radius,
                br, bg_, bb, z.opacity if z.border is not None else 0.0,
                z.border_w,
                z.gradient.angle if z.gradient is not None else 0.0,
                float(ramp_row),
            ))

        count = len(data) // _ZONE_FLOATS
        buf = array("f", data)
        need = len(buf) * 4
        if need > self.zone_vbo.size:
            self.zone_vbo.orphan(need)
        else:
            self.zone_vbo.orphan()
        self.zone_vbo.write(buf)
        self.zone_ramp.use(2)
        self.zone_program["u_win"] = (self.win_w, self.win_h)
        self.zone_vao.render(moderngl.TRIANGLES, vertices=6, instances=count)

    def _render_images(self):
        """Draw placed images (YT;img), each as one textured quad pinned to its
        cells so it scrolls with the text."""
        term = self.term
        # Release textures for placements that no longer exist (either buffer).
        live = {id(im) for im in term.images}
        for key in list(self._img_textures):
            if key not in live:
                self._img_textures.pop(key).release()

        drawable = [im for im in term.images if im.alt == term.alt_screen]
        if not drawable:
            return

        # Absolute line number of the top visible row: image row = top_line - it.
        top_abs = term.first_line_no + len(term.scrollback) - term.scroll_offset
        sx = self.cell_w / self.win_w * 2.0
        sy = self.cell_h / self.win_h * 2.0

        for im in drawable:
            row_top = im.top_line - top_abs
            if row_top + im.rows <= 0 or row_top >= term.height:
                continue  # scrolled fully out of view

            tex = self._img_textures.get(id(im))
            if tex is None:
                tex = self.ctx.texture((im.iw, im.ih), 4, im.rgba)
                tex.build_mipmaps()
                tex.anisotropy = self.ctx.max_anisotropy
                self._img_textures[id(im)] = tex

            box_l = im.left * sx - 1.0
            box_r = (im.left + im.cols) * sx - 1.0
            box_t = 1.0 - row_top * sy
            box_b = 1.0 - (row_top + im.rows) * sy
            if im.fit == "contain":
                l, r, t, b = self._contain(box_l, box_r, box_t, box_b,
                                           self.win_w, self.win_h, im.iw, im.ih)
            else:  # 'fill' / 'cover' just use the whole cell box for now
                l, r, t, b = box_l, box_r, box_t, box_b

            # Top image row is v=0, and the top edge sits at higher NDC y.
            quad = array("f", (
                l, b, 0.0, 1.0,
                r, b, 1.0, 1.0,
                r, t, 1.0, 0.0,
                l, b, 0.0, 1.0,
                r, t, 1.0, 0.0,
                l, t, 0.0, 0.0,
            ))
            tex.use(1)
            self.img_vbo.orphan()
            self.img_vbo.write(quad)
            self.img_vao.render(moderngl.TRIANGLES, vertices=6)

    def _text_blink_on(self):
        half = TEXT_BLINK_PERIOD / 2.0
        return int(time.monotonic() / half) % 2 == 0

    # ------------------------------------------------------------ Settings

    def apply_config(self):
        """Re-read the settings that can change while a tab is running.

        `shell` isn't one of them: a shell that's already running can't be
        swapped underneath itself, so that only takes effect on new tabs.
        """
        if self.term is not None:
            self.term.cursor.shape = CONFIG.shape()
        self.set_font_size(CONFIG.font_size)
        self._invalidate()

    def set_font_size(self, px):
        """Resize the text, rebuilding the glyph atlas around the new size."""
        px = max(MIN_FONT_PX, min(MAX_FONT_PX, int(px)))
        if px == self.font_px:
            return
        self.font_px = px
        self.atlas = shared_atlas(px)
        self.cell_w = max(1, round(self.atlas.glyph_w / SUPERSAMPLE))
        self.cell_h = max(1, round(self.atlas.glyph_h / SUPERSAMPLE))
        self._rebuild_texture()
        self._resync_grid()      # fewer/more cells now fit; tell the shell
        self._invalidate()

    def _rebuild_texture(self):
        """The atlas is a different size now, so the texture can't be reused."""
        if self.ctx is None:
            return
        self.makeCurrent()
        try:
            if self.texture is not None:
                self.texture.release()
            self.texture = self.ctx.texture(
                (self.atlas.width, self.atlas.height), 4,
                self.atlas.image.tobytes())
            self.texture.build_mipmaps()
            self.texture.anisotropy = self.ctx.max_anisotropy
            self._atlas_cursor = len(self.atlas.written)
        finally:
            self.doneCurrent()

    # ------------------------------------------------------------ Damage

    def _invalidate(self):
        """Mark the screen's geometry stale and ask for a repaint."""
        self._dirty = True
        self.update()

    def _cursor_state(self):
        """What the caret looks like right now. Compared frame to frame so a
        blinking caret repaints, but a still one doesn't."""
        if not CONFIG.cursor:
            return None            # nothing to repaint for
        cur = self.term.cursor
        if not cur.visible or self.term.scroll_offset != 0:
            return None
        # Quantise the alpha: sub-percent fade steps aren't visible, and
        # repainting for them would defeat the point of tracking damage.
        return (
            cur.x,
            cur.y,
            cur.shape,
            round(self._cursor_alpha(cur, time.monotonic()) * 64),
        )

    def _cursor_alpha(self, cur, now):
        """How solid the caret should be right now, 0..1.

        A caret that blinks *through* your typing reads as laggy, so activity
        (a keystroke, or the cursor moving) holds it solid and the blink only
        resumes after a pause. The blink itself is an eased fade rather than a
        hard toggle, held near full on/off so it still reads as a blink.
        """
        if not self.hasFocus():
            return CURSOR_UNFOCUSED_ALPHA
        if not cur.blink:
            return 1.0
        idle = now - self._cursor_active
        if idle < CURSOR_BLINK_DELAY:
            return 1.0
        phase = (
            (idle - CURSOR_BLINK_DELAY) % CURSOR_BLINK_PERIOD
        ) / CURSOR_BLINK_PERIOD
        if not CONFIG.smooth_blink:
            return 1.0 if phase < 0.5 else 0.0     # classic hard blink
        level = 0.5 + 0.5 * math.cos(2.0 * math.pi * phase)  # 1 -> 0 -> 1
        for _ in range(2):  # smoothstep twice: ease the edges,
            level = level * level * (3.0 - 2.0 * level)  # flatten the holds
        return level

    def _add_cursor(self, builder):
        if not CONFIG.cursor:
            return                 # the user turned the caret off entirely
        cur = self.term.cursor
        if not cur.visible or self.term.scroll_offset != 0:
            return
        if not (0 <= cur.x < self.term.width and 0 <= cur.y < self.term.height):
            return

        now = time.monotonic()
        pos = (cur.x, cur.y)
        if pos != self._cursor_pos:  # movement counts as activity
            self._cursor_pos = pos
            self._cursor_active = now

        alpha = self._cursor_alpha(cur, now)
        if alpha <= 0.01:
            return

        cell = self.term.screen[cur.y][cur.x]
        _fg, bg = self._resolve_colors(cell)
        base = BG_COLOR if bg is None else bg
        # The vertex format has no alpha channel, so fade the caret against the
        # cell's own background instead. Same result, and it stays correct over
        # a colored background.
        color = _lerp(base, CURSOR_COLOR, alpha)

        rx, ry, rw, rh = self._rect(cur.x, cur.y)
        su0, sv0, su1, sv1 = self.atlas.solid_uv()
        # Thickness in logical px rather than a fraction of the cell: a real
        # caret keeps its weight when the font size changes.
        bar_w = min(CURSOR_THICK_PX / self.win_w * 2.0, rw)
        bar_h = min(CURSOR_THICK_PX / self.win_h * 2.0, rh)

        if cur.shape == "block":
            self._add_block_cursor(builder, cell, rx, ry, rw, rh, color, alpha)
        elif cur.shape in ("bar", "vertical"):
            builder.add(rx, ry, bar_w, rh, color=color, u0=su0, v0=sv0, u1=su1, v1=sv1)
        else:  # underline
            builder.add(rx, ry, rw, bar_h, color=color, u0=su0, v0=sv0, u1=su1, v1=sv1)

    def _add_block_cursor(self, builder, cell, rx, ry, rw, rh, color, alpha):
        su0, sv0, su1, sv1 = self.atlas.solid_uv()
        if not self.hasFocus():
            # Hollow outline when the window is inactive, the way editors show
            # "this caret isn't taking input".
            t_w = min(CURSOR_THICK_PX / self.win_w * 2.0, rw)
            t_h = min(CURSOR_THICK_PX / self.win_h * 2.0, rh)
            for x, y, w, h in (
                (rx, ry, rw, t_h),  # bottom
                (rx, ry + rh - t_h, rw, t_h),  # top
                (rx, ry, t_w, rh),  # left
                (rx + rw - t_w, ry, t_w, rh),
            ):  # right
                builder.add(x, y, w, h, color=color, u0=su0, v0=sv0, u1=su1, v1=sv1)
            return

        builder.add(rx, ry, rw, rh, color=color, u0=su0, v0=sv0, u1=su1, v1=sv1)
        if cell.char == " " or cell.width == 0 or cell.conceal:
            return
        u0, v0, u1, v1, is_color = self.atlas.cell_uv(
            cell.char, bold=cell.bold, italic=cell.italic
        )
        if is_color:
            return  # color emoji carry their own RGBA and can't be tinted
        # Redraw the glyph in the background color so the character stays
        # readable *through* the block, fading in along with the caret.
        fg, bg = self._resolve_colors(cell)
        base = BG_COLOR if bg is None else bg
        gw = rw * 2 if cell.width == 2 else rw
        builder.add(
            rx, ry, gw, rh, color=_lerp(fg, base, alpha), u0=u0, v0=v0, u1=u1, v1=v1
        )

    # ------------------------------------------------------------ Selection

    def _ordered_selection(self):
        """(start, end) as absolute (line, col), reading order; None if empty."""
        a, b = self.sel_anchor, self.sel_focus
        if a is None or b is None or a == b:
            return None
        return (a, b) if a <= b else (b, a)

    def _selection_cols(self, abs_line):
        """Column range [cs, ce) selected on `abs_line`, or None. Middle lines
        extend to the full width (linear, text-flow selection)."""
        sel = self._ordered_selection()
        if sel is None:
            return None
        (sl, sc), (el, ec) = sel
        if abs_line < sl or abs_line > el:
            return None
        cs = sc if abs_line == sl else 0
        ce = ec if abs_line == el else self.term.width
        return (cs, ce) if cs < ce else None

    def _add_selection(self, builder):
        if self._ordered_selection() is None:
            return
        su0, sv0, su1, sv1 = self.atlas.solid_uv()
        top = self.term.visible_top()
        for y in range(self.term.height):
            rng = self._selection_cols(top + y)
            if rng is None:
                continue
            cs, ce = rng
            rx, ry, rw, rh = self._rect(cs, y)
            builder.add(
                rx,
                ry,
                rw * (ce - cs),
                rh,
                color=SELECTION_COLOR,
                u0=su0,
                v0=sv0,
                u1=su1,
                v1=sv1,
            )

    def _cell_at(self, pos):
        """Map a widget position (logical px) to absolute (line, col)."""
        col = int(pos.x() // self.cell_w)
        row = int(pos.y() // self.cell_h)
        col = max(0, min(col, self.term.width))
        row = max(0, min(row, self.term.height - 1))
        return (self.term.visible_top() + row, col)

    def _selected_text(self):
        sel = self._ordered_selection()
        if sel is None:
            return ""
        (sl, sc), (el, ec) = sel
        out = []
        for line in range(sl, el + 1):
            row = self.term.line_at(line)
            if row is None:
                out.append("")
                continue
            cs = sc if line == sl else 0
            ce = ec if line == el else len(row)
            cs = max(0, min(cs, len(row)))
            ce = max(0, min(ce, len(row)))
            out.append("".join(c.char for c in row[cs:ce] if c.width != 0).rstrip())
        return "\n".join(out)

    def _clear_selection(self):
        if self.sel_anchor is not None or self.sel_focus is not None:
            self.sel_anchor = self.sel_focus = None

    # ---- terminal mouse reporting (apps that enable ?1000/1002/1003) ----

    _QT_BUTTON = {Qt.LeftButton: 0, Qt.MiddleButton: 1, Qt.RightButton: 2}

    def _mouse_cell(self, pos):
        col = max(1, min(int(pos.x() // self.cell_w) + 1, self.term.width))
        row = max(1, min(int(pos.y() // self.cell_h) + 1, self.term.height))
        return col, row

    def _send_mouse(self, button, col, row, pressed, motion=False):
        if self.term.mouse_sgr:  # SGR (1006): ESC[<b;col;row(M|m)
            b = button + (32 if motion else 0)
            self._write_pty("\x1b[<%d;%d;%d%s" % (b, col, row, "M" if pressed else "m"))
        else:  # legacy X10: ESC[M <b><col><row>
            b = (button if pressed else 3) + (32 if motion else 0)
            self._write_pty(
                "\x1b[M%c%c%c"
                % (min(255, 32 + b), min(255, 32 + col), min(255, 32 + row))
            )

    def _report_mouse(self, event, shift):
        """Send a mouse event to the shell if the app enabled tracking and
        Shift isn't held (Shift forces local selection). Returns True if sent."""
        if not self.term.mouse_mode or shift:
            return False
        col, row = self._mouse_cell(event.position())
        held = event.buttons()
        if event.type() == QtCore.QEvent.MouseMove:
            if self.term.mouse_mode != 1003 and held == Qt.NoButton:
                return True  # 1000/1002 don't report bare motion (hover)
            if (col, row) == self._last_move_cell:
                return True  # only report once per cell, not per pixel
            self._last_move_cell = (col, row)
            btn = (
                0
                if held & Qt.LeftButton
                else 1 if held & Qt.MiddleButton else 2 if held & Qt.RightButton else 3
            )  # 3 = no button held (pure hover motion)
            self._send_mouse(btn, col, row, pressed=True, motion=True)
        else:
            btn = self._QT_BUTTON.get(event.button(), 0)
            pressed = event.type() == QtCore.QEvent.MouseButtonPress
            self._send_mouse(btn, col, row, pressed=pressed)
        return True

    def mousePressEvent(self, event):
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if self._report_mouse(event, shift):
            return
        if event.button() == Qt.LeftButton:
            self._selecting = True
            cell = self._cell_at(event.position())
            self.sel_anchor = cell
            self.sel_focus = cell
            self._invalidate()

    def mouseMoveEvent(self, event):
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if self._report_mouse(event, shift):
            return
        if self._selecting:
            self.sel_focus = self._cell_at(event.position())
            self._invalidate()

    def mouseReleaseEvent(self, event):
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if self._report_mouse(event, shift):
            return
        if event.button() == Qt.LeftButton and self._selecting:
            self._selecting = False
            self.sel_focus = self._cell_at(event.position())
            self._invalidate()

    # ------------------------------------------------------------ Input

    # DECKPAM: with the keypad in application mode the numeric keys send these
    # instead of digits. Programs bind against them, so a stored flag that
    # nothing reads is worse than useless — it looks implemented.
    _KEYPAD_APP = {
        Qt.Key_0: "\x1bOp", Qt.Key_1: "\x1bOq", Qt.Key_2: "\x1bOr",
        Qt.Key_3: "\x1bOs", Qt.Key_4: "\x1bOt", Qt.Key_5: "\x1bOu",
        Qt.Key_6: "\x1bOv", Qt.Key_7: "\x1bOw", Qt.Key_8: "\x1bOx",
        Qt.Key_9: "\x1bOy", Qt.Key_Period: "\x1bOn", Qt.Key_Comma: "\x1bOl",
        Qt.Key_Plus: "\x1bOk", Qt.Key_Minus: "\x1bOm",
        Qt.Key_Asterisk: "\x1bOj", Qt.Key_Slash: "\x1bOo",
        Qt.Key_Enter: "\x1bOM",
    }

    def _arrow(self, letter):
        """A cursor-key sequence, honouring DECCKM (?1).

        In application mode the arrows send ESC O A rather than ESC [ A, and
        readline/vim key bindings are written against that — send the wrong one
        and arrow keys stop working inside them.
        """
        return ("\x1bO" if self.term.cursor_keys_app else "\x1b[") + letter

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)

        # Clipboard (terminal convention: Ctrl+Shift+C/V).
        if ctrl and shift and key == Qt.Key_V:
            self._paste()
            return
        if ctrl and shift and key == Qt.Key_C:
            self._copy()
            return

        # Ctrl+C copies when there's a selection, otherwise sends interrupt
        # (matches Windows Terminal).
        if ctrl and not shift and key == Qt.Key_C:
            if self._ordered_selection() is not None:
                self._copy()
                self._clear_selection()
            else:
                self._send("\x03")
            self._invalidate()
            return

        # View scrolling (not sent to the shell).
        if key == Qt.Key_PageUp:
            self.term.scroll_up(max(1, self.term.height - 1))
            return
        if key == Qt.Key_PageDown:
            self.term.scroll_down(max(1, self.term.height - 1))
            return

        # DECKPAM: the keypad speaks application sequences. Checked before the
        # normal tables, since Enter and the digits appear in both.
        if self.term.keypad_app and (mods & Qt.KeypadModifier):
            seq = self._KEYPAD_APP.get(key)
            if seq:
                self._send(seq)
                return

        special = {
            Qt.Key_Return: "\r",
            Qt.Key_Enter: "\r",
            Qt.Key_Backspace: "\x7f",
            Qt.Key_Tab: "\t",
            Qt.Key_Escape: "\x1b",
            Qt.Key_Up: self._arrow("A"),
            Qt.Key_Down: self._arrow("B"),
            Qt.Key_Right: self._arrow("C"),
            Qt.Key_Left: self._arrow("D"),
            Qt.Key_Home: self._arrow("H"),
            Qt.Key_End: self._arrow("F"),
            Qt.Key_Delete: "\x1b[3~",
        }
        if key in special:
            self._send(special[key])
            return

        # Ctrl+<letter> -> control code (Ctrl+C = 0x03 interrupt, etc.).
        if ctrl and not shift and Qt.Key_A <= key <= Qt.Key_Z:
            self._send(chr(key & 0x1F))
            return

        text = event.text()
        if text and not ctrl and ord(text[0]) >= 32:
            self._send(text)
            return

        super().keyPressEvent(event)

    WHEEL_LINES = 3

    def wheelEvent(self, event):
        dy = event.angleDelta().y()
        if dy == 0:
            return
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        up = dy > 0

        # The app asked for mouse events: hand it the wheel (unless Shift
        # forces local scrolling).
        if self.term.mouse_mode and not shift:
            col, row = self._mouse_cell(event.position())
            self._send_mouse(64 if up else 65, col, row, pressed=True)
            return

        # Full-screen apps (less, vim, git log) run on the alternate screen,
        # which has no scrollback — scrolling it locally does nothing at all.
        # Send arrow keys instead: that's what real terminals do, and what
        # those apps are listening for.
        if self.term.alt_screen and not shift:
            self._write_pty(self._arrow("A" if up else "B") * self.WHEEL_LINES)
            return

        if up:
            self.term.scroll_up(self.WHEEL_LINES)
        else:
            self.term.scroll_down(self.WHEEL_LINES)
        self._invalidate()

    def _paste(self):
        text = QtWidgets.QApplication.clipboard().text()
        if not text:
            return
        text = text.replace("\r\n", "\r").replace("\n", "\r")
        if self.term.bracketed_paste:
            # Wrap so the app knows it's a paste (won't treat newlines as Enter).
            self.term.scroll_to_bottom()
            self._write_pty("\x1b[200~" + text + "\x1b[201~")
        else:
            self._send(text)

    def _copy(self):
        text = self._selected_text()
        if text:
            QtWidgets.QApplication.clipboard().setText(text)


class SettingsDialog(QtWidgets.QDialog):
    """The settings GUI, built by walking YTConfig's fields.

    Nothing here knows what the settings *are* — the widget type comes from the
    field's default (bool -> checkbox, int -> spinbox) and its metadata
    (choices -> combo box), so adding a setting to config.py makes it appear
    here with no UI code at all.
    """

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("YoTerm Settings")
        self.setMinimumWidth(440)
        self._editors = {}        # name -> (get, set)

        form = QtWidgets.QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft)
        for spec in fields(config):
            widget, get, set_ = self._editor(spec, getattr(config, spec.name))
            self._editors[spec.name] = (get, set_)
            label = QtWidgets.QLabel(spec.metadata.get("label", spec.name))
            hint = spec.metadata.get("help")
            if hint:
                label.setToolTip(hint)
                widget.setToolTip(hint)
            form.addRow(label, widget)

        note = QtWidgets.QLabel(
            "Saved to %s — that file is plain Python, so you can edit it by "
            "hand too." % config_path())
        note.setObjectName("hint")
        note.setWordWrap(True)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok
            | QtWidgets.QDialogButtonBox.Cancel
            | QtWidgets.QDialogButtonBox.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QtWidgets.QDialogButtonBox.RestoreDefaults
                       ).clicked.connect(self._restore_defaults)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    @staticmethod
    def _editor(spec, value):
        """(widget, getter, setter) for one field."""
        meta = spec.metadata
        if isinstance(spec.default, bool):        # before int: bool is an int
            box = QtWidgets.QCheckBox()
            box.setChecked(bool(value))
            return box, box.isChecked, box.setChecked
        if meta.get("choices"):
            combo = QtWidgets.QComboBox()
            combo.addItems(meta["choices"])
            combo.setCurrentText(value)
            return combo, combo.currentText, combo.setCurrentText
        if isinstance(spec.default, int):
            spin = QtWidgets.QSpinBox()
            spin.setRange(meta.get("min", 0), meta.get("max", 9999))
            spin.setValue(int(value))
            return spin, spin.value, spin.setValue
        line = QtWidgets.QLineEdit(str(value))
        return line, line.text, line.setText

    def _restore_defaults(self):
        defaults = YTConfig()
        for name, (_get, set_) in self._editors.items():
            set_(getattr(defaults, name))

    def result_config(self):
        config = YTConfig()
        for name, (get, _set) in self._editors.items():
            setattr(config, name, get())
        return config


class MainWindow(QtWidgets.QMainWindow):
    """The window owns the tabs; each tab is a TerminalWidget with its own
    shell, and names itself via OSC 0/2.

    Frameless, in Windows Terminal's shape: the tab strip *is* the title bar,
    with the window controls in the same row. That means we own moving,
    resizing and maximising, which the native frame would normally do.

    It's a QTabBar + QStackedWidget rather than a QTabWidget so the header can
    be laid out properly -- tabs, then '+' immediately after the last tab
    instead of stranded at the far right. The two are kept index-for-index in
    sync.
    """

    DEFAULT_TAB_TITLE = "Shell"
    TAB_TITLE_LIMIT = 26

    def __init__(self):
        super().__init__()
        self.setWindowTitle("YoTerm")
        self.setWindowIcon(app_icon())
        self.setStyleSheet(STYLE_SHEET + combo_arrow_qss())

        self.bar = QtWidgets.QTabBar()
        self.bar.setMovable(True)
        self.bar.setDrawBase(False)
        self.bar.setExpanding(False)
        self.bar.setElideMode(Qt.ElideRight)
        self.bar.setUsesScrollButtons(True)
        self.bar.setIconSize(QtCore.QSize(16, 16))
        self.bar.setFocusPolicy(Qt.NoFocus)  # never steal focus from the shell
        self.bar.currentChanged.connect(self._tab_changed)
        self.bar.tabMoved.connect(self._tab_moved)
        self.bar.installEventFilter(self)  # middle-click close

        self.stack = QtWidgets.QStackedWidget()

        add = QtWidgets.QToolButton()
        add.setObjectName("strip")
        add.setText("+")
        add.setToolTip("New tab (Ctrl+Shift+T)")
        add.setCursor(Qt.PointingHandCursor)
        add.setFocusPolicy(Qt.NoFocus)
        add.clicked.connect(self.new_tab)

        menu = QtWidgets.QMenu(self)
        menu.addAction("New tab\tCtrl+Shift+T", self.new_tab)
        menu.addAction("Close tab\tCtrl+Shift+W", self.close_current_tab)
        menu.addSeparator()
        menu.addAction("Settings…\tCtrl+,", self.open_settings)
        menu.addAction("Edit settings file…", self.edit_config_file)
        menu.addSeparator()
        menu.addAction("Zoom in\tCtrl+=", lambda: self._zoom(1))
        menu.addAction("Zoom out\tCtrl+-", lambda: self._zoom(-1))
        menu.addAction("Reset zoom\tCtrl+0", lambda: self._zoom(0))
        menu.addSeparator()
        menu.addAction("About YoTerm", self._about)
        drop = QtWidgets.QToolButton()
        drop.setObjectName("strip")
        drop.setText("\u2304")
        drop.setToolTip("More")
        drop.setCursor(Qt.PointingHandCursor)
        drop.setFocusPolicy(Qt.NoFocus)
        drop.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        drop.setMenu(menu)

        header = QtWidgets.QWidget()
        header.setObjectName("header")
        header.setFixedHeight(HEADER_H)
        row = QtWidgets.QHBoxLayout(header)
        row.setContentsMargins(4, 0, 0, 0)  # small gutter before the first tab
        row.setSpacing(0)
        row.addWidget(self.bar)
        row.addWidget(add)
        row.addWidget(drop)
        row.addStretch(1)

        central = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(header)
        column.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        # ApplicationShortcut so these fire before the focused terminal's
        # keyPressEvent forwards them to the shell.
        for keys, slot in (
            ("Ctrl+Shift+T", self.new_tab),
            ("Ctrl+Shift+W", self.close_current_tab),
            ("Ctrl+,", self.open_settings),
            ("Ctrl+=", lambda: self._zoom(1)),
            ("Ctrl++", lambda: self._zoom(1)),     # the shifted key, on some layouts
            ("Ctrl+-", lambda: self._zoom(-1)),
            ("Ctrl+0", lambda: self._zoom(0)),
            ("Ctrl+Tab", lambda: self._cycle(1)),
            ("Ctrl+Shift+Tab", lambda: self._cycle(-1)),
            ("Ctrl+PgDown", lambda: self._cycle(1)),
            ("Ctrl+PgUp", lambda: self._cycle(-1)),
        ):
            self._shortcut(keys, slot)
        for n in range(1, 10):
            self._shortcut("Ctrl+%d" % n, lambda i=n - 1: self._select(i))

        self.resize(1200, 760)
        self.new_tab()

    def _shortcut(self, keys, slot):
        sc = QtGui.QShortcut(QtGui.QKeySequence(keys), self)
        sc.setContext(Qt.ApplicationShortcut)
        sc.activated.connect(slot)
        return sc

    # ------------------------------------------------------------ settings

    def open_settings(self):
        global CONFIG
        dialog = SettingsDialog(CONFIG, self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        new = dialog.result_config()
        try:
            config_module.save(new)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(
                self, "YoTerm", "Couldn't save your settings:\n\n%s" % exc)
            return
        CONFIG = new
        for i in range(self.stack.count()):
            self.stack.widget(i).apply_config()

    def edit_config_file(self):
        """Hand the file to whatever the OS opens .py with."""
        path = config_path()
        if not os.path.exists(path):
            try:
                path = config_module.save(CONFIG)   # something to actually edit
            except OSError as exc:
                QtWidgets.QMessageBox.warning(
                    self, "YoTerm", "Couldn't create %s:\n\n%s" % (path, exc))
                return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _zoom(self, step):
        """Zoom every tab together.

        Per-tab sizes would mean a separate 45 MB atlas per size on screen,
        which isn't a trade worth making for a terminal.
        """
        current = self.current().font_px if self.current() else CONFIG.font_size
        px = CONFIG.font_size if step == 0 else current + step
        px = max(MIN_FONT_PX, min(MAX_FONT_PX, px))
        for i in range(self.stack.count()):
            self.stack.widget(i).set_font_size(px)

    def _about(self):
        QtWidgets.QMessageBox.about(
            self,
            "About YoTerm",
            "<b>YoTerm</b><br>A GPU-accelerated terminal, built from scratch.",
        )

    # ------------------------------------------------------------ tabs

    def count(self):
        return self.bar.count()

    def widget(self, index):
        return self.stack.widget(index)

    def current(self):
        return self.stack.currentWidget()

    def new_tab(self):
        term = TerminalWidget(self)
        index = self.stack.addWidget(term)
        self.bar.addTab(QtGui.QIcon(make_logo(16)), self.DEFAULT_TAB_TITLE)
        term.titleChanged.connect(lambda t, w=term: self._set_tab_title(w, t))
        term.exited.connect(lambda w=term: self._close_widget(w))

        # Bind the close button to the *widget*: indices shift when tabs are
        # dragged or neighbours close, but the widget identity doesn't.
        close = QtWidgets.QToolButton()
        close.setObjectName("tabClose")
        close.setText("\u2715")
        close.setFixedSize(18, 18)
        close.setCursor(Qt.PointingHandCursor)
        close.setFocusPolicy(Qt.NoFocus)
        close.setToolTip("Close tab (Ctrl+Shift+W)")
        close.clicked.connect(lambda _=False, w=term: self._close_widget(w))
        self.bar.setTabButton(index, QtWidgets.QTabBar.RightSide, close)

        self.bar.setCurrentIndex(index)
        self._tab_changed(index)
        term.setFocus()
        return term

    def close_tab(self, index):
        widget = self.stack.widget(index)
        if widget is None:
            return
        self.bar.removeTab(index)
        self.stack.removeWidget(widget)
        widget.shutdown()
        widget.deleteLater()
        if self.bar.count() == 0:
            self.close()  # last tab closed -> close the window
        else:
            self._tab_changed(self.bar.currentIndex())

    def _close_widget(self, widget):
        index = self.stack.indexOf(widget)
        if index >= 0:
            self.close_tab(index)

    def close_current_tab(self):
        self.close_tab(self.bar.currentIndex())

    def _tab_moved(self, frm, to):
        # Keep the stack in the same order as the bar.
        widget = self.stack.widget(frm)
        self.stack.removeWidget(widget)
        self.stack.insertWidget(to, widget)
        self.stack.setCurrentIndex(self.bar.currentIndex())

    def _cycle(self, step):
        count = self.bar.count()
        if count > 1:
            self.bar.setCurrentIndex((self.bar.currentIndex() + step) % count)

    def _select(self, index):
        if 0 <= index < self.bar.count():
            self.bar.setCurrentIndex(index)

    # ------------------------------------------------------------ titles

    @staticmethod
    def _clean_title(title):
        """Tidy a raw OSC title.

        Windows sets the console title to the launched program's image path by
        default, and ConPTY *restores* that default whenever a program that set
        its own title exits (lazygit, vim, ...). So quitting lazygit renames the
        tab to 'C:\\Program Files\\WindowsApps\\...\\pwsh.exe'. That's Windows'
        placeholder rather than a real title \u2014 show the program's name instead.
        """
        title = " ".join(title.split())
        if title.lower().endswith(".exe") and ("\\" in title or "/" in title):
            return os.path.splitext(os.path.basename(title))[0]
        return title

    @classmethod
    def _tab_label(cls, title):
        """Fit a title onto a tab. '&' is a mnemonic marker in Qt tab text, so
        it has to be doubled or a path like 'A&B' silently loses it."""
        title = cls._clean_title(title) or cls.DEFAULT_TAB_TITLE
        if len(title) > cls.TAB_TITLE_LIMIT:
            title = title[: cls.TAB_TITLE_LIMIT - 1] + "\u2026"
        return title.replace("&", "&&")

    def _set_tab_title(self, widget, title):
        index = self.stack.indexOf(widget)
        if index < 0:
            return
        self.bar.setTabText(index, self._tab_label(title))
        self.bar.setTabToolTip(index, title)  # full title on hover
        if index == self.bar.currentIndex():
            self._sync_window_title()

    def _sync_window_title(self):
        widget = self.current()
        raw = widget.term.title if (widget and widget.term) else ""
        title = self._clean_title(raw)
        self.setWindowTitle("%s \u2014 YoTerm" % title if title else "YoTerm")

    def _tab_changed(self, index):
        self.stack.setCurrentIndex(index)
        self._sync_window_title()
        widget = self.stack.widget(index)
        if widget is not None:
            widget.setFocus()

    # ------------------------------------------------------------ window

    def eventFilter(self, obj, event):
        if obj is self.bar:
            kind = event.type()
            if (
                kind == QtCore.QEvent.MouseButtonRelease
                and event.button() == Qt.MiddleButton
            ):
                index = self.bar.tabAt(event.position().toPoint())
                if index >= 0:
                    self.close_tab(index)  # middle-click closes a tab
                    return True
            elif kind == QtCore.QEvent.MouseButtonDblClick:
                if self.bar.tabAt(event.position().toPoint()) < 0:
                    self.new_tab()  # double-click strip = new tab
                    return True
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        enable_dark_titlebar(self)  # needs a real handle, so not in __init__

    def closeEvent(self, event):
        for i in range(self.stack.count()):
            self.stack.widget(i).shutdown()
        event.accept()


def main():
    global CONFIG

    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setDepthBufferSize(24)
    QSurfaceFormat.setDefaultFormat(fmt)

    # Load settings before the first widget exists: they decide the font size,
    # the shell and the caret, all of which are read during construction.
    CONFIG, problem = config_module.load()

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("YoTerm")
    app.setApplicationDisplayName("YoTerm")
    app.setWindowIcon(app_icon())
    window = MainWindow()
    window.show()
    window.current().setFocus()

    if problem:
        # Started fine on defaults; say what was wrong rather than silently
        # ignoring a file the user thought was in effect.
        QtWidgets.QMessageBox.warning(
            window, "YoTerm settings",
            "Your settings file couldn't be used in full, so defaults were "
            "applied where needed:\n\n%s\n\n%s" % (problem, config_path()))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
