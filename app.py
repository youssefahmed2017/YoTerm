# YoTerm: PySide6 (Qt) application shell hosting the ModernGL terminal renderer.
#
#   Qt (window / menus / tabs / clipboard / input)
#     └── TerminalWidget (QOpenGLWidget): PTY I/O, input, the terminal model,
#           video-playback control -- and it owns a
#           └── renderer.Renderer: the ModernGL context and every draw pass
#                 (glyphs, cursor, selection, zones, gradients, images, the
#                  video overlay). Reads live widget state through __getattr__.
#
#   TerminalWidget -> Terminal model -> pywinpty -> shell
#
# The GPU renderer lives in renderer.py; app.py keeps the Qt/PTY/input shell.

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

# Appearance constants the *renderer* owns now live in renderer.py (BG_COLOR,
# SELECTION_COLOR, DIM_FACTOR, CURSOR_COLOR, CURSOR_THICK_PX, _lerp). The cursor
# *timing* below stays here: it drives the widget's blink scheduler (_tick).

# Cursor. A caret reads as "real" when it keeps a constant weight regardless of
# font size, stays solid while you're working, and only blinks once you pause.
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
        p.drawPolyline(
            QtGui.QPolygonF(
                [
                    QtCore.QPointF(4.5, 6.5),
                    QtCore.QPointF(8.0, 10.0),
                    QtCore.QPointF(11.5, 6.5),
                ]
            )
        )
        p.end()
        path = os.path.join(tempfile.gettempdir(), "yoterm_combo_arrow.png")
        pm.save(path, "PNG")
        _ARROW_PATH = path.replace("\\", "/")  # QSS urls want forward slashes
    return (
        "QComboBox::down-arrow { image: url(%s); width: 16px; height: 16px; }"
        % _ARROW_PATH
    )


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


from renderer import Renderer


class _PullDevice(QtCore.QIODevice):
    """Feeds a QAudioSink in pull mode from its owner's PCM buffer.

    QAudioSink calls readData() (on Qt's audio thread) whenever the device needs
    more samples; we hand back what's buffered and pad any shortfall with silence
    so the device never underruns and its played clock keeps ticking smoothly.
    """

    def __init__(self, sink):
        super().__init__(sink)
        self._sink = sink

    def isSequential(self):
        return True

    def bytesAvailable(self):
        # Report a floor of buffered data so QAudioSink keeps pulling steadily;
        # readData pads with silence when the buffer is actually short.
        with self._sink._lock:
            return len(self._sink._buf) + self._sink._bytes_per_sec

    def readData(self, maxlen):
        s = self._sink
        with s._lock:
            take = min(int(maxlen), len(s._buf))
            data = bytes(s._buf[:take])
            del s._buf[:take]
        if len(data) < maxlen:
            data += b"\x00" * (int(maxlen) - len(data))  # silence on shortfall
        return data

    def writeData(self, data):
        return 0


class _QtAudioSink(QtCore.QObject):
    """Engine ``audio.AudioSink`` backed by QtMultimedia's ``QAudioSink``.

    The engine's feed thread only touches the byte buffer and a cached clock
    (``open``/``write``/``flush``/``buffered_seconds``/``played_seconds`` — all
    lock-guarded, thread-safe). The Qt device object is created, suspended and
    stopped on the GUI thread (this QObject's thread); ``open``/``close`` marshal
    there via queued signals. The master clock is ``processedUSecs()``, sampled
    by a GUI-thread timer so the engine reads it without calling Qt off-thread.
    """

    _openSig = QtCore.Signal(int, int)
    _closeSig = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._bytes_per_sec = 1
        self._played_us = 0.0
        self._muted = False
        self._sink = None
        self._io = None
        self._timer = None
        self._openSig.connect(self._open_gui, QtCore.Qt.QueuedConnection)
        self._closeSig.connect(self._close_gui, QtCore.Qt.QueuedConnection)

    # --- engine-thread API (buffer + cached clock only; no Qt calls) --------
    def open(self, rate, channels):
        with self._lock:
            self._bytes_per_sec = max(1, int(rate) * int(channels) * 2)
        self._openSig.emit(int(rate), int(channels))

    def write(self, pcm):
        with self._lock:
            self._buf += pcm

    def buffered_seconds(self):
        with self._lock:
            return len(self._buf) / self._bytes_per_sec

    def played_seconds(self):
        return self._played_us / 1_000_000.0

    def flush(self):
        with self._lock:
            self._buf.clear()

    def close(self):
        self._closeSig.emit()

    # --- transport (called on the GUI thread) -------------------------------
    @property
    def muted(self):
        return self._muted

    def set_muted(self, muted):
        self._muted = bool(muted)
        if self._sink is not None:
            self._sink.setVolume(0.0 if self._muted else 1.0)

    def pause(self):
        if self._sink is not None:
            self._sink.suspend()

    def resume(self):
        if self._sink is not None:
            self._sink.resume()

    # --- GUI-thread slots ---------------------------------------------------
    @QtCore.Slot(int, int)
    def _open_gui(self, rate, channels):
        try:
            from PySide6.QtMultimedia import QAudioFormat, QAudioSink

            fmt = QAudioFormat()
            fmt.setSampleRate(rate)
            fmt.setChannelCount(channels)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            self._sink = QAudioSink(fmt, self)
            self._sink.setVolume(0.0 if self._muted else 1.0)
            self._io = _PullDevice(self)
            self._io.open(QtCore.QIODevice.ReadOnly)
            self._sink.start(self._io)
            self._timer = QtCore.QTimer(self)
            self._timer.timeout.connect(self._sample_clock)
            self._timer.start(8)  # ~125 Hz clock sampling; finer than a frame
        except Exception as exc:  # no device / backend: play silent, don't crash
            print(f"YT;vid: audio output unavailable ({exc})", file=sys.stderr)
            self._sink = None

    @QtCore.Slot()
    def _sample_clock(self):
        if self._sink is not None:
            self._played_us = float(self._sink.processedUSecs())

    @QtCore.Slot()
    def _close_gui(self):
        try:
            if self._timer is not None:
                self._timer.stop()
            if self._sink is not None:
                self._sink.stop()
        except Exception:
            pass
        self._sink = self._io = self._timer = None


