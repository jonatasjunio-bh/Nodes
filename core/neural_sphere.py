"""
core/neural_sphere.py — the centrepiece as J.A.R.V.I.S's mind in Age of Ultron:
a golden globe of synapses that grows as the assistant learns.

    synapses    nodes of light on nested spherical shells, joined by filaments
                that curve along the globe; pulses fire along them and light the
                node they reach, sometimes setting off the next
    growth      how many nodes are alive follows what the assistant knows
                (memories kept + things learned or corrected, core/hud_data.py):
                each new thing learned makes new nodes bloom with a flash, and
                the web gets denser over the weeks
    rings       faint circuit-like arcs and fragments around it — the data
                texture of the film's hologram
    the core    a bright swirl at the centre that spins harder when it works

How busy it is follows what the assistant is doing:

    SLEEPING / muted   slow turn, few pulses, dim
    LISTENING          steady, lifted by your voice
    THINKING           fast, cascades of pulses, the core spins hard
    SPEAKING           pulses with the assistant's own audio level

Same interface as OrganicField — step() advances, paint() draws — plus
set_knowledge(). Same budget: numpy for the geometry, every layer drawn in a few
grouped calls (a path per brightness level, points in batches, cached glow
sprites), because PyQt holds the GIL through each call and hundreds of small
ones per frame starve the audio threads.
"""

from __future__ import annotations

import math
import time

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import (QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
                         QRadialGradient)

# the film's palette: amber light, white-hot where it is densest. Fixed — this
# centrepiece is meant to look like the hologram, whatever the HUD theme.
GOLD = QColor(255, 150, 28)
HOT = QColor(255, 205, 105)

_STATE = {
    "SLEEPING":  dict(tempo=0.35, rate=1.0, gain=0.0, chain=0.06, spin=0.6, dim=0.5),
    "MUTED":     dict(tempo=0.35, rate=1.0, gain=0.0, chain=0.06, spin=0.6, dim=0.5),
    "IDLE":      dict(tempo=0.7, rate=4.0, gain=8.0, chain=0.14, spin=1.0, dim=1.0),
    "LISTENING": dict(tempo=0.8, rate=5.0, gain=18.0, chain=0.16, spin=1.2, dim=1.0),
    "THINKING":  dict(tempo=1.5, rate=16.0, gain=3.0, chain=0.45, spin=3.2, dim=1.0),
    "SPEAKING":  dict(tempo=1.1, rate=6.0, gain=22.0, chain=0.22, spin=2.0, dim=1.0),
}
_SHELLS = (1.0, 0.84, 0.66, 0.46)
_SHELL_P = (0.46, 0.26, 0.17, 0.11)
_RING_SAMPLES = 48
_TAIL = 9
_TAIL_GAP = 0.03          # curve progress between tail samples
_BLOOM = 2.6              # seconds a newborn node flashes


def _basis(rng: np.random.Generator, n: int):
    axis = rng.normal(size=(n, 3))
    axis /= np.linalg.norm(axis, axis=1, keepdims=True)
    ref = np.where(np.abs(axis[:, :1]) < 0.9, np.array([[1.0, 0, 0]]), np.array([[0, 1.0, 0]]))
    u = np.cross(axis, ref)
    u /= np.linalg.norm(u, axis=1, keepdims=True)
    return u, np.cross(axis, u)


