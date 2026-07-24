"""GPU renderer for YoTerm.

Extracted from ``app.py``: this owns the ModernGL context and every GPU
resource (programs, buffers, vertex arrays, the glyph-atlas texture, the zone
gradient ramp), plus the whole paint pass. ``TerminalWidget`` keeps the Qt
lifecycle, the PTY, input handling and the terminal model, and delegates
drawing to a ``Renderer`` it owns.

The renderer reads live per-frame state (the terminal model, cell size, cursor
liveness, overlay hover timers, selection) from the hosting widget: any
attribute it doesn't define itself falls through ``__getattr__`` to the widget.
Writes that must land back on the widget (repaint-signal flags, the hit-test
box cache) go through ``self.w`` explicitly.
"""
import math
import time
import moderngl
from array import array

from tools import RectangleBuilder, cell_rect_px, PALETTE

# Rendering appearance constants live here (the module that draws with them), so
# renderer.py has no dependency on app.py -- app.py imports Renderer, and a back
# dependency would be a circular import (fatal when app.py runs as __main__).
# Live settings are read per-frame off the widget as ``self.w.config`` instead.
BG_COLOR = (0.06, 0.06, 0.08)         # terminal background / clear color
SELECTION_COLOR = (0.20, 0.30, 0.52)  # highlight behind selected cells
DIM_FACTOR = 0.55                     # brightness multiplier for SGR 2 (dim)
CURSOR_COLOR = (0.90, 0.92, 0.98)     # caret color when fully on
CURSOR_THICK_PX = 2.0                 # bar/underline weight in *logical* px


def _lerp(a, b, t):
    """Blend two RGB tuples."""
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)

# Shared by both shaders below that need a rounded-rect containment test:
# the main glyph/background program (for clipping to a zone's shape) and the
# zone program itself (for its own fill/border/shadow). One definition so the
# two can't drift apart. Inigo Quilez's rounded-box SDF: negative inside,
# positive outside, in the same units as p/b.
_SD_ROUND_BOX_GLSL = """
float sdRoundBox(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}
"""

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

// Only used when this draw call is clipped to a zone (see u_clip_round in the
// fragment shader) -- a widget-size uniform is enough to recover each
// fragment's absolute logical-px position from its NDC one.
uniform vec2 u_win;

out vec3 v_color;
out vec2 v_uv;
out float v_mode;
out vec2 v_frag_px;
void main() {
    vec2 ndc = in_pos + in_corner * in_size;
    gl_Position = vec4(ndc, 0, 1);
    v_uv = mix(in_uv0, in_uv1, in_corner);
    v_color = in_color;
    v_mode = in_mode;
    v_frag_px = (ndc * 0.5 + 0.5) * u_win;
}
"""

FRAGMENT_SHADER = (
    """
#version 330
uniform sampler2D tex;

// Rounded-clip test for a batch drawn inside a `clip:on` zone (Phase 4, see
// docs/zones.md). u_clip_round is 0 for every ordinary draw call -- the
// overwhelming majority -- so they pay only the branch, never the discard.
// Rectangular clipping doesn't need this at all: it's a real glScissor.
uniform float u_clip_round;
uniform vec2 u_clip_center;
uniform vec2 u_clip_half;
uniform float u_clip_radius;

in vec3 v_color;
in vec2 v_uv;
in float v_mode;
in vec2 v_frag_px;
out vec4 color;
"""
    + _SD_ROUND_BOX_GLSL
    + """
void main() {
    if (u_clip_round > 0.5) {
        float d = sdRoundBox(v_frag_px - u_clip_center, u_clip_half, u_clip_radius);
        if (d > 0.0) discard;
    }
    vec4 texel = texture(tex, v_uv);
    if (v_mode > 0.5) {
        color = texel;                   // color glyph (emoji): own RGBA
    } else {
        color = vec4(v_color, texel.a);  // alpha-keyed glyph tinted by cell
    }
}
"""
)

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
ZONE_RAMP_W = 128  # texels per gradient ramp (resolution along the ramp)
ZONE_RAMP_H = 64  # rows: max zones with a *distinct* gradient drawn per frame

ZONE_SHADOW_COLOR = (0.0, 0.0, 0.0, 0.5)  # translucent black; no colour field yet

ZONE_VERTEX_SHADER = """
#version 330
in vec2 in_corner;        // per-vertex: the shared unit quad, (0,0)..(1,1)
in vec2 in_pos;           // per-instance: content box bottom-left, NDC
in vec2 in_size;          // per-instance: content box width/height, NDC
in vec4 in_color;         // fill colour (rgb; a is the fill's own alpha, 1=opaque)
in float in_radius;       // corner radius, logical px
in vec4 in_border_color;
in float in_border_width; // logical px; 0 = no border
in float in_angle;        // gradient angle, degrees (0 = left->right)
in float in_ramp_row;     // row in the ramp texture, or < 0 for a solid fill
in float in_shadow;       // soft-shadow spread, logical px; 0 = none
in float in_opacity;      // final multiplier over the whole composited zone

uniform vec2 u_win;       // widget size, logical px -- converts NDC to px

// The rendered quad is padded by `in_shadow` px on every side so the shadow's
// falloff has room to fade to nothing without being clipped by the quad
// itself -- a shadow is drawn in the SAME pass, not a second blurred texture.
out vec2 v_local;         // fragment position relative to the CONTENT centre, px
out vec2 v_half;          // CONTENT half-size (unpadded), px
flat out vec4 v_color;
flat out float v_radius;
flat out vec4 v_border_color;
flat out float v_border_width;
flat out float v_angle;
flat out float v_ramp_row;
flat out float v_shadow;
flat out float v_opacity;

