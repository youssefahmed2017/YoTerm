"""YoTerm custom-OSC (the `ESC ] YT ; ...` namespace) demo + smoke checks.

Run this *inside YoTerm* to see the features it can do that a cell-stepped
terminal can only fake:

    python yt_seq_tests.py            # handshake check, then the visual demo
    python yt_seq_tests.py --fast     # no pauses
    python yt_seq_tests.py --check    # handshake only, no visuals

The handshake is genuinely asserted (YoTerm answers `YT;?`); the gradients are
visual — each block prints what it should look like next to the real thing.
On any other terminal the whole `YT` namespace is silently ignored, so the
gradient text simply prints in its normal colour instead of turning into
escape-code garbage. That graceful fallback is the point of using OSC.
"""

import os
import re
import sys
import time

from ansi import out, raw_input_mode, cursor_pos  # reuse the console plumbing

ESC = "\x1b"
ST = "\x1b\\"

PAUSE = 1.4
FAST = False


def pause(mult=1.0):
    if not FAST:
        time.sleep(PAUSE * mult)


def title(text):
    print(f"\n{ESC}[1;4;36m{text}{ESC}[0m")


def note(text):
    print(f"{ESC}[2m  expect: {text}{ESC}[0m")


# --------------------------------------------------------------- sequence helpers


def yt(payload):
    """Wrap a YT payload in a full OSC: ESC ] YT ; <payload> ST."""
    return ESC + "]YT;" + payload + ST


def gradient(*stops, **opts):
    """Begin-gradient sequence from colour stops and key:value options."""
    parts = [str(s) for s in stops]
    parts += [f"{k}:{v}" for k, v in opts.items()]
    return yt("gradient;" + ";".join(parts))


GRAD_OFF = yt("gradient;off")


def gtext(text, *stops, **opts):
    """A run of text wrapped in a gradient that turns itself off afterwards."""
    return gradient(*stops, **opts) + text + GRAD_OFF


# --------------------------------------------------------------- handshake


def _read_osc(timeout=0.5):
    """Read one OSC reply (terminated by ST or BEL). `raw_input_mode` must be
    active. Unlike ansi.py's CSI reader this can't stop at the first letter —
    the YT reply is full of them — so it reads until the terminator."""
    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        if os.name == "nt":
            import msvcrt

            while msvcrt.kbhit():
                buf += msvcrt.getwch()
                if buf.endswith(ST) or buf.endswith("\x07"):
                    return buf
            time.sleep(0.005)
        else:
            import select

            if select.select([sys.stdin], [], [], 0.05)[0]:
                buf += os.read(sys.stdin.fileno(), 64).decode("utf-8", "replace")
                if buf.endswith(ST) or buf.endswith("\x07"):
                    return buf
    return buf


def check_handshake():
    """Ask the terminal whether it's YoTerm and what it can do."""
    title("Capability handshake (YT;?)")
    with raw_input_mode():
        out(yt("?"))
        reply = _read_osc()

    m = re.search(r"YT;version:(\d+);feat:([^\x1b\x07]*)", reply)
    if m:
        version, feats = m.group(1), m.group(2)
        print(
            f"  {ESC}[32mPASS{ESC}[0m  YoTerm detected — "
            f"version {version}, features: {feats}"
        )
        return True
    print(
        f"  {ESC}[33m----{ESC}[0m  no YT reply: this isn't YoTerm (or the "
        f"terminal ate the query). Gradients will show as plain text below."
    )
    return False


# --------------------------------------------------------------- gradient demos


def demo_gradient_basics():
    title("Static gradients — smooth across the whole run, sub-cell")
    note("colour changes *inside* each glyph, not per cell")
    out("  " + gtext("SGR codes  33 -> 31 (yellow to red)", 33, 31) + "\n")
    out("  " + gtext("hex        #00aaff -> #ff00aa", "#00aaff", "#ff00aa") + "\n")
    out(
        "  "
        + gtext(
            "names      red -> orange -> yellow -> lime",
            "red",
            "orange",
            "yellow",
            "lime",
        )
        + "\n"
    )
    out(
        "  "
        + gtext(
            "multi-stop #ffcc00 -> #ff0000 -> #7700ff", "#ffcc00", "#ff0000", "#7700ff"
        )
        + "\n"
    )
    pause(1.6)


