# YoTerm-Vids

Play videos **inside** [YoTerm](../README.md) — real, GPU-sampled frames, not
ASCII art. It decodes a video with PyAV and streams each frame to the terminal
as a YoTerm `YT;img` image, replacing the previous frame in place.

```
pip install -e .
yoterm-vids movie.mp4
```

Requires YoTerm (for actual display) and Python 3.11+. On any other terminal the
image sequences are silently ignored, so it degrades to doing nothing rather
than spewing escape-code garbage.

## Status

Built milestone-by-milestone:

- **M0 — Setup** ✅ CLI, logging, dependency management.
- **M1 — Decoding** ✅ streaming `Decoder`, metadata, timestamps, EOF.
- **M2 — Scheduling** ✅ drift-free pacing, frame-skip under load.
- **M3 — Image processing** ✅ RGB convert + aspect-preserving resize.
- **M4 — YT output** ✅ two paths: the `yoterm-vids <file> --play` CLI streams
  `YT;img` frames; **native `YT;vid`** in YoTerm imports this package, decodes on
  a worker thread, and feeds the renderer directly (no encode/base64 round-trip).
  Spacebar pauses with a ❚❚ indicator.
- **M5 — Controls** ✅ pause/resume (space), restart (r), frame-step (.),
  quit (q/Esc). The engine has a pausable clock, restart, and single-frame step.
- M6 — Seeking · M7 — Performance · M8 — Audio · … (next)

The engine (`decoder`/`scheduler`/`resize`/`player`) is sink-agnostic, which is
why the same code drives both the CLI (`renderer.EscapeSink`) and YoTerm's native
player (a callback sink in `app.py`).

## Layout

```
yoterm_vids/
├── cli.py        argument parsing, entry point
├── decoder.py    PyAV: open, metadata, frames, timestamps
├── scheduler.py  drift-free frame timing            (M2)
├── resize.py     RGB convert + aspect-preserving fit (M3)
├── renderer.py   encode frame + emit YT;img          (M4)
├── protocols.py  YT / terminal escape-sequence builders
├── controls.py   keyboard: pause/resume/seek/quit    (M5)
├── audio.py      audio thread + A/V sync             (M8)
└── utils.py      logging, formatting
```
