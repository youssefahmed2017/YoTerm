"""YoTerm ANSI / VT100 / VT220 conformance suite.

Run this *inside* YoTerm (or any terminal) to exercise what it supports:

    python ansi.py            # automatic checks, then the visual suite
    python ansi.py --auto     # only the automatic checks
    python ansi.py --visual   # only the visual suite

The visual suite is paced so you can actually watch it — blocks that erase or
swap the screen draw themselves first, hold, and only then destroy what they
drew. Control the speed:

    --step        wait for Enter between blocks (go at your own pace)
    --slow        2.5s per block
    --pause=N     N seconds per block
    --fast        no pauses at all

The automatic checks work by asking the terminal itself where the cursor
ended up (DSR, `ESC[6n`) after each movement, so cursor positioning, scroll
regions, origin mode, tabs and auto-wrap are genuinely *asserted* rather than
eyeballed. They run inside the alternate screen buffer so they don't scribble
over the report.

Colors, attributes and the erase/insert/delete family can't report themselves,
so those live in the visual suite: each block prints what it should look like
next to what the terminal actually drew.

Checks marked `optional` cover sequences YoTerm hasn't implemented yet. They
report as GAP, not FAIL, so a clean run stays clean while still telling you
what's missing.
"""

import math
import os
import re
import sys
import time

ESC = "\x1b"
CSI = "\x1b["


def out(s):
    """Write exactly these bytes to the terminal.

    Python's *text* stdout translates '\\n' to '\\r\\n' on Windows, which would
    quietly turn a bare-LF test into a CRLF test — the sequence under test
    would never reach the terminal at all. Going through the binary buffer
    keeps what we send identical to what we wrote.
    """
    raw = getattr(sys.stdout, "buffer", None)
    if raw is None:               # a redirected/wrapped stdout: nothing to fix
        sys.stdout.write(s)
        sys.stdout.flush()
        return
    sys.stdout.flush()            # keep our bytes ordered against print()
    raw.write(s.encode("utf-8", "replace"))
    raw.flush()


def term_size():
    try:
        size = os.get_terminal_size()
        return size.columns, size.lines
    except OSError:
        return 80, 24


# --------------------------------------------------------------- reply reading

# Windows console input flags (winbase.h).
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

_raw_depth = 0
_raw_saved = None


class raw_input_mode:
    """Put the console where terminal replies can actually be read.

    A reply like `ESC[12;3R` arrives through the same channel as typed keys.
    On Windows the console normally digests input into key *events*, which
    mangles a VT reply into garbage (`ESC[6n` comes back as '\\x00') — only
    ENABLE_VIRTUAL_TERMINAL_INPUT keeps it as raw bytes. On POSIX, cbreak is
    enough to defeat line buffering.

    Nesting is refcounted so the mode can stay on across a whole batch of
    queries: if it flipped back to cooked between them, a reply landing in
    that window would be mangled before we could read it.
    """

    def __enter__(self):
        global _raw_depth, _raw_saved
        _raw_depth += 1
        if _raw_depth > 1:
            return self
        _raw_saved = None
        try:
            if os.name == "nt":
                import ctypes

                k32 = ctypes.windll.kernel32
                handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
                mode = ctypes.c_uint32()
                if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                    _raw_saved = (k32, handle, mode.value)
                    k32.SetConsoleMode(
                        handle,
                        (
                            mode.value
                            & ~(
                                _ENABLE_PROCESSED_INPUT
                                | _ENABLE_LINE_INPUT
                                | _ENABLE_ECHO_INPUT
                            )
                        )
                        | _ENABLE_VIRTUAL_TERMINAL_INPUT,
                    )
            else:
                import termios
                import tty

                fd = sys.stdin.fileno()
                _raw_saved = (fd, termios.tcgetattr(fd))
                tty.setcbreak(fd)
        except Exception:
            _raw_saved = None  # not a real console; queries will just fail
        return self

    def __exit__(self, *exc):
        global _raw_depth, _raw_saved
        _raw_depth -= 1
        if _raw_depth > 0 or _raw_saved is None:
            return False
        try:
            if os.name == "nt":
                k32, handle, mode = _raw_saved
                k32.SetConsoleMode(handle, mode)
            else:
                import termios

                fd, saved = _raw_saved
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:
            pass
        _raw_saved = None
        return False


def _read_reply(timeout=0.4):
    """Read one terminal reply. `raw_input_mode` must already be active."""
    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        if os.name == "nt":
            import msvcrt

            while msvcrt.kbhit():
                buf += msvcrt.getwch()
                if len(buf) > 2 and buf[-1].isalpha():
                    return buf
            time.sleep(0.005)
        else:
            import select

            fd = sys.stdin.fileno()
            if select.select([fd], [], [], 0.05)[0]:
                buf += os.read(fd, 32).decode("utf-8", "replace")
                if len(buf) > 2 and buf[-1].isalpha():
                    return buf
    return buf


def cursor_pos():
    """Ask the terminal for the cursor position. Returns (row, col) or None."""
    with raw_input_mode():
        out(CSI + "6n")
        # A reply can carry leading noise, so search rather than match.
        m = re.search(r"\x1b\[(\d+);(\d+)R", _read_reply())
    return (int(m.group(1)), int(m.group(2))) if m else None


def device_status():
    with raw_input_mode():
        out(CSI + "5n")
        reply = _read_reply()
    m = re.search(r"\x1b\[\d+n", reply)
    return m.group(0) if m else reply


def _restore_terminal():
    """Leave the terminal exactly as we found it.

    Both ESC[r (DECSTBM) and ESC[?6l (DECOM) *home the cursor* as a documented
    side effect, so the whole cleanup has to sit inside DECSC/DECRC. Without
    that the script exits with the cursor at row 1 and the shell paints its
    next prompt there, on top of our output.
    """
    out(CSI + "?1049l")  # leave the alt screen first: it restores its own
    out(ESC + "7")  # cursor, which we then save
    out(CSI + "0m" + CSI + "?6l" + CSI + "?7h" + CSI + "?25h" + CSI + "r")
    # Character sets are global, not part of the saved cursor state, so a test
    # that redesignated G0-G3 or left GL shifted would hand the shell a
    # terminal that draws the wrong glyphs. Put all four back to ASCII, lock G0
    # into GL (SI), and clear any single shift with a soft reset's worth of
    # designation. This is belt-and-suspenders: the tests also clean up after
    # themselves, but the restore must be correct even if one forgot.
    out(ESC + "(B" + ESC + ")B" + ESC + "*B" + ESC + "+B" + "\x0f")
    out(ESC + "8")  # put the cursor back where the output ended


# ------------------------------------------------------------------ harness

PASS, FAIL, GAP, SKIP = "pass", "fail", "gap", "skip"
results = []


def check(name, got, want, optional=False):
    if got == want:
        status = PASS
    elif optional:
        status = GAP
    else:
        status = FAIL
    results.append((status, name, got, want))
    return status == PASS


def skip(name, why):
    """Record a check the *platform* won't let us make.

    Different from GAP: a gap is something YoTerm doesn't do, a skip is
    something this OS won't let us observe, so the result would say nothing
    about YoTerm either way.
    """
    results.append((SKIP, name, why, None))


def section(title):
    results.append(("section", title, None, None))


def report():
    """Print the collected results. Runs on the primary screen."""
    green, red, yellow, dim, reset = (
        CSI + "32m",
        CSI + "31m",
        CSI + "33m",
        CSI + "2m",
        CSI + "0m",
    )
    counts = {PASS: 0, FAIL: 0, GAP: 0, SKIP: 0}

    for status, name, got, want in results:
        if status == "section":
            print(f"\n{CSI}1;36m-- {name} --{reset}")
            continue
        counts[status] += 1
        if status == PASS:
            print(f"  {green}PASS{reset}  {name}")
        elif status == SKIP:
            print(f"  {dim}SKIP  {name}  ({got}){reset}")
        elif status == GAP:
            print(
                f"  {yellow}GAP {reset}  {name}  {dim}(got {got!r}, "
                f"want {want!r} — not implemented){reset}"
            )
        else:
            print(
                f"  {red}FAIL{reset}  {name}  {dim}(got {got!r}, "
                f"want {want!r}){reset}"
            )

    ran = counts[PASS] + counts[FAIL] + counts[GAP]
    print(
        f"\n{CSI}1m{counts[PASS]}/{ran} passed{reset}"
        f"  {red}{counts[FAIL]} failed{reset}"
        f"  {yellow}{counts[GAP]} not implemented{reset}"
        f"  {dim}{counts[SKIP]} skipped{reset}\n"
    )
    return counts[FAIL] == 0