void main() {
    vec2 content_half_px = in_size * u_win * 0.25;
    vec2 content_center = in_pos + in_size * 0.5;
    // 1 NDC unit == u_win/2 px, so a px pad converts back with 2/u_win.
    vec2 pad_ndc = vec2(in_shadow) * 2.0 / u_win;
    vec2 padded_pos = in_pos - pad_ndc;
    vec2 padded_size = in_size + pad_ndc * 2.0;
    vec2 vertex_ndc = padded_pos + in_corner * padded_size;

    gl_Position = vec4(vertex_ndc, 0, 1);
    v_local = (vertex_ndc - content_center) * u_win * 0.5;
    v_half = content_half_px;
    v_color = in_color;
    v_radius = in_radius;
    v_border_color = in_border_color;
    v_border_width = in_border_width;
    v_angle = in_angle;
    v_ramp_row = in_ramp_row;
    v_shadow = in_shadow;
    v_opacity = in_opacity;
}
"""

ZONE_FRAGMENT_SHADER = (
    """
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
flat in float v_shadow;
flat in float v_opacity;
out vec4 color;

const vec4 SHADOW_COLOR = vec4(%(sr)s, %(sg)s, %(sb)s, %(sa)s);
"""
    % {
        "sr": ZONE_SHADOW_COLOR[0],
        "sg": ZONE_SHADOW_COLOR[1],
        "sb": ZONE_SHADOW_COLOR[2],
        "sa": ZONE_SHADOW_COLOR[3],
    }
    + _SD_ROUND_BOX_GLSL
    + """
