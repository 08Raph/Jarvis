"""The HUD widget: the glowing core, concentric rings, particles and waveform of the Home screen. It only PAINTS what `jarvis.ui.hud_model` computes.

STATUS: written and covered by tests that build and render it on Qt's offscreen platform wherever PySide6 is installed (the Windows CI job). It has not been looked at
by a person on a real screen, and it has never been shown with a real microphone or speaker.

The widget asks a `provider` once per frame for (state, microphone level, wake age). The provider reads only in-memory values (no file, no device), so a frame costs a few
dozen paint calls. The timer stops while the widget is hidden or the window is minimised, and runs at ~15 fps in quiet states."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from jarvis.ui import hud_model as M


def _c(key: str, alpha: int = 255) -> QColor:
    c = QColor(M.HUD.get(key, key))
    c.setAlpha(max(0, min(255, int(alpha))))
    return c


class HudWidget(QWidget):
    stateChanged = Signal(str)

    def __init__(self, provider=None, parent=None, reduce_motion: bool | None = None):
        super().__init__(parent)
        self.setObjectName("hudCore")
        self.setMinimumSize(300, 300)
        self.setAccessibleName("Jarvis status display")
        self._provider = provider or (lambda: (M.OFF, 0.0, None))
        self.model = M.Hud()
        self._state = M.OFF
        self._reduce = M.reduce_motion_requested() if reduce_motion is None else bool(reduce_motion)
        self._last = time.monotonic()
        self.frames = 0                      # frames advanced (tests and the self-test read it)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    # ------------------------------------------------------------ timing
    @property
    def state(self) -> str:
        return self._state

    def _can_animate(self) -> bool:
        if not self.isVisible():
            return False
        w = self.window()
        return not (w is not None and w.isMinimized())

    def _reschedule(self) -> None:
        ms = M.frame_interval_ms(self._state, self._can_animate(), self._reduce)
        if ms <= 0:
            self.timer.stop()
            return
        if self.timer.interval() != ms or not self.timer.isActive():
            self.timer.start(ms)

    def showEvent(self, e):
        super().showEvent(e)
        self._last = time.monotonic()
        self._reschedule()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.timer.stop()

    def _tick(self) -> None:
        if not self._can_animate():
            self.timer.stop()
            return
        now = time.monotonic()
        dt, self._last = now - self._last, now
        self.step(dt)

    def step(self, dt: float) -> None:
        """One frame: read the provider, advance the animation, repaint. (The timer calls this; tests call it directly.)"""
        try:
            st, rms, age = self._provider()
            if st not in M.LOOKS:
                st = self._state
        except Exception:  # noqa: BLE001 - a broken provider keeps the last state and a zero level; it never claims activity
            st, rms, age = self._state, 0.0, None
        if st != self._state:
            self._state = st
            self.stateChanged.emit(st)
            self._reschedule()
        self.model.advance(dt, st, rms, age)
        self.frames += 1
        self.update()

    # ------------------------------------------------------------ painting
    def paintEvent(self, ev):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._paint(p)
        finally:
            p.end()

    def _paint(self, p: QPainter) -> None:
        w, h = self.width(), self.height()
        m = self.model
        look = M.LOOKS[m.state]
        key = look.accent
        p.fillRect(0, 0, w, h, _c("bg"))
        cx, cy = w / 2.0, h / 2.0
        R = max(40.0, min(w, h) / 2.0 - 6.0)
        centre = QPointF(cx, cy)
        # soft glow behind everything
        g = QRadialGradient(centre, R)
        a = int(80 * m.glow + 90 * m.flash)
        g.setColorAt(0.0, _c(key, a))
        g.setColorAt(0.55, _c(key, a // 4))
        g.setColorAt(1.0, _c("bg", 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(centre, R, R)
        # concentric rings: a faint full circle plus bright arcs that turn at their own speed and direction
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i, (rf, wd, mult, seg, arc) in enumerate(M.RINGS):
            r = R * rf
            rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
            p.setPen(QPen(_c("accent_dim", 90), 1.0))
            p.drawEllipse(centre, r, r)
            bright = int(150 + 80 * m.glow + 25 * m.flash)
            pen = QPen(_c(key, bright), wd)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            base = (m.ring_deg * mult) % 360.0
            for k in range(seg):
                start = base + k * 360.0 / seg
                p.drawArc(rect, int(-start * 16), int(-arc * 16))
        # particles
        p.setPen(Qt.PenStyle.NoPen)
        for ang, rf, size, alpha in m.particles.points():
            rad = math.radians(ang)
            p.setBrush(_c("core", int(255 * alpha * 0.8)))
            p.drawEllipse(QPointF(cx + R * rf * math.cos(rad), cy + R * rf * math.sin(rad)), size, size)
        # waveform: mirrored bars from the core outwards (real microphone level, or a labelled pattern, or flat)
        cr = R * 0.22 * m.core_scale()
        x0, x1 = cx + cr * 1.15, cx + R * 0.98
        step = (x1 - x0) / M.Wave.SIZE
        base_pen = QPen(_c(key, 60), 1.0)
        p.setPen(base_pen)
        p.drawLine(QPointF(cx - R * 0.98, cy), QPointF(cx + R * 0.98, cy))
        bar = QPen(_c(key, 230), max(1.5, step * 0.55))
        bar.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(bar)
        for i, v in enumerate(m.wave.heights()):
            if v <= 0.004:
                continue
            hh = max(1.0, v * R * 0.26)
            dx = (i + 0.5) * step
            p.drawLine(QPointF(x0 + dx - step * 0.5, cy - hh), QPointF(x0 + dx - step * 0.5, cy + hh))
            p.drawLine(QPointF(2 * cx - (x0 + dx - step * 0.5), cy - hh), QPointF(2 * cx - (x0 + dx - step * 0.5), cy + hh))
        # the core
        g2 = QRadialGradient(centre, cr * 1.9)
        g2.setColorAt(0.0, _c("core", int(255 * min(1.0, m.glow + 0.25))))
        g2.setColorAt(0.5, _c(key, int(170 * m.glow + 60 * m.flash)))
        g2.setColorAt(1.0, _c(key, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g2))
        p.drawEllipse(centre, cr * 1.9, cr * 1.9)
        p.setBrush(_c("bg", 225))
        p.setPen(QPen(_c(key, 235), 2.0))
        p.drawEllipse(centre, cr, cr)
        f = QFont(self.font())
        f.setPixelSize(max(10, int(cr * 0.36)))
        f.setBold(False)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, max(1.0, cr * 0.05))
        p.setFont(f)
        p.setPen(_c("text"))
        p.drawText(QRectF(cx - cr, cy - cr, 2 * cr, 2 * cr), Qt.AlignmentFlag.AlignCenter, "JARVIS")