# =========================================================================
# Automatic checks (cursor reports back, so we can assert)
# =========================================================================


def auto_cursor(cols, rows):
    section("Cursor positioning (CUP/CUU/CUD/CUF/CUB/CHA/VPA/CNL/CPL)")

    out(CSI + "5;10H")
    check("CUP  ESC[5;10H -> row 5 col 10", cursor_pos(), (5, 10))

    out(CSI + "2A")
    check("CUU  ESC[2A moves up 2", cursor_pos(), (3, 10))

    out(CSI + "4B")
    check("CUD  ESC[4B moves down 4", cursor_pos(), (7, 10))

    out(CSI + "3C")
    check("CUF  ESC[3C moves right 3", cursor_pos(), (7, 13))

    out(CSI + "5D")
    check("CUB  ESC[5D moves left 5", cursor_pos(), (7, 8))

    out(CSI + "A")
    check("CUU  ESC[A (no param) defaults to 1", cursor_pos(), (6, 8))

    out(CSI + "0C")
    check("CUF  ESC[0C treats 0 as 1", cursor_pos(), (6, 9))

    out(CSI + "7G")
    check("CHA  ESC[7G -> column 7", cursor_pos(), (6, 7))

    out(CSI + "4d")
    check("VPA  ESC[4d -> row 4", cursor_pos(), (4, 7))

    out(CSI + "12`")
    check("HPA  ESC[12` -> column 12", cursor_pos(), (4, 12))

    out(CSI + "2E")
    check("CNL  ESC[2E -> 2 down, column 1", cursor_pos(), (6, 1))

    out(CSI + "10;5H" + CSI + "3F")
    check("CPL  ESC[3F -> 3 up, column 1", cursor_pos(), (7, 1))

    out(CSI + "H")
    check("CUP  ESC[H (no params) homes", cursor_pos(), (1, 1))

    out(CSI + "8;4f")
    check("HVP  ESC[8;4f is CUP's twin", cursor_pos(), (8, 4))

    section("Cursor clamping (must never leave the screen)")

    out(CSI + "999;999H")
    check("CUP past the edge clamps to the last cell", cursor_pos(), (rows, cols))

    out(CSI + "1;1H" + CSI + "99A")
    check("CUU at the top stays on row 1", cursor_pos(), (1, 1))

    out(CSI + "1;1H" + CSI + "99D")
    check("CUB at the left edge stays on column 1", cursor_pos(), (1, 1))

    out(CSI + "999B")
    check("CUD at the bottom stays on the last row", cursor_pos(), (rows, 1))

    out(CSI + "999C")
    check("CUF at the right edge stays on the last column", cursor_pos(), (rows, cols))


def auto_save_restore(rows):
    section("Save / restore cursor (DECSC/DECRC, SCOSC/SCORC)")

    out(CSI + "9;20H" + CSI + "s")  # save
    out(CSI + "1;1H")  # wander off
    out(CSI + "u")  # restore
    check("ESC[s / ESC[u round-trips", cursor_pos(), (9, 20))

    out(CSI + "6;12H" + ESC + "7")  # DECSC
    out(CSI + "20;40H")
    out(ESC + "8")  # DECRC
    check("ESC 7 / ESC 8 round-trips", cursor_pos(), (6, 12))

    # DECSC saves the whole drawing state, not just the position: origin mode
    # rides along with it.
    #
    # Probe that with *confinement*, not a plain CUP. In origin mode DSR
    # reports rows relative to the region, so 'CUP 1;1' answers (1,1) whether
    # or not origin mode came back — that test could never fail. Running off
    # the bottom can only be confined if origin mode is actually on.
    out(CSI + "3;8r" + CSI + "?6h")
    out(ESC + "7")             # save while origin mode is on
    out(CSI + "?6l")           # turn it off
    out(ESC + "8")             # DECRC should bring origin mode back with it
    out(CSI + "99;1H")
    check("DECRC restores origin mode along with the position",
          cursor_pos(), (6, 1))         # region 3..8 is 6 rows tall
    out(CSI + "?6l" + CSI + "r")

    # SCORC is the ANSI.SYS variant and only deals in the position, so it must
    # NOT drag origin mode along the way DECRC does.
    out(CSI + "3;8r" + CSI + "?6l")
    out(CSI + "5;1H" + CSI + "s")   # save position with origin mode off
    out(CSI + "?6h")                # turn it on
    out(CSI + "u")                  # SCORC: position only
    out(CSI + "99;1H")
    check("SCORC restores the position without touching origin mode",
          cursor_pos(), (6, 1))         # still confined: origin mode stayed on
    out(CSI + "?6l" + CSI + "r")

    # With nothing ever saved, a VT homes the cursor.
    out(CSI + "12;30H")
    check("ESC 7 / ESC 8 leave the cursor put when nothing moved it",
          cursor_pos(), (12, 30))


def auto_scroll_region(rows):
    section("Scroll region (DECSTBM) and origin mode (DECOM)")

    out(CSI + "3;8r")
    check("DECSTBM ESC[3;8r homes the cursor", cursor_pos(), (1, 1))

    out(CSI + "?6h")
    check("DECOM on homes to the region top", cursor_pos(), (1, 1))

    out(CSI + "1;1H")
    check("DECOM: CUP 1;1 is the region top (reported relative)", cursor_pos(), (1, 1))

    out(CSI + "3;5H")
    check("DECOM: CUP 3;5 is relative to the region", cursor_pos(), (3, 5))

    out(CSI + "99;1H")
    check(
        "DECOM: CUP past the region bottom is confined", cursor_pos(), (6, 1)
    )  # region 3..8 is 6 rows tall

    out(CSI + "?6l")
    check("DECOM off homes to the screen top", cursor_pos(), (1, 1))

    out(CSI + "3;5H")
    check("DECOM off: CUP is absolute again", cursor_pos(), (3, 5))

    out(CSI + "r")
    check("DECSTBM ESC[r resets to the full screen and homes", cursor_pos(), (1, 1))

    # With the region reset, the bottom row must still scroll normally.
    out(CSI + "%d;1H" % rows)
    out("\n")
    check("LF on the last row scrolls instead of moving down", cursor_pos(), (rows, 1))


def auto_controls(cols):
    section("C0 controls (BS, CR, HT, LF) and auto-wrap")

    out(CSI + "3;5H\b")
    check("BS  backspace moves left one column", cursor_pos(), (3, 4))

    out(CSI + "3;1H\b")
    check("BS  at column 1 does nothing", cursor_pos(), (3, 1))

    out(CSI + "4;9H\r")
    check("CR  returns to column 1", cursor_pos(), (4, 1))

    out(CSI + "5;1H\t")
    check("HT  from column 1 -> column 9", cursor_pos(), (5, 9))

    out(CSI + "5;10H\t")
    check("HT  from column 10 -> column 17", cursor_pos(), (5, 17))

    out(CSI + "6;1H\n")
    check("LF  moves down, keeping the column", cursor_pos(), (7, 1))

    # Deferred auto-wrap: a glyph in the last column parks the cursor there;
    # the wrap only happens when the *next* glyph arrives. Wrapping eagerly is
    # the classic "extra blank line" bug.
    out(CSI + "10;%dH" % cols + "X")
    check(
        "Auto-wrap is deferred: glyph in the last column parks there",
        cursor_pos(),
        (10, cols),
    )

    out("Y")
    check("Auto-wrap fires on the next glyph", cursor_pos(), (11, 2))

    # A CR/LF after a last-column glyph must not produce a second newline.
    out(CSI + "13;%dH" % cols + "X" + "\r\n")
    check("CR/LF after a last-column glyph adds no extra line", cursor_pos(), (14, 1))


def auto_unicode():
    section("Unicode widths (the terminal and the app must agree)")

    out(CSI + "2;1H" + "abc")
    check("Narrow ASCII advances 1 column each", cursor_pos(), (2, 4))

    out(CSI + "3;1H" + "漢")  # 漢 (CJK, wide)
    check("CJK ideograph advances 2 columns", cursor_pos(), (3, 3))

    out(CSI + "4;1H" + "\U0001f600")  # 😀 (emoji, wide)
    check("Emoji advances 2 columns", cursor_pos(), (4, 3))

    out(CSI + "5;1H" + "é")  # e + combining acute
    check("Combining mark advances 0 columns", cursor_pos(), (5, 2))

    out(CSI + "6;1H" + "✓✗")  # ✓ ✗ — narrow, not emoji-wide
    check("Dingbats stay 1 column wide", cursor_pos(), (6, 3))


