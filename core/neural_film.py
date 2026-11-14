"""
core/neural_film.py — the NODES screen's second centrepiece: J.A.R.V.I.S.'s
consciousness as the film shows it, built on the ULTRON neural sphere's ideas.

WHAT IT IS
    Not dots joined by straight lines (that is core/neural_plexus.py, which
    stays): a VOLUME of thousands of short luminous fragments on concentric
    shells, brightest at the rim, open in places and standing proud in others;
    stepped circuit traces with pads; glass panels; a few big sweeping arcs; a
    small core that is a tunnel of rings. (No bokeh: blurred dots floating
    outside the globe read as stray objects, so they were taken out.)

    And what it has learned: ONE POINT PER THING KNOWN on the shell, each linked
    to the core by a ray — "each synapse links all the learning". The label
    under it ("N SINAPSES") is that count, the same number on every screen.
    Recent ones (48 h) glow brighter, a few strong ones read as the film's long
    rays, and while it talks or thinks impulses run out along them. Hovering a
    point shows what it stands for; clicking opens the learning page (ui.py).

HOW IT STAYS FAST
    The fragment shell is thousands of antialiased strokes — about 60–120 ms to
    draw in Python + QPainter, far too slow for every frame. It turns slowly
    (a full turn in about two minutes), so a background thread redraws it, with
    its bloom, into an image ~12 times a second; each frame only lays that
    image down and draws the light, moving parts on top (rays, impulses,
    points, sweeping arcs, core). QPainter on a QImage is allowed off
    the GUI thread, and PyQt lets go of the GIL while Qt rasterises.

Interface (the same as PlexusSphere, so the HUD treats them alike):
    set_knowledge, set_items, item_for, over, node_at, hover, alive, step, paint.
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF, QRadialGradient

from core.neural_plexus import PlexusSphere as _Plexus

AMBER = (255, 132, 18)
HOT = (255, 196, 80)
WHITE = (255, 236, 190)
GREY = (150, 160, 172)          # muted
MAX_ITEMS = 600                 # points drawn for what it knows (the label keeps the true count)
BG_FPS = 12.0
PITCH = 0.22


def _q(rgb, a: float) -> QColor:
    c = QColor(*rgb)
    c.setAlphaF(max(0.0, min(1.0, float(a))))
    return c


def _sph(rad, lat, lon):
    c = np.cos(lat)
    return np.stack([rad * c * np.cos(lon), rad * np.sin(lat), rad * c * np.sin(lon)], -1)


def _project(P, cx, cy, r, yaw):
    cy_, sy_ = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(PITCH), math.sin(PITCH)
    x, y, z = P[..., 0], P[..., 1], P[..., 2]
    x1 = x * cy_ + z * sy_
    z1 = -x * sy_ + z * cy_
    y1 = y * cp - z1 * sp
    z2 = y * sp + z1 * cp
    k = 3.4 / (3.4 + z2)
    return cx + x1 * r * k, cy - y1 * r * k, np.clip((1 - z2) / 2, 0, 1)


def _draw_binned(p: QPainter, x0, y0, x1, y1, alpha, rgb, width: float, bins: int = 10) -> None:
    """Many segments with their own alpha, drawn as a few batches."""
    if len(alpha) == 0:
        return
    b = np.clip((alpha * bins).astype(int), 0, bins)
    for k in np.unique(b):
        sel = np.where(b == k)[0]
        a = (k + 0.5) / bins
        if a < 0.02:
            continue
        p.setPen(QPen(_q(rgb, a), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLines([QLineF(x0[i], y0[i], x1[i], y1[i]) for i in sel])


class FilmSphere:
    def __init__(self, seed: int = 7):
        self._rng = np.random.default_rng(seed)
        rng = self._rng
        self.t = 0.0
        self.yaw = 0.5
        self.clock = 0.0
        self.act = 0.12
        self.amp = 0.0
        self.pulse = 1.0
        self._pt, self._pw = 1.0, 0.0
        self.muted = False
        self.state = "IDLE"
        self.direct_glow = False
        self.hover: int | None = None
        self.items: list[dict] = []
        self._known: int | None = None
        self._proj = None

        # 1. the fragment shell, fixed in the sphere's own frame: each stroke a
        #    3-point curve (start, middle, end) — clusters and voids, a ragged rim
        N = 9000
        u = rng.random(N)
        rad = np.where(u < 0.72, rng.uniform(0.90, 1.0, N),
                       np.where(u < 0.88, rng.uniform(0.70, 0.90, N), rng.uniform(0.35, 0.70, N)))
        lat = np.arcsin(rng.uniform(-1, 1, N))
        lon = rng.uniform(0, 2 * np.pi, N)
        along = rng.random(N) < 0.72
        length = rng.uniform(0.03, 0.16, N) * (1 + 2.0 * (rng.random(N) < 0.10))
        bright = rng.random(N) ** 1.8

        def field(la, lo, s):
            g = np.random.default_rng(s)
            v = np.zeros_like(la)
            for _ in range(7):
                k1, k2, ph = g.uniform(1, 4), g.integers(1, 5), g.uniform(0, 6.3)
                v += np.sin(k1 * la * 2 + ph) * np.cos(k2 * lo + ph * 0.7)
            return v / 7
        dens = field(lat, lon, 11)
        keep = dens > np.quantile(dens, 0.42)
        rad = np.where(rad > 0.9, rad + np.clip(field(lat, lon, 23), 0, None) * 0.22, rad)
        rad, lat, lon, along, length, bright = (a[keep] for a in (rad, lat, lon, along, length, bright))
        pts = []
        for f in (0.0, 0.5, 1.0):
            pts.append(_sph(rad, lat + np.where(along, 0, length * f), lon + np.where(along, length * f, 0)))
        self._frag = np.stack(pts, 1)                    # (n, 3, 3)
        self._frag_rad = rad
        self._frag_b = bright
        self._frag_hot = bright > 0.86

        # 2. circuit traces (stepped) with a pad at the end
        segs, pads, tb = [], [], []
        for _ in range(300):
            r0 = float(rng.choice([0.9, 0.95, 1.0, 0.7, 0.8]))
            la, lo = math.asin(rng.uniform(-0.95, 0.95)), rng.uniform(0, 2 * math.pi)
            b = rng.random() ** 1.6
            prev = (la, lo)
            for k in range(int(rng.integers(2, 6))):
                if k % 2 == 0:
                    lo += rng.uniform(0.03, 0.14) * rng.choice([-1, 1])
                else:
                    la += rng.uniform(0.02, 0.09) * rng.choice([-1, 1])
                segs.append((prev, (la, lo), r0)); tb.append(b)
                prev = (la, lo)
            pads.append((la, lo, r0, b))
        a = np.array([[s[0][0], s[0][1], s[1][0], s[1][1], s[2]] for s in segs])
        self._tr0 = _sph(a[:, 4], a[:, 0], a[:, 1])
        self._tr1 = _sph(a[:, 4], a[:, 2], a[:, 3])
        self._tr_b = np.array(tb)
        pa = np.array(pads)
        self._pads = _sph(pa[:, 2], pa[:, 0], pa[:, 1])
        self._pads_b = pa[:, 3]

        # 3. glass panels: four corners each
        self._panels = []
        for _ in range(9):
            c = rng.normal(size=3); c /= np.linalg.norm(c)
            rr, w, h, rot = rng.uniform(0.72, 0.95), rng.uniform(0.12, 0.26), rng.uniform(0.08, 0.18), rng.uniform(0, math.pi)
            e1 = np.cross(c, [0, 1.0, 0]); e1 /= (np.linalg.norm(e1) or 1); e2 = np.cross(c, e1)
            e1, e2 = e1 * math.cos(rot) + e2 * math.sin(rot), -e1 * math.sin(rot) + e2 * math.cos(rot)
            self._panels.append(np.array([c * rr + e1 * sx * w + e2 * sy * h
                                          for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]))

        # 4. spikes on the rim
        sd = rng.normal(size=(160, 3)); sd /= np.linalg.norm(sd, axis=1, keepdims=True)
        self._sp0 = sd * rng.uniform(0.96, 1.0, (160, 1))
        self._sp1 = sd * rng.uniform(1.02, 1.09, (160, 1))
        self._sp_b = rng.random(160) ** 2

        # 5. the sweeping arcs (drawn every frame: they move faster than the shell)
        self._sweeps = []
        for nrm, rr, st, spn, spd, w in (((0.05, 0.25, 1.0), 0.42, 0.8, 4.2, 0.6, 1.6),
                                         ((0.15, 1, 0.35), 0.62, 0.3, 3.4, 0.5, 1.0),
                                         ((0.9, 0.25, 0.3), 0.93, 2.0, 2.6, -0.35, 0.85),
                                         ((-0.4, 0.8, 0.45), 1.0, 4.1, 2.0, 0.28, 0.7),
                                         ((0.3, -0.2, 0.93), 0.78, 1.0, 2.2, -0.45, 0.6)):
            n = np.array(nrm, float); n /= np.linalg.norm(n)
            a1 = np.cross(n, [0, 0, 1.0] if abs(n[2]) < 0.9 else [1.0, 0, 0]); a1 /= np.linalg.norm(a1)
            self._sweeps.append((a1, np.cross(n, a1), rr, st, spn, spd, w))

        # what it knows: points, rays
        self._dirs = rng.normal(size=(MAX_ITEMS, 3))
        self._dirs /= np.linalg.norm(self._dirs, axis=1, keepdims=True)
        self._dirs *= rng.uniform(0.84, 0.99, (MAX_ITEMS, 1))
        self._major = rng.random(MAX_ITEMS) < 0.06
        self._ph = rng.random(MAX_ITEMS)
        self.recent = np.zeros(MAX_ITEMS, bool)
        self.n = 0
        self.glow = np.zeros(MAX_ITEMS)                 # a point just learned flares

        # the background shell image
        self._bg = None                                  # (QImage, yaw, r, size, muted)
        self._want = None                                # (yaw, r, muted) the worker should draw next
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self._sprites: dict = {}
        self._stop = False
        self._last_paint = 0.0

    # ── what it knows ───────────────────────────────────────────────────────

    @property
    def alive(self) -> int:
        """The count under the globe: everything it knows (not capped)."""
        return int(self._known or 0)

    def set_knowledge(self, known) -> None:
        if known is None:
            return
        known = max(0, int(known))
        if self._known is not None and known > self._known:
            for i in range(min(self._known, MAX_ITEMS), min(known, MAX_ITEMS)):
                self.glow[i] = 1.0
        self._known = known
        self.n = min(known, MAX_ITEMS)

    def set_items(self, items) -> None:
        if not items or items is self.items:
            return
        self.items = list(items)
        cut = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 48 * 3600))
        self.recent[:] = False
        for i in range(self.n):
            if str(self.item_for(i).get("ts", "")) >= cut:
                self.recent[i] = True

    def item_for(self, i) -> dict:
        if not self.items or i is None:
            return {}
        n = max(1, self.n)
        return self.items[min(len(self.items) - 1, int(i * len(self.items) / n))]

    def over(self, x: float, y: float) -> bool:
        if not self._proj:
            return False
        cx, cy, r = self._proj[4:7]
        return (x - cx) ** 2 + (y - cy) ** 2 <= (r * 1.05) ** 2

    def node_at(self, x: float, y: float, reach: float = 10.0):
        if not self._proj or not self.items or not self.n:
            return None
        nx, ny, nd = self._proj[:3]
        d2 = np.where(nd > 0.45, (nx - x) ** 2 + (ny - y) ** 2, np.inf)
        i = int(np.argmin(d2))
        return i if d2[i] <= reach * reach else None

    # ── motion ──────────────────────────────────────────────────────────────

    def step(self, dt: float, amp: float, speaking: bool = False, muted: bool = False,
             state: str = "IDLE") -> None:
        dt = max(0.0, min(dt, 0.1))
        self.muted, self.state = muted, state
        self.amp += (max(0.0, min(1.0, float(amp))) - self.amp) * min(1.0, dt * 9)
        target = (0.0 if muted else 0.9 if state in ("THINKING", "PROCESSING")
                  else (0.35 + 0.65 * self.amp) if speaking else 0.12)
        self.act += (target - self.act) * (1 - math.exp(-dt * 5))
        self.t += dt
        self.clock += dt * (0.7 + 1.1 * self.act)
        self.yaw += dt * (0.02 if muted else 0.045 + 0.05 * self.act)
        # uneven breathing, never a mechanical heartbeat (the ULTRON sphere's)
        self._pw -= dt
        if self._pw <= 0:
            self._pw = float(self._rng.uniform(0.38, 1.45))
            self._pt = 1 + float(self._rng.uniform(-1, 0.85)) * (0.010 + 0.04 * self.act)
        wave = math.sin(self.clock * 2.3) * 0.006 + math.sin(self.clock * 3.71 + 0.8) * 0.004
        tgt = 1.0 if muted else self._pt + wave + self.amp * 0.014
        self.pulse += (tgt - self.pulse) * (1 - math.exp(-dt * 4.8))
        self.pulse = max(0.94, min(1.05, self.pulse))
        self.glow *= math.exp(-dt * 0.8)

    # ── the background shell, drawn off the GUI thread ──────────────────────

    def _ensure_worker(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, daemon=True, name="film-sphere")
            self._thread.start()

    def _worker(self) -> None:
        last = 0.0
        while not self._stop:
            self._wake.wait(1.0)
            self._wake.clear()
            with self._lock:
                want = self._want
            if want is None or time.monotonic() - self._last_paint > 0.6:
                continue                       # not on screen: nothing to draw
            wait = 1.0 / BG_FPS - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
                with self._lock:
                    want = self._want
            last = time.monotonic()
            try:
                img = self._render_shell(*want)
            except Exception as e:
                print(f"[FilmSphere] {e}")
                time.sleep(0.5)
                continue
            with self._lock:
                self._bg = (img,) + tuple(want)

    def _render_shell(self, yaw: float, r: float, muted: bool) -> QImage:
        size = max(8, int(r * 2.7))
        c = size / 2
        layer = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(Qt.GlobalColor.transparent)
        q = QPainter(layer)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        q.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        col, hot = (GREY, GREY) if muted else (AMBER, HOT)
        k = 0.45 if muted else 1.0
        unit = max(0.7, r / 230)

        # fragments: two segments per stroke, alpha from the rim and the depth
        x, y, d = _project(self._frag, c, c, r, yaw)
        ex, ey = (x[:, 0] - c) / r, (y[:, 0] - c) / r
        limb = np.clip(np.sqrt(ex * ex + ey * ey) / np.maximum(self._frag_rad, 1e-3), 0, 1) ** 3
        dm = d.mean(axis=1)
        a = k * 0.72 * (0.08 + 0.75 * self._frag_b) * (0.08 + 1.0 * limb) * (0.55 + 0.45 * dm) \
            * np.minimum(1.0, self._frag_rad) ** 0.6
        for hot_sel, rgb, w in ((~self._frag_hot, col, 1.5), (self._frag_hot, hot, 1.8)):
            for s in (0, 1):
                _draw_binned(q, x[hot_sel, s], y[hot_sel, s], x[hot_sel, s + 1], y[hot_sel, s + 1],
                             a[hot_sel], rgb, w * unit)

        # traces and pads
        x0, y0, d0 = _project(self._tr0, c, c, r, yaw)
        x1, y1, _ = _project(self._tr1, c, c, r, yaw)
        _draw_binned(q, x0, y0, x1, y1, k * (0.12 + 0.5 * self._tr_b) * (0.2 + 0.8 * d0), col, 1.0 * unit)
        px, py, pd = _project(self._pads, c, c, r, yaw)
        q.setPen(Qt.PenStyle.NoPen)
        pa = k * (0.2 + 0.6 * self._pads_b) * (0.2 + 0.8 * pd)
        for i in range(len(px)):
            q.setBrush(_q(hot, pa[i]))
            q.drawEllipse(QPointF(px[i], py[i]), 1.4 * unit, 1.4 * unit)

        # glass panels
        for P in self._panels:
            x, y, d = _project(P, c, c, r, yaw)
            dmn = float(d.mean())
            q.setPen(QPen(_q(hot, k * (0.10 + 0.30 * dmn)), 0.9 * unit))
            q.setBrush(_q(col, k * (0.025 + 0.05 * dmn)))
            q.drawPolygon(QPolygonF([QPointF(x[i], y[i]) for i in range(4)]))
            q.setPen(QPen(_q(col, k * (0.06 + 0.16 * dmn)), 0.7 * unit))
            for j in range(1, 5):
                f = j / 5
                q.drawLine(QPointF(x[0] + (x[3] - x[0]) * f, y[0] + (y[3] - y[0]) * f),
                           QPointF(x[1] + (x[2] - x[1]) * f, y[1] + (y[2] - y[1]) * f))

        # ragged rim
        x0, y0, d0 = _project(self._sp0, c, c, r, yaw)
        x1, y1, _ = _project(self._sp1, c, c, r, yaw)
        _draw_binned(q, x0, y0, x1, y1, k * (0.15 + 0.55 * self._sp_b) * (0.25 + 0.75 * d0), col, 1.1 * unit)
        q.end()

        # bloom: a blurred copy under the sharp light
        small = layer.scaled(max(1, size // 6), max(1, size // 6), Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        blur = small.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        out = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(Qt.GlobalColor.transparent)
        o = QPainter(out)
        o.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        o.setOpacity(0.85)
        o.drawImage(0, 0, blur)
        o.setOpacity(1.0)
        o.drawImage(0, 0, layer)
        o.end()
        return out

    # ── sprites for the GUI thread ──────────────────────────────────────────

    def _sprite(self, key, make):
        s = self._sprites.get(key)
        if s is None:
            if len(self._sprites) > 64:
                self._sprites.clear()
            s = self._sprites[key] = make()
        return s

    def _dot(self, rad: int, rgb_in, rgb_out) -> QImage:
        def make():
            pm = QImage(2 * rad, 2 * rad, QImage.Format.Format_ARGB32_Premultiplied)
            pm.fill(Qt.GlobalColor.transparent)
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.RenderHint.Antialiasing)
            g = QRadialGradient(QPointF(rad, rad), rad)
            g.setColorAt(0, _q(rgb_in, 1.0)); g.setColorAt(0.35, _q(rgb_out, 0.45)); g.setColorAt(1, _q(rgb_out, 0))
            pp.setPen(Qt.PenStyle.NoPen); pp.setBrush(g); pp.drawEllipse(QPointF(rad, rad), rad, rad)
            pp.end()
            return pm
        return self._sprite(("dot", rad, rgb_in, rgb_out), make)

    # ── drawing, every frame ────────────────────────────────────────────────

    def paint(self, p: QPainter, cx: float, cy: float, r: float, main=None, acc=None, bg=None) -> None:
        r = r * self.pulse
        muted = bool(self.muted)
        self._last_paint = time.monotonic()
        self._ensure_worker()
        with self._lock:
            self._want = (self.yaw, round(r / 4) * 4.0, muted)       # r in steps: no redraw per breath
            have = self._bg
        self._wake.set()

        dest, dcx, dcy = p, cx, cy
        fs = int(r * 2.8)
        frame = self._frame if getattr(self, "_frame", None) is not None and self._frame.width() == fs else None
        if frame is None:
            frame = self._frame = QImage(fs, fs, QImage.Format.Format_ARGB32_Premultiplied)
        frame.fill(Qt.GlobalColor.transparent)
        p = QPainter(frame)
        cx = cy = fs / 2
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        yaw = self.yaw
        if have is not None:
            img, yaw, br, _bm = have[0], have[1], have[2], have[3]
            size = img.width() * (r / br)
            p.drawImage(QRectF(cx - size / 2, cy - size / 2, size, size), img)
        col, hot, white = (GREY, GREY, GREY) if muted else (AMBER, HOT, WHITE)
        act = self.act

        # sweeping arcs, glowing, tapered
        for a1, b1, rr, st, spn, spd, w in self._sweeps:
            th = st + self.t * spd + np.linspace(0, spn, 61)
            P = rr * (np.outer(np.cos(th), a1) + np.outer(np.sin(th), b1))
            x, y, d = _project(P, cx, cy, r, yaw)
            fade = np.sin(np.linspace(0, np.pi, 61)) ** 0.7
            for width, al, rgb in ((9 * w, 0.05, col), (4 * w, 0.14, col), (1.6 * w, 0.55, hot)):
                _draw_binned(p, x[:-1], y[:-1], x[1:], y[1:], al * fade[:-1] * (0.3 + 0.7 * d[:-1]),
                             rgb, width * max(0.7, r / 230), bins=6)

        # what it knows: a ray from the core to every point, the points themselves
        n = self.n
        nx = ny = nd = np.zeros(0)
        if n:
            D = self._dirs[:n]
            nx, ny, nd = _project(D, cx, cy, r, yaw)
            far = D * np.where(self._major[:n], 1.18, 1.0)[:, None]
            fx, fy, _ = _project(far, cx, cy, r, yaw)
            rec = self.recent[:n]
            a = (0.018 + 0.03 * nd) * np.where(self._major[:n], 7.0, 1.0) * np.where(rec, 2.2, 1.0)
            a = a + self.glow[:n] * 0.5
            ccx = np.full(n, cx); ccy = np.full(n, cy)
            _draw_binned(p, ccx[~rec], ccy[~rec], fx[~rec], fy[~rec], a[~rec], col, 0.8, bins=8)
            _draw_binned(p, ccx[rec], ccy[rec], fx[rec], fy[rec], a[rec], hot, 0.9, bins=8)
            # impulses out along the rays while it talks or thinks
            if act > 0.2:
                sel = np.where(self._ph[:n] < act * 0.7)[0]
                if len(sel):
                    kk = (self.clock * (0.5 + self._ph[sel]) + self._ph[sel] * 3) % 1
                    ix, iy, idp = _project(D[sel] * kk[:, None], cx, cy, r, yaw)
                    spr = self._dot(4, white, hot)
                    for i in range(len(sel)):
                        p.setOpacity(min(1.0, act * (0.4 + 0.6 * idp[i])))
                        p.drawImage(QPointF(ix[i] - 4, iy[i] - 4), spr)
                    p.setOpacity(1.0)
            # the points: recent ones larger and whiter, the front ones brighter
            small, big = self._dot(4, hot, col), self._dot(7, white, hot)
            for i in np.argsort(nd):
                p.setOpacity(min(1.0, 0.25 + 0.75 * nd[i] + self.glow[i]))
                if rec[i] or self.glow[i] > 0.3:
                    p.drawImage(QPointF(nx[i] - 7, ny[i] - 7), big)
                else:
                    p.drawImage(QPointF(nx[i] - 4, ny[i] - 4), small)
            p.setOpacity(1.0)

        # the core: a tunnel of rings, white-hot inside
        rc = r * 0.12
        p.setBrush(Qt.BrushStyle.NoBrush)
        for k in range(11):
            f = k / 10
            rr = rc * (1 - 0.72 * f)
            ox = cx + rc * 0.22 * f * math.cos(self.clock * 0.6)
            oy = cy - rc * 0.14 * f
            p.setPen(QPen(_q(hot if k > 4 else col, 0.35 + 0.55 * f), (1.4 + 0.8 * f) * max(0.7, r / 230)))
            start = int((self.clock * 30 * (1 if k % 2 else -1) + k * 33) * 16)
            p.drawArc(QRectF(ox - rr, oy - rr * 0.92, 2 * rr, 2 * rr * 0.92), start, int((300 - k * 9) * 16))
        g = QRadialGradient(QPointF(cx, cy), rc * 0.9)
        g.setColorAt(0, _q(white, 0.35 + 0.25 * act)); g.setColorAt(0.15, _q(hot, 0.22))
        g.setColorAt(0.5, _q(col, 0.06)); g.setColorAt(1, _q(col, 0))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(g); p.drawEllipse(QPointF(cx, cy), rc * 0.9, rc * 0.9)

        p.end()

        # onto the HUD, once; positions back in the HUD's coordinates
        ox, oy = dcx - fs / 2, dcy - fs / 2
        dest.save()
        dest.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        dest.drawImage(QPointF(ox, oy), frame)
        nx, ny = nx + ox, ny + oy
        self._proj = (nx, ny, nd, n, dcx, dcy, r)
        if self.hover is not None and self.hover < n and self.items:
            dest.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._tooltip(dest, self.hover, nx[self.hover], ny[self.hover], r)
        dest.restore()

    # the tooltip is the constellation's: the same card for the same items
    _CATS = _Plexus._CATS
    _SOURCES = _Plexus._SOURCES
    _tooltip = _Plexus._tooltip
