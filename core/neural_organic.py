"""
core/neural_organic.py — the brain centrepiece, drawn as living tissue.

The same idea as core/neural.py (the assistant as a network at work) without
the mechanics: no straight wires and no spark jumping point to point. Here

    filaments   are soft curves that sway, each with its own slow rhythm
    pulses      glide along them, easing in and out, with a fading tail
    neurons     glow up when a pulse arrives and fade like an afterimage
    the network breathes, and swells with the voice

and how much happens follows what the assistant is doing:

    SLEEPING / muted   a slow drift, a pulse now and then, dim
    LISTENING          steady pulses, lifted by your voice
    THINKING           cascades: an arriving pulse often sets off the next
    SPEAKING           bursts driven by the assistant's own audio level

Same interface as NeuralField — step() advances, paint() draws — so HudCanvas
drives it unchanged. Same budget too: numpy for the state, and each layer drawn
in a handful of grouped calls (a path per brightness level, points per tail
step, cached glow sprites), because PyQt holds the GIL through every call and
hundreds of small ones per frame starve the audio threads.
"""

from __future__ import annotations

import math

import numpy as np
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient

from core.neural import _brain_points, _synapses

# per state: spontaneous pulses per second, extra per unit of audio level,
# chance an arriving pulse sets off another, and how fast everything moves
_STATE = {
    "SLEEPING":  dict(rate=0.4, gain=0.0, chain=0.05, tempo=0.45, dim=0.45),
    "MUTED":     dict(rate=0.5, gain=0.0, chain=0.05, tempo=0.5, dim=0.45),
    "IDLE":      dict(rate=1.6, gain=6.0, chain=0.12, tempo=0.8, dim=1.0),
    "LISTENING": dict(rate=2.2, gain=14.0, chain=0.15, tempo=0.9, dim=1.0),
    "THINKING":  dict(rate=7.0, gain=2.0, chain=0.42, tempo=1.35, dim=1.0),
    "SPEAKING":  dict(rate=3.0, gain=16.0, chain=0.2, tempo=1.1, dim=1.0),
}
_TAIL = 10                # samples in a pulse's tail
_TAIL_GAP = 0.018         # progress between tail samples (close enough to read as a streak)