void main() {
    float r = min(v_radius, min(v_half.x, v_half.y));
    float d = sdRoundBox(v_local, v_half, r);
    // ~1px antialiased edge; independent of zoom since everything is in px.
    float shape_alpha = 1.0 - smoothstep(-1.0, 1.0, d);

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
    vec3 content_rgb = mix(v_border_color.rgb, fill.rgb, fill_mask);
    float content_a = mix(v_border_color.a, fill.a, fill_mask) * shape_alpha;

    vec4 result = vec4(content_rgb, content_a);
    if (v_shadow > 0.0) {
        // Soft-edge falloff from the content's own edge outward, over
        // `v_shadow` px -- a blur approximation computed for this fragment
        // directly, not a second draw with a real gaussian.
        float shadow_mask = 1.0 - smoothstep(0.0, v_shadow, max(d, 0.0));
        vec4 shadow = vec4(SHADOW_COLOR.rgb, SHADOW_COLOR.a * shadow_mask);
        // Composite the content OVER the shadow (standard "over" blending),
        // so the shadow only shows where the content doesn't cover it.
        float out_a = content_a + shadow.a * (1.0 - content_a);
        vec3 out_rgb = (content_rgb * content_a
                        + shadow.rgb * shadow.a * (1.0 - content_a))
                       / max(out_a, 0.0001);
        result = vec4(out_rgb, out_a);
    }

    result.a *= v_opacity;
    if (result.a <= 0.0) discard;
    color = result;
}
"""
    % {"ramp_h": ZONE_RAMP_H}
)

# 18 floats per zone instance: pos.xy, size.xy, color.rgba, radius,
# border.rgba, border_width, angle, ramp_row, shadow, opacity.
_ZONE_FLOATS = 18

class Renderer:
    """Owns the ModernGL context and draws every frame for one TerminalWidget."""

    def __init__(self, widget):
        self.w = widget
        self.ctx = moderngl.create_context()

        self.program = self.ctx.program(
            vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER
        )
        self.program["tex"] = 0
        # Off by default; a clip-bucket draw call turns it on for just that
        # call, so the ordinary (overwhelmingly common) draw never pays for it.
        self.program["u_clip_round"] = 0.0

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

        # A second instance buffer for clip-bucket draws (Phase 4, see
        # docs/zones.md): cells inside a `clip:on` zone are built into their
        # own small batch and rendered separately (scissored, and for a
        # rounded zone also shader-discarded), so they can't share `self.vbo`
        # with the main screen -- that buffer's contents must survive
        # untouched into next frame for the "skip the upload if unchanged"
        # optimisation above. Reused sequentially, one clip zone at a time.
        self.clip_vbo = self.ctx.buffer(reserve=256_000)
        self.clip_vao = self.ctx.vertex_array(
            self.program,
            [
                (self.quad_vbo, "2f", "in_corner"),
                (
                    self.clip_vbo,
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
        self.zone_program["ramp_tex"] = 2  # unit 0 = glyph atlas, 1 = images
        self.zone_vbo = self.ctx.buffer(reserve=64_000)
        self.zone_vao = self.ctx.vertex_array(
            self.zone_program,
            [
                (self.quad_vbo, "2f", "in_corner"),
                (
                    self.zone_vbo,
                    "2f 2f 4f 1f 4f 1f 1f 1f 1f 1f/i",
                    "in_pos",
                    "in_size",
                    "in_color",
                    "in_radius",
                    "in_border_color",
                    "in_border_width",
                    "in_angle",
                    "in_ramp_row",
                    "in_shadow",
                    "in_opacity",
                ),
            ],
        )
        # Shared gradient-ramp atlas: one row per zone-with-a-gradient drawn
        # this frame, rebuilt only when at least one is on screen.
        self.zone_ramp = self.ctx.texture(
            (ZONE_RAMP_W, ZONE_RAMP_H), 4, b"\x00" * (ZONE_RAMP_W * ZONE_RAMP_H * 4)
        )
        self.zone_ramp.filter = (moderngl.LINEAR, moderngl.LINEAR)

        # Render-side caches -- were TerminalWidget attributes before the split;
        # only the renderer touches them, so they live here now.
        self._static_data = None
        self._static_quads = 0
        self._clip_render_data = []
        self._color_cache = {}
        self._img_textures = {}
        self._thumb_tex = {}
        self._grad_glyphs = []
        self._grad_bbox = {}
        self._grad_specs = {}
        self._atlas_pending = False  # a font resize asked to rebuild the texture

    def __getattr__(self, name):
        """Forward reads of widget/model/input state to the hosting widget.

        Only names not found on the Renderer reach here, so the GL resources and
        caches set in ``__init__`` never forward. Uses ``__dict__`` directly to
        stay recursion-safe before ``self.w`` is set.
        """
        w = self.__dict__.get("w")
        if w is not None:
            return getattr(w, name)
        raise AttributeError(name)

    def paint(self):

        # cell_rect_px works in logical px (ratios); the GL viewport must be in
        # physical px so we fill the whole (HiDPI) framebuffer.
        self.w.win_w, self.w.win_h = self.width(), self.height()
        dpr = self.devicePixelRatioF()
        pw = max(1, int(round(self.width() * dpr)))
        ph = max(1, int(round(self.height() * dpr)))

        # QOpenGLWidget renders into its OWN framebuffer, not FBO 0.
        self.ctx.detect_framebuffer(self.defaultFramebufferObject()).use()
        self.ctx.viewport = (0, 0, pw, ph)

        # A pending font-resize rebuilds the atlas texture here, with the context
        # current (see rebuild_atlas). Before _add_cells so fresh glyphs upload
        # into the correctly-sized texture this same frame.
        if self._atlas_pending:
            self._rebuild_atlas_texture()
            self._atlas_pending = False

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
            # may rasterize new glyphs on demand, and returns cells routed
            # into a `clip:on` zone instead (Phase 4, see docs/zones.md)
            clip_batches = self._add_cells(body, glyphs)
            self._add_selection(body)
            body.extend(glyphs)
            self._static_data = body.buffer()
            self._static_quads = body.count
            self._clip_render_data = [
                (buf.buffer(), buf.count, center, half, radius)
                for buf, center, half, radius in clip_batches.values()
            ]
            self.w._dirty = False

        cursor = RectangleBuilder()
        self._add_cursor(cursor)
        self._add_hover_link(cursor)
        self.w._last_cursor_state = self._cursor_state()

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

        self.program["u_win"] = (self.win_w, self.win_h)
        self.program["u_clip_round"] = 0.0  # the default draw is never clipped

        total = self._static_quads + cursor.count
        if total:
            stride = RectangleBuilder.FLOATS_PER_QUAD * 4  # bytes per quad
            needed = total * stride
            if needed > self.vbo.size:
                self.vbo.orphan(needed)  # grow; the old contents go with it
                rewrite = True
            elif rewrite:
                self.vbo.orphan()  # discard first; avoids stalling on the GPU
            if rewrite:
                self.vbo.write(self._static_data)
            if cursor.count:
                self.vbo.write(cursor.buffer(), offset=self._static_quads * stride)
            self.vao.render(moderngl.TRIANGLES, vertices=6, instances=total)

        self._render_clip_batches(pw, ph, dpr)

        # Images sit over their (blank, reserved) cells; gradient text draws on
        # top of the instanced pass. Both are their own programs.
        self._render_images()
        self._render_video_overlay()  # pause indicator, on top of the frame
        self._render_gradients()
        self._render_zones(above=True)  # overlays: modals, tooltips, menus
        self._render_fullscreen_video()  # a video taking over the whole terminal

    # ------------------------------------------------------------ PTY I/O


    def rebuild_atlas(self):
        """The atlas changed size (a font resize), so the texture can't be
        reused. Just flag it: the actual GL texture is (re)created at the top of
        the next paint, where this widget's GL context is properly current --
        creating it from a settings callback (via makeCurrent) left the glyphs
        unsampled and the text invisible."""
        self._atlas_pending = True

    def _rebuild_atlas_texture(self):
        """Re-create the atlas texture for the current (resized) atlas. Runs
        inside paint(), context already current."""
        if self.texture is not None:
            self.texture.release()
        self.texture = self.ctx.texture(
            (self.atlas.width, self.atlas.height), 4, self.atlas.image.tobytes()
        )
        self.texture.build_mipmaps()
        self.texture.anisotropy = self.ctx.max_anisotropy
        self._atlas_cursor = len(self.atlas.written)

    # ------------------------------------------------------------ Damage


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
            if invert:  # DECSCNM (?5): the whole screen reads inverted
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

    def _clip_zones_screen_rects(self):
        """Active `clip:on` zones this frame, as (zone, row0, row1, col0, col1)
        in SCREEN space (row1/col1 exclusive), clamped to the visible grid and
        scroll-adjusted. See docs/zones.md Phase 4.

        Only zone-to-cell clipping is in scope here: a zone's own quad
        (`_render_zones`) is never itself clipped by another zone, and
        gradient text / images aren't routed through this -- just the plain
        background+glyph pass, which is the overwhelmingly common case.
        """
        term = self.term
        if not term.zones:
            return []
        top_abs = term.first_line_no + len(term.scrollback) - term.scroll_offset
        alt = term.alt_screen
        out = []
        for z in term.zones.values():
            if not z.clip or z.alt != alt:
                continue
            row0 = max(0, z.top_line - top_abs)
            row1 = min(term.height, z.top_line - top_abs + z.h)
            col0 = max(0, z.x)
            col1 = min(term.width, z.x + z.w)
            if row1 <= row0 or col1 <= col0:
                continue  # off-screen or degenerate
            out.append((z, row0, row1, col0, col1))
        return out

    def _clip_rect_px(self, row0, row1, col0, col1):
        """A screen cell-rect -> (center_px, half_px) in the same absolute,
        bottom-up logical-px convention the glyph shader's v_frag_px uses."""
        xs, ys, sx, sy = self._grid_tables()
        left_ndc, right_ndc = xs[col0], xs[col1]
        top_ndc = ys[row0] + sy       # top of the first included row
        bottom_ndc = ys[row1 - 1]     # bottom of the last included row
        left_px = (left_ndc * 0.5 + 0.5) * self.win_w
        right_px = (right_ndc * 0.5 + 0.5) * self.win_w
        top_px = (top_ndc * 0.5 + 0.5) * self.win_h
        bottom_px = (bottom_ndc * 0.5 + 0.5) * self.win_h
        center = ((left_px + right_px) * 0.5, (bottom_px + top_px) * 0.5)
        half = ((right_px - left_px) * 0.5, (top_px - bottom_px) * 0.5)
        return center, half

    def _add_cells(self, bg_builder, glyph_builder):
        """One pass over the grid emitting both background and glyph quads.

        Backgrounds must all sit behind all glyphs, so they go to separate
        builders that the caller concatenates in order — that keeps the draw
        order while resolving each cell's colours and geometry only once.

        A cell whose (row, col) falls inside a `clip:on` zone is routed into
        that zone's OWN pair of builders instead of the ones passed in, so it
        can later be drawn as its own scissored batch (Phase 4, see
        docs/zones.md) rather than the normal full-screen one. Returns
        {zone_id: (RectangleBuilder, center_px, half_px, radius_px)} for the
        zones that ended up with anything in them; empty when nothing clips.
        """
        su0, sv0, su1, sv1 = self.atlas.solid_uv()
        xs, ys, rw, rh = self._grid_tables()
        width = self.term.width
        colors_for, atlas_uv = self._colors_for, self.atlas.cell_uv
        default_add_bg, default_add_glyph = bg_builder.add, glyph_builder.add
        ul_h, st_h, st_off = rh * 0.08, rh * 0.08, rh * 0.45

        # SGR 5 only animates if the user asked for it: blink is ancient and
        # most terminals render it steadily. Off, it also costs nothing — no
        # blinking cells means no repaints to drive them.
        blink_enabled = self.w.config.text_blink
        blink_on = (not blink_enabled) or self._text_blink_on()
        has_blink = False

        # Gradient glyphs are pulled out of the fast instanced path and drawn
        # per-vertex instead. Collect their geometry and each run's bounding box
        # here; the colours are filled in per frame in _build_grad_vertices.
        grad_glyphs, grad_bbox, grad_specs = [], {}, {}
        has_cycle = False

        # Clip buckets: only allocated when something on screen actually
        # clips, so the overwhelmingly common "no clip zones" case pays just
        # one list-emptiness check per cell, not a lookup.
        clip_zones = self._clip_zones_screen_rects()
        clip_bg, clip_glyph = {}, {}
        for z, *_rect in clip_zones:
            clip_bg[z.id] = RectangleBuilder()
            clip_glyph[z.id] = RectangleBuilder()

        for y, row in enumerate(self.term.visible_lines()):
            ry = ys[y]
            for x, cell in enumerate(row):
                if x >= width:
                    break
                char = cell.char
                # Fast path: a plain empty cell contributes nothing at all.
                # A space can still need decoration -- underline/strike, or a
                # dotted link underline if it falls inside hyperlinked text
                # (e.g. the space between two words of one link).
                if (
                    char == " "
                    and cell.bg == "default"
                    and not cell.reverse
                    and not cell.underline
                    and not cell.strike
                    and not cell.href
                ):
                    continue

                if clip_zones:
                    zid = None
                    for z, row0, row1, col0, col1 in clip_zones:
                        if row0 <= y < row1 and col0 <= x < col1:
                            zid = z.id
                            break
                    if zid is not None:
                        add_bg, add_glyph = clip_bg[zid].add, clip_glyph[zid].add
                    else:
                        add_bg, add_glyph = default_add_bg, default_add_glyph
                else:
                    add_bg, add_glyph = default_add_bg, default_add_glyph

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
                        # (Gradient text isn't clip-routed in Phase 4 -- it's
                        # its own pass with no scissor support yet.)
                        gid = id(cell.grad)
                        grad_specs[gid] = cell.grad
                        x1, y1 = rx + gw, ry + rh
                        bb = grad_bbox.get(gid)
                        if bb is None:
                            grad_bbox[gid] = [rx, ry, x1, y1]
                        else:
                            if rx < bb[0]:
                                bb[0] = rx
                            if ry < bb[1]:
                                bb[1] = ry
                            if x1 > bb[2]:
                                bb[2] = x1
                            if y1 > bb[3]:
                                bb[3] = y1
                        grad_glyphs.append((gid, rx, ry, gw, rh, u0, v0, u1, v1))
                        if cell.grad.cycle:
                            has_cycle = True
                    else:
                        add_glyph(
                            rx, ry, gw, rh, fg, u0, v0, u1, v1, 1.0 if is_color else 0.0
                        )

                # Underline / strikethrough are thin solid bars (they apply to
                # spaces too, e.g. an underlined blank), and blink with the
                # cell they belong to.
                if blink_on or not cell.blink:
                    if cell.underline:
                        add_glyph(rx, ry, rw, ul_h, fg, su0, sv0, su1, sv1)
                    elif cell.href:
                        # Dotted underline: the always-visible affordance
                        # that this text is a link (skipped when the app
                        # already drew its own solid underline there). The
                        # SOLID version on hover is a separate per-frame
                        # overlay (_add_hover_link) precisely so hovering
                        # never forces a full geometry rebuild on every
                        # mouse move -- only content changes do that.
                        dot_w = min(ul_h, rw * 0.22)
                        add_glyph(rx + rw * 0.14, ry, dot_w, ul_h, fg,
                                 su0, sv0, su1, sv1)
                        add_glyph(rx + rw * 0.56, ry, dot_w, ul_h, fg,
                                 su0, sv0, su1, sv1)
                    if cell.strike:
                        add_glyph(rx, ry + st_off, rw, st_h, fg, su0, sv0, su1, sv1)

        self.w._has_blink = has_blink  # drives repaints only while it's True
        self._grad_glyphs = grad_glyphs
        self._grad_bbox = grad_bbox
        self._grad_specs = grad_specs
        self.w._has_cycle = has_cycle  # drives repaints while a gradient cycles

        clip_batches = {}
        for z, row0, row1, col0, col1 in clip_zones:
            b, g = clip_bg[z.id], clip_glyph[z.id]
            b.extend(g)
            if b.count:
                center, half = self._clip_rect_px(row0, row1, col0, col1)
                clip_batches[z.id] = (b, center, half, z.radius)
        return clip_batches

    def _render_clip_batches(self, pw, ph, dpr):
        """Draw each `clip:on` zone's cells as its own scissored (and, if the
        zone is rounded, shader-discarded) batch. See docs/zones.md Phase 4.

        Unlike the main screen buffer, these are re-uploaded every frame
        rather than skipped-when-unchanged: giving each clip zone its own
        persistent GPU-buffer slot would track the zones coming and going,
        which isn't worth it at the "dozens of zones" scale this is for.
        """
        if not self._clip_render_data:
            return
        for arr, count, center, half, radius in self._clip_render_data:
            need = len(arr) * 4  # floats -> bytes
            if need > self.clip_vbo.size:
                self.clip_vbo.orphan(need)
            else:
                self.clip_vbo.orphan()
            self.clip_vbo.write(arr)

            cx, cy = center
            hx, hy = half
            x0 = max(0, int(round((cx - hx) * dpr)))
            y0 = max(0, int(round((cy - hy) * dpr)))
            w0 = max(0, min(int(round(hx * 2 * dpr)), pw - x0))
            h0 = max(0, min(int(round(hy * 2 * dpr)), ph - y0))
            self.ctx.scissor = (x0, y0, w0, h0)

            if radius > 0:
                self.program["u_clip_round"] = 1.0
                self.program["u_clip_center"] = center
                self.program["u_clip_half"] = half
                self.program["u_clip_radius"] = radius
            else:
                # A plain rectangle is already exact via glScissor alone --
                # no need to pay for the discard test too.
                self.program["u_clip_round"] = 0.0

            self.clip_vao.render(moderngl.TRIANGLES, vertices=6, instances=count)

        self.ctx.scissor = None
        self.program["u_clip_round"] = 0.0

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
            projs = (
                minx * ax + miny * ay,
                maxx * ax + miny * ay,
                maxx * ax + maxy * ay,
                minx * ax + maxy * ay,
            )
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
                    m = (t + phase) % 2.0  # ping-pong so the loop is seamless
                    t = 2.0 - m if m > 1.0 else m
                if t < 0.0:
                    t = 0.0
                elif t > 1.0:
                    t = 1.0
                return color_at(t)

            x1, y1 = rx + gw, ry + rh
            r0, g0, b0 = col(rx, ry)  # bottom-left
            r1, g1, b1 = col(x1, ry)  # bottom-right
            r2, g2, b2 = col(x1, y1)  # top-right
            r3, g3, b3 = col(rx, y1)  # top-left
            # Two triangles, winding matching the instanced unit quad.
            data.extend(
                (
                    rx,
                    ry,
                    u0,
                    v0,
                    r0,
                    g0,
                    b0,
                    x1,
                    ry,
                    u1,
                    v0,
                    r1,
                    g1,
                    b1,
                    x1,
                    y1,
                    u1,
                    v1,
                    r2,
                    g2,
                    b2,
                    rx,
                    ry,
                    u0,
                    v0,
                    r0,
                    g0,
                    b0,
                    x1,
                    y1,
                    u1,
                    v1,
                    r2,
                    g2,
                    b2,
                    rx,
                    y1,
                    u0,
                    v1,
                    r3,
                    g3,
                    b3,
                )
            )
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
        if img_a > box_a:  # image relatively wider: pillarbox top/bottom
            frac = box_a / img_a
            mid, half = (t + b) / 2.0, (t - b) * frac / 2.0
            return l, r, mid + half, mid - half
        frac = img_a / box_a  # image relatively taller: bars left/right
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
        wanted = [
            z
            for z in term.zones.values()
            if z.alt == alt
            and (z.z >= 1) == above
            and (z.bg is not None or z.gradient is not None or z.shadow > 0)
        ]
        if not wanted:
            return
        wanted.sort(key=lambda z: z.z)  # lower z first within the layer

        top_abs = term.first_line_no + len(term.scrollback) - term.scroll_offset
        sx = self.cell_w / self.win_w * 2.0
        sy = self.cell_h / self.win_h * 2.0

        visible = []
        for z in wanted:
            row_top = z.top_line - top_abs
            if row_top + z.h <= 0 or row_top >= term.height:
                continue  # scrolled out of view
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
            data.extend(
                (
                    left,
                    bottom,
                    right - left,
                    top - bottom,  # pos, size
                    fr,
                    fg,
                    fb,
                    1.0 if z.bg is not None or z.gradient is not None else 0.0,
                    z.radius,
                    br,
                    bg_,
                    bb,
                    1.0 if z.border is not None else 0.0,
                    z.border_w,
                    z.gradient.angle if z.gradient is not None else 0.0,
                    float(ramp_row),
                    z.shadow,
                    z.opacity,  # applied once, to the whole composited zone
                )
            )

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
                self._img_textures.pop(key)[1].release()

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

            entry = self._img_textures.get(id(im))
            if entry is None or entry[0] is not im:
                # New placement (the identity check guards against a replacement
                # frame reusing a freed id()): create the texture.
                if entry is not None:
                    entry[1].release()
                tex = self.ctx.texture((im.iw, im.ih), 4, im.rgba)
                tex.build_mipmaps()
                tex.anisotropy = self.ctx.max_anisotropy
                self._img_textures[id(im)] = (im, tex, im.rev)
            elif entry[2] != im.rev:
                # Same placement, new pixels (a video frame swapped them in
                # place, rev bumped). Reuse the GPU texture and upload in place
                # when the size is unchanged -- which it always is frame to frame
                # -- instead of freeing and reallocating a texture every frame.
                tex = entry[1]
                if tex.size == (im.iw, im.ih):
                    tex.write(im.rgba)
                    tex.build_mipmaps()
                else:  # a resized frame: the old texture can't be reused
                    tex.release()
                    tex = self.ctx.texture((im.iw, im.ih), 4, im.rgba)
                    tex.build_mipmaps()
                    tex.anisotropy = self.ctx.max_anisotropy
                self._img_textures[id(im)] = (im, tex, im.rev)
            else:
                tex = entry[1]

            if im.id == self.w._fullscreen_vid:
                continue  # drawn by _render_fullscreen_video, not in its cell box

            box_l = im.left * sx - 1.0
            box_r = (im.left + im.cols) * sx - 1.0
            box_t = 1.0 - row_top * sy
            box_b = 1.0 - (row_top + im.rows) * sy
            if im.fit == "contain":
                l, r, t, b = self._contain(
                    box_l, box_r, box_t, box_b, self.win_w, self.win_h, im.iw, im.ih
                )
            else:  # 'fill' / 'cover' just use the whole cell box for now
                l, r, t, b = box_l, box_r, box_t, box_b

            # Top image row is v=0, and the top edge sits at higher NDC y.
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

    # ------------------------------------------------------------ Native video


    def _px_to_ndc(self, x, y_top, w, h):
        """A logical-px rect (top-left origin) -> NDC rect (bottom-left origin)."""
        return (
            x / self.win_w * 2.0 - 1.0,
            1.0 - (y_top + h) / self.win_h * 2.0,
            w / self.win_w * 2.0,
            h / self.win_h * 2.0,
        )

    def _clip_write(self, data):
        """Upload overlay geometry to the shared clip VBO, growing it if the
        batch outgrew the current buffer, else just discarding the old contents
        (orphaning) so the write never stalls on the GPU."""
        if len(data) > self.clip_vbo.size:
            self.clip_vbo.orphan(len(data))
        else:
            self.clip_vbo.orphan()
        self.clip_vbo.write(data)

    def _overlay_quad(self, builder, x, y_top, w, h, color, mode=0.0,
                      uv=None):
        nl, nb, nw, nh = self._px_to_ndc(x, y_top, w, h)
        if uv is None:
            uv = self.atlas.solid_uv()
        builder.add(nl, nb, nw, nh, color, uv[0], uv[1], uv[2], uv[3], mode)

    def _overlay_round(self, x, y_top, w, h, color, radius_frac=1.0):
        """Draw one rounded rect (a circle when w==h and radius_frac==1) via the
        pipeline's per-draw rounded-clip test. Its own draw call — cheap, and
        the overlay only ever has a few."""
        b = RectangleBuilder()
        self._overlay_quad(b, x, y_top, w, h, color)
        # The clip test runs in v_frag_px space, whose y counts up from the
        # bottom — so flip the centre's y from the top-left px we drew with.
        self.program["u_win"] = (self.win_w, self.win_h)
        self.program["u_clip_round"] = 1.0
        self.program["u_clip_center"] = (x + w * 0.5,
                                         self.win_h - (y_top + h * 0.5))
        self.program["u_clip_half"] = (w * 0.5, h * 0.5)
        self.program["u_clip_radius"] = min(w, h) * 0.5 * radius_frac
        self._clip_write(b.buffer())
        self.clip_vao.render(moderngl.TRIANGLES, vertices=6, instances=1)
        self.program["u_clip_round"] = 0.0

    def _overlay_text(self, builder, text, x, y_top, h, color):
        """Lay out a string into `builder` at pixel (x, y_top), glyphs `h` px
        tall, keeping the font's cell aspect. Returns nothing — width is
        predictable via _text_w."""
        gw = h * self.cell_w / self.cell_h
        for ch in text:
            u0, v0, u1, v1, is_color = self.atlas.cell_uv(ch)
            self._overlay_quad(builder, x, y_top, gw, h, color,
                               mode=1.0 if is_color else 0.0,
                               uv=(u0, v0, u1, v1))
            x += gw

    def _text_w(self, text, h):
        return len(text) * h * self.cell_w / self.cell_h

    OV_TRAIL = (0.78, 0.78, 0.80)   # light hover-ahead trail on the scrubber

    def _thumb_texture(self, vid):
        """The GPU texture for a video's scrub-preview thumbnail, (re)built from
        the latest decoded pixels. Returns (tex, w, h) or None. Runs in paintGL,
        so creating/releasing textures here is safe."""
        thumb = self._thumbs.get(vid)
        if thumb is None:
            return None
        rgba, w, h, seq = thumb
        entry = self._thumb_tex.get(vid)
        if entry is None or entry[1] != seq:
            if entry is not None:
                entry[0].release()
            tex = self.ctx.texture((w, h), 4, rgba)
            tex.build_mipmaps()
            self._thumb_tex[vid] = (tex, seq)
        return self._thumb_tex[vid][0], w, h

    def _overlay_thumb(self, tex, x, y_top, w, h):
        """Draw a thumbnail texture (unit 1, its own program) as one quad."""
        nl, nb, nw, nh = self._px_to_ndc(x, y_top, w, h)
        r2, t2 = nl + nw, nb + nh
        quad = array("f", (
            nl, nb, 0.0, 1.0, r2, nb, 1.0, 1.0, r2, t2, 1.0, 0.0,
            nl, nb, 0.0, 1.0, r2, t2, 1.0, 0.0, nl, t2, 0.0, 0.0,
        ))
        tex.use(1)
        self.img_vbo.orphan()
        self.img_vbo.write(quad)
        self.img_vao.render(moderngl.TRIANGLES, vertices=6)

    def _render_video_overlay(self):
        """Draw the overlay for every on-screen video; cache their boxes so the
        mouse handlers can hit-test the scrubber."""
        boxes = self._video_boxes_px()
        self.w._video_boxes = {vid: (l, t, r, b) for vid, l, t, r, b in boxes}
        # Free thumbnail pixels/textures for videos that have gone away.
        for vid in [v for v in self._thumb_tex if v not in self._videos]:
            self._thumb_tex.pop(vid)[0].release()
            self._thumbs.pop(vid, None)
        if not boxes:
            return
        self.texture.use()  # solid_uv + glyphs both sample the atlas
        now = time.monotonic()
        controls_on = now < self._controls_until
        flash_on = now < self._seek_flash_until
        for vid, l, t, r, b in boxes:
            if vid == self.w._fullscreen_vid:
                continue  # its overlay is drawn by _render_fullscreen_video
            ctrl = self._videos.get(vid)
            if ctrl is not None:
                self._render_one_overlay(vid, ctrl, l, t, r, b,
                                         controls_on, flash_on)

    def _render_fullscreen_video(self):
        """Draw the fullscreen video over everything: opaque black across the
        whole terminal, the frame aspect-fit into it, then the scrubber overlay.
        The frame is decoded at the viewport size (Player.resize) so it's crisp;
        any not-yet-resized frame is just scaled up to fill in the meantime."""
        vid = self.w._fullscreen_vid
        if vid is None or vid not in self._videos:
            return
        im = next((i for i in self.term.images if i.id == vid), None)
        entry = self._img_textures.get(id(im)) if im is not None else None
        if im is None or entry is None or not im.iw or not im.ih:
            return
        tex = entry[1]
        W, H = float(self.win_w), float(self.win_h)

        # 1. Opaque black over the whole terminal (hides text + the inline video).
        self.texture.use()
        bg = RectangleBuilder()
        self._overlay_quad(bg, 0.0, 0.0, W, H, (0.0, 0.0, 0.0))
        self.program["u_win"] = (self.win_w, self.win_h)
        self.program["u_clip_round"] = 0.0
        self._clip_write(bg.buffer())
        self.clip_vao.render(moderngl.TRIANGLES, vertices=6, instances=1)

        # 2. The frame, aspect-fit (letterboxed) into the viewport and centred.
        aspect = im.iw / im.ih
        if W / H > aspect:      # viewport is wider: fit to height
            fh, fw = H, H * aspect
        else:                   # fit to width
            fw, fh = W, W / aspect
        x, y = (W - fw) * 0.5, (H - fh) * 0.5
        self._overlay_thumb(tex, x, y, fw, fh)

        # 3. The scrubber overlay on the video rect; expose the box for the mouse.
        l, t, r, b = x, y, x + fw, y + fh
        self.w._video_boxes[vid] = (l, t, r, b)
        ctrl = self._videos.get(vid)
        if ctrl is not None:
            self.texture.use()
            now = time.monotonic()
            self._render_one_overlay(vid, ctrl, l, t, r, b,
                                     now < self._controls_until,
                                     now < self._seek_flash_until)

    def _render_one_overlay(self, vid, ctrl, l, t, r, b, controls_on, flash_on):
        w, h = r - l, b - t
        dur = ctrl.duration or 0.0
        pos = ctrl.position
        frac = max(0.0, min(1.0, pos / dur)) if dur > 0 else 0.0
        paused = ctrl.paused
        full = paused or controls_on          # scrubber + time + handle
        flash = flash_on and not full         # bare red line + timestamp + dot
        tl, tr, tw, ty, th = self._scrubber_geom(l, t, r, b)
        txt_h = min(max(9.0, h * 0.035), 15.0)  # capped so it stays compact
        pad = txt_h * 0.55
        chip_y = ty - th * 0.5 - txt_h - pad * 1.8  # time chips sit above the bar

        # Where the cursor sits along the bar (for the hover trail + preview).
        hovering = (full and self._video_hover == ctrl.img_id
                    and self._video_hover_px is not None)
        hx = hf = None
        if hovering:
            hx = min(max(self._video_hover_px, tl), tr)
            hf = (hx - tl) / tw

        # --- rounded backgrounds first: they sit UNDER the flat batch ---
        if paused:
            rad = min(min(w, h) * 0.11, 46.0)
            self._overlay_round(l + w / 2 - rad, t + h / 2 - rad,
                                2 * rad, 2 * rad, self.OV_DARK, 1.0)
            bw, bh, gap = rad * 0.28, rad * 0.9, rad * 0.30
            by = t + h / 2 - bh / 2
            self._overlay_round(l + w / 2 - gap / 2 - bw, by, bw, bh,
                                self.OV_WHITE, 0.9)
            self._overlay_round(l + w / 2 + gap / 2, by, bw, bh,
                                self.OV_WHITE, 0.9)

        preview = None  # (text, centre_x) for a scrub-preview timestamp
        if hovering:
            label = self._fmt_time(hf * dur)
            lw = self._text_w(label, txt_h)
            cx = min(max(hx, tl + lw / 2 + pad), tr - lw / 2 - pad)
            self._overlay_round(cx - lw / 2 - pad, chip_y - pad * 0.4,
                                lw + 2 * pad, txt_h + pad * 0.8, self.OV_DARK, 0.6)
            preview = (label, cx)

        time_label = None
        if full:
            time_label = f"{self._fmt_time(pos)} / {self._fmt_time(dur)}"
            self._overlay_round(tl - pad * 0.5, chip_y - pad * 0.4,
                                self._text_w(time_label, txt_h) + pad,
                                txt_h + pad * 0.8, self.OV_DARK, 0.6)
        elif flash:
            time_label = f"{self._fmt_time(pos)} / {self._fmt_time(dur)}"
            fw = self._text_w(time_label, txt_h)
            self._overlay_round(l + w / 2 - fw / 2 - pad, chip_y - pad * 0.4,
                                fw + 2 * pad, txt_h + pad * 0.8, self.OV_DARK, 0.6)

        # --- flat batch: track, hover trail, progress, glyphs (one draw) ---
        flat = RectangleBuilder()
        if full or flash:
            track = self.OV_TRACK if full else (0.4, 0.4, 0.43)
            self._overlay_quad(flat, tl, ty - th / 2, tw, th, track)
            # light hover-ahead trail (YouTube's grey fill toward the cursor),
            # under the red so the red always marks the true position
            if hovering and hf > frac:
                self._overlay_quad(flat, tl, ty - th / 2, tw * hf, th,
                                   self.OV_TRAIL)
            self._overlay_quad(flat, tl, ty - th / 2, tw * frac, th, self.OV_RED)
        if full:
            self._overlay_text(flat, time_label, tl + pad * 0.5,
                               chip_y, txt_h, self.OV_WHITE)
            if preview is not None:
                self._overlay_text(flat, preview[0],
                                   preview[1] - self._text_w(preview[0], txt_h) / 2,
                                   chip_y, txt_h, self.OV_WHITE)
        elif flash:
            self._overlay_text(flat, time_label, l + w / 2
                               - self._text_w(time_label, txt_h) / 2,
                               chip_y, txt_h, self.OV_WHITE)
        if flat.count:
            self.program["u_win"] = (self.win_w, self.win_h)
            self.program["u_clip_round"] = 0.0
            self._clip_write(flat.buffer())
            self.clip_vao.render(moderngl.TRIANGLES, vertices=6,
                                 instances=flat.count)

        # --- handle dot on the progress fill (───────●───────), rounded ---
        # Shown on a seek flash too, so a keyboard/click seek reads as YouTube's
        # scrubber-with-handle, not just a bare line.
        if full or flash:
            hr = min(max(th * 1.4, 5.0), 8.0)
            self._overlay_round(tl + tw * frac - hr, ty - hr, 2 * hr, 2 * hr,
                                self.OV_RED, 1.0)

        # --- scrub-preview thumbnail floating above the cursor ---
        if hovering:
            self._render_thumb(vid, l, r, hx, chip_y - pad, h)

    def _render_thumb(self, vid, l, r, hx, above_y, box_h):
        """Draw the hovered frame's thumbnail (with a dark frame) centred on the
        cursor x, its bottom just above `above_y`. No-op until a frame decodes."""
        got = self._thumb_texture(vid)
        if got is None:
            return
        tex, iw, ih = got
        disp_w = min(max(box_h * 0.42, 96.0), 190.0)
        disp_h = disp_w * ih / iw
        cx = min(max(hx, l + disp_w / 2 + 3), r - disp_w / 2 - 3)
        x = cx - disp_w / 2
        y = max(3.0, above_y - disp_h)
        bd = 2.5  # dark frame thickness
        self._overlay_round(x - bd, y - bd, disp_w + 2 * bd, disp_h + 2 * bd,
                            self.OV_DARK, 0.28)
        self._overlay_thumb(tex, x, y, disp_w, disp_h)


    def _add_cursor(self, builder):
        if not self.w.config.cursor:
            return  # the user turned the caret off entirely
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


    def _add_hover_link(self, builder):
        """Underline every cell sharing the currently-hovered OSC 8 target, so
        a link spanning multiple cells (or wrapped onto more than one line)
        highlights as one unit, like a browser does. Rebuilt fresh every
        frame rather than folded into the cached screen geometry, since it
        changes on every mouse move; skipped entirely when nothing's
        hovered, which is the common case. Works while scrolled into
        history too -- `visible_lines()` already accounts for that.
        """
        href = self._hover_href
        if href is None:
            return
        su0, sv0, su1, sv1 = self.atlas.solid_uv()
        width = self.term.width
        colors_for = self._colors_for
        for y, row in enumerate(self.term.visible_lines()):
            if y >= self.term.height:
                break
            for x, cell in enumerate(row):
                if x >= width:
                    break
                if cell.href != href:
                    continue
                fg, _bg = colors_for(cell)
                rx, ry, rw, rh = self._rect(x, y)
                builder.add(rx, ry, rw, rh * 0.08, fg, su0, sv0, su1, sv1)

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

