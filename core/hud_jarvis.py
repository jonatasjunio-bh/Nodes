"""
core/hud_jarvis.py — the HUD centrepiece styled after the film's J.A.R.V.I.S.

A particle sphere turning in 3-D inside a ring of glowing particles, framed by
rotating segmented rings and a spectrum ring that is the real audio level.

Everything is drawn with QPainter (works on the CPU raster engine and on the
OpenGL widget alike). The particles are batched by depth into a handful of
drawPoints() calls instead of one call per particle, which is what keeps ~900
particles at 30 fps cheap in Python.

What moves means something, as with the other centrepieces:
  - rotation speed      follows the state (idle slow, thinking fast)
  - sphere radius/glow  breathe with the voice level
  - spectrum ring       is the live audio level, mic or voice
  - colour accent       is the state colour (listening, thinking, speaking…)
"""

from __future__ import annotations

import math
import random

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QPainter, QPen, QPixmap, QPolygonF,
                         QRadialGradient)

_SPHERE_N = 640
_RING_N = 700
_SPECTRUM_N = 120
_BUCKETS = 7


def _fib_sphere(n: int) -> list[tuple[float, float, float]]:
    pts = []
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(n):
        y = 1.0 - 2.0 * (i + 0.5) / n
        rr = math.sqrt(max(0.0, 1.0 - y * y))
        th = golden * i
        pts.append((math.cos(th) * rr, y, math.sin(th) * rr))
    return pts


def _with_alpha(c: QColor, a: float) -> QColor:
    q = QColor(c)
    q.setAlphaF(max(0.0, min(1.0, a)))
    return q


def _mix(a: QColor, b: QColor, k: float) -> QColor:
    k = max(0.0, min(1.0, k))
    return QColor(int(a.red() + (b.red() - a.red()) * k),
                  int(a.green() + (b.green() - a.green()) * k),
                  int(a.blue() + (b.blue() - a.blue()) * k))