class OrganicField:
    N = 64
    K = 3
    SPAN = 2.0
    MAX_PULSES = 70
    MAX_HALOS = 26

    def __init__(self, seed: int = 11):
        rng = np.random.default_rng(seed)
        self._rng = np.random.default_rng()
        self.base = _brain_points(self.N, rng)
        self.edges = _synapses(self.base, self.K, rng)
        self.n_edges = len(self.edges)
        self.adj: list[list[tuple[int, int]]] = [[] for _ in range(self.N)]
        for e, (i, j) in enumerate(self.edges):
            self.adj[i].append((e, j))
            self.adj[j].append((e, i))
        self.ph = rng.uniform(0, 2 * math.pi, (self.N, 3))      # sway phases per axis
        self.e_bend = rng.uniform(-0.45, 0.45, self.n_edges)     # resting curvature
        self.e_ph = rng.uniform(0, 2 * math.pi, self.n_edges)    # curvature rhythm
        self.glow = np.zeros(self.N)

        # pulses in flight
        self.p_edge = np.zeros(0, dtype=int)
        self.p_rev = np.zeros(0, dtype=bool)     # travelling j → i
        self.p_k = np.zeros(0)                    # progress 0..1
        self.p_life = np.zeros(0)                 # seconds to cross

        self.t = 0.0
        self.yaw = 0.0
        self.amp = 0.0
        self.state = "IDLE"
        self.muted = False
        self.direct_glow = False
        self._glow_pm = None
        self._glow_key = None
        self._halos: dict[tuple, QPixmap] = {}

    # ── simulation ──────────────────────────────────────────────────────────

    def _mode(self, speaking: bool, muted: bool, state: str) -> str:
        if muted:
            return "MUTED"
        if speaking:
            return "SPEAKING"
        if state in ("THINKING", "PROCESSING"):
            return "THINKING"
        return state if state in _STATE else "IDLE"

    def _spawn(self, nodes) -> None:
        """Start a pulse from each node, down a random filament of its own."""
        room = self.MAX_PULSES - len(self.p_edge)
        if room <= 0 or not len(nodes):
            return
        es, rv = [], []
        for i in list(nodes)[:room]:
            if not self.adj[i]:
                continue
            e, _j = self.adj[i][self._rng.integers(len(self.adj[i]))]
            es.append(e)
            rv.append(self.edges[e, 0] != i)
            self.glow[i] = min(1.0, self.glow[i] + 0.5)
        if not es:
            return
        n = len(es)
        self.p_edge = np.concatenate([self.p_edge, np.array(es, dtype=int)])
        self.p_rev = np.concatenate([self.p_rev, np.array(rv, dtype=bool)])
        self.p_k = np.concatenate([self.p_k, np.zeros(n)])
        self.p_life = np.concatenate([self.p_life, self._rng.uniform(0.8, 1.6, n)])

    def step(self, dt: float, amp: float, speaking: bool = False,
             muted: bool = False, state: str = "IDLE") -> None:
        dt = max(0.0, min(dt, 0.1))
        mode = self._mode(speaking, muted, state)
        cfg = _STATE[mode]
        self.state, self.muted = mode, muted
        self.t += dt * cfg["tempo"]
        self.yaw += dt * (0.22 if mode == "THINKING" else 0.11)
        self.amp += (float(amp) - self.amp) * min(1.0, dt * 9.0)
        self.glow *= math.exp(-dt * 2.6)

        # pulses glide; arrivals light their neuron and may carry on
        if len(self.p_edge):
            self.p_k += dt * cfg["tempo"] / self.p_life
            done = self.p_k >= 1.0
            if done.any():
                ends = np.where(self.p_rev[done], self.edges[self.p_edge[done], 0],
                                self.edges[self.p_edge[done], 1])
                np.add.at(self.glow, ends, 0.85)
                np.clip(self.glow, 0, 1, out=self.glow)
                keep = ~done
                self.p_edge, self.p_rev = self.p_edge[keep], self.p_rev[keep]
                self.p_k, self.p_life = self.p_k[keep], self.p_life[keep]
                load = len(self.p_edge) / self.MAX_PULSES
                onward = ends[self._rng.random(len(ends)) < cfg["chain"] * (1 - load)]
                self._spawn(onward)

        # spontaneous pulses
        rate = cfg["rate"] + cfg["gain"] * self.amp
        n_new = self._rng.poisson(rate * dt)
        if n_new:
            self._spawn(self._rng.integers(0, self.N, n_new))

    # ── drawing ─────────────────────────────────────────────────────────────

    def _positions(self, cx: float, cy: float, r: float):
        """Screen positions (swaying, breathing, turning) and depth 0..1."""
        t = self.t
        sway = 0.035 * np.sin(t * np.array([0.8, 0.7, 0.6]) + self.ph)
        breathe = 1.0 + 0.022 * math.sin(t * 0.9) + 0.06 * self.amp
        p = (self.base + sway) * breathe
        cyaw, syaw = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(0.32), math.sin(0.32)
        x, y, z = p[:, 0], p[:, 1], p[:, 2]
        x1 = x * cyaw + z * syaw
        z1 = -x * syaw + z * cyaw
        y1 = y * cp - z1 * sp
        z2 = y * sp + z1 * cp
        persp = 3.2 / (3.2 + z2)
        sx = cx + x1 * r * persp
        sy = cy - y1 * r * persp
        depth = np.clip((1.0 - z2) / 2.0, 0.0, 1.0)
        return sx, sy, depth

    def _controls(self, sx, sy):
        """Each filament's curve control point: the midpoint pushed sideways
        by its own slowly changing bend."""
        a, b = self.edges[:, 0], self.edges[:, 1]
        bend = self.e_bend + 0.18 * np.sin(self.t * 0.9 + self.e_ph)
        mx, my = (sx[a] + sx[b]) / 2, (sy[a] + sy[b]) / 2
        cx = mx - (sy[b] - sy[a]) * bend * 0.5
        cy = my + (sx[b] - sx[a]) * bend * 0.5
        return cx, cy

    def _halo(self, col: QColor, rad: int) -> QPixmap:
        key = (col.rgb(), rad)
        spr = self._halos.get(key)
        if spr is None:
            if len(self._halos) > 160:
                self._halos.clear()
            spr = QPixmap(2 * rad, 2 * rad)
            spr.fill(Qt.GlobalColor.transparent)
            hp = QPainter(spr)
            hp.setRenderHint(QPainter.RenderHint.Antialiasing)
            g = QRadialGradient(QPointF(rad, rad), rad)
            c0 = QColor(col); c0.setAlpha(170)
            c1 = QColor(col); c1.setAlpha(55)
            c2 = QColor(col); c2.setAlpha(0)
            g.setColorAt(0.0, c0); g.setColorAt(0.25, c1); g.setColorAt(1.0, c2)
            hp.setPen(Qt.PenStyle.NoPen); hp.setBrush(g)
            hp.drawEllipse(QPointF(rad, rad), rad, rad)
            hp.end()
            self._halos[key] = spr
        return spr

    def paint(self, p: QPainter, cx: float, cy: float, r: float,
              main: QColor, acc: QColor, bg: QColor) -> None:

        def mix(col: QColor, k: float, alpha: int = 255) -> QColor:
            k = max(0.0, min(1.0, k))
            return QColor(int(bg.red() + (col.red() - bg.red()) * k),
                          int(bg.green() + (col.green() - bg.green()) * k),
                          int(bg.blue() + (col.blue() - bg.blue()) * k), alpha)

        white = QColor(238, 247, 255)

        def lighten(col: QColor, k: float) -> QColor:
            return QColor(int(col.red() + (white.red() - col.red()) * k),
                          int(col.green() + (white.green() - col.green()) * k),
                          int(col.blue() + (white.blue() - col.blue()) * k))

        dim = _STATE[self.state]["dim"]
        sx, sy, depth = self._positions(cx, cy, r * 1.06)
        ccx, ccy = self._controls(sx, sy)
        a, b = self.edges[:, 0], self.edges[:, 1]

        # 1. atmosphere: one soft glow, breathing with the voice
        gr = int(r * 1.2)
        strength = int((34 + 30 * self.amp) * dim)
        if self.direct_glow:
            g = QRadialGradient(QPointF(cx, cy), gr)
            c0 = QColor(main); c0.setAlpha(strength)
            c1 = QColor(main); c1.setAlpha(0)
            g.setColorAt(0.0, c0); g.setColorAt(1.0, c1)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(g)
            p.drawEllipse(QPointF(cx, cy), gr, gr)
        else:
            key = (gr, main.rgb(), strength // 6)
            if self._glow_key != key:
                pm = QPixmap(2 * gr, 2 * gr)
                pm.fill(Qt.GlobalColor.transparent)
                gp = QPainter(pm)
                g = QRadialGradient(QPointF(gr, gr), gr)
                c0 = QColor(main); c0.setAlpha(strength)
                c1 = QColor(main); c1.setAlpha(0)
                g.setColorAt(0.0, c0); g.setColorAt(1.0, c1)
                gp.setPen(Qt.PenStyle.NoPen); gp.setBrush(g)
                gp.drawEllipse(QPointF(gr, gr), gr, gr)
                gp.end()
                self._glow_pm, self._glow_key = pm, key
            p.drawPixmap(QPointF(cx - gr, cy - gr), self._glow_pm)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # 2. filaments: curved, one path per brightness level
        e_depth = (depth[a] + depth[b]) / 2
        e_act = np.maximum(self.glow[a], self.glow[b])
        lvl = (0.14 + 0.24 * e_depth + 0.45 * e_act) * dim
        buckets = np.minimum((lvl * 7).astype(int), 6)
        for lv in range(7):
            sel = np.where(buckets == lv)[0]
            if not len(sel):
                continue
            path = QPainterPath()
            for e in sel:
                path.moveTo(sx[a[e]], sy[a[e]])
                path.quadTo(ccx[e], ccy[e], sx[b[e]], sy[b[e]])
            p.setPen(QPen(mix(main, (lv + 0.6) / 7), 1.0 if lv < 4 else 1.3))
            p.drawPath(path)

        # 3. pulses: eased glide along the curve, a fading tail behind
        if len(self.p_edge):
            e = self.p_edge
            u0 = np.where(self.p_rev, b[e], a[e])       # where the pulse started
            u1 = np.where(self.p_rev, a[e], b[e])
            x0, y0, x1, y1 = sx[u0], sy[u0], sx[u1], sy[u1]
            qx, qy = ccx[e], ccy[e]
            head = lighten(acc, 0.45)
            rad = max(1.6, r / 95)
            for s in range(_TAIL):
                k = np.clip(self.p_k - s * _TAIL_GAP, 0, 1)
                k = np.where(k < 0.5, 2 * k * k, 1 - (-2 * k + 2) ** 2 / 2)     # ease in-out
                # the control point is the same whichever way it travels
                u = 1 - k
                px = u * u * x0 + 2 * u * k * qx + k * k * x1
                py = u * u * y0 + 2 * u * k * qy + k * k * y1
                fade = (1 - s / _TAIL) * dim
                p.setPen(QPen(mix(head, 0.2 + 0.8 * fade), rad * 2 * (1 - s * 0.07),
                              Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawPoints([QPointF(px[i], py[i]) for i in range(len(e))])

        # 4. neurons: a faint glow around every one (nearer = brighter), a
        #    stronger one around those a pulse just reached, then the bodies
        soft_col = lighten(main, 0.1)
        soft_r = max(4, int(r / 26))
        soft = self._halo(soft_col, soft_r)
        for i in np.argsort(depth):
            p.setOpacity(float(0.10 + 0.22 * depth[i]) * dim)
            p.drawPixmap(QPointF(sx[i] - soft_r, sy[i] - soft_r), soft)
        p.setOpacity(1.0)
        hot = np.where(self.glow > 0.08)[0]
        if len(hot):
            hot = hot[np.argsort(self.glow[hot])[-self.MAX_HALOS:]]
            halo_col = lighten(acc, 0.2)
            for i in hot:
                gl = float(self.glow[i])
                hr = max(4, int(round(r / 40 * (1.2 + 2.2 * gl) * (0.7 + 0.5 * depth[i]))))
                p.setOpacity(min(1.0, (0.35 + 0.65 * gl) * dim))
                p.drawPixmap(QPointF(sx[i] - hr, sy[i] - hr), self._halo(halo_col, hr))
            p.setOpacity(1.0)
        size = np.clip(np.round((1.0 + 1.6 * depth) * max(0.6, r / 170) * 2) / 2, 0.5, 5)
        level = np.minimum(((0.35 + 0.5 * depth + 0.6 * self.glow) * dim * 5).astype(int), 4)
        groups: dict[tuple, list] = {}
        for i in range(self.N):
            groups.setdefault((int(level[i]), float(size[i])), []).append(QPointF(sx[i], sy[i]))
        for (lv, sz), pts in sorted(groups.items()):
            col = lighten(main, 0.15 * lv) if lv >= 3 else mix(main, 0.45 + lv * 0.18)
            p.setPen(QPen(col, sz * 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(pts)
