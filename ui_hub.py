"""
ui_hub.py — the module hub: every plugin as a card with its own icon, grouped
into Smart Home and Personal features, with the selected card's settings in a
detail panel on the right.

Styled after the film's J.A.R.V.I.S screens: navy glass panels, luminous blue
lines, corner brackets. Everything is painted with QPainter — the icons are
vector drawings, so they take the theme colour and stay sharp at any size.

The forms themselves are PluginSettingsOverlay's own (this class reuses its
field builder, save and test/connect logic), so a plugin still declares its
settings once, in PLUGIN_SETTINGS, and appears here with no UI code of its own.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPolygonF, QRadialGradient)
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QPushButton, QScrollArea, QVBoxLayout, QWidget)

import ui as _ui
from ui import C, UI_FONT, qcol

BASE = Path(__file__).resolve().parent

# ── what each plugin is, in the user's language ──────────────────────────────
HOME = "home"
PERSONAL = "personal"

META = {   # name → (title, one line, icon, group)
    "smart_home":     ("Casa Inteligente", "Sonoff · i2GO · Broadlink · TV LG — os aparelhos da sua rede", "home", HOME),
    "alexa":          ("Alexa", "Echo: comandos, anúncios, música e rotinas", "alexa", HOME),
    "daily_briefing": ("Briefing Diário", "Clima, agenda do iCloud e notícias da sua cidade", "briefing", PERSONAL),
    "send_email":     ("E-mail", "Envia e-mails pelo seu Gmail — você confere e confirma na tela", "briefing", PERSONAL),
    "software":       ("Programas", "Baixa, instala, atualiza e remove programas — com a sua confirmação", "cube", PERSONAL),
    "spotify":        ("Spotify", "Música no PC, no celular e nas caixas", "spotify", PERSONAL),
    "android_phone":  ("Celular Android", "Lanterna, volume, alarmes, apps, ligações e SMS", "phone", PERSONAL),
    "voice_id":       ("Voice ID", "Biometria de voz — só obedece a você", "voice", PERSONAL),
    "deep_think":     ("Segundo Cérebro", "GPT + Gemini: pesquisa, raciocínio e desenhos em segundo plano", "brain", PERSONAL),
    "remote_access":  ("Acesso Remoto", "App no celular, Tailscale e chave do dono", "remote", PERSONAL),
    "anatomy":        ("Anatomy", "Explica qualquer conceito como uma máquina isométrica interativa", "cube", PERSONAL),
    "hwinfo":         ("HWiNFO64", "Todos os sensores do hardware, mín/máx/média e alertas discretos", "gauge", PERSONAL),
    "pc_sensors":     ("Sensores do PC", "Temperaturas de CPU, GPU e memória, uso, clocks e ventoinhas", "gauge", PERSONAL),
    "hud_display":    ("Indicadores no HUD", "Medidores e gráficos desenhados na hora, ao vivo", "gauge", PERSONAL),
}
DEVICE_ICON = {"sonoff": "bulb", "tasmota": "bulb", "smart_switch": "bulb", "tuya": "plug",
               "broadlink": "ir", "lg_tv": "tv", "alexa": "alexa", "chromecast": "cast"}
DEVICE_KIND = {"sonoff": "Sonoff · eWeLink", "tasmota": "Tasmota", "smart_switch": "Interruptor",
               "tuya": "i2GO · Tuya", "broadlink": "Broadlink IR/RF", "lg_tv": "TV LG webOS",
               "alexa": "Amazon Echo", "chromecast": "Chromecast"}

CARD_W, CARD_H, GAP = 268, 116, 14


def _font(size: float, bold: bool = False) -> QFont:
    f = QFont(UI_FONT)
    f.setPointSizeF(size)
    if bold:
        f.setWeight(QFont.Weight.DemiBold)
    return f


def _alpha(hex_or_col, a: float) -> QColor:
    c = QColor(hex_or_col)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


def _reachable(ip: str) -> bool:
    """One ICMP ping (devices on the LAN answer in a few ms). Windows' ping exits
    0 even for 'destination unreachable' from the router, so look for a TTL."""
    import subprocess
    import sys
    if not ip:
        return False
    win = sys.platform.startswith("win")
    cmd = ["ping", "-n", "1", "-w", "800", ip] if win else ["ping", "-c", "1", "-W", "1", ip]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=4,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return b"TTL=" in out.upper()
    except Exception:
        return False


# ── vector icons ─────────────────────────────────────────────────────────────

def paint_icon(p: QPainter, r: QRectF, key: str, col: QColor) -> None:
    """A line icon centred in r, drawn in col."""
    cx, cy = r.center().x(), r.center().y()
    s = min(r.width(), r.height()) / 2.0
    pen = QPen(col, max(1.6, s * 0.09), Qt.PenStyle.SolidLine,
               Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    def P(x, y):                      # unit coords (-1..1) → pixels
        return QPointF(cx + x * s, cy + y * s)

    if key == "home":
        path = QPainterPath(P(-0.75, -0.05))
        for pt in ((0, -0.78), (0.75, -0.05)):
            path.lineTo(P(*pt))
        p.drawPath(path)
        p.drawPolyline(QPolygonF([P(-0.58, -0.18), P(-0.58, 0.72), P(0.58, 0.72), P(0.58, -0.18)]))
        for k in (0.16, 0.30):        # Wi-Fi arcs inside the house
            rr = s * k
            p.drawArc(QRectF(cx - rr, cy + s * 0.42 - rr, 2 * rr, 2 * rr), 45 * 16, 90 * 16)
        p.drawPoint(P(0, 0.42))
    elif key == "alexa":
        p.drawEllipse(P(0, -0.05), s * 0.68, s * 0.68)
        p.drawLine(P(-0.28, 0.55), P(-0.52, 0.86))
        rr = s * 0.36
        p.drawArc(QRectF(cx - rr, cy - 0.05 * s - rr, 2 * rr, 2 * rr), 200 * 16, 140 * 16)
    elif key == "spotify":
        p.drawEllipse(P(0, 0), s * 0.8, s * 0.8)
        for y, w, k in ((-0.28, 0.5, 0.20), (0.02, 0.42, 0.16), (0.30, 0.34, 0.12)):
            path = QPainterPath(P(-w, y))
            path.quadTo(P(0, y - k), P(w, y + 0.06))
            p.drawPath(path)
    elif key == "phone":
        p.drawRoundedRect(QRectF(cx - s * 0.42, cy - s * 0.8, s * 0.84, s * 1.6), s * 0.14, s * 0.14)
        p.drawLine(P(-0.12, -0.62), P(0.12, -0.62))
        p.drawPoint(P(0, 0.6))
    elif key == "briefing":
        p.drawRoundedRect(QRectF(cx - s * 0.75, cy - s * 0.35, s * 1.15, s * 1.05), s * 0.08, s * 0.08)
        p.drawLine(P(-0.75, -0.08), P(0.40, -0.08))
        for x in (-0.45, -0.05):
            p.drawLine(P(x, -0.48), P(x, -0.25))
        p.drawEllipse(P(0.48, -0.42), s * 0.22, s * 0.22)          # the sun
        for i in range(8):
            a = i * math.tau / 8
            p.drawLine(P(0.48 + math.cos(a) * 0.32, -0.42 + math.sin(a) * 0.32),
                       P(0.48 + math.cos(a) * 0.42, -0.42 + math.sin(a) * 0.42))
    elif key == "voice":
        for i, h in enumerate((0.25, 0.55, 0.85, 0.55, 0.25)):
            x = -0.56 + i * 0.28
            p.drawLine(P(x, -h * 0.75), P(x, h * 0.75))
    elif key == "brain":
        nodes = [(math.cos(a) * 0.62, math.sin(a) * 0.62) for a in
                 [i * math.tau / 6 - math.pi / 2 for i in range(6)]]
        for x, y in nodes:
            p.drawLine(P(0, 0), P(x, y))
        p.drawPolygon(QPolygonF([P(x, y) for x, y in nodes]))
        p.setBrush(QBrush(col))
        for x, y in nodes + [(0, 0)]:
            p.drawEllipse(P(x, y), s * 0.09, s * 0.09)
        p.setBrush(Qt.BrushStyle.NoBrush)
    elif key == "remote":
        p.drawEllipse(P(0, 0), s * 0.78, s * 0.78)
        p.drawEllipse(P(0, 0), s * 0.34, s * 0.78)
        p.drawLine(P(-0.78, 0), P(0.78, 0))
        p.drawLine(P(-0.68, -0.38), P(0.68, -0.38))
        p.drawLine(P(-0.68, 0.38), P(0.68, 0.38))
    elif key == "cube":                 # an isometric block: the Anatomy look
        top = QPolygonF([P(0, -0.8), P(0.7, -0.42), P(0, -0.04), P(-0.7, -0.42)])
        p.drawPolygon(top)
        p.drawPolyline(QPolygonF([P(-0.7, -0.42), P(-0.7, 0.42), P(0, 0.8), P(0.7, 0.42), P(0.7, -0.42)]))
        p.drawLine(P(0, -0.04), P(0, 0.8))
        p.drawLine(P(-0.35, -0.23), P(-0.35, 0.6))
    elif key == "gauge":
        rr = s * 0.78
        p.drawArc(QRectF(cx - rr, cy - rr * 0.8, 2 * rr, 2 * rr), 200 * 16, -220 * 16)
        p.drawLine(P(0, 0.22), P(0.42, -0.25))
        p.setBrush(QBrush(col))
        p.drawEllipse(P(0, 0.22), s * 0.09, s * 0.09)
        p.setBrush(Qt.BrushStyle.NoBrush)
    elif key == "bulb":
        path = QPainterPath(P(-0.22, 0.42))
        path.cubicTo(P(-0.22, 0.12), P(-0.6, -0.05), P(-0.6, -0.32))
        path.cubicTo(P(-0.6, -0.95), P(0.6, -0.95), P(0.6, -0.32))
        path.cubicTo(P(0.6, -0.05), P(0.22, 0.12), P(0.22, 0.42))
        p.drawPath(path)
        p.drawLine(P(-0.22, 0.56), P(0.22, 0.56))
        p.drawLine(P(-0.14, 0.72), P(0.14, 0.72))
    elif key == "ir":
        p.drawRoundedRect(QRectF(cx - s * 0.32, cy - s * 0.35, s * 0.64, s * 1.15), s * 0.18, s * 0.18)
        p.drawEllipse(P(0, -0.12), s * 0.11, s * 0.11)
        for y in (0.22, 0.46):
            p.drawPoint(P(-0.12, y)); p.drawPoint(P(0.12, y))
        for k in (0.18, 0.34):
            rr = s * k
            p.drawArc(QRectF(cx - rr, cy - 0.5 * s - rr, 2 * rr, 2 * rr), 40 * 16, 100 * 16)
    elif key == "tv":
        p.drawRoundedRect(QRectF(cx - s * 0.8, cy - s * 0.55, s * 1.6, s * 1.0), s * 0.08, s * 0.08)
        p.drawLine(P(-0.3, 0.72), P(0.3, 0.72))
        p.drawLine(P(0, 0.45), P(0, 0.72))
    elif key == "plug":
        p.drawRoundedRect(QRectF(cx - s * 0.45, cy - s * 0.3, s * 0.9, s * 0.7), s * 0.14, s * 0.14)
        p.drawLine(P(-0.2, -0.3), P(-0.2, -0.7))
        p.drawLine(P(0.2, -0.3), P(0.2, -0.7))
        p.drawLine(P(0, 0.4), P(0, 0.8))
    elif key == "cast":
        p.drawRoundedRect(QRectF(cx - s * 0.75, cy - s * 0.55, s * 1.5, s * 1.05), s * 0.08, s * 0.08)
        for k in (0.22, 0.42):
            rr = s * k
            p.drawArc(QRectF(cx - 0.75 * s - rr, cy + 0.5 * s - rr, 2 * rr, 2 * rr), 0, 90 * 16)
    else:                              # anything else: a hexagon with a dot
        p.drawPolygon(QPolygonF([P(math.cos(a) * 0.7, math.sin(a) * 0.7)
                                 for a in [i * math.tau / 6 for i in range(6)]]))
        p.drawEllipse(P(0, 0), s * 0.12, s * 0.12)


def paint_badge(p: QPainter, r: QRectF, key: str, col: QColor, glow: float = 0.35) -> None:
    """The icon inside a glowing hexagon."""
    cx, cy = r.center().x(), r.center().y()
    s = min(r.width(), r.height()) / 2.0
    g = QRadialGradient(QPointF(cx, cy), s * 1.1)
    g.setColorAt(0.0, _alpha(col, glow))
    g.setColorAt(1.0, _alpha(col, 0.0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(QPointF(cx, cy), s * 1.1, s * 1.1)
    hexa = QPolygonF([QPointF(cx + math.cos(a) * s * 0.95, cy + math.sin(a) * s * 0.95)
                      for a in [i * math.tau / 6 + math.pi / 6 for i in range(6)]])
    p.setBrush(QBrush(_alpha(C.PANEL, 0.85)))
    p.setPen(QPen(_alpha(col, 0.9), 1.4))
    p.drawPolygon(hexa)
    p.setBrush(Qt.BrushStyle.NoBrush)
    k = s * 0.48
    paint_icon(p, QRectF(cx - k, cy - k, 2 * k, 2 * k), key, col)


def paint_brackets(p: QPainter, r: QRectF, col: QColor, L: float = 12.0, w: float = 1.6) -> None:
    p.setPen(QPen(col, w))
    x0, y0, x1, y1 = r.left(), r.top(), r.right(), r.bottom()
    for x, y, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        p.drawLine(QPointF(x, y), QPointF(x + sx * L, y))
        p.drawLine(QPointF(x, y), QPointF(x, y + sy * L))


# ── a card ───────────────────────────────────────────────────────────────────

class HubCard(QWidget):
    clicked = pyqtSignal(str)

    def __init__(self, key: str, title: str, sub: str, icon: str,
                 chip: str = "", chip_ok: bool | None = None, small: bool = False, parent=None):
        super().__init__(parent)
        self.key, self.title, self.sub, self.icon = key, title, sub, icon
        self.chip, self.chip_ok = chip, chip_ok
        self.selected = False
        self._hover = False
        self.setFixedSize(CARD_W, 86 if small else CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

    def enterEvent(self, _):
        self._hover = True; self.update()

    def leaveEvent(self, _):
        self._hover = False; self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.key)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        lit = self.selected or self._hover
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0.0, _alpha(C.PANEL2, 0.95))
        g.setColorAt(1.0, _alpha(C.BG, 0.92))
        p.setBrush(QBrush(g))
        border = qcol(C.PRI) if self.selected else (qcol(C.BORDER_B) if self._hover else qcol(C.BORDER))
        p.setPen(QPen(border, 1.2))
        p.drawRoundedRect(r, 6, 6)
        if lit:
            glow = QLinearGradient(r.topLeft(), r.bottomLeft())
            glow.setColorAt(0.0, _alpha(C.PRI, 0.14 if self.selected else 0.08))
            glow.setColorAt(1.0, _alpha(C.PRI, 0.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            p.drawRoundedRect(r, 6, 6)
        paint_brackets(p, r.adjusted(3, 3, -3, -3), _alpha(C.PRI, 0.9 if lit else 0.5), 9)

        h = r.height()
        badge = QRectF(r.left() + 12, r.top() + (h - 56) / 2, 56, 56) if h > 90 else \
            QRectF(r.left() + 12, r.top() + (h - 46) / 2, 46, 46)
        paint_badge(p, badge, self.icon, qcol(C.PRI if self.chip_ok is not False else C.TEXT_DIM),
                    0.45 if lit else 0.3)

        tx = badge.right() + 14
        tw = r.right() - tx - 10
        p.setPen(qcol(C.WHITE))
        p.setFont(_font(11.5, True))
        p.drawText(QRectF(tx, r.top() + 12, tw, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.title)
        p.setPen(qcol(C.TEXT_MED))
        p.setFont(_font(8.5))
        sub_h = h - 12 - 22 - (26 if self.chip else 8)
        p.drawText(QRectF(tx, r.top() + 34, tw, max(14, sub_h)),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                   self.sub)
        if self.chip:
            col = {True: qcol(C.GREEN), False: qcol(C.ACC), None: qcol(C.PRI)}[self.chip_ok]
            p.setFont(_font(7.5, True))
            fm = p.fontMetrics()
            cw = fm.horizontalAdvance(self.chip) + 18
            cr = QRectF(tx, r.bottom() - 24, cw, 16)
            p.setPen(QPen(_alpha(col, 0.8), 1))
            p.setBrush(QBrush(_alpha(col, 0.12)))
            p.drawRoundedRect(cr, 8, 8)
            p.setBrush(QBrush(col))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(cr.left() + 8, cr.center().y()), 2.6, 2.6)
            p.setPen(col)
            p.drawText(cr.adjusted(13, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, self.chip)
        p.end()


class _Group(QWidget):
    """A titled group whose cards flow left-to-right and wrap to new rows."""

    def __init__(self, title: str, icon: str, parent=None):
        super().__init__(parent)
        self.title, self.icon = title, icon
        self.cards: list[HubCard] = []
        self.subs: list[tuple[int, str]] = []      # (index of first card, sub-heading)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 44, 0, 8)
        self._lay.setSpacing(6)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(GAP)
        self._grid.setVerticalSpacing(GAP)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._lay.addWidget(self._grid_host)
        self._cols = 0

    def add(self, card: HubCard, sub_heading: str = "") -> None:
        if sub_heading:
            self.subs.append((len(self.cards), sub_heading))
        self.cards.append(card)

    def relayout(self, width: int) -> None:
        cols = max(1, (width + GAP) // (CARD_W + GAP))
        cw = max(CARD_W, (width - (cols - 1) * GAP) // cols)   # cards share the row
        for c in self.cards:
            c.setFixedWidth(cw)
        if cols == self._cols:
            return
        self._cols = cols
        while self._grid.count():
            it = self._grid.takeAt(0)
            w = it.widget()
            if w is not None and not isinstance(w, HubCard):
                w.deleteLater()
        row = col = 0
        subs = dict(self.subs)
        for i, card in enumerate(self.cards):
            if i in subs:
                if col:
                    row += 1; col = 0
                lbl = QLabel(subs[i])
                lbl.setFont(_font(8, True))
                lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; "
                                  f"letter-spacing: 2px; padding-top: 6px;")
                self._grid.addWidget(lbl, row, 0, 1, cols)
                row += 1
            self._grid.addWidget(card, row, col)
            col += 1
            if col >= cols:
                row += 1; col = 0

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_badge(p, QRectF(0, 4, 30, 30), self.icon, qcol(C.PRI), 0.35)
        p.setPen(qcol(C.WHITE))
        f = _font(13, True)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        p.setFont(f)
        p.drawText(QRectF(42, 4, self.width() - 42, 30), Qt.AlignmentFlag.AlignVCenter, self.title)
        tw = p.fontMetrics().horizontalAdvance(self.title)
        p.setPen(QPen(_alpha(C.BORDER_B, 0.8), 1))
        p.drawLine(QPointF(42 + tw + 16, 19), QPointF(self.width() - 4, 19))
        p.setPen(qcol(C.TEXT_DIM))
        p.setFont(_font(8))
        p.drawText(QRectF(42 + tw + 16, 22, self.width() - tw - 64, 16), Qt.AlignmentFlag.AlignRight,
                   f"{len(self.cards)} MÓDULOS")
        p.end()


# ── the hub ──────────────────────────────────────────────────────────────────

class PluginHubOverlay(_ui.PluginSettingsOverlay):
    """Full-panel module hub. Inherits the settings overlay only for its form
    machinery (_build_section, _save_ns, _run_action…); the look is its own."""

    DETAIL_W = 440

    def __init__(self, sections: list[dict], plugins: list[dict], parent=None):
        QWidget.__init__(self, parent)          # not the overlay's own layout
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._sections = sections or []
        self._widgets, self._types, self._status_labels = {}, {}, {}
        self._test_done.connect(self._on_test_done)
        self._fs = (f"QLineEdit {{ background: {C.BG}; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 4px; padding: 4px 8px; }}"
                    f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")
        self._plugins = {p["name"]: p for p in (plugins or [])}
        self._sec_by_plugin = {s.get("plugin"): s for s in self._sections}
        self._cards: dict[str, HubCard] = {}
        self._devices = self._load_devices()
        self._dev_status: dict[str, bool | None] = {d["_key"]: None for d in self._devices}
        self._detail_dev: str | None = None
        self._probe_devices()

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(14)

        # header
        head = QHBoxLayout()
        title = QLabel("CENTRAL DE MÓDULOS")
        f = _font(19, True); f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 5)
        title.setFont(f)
        title.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        head.addWidget(title)
        n_on = sum(1 for p in self._plugins.values() if p.get("enabled"))
        sub = QLabel(f"   {n_on} ATIVOS  ·  {len(self._devices)} APARELHOS NA REDE")
        sub.setFont(_font(9, True))
        sub.setStyleSheet(f"color: {C.PRI}; background: transparent; letter-spacing: 2px;")
        head.addWidget(sub)
        head.addStretch(1)
        close = QPushButton("✕")
        close.setFixedSize(34, 34)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setFont(_font(12, True))
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER_A}; border-radius: 17px; }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI}; background: {C.PRI_GHO}; }}""")
        close.clicked.connect(self.hide)
        head.addWidget(close)
        root.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(20)

        # groups, stacked vertically; cards flow horizontally inside each
        self._groups_host = QWidget()
        self._groups_host.setStyleSheet("background: transparent;")
        gl = QVBoxLayout(self._groups_host)
        gl.setContentsMargins(4, 0, 12, 0)
        gl.setSpacing(18)
        self._groups = [self._build_home(), self._build_personal()]
        for g in self._groups:
            gl.addWidget(g)
        gl.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(self._scroll_css())
        scroll.setWidget(self._groups_host)
        self._groups_scroll = scroll
        body.addWidget(scroll, 1)

        # detail panel
        self._detail = QFrame()
        self._detail.setFixedWidth(self.DETAIL_W)
        self._detail.setObjectName("hubDetail")
        self._detail.setStyleSheet(f"""
            QFrame#hubDetail {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(12, 37, 82, 200), stop:1 rgba(3, 13, 36, 220));
                border: 1px solid {C.BORDER_B}; border-radius: 8px; }}""")
        self._detail_lay = QVBoxLayout(self._detail)
        self._detail_lay.setContentsMargins(20, 18, 20, 18)
        self._detail_lay.setSpacing(8)
        body.addWidget(self._detail)
        root.addLayout(body, 1)
        # closed until a card is clicked: the cards get the whole width
        self._detail.hide()

    # ── data ────────────────────────────────────────────────────────────────

    @staticmethod
    def _load_devices() -> list[dict]:
        try:
            data = json.loads((BASE / "memory" / "smart_home.json").read_text(encoding="utf-8"))
        except Exception:
            return []
        out = []
        for key, d in (data.get("devices") or {}).items():
            if d.get("kind") in DEVICE_ICON:
                out.append({**d, "_key": key})
        order = list(DEVICE_ICON)
        return sorted(out, key=lambda d: (order.index(d["kind"]), str(d.get("alias") or d.get("name") or "")))

    def _status(self, name: str) -> tuple[str, bool | None]:
        p = self._plugins.get(name, {})
        if not p.get("valid", True):
            return "COM ERRO", False
        if not p.get("enabled", True):
            return "DESATIVADO", False
        mem = BASE / "memory"
        vals = (self._sec_by_plugin.get(name) or {}).get("values") or {}
        if name == "alexa":
            return ("CONECTADA", True) if (mem / "alexa" / "auth.json").exists() else ("CONECTAR", False)
        if name == "spotify":
            return ("CONECTADO", True) if (mem / "spotify_token.json").exists() else ("CONECTAR", False)
        if name == "smart_home":
            parts = []
            if (mem / "ewelink_token.json").exists():
                parts.append("eWeLink")
            if vals.get("tuya_api_key"):
                parts.append("Tuya")
            return ((" · ".join(parts) + " ✓") if parts else f"{len(self._devices)} APARELHOS",
                    True if parts else None)
        if name == "android_phone":
            try:
                from dashboard.server import DashboardServer
                d = getattr(DashboardServer, "instance", None)
                if d and d.device_connected():
                    return "PAREADO", True
            except Exception:
                pass
            return "OFFLINE", None
        if name == "voice_id":
            if not (mem / "voiceprint.npy").exists():
                return "CADASTRAR VOZ", False
            mode = str(vals.get("mode") or "off")
            return {"off": ("VOZ CADASTRADA", None), "sensitive": ("MODO SENSÍVEL", True),
                    "strict": ("MODO ESTRITO", True)}.get(mode, (mode.upper(), None))
        if name == "hwinfo":
            try:
                from core import hwinfo
                src = hwinfo.source()
                if src == "shared memory":
                    return f"{len(hwinfo.readings())} LEITURAS", True
                if src == "gadget":
                    return "SÓ GADGET", None
                return ("LIGAR SHARED MEMORY", False) if hwinfo.running() else ("HWINFO FECHADO", False)
            except Exception:
                return "HWINFO", None
        if name == "pc_sensors":
            try:
                from core import sensors
                return ("SENSORES ATIVOS", True) if sensors.lhm_running() else ("SÓ GPU E USO", None)
            except Exception:
                return "SÓ GPU E USO", None
        if name == "deep_think":
            if not vals.get("openai_api_key"):
                return "SÓ GEMINI", None
            prov = str(vals.get("provider") or "auto")
            return ("GEMINI PRIMEIRO", True) if prov == "gemini" else ("GPT + GEMINI", True)
        if name == "daily_briefing":
            city = str(vals.get("city") or "").split(",")[0].strip()
            return (city.upper(), True) if city else ("DEFINIR CIDADE", False)
        return "ATIVO", True

    def _card_for(self, name: str) -> HubCard:
        title, sub, icon, _grp = META.get(name, (name.replace("_", " ").title(),
                                                self._short(name), "hex", PERSONAL))
        chip, ok = self._status(name)
        card = HubCard(name, title, sub, icon, chip, ok)
        card.clicked.connect(self._select)
        self._cards[name] = card
        return card

    def _short(self, name: str) -> str:
        d = str(self._plugins.get(name, {}).get("description") or "")
        return (d.split(". ")[0][:90] + "…") if len(d) > 90 else d

    def _probe_devices(self) -> None:
        """Ping every device in the background; the cards show VERIFICANDO… until
        the answer is in (a QTimer brings the results to the Qt thread)."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        def run():
            with ThreadPoolExecutor(max_workers=8) as pool:
                keys = [d["_key"] for d in self._devices]
                for k, ok in zip(keys, pool.map(lambda d: _reachable(str(d.get("ip") or "")),
                                                 self._devices)):
                    self._dev_status[k] = ok
        threading.Thread(target=run, daemon=True, name="hub-ping").start()
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_dev_chips)
        self._status_timer.start(400)

    @staticmethod
    def _dev_chip(st: bool | None) -> tuple[str, bool | None]:
        return {True: ("CONECTADO", True), False: ("OFFLINE", False)}.get(st, ("VERIFICANDO…", None))

    def _refresh_dev_chips(self) -> None:
        pending = False
        for k, st in list(self._dev_status.items()):
            card = self._cards.get("dev:" + k)
            if card is not None:
                chip = self._dev_chip(st)
                if (card.chip, card.chip_ok) != chip:
                    card.chip, card.chip_ok = chip
                    card.update()
                    lbl = getattr(self, "_detail_chip", None)
                    if self._detail_dev == k and lbl is not None:
                        lbl.setText(f"●  {chip[0]}")
                        lbl.setStyleSheet(f"color: {({True: C.GREEN, False: C.ACC, None: C.PRI})[chip[1]]}; "
                                          f"background: transparent; padding-bottom: 6px;")
            pending = pending or st is None
        if not pending:
            self._status_timer.stop()

    def _build_home(self) -> _Group:
        g = _Group("CASA INTELIGENTE", "home")
        for name in ("smart_home", "alexa"):
            if name in self._plugins:
                g.add(self._card_for(name))
        first = True
        for d in self._devices:
            name = str(d.get("alias") or d.get("name") or "Aparelho").strip()
            ck = "dev:" + d["_key"]
            sub = DEVICE_KIND.get(d["kind"], d["kind"]) + (f"  ·  {d['room']}" if d.get("room") else "")
            chip, ok = self._dev_chip(self._dev_status.get(d["_key"]))
            card = HubCard(ck, name[:24], sub, DEVICE_ICON[d["kind"]], chip, ok, small=True)
            card.clicked.connect(self._select)
            self._cards[ck] = card
            g.add(card, "DISPOSITIVOS NA REDE" if first else "")
            first = False
        return g

    def _build_personal(self) -> _Group:
        g = _Group("RECURSOS PESSOAIS", "brain")
        order = ["daily_briefing", "send_email", "software", "spotify", "android_phone", "voice_id", "deep_think",
                 "anatomy", "hwinfo", "pc_sensors", "hud_display", "remote_access"]
        rest = [n for n in self._plugins if n not in order and META.get(n, ("", "", "", PERSONAL))[3] != HOME]
        for name in [n for n in order if n in self._plugins] + sorted(rest):
            g.add(self._card_for(name))
        return g

    # ── selection / detail ──────────────────────────────────────────────────

    def _clear_detail(self) -> None:
        self._clear_layout(self._detail_lay)

    def _clear_layout(self, lay) -> None:
        # Detach at once (hide + unparent) as well as deleteLater: the previous
        # card's form must not linger under the new one until the event loop
        # gets round to deleting it.
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
            elif it.layout() is not None:
                self._clear_layout(it.layout())

    def _select(self, key: str) -> None:
        for k, c in self._cards.items():
            c.selected = (k == key)
            c.update()
        self._clear_detail()
        self._widgets, self._types, self._status_labels = {}, {}, {}
        self._detail_dev = key[4:] if key.startswith("dev:") else None
        self._detail_chip = None
        bar = QHBoxLayout()
        bar.addStretch(1)
        x = QPushButton("✕")
        x.setFixedSize(26, 26)
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.setToolTip("Fechar")
        x.setStyleSheet(f"QPushButton {{ color: {C.TEXT_DIM}; background: transparent; border: none; "
                        f"font-size: 14px; }} QPushButton:hover {{ color: {C.PRI}; }}")
        x.clicked.connect(self._close_detail)
        bar.addWidget(x)
        self._detail_lay.addLayout(bar)
        if key.startswith("dev:"):
            self._detail_device(key[4:])
        else:
            self._detail_plugin(key)
        if not self._detail.isVisible():
            self._detail.show()
            self._relayout()

    def _detail_header(self, title: str, sub: str, icon: str) -> None:
        top = _DetailHead(title, sub, icon)
        self._detail_lay.addWidget(top)

    def _detail_plugin(self, name: str) -> None:
        p = self._plugins.get(name, {})
        title, sub, icon, _ = META.get(name, (name, self._short(name), "hex", PERSONAL))
        self._detail_header(title, sub, icon)

        row = QHBoxLayout()
        st, ok = self._status(name)
        chip = QLabel(f"●  {st}")
        chip.setFont(_font(8.5, True))
        colr = {True: C.GREEN, False: C.ACC, None: C.PRI}[ok]
        chip.setStyleSheet(f"color: {colr}; background: transparent;")
        row.addWidget(chip)
        row.addStretch(1)
        if p.get("valid", True):
            tog = QPushButton()
            tog.setFixedSize(92, 26)
            tog.setCursor(Qt.CursorShape.PointingHandCursor)
            tog.setFont(_font(8, True))
            self._style_enable(tog, bool(p.get("enabled", True)))
            tog.clicked.connect(lambda _=False, n=name, b=tog: self._toggle_enabled(n, b))
            row.addWidget(tog)
        self._detail_lay.addLayout(row)

        sec = self._sec_by_plugin.get(name)
        if not sec:
            msg = QLabel("Este módulo não precisa de configuração — é só falar com o assistente.")
            msg.setWordWrap(True)
            msg.setFont(_font(9.5))
            msg.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; padding-top: 10px;")
            self._detail_lay.addWidget(msg)
            self._detail_lay.addStretch(1)
            return

        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        form = QVBoxLayout(holder)
        form.setContentsMargins(0, 0, 6, 0)
        form.setSpacing(6)
        self._build_section(form, {**sec, "title": "Configuração"})
        form.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(self._scroll_css())
        scroll.setWidget(holder)
        self._detail_lay.addWidget(scroll, 1)

        save = QPushButton("SALVAR")
        save.setFixedHeight(36)
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.setFont(_font(9.5, True))
        save.setStyleSheet(f"""
            QPushButton {{ background: {C.PRI_GHO}; color: {C.WHITE};
                border: 1px solid {C.PRI}; border-radius: 4px; letter-spacing: 3px; }}
            QPushButton:hover {{ background: {C.PRI_DIM}; }}""")
        ns = sec.get("namespace") or name
        save.clicked.connect(lambda _=False, n=ns: self._save_one(n))
        self._detail_lay.addWidget(save)

    def _detail_device(self, key: str) -> None:
        d = next((x for x in self._devices if x["_key"] == key), None)
        if not d:
            return
        name = str(d.get("alias") or d.get("name") or "Aparelho").strip()
        self._detail_header(name, DEVICE_KIND.get(d["kind"], d["kind"]), DEVICE_ICON[d["kind"]])

        st, ok = self._dev_chip(self._dev_status.get(key))
        chip = QLabel(f"●  {st}")
        chip.setFont(_font(8.5, True))
        chip.setStyleSheet(f"color: {({True: C.GREEN, False: C.ACC, None: C.PRI})[ok]}; "
                           f"background: transparent; padding-bottom: 6px;")
        self._detail_lay.addWidget(chip)
        self._detail_chip = chip

        def row(k, v, into):
            kl = QLabel(k)
            kl.setFont(_font(7.5, True))
            kl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; letter-spacing: 2px;")
            vl = QLabel(str(v))
            vl.setWordWrap(True)
            vl.setFont(_font(10.5))
            vl.setStyleSheet(f"color: {C.TEXT}; background: transparent; padding-bottom: 6px;")
            into.addWidget(kl)
            into.addWidget(vl)

        # the everyday view: what it is, where, what it can do
        codes = list((d.get("codes") or {}).keys())
        for k, v in (("TIPO", DEVICE_KIND.get(d["kind"], d["kind"])), ("SALA", d.get("room")),
                     ("BOTÕES APRENDIDOS", ", ".join(codes) if codes else None)):
            if v:
                row(k, v, self._detail_lay)

        # the technical view, only when asked for
        tech = [("IP", d.get("ip")), ("MAC", d.get("mac")), ("FABRICANTE", d.get("vendor")),
                ("MODELO", d.get("model") or (d.get("broadlink") or {}).get("type")),
                ("FREQUÊNCIA RF", f"{(d.get('broadlink') or {}).get('rf_mhz')} MHz"
                 if (d.get("broadlink") or {}).get("rf_mhz") else None)]
        tech = [(k, v) for k, v in tech if v]
        if tech:
            box = QWidget()
            box.setStyleSheet(f"background: transparent;")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(10, 8, 10, 4)
            bl.setSpacing(2)
            for k, v in tech:
                row(k, v, bl)
            box.setVisible(False)
            btn = QPushButton("DETALHES  ▾")
            btn.setFixedHeight(30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFont(_font(8.5, True))
            btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.PRI};
                    border: 1px solid {C.BORDER_A}; border-radius: 4px; letter-spacing: 2px; }}
                QPushButton:hover {{ border-color: {C.PRI}; background: {C.PRI_GHO}; }}""")

            def toggle(_=False, b=btn, x=box):
                x.setVisible(not x.isVisible())
                b.setText("OCULTAR DETALHES  ▴" if x.isVisible() else "DETALHES  ▾")
            btn.clicked.connect(toggle)
            self._detail_lay.addSpacing(6)
            self._detail_lay.addWidget(btn)
            self._detail_lay.addWidget(box)
        tip = QLabel("Controle por voz: \"liga a " + name.lower() + "\", \"desliga…\".\n"
                     "Renomeie pedindo ao assistente: \"chama esse aparelho de …\".")
        tip.setWordWrap(True)
        tip.setFont(_font(8.5))
        tip.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; padding-top: 8px;")
        self._detail_lay.addWidget(tip)
        self._detail_lay.addStretch(1)

    # ── actions ─────────────────────────────────────────────────────────────

    def _save_one(self, ns: str) -> None:
        self._save_ns(ns)
        lbl = self._status_labels.get(ns)
        if lbl:
            lbl.setText("Salvo ✓")
            lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _style_enable(self, b: QPushButton, on: bool) -> None:
        b.setText("ATIVADO" if on else "DESATIVADO")
        col = C.GREEN if on else C.TEXT_DIM
        b.setStyleSheet(f"""
            QPushButton {{ background: {'rgba(61,255,176,0.10)' if on else 'transparent'};
                color: {col}; border: 1px solid {col}; border-radius: 13px; }}
            QPushButton:hover {{ border-color: {C.WHITE}; }}""")

    def _toggle_enabled(self, name: str, b: QPushButton) -> None:
        from memory.config_manager import get_plugin_enabled, save_plugin_enabled
        on = not get_plugin_enabled(name)
        save_plugin_enabled(name, on)
        if name in self._plugins:
            self._plugins[name]["enabled"] = on
        self._style_enable(b, on)
        card = self._cards.get(name)
        if card:
            card.chip, card.chip_ok = self._status(name)
            card.update()

    # ── look ────────────────────────────────────────────────────────────────

    @staticmethod
    def _scroll_css() -> str:
        return (f"QScrollArea {{ background: transparent; }}"
                f"QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}"
                f"QScrollBar::handle:vertical {{ background: {C.BORDER_A}; border-radius: 3px; min-height: 30px; }}"
                f"QScrollBar::handle:vertical:hover {{ background: {C.PRI_DIM}; }}"
                f"QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _relayout(self) -> None:
        detail = self.DETAIL_W + 20 if getattr(self, "_detail", None) is not None and self._detail.isVisible() else 0
        w = self.width() - detail - 28 * 2 - 28
        for g in getattr(self, "_groups", []):
            g.relayout(max(CARD_W, w))

    def _close_detail(self) -> None:
        for c in self._cards.values():
            c.selected = False
            c.update()
        self._detail.hide()
        self._relayout()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0.0, QColor(6, 22, 54, 246))
        g.setColorAt(1.0, QColor(2, 9, 26, 250))
        p.setBrush(QBrush(g))
        p.setPen(QPen(qcol(C.BORDER_B), 1.2))
        p.drawRoundedRect(r, 10, 10)
        # top light
        tl = QRadialGradient(QPointF(r.center().x(), r.top()), r.width() * 0.45)
        tl.setColorAt(0.0, _alpha(C.PRI, 0.16))
        tl.setColorAt(1.0, _alpha(C.PRI, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(tl))
        p.drawRoundedRect(r, 10, 10)
        paint_brackets(p, r.adjusted(6, 6, -6, -6), qcol(C.PRI), 22, 2.0)
        # header rule
        p.setPen(QPen(_alpha(C.BORDER_B, 0.9), 1))
        p.drawLine(QPointF(28, 70), QPointF(self.width() - 28, 70))
        p.setPen(QPen(qcol(C.PRI), 2))
        p.drawLine(QPointF(28, 70), QPointF(220, 70))
        p.end()


class _DetailHead(QWidget):
    def __init__(self, title: str, sub: str, icon: str, parent=None):
        super().__init__(parent)
        self.title, self.sub, self.icon = title, sub, icon
        self.setFixedHeight(96)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_badge(p, QRectF(0, 8, 76, 76), self.icon, qcol(C.PRI), 0.5)
        p.setPen(qcol(C.WHITE))
        p.setFont(_font(15, True))
        p.drawText(QRectF(92, 12, self.width() - 92, 28), Qt.AlignmentFlag.AlignVCenter, self.title)
        p.setPen(qcol(C.TEXT_MED))
        p.setFont(_font(9))
        p.drawText(QRectF(92, 42, self.width() - 92, 46),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, self.sub)
        p.setPen(QPen(_alpha(C.BORDER_B, 0.8), 1))
        p.drawLine(QPointF(0, self.height() - 1), QPointF(self.width(), self.height() - 1))
        p.end()
