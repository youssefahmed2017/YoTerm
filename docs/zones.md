# Zones

A **zone** is a styled rectangular region. YoTerm never asks what a zone *is* —
not a button, not a card, not a dialog. It only knows there is a rectangle with
these rendering properties, and hands it to the GPU.

```
Cozy TUI / Application
        │  "this button occupies this rectangle"
        ▼
      YoTerm            cells -> GPU coordinates
        ▼
   ModernGL renderer
        ▼
   Fragment shaders     rounded corners, gradients, shadows, ...
```

That separation is the whole point: the protocol never has to grow a
`YT;button`, `YT;checkbox` or `YT;window`. An application describes geometry and
style; new visual effects become shader work, not protocol work.

## The model

```
Zone
  id                    caller-assigned int
  x, y, w, h            integer CELL units
  z         = 0         layer (see z-order below)
  radius    = 0
  bg        = none      solid fill
  border    = none      colour + width
  gradient  = none      reuses ytseq.GradientRun's ramp math
  shadow    = 0
  opacity   = 1.0
```

The first five values are geometry; everything else is optional styling.

**The full field set exists in the model from Phase 1.** Only the *rendering* of
each field arrives with its phase. This freezes the protocol early, so an
application can be written against it while the shader catches up — sending
`radius:8` before Phase 2 is harmless, it simply doesn't draw rounded yet.

## Protocol

```
ESC ] YT;zone;create;id:1;x:10;y:5;w:20;h:3 ST
ESC ] YT;zone;update;id:1;radius:8;bg:#3366ff;border:#88bbff ST
ESC ] YT;zone;move;id:1;x:12;y:6 ST
ESC ] YT;zone;delete;id:1 ST
```

- `create` makes (or replaces) a zone; it also accepts styling in the same
  command, so a fully-styled zone is one round trip.
- `update` changes only the fields mentioned. This is what animation uses — an
  application updates `x` / `radius` / `opacity` each frame rather than
  delete-and-recreate.
- `move` is `update` restricted to `x`/`y`, kept because it reads better.
- `delete` removes it. `delete;id:*` removes all zones on the current screen.

Colours accept everything `ytseq.parse_color` does: `#rrggbb`, `#rgb`, a name
(`red`), or an SGR code (`31`). Gradient stops are **comma**-separated, since
`;` already separates fields: `gradient:#ff0000,#0000ff;angle:90`.

Zone `id`s live in their own namespace — a `YT;img` with `id:1` and a
`YT;zone` with `id:1` are unrelated.

## Coordinates and anchoring

Coordinates are **integer cell units**, exactly like `YT;img`. This fits the
terminal mental model, matches how a TUI already lays out, and keeps hit-testing
and clipping simple. Sub-cell positioning (`x:10.3`, or a `px` suffix) is
deliberately out of scope for now.

`y` is resolved once, at command time, into an **absolute line number** the same
way images are (`first_line_no + len(scrollback) + y`). So a zone scrolls with
the text it belongs to, and is dropped when that line falls out of scrollback.
On the alternate screen — where full-screen TUIs live — there is no scrollback,
so this is a non-issue there.

Zones are scoped to the screen they were created on (primary vs. alternate),
like images: alt-screen zones are discarded when the alt screen is left, and
`ED`/`RIS` clear them the same way they clear images.

## Z-order

Text occupies the **z = 0** layer. Zones sort among themselves by `z`, and:

> **At equal z, text draws over zones.**

That tie-break is what makes the common case free. A button background is just
`z:0` (the default) and its label lands on top without the application
specifying anything:

```
+-----------------------+     zone: x=5 y=10 w=22 h=3 radius=8 bg=blue
| Install Package       |     then simply print "Install Package"
+-----------------------+
```

- `z < 0` — behind other zones (a card behind a panel)
- `z = 0` — behind text, above lower zones  *(default)*
- `z >= 1` — **above** text: modals, tooltips, dropdowns, command palettes,
  loading overlays

Exposing a numeric `z` rather than three fixed layers costs nothing and avoids
repainting ourselves into a corner later.

## Rendering

Zones render as **instanced quads** — one instance per zone, expanded from the
shared unit quad, in two draw calls (below-text and above-text). Not one draw
call per zone: an application redrawing a screenful of cards every frame must
not cost a draw call each.