def auto_reports():
    section("Device status reports (DSR)")

    check("DSR ESC[5n replies ESC[0n (terminal OK)", device_status(), CSI + "0n")

    out(CSI + "11;22H")
    check("DSR ESC[6n reports the cursor position", cursor_pos(), (11, 22))


def auto_index():
    section("Index / reverse index (IND, RI, NEL)")

    out(CSI + "4;30H" + ESC + "D")
    check("IND ESC D moves down one row, keeping the column", cursor_pos(), (5, 30))

    out(CSI + "6;30H" + ESC + "M")
    check("RI  ESC M moves up one row, keeping the column", cursor_pos(), (5, 30))

    out(CSI + "7;30H" + ESC + "E")
    check("NEL ESC E moves down and returns to column 1", cursor_pos(), (8, 1))

    # The margin cases are the ones full-screen apps actually lean on: at a
    # margin these scroll the region instead of moving the cursor.
    out(CSI + "3;8r")  # region = rows 3..8
    out(CSI + "3;5H" + ESC + "M")
    check(
        "RI  at the top margin scrolls the region down, cursor stays",
        cursor_pos(),
        (3, 5),
    )

    out(CSI + "8;5H" + ESC + "D")
    check(
        "IND at the bottom margin scrolls the region up, cursor stays",
        cursor_pos(),
        (8, 5),
    )
    out(CSI + "r")


def auto_tabs(cols):
    section("Tab stops (HTS, TBC)")

    out(CSI + "5;1H\t")
    check("HT uses the default stops every 8 columns", cursor_pos(), (5, 9))

    out(CSI + "3g")  # TBC 3: clear every stop
    out(CSI + "6;20H" + ESC + "H")  # HTS: set one at column 20
    out(CSI + "6;1H\t")
    check("HTS ESC H sets a stop and HT jumps to it", cursor_pos(), (6, 20))

    out(CSI + "6;20H" + CSI + "0g")  # TBC 0: clear it again
    out(CSI + "6;1H\t")
    check(
        "TBC ESC[0g clears the stop at the cursor (HT falls to the edge)",
        cursor_pos(),
        (6, cols),
    )

    # Put the defaults back — tab stops are global, and the visual suite's
    # tab section expects them.
    for col in range(9, cols + 1, 8):
        out(CSI + "6;%dH" % col + ESC + "H")
    out(CSI + "5;1H\t")
    check("default tab stops restored", cursor_pos(), (5, 9))


def auto_rep():
    section("Repeat (REP)")

    out(CSI + "2;1H" + "A" + CSI + "3b")
    check("REP ESC[3b repeats the previous glyph 3 more times", cursor_pos(), (2, 5))

    out(CSI + "3;1H" + "-" + CSI + "9b")
    check("REP ESC[9b repeats 9 more times", cursor_pos(), (3, 11))


def auto_autowrap(cols):
    section("Auto-wrap mode (DECAWM)")

    out(CSI + "?7l")  # DECAWM off
    out(CSI + "1;%dH" % cols + "AB")
    check("DECAWM off: the cursor sticks in the last column", cursor_pos(), (1, cols))

    out(CSI + "?7h")  # DECAWM back on
    out(CSI + "3;%dH" % cols + "A")
    check(
        "DECAWM on: a last-column glyph still parks (deferred wrap)",
        cursor_pos(),
        (3, cols),
    )
    out("B")
    check("DECAWM on: the next glyph wraps to the following line", cursor_pos(), (4, 2))


def auto_cursor_tabs(cols):
    section("Cursor tabulation (CHT, CBT)")

    out(CSI + "5;1H" + CSI + "3I")  # CHT: tab forward 3 stops
    check("CHT ESC[3I tabs forward 3 stops", cursor_pos(), (5, 25))

    out(CSI + "5;30H" + CSI + "Z")  # CBT: tab backward one stop
    check("CBT ESC[Z tabs backward one stop", cursor_pos(), (5, 25))

    out(CSI + "5;1H" + CSI + "I")
    check("CHT ESC[I (no param) defaults to one stop", cursor_pos(), (5, 9))

    out(CSI + "5;30H" + CSI + "3Z")
    check("CBT ESC[3Z tabs backward 3 stops", cursor_pos(), (5, 9))

    out(CSI + "5;1H" + CSI + "Z")
    check("CBT at column 1 stays put", cursor_pos(), (5, 1))

    out(CSI + "5;1H" + CSI + "999I")
    check("CHT past the last stop clamps to the last column", cursor_pos(), (5, cols))


def auto_device_attrs():
    section("Device attributes (DA)")

    # Apps ask what sort of terminal they're talking to before enabling
    # features. xterm answers ESC[?1;2c: "VT100 with advanced video".
    with raw_input_mode():
        out(CSI + "c")
        primary = _read_reply()
    m = re.search(r"\x1b\[\?[\d;]+c", primary)
    check(
        "DA1 ESC[c reports primary device attributes",
        m.group(0) if m else None,
        "\x1b[?1;2c",
    )

    with raw_input_mode():
        out(CSI + "0c")  # ESC[0c is a synonym for ESC[c
        primary0 = _read_reply()
    m = re.search(r"\x1b\[\?[\d;]+c", primary0)
    check("DA1 ESC[0c is a synonym for ESC[c", m.group(0) if m else None, "\x1b[?1;2c")

    with raw_input_mode():
        out(CSI + ">c")  # DA2: secondary attributes
        secondary = _read_reply()
    m = re.search(r"\x1b\[>[\d;]+c", secondary)
    check(
        "DA2 ESC[>c reports secondary device attributes (not DA1's reply)",
        m.group(0) if m else None,
        "\x1b[>0;10;1c",
    )


def _mode_report(query):
    """Send a DECRQM query and return just its reply, or None."""
    with raw_input_mode():
        out(query)
        reply = _read_reply()
    m = re.search(r"\x1b\[\??\d+;\d+\$y", reply)
    return m.group(0) if m else None


def bare_lf_survives():
    """Does a bare LF actually reach the terminal, or does the OS rewrite it?

    Windows' console layer turns LF into CRLF whenever ENABLE_PROCESSED_OUTPUT
    is on — and that same flag is what makes escape sequences work at all, so
    there is no mode where a program can send both VT sequences and a bare LF.
    Inside a Windows console the LF/LNM column tests therefore measure the
    console, not the terminal: 'LNM off' fails and 'LNM on' passes no matter
    what YoTerm does. Detect it rather than assuming a platform.
    """
    out(CSI + "5;7H\n")
    return cursor_pos() == (6, 7)


LF_IS_RAW = None       # decided once, on first use


def _lf_note():
    return ("this console rewrites LF to CRLF before the terminal sees it, so "
            "the result would say nothing about YoTerm")


def auto_vt100_controls(cols, rows):
    global LF_IS_RAW
    section("VT100 C0 controls (VT, FF, SO, SI, CAN, SUB, BEL)")

    LF_IS_RAW = bare_lf_survives()

    # LF, VT and FF all index. With LNM off they keep the column — a bare LF
    # moving to column 1 would be LNM behaviour, not the default.
    for ctrl, name in (("\x0b", "VT  0x0B"), ("\x0c", "FF  0x0C")):
        out(CSI + "5;7H" + ctrl)
        check("%s indexes like LF, keeping the column" % name, cursor_pos(), (6, 7))

    if LF_IS_RAW:
        out(CSI + "5;7H\n")
        check("LF  0x0A keeps the column with LNM off", cursor_pos(), (6, 7))
    else:
        skip("LF  0x0A keeps the column with LNM off", _lf_note())

    # SO/SI only swap the character set; they must not move the cursor.
    out(CSI + "8;4H" + "\x0e")
    check("SO  0x0E doesn't move the cursor", cursor_pos(), (8, 4))
    out("\x0f")
    check("SI  0x0F doesn't move the cursor", cursor_pos(), (8, 4))

    # CAN/SUB abandon an escape sequence mid-flight: the rest of it must not be
    # treated as a command, and its bytes must not land on the screen.
    out(CSI + "10;5H" + CSI + "3" + "\x18" + "H")
    check("CAN 0x18 cancels a sequence in flight (the 'H' is just text)",
          cursor_pos(), (10, 6))
    out(CSI + "11;5H" + CSI + "3" + "\x1a" + "H")
    check("SUB 0x1A cancels a sequence in flight", cursor_pos(), (11, 6))

    out(CSI + "12;5H" + "\x07")
    check("BEL 0x07 doesn't move the cursor or print", cursor_pos(), (12, 5))
    out(CSI + "2J")