def demo_gradient_inline():
    title("Inline — a gradient run composes inside ordinary text")
    note("only the wrapped word is gradient; the rest stays your normal colour")
    out(
        "  the quick brown "
        + gtext("GRADIENT", "#39b54a", "#2cb5e9")
        + " jumps over the lazy dog\n"
    )
    out(
        "  status: "
        + gtext("OK", "lime", "green")
        + "  |  "
        + gtext("DANGER", "#ff8800", "#ff0000")
        + "  |  normal again\n"
    )
    pause(1.6)


def demo_gradient_angles():
    title("Angles — a single run can span several rows")
    note("angle:90 runs the ramp top->bottom across the whole block")
    out(gradient("#00e5ff", "#7c4dff", angle=90))
    for line in (
        "  +----------------------------+",
        "  |  one gradient run,         |",
        "  |  five rows tall,           |",
        "  |  shaded top to bottom      |",
        "  +----------------------------+",
    ):
        out(line + "\n")
    out(GRAD_OFF)
    pause(0.4)

    note("angle:45 is diagonal across the block")
    out(gradient("#ff0000", "#0033cc", angle=45))
    for _ in range(4):
        out("  " + "diagonal " * 4 + "\n")
    out(GRAD_OFF)
    pause(1.6)


def demo_gradient_cycle():
    title("Cycling — animated ramp (cycle:on)")
    note("the colours slide along the text; watch for a few seconds")
    out(
        "  "
        + gradient("#ff0055", "#7700ff", "#00e5ff", cycle="on", speed=1.5)
        + "Y O T E R M   G R A D I E N T   C Y C L E"
        + GRAD_OFF
        + "\n"
    )
    out(
        "  "
        + gradient(33, 31, cycle="on", speed=0.8)
        + "slower two-colour cycle"
        + GRAD_OFF
        + "\n"
    )
    # Hold so the animation is actually visible (it only moves in YoTerm).
    if not FAST:
        time.sleep(4.0)
    print()


def demo_gradient_reset():
    title("Reset behaviour")
    note("both YT;gradient;off and a plain ESC[0m end the gradient")
    out(
        "  "
        + gradient("#00ffcc", "#ff00cc")
        + "gradient..."
        + ESC
        + "[0m"
        + " ...ended by ESC[0m\n"
    )
    out(
        "  "
        + gtext("gradient...", "#00ffcc", "#ff00cc")
        + " ...ended by YT;gradient;off\n"
    )
    pause(1.6)


# --------------------------------------------------------------- images (pending)


def demo_images():
    title("Images (YT;img) — real GPU-sampled, no half-block fakery")
    # A real photo shipped next to this script; the terminal decodes and
    # uploads it — the demo just names the file.
    cat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cat.png")
    if not os.path.exists(cat):
        print(
            f"  {ESC}[2m(cat.png not found next to this script; skipping "
            f"the image demo){ESC}[0m"
        )
        return

    note("a real photo, sized in cells; the aspect is preserved (no distortion)")
    out(yt(f"img;path:{cat};cols:20"))
    pause(0.6)

    note("the same cat shrunk to an inline icon inside a line of text")
    out(
        "  here is a cat "
        + yt(f"img;path:{cat};cols:2;inline:on")
        + " sitting in the sentence\n"
    )
    pause(0.4)

    note("place a copy by id, then delete it")
    out(yt(f"img;path:{cat};cols:10;id:42"))
    out("\n")
    out(yt("img;del;id:42"))
    print(
        f"  {ESC}[2m(placed cat id:42, then removed it — nothing should "
        f"remain above){ESC}[0m"
    )
    pause(1.6)


# --------------------------------------------------------------- zones


def zone(payload):
    return yt("zone;" + payload)


def demo_zones():
    title("Zones (YT;zone) — styled rectangles the GPU draws")
    note("a zone is just a rectangle; text prints on top of it")

    # A zone's `y` is an absolute screen row, not "wherever the cursor is", so
    # print the rows first and then ask the terminal where they landed. That
    # also proves the ordering doesn't matter: a z<=0 zone draws behind text
    # whether it was created before or after the text was printed.
    print()
    print("        Install Package")
    print()
    pos = cursor_pos()
    if pos is None:
        print(f"  {ESC}[2m(no ESC[6n reply — skipping the zone demo){ESC}[0m")
        return
    top = pos[0] - 1 - 3  # 0-based row of the first of those 3 rows

    out(zone(f"create;id:1;x:6;y:{top};w:28;h:3;bg:#3366ff"))
    pause(1.2)

    note("z orders zones; at equal z the TEXT wins, so a button just works")
    out(zone(f"create;id:2;x:26;y:{top};w:16;h:3;bg:#c4404a;z:-1"))
    print(
        f"  {ESC}[2m  ^ the red zone is z:-1, so the blue z:0 zone covers it "
        f"where they overlap{ESC}[0m"
    )
    pause(1.6)

    note("update is a patch — only the fields you name change")
    out(zone("update;id:2;bg:#22c55e"))  # recolour, geometry untouched
    pause(1.0)
    out(zone("move;id:2;x:30"))  # slide it right
    pause(1.2)

    note("z:1 puts a zone ABOVE the text — modals, tooltips, overlays")
    print()
    print("        this line gets covered by an overlay")
    print()
    pos = cursor_pos()
    over = pos[0] - 1 - 3
    out(zone(f"create;id:3;x:6;y:{over};w:44;h:3;bg:#7700ff;z:1;opacity:0.8"))
    pause(1.8)

    note(
        "radius/border round the still-live button — the SDF is a fragment, "
        "not a fake"
    )
    out(zone("update;id:1;radius:10;border:#88bbff;border_w:2"))
    pause(1.8)