class DataSphere:
    MAX_NODES = 360           # a fully grown mind
    MIN_NODES = 48            # a newborn one
    PER_KNOWLEDGE = 1.5       # nodes per thing known
    RINGS = 9
    FRAGS = 130
    DUST = 220
    MAX_PULSES = 90
    MAX_HALOS = 30

    def __init__(self, seed: int = 7):
        rng = np.random.default_rng(seed)
        self._rng = np.random.default_rng()
        M = self.MAX_NODES

        # nodes, in the order they are born: spread over the shells
        d = rng.normal(size=(M, 3))
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        shell = rng.choice(len(_SHELLS), M, p=_SHELL_P)
        rad = np.array(_SHELLS)[shell] + rng.uniform(-0.03, 0.03, M)
        self.base = d * rad[:, None]
        self.ph = rng.uniform(0, 2 * math.pi, (M, 3))

        # synapses: each node reaches back to its nearest older ones, plus a few
        # long fibres across the globe — so growing adds to the web, in order
        edges, born = [], []
        for i in range(1, M):
            dist = np.linalg.norm(self.base[:i] - self.base[i], axis=1)
            near = np.argsort(dist)[:(1 if i < 4 else 2 + int(rng.random() < 0.35))]
            for j in near:
                edges.append((int(j), i)); born.append(i)
            if i > 20 and rng.random() < 0.10:
                edges.append((int(rng.integers(0, i)), i)); born.append(i)
        self.edges = np.array(edges, dtype=int)
        self.e_born = np.array(born, dtype=int)
        self.adj: list[list[tuple[int, int]]] = [[] for _ in range(M)]
        for e, (i, j) in enumerate(self.edges):
            self.adj[i].append((e, j))
            self.adj[j].append((e, i))
        self.glow = np.zeros(M)
        self.birth = np.full(M, -1e9)          # when each node bloomed (monotonic seconds)

        # growth
        self.n = float(self.MIN_NODES)
        self.target = float(self.MIN_NODES)
        self._known = None

        # pulses in flight
        self.p_edge = np.zeros(0, dtype=int)
        self.p_rev = np.zeros(0, dtype=bool)
        self.p_k = np.zeros(0)
        self.p_life = np.zeros(0)

        # the film texture: faint broken rings, circuit fragments, dust
        R = self.RINGS
        self.r_u, self.r_v = _basis(rng, R)
        self.r_rad = rng.uniform(0.9, 1.12, R)
        self.r_speed = rng.uniform(0.2, 0.6, R) * rng.choice([-1, 1], R)
        self.r_mask = np.zeros((R, _RING_SAMPLES), dtype=bool)
        for i in range(R):
            k = 0
            while k < _RING_SAMPLES:
                on = int(rng.integers(3, 12))
                self.r_mask[i, k:k + on] = True
                k += on + int(rng.integers(3, 10))
        self.theta = np.linspace(0, 2 * math.pi, _RING_SAMPLES, endpoint=False)
        self.r_ph = np.zeros(R)
        F = self.FRAGS
        self.f_u, self.f_v = _basis(rng, F)
        self.f_rad = rng.uniform(0.35, 1.05, F)
        self.f_ang = rng.uniform(0, 2 * math.pi, F)
        self.f_len = rng.uniform(0.03, 0.09, F)
        self.f_speed = rng.uniform(0.1, 0.4, F) * rng.choice([-1, 1], F)
        dd = rng.normal(size=(self.DUST, 3))
        self.d_pos = dd / np.linalg.norm(dd, axis=1, keepdims=True) * (rng.uniform(0.1, 1.05, (self.DUST, 1)) ** 0.5)

        self.t = 0.0
        self.yaw = 0.0
        self.core_ph = 0.0
        self.amp = 0.0
        self.state = "IDLE"
        self.muted = False
        self.direct_glow = False
        self._glow_pm = None
        self._glow_key = None
        self._halos: dict[tuple, QPixmap] = {}

        # what each node stands for: the learning history, oldest first
        self.items: list[dict] = []
        self.recent = np.zeros(self.MAX_NODES, dtype=bool)     # mapped to the last 48 h
        self.hover: int | None = None
        self._proj = None                  # last frame's node positions, for the mouse

    # ── growth ──────────────────────────────────────────────────────────────

    # ── what the nodes stand for ────────────────────────────────────────────

    def set_items(self, items) -> None:
        """The learning history (oldest first): node i stands for the item at the
        same point in time, so the newest nodes are the newest things learned."""
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
        return (x - cx) ** 2 + (y - cy) ** 2 <= (r * 1.05) ** 2

    def node_at(self, x: float, y: float, reach: float = 10.0) -> int | None:
        """The front node under the mouse, if any."""
        if not self._proj or not self.items:
            return None
        nx, ny, nd, n = self._proj[:4]
        d2 = (nx - x) ** 2 + (ny - y) ** 2
        d2 = np.where(nd > 0.35, d2, np.inf)          # the back of the globe is not clickable
        i = int(np.argmin(d2))
        return i if d2[i] <= reach * reach else None

    def set_knowledge(self, known) -> None:
        """How much the assistant knows (memories + things learned). The first
        reading sets the size at once; later ones grow it, node by node."""
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
            self.glow[i] = min(1.0, self.glow[i] + 0.45)
        if not es:
            return
        n = len(es)
        self.p_edge = np.concatenate([self.p_edge, np.array(es, dtype=int)])
        self.p_rev = np.concatenate([self.p_rev, np.array(rv, dtype=bool)])
        self.p_k = np.concatenate([self.p_k, np.zeros(n)])
        self.p_life = np.concatenate([self.p_life, self._rng.uniform(0.7, 1.4, n)])

    def step(self, dt: float, amp: float, speaking: bool = False,
             muted: bool = False, state: str = "IDLE") -> None:
        dt = max(0.0, min(dt, 0.1))
        mode = self._mode(speaking, muted, state)
        cfg = _STATE[mode]
        self.state, self.muted = mode, muted
        tempo = cfg["tempo"] * (1.0 + 0.6 * self.amp)
        self.t += dt * tempo
        self.yaw += dt * 0.10 * cfg["tempo"]
        self.core_ph += dt * cfg["spin"] * (1.0 + 1.5 * self.amp)
        self.amp += (float(amp) - self.amp) * min(1.0, dt * 9.0)
        self.glow *= math.exp(-dt * 2.4)
        self.r_ph += dt * tempo * self.r_speed
        self.f_ang += dt * tempo * self.f_speed

        # growth: about two nodes a second until the mind has caught up
        if self.n < self.target:
            before = int(self.n)
            self.n = min(self.target, self.n + dt * 2.0)
            now = time.monotonic()
            for i in range(before, int(self.n)):
                self.birth[i] = now
                self.glow[i] = 1.0
                self._spawn([i])                    # a newborn fires at once
        elif self.n > self.target:                  # forgetting: fade back quietly
            self.n = max(self.target, self.n - dt * 4.0)

        if len(self.p_edge):
            # pulses on synapses that faded with forgetting go first
            alive = self.e_born[self.p_edge] < self.alive
            if not alive.all():
                self.p_edge, self.p_rev = self.p_edge[alive], self.p_rev[alive]
                self.p_k, self.p_life = self.p_k[alive], self.p_life[alive]
            self.p_k += dt * tempo / self.p_life
            done = self.p_k >= 1.0
            if done.any():
                ends = np.where(self.p_rev[done], self.edges[self.p_edge[done], 0],
                                self.edges[self.p_edge[done], 1])
                np.add.at(self.glow, ends, 0.8)
                np.clip(self.glow, 0, 1, out=self.glow)
                keep = ~done
                self.p_edge, self.p_rev = self.p_edge[keep], self.p_rev[keep]
                self.p_k, self.p_life = self.p_k[keep], self.p_life[keep]
                load = len(self.p_edge) / self.MAX_PULSES
                self._spawn(ends[self._rng.random(len(ends)) < cfg["chain"] * (1 - load)])

        n_new = self._rng.poisson((cfg["rate"] + cfg["gain"] * self.amp) * dt * (0.5 + self.alive / 240))
        if n_new:
            self._spawn(self._rng.integers(0, max(1, self.alive), n_new))

    # ── drawing ─────────────────────────────────────────────────────────────

    def _project(self, P: np.ndarray, cx: float, cy: float, r: float):
        cyaw, syaw = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(0.30), math.sin(0.30)
        x, y, z = P[..., 0], P[..., 1], P[..., 2]
        x1 = x * cyaw + z * syaw
        z1 = -x * syaw + z * cyaw
        y1 = y * cp - z1 * sp
        z2 = y * sp + z1 * cp
        persp = 3.4 / (3.4 + z2)
        return cx + x1 * r * persp, cy - y1 * r * persp, np.clip((1.0 - z2) / 2.0, 0.0, 1.0)

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
            c0 = QColor(col); c0.setAlpha(200)
            c1 = QColor(col); c1.setAlpha(60)
            c2 = QColor(col); c2.setAlpha(0)
            g.setColorAt(0.0, c0); g.setColorAt(0.3, c1); g.setColorAt(1.0, c2)
            hp.setPen(Qt.PenStyle.NoPen); hp.setBrush(g)
            hp.drawEllipse(QPointF(rad, rad), rad, rad)
            hp.end()
            self._halos[key] = spr
        return spr

    _CATS = {"lessons": "LIÇÃO", "habits": "HÁBITO", "identity": "IDENTIDADE", "home": "CASA",
             "preferences": "PREFERÊNCIA", "preference": "PREFERÊNCIA", "relationships": "PESSOAS",
             "projects": "PROJETO", "wishes": "DESEJO", "notes": "NOTA", "personal": "PESSOAL",
             "monitors": "MONITORADO"}
    _SOURCES = {"correction": "sua correção", "assistant": "ele guardou", "user": "você",
                "consolidation": "arrumação diária", "default": "padrão", "import": "importado",
                "app": "app", "revert": "desfeito"}

    def _tooltip(self, p, i, x, y, r, main, lighten, lum) -> None:
        it = self.item_for(i)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setPen(QPen(QColor(255, 248, 230), 1.6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(x, y), 7, 7)
        cat = self._CATS.get(it.get("cat", ""), str(it.get("cat", "")).upper())
        ts = str(it.get("ts", ""))
        when = f"{ts[8:10]}/{ts[5:7]} {ts[11:16]}" if len(ts) >= 16 else ts
        src = self._SOURCES.get(it.get("source", ""), it.get("source", ""))
        head = f"{cat} · {when} · {src}"
        key = str(it.get("key", "")).replace("_", " ")
        val = str(it.get("value") or "")
        if it.get("action") == "delete":
            val = "esquecido: " + val
        f = QFont()
        f.setPointSizeF(max(7.5, r / 24))
        p.setFont(f)
        fm = p.fontMetrics()
        width = int(min(360, max(200, r * 1.5)))
        words, lines, cur = val.split(), [], ""
        for w in words:
            if fm.horizontalAdvance(cur + " " + w) > width - 16 and cur:
                lines.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            lines.append(cur)
        lines = lines[:4]
        lh = fm.height()
        h = 10 + lh * (2 + len(lines)) + 6
        bx = x + 14 if x + 14 + width < self._proj[4] + r * 1.6 else x - 14 - width
        by = y - h / 2
        box = QRectF(bx, by, width, h)
        p.setPen(QPen(lum(main, 0.9), 1))
        p.setBrush(QColor(6, 14, 30, 235))
        p.drawRoundedRect(box, 4, 4)
        p.setPen(lighten(main, 0.35))
        p.drawText(QPointF(bx + 8, by + 6 + lh * 0.8), head)
        p.setPen(QColor(255, 248, 230))
        p.drawText(QPointF(bx + 8, by + 6 + lh * 1.8), fm.elidedText(key, Qt.TextElideMode.ElideRight, width - 16))
        p.setPen(QColor(200, 215, 235))
        for k, line in enumerate(lines):
            p.drawText(QPointF(bx + 8, by + 6 + lh * (2.8 + k)), line)
        p.drawText(QPointF(bx + 8, by + h - 4), "")
        p.setBrush(Qt.BrushStyle.NoBrush)

    def paint(self, p: QPainter, cx: float, cy: float, r: float,
              main: QColor, acc: QColor, bg: QColor) -> None:
        main, acc = GOLD, HOT          # the hologram's own colours, not the theme's
        white = QColor(255, 248, 230)

        def lighten(col: QColor, k: float) -> QColor:
            return QColor(int(col.red() + (white.red() - col.red()) * k),
                          int(col.green() + (white.green() - col.green()) * k),
                          int(col.blue() + (white.blue() - col.blue()) * k))

        def lum(col: QColor, k: float) -> QColor:
            k = max(0.0, min(1.0, k))
            return QColor(int(col.red() * k), int(col.green() * k), int(col.blue() * k))

        dim = _STATE[self.state]["dim"]
        R = r * 1.0
        unit = max(0.7, r / 180)
        n = max(2, self.alive)
        grown = n / self.MAX_NODES                   # 0..1: the glow deepens as it grows
        shown = n
        if not self.direct_glow:
            # the CPU painter (no OpenGL here) antialiases every curve itself:
            # past ~140 nodes that is 20 ms a frame, so it draws the oldest part
            n = min(n, 140)

        # 1. atmosphere
        gr = int(r * 1.3)
        strength = int((34 + 26 * grown + 30 * self.amp) * dim)
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

        # light adds up from here: crossings glow, dark is transparent
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)

        # 2. the film texture: faint broken rings and circuit fragments
        a = self.theta[None, :] + self.r_ph[:, None]
        P = self.r_rad[:, None, None] * (np.cos(a)[..., None] * self.r_u[:, None, :]
                                         + np.sin(a)[..., None] * self.r_v[:, None, :])
        sx, sy, dp = self._project(P, cx, cy, R)
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
                p.setPen(QPen(lum(main, (0.12 + 0.4 * d) * dim), (0.8 + 0.8 * d) * unit,
                              Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawPolyline(QPolygonF([QPointF(sx[i, j], sy[i, j])
                                          for j in range(k, min(e + 1, _RING_SAMPLES))]))
                k = e
        fa = self.f_ang
        p0 = self.f_rad[:, None] * (np.cos(fa)[:, None] * self.f_u + np.sin(fa)[:, None] * self.f_v)
        tang = -np.sin(fa)[:, None] * self.f_u + np.cos(fa)[:, None] * self.f_v
        x0, y0, d0 = self._project(p0, cx, cy, R)
        x1, y1, _ = self._project(p0 + tang * self.f_len[:, None], cx, cy, R)
        for lv in range(2):
            sel = np.where((d0 > 0.5) == bool(lv))[0]
            if len(sel):
                p.setPen(QPen(lum(lighten(main, 0.3), (0.2 + 0.25 * lv) * dim), (0.9 + 0.5 * lv) * unit))
                p.drawLines([QLineF(x0[j], y0[j], x1[j], y1[j]) for j in sel])

        # 3. synapses: nodes sway a little and breathe with the voice
        sway = 0.02 * np.sin(self.t * np.array([0.8, 0.7, 0.6]) + self.ph[:n])
        breathe = 1.0 + 0.018 * math.sin(self.t * 0.9) + 0.05 * self.amp
        Pn = (self.base[:n] + sway) * breathe
        nx, ny, nd = self._project(Pn, cx, cy, R)
        self._proj = (nx, ny, nd, n, cx, cy, r)
        if self.items and getattr(self, "_mapped_n", 0) != self.alive:   # grew: remap
            self._map_recent()
            self._mapped_n = self.alive
        n_e = int(np.searchsorted(self.e_born, n))
        E = self.edges[:n_e]
        ea, eb = E[:, 0], E[:, 1]
        # each filament bends along the globe: its control point makes the curve
        # pass over the shell midway
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
        lvl = (0.16 + 0.42 * e_depth + 0.5 * e_act) * dim
        buckets = np.minimum((lvl * 7).astype(int), 6)
        for lv in range(7):
            sel = np.where(buckets == lv)[0]
            if not len(sel):
                continue
            path = QPainterPath()
            for e in sel:
                path.moveTo(nx[ea[e]], ny[ea[e]])
                path.quadTo(qx[e], qy[e], nx[eb[e]], ny[eb[e]])
            p.setPen(QPen(lum(lighten(main, 0.05 * lv), (lv + 0.8) / 7.5), (0.8 + 0.16 * lv) * unit))
            p.drawPath(path)

        # 4. pulses: a bright head gliding along the filament, a fading tail
        on = self.p_edge < n_e                  # only on the filaments drawn
        if on.any():
            e = self.p_edge[on]
            rev, pk = self.p_rev[on], self.p_k[on]
            u0 = np.where(rev, eb[e], ea[e])
            u1 = np.where(rev, ea[e], eb[e])
            X0, Y0, X1, Y1 = nx[u0], ny[u0], nx[u1], ny[u1]
            QX, QY = qx[e], qy[e]
            head = lighten(acc, 0.5)
            hw = max(1.6, r / 95)
            for s in range(_TAIL):
                k = np.clip(pk - s * _TAIL_GAP, 0, 1)
                k = np.where(k < 0.5, 2 * k * k, 1 - (-2 * k + 2) ** 2 / 2)
                u = 1 - k
                px = u * u * X0 + 2 * u * k * QX + k * k * X1
                py = u * u * Y0 + 2 * u * k * QY + k * k * Y1
                f = (1 - s / _TAIL) * dim
                p.setPen(QPen(lum(head, 0.2 + 0.8 * f), hw * (1 - s * 0.07),
                              Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawPoints([QPointF(px[i], py[i]) for i in range(len(e))])

        # 5. dust: specks of light inside the globe
        yaw = self.yaw * 0.6
        rot = np.array([[math.cos(yaw), 0, math.sin(yaw)], [0, 1, 0], [-math.sin(yaw), 0, math.cos(yaw)]])
        qx2, qy2, qd2 = self._project(self.d_pos @ rot.T, cx, cy, R)
        sel = np.where(qd2 > 0.25)[0]
        p.setPen(QPen(lum(lighten(main, 0.3), 0.45 * dim), 1.1 * unit, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawPoints([QPointF(qx2[j], qy2[j]) for j in sel])

        # 6. the core: a bright swirl spinning with the work being done
        arms = 5
        tt = np.linspace(0, 1, 28)
        grow = (1.0 + 0.25 * self.amp) * (0.85 + 0.3 * grown)
        for a_i in range(arms):
            ang = self.core_ph * 2.2 + a_i * 2 * math.pi / arms + tt * 3.6
            rr = (0.04 + 0.22 * tt) * r * grow
            p.setPen(QPen(lum(lighten(acc, 0.5), (1.0 - 0.1 * a_i) * dim),
                          max(1.0, r / 150) * (1.5 - 0.15 * a_i), Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawPolyline(QPolygonF([QPointF(cx + math.cos(ang[j]) * rr[j], cy + math.sin(ang[j]) * rr[j] * 0.8)
                                      for j in range(len(tt))]))
        cr = max(6, int(r * (0.2 + 0.1 * self.amp)))
        p.setOpacity(min(1.0, 0.75 * dim + 0.3 * self.amp))
        p.drawPixmap(QPointF(cx - cr, cy - cr), self._halo(lighten(acc, 0.5), cr))
        p.setOpacity(1.0)

        # 6b. what was learned in the last 48 h pulses, slowly
        rec = np.where(self.recent[:n] & (nd > 0.2))[0][:40]
        if len(rec):
            spr = self._halo(white, max(5, int(r / 26)))
            hr = spr.width() / 2
            for i in rec:
                p.setOpacity(float(0.25 + 0.35 * (0.5 + 0.5 * math.sin(self.t * 4 + i))) * dim)
                p.drawPixmap(QPointF(nx[i] - hr, ny[i] - hr), spr)
            p.setOpacity(1.0)

        # 7. nodes: halos on the ones just fired or just born, then the bodies
        hot = np.where((self.glow[:n] > 0.1) | (young > 0))[0]
        if len(hot):
            heat = np.maximum(self.glow[hot], young[hot])
            hot = hot[np.argsort(heat)[-self.MAX_HALOS:]]
            for i in hot:
                gl = float(max(self.glow[i], young[i]))
                born = young[i] > 0
                hr = max(4, int(r / (22 if born else 38) * (1 + 1.6 * gl) * (0.7 + 0.5 * nd[i])))
                p.setOpacity(min(1.0, (0.4 + 0.6 * gl) * dim))
                p.drawPixmap(QPointF(nx[i] - hr, ny[i] - hr),
                             self._halo(white if born else lighten(acc, 0.25), hr))
            p.setOpacity(1.0)
        size = np.clip(np.round((0.9 + 1.4 * nd) * unit * 2) / 2, 0.5, 4.5)
        level = np.minimum(((0.3 + 0.5 * nd + 0.6 * np.maximum(self.glow[:n], young)) * dim * 5).astype(int), 4)
        groups: dict[tuple, list] = {}
        for i in range(n):
            groups.setdefault((int(level[i]), float(size[i])), []).append(QPointF(nx[i], ny[i]))
        for (lv, sz), pts in sorted(groups.items()):
            p.setPen(QPen(lighten(main, 0.18 * lv) if lv >= 3 else lum(main, 0.45 + lv * 0.18), sz * 2,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPoints(pts)

        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # 7b. the node under the mouse: a ring and what it stands for
        h = self.hover
        if h is not None and h < n and self.items:
            self._tooltip(p, h, nx[h], ny[h], r, main, lighten, lum)

        # 8. how grown it is, quietly under the globe
        if self._known is not None:
            p.setPen(lum(lighten(main, 0.2), 0.75 * dim))
            f = QFont()
            f.setPointSizeF(max(6.5, r / 26))
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, max(1.0, r / 120))
            p.setFont(f)
            txt = f"{shown} SINAPSES"
            w = p.fontMetrics().horizontalAdvance(txt)
            p.drawText(QPointF(cx - w / 2, cy + r * 1.13), txt)