def auto_vt100_modes(cols, rows):
    section("VT100 modes (LNM, IRM, DECCKM, DECSCNM, DECCOLM)")

    out(CSI + "20h")                       # LNM on
    if LF_IS_RAW:
        out(CSI + "5;7H\n")
        check("LNM ESC[20h makes LF return to column 1", cursor_pos(), (6, 1))
    else:
        # This one would "pass" for the wrong reason: the console's own CR puts
        # the cursor in column 1 whether or not LNM did anything at all.
        skip("LNM ESC[20h makes LF return to column 1", _lf_note())
    check("DECRQM reports LNM set", _mode_report(CSI + "20$p"), "\x1b[20;1$y")
    out(CSI + "20l")
    if LF_IS_RAW:
        out(CSI + "5;7H\n")
        check("LNM off: LF keeps the column again", cursor_pos(), (6, 7))
    else:
        skip("LNM off: LF keeps the column again", _lf_note())
    check("DECRQM reports LNM reset", _mode_report(CSI + "20$p"), "\x1b[20;2$y")

    # IRM: typing pushes the rest of the line right instead of overwriting it.
    out(CSI + "2J" + CSI + "3;1H" + "ABCDEF")
    out(CSI + "4h")                        # IRM on
    out(CSI + "3;1H" + "xy")
    check("IRM ESC[4h inserts, so the cursor still advances normally",
          cursor_pos(), (3, 3))
    check("DECRQM reports IRM set", _mode_report(CSI + "4$p"), "\x1b[4;1$y")
    out(CSI + "4l")
    check("DECRQM reports IRM reset", _mode_report(CSI + "4$p"), "\x1b[4;2$y")

    out(CSI + "?1h")
    check("DECRQM reports DECCKM set (arrows send ESC O A)",
          _mode_report(CSI + "?1$p"), "\x1b[?1;1$y")
    out(CSI + "?1l")
    check("DECRQM reports DECCKM reset",
          _mode_report(CSI + "?1$p"), "\x1b[?1;2$y")

    out(CSI + "?5h")
    check("DECRQM reports DECSCNM set (reverse video)",
          _mode_report(CSI + "?5$p"), "\x1b[?5;1$y")
    out(CSI + "?5l")

    # DECCOLM's side effects are what programs depend on: region cleared,
    # screen erased, cursor homed.
    out(CSI + "3;8r" + CSI + "9;9H" + CSI + "?3h")
    check("DECCOLM ESC[?3h homes the cursor", cursor_pos(), (1, 1))
    out(CSI + "999;1H")
    check("DECCOLM drops the scroll region", cursor_pos(), (rows, 1))
    out(CSI + "?3l" + CSI + "r" + CSI + "2J")

    # Modes a CRT needed and a GPU doesn't. Accepted so a program that sets
    # them isn't told "unknown", but they do nothing.
    for mode, name in (("?4", "DECSCLM smooth scroll"), ("?8", "DECARM autorepeat"),
                       ("?9", "DECINLM interlace")):
        out(CSI + mode + "h")
        # The reply keeps the '?' — it's a private mode.
        check("%s is accepted (DECRQM: set)" % name,
              _mode_report(CSI + mode + "$p"), "\x1b[%s;1$y" % mode)
        out(CSI + mode + "l")


def auto_vt100_reports():
    section("VT100 reports (DECID, DECREQTPARM)")

    with raw_input_mode():
        out(ESC + "Z")                     # DECID
        reply = _read_reply()
    m = re.search(r"\x1b\[\?[\d;]+c", reply)
    check("DECID ESC Z identifies like DA1", m.group(0) if m else None,
          "\x1b[?1;2c")

    with raw_input_mode():
        out(CSI + "0x")                    # DECREQTPARM, request 0 -> report 2
        reply = _read_reply()
    m = re.search(r"\x1b\[[\d;]+x", reply)
    check("DECREQTPARM ESC[0x reports terminal parameters",
          m.group(0) if m else None, "\x1b[2;1;1;120;120;1;0x")

    with raw_input_mode():
        out(CSI + "1x")                    # request 1 -> report 3
        reply = _read_reply()
    m = re.search(r"\x1b\[[\d;]+x", reply)
    check("DECREQTPARM ESC[1x reports with a solicited flag of 3",
          m.group(0) if m else None, "\x1b[3;1;1;120;120;1;0x")


def auto_vt100_reset(cols, rows):
    section("VT100 reset (RIS) and line attributes")

    # RIS has to put *everything* back — that's the whole point of `reset`.
    out(CSI + "3;8r" + CSI + "?6h" + CSI + "?7l" + CSI + "4h" + CSI + "20h")
    out(CSI + "?25l" + CSI + "?5h" + CSI + "9;9H")
    out(ESC + "c")
    check("RIS ESC c homes the cursor", cursor_pos(), (1, 1))
    out(CSI + "999;1H")
    check("RIS drops the scroll region", cursor_pos(), (rows, 1))
    check("RIS restores auto-wrap", _mode_report(CSI + "?7$p"), "\x1b[?7;1$y")
    check("RIS clears origin mode", _mode_report(CSI + "?6$p"), "\x1b[?6;2$y")
    check("RIS clears insert mode", _mode_report(CSI + "4$p"), "\x1b[4;2$y")
    check("RIS clears newline mode", _mode_report(CSI + "20$p"), "\x1b[20;2$y")
    check("RIS shows the cursor again", _mode_report(CSI + "?25$p"), "\x1b[?25;1$y")
    check("RIS clears reverse video", _mode_report(CSI + "?5$p"), "\x1b[?5;2$y")

    # RIS restores the default tab stops.
    out(CSI + "5;1H\t")
    check("RIS restores the default tab stops", cursor_pos(), (5, 9))

    # Double-height/width lines: we render single-width, but the sequence must
    # still be swallowed whole — the digit must not land on the screen.
    for seq, name in (("3", "DECDHL top"), ("4", "DECDHL bottom"),
                      ("5", "DECSWL"), ("6", "DECDWL")):
        out(CSI + "7;5H" + ESC + "#" + seq)
        check("ESC # %s (%s) is consumed, not printed" % (seq, name),
              cursor_pos(), (7, 5))

    # Keypad mode is an input-side switch: it must not disturb the screen.
    out(CSI + "9;3H" + ESC + "=")
    check("DECKPAM ESC = doesn't move the cursor", cursor_pos(), (9, 3))
    out(ESC + ">")
    check("DECKPNM ESC > doesn't move the cursor", cursor_pos(), (9, 3))
    out(CSI + "2J")


def auto_vt100_charsets():
    section("VT100 character sets (SCS, SO/SI)")

    # ESC ( 0 makes G0 the line-drawing set: 'q' draws a rule, not a letter.
    # The glyph itself can't be seen from here, but the cell advance can, and a
    # charset switch must never change how many columns text takes.
    out(CSI + "2J" + CSI + "3;1H" + ESC + "(0" + "qqqq" + ESC + "(B")
    check("ESC ( 0 line-drawing glyphs still advance one column each",
          cursor_pos(), (3, 5))

    out(CSI + "4;1H" + ESC + ")0" + "\x0e" + "qqq" + "\x0f" + ESC + "(B")
    check("SO/SI switch to G1 and back without disturbing the advance",
          cursor_pos(), (4, 4))

    out(CSI + "5;1H" + "ABC")
    check("back on ASCII after ESC ( B", cursor_pos(), (5, 4))

    # UK national set: '#' is a pound sign. Same width, so the advance is all
    # DSR can see — the glyph itself is the visual suite's job.
    out(CSI + "6;1H" + ESC + "(A" + "##" + ESC + "(B")
    check("ESC ( A (UK set) keeps a single column per glyph",
          cursor_pos(), (6, 3))
    out(CSI + "2J")


