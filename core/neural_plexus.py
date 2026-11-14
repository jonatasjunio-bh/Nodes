"""
core/neural_plexus.py — the golden "plexus" sphere of the reference layout.

    nodes       points of light spread over the surface of a sphere, turning slowly
    links       straight golden lines between each node and its nearest
                neighbours: an irregular web of triangles, bright in front, faint
                behind
    the core    a six-armed white-gold swirl over a warm haze, spinning harder
                when the assistant thinks or speaks
    pulses      sparks running along the links; a node glows when one arrives

It grows with what the assistant learns (nodes = 60 + 1.5 × what it knows, up to
260) and each node stands for one thing learned, like core/neural_sphere.py —
same interface: step(), paint(), set_knowledge(), set_items(), over(),
node_at(), item_for(), hover.
"""

from __future__ import annotations

import math
import time

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap, QPolygonF, QRadialGradient

GOLD = QColor(255, 138, 18)
CREAM = QColor(255, 214, 130)
LINE = QColor(255, 158, 52)
HOT = QColor(255, 214, 140)

_STATE = {
    "SLEEPING":  dict(tempo=0.35, rate=0.8, gain=0.0, chain=0.05, spin=0.5, dim=0.55),
    "MUTED":     dict(tempo=0.35, rate=0.8, gain=0.0, chain=0.05, spin=0.5, dim=0.55),
    "IDLE":      dict(tempo=0.7, rate=2.5, gain=6.0, chain=0.12, spin=0.9, dim=1.0),
    "LISTENING": dict(tempo=0.8, rate=3.0, gain=14.0, chain=0.14, spin=1.1, dim=1.0),
    "THINKING":  dict(tempo=1.4, rate=10.0, gain=2.0, chain=0.4, spin=3.0, dim=1.0),
    "SPEAKING":  dict(tempo=1.1, rate=4.0, gain=18.0, chain=0.2, spin=1.9, dim=1.0),
}
_BLOOM = 2.6


