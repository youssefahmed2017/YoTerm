# YoTerm

A GPU-accelerated terminal emulator for Windows, written from scratch in Python.

YoTerm parses the ANSI/VT stream itself, renders every glyph on the GPU as two
instanced triangles, and drives a real shell over Windows ConPTY. It aims for
serious VT100/VT220 conformance — and then adds a few things a cell-stepped
terminal simply can't do: **genuinely smooth text gradients** and **real inline
images**, both sampled on the GPU rather than faked with block characters.

```
python app.py
```

## Highlights

- **GPU rendering** — a ModernGL pipeline with one instanced quad per cell, a
  glyph atlas shared across tabs, and damage tracking so an idle screen costs no
  frames. A blinking caret is a 48-byte buffer write, not a full-screen rebuild.
- **VT100 / VT220 conformance** — cursor movement, scroll regions, origin mode,
  tabs, auto-wrap, character sets (incl. DEC line-drawing and single/locking
  shifts), selective erase (DECSCA/DECSED/DECSEL), soft reset (DECSTR), device
  attributes, DECRQM, and more. All asserted by a self-checking suite (`ansi.py`)
  that asks the terminal where its own cursor ended up.
- **Unicode done properly** — correct east-asian widths, wide CJK/emoji, combining
  marks composed via NFC, and OS-quality color emoji.
- **Full color** — 16 / 256 / 24-bit truecolor, plus the usual SGR attributes
  (bold, dim, italic, underline, reverse, strike, conceal, and optional blink).
- **Windows-Terminal-style tabs** on the native window frame, with OSC 0/2 titles.
- **Human-editable settings** — a real Python file at `~/.yoterm_config.py`, which
  is exactly what the in-app settings dialog (Ctrl+,) writes.
- **YoTerm's own OSC sequences** (the `YT` namespace) — true gradients and images,
  which degrade to plain text on any other terminal. See below.

## The `YT` sequences

YoTerm's own features live under a custom OSC namespace, `ESC ] YT ; ... ST`.
Because unrecognized OSC is silently swallowed by every conformant terminal, a
gradient falls back to plain text and an image to nothing — never to escape-code
garbage. A program can feature-detect first with the handshake.

```sh
# Capability handshake — YoTerm replies with its version + feature list
printf '\e]YT;?\e\\'

# True gradient text: the colour is interpolated per-vertex across the whole run,
# so it changes *inside* each glyph — not the per-cell stepping of ANSI tricks.
printf '\e]YT;gradient;#00aaff;#ff00aa\e\\SMOOTH\e]YT;gradient;off\e\\\n'
printf '\e]YT;gradient;33;31;cycle:on;speed:1.5\e\\animated\e]YT;gradient;off\e\\\n'

# Real images, GPU-sampled (block, or inline inside a line of text)
printf '\e]YT;img;path:cat.png;cols:20\e\\\n'
printf 'icon \e]YT;img;path:logo.png;cols:2;inline:on\e\\ in a sentence\n'
```

Gradient options: any number of colour stops (`#rrggbb`, names, or SGR codes),
`angle:`, `cycle:on`, `speed:`, `target:fg|bg`. Ended by `YT;gradient;off` or a
plain `ESC[0m`.

Image options: `path:` or `data:` (base64), `cols`/`rows`, `w`/`h` (cells or
`px`), `fit:contain|fill`, `inline:on`, and `id:` (for replace / `del`).

Run the demo inside YoTerm to see them all:

```
python yt_seq_tests.py
```

## Architecture

```
keyboard ─▶ PySide6 widget ─▶ ConPTY (pywinpty) ─▶ shell
                                                     │
shell output ─▶ reader thread ─▶ queue ─▶ Terminal.write()  (term.py: the parser
                                                              + screen model)
                                              │
                                              ▼
                          ModernGL renderer (app.py: instanced glyph quads,
                          gradient batch, image quads, cursor)
```

| File | Role |
|------|------|
| `app.py` | Qt application shell, tabs, and the ModernGL renderer. Entry point. |
| `term.py` | The terminal model: ANSI/VT parser, screen buffer, scrollback. |
| `tools.py` | Glyph atlas, font rasterization, geometry builder, palette. |
| `config.py` | Settings (`~/.yoterm_config.py`) as an editable dataclass. |
| `ytseq.py` | `YT;gradient` parsing and colour ramp math. |
| `ytimg.py` | `YT;img` decode (Pillow) and cell-sizing. |
| `ansi.py` | Self-checking VT100/VT220 conformance suite. |
| `yt_seq_tests.py` | Visual demo of the `YT` gradient/image sequences. |

## Requirements

- **Windows** (uses ConPTY via `pywinpty`, and reads the registry `PATH`)
- **Python 3.11+**
- A GPU/driver with OpenGL 3.3

```
pip install -r requirements.txt
```

## Running

```
python app.py
```

Shortcuts: `Ctrl+,` settings · `Ctrl+=` / `Ctrl+-` / `Ctrl+0` font zoom ·
`Ctrl+Shift+C` / `Ctrl+Shift+V` copy/paste.

## Conformance

`ansi.py` is a real test suite, not a screenshot script: it drives the terminal
and reads back the cursor position / mode replies to *assert* behaviour.

```
python ansi.py            # automatic checks, then the visual suite
python ansi.py --auto     # just the automatic checks
```

A few LF-related checks report `skip` inside a Windows console, which rewrites
`LF`→`CRLF` before the terminal ever sees it — that's a property of the console,
not of YoTerm, so the suite is honest about it rather than pretending to pass.