def auto_vt220_soft_reset(cols, rows):
    section("VT220 soft reset (DECSTR ESC[!p)")

    # Leave every mode DECSTR is meant to touch in its *non-default* state, so
    # a passing check can only mean the reset actually moved it.
    out(CSI + "3;8r")                      # a scroll region
    out(CSI + "?6h")                       # DECOM on
    out(CSI + "4h")                        # IRM on
    out(CSI + "?7l")                       # DECAWM off (reset leaves it off too)
    out(CSI + "?1h")                       # DECCKM on
    out(CSI + "?25l")                      # cursor hidden
    out(CSI + "9;9H")                      # cursor away from home

    out(CSI + "!p")                        # DECSTR
    check("DECSTR homes the cursor", cursor_pos(), (1, 1))

    out(CSI + "999;1H")
    check("DECSTR drops the scroll region", cursor_pos(), (rows, 1))

    check("DECSTR resets origin mode", _mode_report(CSI + "?6$p"), "\x1b[?6;2$y")
    check("DECSTR resets insert mode", _mode_report(CSI + "4$p"), "\x1b[4;2$y")
    check("DECSTR resets DECCKM", _mode_report(CSI + "?1$p"), "\x1b[?1;2$y")
    check("DECSTR shows the cursor again",
          _mode_report(CSI + "?25$p"), "\x1b[?25;1$y")
    # Per the VT220 manual a soft reset leaves auto-wrap OFF, unlike RIS.
    check("DECSTR leaves auto-wrap off (VT220 manual)",
          _mode_report(CSI + "?7$p"), "\x1b[?7;2$y")

    # But it must NOT wipe the screen the way RIS does — that's the whole
    # reason a program reaches for a soft reset. Leave a marker, soft-reset,
    # and confirm the cell is still there by parking the cursor past it.
    out(CSI + "?7h")                       # put auto-wrap back for the rest
    out(CSI + "2J" + CSI + "5;1H" + "keepme")
    out(CSI + "!p")
    out(CSI + "5;1H" + CSI + "6C")
    check("DECSTR does not erase the screen (unlike RIS)",
          cursor_pos(), (5, 7))            # 'keepme' is 6 wide, so col 7 is free
    out(CSI + "2J")


def auto_vt220_selective_erase(cols, rows):
    section("VT220 selective erase (DECSCA/DECSED/DECSEL)")

    # DSR can't read a cell, so here we only assert these are *consumed* and
    # leave the cursor where it was — the content behaviour (protected cells
    # survive) is asserted in the loopback harness and shown in the visual
    # suite. Getting the parse right is what stops the digits landing on screen.
    out(CSI + "6;12H" + CSI + '1"q')        # DECSCA 1: protect
    check("DECSCA ESC[1\"q is consumed, cursor stays", cursor_pos(), (6, 12))

    out(CSI + '0"q')                        # DECSCA 0: unprotect
    check("DECSCA ESC[0\"q is consumed, cursor stays", cursor_pos(), (6, 12))

    out(CSI + "?0J")                         # DECSED: like ED, cursor stays
    check("DECSED ESC[?0J leaves the cursor put", cursor_pos(), (6, 12))

    out(CSI + "?0K")                         # DECSEL: like EL, cursor stays
    check("DECSEL ESC[?0K leaves the cursor put", cursor_pos(), (6, 12))
    out(CSI + "2J")


def auto_vt220_charsets(cols):
    section("VT220 character sets (G2/G3, SS2/SS3, LS2/LS3)")

    # A charset switch must never change how many columns text takes — that's
    # all DSR can see. The glyphs themselves are the visual suite's job.
    out(CSI + "2J")

    # Designate G2 as the line-drawing set, then single-shift one glyph out of
    # it: SS2 affects exactly one character, and GL reverts on its own.
    out(CSI + "3;1H" + ESC + "*0" + ESC + "N" + "q" + "ABC")
    check("SS2 (ESC N) shifts one glyph from G2, then reverts",
          cursor_pos(), (3, 5))

    # Designate G3, single-shift with SS3.
    out(CSI + "4;1H" + ESC + "+0" + ESC + "O" + "x" + "AB")
    check("SS3 (ESC O) shifts one glyph from G3, then reverts",
          cursor_pos(), (4, 4))

    # Lock G2 into GL with LS2, draw a run, then lock G0 back with SI.
    out(CSI + "5;1H" + ESC + "n" + "qqqq" + "\x0f" + "AB")
    check("LS2 (ESC n) locks G2 into GL until SI restores G0",
          cursor_pos(), (5, 7))

    # LS3 likewise.
    out(CSI + "6;1H" + ESC + "o" + "xxx" + "\x0f" + "A")
    check("LS3 (ESC o) locks G3 into GL until SI restores G0",
          cursor_pos(), (6, 5))

    # Hand the terminal back exactly as pristine as we found it: G2/G3 were
    # designated to the graphics set above, and leaving them there is leftover
    # state a shell's line editor can trip over. Redesignate all four to ASCII
    # and lock G0 into GL.
    out(ESC + "(B" + ESC + ")B" + ESC + "*B" + ESC + "+B" + "\x0f")
    out(CSI + "2J")


def auto_vt220_modes():
    section("VT220 modes (DECNKM ?66, DECBKM ?67)")

    out(CSI + "?66h")
    check("DECRQM reports DECNKM set (application keypad)",
          _mode_report(CSI + "?66$p"), "\x1b[?66;1$y")
    out(CSI + "?66l")
    check("DECRQM reports DECNKM reset",
          _mode_report(CSI + "?66$p"), "\x1b[?66;2$y")

    out(CSI + "?67h")
    check("DECRQM reports DECBKM set (backspace sends BS)",
          _mode_report(CSI + "?67$p"), "\x1b[?67;1$y")
    out(CSI + "?67l")
    check("DECRQM reports DECBKM reset",
          _mode_report(CSI + "?67$p"), "\x1b[?67;2$y")


def auto_vt220_conformance():
    section("VT220 conformance level (DECSCL ESC[Ps\"p)")

    # We always answer DA as a VT100, so DECSCL only has to be swallowed whole:
    # the digits and the '\"' intermediate must not reach the screen.
    out(CSI + "7;5H" + CSI + '62"p')
    check("DECSCL ESC[62\"p is consumed, not printed", cursor_pos(), (7, 5))

    out(CSI + "8;5H" + CSI + '61;1"p')
    check("DECSCL ESC[61;1\"p (with a second param) is consumed too",
          cursor_pos(), (8, 5))
    out(CSI + "2J")


def auto_request_mode():
    section("Request mode (DECRQM)")

    # Pm: 1 = set, 2 = reset, 0 = we don't know the mode.
    out(CSI + "?7h")
    check("DECRQM reports auto-wrap set after ESC[?7h",
          _mode_report(CSI + "?7$p"), "\x1b[?7;1$y")

    out(CSI + "?7l")
    check("DECRQM reports auto-wrap reset after ESC[?7l",
          _mode_report(CSI + "?7$p"), "\x1b[?7;2$y")
    out(CSI + "?7h")

    out(CSI + "?25l")
    check("DECRQM tracks cursor visibility (DECTCEM)",
          _mode_report(CSI + "?25$p"), "\x1b[?25;2$y")
    out(CSI + "?25h")
    check("DECRQM tracks cursor visibility back on",
          _mode_report(CSI + "?25$p"), "\x1b[?25;1$y")

    out(CSI + "?2004h")
    check("DECRQM tracks bracketed paste",
          _mode_report(CSI + "?2004$p"), "\x1b[?2004;1$y")
    out(CSI + "?2004l")

    out(CSI + "?1000h")
    check("DECRQM tracks the active mouse mode",
          _mode_report(CSI + "?1000$p"), "\x1b[?1000;1$y")
    check("DECRQM: a mouse mode that isn't the active one reads as reset",
          _mode_report(CSI + "?1003$p"), "\x1b[?1003;2$y")
    out(CSI + "?1000l")

    check("DECRQM reports 0 (unknown) for a mode we don't implement",
          _mode_report(CSI + "?9999$p"), "\x1b[?9999;0$y")


def auto_alignment(cols, rows):
    section("Screen alignment (DECALN)")

    out(CSI + "5;5H" + ESC + "#8")
    check("DECALN ESC#8 homes the cursor", cursor_pos(), (1, 1))

    # DECALN drops any scroll region, so the whole screen is addressable again.
    out(CSI + "3;8r" + ESC + "#8" + CSI + "999;1H")
    check("DECALN drops the scroll region", cursor_pos(), (rows, 1))

    # It fills the screen, so the cursor should sit past 'E's, not blanks:
    # writing at the end of a filled line still wraps normally.
    out(ESC + "#8" + CSI + "1;%dH" % cols + "X")
    check("DECALN leaves a normal screen behind (last column still parks)",
          cursor_pos(), (1, cols))
    out(CSI + "2J")


def auto_margins_clamp(rows):
    section("Cursor vs. scroll margins")

    out(CSI + "5;10r")            # region = rows 5..10
    out(CSI + "8;3H" + CSI + "99A")
    check("CUU from inside the region stops at the top margin",
          cursor_pos(), (5, 3))

    out(CSI + "8;3H" + CSI + "99B")
    check("CUD from inside the region stops at the bottom margin",
          cursor_pos(), (10, 3))

    out(CSI + "8;3H" + CSI + "99E")
    check("CNL stops at the bottom margin too", cursor_pos(), (10, 1))

    out(CSI + "8;3H" + CSI + "99F")
    check("CPL stops at the top margin too", cursor_pos(), (5, 1))

    # Outside the region the margins don't apply — it's just the screen edge.
    out(CSI + "2;3H" + CSI + "99A")
    check("CUU from above the region clamps to the screen top, not the margin",
          cursor_pos(), (1, 3))

    out(CSI + "%d;3H" % rows + CSI + "99B")
    check("CUD from below the region clamps to the screen bottom",
          cursor_pos(), (rows, 3))
    out(CSI + "r")


