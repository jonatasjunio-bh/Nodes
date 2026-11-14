"""
core/neural_synapse.py — the J.A.R.V.I.S. ⇄ ULTRON Neural Synapse Sphere.

Inspired by the holographic consciousness matrices in Avengers: Age of Ultron:
  - J.A.R.V.I.S. state (IDLE / LISTENING / SLEEP):
      Clean, luminous cyan/electric blue, orderly orbital HUD rings, serene rotation,
      breathing with the user's voice.
  - ULTRON / Mind Stone state (THINKING / PROCESSING / DEEP BRAIN):
      Ignites into an incandescent golden/amber/white-hot vortex, acceleration of
      orbital data rings, branching electric plasma arcs, cascading synaptic pulses.

The transition between Jarvis and Ultron is dynamic and continuous (0.0 to 1.0 blend),
reacting in real time to the assistant's internal state and audio amplitude.
Pure QPainter software rendering with vectorized NumPy projections for constant 60 FPS.
"""

from __future__ import annotations

import math
import time
from typing import Optional

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient,
)

# ── Color Palettes ────────────────────────────────────────────────────────────
# J.A.R.V.I.S. — Arc reactor cyan, cold blue, ice-white highlights
JARVIS_MAIN = QColor(0, 195, 255)
JARVIS_ACC  = QColor(140, 235, 255)
JARVIS_DEEP = QColor(0, 75, 170)

# ULTRON / Mind Stone — Blazing solar gold, amber, fiery corona, incandescent core
ULTRON_MAIN = QColor(255, 145, 18)
ULTRON_ACC  = QColor(255, 225, 95)
ULTRON_DEEP = QColor(225, 50, 0)

WHITE_HOT   = QColor(255, 250, 235)

_SHELLS = (1.0, 0.84, 0.66, 0.46)
_SHELL_P = (0.46, 0.26, 0.17, 0.11)
_RING_SAMPLES = 64
_TAIL = 9
_TAIL_GAP = 0.03
_BLOOM = 2.5


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    """Linearly interpolate between two QColors."""
    t = max(0.0, min(1.0, t))
    r = int(c1.red() + (c2.red() - c1.red()) * t)
    g = int(c1.green() + (c2.green() - c1.green()) * t)
    b = int(c1.blue() + (c2.blue() - c1.blue()) * t)
    a = int(c1.alpha() + (c2.alpha() - c1.alpha()) * t)
    return QColor(r, g, b, a)


def _basis(rng: np.random.Generator, n: int):
    axis = rng.normal(size=(n, 3))
    axis /= np.maximum(np.linalg.norm(axis, axis=1, keepdims=True), 1e-7)
    ref = np.where(np.abs(axis[:, :1]) < 0.9, np.array([[1.0, 0, 0]]), np.array([[0, 1.0, 0]]))
    u = np.cross(axis, ref)
    u /= np.maximum(np.linalg.norm(u, axis=1, keepdims=True), 1e-7)
    return u, np.cross(axis, u)


