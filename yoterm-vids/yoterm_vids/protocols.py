"""Escape-sequence builders for the terminal side of playback.

These produce the byte strings a *remote* terminal understands — used by the
CLI's escape-sequence sink (streaming to stdout). The in-process native path
(YoTerm playing its own `YT;vid`) does not go through here at all; it hands
decoded pixels straight to the renderer, which is both faster and sidesteps
YoTerm's 1 KB cap on escape-sequence (OSC) payloads.

Reference — the sequences YoTerm speaks:

    YT;img ;id:N ;data:<base64> ;cols:C ;rows:R ;fit:fill     place/replace image N
    YT;img ;del ;id:N                                         remove image N
    YT;vid ;path:<file> [;cols:C;rows:R;loop:on]              native video (M4b)
"""

# ESC ] ... ST  — an Operating System Command. ST is ESC backslash.
_OSC = "\x1b]"
_ST = "\x1b\\"


def esc_home():
    """Move the cursor to the top-left (so each frame redraws in place)."""
    return "\x1b[H"


def enter_fullscreen():
    """Alternate screen + hidden cursor — a clean canvas that restores on exit."""
    return "\x1b[?1049h\x1b[?25l"


def leave_fullscreen():
    """Undo enter_fullscreen(): show the cursor and restore the user's screen."""
    return "\x1b[?25l\x1b[?1049l".replace("?25l", "?25h")


def clear_screen():
    return "\x1b[2J"


def yt_image(data_b64, cols, rows, img_id=1, fit="fill"):
    """Place (or replace, since the id is fixed) one image frame.

    `fit:fill` because the pixels are already fitted to the box by the resizer,
    so the terminal shouldn't letterbox them a second time.
    """
    return (
        f"{_OSC}YT;img;id:{img_id};data:{data_b64};"
        f"cols:{cols};rows:{rows};fit:{fit}{_ST}"
    )


def yt_image_delete(img_id=1):
    return f"{_OSC}YT;img;del;id:{img_id}{_ST}"


def yt_video(path, cols=None, rows=None, loop=False):
    """The native YT;vid request YoTerm parses to start in-process playback."""
    parts = [f"{_OSC}YT;vid;path:{path}"]
    if cols:
        parts.append(f";cols:{cols}")
    if rows:
        parts.append(f";rows:{rows}")
    if loop:
        parts.append(";loop:on")
    parts.append(_ST)
    return "".join(parts)