def auto_alt_screen(rows):
    """Note this suite is *itself* running on the alt screen, so testing the
    switch means stepping back to the primary first — and putting the primary
    cursor back afterwards, or the report would print in the wrong place."""
    section("Alternate screen buffer (?1049)")

    out(CSI + "?1049l")        # back to the primary
    out(ESC + "7")             # remember where the shell left its cursor

    out(CSI + "9;20H")
    out(CSI + "?1049h")
    check("entering the alt screen homes the cursor", cursor_pos(), (1, 1))
    out(CSI + "?1049l")
    check("leaving the alt screen restores the cursor you came in with",
          cursor_pos(), (9, 20))

    # A region set on the primary screen must not leak into the alt screen.
    out(CSI + "3;8r")
    out(CSI + "?1049h" + CSI + "999;1H")
    check("the alt screen starts with no scroll region", cursor_pos(), (rows, 1))
    out(CSI + "?1049l")
    out(CSI + "r")

    check("DECRQM knows we're on the primary screen",
          _mode_report(CSI + "?1049$p"), "\x1b[?1049;2$y")
    out(CSI + "?1049h")
    check("DECRQM knows we're on the alt screen",
          _mode_report(CSI + "?1049$p"), "\x1b[?1049;1$y")
    out(CSI + "?1049l")

    out(ESC + "8")             # shell's cursor back where we found it
    out(CSI + "?1049h")        # and back onto the alt screen for the rest


def auto_edit_cursor():
    section("Editing sequences leave the cursor alone (ICH/DCH/ECH/IL/DL)")

    # These edit the screen around the cursor; none of them should move it.
    for seq, name in (("3@", "ICH ESC[3@"), ("3P", "DCH ESC[3P"),
                      ("3X", "ECH ESC[3X"), ("2S", "SU  ESC[2S"),
                      ("2T", "SD  ESC[2T")):
        out(CSI + "6;12H" + CSI + seq)
        check("%s doesn't move the cursor" % name, cursor_pos(), (6, 12))


def auto_params():
    section("Parameter parsing")

    out(CSI + "9;9H" + CSI + "5H")
    check("CUP ESC[5H (row only) defaults the column to 1", cursor_pos(), (5, 1))

    out(CSI + "9;9H" + CSI + ";7H")
    check("CUP ESC[;7H (column only) defaults the row to 1", cursor_pos(), (1, 7))

    out(CSI + "9;9H" + CSI + "0;0H")
    check("CUP ESC[0;0H treats 0 as 1", cursor_pos(), (1, 1))

    out(CSI + "4;4H" + CSI + "0A")
    check("CUU ESC[0A moves 1, not 0", cursor_pos(), (3, 4))


def auto_scroll_region_params(rows):
    section("DECSTBM parameters")

    out(CSI + "5r" + CSI + "99;1H")
    check("DECSTBM ESC[5r (top only) runs to the last row",
          cursor_pos(), (rows, 1))

    # An inverted or degenerate region is invalid, and resets to the full screen.
    out(CSI + "10;4r" + CSI + "999;1H")
    check("DECSTBM with bottom above top resets to the full screen",
          cursor_pos(), (rows, 1))

    out(CSI + "5;5r" + CSI + "999;1H")
    check("DECSTBM with a one-row region is rejected (needs 2+ rows)",
          cursor_pos(), (rows, 1))
    out(CSI + "r")




def run_auto():
    cols, rows = term_size()
    if rows < 16 or cols < 45:
        print(
            "Window too small for the automatic checks "
            f"({cols}x{rows}; need at least 45x16)."
        )
        return True

    # Hold raw input for the whole batch: a reply that lands while we're back
    # in cooked mode gets mangled before we can read it.
    with raw_input_mode():
        if cursor_pos() is None:
            print(
                f"{CSI}31mThis terminal did not answer ESC[6n, so the "
                f"automatic checks can't run.{CSI}0m\n"
                f"Try --visual instead.\n"
            )
            return True

        out(CSI + "?1049h")  # alternate screen: keep the report readable
        try:
            auto_cursor(cols, rows)
            auto_save_restore(rows)
            auto_scroll_region(rows)
            auto_index()
            auto_controls(cols)
            auto_tabs(cols)
            auto_cursor_tabs(cols)
            auto_rep()
            auto_autowrap(cols)
            auto_margins_clamp(rows)
            auto_scroll_region_params(rows)
            auto_params()
            auto_edit_cursor()
            auto_alt_screen(rows)
            auto_unicode()
            auto_reports()
            auto_device_attrs()
            auto_request_mode()
            auto_alignment(cols, rows)
            auto_vt100_controls(cols, rows)
            auto_vt100_modes(cols, rows)
            auto_vt100_reports()
            auto_vt100_charsets()
            auto_vt100_reset(cols, rows)
            auto_vt220_soft_reset(cols, rows)
            auto_vt220_selective_erase(cols, rows)
            auto_vt220_charsets(cols)
            auto_vt220_modes()
            auto_vt220_conformance()
        finally:
            _restore_terminal()

    print(
        f"{CSI}1mYoTerm automatic conformance checks{CSI}0m "
        f"{CSI}2m({cols}x{rows}){CSI}0m"
    )
    return report()


# =========================================================================
# Visual suite (things that can't report themselves)
# =========================================================================


def title(text):
    print(f"\n{CSI}1;4;36m{text}{CSI}0m")


def note(text):
    print(f"{CSI}2m  expect: {text}{CSI}0m")


# Pacing. The visual suite is meant to be *watched*, and several blocks erase
# or swap the screen — without a beat to look at the result they're useless.
PAUSE = 1.2  # seconds between blocks; scaled per call site
STEP = False  # --step: wait for Enter instead of sleeping


def pause(mult=1.0):
    """Hold the current block on screen before the next one starts."""
    if STEP:
        try:
            input(f"{CSI}2m  -- press Enter to continue --{CSI}0m")
        except (EOFError, KeyboardInterrupt):
            pass
    else:
        time.sleep(PAUSE * mult)


def visual_sgr_colors():
    title("SGR: the 8 standard colors")
    note("8 named colors, then the same 8 as backgrounds")
    for i in range(30, 38):
        out(f"{CSI}{i}m {i} {CSI}0m")
    print()
    for i in range(40, 48):
        out(f"{CSI}{i}m {i} {CSI}0m")
    print()
    pause()

    title("SGR: bright colors (90-97 / 100-107)")
    note("visibly lighter than the 30-37 / 40-47 row above")
    for i in range(90, 98):
        out(f"{CSI}{i}m {i} {CSI}0m")
    print()
    for i in range(100, 108):
        out(f"{CSI}{i}m {i} {CSI}0m")
    print()
    pause()

    title("SGR: default color codes (39 / 49)")
    note("'default fg' and 'default bg' return to the normal scheme")
    print(
        f"{CSI}31m red {CSI}39m default fg {CSI}0m"
        f"{CSI}41m red bg {CSI}49m default bg {CSI}0m"
    )
    pause()


def visual_sgr_256():
    title("SGR: 256 colors (ESC[38;5;Nm)")
    note("16 system colors, a 6x6x6 cube, then a 24-step grayscale ramp")
    for i in range(256):
        out(f"{CSI}38;5;{i}m{i:>4}{CSI}0m")
        if (i + 1) % 16 == 0:
            print()
    pause(1.5)

    title("SGR: 256-color backgrounds (ESC[48;5;Nm)")
    note("the same palette as blocks — no stray underlines or wrong colors")
    for i in range(256):
        out(f"{CSI}48;5;{i}m  {CSI}0m")
        if (i + 1) % 32 == 0:
            print()
    pause(1.5)