class PlexusSphere:
    MAX_NODES = 240
    MIN_NODES = 55
    PER_KNOWLEDGE = 1.1
    K = 3                       # links per node (to its nearest neighbours)
    MAX_PULSES = 40
    ARMS = 5

    def __init__(self, seed: int = 5):
        rng = np.random.default_rng(seed)
        self._rng = np.random.default_rng()
        M = self.MAX_NODES
        # spread over the sphere, in birth order (the growth adds them in place)
        # spread over the globe at every size, but loosely: the best of only 3
        # random places (the farthest from those already there). With 24 the
        # spacing came out so even the web looked like hexagons; 3 keeps the
        # globe covered and leaves the gaps and clusters of a real constellation
        pts = [self._rand_dir(rng)]
        for _ in range(1, M):
            cand = np.array([self._rand_dir(rng) for _ in range(3)])
            have = np.array(pts)
            far = np.min(np.linalg.norm(cand[:, None, :] - have[None, :, :], axis=2), axis=1)
            pts.append(cand[int(np.argmax(far))])
        # a thin shell, so the web has a little depth without crowding the rim
        self.base = np.array(pts) * rng.uniform(0.86, 1.0, (M, 1))
        self.bright = rng.uniform(0.55, 1.0, M)          # some nodes simply shine more
        edges, born = [], []
        for i in range(1, M):
            dist = np.linalg.norm(self.base[:i] - self.base[i], axis=1)
            # 2 to 4 links, to any of the eight nearest, and now and then a long
            # one: lines of every length, crossing, like the reference
            near = np.argsort(dist)[:8]
            k = min(len(near), int(rng.choice([2, 3, 3, 4])) if i > 8 else 2)
            pick = [near[0]] + rng.choice(near[1:], size=max(0, min(len(near) - 1, k - 1)),
                                          replace=False).tolist() if len(near) > 1 else near.tolist()
            for j in pick:
                if dist[j] < 0.95 or i < 12:
                    edges.append((int(j), i)); born.append(i)
            if i > 20 and rng.random() < 0.3:              # long lines across the globe
                far = np.where((dist > 0.6) & (dist < 1.25))[0]
                if len(far):
                    edges.append((int(rng.choice(far)), i)); born.append(i)
        self.edges = np.array(edges, dtype=int)
        self.e_born = np.array(born, dtype=int)
        self.adj: list[list[tuple[int, int]]] = [[] for _ in range(M)]
        for e, (i, j) in enumerate(self.edges):
            self.adj[i].append((e, j)); self.adj[j].append((e, i))
        self.glow = np.zeros(M)
        self.birth = np.full(M, -1e9)
        self.n = float(self.MIN_NODES)
        self.target = float(self.MIN_NODES)
        self._known = None
        self.p_edge = np.zeros(0, dtype=int)
        self.p_rev = np.zeros(0, dtype=bool)
        self.p_k = np.zeros(0)
        self.p_life = np.zeros(0)
        self.items: list[dict] = []
        self.recent = np.zeros(M, dtype=bool)
        self.hover: int | None = None
        self._proj = None
        self._mapped_n = 0
        self.t = 0.0
        self.yaw = 0.4
        self.core_ph = 0.0
        self.amp = 0.0
        self.state = "IDLE"
        self.muted = False
        self.direct_glow = False
        self._halos: dict[tuple, QPixmap] = {}

    @staticmethod
    def _rand_dir(rng):
        v = rng.normal(size=3)
        return v / np.linalg.norm(v)

    # ── growth and what the nodes stand for ─────────────────────────────────

    @property
    def alive(self) -> int:
        return int(self.n)

    def set_knowledge(self, known) -> None:
        if known is None:
            return
        known = max(0, int(known))
        target = min(self.MAX_NODES, self.MIN_NODES + self.PER_KNOWLEDGE * known)
        if self._known is None:
            self.n = target
        self._known = known
        self.target = target

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
        self._mapped_n = n

    def item_for(self, i: int) -> dict:
        if not self.items or i is None:
            return {}
        n = max(1, self.alive)
        return self.items[min(len(self.items) - 1, int(i * len(self.items) / n))]

    def over(self, x: float, y: float) -> bool:
        if not self._proj:
            return False
        cx, cy, r = self._proj[4:7]
        return (x - cx) ** 2 + (y - cy) ** 2 <= (r * 1.05) ** 2

    def node_at(self, x: float, y: float, reach: float = 10.0):
        if not self._proj or not self.items:
            return None
        nx, ny, nd = self._proj[:3]
        d2 = np.where(nd > 0.45, (nx - x) ** 2 + (ny - y) ** 2, np.inf)
        i = int(np.argmin(d2))
        return i if d2[i] <= reach * reach else None

    # ── simulation ──────────────────────────────────────────────────────────

    def _mode(self, speaking, muted, state):
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
        alive = self.alive
        es, rv = [], []
        for i in list(nodes)[:room]:
            opts = [(e, j) for e, j in self.adj[i] if j < alive]
            if not opts:
                continue
            e, _ = opts[self._rng.integers(len(opts))]
            es.append(e); rv.append(self.edges[e, 0] != i)
            self.glow[i] = min(1.0, self.glow[i] + 0.4)
        if es:
            n = len(es)
            self.p_edge = np.concatenate([self.p_edge, np.array(es, dtype=int)])
            self.p_rev = np.concatenate([self.p_rev, np.array(rv, dtype=bool)])
            self.p_k = np.concatenate([self.p_k, np.zeros(n)])
            self.p_life = np.concatenate([self.p_life, self._rng.uniform(0.8, 1.6, n)])

    def step(self, dt: float, amp: float, speaking: bool = False, muted: bool = False,
             state: str = "IDLE") -> None:
        dt = max(0.0, min(dt, 0.1))
        mode = self._mode(speaking, muted, state)
        cfg = _STATE[mode]
        self.state, self.muted = mode, muted
        tempo = cfg["tempo"] * (1.0 + 0.6 * self.amp)
        self.t += dt * tempo
        self.yaw += dt * 0.07 * cfg["tempo"]
        self.core_ph += dt * cfg["spin"] * (1.0 + 1.5 * self.amp)
        self.amp += (float(amp) - self.amp) * min(1.0, dt * 9.0)
        self.glow *= math.exp(-dt * 2.2)
        if self.n < self.target:
            before = int(self.n)
            self.n = min(self.target, self.n + dt * 2.0)
            now = time.monotonic()
            for i in range(before, int(self.n)):
                self.birth[i] = now; self.glow[i] = 1.0; self._spawn([i])
        elif self.n > self.target:
            self.n = max(self.target, self.n - dt * 4.0)
        if len(self.p_edge):
            keep = self.e_born[self.p_edge] < self.alive
            self.p_edge, self.p_rev, self.p_k, self.p_life = (self.p_edge[keep], self.p_rev[keep],
                                                              self.p_k[keep], self.p_life[keep])
            self.p_k += dt * tempo / self.p_life
            done = self.p_k >= 1.0
            if done.any():
                ends = np.where(self.p_rev[done], self.edges[self.p_edge[done], 0],
                                self.edges[self.p_edge[done], 1])
                np.add.at(self.glow, ends, 0.8)
                np.clip(self.glow, 0, 1, out=self.glow)
                k = ~done
                self.p_edge, self.p_rev, self.p_k, self.p_life = (self.p_edge[k], self.p_rev[k],
                                                                  self.p_k[k], self.p_life[k])
                load = len(self.p_edge) / self.MAX_PULSES
                self._spawn(ends[self._rng.random(len(ends)) < cfg["chain"] * (1 - load)])
        n_new = self._rng.poisson((cfg["rate"] + cfg["gain"] * self.amp) * dt)
        if n_new:
            self._spawn(self._rng.integers(0, max(1, self.alive), n_new))

    # ── drawing ─────────────────────────────────────────────────────────────

    def _project(self, P, cx, cy, r):
        cyaw, syaw = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(0.25), math.sin(0.25)
        x, y, z = P[..., 0], P[..., 1], P[..., 2]
        x1 = x * cyaw + z * syaw
        z1 = -x * syaw + z * cyaw
        y1 = y * cp - z1 * sp
        z2 = y * sp + z1 * cp
        persp = 3.6 / (3.6 + z2)
        return cx + x1 * r * persp, cy - y1 * r * persp, np.clip((1.0 - z2) / 2.0, 0.0, 1.0)

    def _halo(self, col: QColor, rad: int, core: float = 0.25) -> QPixmap:
        key = (col.rgb(), rad, core)
        spr = self._halos.get(key)
        if spr is None:
            if len(self._halos) > 120:
                self._halos.clear()
            spr = QPixmap(2 * rad, 2 * rad)
            spr.fill(Qt.GlobalColor.transparent)
            hp = QPainter(spr)
            hp.setRenderHint(QPainter.RenderHint.Antialiasing)
            g = QRadialGradient(QPointF(rad, rad), rad)
            c0 = QColor(col); c0.setAlpha(255)
            c1 = QColor(col); c1.setAlpha(105)
            c2 = QColor(col); c2.setAlpha(0)
            g.setColorAt(0.0, c0); g.setColorAt(core, c1); g.setColorAt(1.0, c2)
            hp.setPen(Qt.PenStyle.NoPen); hp.setBrush(g)
            hp.drawEllipse(QPointF(rad, rad), rad, rad)
            hp.end()
            self._halos[key] = spr
        return spr

    def paint(self, p: QPainter, cx: float, cy: float, r: float,
              main: QColor, acc: QColor, bg: QColor) -> None:
        def lum(col: QColor, k: float) -> QColor:
            k = max(0.0, min(1.0, k))
            return QColor(int(col.red() * k), int(col.green() * k), int(col.blue() * k))

        dim = _STATE[self.state]["dim"]
        n = max(4, self.alive)
        unit = max(0.7, r / 160)

        sway = 0.012 * np.sin(self.t * 0.7 + np.arange(n)[:, None] * np.array([0.9, 1.3, 1.7]))
        P = (self.base[:n] + sway) * (1.0 + 0.03 * self.amp)
        nx, ny, nd = self._project(P, cx, cy, r)
        self._proj = (nx, ny, nd, n, cx, cy, r)
        if self.items and self._mapped_n != n:
            self._map_recent()

        # Three layers, so the light can behave like light: the back half drawn
        # small and blown up (out of focus), the front sharp, and a blurred copy
        # of the front added over everything (bloom: lines and nodes bleed light).
        S = int(r * 2.5)
        ox, oy = cx - S / 2, cy - S / 2
        back = QImage(S // 2, S // 2, QImage.Format.Format_ARGB32_Premultiplied)
        front = QImage(S, S, QImage.Format.Format_ARGB32_Premultiplied)
        back.fill(Qt.GlobalColor.transparent)
        front.fill(Qt.GlobalColor.transparent)
        bp, fp = QPainter(back), QPainter(front)
        for q, k in ((bp, 0.5), (fp, 1.0)):
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            q.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            q.scale(k, k)
            q.translate(-ox, -oy)

        # links: the back ones go to the blurred layer, the front ones stay sharp
        n_e = int(np.searchsorted(self.e_born, n))
        E = self.edges[:n_e]
        ea, eb = E[:, 0], E[:, 1]
        depth = (nd[ea] + nd[eb]) / 2
        act = np.maximum(self.glow[ea], self.glow[eb])
        lvl = np.clip(depth * 0.85 + act * 0.5, 0, 1)
        # the back half: thin, faint and sharp — blurred it read as dust
        for lo, hi, alpha, width in ((0.0, 0.35, 0.1, 0.55), (0.35, 0.5, 0.2, 0.6),
                                     (0.5, 0.65, 0.42, 0.7), (0.65, 0.82, 0.62, 0.85), (0.82, 2.0, 0.88, 1.0)):
            sel = np.where((lvl >= lo) & (lvl < hi))[0]
            if len(sel):
                fp.setPen(QPen(lum(LINE, alpha * dim), width * unit))
                fp.drawLines([QLineF(nx[ea[e]], ny[ea[e]], nx[eb[e]], ny[eb[e]]) for e in sel])

        # the swirl: five arms, thickest just out of the centre, fading out
        arms = []
        tt = np.linspace(0, 1, 49)
        for k in range(self.ARMS):
            th = self.core_ph * 1.6 + k * 2 * math.pi / self.ARMS + tt * 3.6
            rr = r * (0.02 + 0.34 * tt ** 1.15) * (1 + 0.15 * self.amp)
            arms.append([QPointF(cx + math.cos(th[j]) * rr[j], cy + math.sin(th[j]) * rr[j])
                         for j in range(len(tt))])
        stretches = ((0, 13, 1.0, 1.0), (12, 25, 1.0, 1.0), (24, 37, 0.85, 0.85), (36, 49, 0.6, 0.6))
        for wide, alpha in ((4.5, 0.14), (1.4, 0.95)):
            for a0, a1, ka, kw in stretches:
                fp.setPen(QPen(lum(CREAM, alpha * ka * dim), wide * kw * unit, Qt.PenStyle.SolidLine,
                               Qt.PenCapStyle.RoundCap))
                for pts in arms:
                    fp.drawPolyline(QPolygonF(pts[a0:a1]))
        cr = max(6, int(r * 0.1))
        fp.setOpacity(0.8)
        fp.drawPixmap(QPointF(cx - cr, cy - cr), self._halo(CREAM, cr, 0.2))
        fp.setOpacity(1.0)

        # pulses gliding along the links
        if len(self.p_edge):
            on = self.p_edge < n_e
            e = self.p_edge[on]
            if len(e):
                u0 = np.where(self.p_rev[on], eb[e], ea[e]); u1 = np.where(self.p_rev[on], ea[e], eb[e])
                k = self.p_k[on]
                px, py = nx[u0] + (nx[u1] - nx[u0]) * k, ny[u0] + (ny[u1] - ny[u0]) * k
                fp.setPen(QPen(lum(HOT, dim), 2.6 * unit, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                fp.drawPoints([QPointF(px[i], py[i]) for i in range(len(e)) if (nd[u0[i]] + nd[u1[i]]) > 0.7])

        # nodes: halo + bright dot; the back ones on the blurred layer
        now = time.monotonic()
        young = np.clip(1 - (now - self.birth[:n]) / _BLOOM, 0, 1)
        heat = np.maximum(self.glow[:n], young)
        pulse = np.where(self.recent[:n], 0.5 + 0.5 * np.sin(self.t * 4 + np.arange(n)), 0.0)
        # a short, tight glow only: big halos summed with the bloom blew the
        # points out into blobs; the sharp core is drawn last, over everything
        for i in np.argsort(nd):
            d = nd[i]
            if d < 0.45 and heat[i] < 0.1:
                continue                         # behind: a faint dot only (below), no glow
            hr = max(3, int((3 + 7 * d + 6 * heat[i] + 3 * pulse[i]) * unit))
            fp.setOpacity(min(1.0, (0.35 + 0.75 * d) * self.bright[i] * dim + 0.45 * heat[i]))
            fp.drawPixmap(QPointF(nx[i] - hr, ny[i] - hr), self._halo(GOLD, hr, 0.16))
        bp.setOpacity(1.0); fp.setOpacity(1.0)
        fr = np.where(nd >= 0.45)[0]
        bk = np.where(nd < 0.45)[0]
        fp.setPen(QPen(lum(GOLD, 0.35 * dim), 1.6 * unit, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        fp.drawPoints([QPointF(nx[i], ny[i]) for i in bk])
        bp.end(); fp.end()

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # warm haze behind it all
        g = QRadialGradient(QPointF(cx, cy), r * 1.1)
        # a warm dark inside (the reference's brown-black), brighter at the heart
        g.setColorAt(0.0, QColor(255, 150, 45, int((90 + 30 * self.amp) * dim)))
        g.setColorAt(0.3, QColor(150, 80, 20, int(110 * dim)))
        g.setColorAt(0.8, QColor(60, 34, 12, int(120 * dim)))
        g.setColorAt(1.0, QColor(40, 24, 10, 0))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), r * 1.1, r * 1.1)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        smooth = Qt.TransformationMode.SmoothTransformation
        keep = Qt.AspectRatioMode.IgnoreAspectRatio
        box = QRectF(ox, oy, S, S)
        def blur(img: QImage, f: int) -> QImage:
            # shrinking in steps averages smoothly; one big step leaves blotches
            w = img.width()
            while w // 2 >= max(6, S // f):
                w //= 2
                img = img.scaled(w, w, keep, smooth)
            return img
        # bloom: two soft copies of the front, a near one and a wide one
        p.setOpacity(0.14)
        p.drawImage(box, blur(front, 10))
        # and the front itself, sharp
        p.setOpacity(1.0)
        p.drawImage(QPointF(ox, oy), front)
        # the points, crisp: an amber dot with a small white-hot centre, sized by depth
        for lo, hi, size in ((0.45, 0.62, 2.2), (0.62, 0.8, 3.0), (0.8, 2.0, 3.8)):
            sel = [i for i in fr if lo <= nd[i] < hi]
            if not sel:
                continue
            p.setPen(QPen(lum(QColor(255, 172, 62), dim), size * unit, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawPoints([QPointF(nx[i], ny[i]) for i in sel])
            p.setPen(QPen(lum(QColor(255, 238, 196), dim), size * 0.5 * unit, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawPoints([QPointF(nx[i], ny[i]) for i in sel])
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if self.hover is not None and self.hover < n and self.items:
            self._tooltip(p, self.hover, nx[self.hover], ny[self.hover], r)
        p.restore()

    _CATS = {"lessons": "LIÇÃO", "habits": "HÁBITO", "identity": "IDENTIDADE", "home": "CASA",
             "preferences": "PREFERÊNCIA", "preference": "PREFERÊNCIA", "relationships": "PESSOAS",
             "projects": "PROJETO", "wishes": "DESEJO", "notes": "NOTA", "personal": "PESSOAL",
             "monitors": "MONITORADO"}
    _SOURCES = {"correction": "sua correção", "assistant": "ele guardou", "user": "você",
                "consolidation": "arrumação diária", "default": "padrão", "import": "importado",
                "app": "app", "revert": "desfeito"}

    def _tooltip(self, p, i, x, y, r) -> None:
        it = self.item_for(i)
        p.setPen(QPen(QColor(255, 248, 230), 1.6)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(x, y), 7, 7)
        ts = str(it.get("ts", ""))
        head = (f"{self._CATS.get(it.get('cat', ''), str(it.get('cat', '')).upper())} · "
                f"{ts[8:10]}/{ts[5:7]} {ts[11:16]} · {self._SOURCES.get(it.get('source', ''), it.get('source', ''))}")
        key = str(it.get("key", "")).replace("_", " ")
        val = ("esquecido: " if it.get("action") == "delete" else "") + str(it.get("value") or "")
        f = QFont(); f.setPointSizeF(max(7.5, r / 24)); p.setFont(f)
        fm = p.fontMetrics()
        width = int(min(360, max(200, r * 1.5)))
        lines, cur = [], ""
        for w in val.split():
            if fm.horizontalAdvance(cur + " " + w) > width - 16 and cur:
                lines.append(cur); cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            lines.append(cur)
        lines = lines[:4]
        lh = fm.height()
        h = 10 + lh * (2 + len(lines)) + 6
        cx = self._proj[4]
        bx = x + 14 if x + 14 + width < cx + r * 1.6 else x - 14 - width
        by = y - h / 2
        p.setPen(QPen(QColor(255, 168, 52), 1)); p.setBrush(QColor(6, 14, 30, 235))
        p.drawRoundedRect(QRectF(bx, by, width, h), 4, 4)
        p.setPen(QColor(255, 205, 120)); p.drawText(QPointF(bx + 8, by + 6 + lh * 0.8), head)
        p.setPen(QColor(255, 248, 230))
        p.drawText(QPointF(bx + 8, by + 6 + lh * 1.8), fm.elidedText(key, Qt.TextElideMode.ElideRight, width - 16))
        p.setPen(QColor(200, 215, 235))
        for k, line in enumerate(lines):
            p.drawText(QPointF(bx + 8, by + 6 + lh * (2.8 + k)), line)
        p.setBrush(Qt.BrushStyle.NoBrush)