class SynapseMatrix:
    MAX_NODES = 360
    MIN_NODES = 52
    PER_KNOWLEDGE = 1.5
    RINGS = 10           # Concentric & tilted orbital data bands
    FRAGS = 140          # Circuit tick marks
    DUST = 240           # Floating neural particles
    MAX_PULSES = 110
    MAX_HALOS = 35
    MAX_ARCS = 16        # Electric plasma lightning arcs

    def __init__(self, seed: int = 42):
        rng = np.random.default_rng(seed)
        self._rng = np.random.default_rng()
        M = self.MAX_NODES

        # 3D Node positions distributed across nested spherical shells
        d = rng.normal(size=(M, 3))
        d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-7)
        shell = rng.choice(len(_SHELLS), M, p=_SHELL_P)
        rad = np.array(_SHELLS)[shell] + rng.uniform(-0.03, 0.03, M)
        self.base = d * rad[:, None]
        self.ph = rng.uniform(0, 2 * math.pi, (M, 3))

        # Synaptic connections (edges)
        edges, born = [], []
        for i in range(1, M):
            dist = np.linalg.norm(self.base[:i] - self.base[i], axis=1)
            near = np.argsort(dist)[:(1 if i < 4 else 2 + int(rng.random() < 0.35))]
            for j in near:
                edges.append((int(j), i))
                born.append(i)
            if i > 20 and rng.random() < 0.12:
                edges.append((int(rng.integers(0, i)), i))
                born.append(i)
        self.edges = np.array(edges, dtype=int)
        self.e_born = np.array(born, dtype=int)
        self.adj: list[list[tuple[int, int]]] = [[] for _ in range(M)]
        for e, (i, j) in enumerate(self.edges):
            self.adj[i].append((e, j))
            self.adj[j].append((e, i))
        self.glow = np.zeros(M)
        self.birth = np.full(M, -1e9)

        # Knowledge growth
        self.n = float(self.MIN_NODES)
        self.target = float(self.MIN_NODES)
        self._known = None

        # Synaptic pulses
        self.p_edge = np.zeros(0, dtype=int)
        self.p_rev = np.zeros(0, dtype=bool)
        self.p_k = np.zeros(0)
        self.p_life = np.zeros(0)

        # Orbital data rings (film HUD texture)
        R = self.RINGS
        self.r_u, self.r_v = _basis(rng, R)
        self.r_rad = rng.uniform(0.88, 1.18, R)
        self.r_speed = rng.uniform(0.18, 0.65, R) * rng.choice([-1, 1], R)
        self.r_mask = np.zeros((R, _RING_SAMPLES), dtype=bool)
        for i in range(R):
            k = 0
            while k < _RING_SAMPLES:
                on = int(rng.integers(4, 14))
                self.r_mask[i, k:k + on] = True
                k += on + int(rng.integers(3, 10))
        self.theta = np.linspace(0, 2 * math.pi, _RING_SAMPLES, endpoint=False)
        self.r_ph = np.zeros(R)

        # Circuit fragments
        F = self.FRAGS
        self.f_u, self.f_v = _basis(rng, F)
        self.f_rad = rng.uniform(0.35, 1.08, F)
        self.f_ang = rng.uniform(0, 2 * math.pi, F)
        self.f_len = rng.uniform(0.025, 0.08, F)
        self.f_speed = rng.uniform(0.15, 0.45, F) * rng.choice([-1, 1], F)

        # Neural dust particles
        dd = rng.normal(size=(self.DUST, 3))
        self.d_pos = dd / np.maximum(np.linalg.norm(dd, axis=1, keepdims=True), 1e-7) * (
            rng.uniform(0.08, 1.08, (self.DUST, 1)) ** 0.5
        )

        # Electric plasma lightning arcs (active in Ultron & Thinking states)
        self.arcs: list[dict] = []  # list of {pts: np.ndarray, life: float, max_life: float}

        # Timers and motion
        self.t = 0.0
        self.yaw = 0.0
        self.pitch = 0.28
        self.core_ph = 0.0
        self.amp = 0.0
        self.state = "IDLE"
        self.muted = False
        self.direct_glow = False

        # Dynamic Jarvis ⇄ Ultron transition (0.0 = Jarvis, 1.0 = Ultron)
        self._blend = 0.0
        self._target_blend = 0.0

        # Cached halos
        self._glow_pm: Optional[QPixmap] = None
        self._glow_key = None
        self._halos: dict[tuple, QPixmap] = {}

        # Memory items for tooltips
        self.items: list[dict] = []
        self.recent = np.zeros(self.MAX_NODES, dtype=bool)
        self.hover: Optional[int] = None
        self._proj = None

    # ── Memory Integration ────────────────────────────────────────────────────

    def set_items(self, items) -> None:
        if not items or items is self.items:
            return
        self.items = list(items)
        self._map_recent()

    def _map_recent(self) -> None:
        n = self.alive
        self.recent[:] = False
        if not self.items or not n:
            return
        cut = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 48 * 3600))
        for i in range(n):
            if self.item_for(i).get("ts", "") >= cut:
                self.recent[i] = True

    def item_for(self, i: int) -> dict:
        if not self.items:
            return {}
        n = max(1, self.alive)
        return self.items[min(len(self.items) - 1, int(i * len(self.items) / n))]

    def over(self, x: float, y: float) -> bool:
        if not self._proj:
            return False
        cx, cy, r = self._proj[4:7]
        return (x - cx) ** 2 + (y - cy) ** 2 <= (r * 1.15) ** 2

    def node_at(self, x: float, y: float, reach: float = 12.0) -> Optional[int]:
        if not self._proj or not self.items:
            return None
        nx, ny, nd, n = self._proj[:4]
        d2 = (nx - x) ** 2 + (ny - y) ** 2
        d2 = np.where(nd > 0.30, d2, np.inf)
        i = int(np.argmin(d2))
        return i if d2[i] <= reach * reach else None

    def set_knowledge(self, known) -> None:
        if known is None:
            return
        known = max(0, int(known))
        target = min(self.MAX_NODES, self.MIN_NODES + self.PER_KNOWLEDGE * known)
        if self._known is None:
            self.n = target
        self._known = known
        self.target = target

    @property
    def alive(self) -> int:
        return int(self.n)

    # ── Simulation Step ───────────────────────────────────────────────────────

    def step(self, dt: float, amp: float, speaking: bool = False,
             muted: bool = False, state: str = "IDLE") -> None:
        dt = max(0.0, min(dt, 0.1))
        self.state, self.muted = state, muted

        # Calculate target blend:
        # Thinking/Processing -> 1.0 (Full Ultron Gold/Amber)
        # Speaking -> 0.40 (Gold accents on Jarvis Blue)
        # Idle/Listening/Sleeping -> 0.0 (Pure Jarvis Blue)
        if state in ("THINKING", "PROCESSING"):
            self._target_blend = 1.0
        elif speaking:
            self._target_blend = 0.40
        else:
            self._target_blend = 0.0

        # Smooth transition between Jarvis and Ultron
        blend_speed = 3.0 if self._target_blend > self._blend else 1.4
        self._blend += (self._target_blend - self._blend) * min(1.0, dt * blend_speed)

        # Dynamic speed factors
        spin_mult = 1.0 + 2.4 * self._blend
        tempo = (0.7 + 0.9 * self._blend + 0.5 * self.amp) * (1.6 if state in ("THINKING", "PROCESSING") else 1.0)
        if muted:
            tempo *= 0.4

        self.t += dt * tempo
        self.yaw += dt * 0.14 * spin_mult
        self.core_ph += dt * (1.8 * spin_mult + 2.0 * self.amp)
        self.amp += (float(amp) - self.amp) * min(1.0, dt * 9.0)
        self.glow *= math.exp(-dt * 2.5)

        self.r_ph += dt * tempo * self.r_speed * spin_mult
        self.f_ang += dt * tempo * self.f_speed * spin_mult

        # Knowledge growth
        if self.n < self.target:
            before = int(self.n)
            self.n = min(self.target, self.n + dt * 2.2)
            now = time.monotonic()
            for i in range(before, int(self.n)):
                self.birth[i] = now
                self.glow[i] = 1.0
                self._spawn([i])
        elif self.n > self.target:
            self.n = max(self.target, self.n - dt * 3.5)

        # Pulse propagation
        if len(self.p_edge):
            alive = self.e_born[self.p_edge] < self.alive
            if not alive.all():
                self.p_edge, self.p_rev = self.p_edge[alive], self.p_rev[alive]
                self.p_k, self.p_life = self.p_k[alive], self.p_life[alive]
            self.p_k += dt * tempo / self.p_life
            done = self.p_k >= 1.0
            if done.any():
                ends = np.where(self.p_rev[done], self.edges[self.p_edge[done], 0],
                                self.edges[self.p_edge[done], 1])
                np.add.at(self.glow, ends, 0.85)
                np.clip(self.glow, 0, 1, out=self.glow)
                keep = ~done
                self.p_edge, self.p_rev = self.p_edge[keep], self.p_rev[keep]
                self.p_k, self.p_life = self.p_k[keep], self.p_life[keep]
                chain_prob = 0.18 + 0.35 * self._blend
                load = len(self.p_edge) / self.MAX_PULSES
                self._spawn(ends[self._rng.random(len(ends)) < chain_prob * (1 - load)])

        # Spawn new pulses
        rate = 4.0 + 16.0 * self._blend + 14.0 * self.amp
        n_new = self._rng.poisson(rate * dt * (0.5 + self.alive / 220))
        if n_new:
            self._spawn(self._rng.integers(0, max(1, self.alive), n_new))

        # Electric Plasma Arcs (Ultron & High Energy)
        # Update existing arcs
        for a in self.arcs:
            a["life"] -= dt
        self.arcs = [a for a in self.arcs if a["life"] > 0]

        # Spawn new electric arcs if energy is high
        arc_prob = 0.15 + 0.85 * self._blend + 0.4 * self.amp
        if len(self.arcs) < self.MAX_ARCS and self._rng.random() < arc_prob * dt * 8.0:
            self._spawn_arc()

    def _spawn(self, nodes) -> None:
        room = self.MAX_PULSES - len(self.p_edge)
        if room <= 0 or not len(nodes):
            return
        n_alive = self.alive
        es, rv = [], []
        for i in list(nodes)[:room]:
            opts = [(e, j) for e, j in self.adj[i] if j < n_alive]
            if not opts:
                continue
            e, _j = opts[self._rng.integers(len(opts))]
            es.append(e)
            rv.append(self.edges[e, 0] != i)
            self.glow[i] = min(1.0, self.glow[i] + 0.5)
        if not es:
            return
        n = len(es)
        self.p_edge = np.concatenate([self.p_edge, np.array(es, dtype=int)])
        self.p_rev = np.concatenate([self.p_rev, np.array(rv, dtype=bool)])
        self.p_k = np.concatenate([self.p_k, np.zeros(n)])
        self.p_life = np.concatenate([self.p_life, self._rng.uniform(0.6, 1.3, n)])

    def _spawn_arc(self) -> None:
        """Create a procedural jagged lightning arc across the sphere."""
        if self.alive < 10:
            return
        i = int(self._rng.integers(0, self.alive))
        # Find another node reasonably far on the outer shell
        j = int(self._rng.integers(0, self.alive))
        if i == j:
            return
        p1 = self.base[i]
        p2 = self.base[j]
        # Intermediate jagged points pushed outward
        steps = 5
        t_vals = np.linspace(0, 1, steps)
        pts = []
        for t in t_vals:
            mid = (1 - t) * p1 + t * p2
            norm = np.linalg.norm(mid)
            if norm > 1e-4:
                mid = mid / norm * 1.05  # slightly above outer shell
            mid += self._rng.normal(scale=0.06, size=3)
            pts.append(mid)
        self.arcs.append({
            "pts": np.array(pts),
            "life": float(self._rng.uniform(0.12, 0.28)),
            "max_life": 0.28,
        })

    # ── Projection and Rendering ──────────────────────────────────────────────

    def _project(self, P: np.ndarray, cx: float, cy: float, r: float):
        cyaw, syaw = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        x, y, z = P[..., 0], P[..., 1], P[..., 2]
        x1 = x * cyaw + z * syaw
        z1 = -x * syaw + z * cyaw
        y1 = y * cp - z1 * sp
        z2 = y * sp + z1 * cp
        persp = 3.6 / (3.6 + z2)
        return cx + x1 * r * persp, cy - y1 * r * persp, np.clip((1.0 - z2) / 2.0, 0.0, 1.0)

    def _halo(self, col: QColor, rad: int) -> QPixmap:
        key = (col.rgb(), rad)
        spr = self._halos.get(key)
        if spr is None:
            if len(self._halos) > 200:
                self._halos.clear()
            spr = QPixmap(2 * rad, 2 * rad)
            spr.fill(Qt.GlobalColor.transparent)
            hp = QPainter(spr)
            hp.setRenderHint(QPainter.RenderHint.Antialiasing)
            g = QRadialGradient(QPointF(rad, rad), rad)
            c0 = QColor(col); c0.setAlpha(220)
            c1 = QColor(col); c1.setAlpha(70)
            c2 = QColor(col); c2.setAlpha(0)
            g.setColorAt(0.0, c0); g.setColorAt(0.35, c1); g.setColorAt(1.0, c2)
            hp.setPen(Qt.PenStyle.NoPen); hp.setBrush(g)
            hp.drawEllipse(QPointF(rad, rad), rad, rad)
            hp.end()
            self._halos[key] = spr
        return spr

    def paint(self, p: QPainter, cx: float, cy: float, r: float,
              main: QColor = None, acc: QColor = None, bg: QColor = None) -> None:
        # Determine dynamic colors based on current Jarvis ⇄ Ultron blend
        blend = self._blend
        dyn_main = _lerp_color(JARVIS_MAIN, ULTRON_MAIN, blend)
        dyn_acc  = _lerp_color(JARVIS_ACC, ULTRON_ACC, blend)
        dyn_deep = _lerp_color(JARVIS_DEEP, ULTRON_DEEP, blend)

        def lighten(col: QColor, k: float) -> QColor:
            return QColor(int(col.red() + (WHITE_HOT.red() - col.red()) * k),
                          int(col.green() + (WHITE_HOT.green() - col.green()) * k),
                          int(col.blue() + (WHITE_HOT.blue() - col.blue()) * k))

        def lum(col: QColor, k: float) -> QColor:
            k = max(0.0, min(1.0, k))
            return QColor(int(col.red() * k), int(col.green() * k), int(col.blue() * k))

        R = r * 1.0
        unit = max(0.7, r / 180)
        n = max(2, self.alive)
        grown = n / self.MAX_NODES
        dim = 0.5 if self.muted else 1.0

        # ── 1. Atmosphere Glow & Corona ───────────────────────────────────────
        gr = int(r * 1.35)
        strength = int((40 + 35 * grown + 35 * self.amp + 35 * blend) * dim)
        g = QRadialGradient(QPointF(cx, cy), gr)
        c0 = QColor(dyn_main); c0.setAlpha(min(255, strength))
        c1 = QColor(dyn_deep); c1.setAlpha(min(255, strength // 3))
        c2 = QColor(dyn_main); c2.setAlpha(0)
        g.setColorAt(0.0, c0); g.setColorAt(0.45, c1); g.setColorAt(1.0, c2)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), gr, gr)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Enable additive blending for brilliant holographic light
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)

        # ── 2. Concentric Orbital HUD Rings (Data Bands) ──────────────────────
        a = self.theta[None, :] + self.r_ph[:, None]
        P_rings = self.r_rad[:, None, None] * (
            np.cos(a)[..., None] * self.r_u[:, None, :] + np.sin(a)[..., None] * self.r_v[:, None, :]
        )
        sx, sy, dp = self._project(P_rings, cx, cy, R)
        for i in range(self.RINGS):
            m = self.r_mask[i]
            k = 0
            while k < _RING_SAMPLES:
                if not m[k]:
                    k += 1
                    continue
                e = k
                while e < _RING_SAMPLES and m[e]:
                    e += 1
                d = float(dp[i, k:e].mean())
                pen_color = lum(lighten(dyn_main, 0.25 * d), (0.18 + 0.55 * d) * dim)
                pen_w = (0.9 + 1.1 * d) * unit
                p.setPen(QPen(pen_color, pen_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawPolyline(QPolygonF([QPointF(sx[i, j], sy[i, j])
                                          for j in range(k, min(e + 1, _RING_SAMPLES))]))
                k = e

        # ── 3. Synaptic Web (Filaments) ───────────────────────────────────────
        sway = 0.02 * np.sin(self.t * np.array([0.8, 0.7, 0.6]) + self.ph[:n])
        breathe = 1.0 + 0.02 * math.sin(self.t * 1.1) + 0.06 * self.amp
        Pn = (self.base[:n] + sway) * breathe
        nx, ny, nd = self._project(Pn, cx, cy, R)
        self._proj = (nx, ny, nd, n, cx, cy, r)

        n_e = int(np.searchsorted(self.e_born, n))
        E = self.edges[:n_e]
        ea, eb = E[:, 0], E[:, 1]
        mid = (Pn[ea] + Pn[eb]) / 2
        mr = (np.linalg.norm(Pn[ea], axis=1) + np.linalg.norm(Pn[eb], axis=1)) / 2
        mid = mid / np.maximum(np.linalg.norm(mid, axis=1, keepdims=True), 1e-6) * mr[:, None]
        mx, my, _ = self._project(mid, cx, cy, R)
        qx = 2 * mx - (nx[ea] + nx[eb]) / 2
        qy = 2 * my - (ny[ea] + ny[eb]) / 2

        now = time.monotonic()
        young = np.clip(1 - (now - self.birth[:n]) / _BLOOM, 0, 1)
        e_depth = (nd[ea] + nd[eb]) / 2
        e_act = np.maximum(np.maximum(self.glow[ea], self.glow[eb]), young[eb])
        lvl = (0.16 + 0.44 * e_depth + 0.55 * e_act) * dim
        buckets = np.minimum((lvl * 7).astype(int), 6)

        for lv in range(7):
            sel = np.where(buckets == lv)[0]
            if not len(sel):
                continue
            path = QPainterPath()
            for e in sel:
                path.moveTo(nx[ea[e]], ny[ea[e]])
                path.quadTo(qx[e], qy[e], nx[eb[e]], ny[eb[e]])
            line_col = lum(lighten(dyn_main, 0.08 * lv), (lv + 1.0) / 7.5)
            p.setPen(QPen(line_col, (0.85 + 0.22 * lv) * unit))
            p.drawPath(path)

        # ── 4. Electric Plasma Lightning Arcs ─────────────────────────────────
        if self.arcs:
            for arc in self.arcs:
                alpha = arc["life"] / arc["max_life"]
                arc_proj_x, arc_proj_y, _ = self._project(arc["pts"], cx, cy, R)
                arc_path = QPainterPath()
                arc_path.moveTo(arc_proj_x[0], arc_proj_y[0])
                for pt_idx in range(1, len(arc_proj_x)):
                    arc_path.lineTo(arc_proj_x[pt_idx], arc_proj_y[pt_idx])
                # Draw glowing aura then core spark
                arc_aura = _lerp_color(dyn_main, WHITE_HOT, 0.4)
                arc_core = WHITE_HOT
                p.setPen(QPen(lum(arc_aura, alpha * 0.7), max(2.5, r / 60), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawPath(arc_path)
                p.setPen(QPen(lum(arc_core, alpha), max(1.2, r / 120), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawPath(arc_path)

        # ── 5. Synaptic Pulses ────────────────────────────────────────────────
        on = self.p_edge < n_e
        if on.any():
            e = self.p_edge[on]
            rev, pk = self.p_rev[on], self.p_k[on]
            u0 = np.where(rev, eb[e], ea[e])
            u1 = np.where(rev, ea[e], eb[e])
            X0, Y0, X1, Y1 = nx[u0], ny[u0], nx[u1], ny[u1]
            QX, QY = qx[e], qy[e]
            head = lighten(dyn_acc, 0.6)
            hw = max(1.8, r / 90)
            for s in range(_TAIL):
                k = np.clip(pk - s * _TAIL_GAP, 0, 1)
                k = np.where(k < 0.5, 2 * k * k, 1 - (-2 * k + 2) ** 2 / 2)
                u = 1 - k
                px = u * u * X0 + 2 * u * k * QX + k * k * X1
                py = u * u * Y0 + 2 * u * k * QY + k * k * Y1
                f = (1 - s / _TAIL) * dim
                p.setPen(QPen(lum(head, 0.25 + 0.75 * f), hw * (1 - s * 0.08),
                              Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawPoints([QPointF(px[i], py[i]) for i in range(len(e))])

        # ── 6. Neural Dust Particles ──────────────────────────────────────────
        yaw_dust = self.yaw * 0.6
        rot = np.array([[math.cos(yaw_dust), 0, math.sin(yaw_dust)], [0, 1, 0], [-math.sin(yaw_dust), 0, math.cos(yaw_dust)]])
        qx2, qy2, qd2 = self._project(self.d_pos @ rot.T, cx, cy, R)
        sel_dust = np.where(qd2 > 0.22)[0]
        p.setPen(QPen(lum(lighten(dyn_main, 0.35), 0.55 * dim), 1.2 * unit, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPoints([QPointF(qx2[j], qy2[j]) for j in sel_dust])

        # ── 7. The Core: Swirling Vortex & Incandescent Solar Heart ───────────
        arms = 6
        tt = np.linspace(0, 1, 32)
        grow = (1.0 + 0.30 * self.amp + 0.25 * blend) * (0.85 + 0.3 * grown)
        for a_i in range(arms):
            ang = self.core_ph * 2.4 + a_i * 2 * math.pi / arms + tt * 4.2
            rr = (0.03 + 0.24 * tt) * r * grow
            arm_color = lum(lighten(dyn_acc, 0.55), (1.0 - 0.08 * a_i) * dim)
            p.setPen(QPen(arm_color, max(1.2, r / 130) * (1.6 - 0.12 * a_i),
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPolyline(QPolygonF([QPointF(cx + math.cos(ang[j]) * rr[j], cy + math.sin(ang[j]) * rr[j] * 0.85)
                                      for j in range(len(tt))]))

        # Central white-hot core halo
        cr = max(8, int(r * (0.24 + 0.14 * self.amp + 0.12 * blend)))
        core_spr = self._halo(WHITE_HOT if blend > 0.3 else dyn_acc, cr)
        p.setOpacity(min(1.0, 0.85 * dim + 0.3 * self.amp + 0.2 * blend))
        p.drawPixmap(QPointF(cx - cr, cy - cr), core_spr)
        p.setOpacity(1.0)

        # ── 8. Nodes & Synaptic Firings ───────────────────────────────────────
        hot = np.where((self.glow[:n] > 0.1) | (young > 0))[0]
        if len(hot):
            heat = np.maximum(self.glow[hot], young[hot])
            hot = hot[np.argsort(heat)[-self.MAX_HALOS:]]
            for i in hot:
                gl = float(max(self.glow[i], young[i]))
                born = young[i] > 0
                hr = max(5, int(r / (22 if born else 36) * (1 + 1.8 * gl) * (0.7 + 0.5 * nd[i])))
                p.setOpacity(min(1.0, (0.45 + 0.55 * gl) * dim))
                p.drawPixmap(QPointF(nx[i] - hr, ny[i] - hr),
                             self._halo(WHITE_HOT if born or blend > 0.4 else lighten(dyn_acc, 0.3), hr))
            p.setOpacity(1.0)

        # Node points
        size = np.clip(np.round((0.9 + 1.5 * nd) * unit * 2) / 2, 0.6, 4.8)
        level = np.minimum(((0.3 + 0.5 * nd + 0.6 * np.maximum(self.glow[:n], young)) * dim * 5).astype(int), 4)
        groups: dict[tuple, list] = {}
        for i in range(n):
            groups.setdefault((int(level[i]), float(size[i])), []).append(QPointF(nx[i], ny[i]))
        for (lv, sz), pts in sorted(groups.items()):
            pt_col = lighten(dyn_main, 0.22 * lv) if lv >= 3 else lum(dyn_main, 0.50 + lv * 0.18)
            p.setPen(QPen(pt_col, sz * 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(pts)

        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # ── 9. Subtitle Label ─────────────────────────────────────────────────
        p.setPen(lum(lighten(dyn_main, 0.2), 0.8 * dim))
        f = QFont()
        f.setPointSizeF(max(6.5, r / 25))
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, max(1.0, r / 110))
        p.setFont(f)
        mode_label = "ULTRON CORE" if blend > 0.65 else ("TRANSITION" if blend > 0.25 else "J.A.R.V.I.S. MATRIX")
        txt = f"{n} SYNAPSES · {mode_label}"
        w = p.fontMetrics().horizontalAdvance(txt)
        p.drawText(QPointF(cx - w / 2, cy + r * 1.15), txt)
