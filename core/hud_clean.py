"""
core/hud_clean.py — the clean HUD: the synapse core in a thin scaled ring, the
machine's performance on one glass sub-screen at the left, today's agenda
(personal and shared together) and the network at the right, the state and the
last sentence heard under the core, and three controls at the bottom (mic,
interrupt, modules — the modules open in their own sub-screen, the hub).

Painted by HudCanvas when the HUD style is "clean". Cost is kept where it was:
  * the side panels change once a second (that is how often the numbers do),
    so they are rendered into a pixmap once a second and blitted every frame;
  * the ring's scale is cached per size and colour;
  * per frame only the core, two arcs, the waveform and a few labels are drawn.

When the assistant puts cards on the HUD the side panels step aside and the
cards take the columns, exactly as before — nothing about cards, holograms,
links or the whole-screen overlay changes.
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap, QRadialGradient)

MONO = "Consolas"


_WEEK = ("Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom")
_MON = ("Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez")


def _rgba(c: QColor, a: float) -> QColor:
    q = QColor(c)
    q.setAlphaF(max(0.0, min(1.0, a)))
    return q


def _font(family: str, px: float, bold: bool = False, spacing: float = 0.0) -> QFont:
    f = QFont(family)
    f.setPixelSize(max(6, int(round(px))))
    if bold:
        f.setWeight(QFont.Weight.DemiBold)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


class CleanHUD:
    def __init__(self, ui_font: str):
        self.F = ui_font
        self._panels: QPixmap | None = None
        self._panels_key = None
        self._ring: QPixmap | None = None
        self._ring_key = None
        self.hits: dict[str, QRectF] = {}     # control name → clickable rect
        self.core = (0.0, 0.0, 0.0)           # cx, cy, ring radius (for the cards' layout)

    # ── geometry ────────────────────────────────────────────────────────────

    def _geo(self, W: float, H: float, panels: bool) -> dict:
        s = max(0.62, min(W / 1440.0, H / 900.0))
        M = 40 * s
        top = 92 * s
        pw = min(352 * s, W * 0.26)
        show = panels and W >= 980
        ctrl_h = 36 * s
        ctrl_y = H - 20 * s - ctrl_h
        below = 72 * s                          # state + waveform (or the muted hint) under the ring
        avail_top, avail_bot = top, ctrl_y - below
        cx = W / 2.0
        side = (M + pw + 36 * s) if show else M
        r = max(60.0, min((avail_bot - avail_top) / 2.0 - 8 * s, (W - 2 * side) / 2.0))
        cy = avail_top + (avail_bot - avail_top) / 2.0
        return {"s": s, "M": M, "top": top, "pw": pw, "show": show, "cx": cx, "cy": cy, "r": r,
                "ctrl_y": ctrl_y, "ctrl_h": ctrl_h, "W": W, "H": H}

    # ── public ──────────────────────────────────────────────────────────────

    def paint(self, p: QPainter, hud, W: float, H: float, colors: dict, data: dict,
              cards_active: bool, k: float = 1.0) -> None:
        g = self._geo(W, H, panels=not cards_active)
        self.core_base_r = g["r"]
        g["r"] *= max(0.4, min(1.0, k))       # a hologram on the centre stage: the core makes room
        s, cx, cy, r = g["s"], g["cx"], g["cy"], g["r"]
        self.core = (cx, cy, r)
        side = (g["M"] + g["pw"] + 24 * s) if g["show"] else g["M"]
        self.centre_band = (side, W - side, g["top"] + 12 * s, g["ctrl_y"] - 70 * s)
        C = colors
        muted = bool(getattr(hud, "muted", False))
        state = str(getattr(hud, "state", "") or "")
        speaking = bool(getattr(hud, "speaking", False))
        if muted:
            st_col, st_txt = C["red"], "MICROFONE DESLIGADO"
        elif speaking:
            st_col, st_txt = C["orange"], "FALANDO"
        elif state in ("THINKING", "PROCESSING"):
            st_col, st_txt = C["yellow"], "PENSANDO"
        elif state == "SLEEPING":
            st_col, st_txt = C["dim"], "DORMINDO"
        elif state in ("CONNECTING", "OFFLINE"):
            st_col, st_txt = C["dim"], "CONECTANDO"
        else:
            st_col, st_txt = C["green"], "OUVINDO"

        self._brackets(p, g, C)
        self._topbar(p, g, C, data)

        # side panels: re-rendered when their numbers change (once a second)
        if g["show"]:
            key = (int(data.get("t", 0)), int(W), int(H), C["main"].rgb(), muted,
                   len(data.get("agenda") or []), self._ag_scroll_state())
            if self._panels_key != key or self._panels is None:
                pm = QPixmap(int(W), int(H))
                pm.fill(Qt.GlobalColor.transparent)
                pp = QPainter(pm)
                pp.setRenderHint(QPainter.RenderHint.Antialiasing)
                self._perf_panel(pp, g, C, data)
                self._right_panels(pp, g, C, data)
                pp.end()
                self._panels, self._panels_key = pm, key
            p.drawPixmap(0, 0, self._panels)

        self._ring_scale(p, g, C)
        amp = float(getattr(hud, "_amp_disp", 0.0) or 0.0)
        t = time.monotonic()
        # two live arcs on the outer ring: the CPU load, and the state (it breathes with the voice)
        cpu = float((data.get("cpu") or {}).get("load") or 0.0)
        rr = r + 10 * s
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_rgba(C["main"], 0.9), 3 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        span = 18 + 120 * min(1.0, cpu / 100.0)
        p.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), int((128 - span) * 16), int(span * 16))
        p.setPen(QPen(st_col, 3 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        span2 = 26 + 70 * min(1.0, amp * 1.4)
        start2 = -40 - (t * 9) % 360
        p.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), int(start2 * 16), int(-span2 * 16))

        # the centre: the synapses, or the golden data sphere when chosen
        neural = getattr(hud, "_neural", None)
        if getattr(hud, "hud_core", "") in ("synapse", "sphere") and getattr(hud, "_synapse", None) is not None:
            neural = hud._synapse
            neural.set_knowledge(data.get("knowledge"))      # it grows with what it learns
            neural.set_items(data.get("knowledge_items"))    # …and each node is one of those things
        elif getattr(hud, "hud_core", "") == "sphere" and getattr(hud, "_sphere", None) is not None:
            neural = hud._sphere
            neural.set_knowledge(data.get("knowledge"))      # it grows with what it learns
            neural.set_items(data.get("knowledge_items"))    # …and each node is one of those things
        if neural is not None:
            acc = C["red"] if muted else st_col
            neural.paint(p, cx, cy, r * 0.70, C["main"] if not muted else C["red"], acc, C["bg"])

        self._state_block(p, g, C, st_col, st_txt, amp, data, muted)
        self._file_label = getattr(hud, "file_label", "")
        self._log_open = bool(getattr(hud, "log_open", False))
        self._open_drawers = tuple(getattr(hud, "open_drawers", ()) or ())
        self._controls(p, g, C, muted, speaking)

    # ── pieces ──────────────────────────────────────────────────────────────

    def _brackets(self, p, g, C):
        s, W, H = g["s"], g["W"], g["H"]
        L, m = 42 * s, 24 * s
        p.setPen(QPen(_rgba(C["border_b"], 0.95), 1.5))
        for x, y, dx, dy in ((m, m, 1, 1), (W - m, m, -1, 1), (m, H - m, 1, -1), (W - m, H - m, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * L, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * L))

    def _topbar(self, p, g, C, data):
        s, M, W = g["s"], g["M"], g["W"]
        y = 30 * s
        name = str(data.get("name") or "JARVIS").upper()
        p.setPen(C["white"])
        p.setFont(_font(self.F, 22 * s, True, 9 * s))
        fm = QFontMetrics(p.font())
        p.drawText(QPointF(M + 20 * s, y + 24 * s), name)
        x = M + 20 * s + fm.horizontalAdvance(name) + 18 * s
        online = bool(data.get("online", True))
        dot = C["green"] if online else C["dim"]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dot)
        p.drawEllipse(QPointF(x + 4 * s, y + 19 * s), 3.5 * s, 3.5 * s)
        p.setPen(C["text_med"])
        p.setFont(_font(MONO, 12 * s, False, 0.6 * s))
        label = "ONLINE" if online else "OFFLINE"
        p.drawText(QPointF(x + 14 * s, y + 23 * s), label)

        # what the window's top bar held: setup, everyday controls, clear projections
        bx = x + 14 * s + QFontMetrics(p.font()).horizontalAdvance(label) + 26 * s
        rad = 15 * s
        self.top_hits = {}
        for key in ("setup", "controls", "modules", "clear"):
            c = QPointF(bx + rad, y + 18 * s)
            rect = QRectF(c.x() - rad, c.y() - rad, 2 * rad, 2 * rad)
            on = key in getattr(self, "_open_drawers", ())
            p.setBrush(_rgba(C["main"], 0.16 if on else 0.06))
            p.setPen(QPen(_rgba(C["main"] if on else C["glass_edge"], 0.55 if on else 0.30), 1))
            p.drawEllipse(rect)
            self._top_icon(p, key, c.x(), c.y(), s, C["white"] if on else C["text"])
            self.top_hits[key] = rect
            bx += 2 * rad + 10 * s

        # the clock (blue, with seconds) and the date under it
        now = time.localtime()
        clock = time.strftime("%H:%M:%S", now)
        p.setFont(_font(self.F, 24 * s, True))
        cw = QFontMetrics(p.font()).horizontalAdvance(clock)
        p.setPen(C["main"])
        p.drawText(QPointF(W - M - 20 * s - cw, y + 22 * s), clock)
        dtxt = f"{_WEEK[now.tm_wday]} {now.tm_mday:02d} {_MON[now.tm_mon - 1]} {now.tm_year}"
        p.setFont(_font(MONO, 11 * s, False, 0.8 * s))
        dw = QFontMetrics(p.font()).horizontalAdvance(dtxt)
        p.setPen(C["text_med"])
        p.drawText(QPointF(W - M - 20 * s - dw, y + 40 * s), dtxt)

    def _top_icon(self, p, key, x, y, s, col):
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(col, 1.4 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if key == "setup":                      # gear
            p.drawEllipse(QPointF(x, y), 3 * s, 3 * s)
            for i in range(8):
                a = math.radians(i * 45)
                p.drawLine(QPointF(x + math.cos(a) * 5 * s, y + math.sin(a) * 5 * s),
                           QPointF(x + math.cos(a) * 7.5 * s, y + math.sin(a) * 7.5 * s))
        elif key == "controls":                 # three sliders
            for i, kx in enumerate((-2.5, 3.0, -0.5)):
                yy = y + (i - 1) * 4.5 * s
                p.drawLine(QPointF(x - 6.5 * s, yy), QPointF(x + 6.5 * s, yy))
                p.setBrush(col)
                p.drawEllipse(QPointF(x + kx * s, yy), 1.8 * s, 1.8 * s)
                p.setBrush(Qt.BrushStyle.NoBrush)
        elif key == "modules":                  # ◈
            path = QPainterPath()
            path.moveTo(x, y - 7 * s)
            path.lineTo(x + 7 * s, y)
            path.lineTo(x, y + 7 * s)
            path.lineTo(x - 7 * s, y)
            path.closeSubpath()
            p.drawPath(path)
            p.setBrush(col)
            p.drawEllipse(QPointF(x, y), 2.2 * s, 2.2 * s)
            p.setBrush(Qt.BrushStyle.NoBrush)
        else:                                   # clear: a circle struck through
            p.drawEllipse(QPointF(x, y), 6.5 * s, 6.5 * s)
            p.drawLine(QPointF(x - 4.6 * s, y + 4.6 * s), QPointF(x + 4.6 * s, y - 4.6 * s))

    def _glass(self, p, rect: QRectF, C, s, strong: float = 1.0):
        g = QLinearGradient(rect.topLeft(), rect.bottomRight())
        g.setColorAt(0.0, _rgba(C["main"], 0.10 * strong))
        g.setColorAt(1.0, _rgba(C["main"], 0.025 * strong))
        p.setPen(QPen(_rgba(C["glass_edge"], 0.20 * strong), 1))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(rect, 16 * s, 16 * s)
        p.setPen(QPen(_rgba(C["white"], 0.16 * strong), 1))
        p.drawLine(QPointF(rect.left() + 16 * s, rect.top() + 0.5), QPointF(rect.right() - 16 * s, rect.top() + 0.5))

    def _label(self, p, x, y, txt, C, s, right: str = ""):
        p.setPen(C["white"])
        p.setFont(_font(self.F, 12 * s, True, 3.6 * s))
        p.drawText(QPointF(x, y), txt)

    def _perf_panel(self, p, g, C, data):
        """Compact performance card: narrower than the side panels and only
        as tall as what it shows (measured first, then painted on glass)."""
        s, M = g["s"], g["M"]
        pw = min(292 * s, g["pw"])
        top = g["top"] + 12 * s
        x0 = M + 8 * s + 18 * s
        x1 = M + 8 * s + pw - 18 * s
        probe = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
        mp = QPainter(probe)
        end = self._perf_body(mp, s, C, data, x0, x1, top + 26 * s)
        mp.end()
        max_bottom = g["ctrl_y"] - 18 * s
        R = QRectF(M + 8 * s, top, pw, min(max_bottom, end + 4 * s) - top)
        self._glass(p, R, C, s)
        self._perf_body(p, s, C, data, x0, x1, top + 26 * s)

    def _perf_body(self, p, s, C, data, x0, x1, y) -> float:
        self._label(p, x0, y, "DESEMPENHO", C, s)
        p.setPen(C["text_med"])
        p.setFont(_font(MONO, 10.5 * s))
        src = "AO VIVO"
        p.drawText(QPointF(x1 - QFontMetrics(p.font()).horizontalAdvance(src), y), src)

        cpu, gpu = data.get("cpu") or {}, data.get("gpu") or {}
        # CPU / GPU arc gauges
        y += 12 * s
        cw = (x1 - x0) / 2.0
        for i, (nm, d, model) in enumerate((("CPU", cpu, cpu.get("name") or ""),
                                           ("GPU", gpu, (gpu.get("name") or "").replace("NVIDIA GeForce ", "")))):
            gx = x0 + cw * i + cw / 2.0
            gy = y + 46 * s
            ro, ri = 40 * s, 31 * s
            load = float(d.get("load") or 0.0)
            temp = d.get("temp")
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_rgba(C["glass_edge"], 0.16), 6 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(QRectF(gx - ro, gy - ro, 2 * ro, 2 * ro), 225 * 16, -270 * 16)
            if load > 0.5:
                p.setPen(QPen(C["main"], 6 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawArc(QRectF(gx - ro, gy - ro, 2 * ro, 2 * ro), 225 * 16, int(-270 * 16 * min(1.0, load / 100.0)))
            p.setPen(QPen(_rgba(C["glass_edge"], 0.10), 3 * s))
            p.drawArc(QRectF(gx - ri, gy - ri, 2 * ri, 2 * ri), 225 * 16, -270 * 16)
            if temp is not None:
                tc = C["red"] if temp >= 85 else C["orange"] if temp >= 75 else C["green"]
                p.setPen(QPen(tc, 3 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawArc(QRectF(gx - ri, gy - ri, 2 * ri, 2 * ri), 225 * 16, int(-270 * 16 * min(1.0, temp / 100.0)))
            p.setPen(C["white"])
            p.setFont(_font(self.F, 21 * s))
            txt = f"{temp:.0f}°" if temp is not None else "--"
            p.drawText(QRectF(gx - ro, gy - 17 * s, 2 * ro, 26 * s), Qt.AlignmentFlag.AlignCenter, txt)
            p.setPen(C["text_med"])
            p.setFont(_font(MONO, 9.5 * s))
            p.drawText(QRectF(gx - ro, gy + 7 * s, 2 * ro, 14 * s), Qt.AlignmentFlag.AlignCenter, f"{load:.0f}%")
            p.setPen(C["white"])
            p.setFont(_font(self.F, 11 * s, True, 2.4 * s))
            p.drawText(QRectF(gx - cw / 2, gy + ro + 1 * s, cw, 15 * s), Qt.AlignmentFlag.AlignCenter, nm)
            p.setPen(C["text_med"])
            p.setFont(_font(self.F, 10 * s))
            p.drawText(QRectF(gx - cw / 2, gy + ro + 15 * s, cw, 15 * s), Qt.AlignmentFlag.AlignCenter,
                       QFontMetrics(p.font()).elidedText(model, Qt.TextElideMode.ElideRight, int(cw - 8 * s)))
        y = y + 46 * s + 40 * s + 52 * s

        # per-core load
        p.setPen(C["text_med"])
        p.setFont(_font(self.F, 11 * s, False, 2.2 * s))
        p.drawText(QPointF(x0, y), "NÚCLEOS")
        info = []
        if cpu.get("clock"):
            info.append(f"{cpu['clock'] / 1000:.2f} GHz".replace(".", ","))
        if cpu.get("power"):
            info.append(f"{cpu['power']:.0f} W")
        p.setFont(_font(MONO, 11 * s))
        it = " · ".join(info)
        p.drawText(QPointF(x1 - QFontMetrics(p.font()).horizontalAdvance(it), y), it)
        cores = list(cpu.get("cores") or [])[:16]
        bar_h = 30 * s
        y += 8 * s
        if cores:
            n = len(cores)
            gap = 5 * s
            bw = ((x1 - x0) - gap * (n - 1)) / n
            for i, v in enumerate(cores):
                h = max(3 * s, bar_h * min(1.0, float(v) / 100.0))
                rect = QRectF(x0 + i * (bw + gap), y + bar_h - h, bw, h)
                gr = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                gr.setColorAt(0.0, C["main"])
                gr.setColorAt(1.0, _rgba(C["main"], 0.25))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(gr))
                p.drawRoundedRect(rect, 2.5 * s, 2.5 * s)
        y += bar_h + 22 * s

        # one minute of load
        p.setPen(C["text_med"])
        p.setFont(_font(self.F, 11 * s, False, 2.2 * s))
        p.drawText(QPointF(x0, y), "CARGA · 60 S")
        p.setFont(_font(MONO, 11 * s))
        p.setPen(C["main"])
        lg = "— CPU"
        p.drawText(QPointF(x1 - 110 * s, y), lg)
        p.setPen(C["text_med"])
        p.drawText(QPointF(x1 - 52 * s, y), "┄ GPU")
        y += 8 * s
        ch = 46 * s
        box = QRectF(x0, y, x1 - x0, ch)
        p.setPen(QPen(_rgba(C["glass_edge"], 0.08), 1))
        for k in (1, 2, 3):
            yy = box.top() + box.height() * k / 4
            p.drawLine(QPointF(box.left(), yy), QPointF(box.right(), yy))
        for series, solid in ((data.get("cpu_hist") or [], True), (data.get("gpu_hist") or [], False)):
            if len(series) < 2:
                continue
            path = QPainterPath()
            for i, v in enumerate(series):
                px = box.left() + box.width() * i / (len(series) - 1)
                py = box.bottom() - box.height() * min(1.0, max(0.0, float(v) / 100.0))
                path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
            if solid:
                fill = QPainterPath(path)
                fill.lineTo(box.right(), box.bottom())
                fill.lineTo(box.left(), box.bottom())
                fill.closeSubpath()
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_rgba(C["main"], 0.14))
                p.drawPath(fill)
                p.setPen(QPen(C["main"], 1.4 * s))
            else:
                p.setPen(QPen(C["text_med"], 1.1 * s, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        y += ch + 22 * s

        # memory: segmented bar
        mem = data.get("mem") or {}
        p.setPen(C["text_med"])
        p.setFont(_font(self.F, 11 * s, False, 2.2 * s))
        p.drawText(QPointF(x0, y), "MEMÓRIA")
        temps = " ".join(f"{t:.0f}°" for t in (mem.get("temps") or []))
        mtxt = (f"{mem.get('used_gb', 0):.1f} / {mem.get('total_gb', 0):.0f} GB".replace(".", ",")
                + (f" · {temps}" if temps else ""))
        p.setFont(_font(MONO, 12 * s))
        p.setPen(C["white"])
        p.drawText(QPointF(x1 - QFontMetrics(p.font()).horizontalAdvance(mtxt), y), mtxt)
        y += 8 * s
        segs = 24
        gap = 3 * s
        sw = ((x1 - x0) - gap * (segs - 1)) / segs
        used = round(segs * float(mem.get("pct") or 0) / 100.0)
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(segs):
            p.setBrush(C["main"] if i < used else _rgba(C["glass_edge"], 0.14))
            p.drawRoundedRect(QRectF(x0 + i * (sw + gap), y, sw, 11 * s), 2 * s, 2 * s)
        y += 11 * s + 22 * s

        # VRAM + SSD bars
        ssd = data.get("ssd") or {}
        vu, vt = gpu.get("vram_used"), gpu.get("vram_total")
        bars = []
        if vu is not None:
            bars.append(("VRAM", (f"{vu / 1024:.1f} / {vt / 1024:.0f} GB".replace(".", ",") if vt else f"{vu:.0f} MB"),
                         (vu / vt) if vt else 0.0))
        if ssd.get("pct") is not None:
            extra = []
            if ssd.get("temp") is not None:
                extra.append(f"{ssd['temp']:.0f}°")
            if ssd.get("life") is not None:
                extra.append(f"vida {ssd['life']:.0f}%")
            bars.append(("SSD", f"{ssd['pct']:.0f}%" + (" · " + " · ".join(extra) if extra else ""),
                         ssd["pct"] / 100.0))
        for nm, val, frac in bars:
            p.setPen(C["text_med"])
            p.setFont(_font(self.F, 11 * s, False, 2.2 * s))
            p.drawText(QPointF(x0, y), nm)
            p.setPen(C["white"])
            p.setFont(_font(MONO, 12 * s))
            p.drawText(QPointF(x1 - QFontMetrics(p.font()).horizontalAdvance(val), y), val)
            y += 8 * s
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_rgba(C["glass_edge"], 0.14))
            p.drawRoundedRect(QRectF(x0, y, x1 - x0, 6 * s), 3 * s, 3 * s)
            gr = QLinearGradient(QPointF(x0, y), QPointF(x1, y))
            gr.setColorAt(0.0, _rgba(C["main"], 0.45))
            gr.setColorAt(1.0, C["main"])
            p.setBrush(QBrush(gr))
            p.drawRoundedRect(QRectF(x0, y, (x1 - x0) * max(0.02, min(1.0, frac)), 6 * s), 3 * s, 3 * s)
            y += 6 * s + 22 * s
        return y - 22 * s + 18 * s

    def _right_panels(self, p, g, C, data):
        s, M, pw, W = g["s"], g["M"], g["pw"], g["W"]
        x = W - M - 8 * s - pw
        top = g["top"] + 12 * s
        bottom = g["ctrl_y"] - 18 * s
        net_h = 112 * s
        ag = QRectF(x, top, pw, bottom - top - net_h - 18 * s)
        self._glass(p, ag, C, s)
        x0, x1 = ag.left() + 20 * s, ag.right() - 20 * s
        y = ag.top() + 30 * s
        month_name = str(data.get("month_name") or "")
        self._label(p, x0, y, f"AGENDA · {month_name}" if month_name else "AGENDA · HOJE", C, s)
        shared = C["yellow"]
        # legend, at the foot of the panel (the month's name needs the title row)
        p.setFont(_font(self.F, 11.5 * s))
        lx = x1
        y_title, y = y, ag.bottom() - 14 * s
        for txt, col, square in (("Compartilhada", shared, True), ("Pessoal", C["main"], False)):
            tw = QFontMetrics(p.font()).horizontalAdvance(txt)
            lx -= tw
            p.setPen(C["text"])
            p.drawText(QPointF(lx, y), txt)
            lx -= 13 * s
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            if square:
                p.drawRoundedRect(QRectF(lx, y - 8 * s, 8 * s, 8 * s), 2 * s, 2 * s)
            else:
                p.drawEllipse(QRectF(lx, y - 8 * s, 8 * s, 8 * s))
            lx -= 16 * s
        y = y_title + 14 * s
        self._month_list(p, C, s, data, QRectF(ag.left() + 8 * s, y, ag.width() - 16 * s,
                                               ag.bottom() - 34 * s - y), shared)

        # network
        nr = QRectF(x, ag.bottom() + 18 * s, pw, net_h)
        self._glass(p, nr, C, s, strong=0.8)
        nx0, nx1 = nr.left() + 20 * s, nr.right() - 20 * s
        ny = nr.top() + 30 * s
        self._label(p, nx0, ny, "REDE", C, s)
        net = data.get("net") or {}
        ping = net.get("ping")
        val = f"↓ {net.get('down', 0):.1f} · ↑ {net.get('up', 0):.1f} Mbps" + (f" · {ping:.0f} ms" if ping else "")
        p.setPen(C["white"])
        p.setFont(_font(MONO, 12 * s))
        p.drawText(QPointF(nx1 - QFontMetrics(p.font()).horizontalAdvance(val), ny), val)
        hist = list(data.get("net_hist") or [])
        box = QRectF(nx0, ny + 14 * s, nx1 - nx0, nr.bottom() - ny - 30 * s)
        if len(hist) > 1:
            top_v = max(1.0, max(hist))
            path = QPainterPath()
            for i, v in enumerate(hist):
                px = box.left() + box.width() * i / (len(hist) - 1)
                py = box.bottom() - box.height() * (v / top_v)
                path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
            p.setPen(QPen(C["main"], 1.2 * s))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

    # ── the month's agenda ──────────────────────────────────────────────────

    _AG_IDLE = 60.0        # a scrolled agenda goes back to today after a minute

    def _ag_scroll_state(self):
        if getattr(self, "_ag_user", None) is not None and \
                time.monotonic() - getattr(self, "_ag_user_t", 0.0) > self._AG_IDLE:
            self._ag_user = None
        u = getattr(self, "_ag_user", None)
        return None if u is None else int(u)

    def scroll_agenda(self, dy: float) -> bool:
        """Mouse wheel over the agenda. True when it moved."""
        mx = getattr(self, "_ag_max", 0.0)
        if mx <= 0:
            return False
        base = self._ag_user if getattr(self, "_ag_user", None) is not None else getattr(self, "_ag_anchor", 0.0)
        self._ag_user = max(0.0, min(mx, base + dy))
        self._ag_user_t = time.monotonic()
        return True

    def over_agenda(self, x: float, y: float) -> bool:
        r = getattr(self, "agenda_rect", None)
        return bool(r is not None and r.contains(QPointF(x, y)))

    def _month_list(self, p, C, s, data, view: QRectF, shared: QColor) -> None:
        month = list(data.get("month") or [])
        if not month:                       # before the first month refresh: today only
            month = [{"label": "HOJE", "today": True, "past": False,
                      "events": list(data.get("agenda") or [])}]
        head_h, row_h, empty_h, gap = 30 * s, 48 * s, 34 * s, 10 * s
        rows, y, anchor, starts = [], 0.0, 0.0, []
        for d in month:
            evs = d.get("events") or []
            h = head_h + (len(evs) * row_h if evs else empty_h)
            if d.get("today"):
                anchor = y
            rows.append((y, h, d))
            starts.append(y)
            starts += [y + head_h + i * row_h for i in range(len(evs))]
            y += h + gap
        content_h = max(0.0, y - gap)
        self._ag_max = max(0.0, content_h - view.height())
        # today at the top when the month allows it; otherwise as high as it
        # goes, starting on a whole row rather than half an event
        want = min(anchor, self._ag_max)
        self._ag_anchor = min((b for b in starts if b >= want - 0.5), default=want)
        self._ag_max = max(self._ag_max, self._ag_anchor)
        self.agenda_rect = QRectF(view)
        off = self._ag_user if getattr(self, "_ag_user", None) is not None else self._ag_anchor
        off = max(0.0, min(self._ag_max, off))

        p.save()
        p.setClipRect(view)
        lx0, lx1 = view.left() + 12 * s, view.right() - 14 * s
        for top, h, d in rows:
            ty = view.top() + top - off
            if ty > view.bottom() or ty + h < view.top():
                continue
            today, past = bool(d.get("today")), bool(d.get("past"))
            p.setOpacity(0.45 if past else 1.0)
            if today:
                hl = QRectF(view.left() + 2 * s, ty, view.width() - 8 * s, h)
                p.setPen(QPen(_rgba(C["main"], 0.45), 1))
                p.setBrush(_rgba(C["main"], 0.10))
                p.drawRoundedRect(hl, 10 * s, 10 * s)
            # day header
            p.setPen(C["main"] if today else C["text"])
            p.setFont(_font(self.F, 11.5 * s, True, 2.4 * s))
            label = str(d.get("label") or "") + ("  ·  HOJE" if today and d.get("label") != "HOJE" else "")
            p.drawText(QPointF(lx0, ty + 20 * s), label)
            if not today:
                fm = QFontMetrics(p.font())
                p.setPen(QPen(_rgba(C["glass_edge"], 0.14), 1))
                lx = lx0 + fm.horizontalAdvance(label) + 10 * s
                if lx < lx1:
                    p.drawLine(QPointF(lx, ty + 16 * s), QPointF(lx1, ty + 16 * s))
            ey = ty + head_h
            evs = d.get("events") or []
            if not evs:
                p.setPen(C["text_med"])
                p.setFont(_font(self.F, 13.5 * s))
                p.drawText(QPointF(lx0, ey + 20 * s), "Nada marcado para hoje" if today else "Livre")
            for e in evs:
                col = shared if e.get("kind") == "compartilhada" else \
                    C["main"] if e.get("kind") == "pessoal" else C["text_med"]
                p.setPen(C["white"])
                p.setFont(_font(MONO, 13 * s))
                tt = str(e.get("time") or "")
                p.drawText(QPointF(lx0, ey + 22 * s), "dia" if tt == "dia todo" else tt)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(QRectF(lx0 + 54 * s, ey + 8 * s, 3 * s, row_h - 16 * s), 1.5 * s, 1.5 * s)
                tx = lx0 + 66 * s
                p.setPen(C["white"])
                p.setFont(_font(self.F, 14.5 * s))
                p.drawText(QPointF(tx, ey + 22 * s), QFontMetrics(p.font()).elidedText(
                    str(e.get("title") or ""), Qt.TextElideMode.ElideRight, int(lx1 - tx)))
                p.setPen(C["text_med"])
                p.setFont(_font(self.F, 11 * s))
                kind = {"compartilhada": "Compartilhada", "pessoal": "Pessoal"}.get(e.get("kind"), "Nota")
                sub = kind + (f" · {e['calendar']}" if e.get("calendar") else "")
                p.drawText(QPointF(tx, ey + 39 * s), QFontMetrics(p.font()).elidedText(
                    sub, Qt.TextElideMode.ElideRight, int(lx1 - tx)))
                ey += row_h
        p.setOpacity(1.0)
        p.restore()

        # scroll bar, only when the month does not fit
        if self._ag_max > 0:
            track = QRectF(view.right() - 4 * s, view.top() + 4 * s, 2.5 * s, view.height() - 8 * s)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_rgba(C["glass_edge"], 0.10))
            p.drawRoundedRect(track, 1.2 * s, 1.2 * s)
            frac = view.height() / content_h
            th = max(24 * s, track.height() * frac)
            tyy = track.top() + (track.height() - th) * (off / self._ag_max)
            p.setBrush(_rgba(C["main"], 0.55))
            p.drawRoundedRect(QRectF(track.left(), tyy, track.width(), th), 1.2 * s, 1.2 * s)

    def _ring_scale(self, p, g, C):
        cx, cy, r, s = g["cx"], g["cy"], g["r"], g["s"]
        key = (int(r), int(cx), int(cy), C["main"].rgb(), C["border_b"].rgb())
        if self._ring_key != key or self._ring is None:
            pad = int(r + 40 * s)
            pm = QPixmap(2 * pad, 2 * pad)
            pm.fill(Qt.GlobalColor.transparent)
            rp = QPainter(pm)
            rp.setRenderHint(QPainter.RenderHint.Antialiasing)
            c = QPointF(pad, pad)
            glow = QRadialGradient(c, r)
            glow.setColorAt(0.0, _rgba(C["main"], 0.14))
            glow.setColorAt(0.6, _rgba(C["main"], 0.04))
            glow.setColorAt(1.0, _rgba(C["main"], 0.0))
            rp.setPen(Qt.PenStyle.NoPen)
            rp.setBrush(QBrush(glow))
            rp.drawEllipse(c, r, r)
            rp.setBrush(Qt.BrushStyle.NoBrush)
            rp.setPen(QPen(_rgba(C["border"], 0.9), 1))
            rp.drawEllipse(c, r, r)
            rp.setPen(QPen(_rgba(C["border_b"], 0.85), 1))
            for a in range(0, 360, 5):
                if a % 90 == 0:
                    continue
                major = a % 30 == 0
                r1, r2 = (r - 24 * s, r - 4 * s) if major else (r - 14 * s, r - 8 * s)
                ca, sa = math.cos(math.radians(a)), math.sin(math.radians(a))
                rp.drawLine(QPointF(c.x() + r1 * ca, c.y() + r1 * sa), QPointF(c.x() + r2 * ca, c.y() + r2 * sa))
            rp.setPen(QPen(C["main"], 1.6 * s))
            for a in (0, 90, 180, 270):
                ca, sa = math.cos(math.radians(a)), math.sin(math.radians(a))
                rp.drawLine(QPointF(c.x() + (r + 2 * s) * ca, c.y() + (r + 2 * s) * sa),
                            QPointF(c.x() + (r - 18 * s) * ca, c.y() + (r - 18 * s) * sa))
            rp.setPen(QPen(_rgba(C["border"], 0.9), 1, Qt.PenStyle.CustomDashLine))
            pen = rp.pen()
            pen.setDashPattern([1.5, 6])
            rp.setPen(pen)
            rp.drawEllipse(c, r * 0.82, r * 0.82)
            rp.end()
            self._ring, self._ring_key = pm, key
            self._ring_pad = pad
        p.drawPixmap(QPointF(cx - self._ring_pad, cy - self._ring_pad), self._ring)

    def _state_block(self, p, g, C, st_col, st_txt, amp, data, muted):
        s, cx, cy, r = g["s"], g["cx"], g["cy"], g["r"]
        y = cy + r + 30 * s
        p.setPen(st_col)
        p.setFont(_font(self.F, 13 * s, True, 6.5 * s))
        p.drawText(QRectF(cx - 300 * s, y - 14 * s, 600 * s, 20 * s), Qt.AlignmentFlag.AlignCenter, st_txt)
        y += 26 * s
        if not muted:
            w = 440 * s
            path = QPainterPath()
            t = time.monotonic()
            n = 110
            for i in range(n + 1):
                fx = i / n
                env = math.sin(math.pi * fx) ** 1.4
                v = env * (0.55 * math.sin(fx * 58 + t * 6) + 0.3 * math.sin(fx * 170 - t * 9)
                           + 0.15 * math.sin(fx * 290 + t * 13))
                hgt = (2.0 + 15.0 * min(1.0, amp * 1.6)) * s
                px, py = cx - w / 2 + w * fx, y + v * hgt
                path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
            p.setPen(QPen(_rgba(st_col, 0.85), 1.4 * s))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        else:
            # what was heard is not echoed here: it goes to the REGISTRO log
            p.setPen(C["text_med"])
            p.setFont(_font(self.F, 14 * s))
            p.drawText(QRectF(cx - 300 * s, y - 12 * s, 600 * s, 24 * s), Qt.AlignmentFlag.AlignCenter,
                       "Toque no microfone para voltar a ouvir")

    def _controls(self, p, g, C, muted, speaking):
        s, cx = g["s"], g["cx"]
        y, h = g["ctrl_y"], g["ctrl_h"]
        f = _font(self.F, 11 * s, True, 2.4 * s)
        fm = QFontMetrics(f)
        fname = str(getattr(self, "_file_label", "") or "")
        if len(fname) > 16:
            fname = fname[:13] + "…"
        specs = [
            ("mic", "MIC DESLIGADO" if muted else "MIC ATIVO", C["red"] if muted else C["green"], True),
            ("interrupt", "INTERROMPER", C["orange"], speaking),
            ("log", "REGISTRO", C["white"] if getattr(self, "_log_open", False) else C["text"], True),
            ("files", fname or "ARQUIVOS", C["text"], True),
        ]
        widths = [fm.horizontalAdvance(t) + 52 * s for _, t, _, _ in specs]
        gap = 10 * s
        x = cx - (sum(widths) + gap * (len(specs) - 1)) / 2
        self.hits = {}
        for (key, txt, col, enabled), w in zip(specs, widths):
            rect = QRectF(x, y, w, h)
            a = 1.0 if enabled else 0.4
            g2 = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            g2.setColorAt(0.0, _rgba(col if key != "modules" else C["main"], 0.13 * a))
            g2.setColorAt(1.0, _rgba(col if key != "modules" else C["main"], 0.04 * a))
            p.setBrush(QBrush(g2))
            p.setPen(QPen(_rgba(col if key != "modules" else C["glass_edge"], 0.38 * a), 1))
            p.drawRoundedRect(rect, h / 2, h / 2)
            p.setPen(_rgba(col, a))
            ix, iy = rect.left() + 18 * s, rect.center().y()
            self._icon(p, key, ix, iy, s, _rgba(col, a))
            p.setFont(f)
            p.setPen(_rgba(col, a))
            p.drawText(QRectF(ix + 14 * s, rect.top(), w - 36 * s, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)
            if enabled:
                self.hits[key] = rect
            x += w + gap

    def _icon(self, p, key, x, y, s, col):
        p.save()
        p.setPen(QPen(col, 1.4 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if key == "mic":
            p.drawRoundedRect(QRectF(x - 2.5 * s, y - 7 * s, 5 * s, 8.5 * s), 2.5 * s, 2.5 * s)
            p.drawArc(QRectF(x - 5 * s, y - 5 * s, 10 * s, 8 * s), 180 * 16, 180 * 16)
            p.drawLine(QPointF(x, y + 3 * s), QPointF(x, y + 6 * s))
        elif key == "interrupt":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(x - 4.5 * s, y - 4.5 * s, 9 * s, 9 * s), 1.5 * s, 1.5 * s)
        elif key == "log":
            for dy in (-4, 0, 4):
                p.drawLine(QPointF(x - 5 * s, y + dy * s), QPointF(x + 5 * s, y + dy * s))
        elif key == "files":
            path = QPainterPath()
            path.moveTo(x - 4.5 * s, y - 6 * s)
            path.lineTo(x + 1.5 * s, y - 6 * s)
            path.lineTo(x + 4.5 * s, y - 3 * s)
            path.lineTo(x + 4.5 * s, y + 6 * s)
            path.lineTo(x - 4.5 * s, y + 6 * s)
            path.closeSubpath()
            p.drawPath(path)
        else:
            for dx in (-4, 2):
                for dy in (-4, 2):
                    p.drawRoundedRect(QRectF(x + dx * s - 1 * s, y + dy * s - 1 * s, 4.5 * s, 4.5 * s), 1 * s, 1 * s)
        p.restore()

    def hit(self, x: float, y: float) -> str | None:
        for k, r in list(getattr(self, "top_hits", {}).items()) + list(self.hits.items()):
            if r.contains(QPointF(x, y)):
                return k
        return None