The CPU never rasterises a rounded rectangle. It uploads a rect, a radius, and
some colours; the fragment shader does every pixel via a signed-distance field.

Expected scale: **dozens to a few hundred** zones. Zones are for visual
containers — windows, panels, cards, buttons, scroll views, dialogs, menus,
overlays, progress bars, input boxes. They are *not* for per-cell colouring;
ANSI already does that, and one zone per cell in a 100×100 grid (10,000 zones)
is not the intended use.

## Clipping — and why it's the expensive phase

Rectangular clipping uses `glScissor`: hardware, fast, and it doesn't touch the
shaders. Rounded clipping later discards fragments outside the rounded SDF. Both,
each where it fits.

The catch worth knowing up front: **scissor doesn't compose with instancing.**
Scissor is per-draw-call GL state, so clipping content *inside* a parent zone
means the clipped content's draws must be bracketed by a scissor — which forces
the **glyph pass to be split by clip region**. Today that pass is a single call
for the whole screen.

At the expected zone counts (a handful of scroll views) that's a few extra draw
calls and completely fine. But it means Phase 4 restructures glyph batching
rather than adding to it. Phases 1–3 don't touch the text pipeline at all.

## Phases

**Phase 1 — Geometry.** ✅ Done. `create` / `update` / `move` / `delete`,
absolute-line anchoring, scroll pruning, screen scoping, z-sorted into two
instanced batches. Proved the whole pipeline (protocol → model → anchoring →
instancing → GPU) before any shader math.

**Phase 2 — Styling.** ✅ Done. `radius`, `bg`, `border`, `gradient` all render,
via a signed-distance-field rounded rect computed per fragment (Inigo Quilez's
`sdRoundBox`) — the CPU still only uploads a rectangle, a radius, and colours.
Gradient fill reads a **shared ramp texture** (`ZONE_RAMP_W` × `ZONE_RAMP_H`,
one row per zone-with-a-gradient drawn that frame) rather than packing colour
stops into vertex attributes: that scales to any number of stops without
bumping into the ~16 vertex-attribute limit a fixed-size instance layout would
hit. The ramp is rebuilt fresh each frame gradients are on screen — simpler
than cache invalidation, and it's what makes an animating (`cycle:on`) zone
gradient correct later. Border and fill are composed in one pass via a second
SDF offset by `border_width`, so a rounded border follows the same curve as
the fill with no separate geometry.

**Phase 3 — Effects.** ✅ Done. `shadow` and `opacity` both render.

Shadow is a soft-edge falloff computed in the *same* SDF pass — no second draw,
no blurred texture. The rendered quad is padded by exactly `shadow` px on every
side (so the falloff has room to reach zero before the geometry's own edge,
with no visible seam), and the fragment shader composites the content *over*
a translucent silhouette of the same rounded shape whose alpha fades linearly
from the content's edge outward. A zone with only `shadow` set (no `bg`, no
`gradient`) renders as a soft shadow shape with nothing solid on top — useful
as a pure decoration.

Opacity is applied **once, to the whole composited result** (fill + border +
shadow together) rather than baked separately into each color's alpha. That is
what makes a translucent card cast a correspondingly faint shadow instead of a
full-strength one — the same reasoning a UI compositor would use.

Note: *background* blur — blurring what is behind a zone — needs an offscreen
framebuffer and is a different cost class; it is not in scope here.

**Phase 4 — Clipping.** Scissor for rectangular, shader-discard for rounded,
including the glyph-batching split described above. Enables scroll views and
cards that actually contain their contents.

**Phase 5 — Animation.** No new protocol; Phase 1's `update` already covers it.
The work is ensuring a zone update invalidates only the zone instance buffer and
never forces a full glyph rebuild — the same discipline as the existing
`_dirty` / `_has_cycle` split used for cycling gradients.

## Code layout

| File | Role |
|------|------|
| `ytzone.py` | `Zone` model, styling/colour parsing. Shell-agnostic. |
| `term.py` | `YT;zone` dispatch, `self.zones`, anchoring, clearing, pruning. |
| `app.py` | The instanced zone render pass. |

Parsing and the model are deliberately shell-agnostic, so the experimental GLFW
build (`app_glfw.py`) would inherit zones for free — only the render pass is
shell-specific.
