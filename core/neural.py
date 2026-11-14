"""
core/neural.py — the assistant drawn as a brain at work.

A third centrepiece for the HUD, beside the animated head and the reactor core:
a slowly turning brain of ~140 neurons wired to their neighbours, read out like
a scan. Nothing on it moves for decoration's sake — every flash is a neuron
firing, every travelling spark is a spike crossing a synapse, and how much of
that happens is set by what the assistant is actually doing:

    SLEEPING / muted   a few stray spikes, dim
    LISTENING          steady background firing, lifted by your voice
    THINKING           a wandering focus that sets off cascades across the cortex
    SPEAKING           bursts driven by the assistant's own audio level

The dynamics are a small integrate-and-fire network: input charges a neuron,
charge leaks away, a neuron over threshold fires, goes refractory for a moment
and sends spikes down its synapses, each of which adds charge where it lands.
That is what makes activity spread in waves instead of twinkling at random.

Same budget as the head: numpy for the state, QPainter for the picture, no GPU
and no extra package. The interface mirrors HoloAvatar — step() advances the
simulation, paint() draws it — so HudCanvas drives both the same way.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QRadialGradient


def _brain_points(n: int, rng: np.random.Generator) -> np.ndarray:
    """Neuron positions in a brain-shaped volume, roughly within the unit
    sphere. x = left/right, y = up/down, z = front/back. Most neurons sit in a
    shell near the surface (the cortex), the rest scattered inside."""

    def hemi_norm(p):
        side = np.sign(p[:, 0])
        side[side == 0] = 1
        c = np.stack([side * 0.31, np.full(len(p), 0.05), np.zeros(len(p))], axis=1)
        q = (p - c) / np.array([0.33, 0.62, 0.92])
        return np.sqrt((q ** 2).sum(axis=1))

    def cereb_norm(p):
        q = (p - np.array([0.0, -0.50, -0.62])) / np.array([0.48, 0.22, 0.26])
        return np.sqrt((q ** 2).sum(axis=1))

    pts: list[np.ndarray] = []
    want_cortex = int(n * 0.68)
    want_inner = int(n * 0.20)
    want_cereb = n - want_cortex - want_inner
    got = {"cortex": 0, "inner": 0, "cereb": 0}
    while sum(got.values()) < n:
        p = rng.uniform(-1, 1, size=(4000, 3))
        p = p[np.abs(p[:, 0]) > 0.035]                 # the fissure between hemispheres
        p = p[p[:, 1] > -0.62 + 0.25 * (p[:, 2] < -0.35)]  # flat-ish underside
        h = hemi_norm(p)
        c = cereb_norm(p)
        for row, hn, cn in zip(p, h, c):
            if got["cereb"] < want_cereb and cn < 1.0 and hn > 0.95:
                pts.append(row); got["cereb"] += 1
            elif got["cortex"] < want_cortex and 0.80 < hn < 1.0:
                pts.append(row); got["cortex"] += 1
            elif got["inner"] < want_inner and hn < 0.65:
                pts.append(row); got["inner"] += 1
            if sum(got.values()) >= n:
                break
    return np.array(pts[:n], dtype=float)


def _synapses(pts: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    """Undirected edges: each neuron to its k nearest neighbours, plus a few
    long fibres crossing between the hemispheres (the corpus callosum), which
    is what lets a cascade on one side light up the other."""
    d = np.sqrt(((pts[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2))
    np.fill_diagonal(d, np.inf)
    edges = set()
    for i in range(len(pts)):
        for j in np.argsort(d[i])[:k]:
            edges.add((min(i, int(j)), max(i, int(j))))
    left = np.where(pts[:, 0] < -0.1)[0]
    right = np.where(pts[:, 0] > 0.1)[0]
    for _ in range(10):
        a = int(rng.choice(left))
        b = int(right[np.argmin(np.abs(pts[right, 1] - pts[a, 1])
                                + np.abs(pts[right, 2] - pts[a, 2])
                                + rng.uniform(0, 0.3, len(right)))])
        edges.add((min(a, b), max(a, b)))
    return np.array(sorted(edges), dtype=int)


# How each state drives the network.
#   rate   spontaneous firing, spikes per neuron per second
#   gain   extra spontaneous rate per unit of audio level
#   weight charge one arriving spike adds (threshold is 1.0)
#   focus  extra rate for neurons near the wandering focus
_STATE = {
    "SLEEPING":   dict(rate=0.010, gain=0.0, weight=0.30, focus=0.0),
    "MUTED":      dict(rate=0.015, gain=0.0, weight=0.30, focus=0.0),
    "IDLE":       dict(rate=0.060, gain=0.8, weight=0.42, focus=0.0),
    "LISTENING":  dict(rate=0.080, gain=2.5, weight=0.45, focus=0.0),
    "THINKING":   dict(rate=0.150, gain=0.5, weight=0.58, focus=2.2),
    "SPEAKING":   dict(rate=0.090, gain=3.5, weight=0.50, focus=0.6),
}


class NeuralField:
    N = 220
    K = 3
    SPAN = 2.0                 # vertical extent in radii, for layout (like HoloAvatar)

    REFRACTORY = 0.35          # seconds a neuron stays silent after firing
    LEAK = 2.6                 # charge decay rate, 1/s
    SPEED = 1.5                # spike travel speed, units/s
    MAX_SPIKES = 240           # keeps a THINKING cascade from running away
    MAX_HALOS = 36             # glows drawn per frame; the brightest win
    EEG_LEN = 150              # samples kept for the readout traces

    def __init__(self, seed: int = 7):
        rng = np.random.default_rng(seed)
        self._rng = np.random.default_rng()
        self.pts = _brain_points(self.N, rng)
        self.edges = _synapses(self.pts, self.K, rng)
        a, b = self.edges[:, 0], self.edges[:, 1]
        self.edge_len = np.sqrt(((self.pts[a] - self.pts[b]) ** 2).sum(axis=1))
        self.adj: list[list[tuple[int, int]]] = [[] for _ in range(self.N)]
        for e, (i, j) in enumerate(self.edges):
            self.adj[i].append((e, j))
            self.adj[j].append((e, i))

        self.v = np.zeros(self.N)            # membrane charge
        self.refr = np.zeros(self.N)         # refractory time left
        self.flash = np.zeros(self.N)        # brightness of the last firing, decays
        # Live spikes: edge, source end (0 = edge's first node), progress 0..1
        self.sp_edge = np.zeros(0, dtype=int)
        self.sp_from = np.zeros(0, dtype=int)
        self.sp_prog = np.zeros(0)

        self.yaw = 0.0
        self.t = 0.0
        self.focus = np.array([0.0, 0.2, 0.4])
        self._focus_vel = np.array([0.3, 0.1, -0.2])
        self.scan = 0.0                      # sweeping scan line, 0..1 top to bottom
        self.state = "IDLE"
        self.muted = False
        self.fire_rate = 0.0                 # smoothed firings per second, for the readout
        self._fired_acc = 0
        self._eeg_t = 0.0
        self.eeg = [deque([0.0] * self.EEG_LEN, maxlen=self.EEG_LEN) for _ in range(3)]
        # On the GPU a gradient fill is cheap, and a large, nearly transparent
        # pixmap uploaded as a texture bands and shows its triangle seam — so
        # the OpenGL HUD sets this and the glow is drawn directly instead.
        self.direct_glow = False
        self._glow = None                    # cached atmosphere pixmap
        self._glow_key = None
        self._halos: dict[tuple, QPixmap] = {}   # (rgb, radius) → glow sprite
        self._region = np.where(self.pts[:, 1] < -0.32, 2,
                                np.where(self.pts[:, 0] < 0, 0, 1))

    # ── simulation ──────────────────────────────────────────────────────────

    def _mode(self, speaking: bool, muted: bool, state: str) -> str:
        if muted:
            return "MUTED"
        if speaking:
            return "SPEAKING"
        if state in ("THINKING", "PROCESSING"):
            return "THINKING"
        if state in _STATE:
            return state
        return "IDLE"

    def _fire(self, idx: np.ndarray, emit_p: float) -> None:
        if not len(idx):
            return
        self.v[idx] = 0.0
        self.refr[idx] = self.REFRACTORY
        self.flash[idx] = 1.0
        self._fired_acc += len(idx)
        room = self.MAX_SPIKES - len(self.sp_edge)
        if room <= 0:
            return
        new_e, new_f = [], []
        for i in idx:
            for e, _j in self.adj[i]:
                if self._rng.random() < emit_p:
                    new_e.append(e)
                    new_f.append(0 if self.edges[e, 0] == i else 1)
        if not new_e:
            return
        new_e, new_f = new_e[:room], new_f[:room]
        self.sp_edge = np.concatenate([self.sp_edge, np.array(new_e, dtype=int)])
        self.sp_from = np.concatenate([self.sp_from, np.array(new_f, dtype=int)])
        self.sp_prog = np.concatenate([self.sp_prog, np.zeros(len(new_e))])

    def step(self, dt: float, amp: float, speaking: bool = False,
             muted: bool = False, state: str = "IDLE") -> None:
        dt = max(0.0, min(dt, 0.1))
        mode = self._mode(speaking, muted, state)
        cfg = _STATE[mode]
        self.state, self.muted = mode, muted
        self.t += dt

        # A slow turn, faster while thinking, so the brain reads as 3D.
        self.yaw += dt * (0.32 if mode == "THINKING" else 0.16)
        self.scan = (self.scan + dt / (2.2 if mode == "THINKING" else 4.5)) % 1.0

        # The focus wanders inside the volume, bouncing off its walls.
        self.focus += self._focus_vel * dt * (1.6 if mode == "THINKING" else 0.6)
        for ax, lim in enumerate((0.6, 0.55, 0.85)):
            if abs(self.focus[ax]) > lim:
                self._focus_vel[ax] *= -1
                self.focus[ax] = math.copysign(lim, self.focus[ax])
        if self._rng.random() < dt * 0.5:
            self._focus_vel = self._rng.uniform(-0.5, 0.5, 3)

        # Charge leaks; refractory and flash decay.
        self.v *= math.exp(-self.LEAK * dt)
        self.refr = np.maximum(0.0, self.refr - dt)
        self.flash *= math.exp(-dt * 4.5)

        # Spikes travel; the ones that arrive charge their target.
        if len(self.sp_edge):
            self.sp_prog += dt * self.SPEED / np.maximum(self.edge_len[self.sp_edge], 0.05)
            done = self.sp_prog >= 1.0
            if done.any():
                tgt = np.where(self.sp_from[done] == 0,
                               self.edges[self.sp_edge[done], 1],
                               self.edges[self.sp_edge[done], 0])
                np.add.at(self.v, tgt, cfg["weight"])
                keep = ~done
                self.sp_edge, self.sp_from, self.sp_prog = (
                    self.sp_edge[keep], self.sp_from[keep], self.sp_prog[keep])

        # Spontaneous input: background rate, the audio level, and the focus.
        rate = np.full(self.N, cfg["rate"] + cfg["gain"] * amp)
        if cfg["focus"] > 0:
            d2 = ((self.pts - self.focus) ** 2).sum(axis=1)
            rate += cfg["focus"] * np.exp(-d2 / 0.06)
        kick = self._rng.random(self.N) < rate * dt
        self.v[kick] += 1.0

        # Fire everything over threshold that is not refractory. Fewer spikes
        # are emitted as the network fills up, so cascades crest and subside.
        ready = np.where((self.v >= 1.0) & (self.refr <= 0.0))[0]
        load = len(self.sp_edge) / self.MAX_SPIKES
        self._fire(ready, emit_p=0.75 * (1.0 - load) + 0.05)

        # Readout: firing rate and three EEG-like traces sampled at 30 Hz.
        self._eeg_t += dt
        if self._eeg_t >= 1 / 30:
            n_samples = int(self._eeg_t * 30)
            self._eeg_t -= n_samples / 30
            inst = self._fired_acc / max(dt, 1 / 30)
            self.fire_rate += (inst - self.fire_rate) * 0.08
            self._fired_acc = 0
            for ch in range(3):
                act = float(self.flash[self._region == ch].mean())
                wobble = 0.08 * math.sin(self.t * (7 + ch * 3)) + self._rng.normal(0, 0.03)
                for _ in range(n_samples):
                    self.eeg[ch].append(act * 2.2 + wobble)

    # ── drawing ─────────────────────────────────────────────────────────────

    def _project(self, cx: float, cy: float, r: float):
        cyaw, syaw = math.cos(self.yaw), math.sin(self.yaw)
        pitch = 0.32
        cp, spi = math.cos(pitch), math.sin(pitch)
        x, y, z = self.pts[:, 0], self.pts[:, 1], self.pts[:, 2]
        x1 = x * cyaw + z * syaw
        z1 = -x * syaw + z * cyaw
        y1 = y * cp - z1 * spi
        z2 = y * spi + z1 * cp
        persp = 3.2 / (3.2 + z2)
        sx = cx + x1 * r * persp
        sy = cy - y1 * r * persp
        depth = np.clip((1.0 - z2) / 2.0, 0.0, 1.0)     # 1 = nearest
        return sx, sy, depth, persp

    def _halo(self, acc: QColor, rad: int) -> QPixmap:
        """A firing neuron's glow, rendered once per colour and radius."""
        key = (acc.rgb(), rad)
        spr = self._halos.get(key)
        if spr is None:
            if len(self._halos) > 160:          # retheming leaves old colours behind
                self._halos.clear()
            spr = QPixmap(2 * rad, 2 * rad)
            spr.fill(Qt.GlobalColor.transparent)
            sp = QPainter(spr)
            sp.setRenderHint(QPainter.RenderHint.Antialiasing)
            g = QRadialGradient(QPointF(rad, rad), rad)
            c0 = QColor(acc); c0.setAlpha(150)
            c1 = QColor(acc); c1.setAlpha(0)
            g.setColorAt(0.0, c0); g.setColorAt(1.0, c1)
            sp.setPen(Qt.PenStyle.NoPen); sp.setBrush(g)
            sp.drawEllipse(QPointF(rad, rad), rad, rad)
            sp.end()
            self._halos[key] = spr
        return spr

    def paint(self, p: QPainter, cx: float, cy: float, r: float,
              main: QColor, acc: QColor, bg: QColor) -> None:

        def mix(col: QColor, k: float) -> QColor:
            # Pre-mixed onto the background, like the reactor core: the opaque
            # raster path is far cheaper than compositing every line.
            k = max(0.0, min(1.0, k))
            return QColor(int(bg.red()   + (col.red()   - bg.red())   * k),
                          int(bg.green() + (col.green() - bg.green()) * k),
                          int(bg.blue()  + (col.blue()  - bg.blue())  * k))

        dim = 0.45 if self.muted or self.state == "SLEEPING" else 1.0
        sx, sy, depth, persp = self._project(cx, cy, r * 1.08)

        # Scan line: a band sweeping down the brain; neurons inside it glow a
        # little, which is what makes the picture read as a live scan.
        top, bot = cy - r * 0.85, cy + r * 0.85
        scan_y = top + (bot - top) * self.scan
        near_scan = np.exp(-((sy - scan_y) / (r * 0.06)) ** 2)

        # 1. Atmosphere: one soft radial glow behind everything — rendered once
        #    per size and colour, then blitted. A full-size gradient fill every
        #    frame was a measurable slice of the paint.
        gr = int(r * 1.15)
        if self.direct_glow:
            g = QRadialGradient(QPointF(cx, cy), gr)
            c0 = QColor(main); c0.setAlpha(int(38 * dim))
            c1 = QColor(main); c1.setAlpha(0)
            g.setColorAt(0.0, c0); g.setColorAt(1.0, c1)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(g)
            p.drawEllipse(QPointF(cx, cy), gr, gr)
            self._glow_key = None
        gkey = (gr, main.rgb(), dim)
        if self.direct_glow:
            pass
        elif self._glow_key != gkey:
            pm = QPixmap(2 * gr, 2 * gr)
            pm.fill(Qt.GlobalColor.transparent)
            gp = QPainter(pm)
            g = QRadialGradient(QPointF(gr, gr), gr)
            c0 = QColor(main); c0.setAlpha(int(38 * dim))
            c1 = QColor(main); c1.setAlpha(0)
            g.setColorAt(0.0, c0); g.setColorAt(1.0, c1)
            gp.setPen(Qt.PenStyle.NoPen); gp.setBrush(g)
            gp.drawEllipse(QPointF(gr, gr), gr, gr)
            gp.end()
            self._glow, self._glow_key = pm, gkey
        if not self.direct_glow:
            p.drawPixmap(QPointF(cx - gr, cy - gr), self._glow)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # 2. Synapses, bucketed by brightness so each bucket is one drawLines().
        a, b = self.edges[:, 0], self.edges[:, 1]
        e_depth = (depth[a] + depth[b]) / 2
        e_act = np.maximum(self.flash[a], self.flash[b])
        e_lvl = (0.10 + 0.22 * e_depth + 0.45 * e_act) * dim
        buckets = np.minimum((e_lvl * 8).astype(int), 7)
        for lv in range(8):
            sel = np.where(buckets == lv)[0]
            if not len(sel):
                continue
            col = mix(main, (lv + 0.5) / 8)
            p.setPen(QPen(col, 1.0 if lv < 4 else 1.4))
            p.drawLines([QLineF(sx[a[e]], sy[a[e]], sx[b[e]], sy[b[e]]) for e in sel])

        # 3. Spikes in flight: a bright head with a short tail along the synapse.
        if len(self.sp_edge):
            e = self.sp_edge
            fa = np.where(self.sp_from == 0, self.edges[e, 0], self.edges[e, 1])
            fb = np.where(self.sp_from == 0, self.edges[e, 1], self.edges[e, 0])
            t = self.sp_prog
            hx = sx[fa] + (sx[fb] - sx[fa]) * t
            hy = sy[fa] + (sy[fb] - sy[fa]) * t
            t0 = np.maximum(0.0, t - 0.35)
            tx = sx[fa] + (sx[fb] - sx[fa]) * t0
            ty = sy[fa] + (sy[fb] - sy[fa]) * t0
            p.setPen(QPen(mix(acc, 0.55 * dim), 1.6))
            p.drawLines([QLineF(tx[i], ty[i], hx[i], hy[i]) for i in range(len(e))])
            p.setPen(QPen(mix(acc, 1.0 * dim), 2.6, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawPoints([QPointF(hx[i], hy[i]) for i in range(len(e))])

        # 4. Neurons. This used to be one drawEllipse per neuron plus a fresh
        #    radial gradient per firing one: ~400 calls a frame, and PyQt holds
        #    the GIL through every one of them — enough to starve the audio
        #    thread and make speech stutter. Now: halos are a cached sprite
        #    blitted with an opacity (the brightest few only), and bodies are
        #    grouped by size and colour into a handful of drawPoints calls.
        size = (1.1 + 1.4 * depth) * persp * max(0.6, r / 170)
        firing = self.flash > 0.12
        hot = np.where(firing)[0]
        if len(hot):
            hot = hot[np.argsort(self.flash[hot])[-self.MAX_HALOS:]]
            for i in hot:
                f = float(self.flash[i])
                rad = max(3, int(round(size[i] * (3.0 + 4.0 * f))))
                spr = self._halo(acc, rad)
                p.setOpacity(min(1.0, f * dim))
                p.drawPixmap(QPointF(sx[i] - rad, sy[i] - rad), spr)
            p.setOpacity(1.0)

        lvl = np.clip((0.30 + 0.50 * depth + 0.5 * near_scan) * dim, 0, 1)
        size_q = np.clip(np.round(size * 2) / 2, 0.5, 6)          # 0.5 px steps
        col_q = np.where(firing, 10 + np.minimum((self.flash * 3).astype(int), 2),
                         np.minimum((lvl * 6).astype(int), 5))
        groups: dict[tuple, list] = {}
        for i in range(self.N):
            groups.setdefault((int(col_q[i]), float(size_q[i])), []).append(QPointF(sx[i], sy[i]))
        for (cq, sq), pts in sorted(groups.items()):
            col = (mix(acc, min(1.0, 0.5 + (cq - 10 + 1) / 3)) if cq >= 10
                   else mix(main, (cq + 0.5) / 6))
            p.setPen(QPen(col, sq * 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(pts)

        # 5. The scan line itself, with tick marks at the ends.
        sl = mix(main, 0.35 * dim)
        p.setPen(QPen(sl, 1, Qt.PenStyle.DashLine))
        p.drawLine(QLineF(cx - r * 1.05, scan_y, cx + r * 1.05, scan_y))
        p.setPen(QPen(mix(main, 0.8 * dim), 1.5))
        for side in (-1, 1):
            x0 = cx + side * r * 1.05
            p.drawLine(QLineF(x0, scan_y - 5, x0, scan_y + 5))

        # 6. Readout: firing rate and three EEG traces down the right side.
        font = QFont("Courier New", max(7, int(r / 22)))
        p.setFont(font)
        p.setPen(QPen(mix(main, 0.75 * dim), 1))
        label = {"THINKING": "COGNITION", "SPEAKING": "SPEECH", "LISTENING": "INPUT",
                 "SLEEPING": "DORMANT", "MUTED": "MUTED"}.get(self.state, "IDLE")
        p.drawText(QRectF(cx - r * 1.25, cy - r * 1.02, r, 16),
                   Qt.AlignmentFlag.AlignLeft, f"NEURAL ▸ {label}")
        p.drawText(QRectF(cx - r * 1.25, cy - r * 1.02 + 15, r, 16),
                   Qt.AlignmentFlag.AlignLeft,
                   f"{self.fire_rate:5.0f} spk/s  {len(self.sp_edge):3d} syn")

        w = r * 0.55
        x0 = cx + r * 1.25 - w
        names = ("L-HEM", "R-HEM", "CEREB")
        for ch in range(3):
            base = cy + r * 0.80 + ch * r * 0.13
            p.setPen(QPen(mix(main, 0.45 * dim), 1))
            p.drawText(QRectF(x0 - r * 0.32, base - 8, r * 0.3, 14),
                       Qt.AlignmentFlag.AlignRight, names[ch])
            data = self.eeg[ch]
            n = len(data)
            amp_px = r * 0.05
            pts = [QPointF(x0 + w * k / (n - 1), base - max(-1.2, min(1.5, v)) * amp_px)
                   for k, v in enumerate(data)]
            p.setPen(QPen(mix(acc if ch == 0 else main, 0.85 * dim), 1.2))
            p.drawPolyline(pts)