class JarvisOrb:
    def __init__(self):
        rnd = random.Random(7)
        self._sphere = _fib_sphere(_SPHERE_N)
        # the particle ring: angle, radial jitter, twinkle phase, size
        self._ring = [(rnd.uniform(0, math.tau), rnd.gauss(0.0, 0.028),
                       rnd.uniform(0, math.tau), rnd.uniform(0.6, 1.0)) for _ in range(_RING_N)]
        self._spec_noise = [rnd.uniform(0.35, 1.0) for _ in range(_SPECTRUM_N)]
        self._segments = [  # (radius k, start deg, span deg, width, direction)
            (0.905, 10, 70, 3.2, 1), (0.905, 120, 40, 3.2, 1), (0.905, 200, 95, 3.2, 1),
            (0.905, 320, 25, 3.2, 1),
            (0.845, 40, 110, 1.6, -1), (0.845, 230, 60, 1.6, -1),
        ]
        self.t = 0.0          # integrated animation time
        self.spin = 0.0       # sphere rotation
        self.amp = 0.0
        self.spec = [0.0] * _SPECTRUM_N
        self.state = "IDLE"
        self.speaking = False
        self.muted = False
        self._static: QPixmap | None = None
        self._static_key = None

    # ── animation ────────────────────────────────────────────────────────────

    def step(self, dt: float, amp: float, speaking: bool = False, muted: bool = False,
             state: str = "IDLE") -> None:
        dt = max(0.0, min(0.1, dt))
        self.state, self.speaking, self.muted = state, speaking, muted
        busy = state in ("THINKING", "PROCESSING")
        rate = 0.25 if muted else (1.6 if busy else (1.1 if speaking else 0.55))
        self.t += dt * rate
        self.spin += dt * (0.18 if muted else (0.9 if busy else 0.45 + amp * 0.8))
        self.amp += (amp - self.amp) * 0.35
        for i in range(_SPECTRUM_N):
            wave = 0.5 + 0.5 * math.sin(self.t * 5.0 + i * 0.55) * math.sin(self.t * 2.1 + i * 0.13)
            target = self.amp * self._spec_noise[i] * (0.45 + 0.55 * wave)
            if not speaking and amp < 0.03:
                target = 0.04 + 0.03 * math.sin(self.t * 1.7 + i * 0.4)
            self.spec[i] += (target - self.spec[i]) * 0.4

    # ── painting ─────────────────────────────────────────────────────────────

    def paint(self, p: QPainter, cx: float, cy: float, r: float,
              main: QColor, acc: QColor, bg: QColor, name: str = "",
              W: float = 0.0, H: float = 0.0) -> None:
        if r < 20:
            return
        white = QColor(235, 247, 255)
        amp = 0.0 if self.muted else self.amp

        # Everything that doesn't move — the deep glow, the outer rings and
        # their ticks, the crosshair, the canvas brackets — is drawn once into
        # a pixmap and blitted. It was half the cost of a frame.
        key = (int(cx), int(cy), int(r), main.rgba(), int(W), int(H))
        if self._static is None or self._static_key != key:
            self._static = self._build_static(cx, cy, r, main, white, W, H)
            self._static_key = key
        p.drawPixmap(0, 0, self._static)

        self._paint_frame(p, cx, cy, r, main, acc, white)
        self._paint_spectrum(p, cx, cy, r, main, acc, white)
        self._paint_ring(p, cx, cy, r, main, white)
        self._paint_sphere(p, cx, cy, r, main, acc, white)

        # hot core
        core_r = r * (0.16 + amp * 0.07)
        g2 = QRadialGradient(QPointF(cx, cy), core_r)
        g2.setColorAt(0.0, _with_alpha(white, 0.85))
        g2.setColorAt(0.35, _with_alpha(_mix(main, white, 0.4), 0.55))
        g2.setColorAt(1.0, _with_alpha(main, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g2))
        p.drawEllipse(QPointF(cx, cy), core_r, core_r)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _build_static(self, cx, cy, r, main, white, W, H) -> QPixmap:
        w, h = max(1, int(W or cx * 2)), max(1, int(H or cy * 2))
        pm = QPixmap(w, h)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QRadialGradient(QPointF(cx, cy), r * 1.25)
        g.setColorAt(0.0, _with_alpha(main, 0.32))
        g.setColorAt(0.35, _with_alpha(main, 0.13))
        g.setColorAt(1.0, _with_alpha(main, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), r * 1.25, r * 1.25)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # the particle ring's light: wide, faint strokes (costly — drawn once)
        for w, a in ((r * 0.11, 0.08), (r * 0.06, 0.11), (r * 0.025, 0.20)):
            p.setPen(QPen(_with_alpha(main, a), w))
            p.drawEllipse(QPointF(cx, cy), r * 0.60, r * 0.60)
        p.setPen(QPen(_with_alpha(main, 0.55), 1.2))
        p.drawEllipse(QPointF(cx, cy), r * 0.98, r * 0.98)
        p.setPen(QPen(_with_alpha(main, 0.25), 1.0))
        p.drawEllipse(QPointF(cx, cy), r * 1.04, r * 1.04)
        short, long_ = [], []
        for i in range(120):
            a = i * math.tau / 120
            lg = i % 10 == 0
            r0 = r * (0.955 if lg else 0.968)
            (long_ if lg else short).append(QLineF(
                QPointF(cx + math.cos(a) * r0, cy + math.sin(a) * r0),
                QPointF(cx + math.cos(a) * r * 0.98, cy + math.sin(a) * r * 0.98)))
        p.setPen(QPen(_with_alpha(main, 0.45), 1.0))
        p.drawLines(short)
        p.setPen(QPen(_with_alpha(white, 0.75), 1.6))
        p.drawLines(long_)
        p.setPen(QPen(_with_alpha(white, 0.6), 1.4))
        for a in (0, math.pi / 2, math.pi, 1.5 * math.pi):
            p.drawLine(QPointF(cx + math.cos(a) * r * 1.02, cy + math.sin(a) * r * 1.02),
                       QPointF(cx + math.cos(a) * r * 1.10, cy + math.sin(a) * r * 1.10))
        if W > 0 and H > 0:
            L, m = min(W, H) * 0.05, 18.0
            p.setPen(QPen(_with_alpha(main, 0.55), 1.6))
            for x, y, sx, sy in ((m, m, 1, 1), (W - m, m, -1, 1), (m, H - m, 1, -1), (W - m, H - m, -1, -1)):
                p.drawLine(QPointF(x, y), QPointF(x + sx * L, y))
                p.drawLine(QPointF(x, y), QPointF(x, y + sy * L))
        p.end()
        return pm

    def _paint_frame(self, p, cx, cy, r, main, acc, white):
        t = self.t
        p.setBrush(Qt.BrushStyle.NoBrush)
        # segmented arcs turning in opposite directions
        for k, start, span, width, d in self._segments:
            rr = r * k
            ang = start + d * t * 18.0
            p.setPen(QPen(_with_alpha(main, 0.9), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), int(-ang * 16), int(-span * 16))
        # accent arc: the state colour, sweeping
        rr = r * 0.875
        p.setPen(QPen(_with_alpha(acc, 0.95), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), int(-(t * 40.0) * 16), int(-48 * 16))
        p.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), int(-(t * 40.0 + 180) * 16), int(-48 * 16))
        # dotted ring
        p.setPen(QPen(_with_alpha(main, 0.6), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        dots = QPolygonF()
        rr = r * 0.80
        off = -t * 0.2
        for i in range(90):
            a = off + i * math.tau / 90
            dots.append(QPointF(cx + math.cos(a) * rr, cy + math.sin(a) * rr))
        p.drawPoints(dots)

    def _paint_spectrum(self, p, cx, cy, r, main, acc, white):
        """Bars batched by loudness into a few pens instead of one pen per bar."""
        r0 = r * 0.665
        groups: list[list] = [[] for _ in range(4)]
        for i, v in enumerate(self.spec):
            a = -math.pi / 2 + i * math.tau / _SPECTRUM_N
            ln = r * (0.012 + 0.11 * max(0.0, v))
            k = min(3, int(max(0.0, v) * 6))
            groups[k].append(QLineF(QPointF(cx + math.cos(a) * r0, cy + math.sin(a) * r0),
                                    QPointF(cx + math.cos(a) * (r0 + ln), cy + math.sin(a) * (r0 + ln))))
        for k, lines in enumerate(groups):
            if lines:
                f = k / 3.0
                p.setPen(QPen(_with_alpha(_mix(main, white, f * 0.8), 0.40 + 0.55 * f), 2.0,
                              Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLines(lines)

    def _paint_ring(self, p, cx, cy, r, main, white):
        """The ring of glowing particles around the sphere."""
        t = self.t
        amp = self.amp
        base = r * (0.60 + amp * 0.03)
        buckets = [QPolygonF() for _ in range(_BUCKETS)]
        rot = t * 0.08
        for ang, jit, ph, sz in self._ring:
            a = ang + rot
            rr = base * (1.0 + jit + 0.02 * math.sin(t * 2.0 + ph))
            tw = 0.5 + 0.5 * math.sin(t * 3.0 + ph)
            k = min(_BUCKETS - 1, int(tw * sz * _BUCKETS))
            buckets[k].append(QPointF(cx + math.cos(a) * rr, cy + math.sin(a) * rr))
        # (the band's soft light under the particles is in the static layer)
        for k, poly in enumerate(buckets):
            if poly.isEmpty():
                continue
            f = (k + 1) / _BUCKETS
            col = _mix(main, white, 0.25 + f * 0.75)
            p.setPen(QPen(_with_alpha(col, 0.45 + 0.55 * f), 1.6 + 3.0 * f,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(poly)
        # faint guide circles either side of the band
        p.setPen(QPen(_with_alpha(main, 0.25), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), base * 0.93, base * 0.93)
        p.drawEllipse(QPointF(cx, cy), base * 1.07, base * 1.07)

    def _paint_sphere(self, p, cx, cy, r, main, acc, white):
        amp = self.amp
        R = r * (0.44 + amp * 0.05)
        cy_, sy_ = math.cos(self.spin), math.sin(self.spin)
        tilt = 0.42 + 0.08 * math.sin(self.t * 0.3)
        cx_, sx_ = math.cos(tilt), math.sin(tilt)
        buckets = [QPolygonF() for _ in range(_BUCKETS)]
        pulse = self.t * 2.4
        for x, y, z in self._sphere:
            # spin around Y, then tilt around X
            x1 = x * cy_ + z * sy_
            z1 = -x * sy_ + z * cy_
            y2 = y * cx_ - z1 * sx_
            z2 = y * sx_ + z1 * cx_
            # a soft wave runs over the surface while speaking
            bump = 1.0 + amp * 0.10 * math.sin(pulse + y * 6.0)
            depth = (z2 + 1.0) * 0.5                     # 0 back … 1 front
            k = min(_BUCKETS - 1, int(depth * _BUCKETS))
            buckets[k].append(QPointF(cx + x1 * R * bump, cy + y2 * R * bump))
        for k, poly in enumerate(buckets):
            if poly.isEmpty():
                continue
            f = (k + 1) / _BUCKETS
            col = _mix(main, white, 0.15 + f * 0.7)
            if f > 0.85:
                col = _mix(col, acc, 0.25)
            p.setPen(QPen(_with_alpha(col, 0.22 + 0.78 * f * f), 1.2 + 3.0 * f,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(poly)