def visual_sgr_truecolor():
    title("SGR: truecolor (ESC[38;2;R;G;Bm)")
    note("three smooth 24-bit gradients, no banding into 256-color steps")
    width = min(64, term_size()[0] - 2)
    for r0, g0, b0, r1, g1, b1 in (
        (255, 0, 0, 0, 0, 255),
        (0, 255, 0, 255, 255, 0),
        (0, 0, 0, 255, 255, 255),
    ):
        for i in range(width):
            t = i / max(1, width - 1)
            r = int(r0 + (r1 - r0) * t)
            g = int(g0 + (g1 - g0) * t)
            b = int(b0 + (b1 - b0) * t)
            out(f"{CSI}48;2;{r};{g};{b}m ")
        print(f"{CSI}0m")
    pause()

    title("SGR: truecolor foreground")
    note("each block is a distinct shade; the text stays readable")
    for r in range(0, 256, 16):
        out(f"{CSI}38;2;{r};128;255m█")
    print(f"{CSI}0m")
    pause()

    title("SGR: truecolor animation")
    note("a smooth hue sweep — no flicker, no tearing, no leftover color")
    text = "YOTERM"
    for frame in range(90):
        out("\r")
        for i, char in enumerate(text):
            t = frame * 0.12 + i * 0.4
            r = int((math.sin(t) * 0.5 + 0.5) * 255)
            g = int((math.sin(t + 2.0) * 0.5 + 0.5) * 255)
            b = int((math.sin(t + 4.0) * 0.5 + 0.5) * 255)
            out(f"{CSI}38;2;{r};{g};{b}m{char}")
        out(CSI + "0m")
        time.sleep(1 / 60)
    print()
    pause()


def visual_sgr_attributes():
    title("SGR: text attributes")
    note("each label rendered in the attribute it names")
    for code, name in (
        (1, "Bold"),
        (2, "Dim"),
        (3, "Italic"),
        (4, "Underline"),
        (5, "Blink"),
        (7, "Reverse"),
        (8, "Conceal (should be invisible)"),
        (9, "Strike"),
    ):
        print(f"{CSI}{code}m{name}{CSI}0m {CSI}2m(ESC[{code}m){CSI}0m")
    pause(1.5)

    title("SGR: attribute reset codes")
    note("each 'no X' turns off only X, leaving the rest intact")
    print(f"{CSI}1;4;3;31mbold+underline+italic+red{CSI}0m")
    print(
        f"{CSI}1;4;3;31m"
        f"{CSI}22mnoBold {CSI}24mnoUnderline {CSI}23mnoItalic "
        f"{CSI}39mnoColor{CSI}0m {CSI}2m<- should end fully plain{CSI}0m"
    )
    print(f"{CSI}7mreverse{CSI}27m noReverse{CSI}0m")
    print(f"{CSI}9mstrike{CSI}29m noStrike{CSI}0m")
    print(f"{CSI}2mdim{CSI}22m noDim{CSI}0m")
    pause(1.5)

    title("SGR: combined attributes")
    note("all of these compose without dropping each other")
    print(f"{CSI}1;3;4;31mBold Italic Underline Red{CSI}0m")
    print(f"{CSI}1;38;5;226;48;5;18mBold yellow on blue (256){CSI}0m")
    print(f"{CSI}3;38;2;255;100;150mItalic truecolor pink{CSI}0m")
    print(f"{CSI}4;7mUnderline + reverse{CSI}0m")
    pause(1.5)

    title("SGR: parameter parsing edge cases")
    note("all four lines are plain white — no leftover color or attributes")
    print(f"{CSI}mESC[m alone resets{CSI}0m")
    print(f"{CSI}0;0;0mrepeated zeros{CSI}0m")
    print(f"{CSI}31;;32;0mempty param in the middle{CSI}0m")
    print(f"{CSI}38;5;9m{CSI}0mextended color then reset{CSI}0m")
    pause(1.5)


def visual_erase():
    title("Erase in Line (EL: ESC[0K / ESC[1K / ESC[2K)")
    note("each line is drawn in full, then erased in front of you")

    out("  KEEP-THIS-RIGHT GONE-GONE-GONE")
    pause(0.8)
    out(CSI + "14D" + CSI + "0K" + "\n")
    print(
        f"{CSI}2m    ^ ESC[0K erased from the cursor to the end of the " f"line{CSI}0m"
    )
    pause()

    out("  GONE-GONE-GONE KEEP-THIS-LEFT")
    pause(0.8)
    out("\r" + CSI + "15C" + CSI + "1K" + "\n")
    print(
        f"{CSI}2m    ^ ESC[1K erased from the start of the line to the "
        f"cursor{CSI}0m"
    )
    pause()

    out("  ALL-OF-THIS-SHOULD-VANISH")
    pause(0.8)
    out(CSI + "2K" + "\r\n")
    print(f"{CSI}2m    ^ ESC[2K erased the whole line{CSI}0m")
    pause()

    title("Erase in Display (ED: ESC[0J / ESC[1J / ESC[2J / ESC[3J)")
    note("a block of '#' appears, then everything below the cursor is erased")
    for _ in range(3):
        print("  " + "#" * 30)
    pause(0.9)
    out(CSI + "2A" + CSI + "12C")  # park mid-block
    pause(0.5)
    out(CSI + "0J")  # erase from here to the screen bottom
    out(CSI + "2B" + "\r")
    print(
        f"{CSI}2m  ^ ESC[0J erased from the cursor to the end of the " f"screen{CSI}0m"
    )
    pause()
    print(
        f"{CSI}2m  (ESC[2J clears the visible screen; ESC[3J also drops "
        f"scrollback — 'clear' relies on both){CSI}0m"
    )
    pause()


def visual_insert_delete():
    title("Insert / delete characters (ICH ESC[@, DCH ESC[P, ECH ESC[X)")
    note("ICH pushes right, DCH pulls left, ECH blanks in place")
    for seq, label in (
        ("3@", "ESC[3@ (ICH) inserted 3 blanks, pushing right"),
        ("3P", "ESC[3P (DCH) deleted 3 cells, pulling left"),
        ("3X", "ESC[3X (ECH) erased 3 cells without shifting"),
    ):
        out("  ABCDEFGH")
        pause(0.8)
        out(CSI + "8D" + CSI + seq + "\n")
        print(f"{CSI}2m    ^ {label}{CSI}0m")
        pause()

    title("Insert / delete lines (IL ESC[L, DL ESC[M)")
    note("a blank line opens up between 1 and 2, then line B disappears")
    print("  line 1")
    print("  line 2")
    print("  line 3")
    pause(0.9)
    out(CSI + "2A" + CSI + "L")  # open a blank line above 'line 2'
    out(CSI + "3B" + "\r")
    print(f"{CSI}2m  ^ ESC[L inserted a blank line, pushing the rest " f"down{CSI}0m")
    pause()

    print("  line A")
    print("  line B")
    print("  line C")
    pause(0.9)
    out(CSI + "2A" + CSI + "M")  # delete 'line B'
    out(CSI + "2B" + "\r")
    print(f"{CSI}2m  ^ ESC[M deleted a line, pulling the rest up{CSI}0m")
    pause()


def visual_scroll_region():
    title("Scroll region (DECSTBM ESC[t;br) + SU/SD (ESC[S / ESC[T)")
    note("only the boxed middle rows scroll; the header and footer stay put")
    print("+-- header (must not move) --+")
    for i in range(1, 6):
        print(f"|  region line {i}          |")
    print("+-- footer (must not move) --+")

    pause(0.9)
    out(CSI + "s")
    out(CSI + "6A")  # to the first region line
    # Rows are relative to the screen, so find them from the cursor.
    pos = cursor_pos()
    if pos is None:
        out(CSI + "u")
        print(
            f"{CSI}33m  (skipped: this terminal doesn't answer ESC[6n, so "
            f"the demo can't locate its own rows){CSI}0m"
        )
        pause()
        return
    if pos:
        top = pos[0]
        out(f"{CSI}{top};{top + 4}r")  # region = the 5 middle lines
        for i in range(6):
            out(f"{CSI}{top + 4};1H")
            out(f"|  scrolled in {i + 1}         |")
            out(CSI + "1S")  # scroll the region up
            time.sleep(0.45)
        out(CSI + "r")  # always reset the region
    out(CSI + "u")
    print("  ^ the header/footer above should be exactly where they started")
    pause()


def visual_reverse_index():
    title("Reverse index (RI ESC M): scrolling backwards")
    note(
        "new lines appear at the TOP and push the rest down; "
        "the header and footer stay put"
    )
    print("+-- header (must not move) --+")
    for i in range(1, 6):
        print(f"|  original line {i}        |")
    print("+-- footer (must not move) --+")
    pause(0.9)

    out(CSI + "s")
    out(CSI + "6A")  # to the first region line
    pos = cursor_pos()
    if pos is None:
        out(CSI + "u")
        print(f"{CSI}33m  (skipped: this terminal doesn't answer ESC[6n){CSI}0m")
        pause()
        return

    top = pos[0]
    out(f"{CSI}{top};{top + 4}r")  # region = the 5 middle lines
    for i in range(6):
        out(f"{CSI}{top};1H")  # sit on the top margin
        out(ESC + "M")  # RI there scrolls the region DOWN
        out(f"{CSI}{top};1H")
        out(f"|  scrolled back {i + 1}         |")
        time.sleep(0.45)
    out(CSI + "r")  # always reset the region
    out(CSI + "u")
    print("  ^ this is exactly how a pager scrolls back up through a file")
    pause()