def demo_zone_gradients():
    title("Zone gradients — any number of stops, sampled per fragment")
    note("a shared gradient-ramp texture, not vertex colours: any stop count works")

    for _ in range(6):
        print()
    pos = cursor_pos()
    if pos is None:
        print(f"  {ESC}[2m(no ESC[6n reply — skipping){ESC}[0m")
        return
    top = pos[0] - 1 - 6

    out(zone(f"create;id:10;x:2;y:{top};w:20;h:5;gradient:#00aaff,#ff00aa"))
    out(
        zone(
            f"create;id:11;x:24;y:{top};w:20;h:5;radius:14;"
            "gradient:#ffcc00,#ff0000,#7700ff;angle:20"
        )
    )
    pause(1.8)

    note("a real UI card: 6-stop gradient, radius, border — still one YT;zone")
    for _ in range(5):
        print()
    print("              Python")
    print()
    pos = cursor_pos()
    if pos is None:
        return
    card_top = pos[0] - 1 - 3
    out(
        zone(
            f"create;id:12;x:4;y:{card_top};w:24;h:4;radius:4;"
            "border:#3e0d90;border_w:1;"
            "gradient:#3d0d8f@0.0,#4e24c3@0.35,#6542ff@0.55,#ac42ff@0.75,"
            "#f74792@0.88,#fec723@1.0;angle:91"
        )
    )
    pause(0.4)


def demo_zone_shadows():
    title("Zone shadows — soft-edge falloff, one SDF pass, no blurred texture")
    note("shadow bleeds past the zone's own box; it's not clipped to it")

    for _ in range(6):
        print()
    pos = cursor_pos()
    if pos is None:
        print(f"  {ESC}[2m(no ESC[6n reply — skipping){ESC}[0m")
        return
    top = pos[0] - 1 - 6

    out(zone(f"create;id:13;x:2;y:{top};w:16;h:5;bg:#3366ff;radius:10;shadow:14"))
    out(zone(f"create;id:14;x:22;y:{top};w:16;h:5;bg:#222233;radius:8;"
             "border:#ffcc00;border_w:3;shadow:10"))
    pause(1.6)

    note("opacity applies to the WHOLE zone — including its own shadow")
    out(zone(f"create;id:15;x:42;y:{top};w:16;h:5;bg:#22c55e;radius:8;"
             "shadow:14;opacity:0.4"))
    print(f"  {ESC}[2m  ^ half as opaque, and its shadow fades to match — not "
          f"a fixed-strength shadow underneath{ESC}[0m")
    pause(1.8)

    note("shadow with no bg/gradient at all: a soft shape, nothing solid on it")
    for _ in range(6):
        print()
    pos = cursor_pos()
    if pos is None:
        return
    bottom = pos[0] - 1 - 6
    out(zone(f"create;id:16;x:2;y:{bottom};w:16;h:5;radius:8;shadow:16"))
    pause(1.6)


# --------------------------------------------------------------- entry


def main():
    global FAST
    args = set(sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    FAST = "--fast" in args

    print(f"{ESC}[1mYoTerm YT-sequence demo{ESC}[0m")
    ok = check_handshake()
    if "--check" in args:
        return 0 if ok else 1

    demo_gradient_basics()
    demo_gradient_inline()
    demo_gradient_angles()
    demo_gradient_cycle()
    demo_gradient_reset()
    demo_images()
    demo_zones()
    demo_zone_gradients()
    demo_zone_shadows()

    print(f"\n{ESC}[1;32mDemo done.{ESC}[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
