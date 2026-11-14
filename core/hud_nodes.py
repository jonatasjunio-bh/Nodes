"""
core/hud_nodes.py — the NODES screen: the clean HUD's content in the reference
layout the user picked.

    top         NODES · online | Assistente pessoal ............ bell · sliders ·
                                                                 sun · gear · clock
    left        the performance card (smaller) and the network card under it
    centre      the constellation sphere (core/neural_plexus.py) in a thin ring
                with the CPU and state arcs; NODES and "N SINAPSES · ● OUVINDO"
                in small type under it
    right       the month's agenda (smaller), "ver mês →" opens the calendar
    bottom      Falar · Nova tarefa · Ver agenda · Arquivos · Apps, and a text
                box ("Como posso te ajudar hoje?") — the real QLineEdit is laid
                over `input_rect` by the HUD canvas

It is a CleanHUD underneath (core/hud_clean.py), so the month list, its scroll,
the performance body, the cards stepping aside and the drawers placed under the
top icons all work as they do there. Chosen in ⚙ as the clean HUD's third centre
("nodes").
"""

from __future__ import annotations

import math
import time

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap

from core.hud_clean import MONO, CleanHUD, _MON, _WEEK, _font, _rgba


class NodesHUD(CleanHUD):
    PLACEHOLDER = "Como posso te ajudar hoje?"

    def __init__(self, ui_font: str):
        super().__init__(ui_font)
        self.input_rect: QRectF | None = None

    # ── geometry ────────────────────────────────────────────────────────────

    def _geo(self, W: float, H: float, panels: bool) -> dict:
        s = max(0.62, min(W / 1440.0, H / 900.0))
        M = 30 * s
        top = 84 * s
        pw = min(300 * s, W * 0.23)
        show = panels and W >= 980
        input_h = 40 * s
        input_y = H - 20 * s - input_h
        ctrl_h = 36 * s
        ctrl_y = input_y - 14 * s - ctrl_h
        below = 56 * s                           # NODES + the small status line
        avail_top, avail_bot = top, ctrl_y - below
        side = (M + pw + 30 * s) if show else M
        # held to under a third of the height: the centre, not the whole screen
        r = max(60.0, min((avail_bot - avail_top) / 2.0 - 6 * s, (W - 2 * side) / 2.0, H * 0.29))
        cy = avail_top + (avail_bot - avail_top) / 2.0
        return {"s": s, "M": M, "top": top, "pw": pw, "show": show, "cx": W / 2.0, "cy": cy, "r": r,
                "ctrl_y": ctrl_y, "ctrl_h": ctrl_h, "input_y": input_y, "input_h": input_h, "W": W, "H": H}

    # ── public ──────────────────────────────────────────────────────────────

    def paint(self, p: QPainter, hud, W: float, H: float, colors: dict, data: dict,
              cards_active: bool, k: float = 1.0) -> None:
        g = self._geo(W, H, panels=not cards_active)
        self.core_base_r = g["r"]
        g["r"] *= max(0.4, min(1.0, k))
        s, cx, cy, r = g["s"], g["cx"], g["cy"], g["r"]
        self.core = (cx, cy, r)
        side = (g["M"] + g["pw"] + 24 * s) if g["show"] else g["M"]
        self.centre_band = (side, W - side, g["top"] + 12 * s, g["ctrl_y"] - 60 * s)
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

        self._open_drawers = tuple(getattr(hud, "open_drawers", ()) or ())
        self._log_open = bool(getattr(hud, "log_open", False))
        self._topbar(p, g, C, data)

        if g["show"]:
            key = (int(data.get("t", 0)), int(W), int(H), C["main"].rgb(), muted,
                   len(data.get("agenda") or []), self._ag_scroll_state(), "nodes")
            if self._panels_key != key or self._panels is None:
                pm = QPixmap(int(W), int(H))
                pm.fill(Qt.GlobalColor.transparent)
                pp = QPainter(pm)
                pp.setRenderHint(QPainter.RenderHint.Antialiasing)
                self._left_cards(pp, g, C, data)
                self._agenda_card(pp, g, C, data)
                pp.end()
                self._panels, self._panels_key = pm, key
            p.drawPixmap(0, 0, self._panels)

        # the ring: a thin circle and two glowing arcs (CPU load, state + voice)
        amp = float(getattr(hud, "_amp_disp", 0.0) or 0.0)
        t = time.monotonic()
        rr = r
        box = QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_rgba(C["border_b"], 0.55), 1.4 * s))
        p.drawEllipse(box)
        cpu = float((data.get("cpu") or {}).get("load") or 0.0)
        span = 24 + 110 * min(1.0, cpu / 100.0)
        span2 = 28 + 64 * min(1.0, amp * 1.4)
        start2 = -18 - (t * 9) % 360
        for col, a0, sp in ((C["main"], 150 - span, span), (st_col, start2, -span2)):
            for w, a in ((14 * s, 0.10), (7 * s, 0.22), (3 * s, 1.0)):
                p.setPen(QPen(_rgba(col, a), w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawArc(box, int(a0 * 16), int(sp * 16))

        # the constellation
        # the constellation, or the film's consciousness (hud_core "nodes_film")
        sph = hud._nodes_sphere() if hasattr(hud, "_nodes_sphere") else getattr(hud, "_plexus", None)
        if sph is not None:
            sph.set_knowledge(data.get("knowledge"))
            sph.set_items(data.get("knowledge_items"))
            sph.paint(p, cx, cy, r * 0.80, C["main"], st_col, C["bg"])

        # NODES and the small status line
        y = cy + r + 26 * s
        name = str(data.get("name") or "NODES").upper()
        p.setPen(C["white"])
        p.setFont(_font(self.F, 18 * s, True, 9 * s))
        p.drawText(QRectF(cx - 300 * s, y - 16 * s, 600 * s, 24 * s), Qt.AlignmentFlag.AlignCenter, name)
        y += 22 * s
        n = getattr(sph, "alive", 0) if sph is not None else 0
        f = _font(self.F, 11 * s, False, 2.6 * s)
        p.setFont(f)
        fm = QFontMetrics(f)
        left = f"{n} SINAPSES  ·  " if n else ""
        wl, wr = fm.horizontalAdvance(left), fm.horizontalAdvance(st_txt)
        x0 = cx - (wl + 14 * s + wr) / 2
        p.setPen(C["text_med"])
        p.drawText(QPointF(x0, y), left)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(st_col)
        p.drawEllipse(QPointF(x0 + wl + 4 * s, y - 4 * s), 3.5 * s, 3.5 * s)
        p.setPen(st_col)
        p.drawText(QPointF(x0 + wl + 14 * s, y), st_txt)

        self._file_label = getattr(hud, "file_label", "")
        self._controls(p, g, C, muted, speaking)
        self._input_box(p, g, C)

    # ── top bar ─────────────────────────────────────────────────────────────

    def _topbar(self, p, g, C, data):
        s, M, W = g["s"], g["M"], g["W"]
        y = 40 * s
        name = str(data.get("name") or "NODES").upper()
        p.setPen(C["white"])
        p.setFont(_font(self.F, 21 * s, True, 9 * s))
        fm = QFontMetrics(p.font())
        p.drawText(QPointF(M + 10 * s, y), name)
        x = M + 10 * s + fm.horizontalAdvance(name) + 22 * s
        online = bool(data.get("online", True))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(C["green"] if online else C["dim"])
        p.drawEllipse(QPointF(x + 4 * s, y - 6 * s), 4 * s, 4 * s)
        p.setFont(_font(self.F, 13 * s, False, 0.8 * s))
        p.setPen(C["green"] if online else C["dim"])
        label = "ONLINE" if online else "OFFLINE"
        p.drawText(QPointF(x + 14 * s, y - 1 * s), label)
        x += 14 * s + QFontMetrics(p.font()).horizontalAdvance(label) + 18 * s
        p.setPen(QPen(_rgba(C["glass_edge"], 0.35), 1))
        p.drawLine(QPointF(x, y - 18 * s), QPointF(x, y + 4 * s))
        p.setPen(C["text"])
        p.setFont(_font(self.F, 15 * s))
        p.drawText(QPointF(x + 18 * s, y - 1 * s), "Assistente pessoal")

        # clock, right
        now = time.localtime()
        clock = time.strftime("%H:%M", now)
        p.setFont(_font(self.F, 25 * s, True))
        cw = QFontMetrics(p.font()).horizontalAdvance(clock)
        p.setPen(C["white"])
        p.drawText(QPointF(W - M - 10 * s - cw, y - 6 * s), clock)
        dtxt = f"{_WEEK[now.tm_wday]}, {now.tm_mday:02d} {_MON[now.tm_mon - 1]} {now.tm_year}"
        p.setFont(_font(self.F, 12 * s))
        dw = QFontMetrics(p.font()).horizontalAdvance(dtxt)
        p.setPen(C["text_med"])
        p.drawText(QPointF(W - M - 10 * s - dw, y + 13 * s), dtxt)
        sep = W - M - 10 * s - max(cw, dw) - 22 * s
        p.setPen(QPen(_rgba(C["glass_edge"], 0.35), 1))
        p.drawLine(QPointF(sep, y - 22 * s), QPointF(sep, y + 12 * s))

        # icons: log (bell), everyday controls (sliders), screens (two windows), setup (gear)
        self.top_hits = {}
        bx = sep - 22 * s
        for key in ("setup", "style", "controls", "log"):
            c = QPointF(bx - 14 * s, y - 4 * s)
            rect = QRectF(c.x() - 15 * s, c.y() - 15 * s, 30 * s, 30 * s)
            on = key in self._open_drawers or (key == "log" and self._log_open)
            if on:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_rgba(C["main"], 0.18))
                p.drawEllipse(rect)
            self._top_icon(p, key, c.x(), c.y(), s * 1.15, C["white"] if on else C["text"])
            self.top_hits[key] = rect
            bx -= 46 * s

    def _top_icon(self, p, key, x, y, s, col):
        if key == "controls":
            return super()._top_icon(p, key, x, y, s, col)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(col, 1.4 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if key == "setup":                        # gear: a toothed ring, not the sun's rays
            p.drawEllipse(QPointF(x, y), 2.6 * s, 2.6 * s)
            teeth = QPen(col, 2.6 * s, Qt.PenStyle.CustomDashLine, Qt.PenCapStyle.FlatCap)
            circ = 2 * math.pi * 6.2 * s
            unit = max(0.1, circ / 8 / (2.6 * s))  # 8 teeth, in units of the pen width
            teeth.setDashPattern([unit * 0.5, unit * 0.5])
            p.setPen(teeth)
            p.drawEllipse(QPointF(x, y), 6.2 * s, 6.2 * s)
            p.setPen(QPen(col, 1.4 * s))
            p.drawEllipse(QPointF(x, y), 5.0 * s, 5.0 * s)
            return
        if key == "log":                          # bell
            path = QPainterPath()
            path.moveTo(x - 6 * s, y + 4 * s)
            path.cubicTo(x - 5 * s, y + 2 * s, x - 5 * s, y - 1 * s, x - 5 * s, y - 2 * s)
            path.cubicTo(x - 5 * s, y - 6.5 * s, x + 5 * s, y - 6.5 * s, x + 5 * s, y - 2 * s)
            path.cubicTo(x + 5 * s, y - 1 * s, x + 5 * s, y + 2 * s, x + 6 * s, y + 4 * s)
            path.closeSubpath()
            p.drawPath(path)
            p.drawLine(QPointF(x - 1.5 * s, y + 6.5 * s), QPointF(x + 1.5 * s, y + 6.5 * s))
        elif key == "style":                      # screens: two windows, one behind the other
            back = QPainterPath()
            back.moveTo(x - 4 * s, y - 4.5 * s)
            back.lineTo(x - 4 * s, y - 7 * s)
            back.lineTo(x + 7.5 * s, y - 7 * s)
            back.lineTo(x + 7.5 * s, y + 1.5 * s)
            back.lineTo(x + 5 * s, y + 1.5 * s)
            p.drawPath(back)
            p.drawRoundedRect(QRectF(x - 7.5 * s, y - 3.5 * s, 11.5 * s, 9 * s), 1.4 * s, 1.4 * s)
            p.drawLine(QPointF(x - 7.5 * s, y - 0.8 * s), QPointF(x + 4 * s, y - 0.8 * s))

    # ── cards ───────────────────────────────────────────────────────────────

    def _card(self, p, rect: QRectF, C, s):
        g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        g.setColorAt(0.0, _rgba(C["main"], 0.075))
        g.setColorAt(1.0, _rgba(C["main"], 0.03))
        p.setPen(QPen(_rgba(C["glass_edge"], 0.22), 1))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(rect, 14 * s, 14 * s)

    def _left_cards(self, p, g, C, data):
        s, M = g["s"], g["M"]
        k = 0.84                                   # the same body as the clean HUD's, smaller
        pw = g["pw"]
        top = g["top"] + 8 * s
        x0, x1 = M + 18 * s, M + pw - 18 * s
        probe = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
        mp = QPainter(probe)
        end = self._perf_body(mp, s * k, C, data, x0, x1, top + 26 * s)
        mp.end()
        net_h = 86 * s
        bottom_limit = g["ctrl_y"] - 18 * s - net_h - 14 * s
        R = QRectF(M, top, pw, min(bottom_limit, end + 4 * s) - top)
        self._card(p, R, C, s)
        self._perf_body(p, s * k, C, data, x0, x1, top + 26 * s)

        # network, under it
        nr = QRectF(M, R.bottom() + 14 * s, pw, net_h)
        self._card(p, nr, C, s)
        ny = nr.top() + 26 * s
        self._label(p, x0, ny, "REDE", C, s * 0.9)
        net = data.get("net") or {}
        ping = net.get("ping")
        val = f"↓ {net.get('down', 0):.1f}  ↑ {net.get('up', 0):.1f} Mbps" + (f" · {ping:.0f} ms" if ping else "")
        p.setPen(C["white"])
        p.setFont(_font(MONO, 11 * s))
        p.drawText(QPointF(x1 - QFontMetrics(p.font()).horizontalAdvance(val), ny), val)
        hist = list(data.get("net_hist") or [])
        box = QRectF(x0, ny + 12 * s, x1 - x0, nr.bottom() - ny - 24 * s)
        if len(hist) > 1:
            top_v = max(1.0, max(hist))
            path = QPainterPath()
            for i, v in enumerate(hist):
                px = box.left() + box.width() * i / (len(hist) - 1)
                py = box.bottom() - box.height() * (v / top_v)
                path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
            p.setPen(QPen(C["main"], 1.3 * s))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

    def _agenda_card(self, p, g, C, data):
        s, M, pw, W = g["s"], g["M"], g["pw"], g["W"]
        x = W - M - pw
        top = g["top"] + 8 * s
        bottom = g["ctrl_y"] - 18 * s
        ag = QRectF(x, top, pw, bottom - top)
        self._card(p, ag, C, s)
        x0, x1 = ag.left() + 18 * s, ag.right() - 18 * s
        y = ag.top() + 26 * s
        self._label(p, x0, y, "AGENDA", C, s * 0.9)
        month_name = str(data.get("month_name") or "")
        if month_name:
            p.setPen(C["text_med"])
            p.setFont(_font(self.F, 11 * s, False, 2.0 * s))
            p.drawText(QPointF(x1 - QFontMetrics(p.font()).horizontalAdvance(month_name.upper()), y),
                       month_name.upper())
        # foot: legend and "ver mês →"
        shared = C["yellow"]
        fy = ag.bottom() - 14 * s
        p.setFont(_font(self.F, 11 * s))
        lx = x0
        for txt, col in (("Pessoal", C["main"]), ("Compart.", shared)):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawEllipse(QRectF(lx, fy - 8 * s, 7 * s, 7 * s))
            p.setPen(C["text"])
            p.drawText(QPointF(lx + 11 * s, fy), txt)
            lx += 11 * s + QFontMetrics(p.font()).horizontalAdvance(txt) + 12 * s
        more = "ver mês  →"
        p.setPen(C["main"])
        p.setFont(_font(self.F, 11.5 * s, True))
        mw = QFontMetrics(p.font()).horizontalAdvance(more)
        p.drawText(QPointF(x1 - mw, fy), more)
        self._agenda_more = QRectF(x1 - mw - 6 * s, fy - 16 * s, mw + 12 * s, 22 * s)
        p.setPen(QPen(_rgba(C["glass_edge"], 0.14), 1))
        p.drawLine(QPointF(x0, fy - 22 * s), QPointF(x1, fy - 22 * s))
        y += 12 * s
        self._month_list(p, C, s * 0.88, data, QRectF(ag.left() + 6 * s, y, ag.width() - 12 * s,
                                                      fy - 30 * s - y), shared)

    # ── bottom: buttons and the text box ────────────────────────────────────

    def _controls(self, p, g, C, muted, speaking):
        s, cx = g["s"], g["cx"]
        y, h = g["ctrl_y"], g["ctrl_h"]
        f = _font(self.F, 13 * s, False, 0.4 * s)
        fm = QFontMetrics(f)
        fname = str(getattr(self, "_file_label", "") or "")
        if len(fname) > 16:
            fname = fname[:13] + "…"
        if speaking:
            talk = ("talk", "Parar", C["orange"], "interrupt")
        elif muted:
            talk = ("talk", "Mic desligado", C["red"], "mic")
        else:
            talk = ("talk", "Falar", C["green"], "mic")
        specs = [talk, ("task", "Nova tarefa", C["text"], "task"),
                 ("agenda", "Ver agenda", C["text"], "agenda"),
                 ("files", fname or "Arquivos", C["text"], "files"),
                 ("modules", "Apps", C["text"], "modules")]
        widths = [fm.horizontalAdvance(t) + 58 * s for _, t, _, _ in specs]
        gap = 12 * s
        x = cx - (sum(widths) + gap * (len(specs) - 1)) / 2
        self.hits = {}
        for (key, txt, col, icon), w in zip(specs, widths):
            rect = QRectF(x, y, w, h)
            accent = key == "talk"
            p.setBrush(_rgba(col, 0.14) if accent else _rgba(C["main"], 0.05))
            p.setPen(QPen(_rgba(col if accent else C["glass_edge"], 0.85 if accent else 0.32), 1.3 if accent else 1))
            p.drawRoundedRect(rect, h / 2, h / 2)
            ix, iy = rect.left() + 22 * s, rect.center().y()
            self._icon(p, icon, ix, iy, s * 1.1, col if accent else C["white"])
            p.setFont(f)
            p.setPen(col if accent else C["white"])
            p.drawText(QRectF(ix + 16 * s, rect.top(), w - 40 * s, h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)
            self.hits[key] = rect
            x += w + gap
        if getattr(self, "_agenda_more", None) is not None:
            self.hits["agenda_more"] = self._agenda_more

    def _icon(self, p, key, x, y, s, col):
        if key not in ("task", "agenda"):
            return super()._icon(p, key, x, y, s, col)
        p.save()
        p.setPen(QPen(col, 1.4 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if key == "task":                          # +
            p.drawLine(QPointF(x - 6 * s, y), QPointF(x + 6 * s, y))
            p.drawLine(QPointF(x, y - 6 * s), QPointF(x, y + 6 * s))
        else:                                      # calendar
            p.drawRoundedRect(QRectF(x - 6 * s, y - 5 * s, 12 * s, 11 * s), 2 * s, 2 * s)
            p.drawLine(QPointF(x - 6 * s, y - 1.5 * s), QPointF(x + 6 * s, y - 1.5 * s))
            p.drawLine(QPointF(x - 3 * s, y - 7 * s), QPointF(x - 3 * s, y - 4 * s))
            p.drawLine(QPointF(x + 3 * s, y - 7 * s), QPointF(x + 3 * s, y - 4 * s))
        p.restore()

    def _input_box(self, p, g, C):
        s, cx = g["s"], g["cx"]
        w = min(760 * s, g["W"] - 2 * (g["M"] + 40 * s))
        rect = QRectF(cx - w / 2, g["input_y"], w, g["input_h"])
        p.setBrush(_rgba(C["main"], 0.05))
        p.setPen(QPen(_rgba(C["glass_edge"], 0.38), 1))
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        # the sparkle at the left, the send arrow at the right
        sx, sy = rect.left() + 24 * s, rect.center().y()
        p.setPen(QPen(C["main"], 1.5 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        star = QPainterPath()
        star.moveTo(sx, sy - 8 * s)
        star.quadTo(sx, sy, sx + 8 * s, sy)
        star.quadTo(sx, sy, sx, sy + 8 * s)
        star.quadTo(sx, sy, sx - 8 * s, sy)
        star.quadTo(sx, sy, sx, sy - 8 * s)
        p.setBrush(_rgba(C["main"], 0.25))
        p.drawPath(star)
        ax, ay = rect.right() - 26 * s, rect.center().y()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(C["text_med"], 1.5 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        arrow = QPainterPath()
        arrow.moveTo(ax - 8 * s, ay - 7 * s)
        arrow.lineTo(ax + 8 * s, ay)
        arrow.lineTo(ax - 8 * s, ay + 7 * s)
        arrow.lineTo(ax - 5 * s, ay)
        arrow.closeSubpath()
        p.drawPath(arrow)
        self.hits["send"] = QRectF(ax - 14 * s, ay - 14 * s, 28 * s, 28 * s)
        # the typing area (the canvas lays a real text box over it)
        self.input_rect = QRectF(rect.left() + 44 * s, rect.top() + 3 * s, rect.width() - 88 * s, rect.height() - 6 * s)
        self.input_font_px = 15 * s
        self.hits["input"] = self.input_rect
