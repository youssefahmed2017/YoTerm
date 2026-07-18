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

from ansi import out, raw_input_mode  # reuse the console plumbing

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
        print(f"  {ESC}[32mPASS{ESC}[0m  YoTerm detected — "
              f"version {version}, features: {feats}")
        return True
    print(f"  {ESC}[33m----{ESC}[0m  no YT reply: this isn't YoTerm (or the "
          f"terminal ate the query). Gradients will show as plain text below.")
    return False


# --------------------------------------------------------------- gradient demos

def demo_gradient_basics():
    title("Static gradients — smooth across the whole run, sub-cell")
    note("colour changes *inside* each glyph, not per cell")
    out("  " + gtext("SGR codes  33 -> 31 (yellow to red)", 33, 31) + "\n")
    out("  " + gtext("hex        #00aaff -> #ff00aa", "#00aaff", "#ff00aa") + "\n")
    out("  " + gtext("names      red -> orange -> yellow -> lime", "red", "orange",
                     "yellow", "lime") + "\n")
    out("  " + gtext("multi-stop #ffcc00 -> #ff0000 -> #7700ff", "#ffcc00",
                     "#ff0000", "#7700ff") + "\n")
    pause(1.6)


def demo_gradient_inline():
    title("Inline — a gradient run composes inside ordinary text")
    note("only the wrapped word is gradient; the rest stays your normal colour")
    out("  the quick brown " + gtext("GRADIENT", "#39b54a", "#2cb5e9")
        + " jumps over the lazy dog\n")
    out("  status: " + gtext("OK", "lime", "green") + "  |  "
        + gtext("DANGER", "#ff8800", "#ff0000") + "  |  normal again\n")
    pause(1.6)


def demo_gradient_angles():
    title("Angles — a single run can span several rows")
    note("angle:90 runs the ramp top->bottom across the whole block")
    out(gradient("#00e5ff", "#7c4dff", angle=90))
    for line in ("  +----------------------------+",
                 "  |  one gradient run,         |",
                 "  |  five rows tall,           |",
                 "  |  shaded top to bottom      |",
                 "  +----------------------------+"):
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
    out("  " + gradient("#ff0055", "#7700ff", "#00e5ff", cycle="on", speed=1.5)
        + "Y O T E R M   G R A D I E N T   C Y C L E" + GRAD_OFF + "\n")
    out("  " + gradient(33, 31, cycle="on", speed=0.8)
        + "slower two-colour cycle" + GRAD_OFF + "\n")
    # Hold so the animation is actually visible (it only moves in YoTerm).
    if not FAST:
        time.sleep(4.0)
    print()


def demo_gradient_reset():
    title("Reset behaviour")
    note("both YT;gradient;off and a plain ESC[0m end the gradient")
    out("  " + gradient("#00ffcc", "#ff00cc") + "gradient..."
        + ESC + "[0m" + " ...ended by ESC[0m\n")
    out("  " + gtext("gradient...", "#00ffcc", "#ff00cc")
        + " ...ended by YT;gradient;off\n")
    pause(1.6)


# --------------------------------------------------------------- images (pending)

def demo_images():
    title("Images (YT;img) — real GPU-sampled, no half-block fakery")
    # A real photo shipped next to this script; the terminal decodes and
    # uploads it — the demo just names the file.
    cat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cat.png")
    if not os.path.exists(cat):
        print(f"  {ESC}[2m(cat.png not found next to this script; skipping "
              f"the image demo){ESC}[0m")
        return

    note("a real photo, sized in cells; the aspect is preserved (no distortion)")
    out(yt(f"img;path:{cat};cols:20"))
    pause(0.6)

    note("the same cat shrunk to an inline icon inside a line of text")
    out("  here is a cat " + yt(f"img;path:{cat};cols:2;inline:on")
        + " sitting in the sentence\n")
    pause(0.4)

    note("place a copy by id, then delete it")
    out(yt(f"img;path:{cat};cols:10;id:42"))
    out("\n")
    out(yt("img;del;id:42"))
    print(f"  {ESC}[2m(placed cat id:42, then removed it — nothing should "
          f"remain above){ESC}[0m")
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

    print(f"\n{ESC}[1;32mDemo done.{ESC}[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