class _VideoController(QtCore.QObject):
    """Drives one YT;vid: decodes on a worker thread (the yoterm_vids engine)
    and hands each decoded frame to the widget on the GUI thread via a queued
    signal, so all GL/model touching stays on the main thread.

    Pause/resume/stop are called from the GUI thread; the pausable clock in the
    engine means paused time never bleeds into frame timing.
    """

    # img_id, rgba bytes, width, height  (queued to the GUI thread)
    frameReady = QtCore.Signal(int, object, int, int)
    finished = QtCore.Signal(int)
    # img_id, requested seconds, thumbnail rgba, width, height — a scrub-preview
    # frame, decoded off a second container so playback is never disturbed.
    thumbReady = QtCore.Signal(int, float, object, int, int)

    THUMB_W = 168  # scrub-preview thumbnail width in px (height by aspect)

    def __init__(self, img_id, path, box, loop, mute=False, parent=None):
        super().__init__(parent)
        self.img_id = img_id
        self.path = path
        self._ok = False
        # Scrub-preview thumbnail worker: its own decoder + thread, started
        # lazily on the first hover so plain playback pays nothing for it.
        self._thumb_thread = None
        self._thumb_cond = threading.Condition()
        self._thumb_req = None  # latest requested seconds (coalesced)
        self._thumb_stop = False
        try:
            from yoterm_vids.yoterm_vids.player import Player
            from yoterm_vids.yoterm_vids.renderer import CallbackSink
        except Exception as exc:  # package / PyAV not installed
            print(f"YT;vid: video engine unavailable ({exc})", file=sys.stderr)
            self.player = None
            return
        # Ask the engine for raw RGBA bytes: swscale converts+scales straight to
        # the GPU's texture format in one pass, so the frame path never touches
        # PIL (no full-res RGB buffer, no per-frame RGBA convert).
        sink = CallbackSink(on_show=self._on_show, pixel_format="rgba")
        # Native audio output. Created regardless; the engine only opens the
        # device if the file actually has an audio stream (so video-only clips
        # never touch QtMultimedia).
        self._audio = _QtAudioSink(parent=self)
        self._audio.set_muted(mute)
        self.player = Player(
            path,
            sink,
            box,
            mode="contain",
            quality="fast",
            loop=loop,
            audio_sink=self._audio,
        )
        self._ok = True
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def muted(self):
        a = getattr(self, "_audio", None)
        return a.muted if a is not None else False

    def set_muted(self, muted):
        a = getattr(self, "_audio", None)
        if a is not None:
            a.set_muted(muted)

    def resize_box(self, box):
        """Re-decode at a new pixel box (fullscreen enter/exit)."""
        if self.player is not None:
            self.player.resize(box)

    def start(self):
        if self._ok:
            self._thread.start()
        else:
            self.finished.emit(self.img_id)

    @staticmethod
    def _safe_emit(emit):
        """Fire a Qt signal from a worker thread, tolerating our C++ object
        having been deleted during tab/app teardown: a daemon decode thread can
        outlive the QObject and would otherwise crash with 'Signal source has
        been deleted'. `emit` is a thunk so the signal *lookup* -- which also
        raises on a dead object -- is inside the guard too."""
        try:
            emit()
        except RuntimeError:
            pass

    def _run(self):
        try:
            self.player.play()
        except Exception as exc:
            print(f"YT;vid: playback error ({exc})", file=sys.stderr)
        finally:
            self._safe_emit(lambda: self.finished.emit(self.img_id))

    def _on_show(self, frame, pts):
        # Worker thread: `frame` is (rgba_bytes, w, h) straight from swscale
        # (pixel_format="rgba"), already sized for the box -- post it to the GUI
        # thread with no conversion.
        rgba, w, h = frame
        self._safe_emit(lambda: self.frameReady.emit(self.img_id, rgba, w, h))

    @property
    def paused(self):
        return bool(self.player and self.player.paused)

    @property
    def position(self):
        return self.player.position if self.player else 0.0

    @property
    def duration(self):
        return self.player.duration if self.player else 0.0

    def toggle(self):
        if self.player:
            self.player.toggle()

    def restart(self):
        if self.player:
            self.player.restart()

    def step(self):
        if self.player:
            self.player.step()

    def seek_relative(self, delta):
        if self.player:
            self.player.seek_relative(delta)

    def seek_percent(self, fraction):
        if self.player:
            self.player.seek_percent(fraction)

    def stop(self):
        if self.player:
            self.player.stop()
        with self._thumb_cond:
            self._thumb_stop = True
            self._thumb_cond.notify_all()

    # --- scrub-preview thumbnails ------------------------------------------
    def request_thumb(self, seconds):
        """Ask for a preview frame near `seconds`. Cheap and coalescing: rapid
        mouse moves collapse to just the latest request, and the worker only
        decodes the nearest keyframe (no exact-frame walk), so scrubbing stays
        responsive without touching the playing decoder."""
        if not self._ok:
            return
        with self._thumb_cond:
            self._thumb_req = seconds
            if self._thumb_thread is None:
                self._thumb_thread = threading.Thread(
                    target=self._thumb_run, daemon=True
                )
                self._thumb_thread.start()
            self._thumb_cond.notify_all()

    def _thumb_run(self):
        import av
        from fractions import Fraction
        from PIL import Image

        try:
            container = av.open(self.path)
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                container.close()
                return
            stream.thread_type = "AUTO"
            tb = stream.time_base or Fraction(1, 1000)
        except Exception:
            return
        last = None
        try:
            while True:
                with self._thumb_cond:
                    while self._thumb_req is None and not self._thumb_stop:
                        self._thumb_cond.wait()
                    if self._thumb_stop:
                        break
                    req = self._thumb_req
                    self._thumb_req = None
                if req == last:
                    continue
                last = req
                try:
                    container.seek(
                        int(max(0.0, req) / tb),
                        stream=stream,
                        backward=True,
                        any_frame=False,
                    )
                    frame = next(container.decode(stream), None)
                    if frame is None:
                        continue
                    img = Image.fromarray(frame.to_ndarray(format="rgb24"), "RGB")
                    tw = self.THUMB_W
                    th = max(1, round(img.height * tw / img.width))
                    img = img.resize((tw, th), Image.BILINEAR)
                    rgba = img.convert("RGBA").tobytes()
                    self._safe_emit(
                        lambda: self.thumbReady.emit(self.img_id, req, rgba, tw, th)
                    )
                except Exception:
                    continue
        finally:
            container.close()


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
        self._has_blink = False  # is any SGR-5 text actually on screen?
        self._last_blink_on = True
        # YoTerm gradient text (ESC ] YT ; gradient): the renderer collects the
        # per-glyph geometry; the widget only tracks whether an animated gradient
        # is on screen, which drives repaints.
        self._has_cycle = False  # is any animated gradient on screen?
        self.out_queue = queue.Queue()

        # Native video playback (ESC ] YT ; vid). The model reserves an image
        # placement and posts a request; we run a decoder thread per video and
        # stream frames into that placement. img_id -> _VideoController.
        self._videos = {}
        self._muted = False  # tab-wide audio mute (the `m` key); new videos inherit it
        self._fullscreen_vid = (
            None  # id of the video filling the whole terminal, or None
        )
        self._fs_saved_box = {}  # vid -> original (w_px, h_px) to restore on exit
        # YouTube-style playback overlay. It's drawn on top of the video frame
        # and is otherwise invisible, so playback stays clean until you interact.
        #   _video_boxes    vid -> (l, top, r, bottom) px, refreshed each paint
        #                   so the mouse handlers can hit-test the scrubber.
        #   _controls_until controls (scrubber + time) stay up until this
        #                   monotonic time; refreshed on every hover move so they
        #                   auto-hide a couple seconds after the mouse stops.
        #   _seek_flash_until  a bare timestamp + red progress line flashes until
        #                   here after a keyboard/scrub seek, even without hover.
        self._video_boxes = {}
        self._video_hover = None  # vid under the cursor, or None
        self._video_hover_px = None  # cursor x (px) for the scrub-preview time
        self._controls_until = 0.0
        self._seek_flash_until = 0.0
        # Scrub-preview thumbnails: the decoder posts the latest frame here; the
        # renderer uploads it to a GPU texture lazily during paint.
        self._thumbs = {}  # vid -> (rgba, w, h, seq)
        self._thumb_seq = 0

        # Damage tracking. The renderer only rebuilds the screen's cached
        # geometry when this flag says something changed; the widget sets it
        # (on input, resize, config change) and the renderer clears it after a
        # rebuild.
        self._dirty = True
        self._last_cursor_state = None

        # Text selection, stored as absolute (buffer_line, col) so it stays
        # pinned to content while scrolling. None = no selection.
        self.sel_anchor = None
        self.sel_focus = None
        self._selecting = False
        self._last_move_cell = None  # last cell a motion event was reported for

        # OSC 8 hyperlinks: which target (if any) the mouse currently sits
        # over, so we can show a pointing-hand cursor and underline it --
        # Ctrl+click opens it. Always tracked regardless of whether an app
        # has mouse reporting on, since it never sends anything to the shell.
        self._hover_href = None
        self.setMouseTracking(True)

    @property
    def config(self):
        """The live settings the renderer reads each frame. Resolved through the
        module global so a settings change applies everywhere at once -- and
        exposed on the widget (rather than imported) so renderer.py needn't
        depend on app.py, which would be a circular import."""
        return CONFIG

    # ------------------------------------------------------------ GL lifecycle

    def initializeGL(self):
        # Qt made this widget's GL context current; the renderer adopts it
        # and builds every GPU resource (see renderer.py).
        self.renderer = Renderer(self)

        # Terminal grid + shell. Grid is sized in *logical* pixels so text
        # keeps a consistent visual size across DPIs.
        self.win_w, self.win_h = self.width(), self.height()
        cols = max(1, self.win_w // self.cell_w)
        rows = max(1, self.win_h // self.cell_h)
        self.term = Terminal(cols, rows)
        self.term.cursor.shape = CONFIG.shape()  # apps can still override it
        self.term.cell_px = (self.cell_w, self.cell_h)  # image sizing needs it

        try:
            self.pty = PtyProcess.spawn(
                [CONFIG.shell], dimensions=(rows, cols), env=shell_env()
            )
        except Exception as exc:
            # A configured shell that isn't installed shouldn't take the tab
            # down with a traceback — say so on the screen instead.
            self.pty = None
            self.term.write(
                "\x1b[31mYoTerm: couldn't start %s\x1b[0m\r\n  %s\r\n\r\n"
                "Pick another shell in Settings (Ctrl+,), then open a new tab.\r\n"
                % (CONFIG.shell, exc),
                end="",
            )

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
        self.renderer.paint()

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

        # Send any replies the terminal owes the shell (e.g. a DSR cursor-position
        # report the shell is blocking on) BEFORE servicing videos or painting.
        # Under video load a tick can run for tens-to-hundreds of ms, and a query
        # requester (like yt_seq_tests' cursor_pos) times out fast -- so the reply
        # must not wait behind that per-frame work.
        if self.term and self.term.responses:
            for reply in self.term.responses:
                self._write_pty(reply)
            self.term.responses.clear()

        self._service_videos()

        if self._shell_exited:
            self._timer.stop()
            self._stop_all_videos()
            self.exited.emit()  # the window closes the tab, not itself
            return

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

        # Mouse tracking is always on now (set once in __init__): bare motion
        # is needed both for an app's ?1003 any-motion reporting and for
        # OSC 8 hyperlink hover, so there's no mode-dependent toggle anymore.

        # Only repaint when something actually changed: the screen, the caret,
        # or an animated gradient. Note update(), *not* _invalidate(): a cycling
        # gradient only needs new per-corner colours (recomputed in paintGL),
        # not a full geometry rebuild, so marking the screen dirty here would
        # throw away the cached geometry every single frame for nothing.
        if (
            self._dirty
            or self._has_cycle
            or self._cursor_state() != self._last_cursor_state
        ):
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

    def _service_videos(self):
        """Start newly-requested YT;vid videos and stop ones whose placement is
        gone (del, screen clear, scrolled out of history, RIS). Called each tick."""
        term = self.term
        if term is None:
            return
        if term.video_requests:
            requests, term.video_requests = term.video_requests, []
            for req in requests:
                self._start_video(req)
        if self._videos:
            live = {im.id for im in term.images}
            for vid in list(self._videos):
                if vid not in live:
                    self._videos[vid].stop()  # finished handler cleans up

    def _start_video(self, req):
        vid = req["id"]
        if vid in self._videos:  # a replacing YT;vid with the same id
            self._videos.pop(vid).stop()
        box = (max(1, req["cols"] * self.cell_w), max(1, req["rows"] * self.cell_h))
        ctrl = _VideoController(
            vid,
            req["path"],
            box,
            req["loop"],
            mute=req.get("mute", False) or self._muted,
            parent=self,
        )
        ctrl.frameReady.connect(self._on_video_frame, Qt.QueuedConnection)
        ctrl.finished.connect(self._on_video_finished, Qt.QueuedConnection)
        ctrl.thumbReady.connect(self._on_thumb, Qt.QueuedConnection)
        self._videos[vid] = ctrl
        ctrl.start()
        if req.get("fullscreen") and self._fullscreen_vid is None:
            self._toggle_fullscreen(vid)  # start filling the whole terminal

    @QtCore.Slot(int, float, object, int, int)
    def _on_thumb(self, vid, req_s, rgba, w, h):
        """A scrub-preview frame arrived (GUI thread): stash the pixels; the GPU
        texture is (re)built lazily during the next paint (where the GL context
        is current). Ignored if the video is already gone."""
        if vid not in self._videos:
            return
        self._thumb_seq += 1
        self._thumbs[vid] = (rgba, w, h, self._thumb_seq)
        self.update()

    @QtCore.Slot(int, object, int, int)
    def _on_video_frame(self, img_id, rgba, w, h):
        """A decoded frame arrived (GUI thread): swap it into the placement's
        pixels in place and repaint. rev bumps so the texture cache re-uploads."""
        for im in self.term.images:
            if im.id == img_id:
                im.rgba = rgba
                im.iw = w
                im.ih = h
                im.rev += 1
                self.update()
                return

    @QtCore.Slot(int)
    def _on_video_finished(self, img_id):
        ctrl = self._videos.pop(img_id, None)
        if ctrl is not None:
            ctrl.deleteLater()
        if img_id == self._fullscreen_vid:  # its window is gone; leave fullscreen
            self._fullscreen_vid = None
        self._fs_saved_box.pop(img_id, None)
        self.update()  # take down the pause indicator if it was showing

    def _stop_all_videos(self):
        for ctrl in list(self._videos.values()):
            ctrl.stop()

    def _toggle_videos(self):
        for ctrl in self._videos.values():
            ctrl.toggle()
        self.update()

    def _mute_videos(self):
        """Toggle audio for every playing video (the `m` key). The state sticks
        so videos started later inherit it, matching what you'd expect after
        muting a tab."""
        self._muted = not self._muted
        for ctrl in self._videos.values():
            ctrl.set_muted(self._muted)
        self.update()

    def _toggle_fullscreen(self, vid=None):
        """Toggle a video filling the entire terminal (the `f` key). Entering
        re-decodes at the viewport size for a crisp picture; exiting restores the
        inline box. Only one video is fullscreen at a time."""
        if self._fullscreen_vid is not None:  # exit
            v = self._fullscreen_vid
            self._fullscreen_vid = None
            ctrl = self._videos.get(v)
            box = self._fs_saved_box.pop(v, None)
            if ctrl is not None and box is not None:
                ctrl.resize_box(box)
            self.update()
            return
        # enter: prefer the video under the cursor, else the only/first one
        if vid is None or vid not in self._videos:
            vid = self._video_hover if self._video_hover in self._videos else None
        if vid is None:
            vid = next(iter(self._videos), None)
        ctrl = self._videos.get(vid) if vid is not None else None
        if ctrl is None:
            return
        im = next((i for i in self.term.images if i.id == vid), None)
        if im is not None:
            self._fs_saved_box[vid] = (
                max(1, im.cols * self.cell_w),
                max(1, im.rows * self.cell_h),
            )
        self._fullscreen_vid = vid
        ctrl.resize_box((max(1, self.win_w), max(1, self.win_h)))
        self._controls_until = time.monotonic() + 2.5
        self.update()

    def _restart_videos(self):
        for ctrl in self._videos.values():
            ctrl.restart()
        self.update()

    def _step_videos(self):
        for ctrl in self._videos.values():
            ctrl.step()
        self.update()

    def _seek_videos(self, delta):
        for ctrl in self._videos.values():
            ctrl.seek_relative(delta)
        self._flash_seek()

    def _seek_percent_videos(self, fraction):
        for ctrl in self._videos.values():
            ctrl.seek_percent(fraction)
        self._flash_seek()

    def _flash_seek(self):
        """Briefly surface the timestamp + red progress line after a seek, the
        way YouTube flashes the scrubber when you jump — even without a hover."""
        self._seek_flash_until = time.monotonic() + 1.4
        self.update()

    def _quit_videos(self):
        """Stop playback and remove the placements, so the video vanishes (what
        a viewer expects from 'quit', unlike pause which leaves the frame up)."""
        ids = set(self._videos)
        for ctrl in self._videos.values():
            ctrl.stop()
        if ids:
            self.term.images = [im for im in self.term.images if im.id not in ids]
        self.update()

    # ---- native video: a YouTube-style overlay -------------------------
    #
    # Everything below draws on top of the live video frame and is invisible
    # during untouched playback, so the picture stays clean. It surfaces on
    # interaction, the way YouTube's controls do:
    #   * a modern round pause glyph while paused;
    #   * a scrubber (grey track + red progress + handle) and a time readout
    #     that fade in on hover and auto-hide a couple seconds after the mouse
    #     stops;
    #   * a scrub-preview timestamp that tracks the cursor along the bar;
    #   * a bare timestamp + red line that flashes on a keyboard/click seek.
    #
    # The instanced clip pipeline only draws axis-aligned quads, so rounded
    # elements (glyph scrim, handle, chips) use its per-draw rounded-clip test
    # (u_clip_round) — one small draw each, which is nothing for the handful of
    # widgets an overlay has.

    OV_TRACK = (0.52, 0.52, 0.56)  # unfilled scrubber
    OV_RED = (0.92, 0.11, 0.16)  # progress + handle (YouTube red)
    OV_WHITE = (0.95, 0.95, 0.97)  # text / pause bars
    OV_DARK = (0.08, 0.08, 0.10)  # scrims behind glyphs/text

    @staticmethod
    def _fmt_time(seconds):
        seconds = max(0, int(seconds + 0.5))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _video_boxes_px(self):
        """[(vid, left, top, right, bottom)] in logical px for the on-screen
        videos — the *visible* picture rect, not the (often taller) reserved
        cell box: a `contain` frame letterboxes inside its cells, so the overlay
        must hug the image, or the scrubber floats in the black bars below it.
        Shared by the renderer and the mouse hit-testing so both agree."""
        term = self.term
        if term is None or not self._videos:
            return []
        by_id = {im.id: im for im in term.images if im.alt == term.alt_screen}
        top_abs = term.first_line_no + len(term.scrollback) - term.scroll_offset
        out = []
        for vid in self._videos:
            im = by_id.get(vid)
            if im is None:
                continue
            row_top = im.top_line - top_abs
            l = im.left * self.cell_w
            r = (im.left + im.cols) * self.cell_w
            t = row_top * self.cell_h
            b = (row_top + im.rows) * self.cell_h
            if im.fit == "contain" and im.iw and im.ih:
                bw, bh = r - l, b - t
                img_a = im.iw / im.ih
                if bh > 0 and img_a > bw / bh:  # letterbox top/bottom
                    nh = bw / img_a
                    cy = (t + b) / 2
                    t, b = cy - nh / 2, cy + nh / 2
                elif bh > 0:  # pillarbox left/right
                    nw = bh * img_a
                    cx = (l + r) / 2
                    l, r = cx - nw / 2, cx + nw / 2
            out.append((vid, l, t, r, b))
        return out

    def _video_at(self, pos):
        """(vid, (l, t, r, b)) whose box contains a widget position, else
        (None, None). Uses the boxes cached at the last paint."""
        x, y = pos.x(), pos.y()
        for vid, (l, t, r, b) in self._video_boxes.items():
            if l <= x <= r and t <= y <= b:
                return vid, (l, t, r, b)
        return None, None

    @staticmethod
    def _scrubber_geom(l, t, r, b):
        """Track left/right/width, plus its centre-y and thickness (all px).
        Sizes are capped so the bar stays slim on a big video (YouTube-ish),
        not scaled up to a thick slab."""
        w, h = r - l, b - t
        margin = min(max(8.0, w * 0.04), 28.0)
        tl, tr = l + margin, r - margin
        th = min(max(3.0, h * 0.014), 5.0)
        ty = b - min(max(12.0, h * 0.06), 26.0)
        return tl, tr, max(1.0, tr - tl), ty, th

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
        if getattr(self, "renderer", None) is not None:
            self.renderer.rebuild_atlas()  # GL texture is the renderer's now
        self._resync_grid()  # fewer/more cells now fit; tell the shell
        self._invalidate()

    def _invalidate(self):
        """Mark the screen's geometry stale and ask for a repaint."""
        self._dirty = True
        self.update()

    def _cursor_state(self):
        """What the caret looks like right now. Compared frame to frame so a
        blinking caret repaints, but a still one doesn't."""
        if not CONFIG.cursor:
            return None  # nothing to repaint for
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
            return 1.0 if phase < 0.5 else 0.0  # classic hard blink
        level = 0.5 + 0.5 * math.cos(2.0 * math.pi * phase)  # 1 -> 0 -> 1
        for _ in range(2):  # smoothstep twice: ease the edges,
            level = level * level * (3.0 - 2.0 * level)  # flatten the holds
        return level

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

    def _cell_at(self, pos):
        """Map a widget position (logical px) to absolute (line, col)."""
        col = int(pos.x() // self.cell_w)
        row = int(pos.y() // self.cell_h)
        col = max(0, min(col, self.term.width))
        row = max(0, min(row, self.term.height - 1))
        return (self.term.visible_top() + row, col)

    # ---- OSC 8 hyperlinks ----------------------------------------------

    _LINK_SCHEMES = ("http", "https", "mailto", "ftp", "ftps", "file")

    def _href_at(self, pos):
        """The OSC 8 target under a widget position, or None."""
        line, col = self._cell_at(pos)
        row = self.term.line_at(line)
        if row is None or not (0 <= col < len(row)):
            return None
        return row[col].href

    def _update_hover(self, pos):
        """Track which link (if any) the mouse sits over: a pointing-hand
        cursor and an underline (drawn fresh each frame, not baked into the
        cached screen geometry, since it changes on every mouse move)."""
        href = self._href_at(pos)
        if href != self._hover_href:
            self._hover_href = href
            self.setCursor(Qt.PointingHandCursor) if href else self.unsetCursor()
            self._invalidate()

    def _open_link(self, href):
        """Ctrl+click on hyperlinked text opens it with the OS's normal
        handler for that URL scheme via QDesktopServices -- never a shell, so
        the URL text is never interpreted as a command. Restricted to a small
        allowlist of schemes as a cheap defence against something unexpected
        being registered for an exotic one."""
        scheme = href.split(":", 1)[0].lower() if ":" in href else ""
        if scheme not in self._LINK_SCHEMES:
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(href))

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
        if event.button() == Qt.LeftButton and self._try_scrubber_seek(
            event.position()
        ):
            return  # clicked the video's scrubber: seek, don't select
        if event.button() == Qt.LeftButton:
            # Always start a potential selection, even over linked text --
            # mouseReleaseEvent tells a plain click (open the link) apart
            # from a drag (finish a normal text selection) by whether the
            # cell actually moved, so dragging to copy link text still works.
            self._selecting = True
            cell = self._cell_at(event.position())
            self.sel_anchor = cell
            self.sel_focus = cell
            self._invalidate()

    def mouseMoveEvent(self, event):
        self._update_hover(event.position())  # never sent to the shell
        self._update_video_hover(event.position())
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if self._report_mouse(event, shift):
            return
        if self._selecting:
            self.sel_focus = self._cell_at(event.position())
            self._invalidate()

    def _update_video_hover(self, pos):
        """Track the video (if any) under the cursor and keep its controls
        alive, so hovering reveals the scrubber/time and a couple seconds after
        the mouse stops they auto-hide — the YouTube behaviour."""
        if not self._videos:
            return
        vid, box = self._video_at(pos)
        was = self._video_hover
        self._video_hover = vid
        self._video_hover_px = pos.x() if vid is not None else None
        if vid is not None:
            self._controls_until = time.monotonic() + 2.5
            ctrl = self._videos.get(vid)
            tl, tr, tw, _ty, _th = self._scrubber_geom(*box)
            if ctrl is not None and ctrl.duration and tl <= pos.x() <= tr:
                # Ask for the preview frame at the hovered time, quantised to a
                # quarter-second so a slow drag doesn't spam the thumbnailer.
                frac = (pos.x() - tl) / tw
                ctrl.request_thumb(round(frac * ctrl.duration * 4) / 4)
        if vid is not None or was is not None:
            self.update()

    def leaveEvent(self, event):
        if self._hover_href is not None:
            self._hover_href = None
            self.unsetCursor()
            self._invalidate()
        if self._video_hover is not None:
            self._video_hover = None
            self._video_hover_px = None
            self.update()
        super().leaveEvent(event)

    def _try_scrubber_seek(self, pos):
        """A click on a visible video scrubber seeks there (click-to-seek). Only
        fires while the controls are up and within a band around the track, so
        ordinary clicks/selection elsewhere are untouched. Returns True if it
        consumed the click."""
        vid, box = self._video_at(pos)
        if vid is None:
            return False
        ctrl = self._videos.get(vid)
        if ctrl is None:
            return False
        controls_up = ctrl.paused or time.monotonic() < self._controls_until
        if not controls_up:
            return False  # controls hidden: not a scrubber interaction
        tl, tr, tw, ty, th = self._scrubber_geom(*box)
        x, y = pos.x(), pos.y()
        if not (tl <= x <= tr and ty - th * 4 <= y <= ty + th * 4):
            return False
        ctrl.seek_percent((x - tl) / tw)
        self._controls_until = time.monotonic() + 2.5
        self._flash_seek()
        return True

    def mouseReleaseEvent(self, event):
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if self._report_mouse(event, shift):
            return
        if event.button() == Qt.LeftButton and self._selecting:
            self._selecting = False
            self.sel_focus = self._cell_at(event.position())
            # A plain click -- press and release on the same cell, no drag
            # in between -- opens hyperlinked text under it instead of
            # leaving the zero-length "selection" a same-cell click would
            # otherwise be. A drag still selects normally either way.
            if self.sel_anchor == self.sel_focus:
                href = self._href_at(event.position())
                if href:
                    self._open_link(href)
            self._invalidate()

    # ------------------------------------------------------------ Input

    # DECKPAM: with the keypad in application mode the numeric keys send these
    # instead of digits. Programs bind against them, so a stored flag that
    # nothing reads is worse than useless — it looks implemented.
    _KEYPAD_APP = {
        Qt.Key_0: "\x1bOp",
        Qt.Key_1: "\x1bOq",
        Qt.Key_2: "\x1bOr",
        Qt.Key_3: "\x1bOs",
        Qt.Key_4: "\x1bOt",
        Qt.Key_5: "\x1bOu",
        Qt.Key_6: "\x1bOv",
        Qt.Key_7: "\x1bOw",
        Qt.Key_8: "\x1bOx",
        Qt.Key_9: "\x1bOy",
        Qt.Key_Period: "\x1bOn",
        Qt.Key_Comma: "\x1bOl",
        Qt.Key_Plus: "\x1bOk",
        Qt.Key_Minus: "\x1bOm",
        Qt.Key_Asterisk: "\x1bOj",
        Qt.Key_Slash: "\x1bOo",
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

        # Playback controls while a native video is on screen — consumed rather
        # than sent to the shell. Gated on a video being active, so ordinary
        # typing is untouched the rest of the time.
        #   space  pause / resume        r   restart from the top
        #   .      step one frame        q / Esc  quit (remove the video)
        #   -> / <-  seek +/- 5s         0-9  seek to 0%..90%
        #   m      mute / unmute audio
        if self._videos and not ctrl and not shift:
            if key == Qt.Key_Space:
                self._toggle_videos()
                return
            if key == Qt.Key_M:
                self._mute_videos()
                return
            if key == Qt.Key_F:
                self._toggle_fullscreen()
                return
            if key == Qt.Key_R:
                self._restart_videos()
                return
            if key == Qt.Key_Period:
                self._step_videos()
                return
            if key == Qt.Key_Right:
                self._seek_videos(5.0)
                return
            if key == Qt.Key_Left:
                self._seek_videos(-5.0)
                return
            if Qt.Key_0 <= key <= Qt.Key_9:
                self._seek_percent_videos((key - Qt.Key_0) / 10.0)
                return
            if key == Qt.Key_Escape and self._fullscreen_vid is not None:
                self._toggle_fullscreen()  # Esc leaves fullscreen first
                return
            if key in (Qt.Key_Q, Qt.Key_Escape):
                self._quit_videos()
                return

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
        self._editors = {}  # name -> (get, set)

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
            "hand too." % config_path()
        )
        note.setObjectName("hint")
        note.setWordWrap(True)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok
            | QtWidgets.QDialogButtonBox.Cancel
            | QtWidgets.QDialogButtonBox.RestoreDefaults
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QtWidgets.QDialogButtonBox.RestoreDefaults).clicked.connect(
            self._restore_defaults
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    @staticmethod
    def _editor(spec, value):
        """(widget, getter, setter) for one field."""
        meta = spec.metadata
        if isinstance(spec.default, bool):  # before int: bool is an int
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
            ("Ctrl++", lambda: self._zoom(1)),  # the shifted key, on some layouts
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
                self, "YoTerm", "Couldn't save your settings:\n\n%s" % exc
            )
            return
        CONFIG = new
        for i in range(self.stack.count()):
            self.stack.widget(i).apply_config()

    def edit_config_file(self):
        """Hand the file to whatever the OS opens .py with."""
        path = config_path()
        if not os.path.exists(path):
            try:
                path = config_module.save(CONFIG)  # something to actually edit
            except OSError as exc:
                QtWidgets.QMessageBox.warning(
                    self, "YoTerm", "Couldn't create %s:\n\n%s" % (path, exc)
                )
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
            window,
            "YoTerm settings",
            "Your settings file couldn't be used in full, so defaults were "
            "applied where needed:\n\n%s\n\n%s" % (problem, config_path()),
        )

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