def visual_alt_screen():
    title("Alternate screen buffer (ESC[?1049h / l)")
    note("the screen swaps to a message, then restores this text untouched")
    pause(1.2)
    out(CSI + "?1049h" + CSI + "2J" + CSI + "1;1H")
    out(f"{CSI}1;33mThis is the ALTERNATE screen.{CSI}0m\n\n")
    out("Your scrollback and the primary screen are untouched.\n")
    out("This is what a full-screen TUI (vim, htop) runs inside.\n\n")
    for left in range(5, 0, -1):
        out(f"\rReturning to the primary screen in {left}s... ")
        time.sleep(1.0)
    out(CSI + "?1049l")
    print("  ^ back on the primary screen, with the suite intact above")
    pause()


def visual_cursor_visibility():
    title("Cursor visibility (DECTCEM ESC[?25l / ESC[?25h)")
    note("the cursor disappears for ~1s, then comes back")
    out("  hiding the cursor... ")
    out(CSI + "?25l")
    time.sleep(2.0)
    out(CSI + "?25h")
    print("visible again")
    pause()


def visual_cursor_shapes():
    title("Cursor shape (DECSCUSR ESC[n q)")
    note(
        "the caret changes shape at the end of each line; blinking styles "
        "hold solid while 'typing', then fade"
    )
    for n, name in (
        (5, "blinking bar (YoTerm's default)"),
        (6, "steady bar"),
        (1, "blinking block"),
        (2, "steady block"),
        (3, "blinking underline"),
        (4, "steady underline"),
    ):
        out(f"\r{CSI}2K  ESC[{n} q  ->  {name}   ")
        out(CSI + "%d q" % n)
        time.sleep(2.0)
    out(CSI + "5 q")  # back to the default
    out(f"\r{CSI}2K  ESC[5 q  ->  back to the default blinking bar\n")
    pause()


def visual_unicode():
    title("Unicode coverage")
    note("no tofu (missing-glyph boxes); the columns line up")
    rows = (
        ("Box drawing", "┌─┬─┐ │ ├─" "┼─┤ └─┴─┘"),
        ("Heavy box", "┏━┳━┓ ┃ ┣━" "╋━┫ ┗━┻━┛"),
        ("Rounded", "╭───╮ ╰───" "╯"),
        ("Blocks", "█▓▒░ ▀▄ ▌▐"),
        ("Braille", "⠁⠃⠉⠙⠹⡱⢱⢣"),
        ("Arrows", "←↑→↓ ↔↕ ⇐⇒"),
        ("Math", "∀∂∈∑√∞≈≠≤"),
        ("Greek", "αβγδε ΑΒΓ"),
        ("Cyrillic", "АБВГ абвг"),
        ("Latin accents", "àéîõü Æßñ"),
        ("Symbols", "✓✗★☆♪⚠µ€"),
    )
    for name, sample in rows:
        print(f"  {name:<14} {sample}")
    pause(1.5)

    title("Wide characters (2 columns each)")
    note("every '|' below lines up in one straight column")
    for label, text in (
        ("CJK", "漢字テスト"),
        ("Hangul", "한국어테스"),
        ("Emoji", "\U0001f600\U0001f680\U0001f44d" "\U0001f525\U0001f389"),
        ("ASCII", "AAAAAAAAAA"),
    ):
        print(f"  {label:<8} {text}|")
    pause(1.5)

    title("Color emoji")
    note("drawn in full color, not as monochrome outlines")
    print(
        "  \U0001f600 \U0001f680 \U0001f525 \U0001f389 \U0001f4a1 "
        "\U0001f30d \U0001f955 \U0001f436"
    )
    pause(1.5)


def visual_wrapping():
    title("Line wrapping")
    note("one unbroken band of '=' with no blank lines punched through it")
    print("=" * (term_size()[0] * 2 + 7))
    pause()

    title("Backspace (BS): destructive erase")
    note("'ABCDE' is rubbed out one character at a time, right to left")
    out("  ABCDE")
    pause(0.8)
    for _ in range(5):
        out("\b \b")  # left, blank the cell, left again
        time.sleep(0.35)
    print()
    pause()

    title("Backspace (BS): overwrite in place")
    note("the cursor walks back to the 'A', then 'ABCDE' becomes 'XXXXX'")
    out("  ABCDE")
    pause(0.8)
    out("\b" * 5)  # back to the 'A', erasing nothing on the way
    pause(0.5)
    for _ in range(5):
        out("X")  # each glyph advances a column, so they march right
        time.sleep(0.35)
    print()
    pause()

    title("Tab stops (every 8 columns)")
    note("the second column of each row starts at the same place")
    print("  1\t2\t3\t4")
    print("  Hello\tWorld")
    print("  A\tB\tC")
    pause()


def visual_selective_erase():
    title("Selective erase (DECSCA ESC[Ps\"q + DECSED/DECSEL)")
    note("the protected FIELDS survive; only the unprotected text is wiped")

    # A tiny form: fixed labels are protected, the "answers" are not. DECSEL
    # then erases the whole line but the labels stay.
    out("  ")
    out(CSI + '1"q' + "Name: " + CSI + '0"q' + "John Appleseed")
    out("\r\n  ")
    out(CSI + '1"q' + "City: " + CSI + '0"q' + "Cupertino")
    out(CSI + '0"q')  # leave protection off again
    pause(1.2)

    # Walk back to each answer line and selective-erase the whole line. The
    # cursor is sitting on the City row, so it's up ONE to reach Name; use
    # CUD (ESC[B) + CR to step back down, which doesn't depend on how the
    # platform treats a bare LF.
    out(CSI + "1A\r")           # to the Name row, column 1
    out(CSI + "?2K")            # DECSEL 2: wipe unprotected across the line
    out(CSI + "1B\r")           # down to the City row, column 1
    out(CSI + "?2K")
    out(CSI + "1B\r")           # down past the form for the caption
    print(f"{CSI}2m  ^ ESC[?2K erased the answers; 'Name:'/'City:' were "
          f"protected{CSI}0m")
    pause(1.5)


def visual_modes():
    title("Input modes")
    term_reports = []
    for name, seq in (
        ("Mouse click tracking", "?1000"),
        ("Mouse drag tracking", "?1002"),
        ("Mouse any-motion (hover)", "?1003"),
        ("SGR mouse coordinates", "?1006"),
        ("Bracketed paste", "?2004"),
    ):
        out(CSI + seq + "h")
        out(CSI + seq + "l")
        term_reports.append(name)
    note("these are set/reset silently; no stray characters should appear")
    for name in term_reports:
        print(f"  sent set+reset for {name}")
    pause()


def run_visual():
    print(f"{CSI}1mYoTerm visual ANSI suite{CSI}0m")
    if STEP:
        print(f"{CSI}2mStep mode: press Enter to advance.{CSI}0m")
    else:
        print(
            f"{CSI}2mPacing at {PAUSE:g}s per block — use --step to advance "
            f"manually, or --pause=N to change it.{CSI}0m"
        )
    pause()
    visual_sgr_colors()
    visual_sgr_256()
    visual_sgr_truecolor()
    visual_sgr_attributes()
    visual_erase()
    visual_selective_erase()
    visual_insert_delete()
    visual_scroll_region()
    visual_reverse_index()
    visual_alt_screen()
    visual_cursor_visibility()
    visual_cursor_shapes()
    visual_unicode()
    visual_wrapping()
    visual_modes()
    print(f"\n{CSI}1;32mVisual suite done.{CSI}0m")


# ================================================================== entry


def main():
    global PAUSE, STEP

    args = set(sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0

    do_auto = "--visual" not in args
    do_visual = "--auto" not in args

    if "--step" in args:
        STEP = True
    elif "--fast" in args:
        PAUSE = 0.0
    elif "--slow" in args:
        PAUSE = 2.5
    for arg in args:
        if arg.startswith("--pause="):
            try:
                PAUSE = max(0.0, float(arg.split("=", 1)[1]))
            except ValueError:
                print(f"bad value for {arg}; expected a number of seconds")
                return 2

    ok = True
    try:
        if do_auto:
            ok = run_auto()
        if do_visual:
            run_visual()
    finally:
        # Never leave the terminal in a weird state, even on Ctrl-C.
        _restore_terminal()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
