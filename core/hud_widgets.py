"""
core/hud_widgets.py — indicators the assistant draws on the HUD on request.

The assistant describes a widget (plugins/hud_display.py turns a tool call into
a spec) and this layer draws it on the HUD canvas, beside the centrepiece:

  gauge     a dial: value between min and max           ("internet speed")
  number    a big readout with unit and trend            ("bitcoin price")
  bar       one or more horizontal bars                  ("disk usage per drive")
  line      a line chart of values / live history        ("CPU over the last minute")
  columns   vertical bars with labels                    ("rain this week")
  donut     shares of a whole with a legend              ("time per project")
  list      lines of text                                ("today's agenda")
  table     label → value rows                           ("server status")
  text      a short paragraph                            ("a note")
  speedtest download dial + upload and ping, live        ("test my internet")

Pure QPainter, no Qt widgets: it paints into the HUD's own painter, so it works
on both the CPU and the OpenGL canvas. Values ease toward their targets and
cards fade in and out. Colours come in per paint call (the theme), so this
module needs nothing from ui.py.
"""

from __future__ import annotations

import math
import re
import time
from urllib.parse import urlparse

from core.hud_holo import HoloSprite, render_template, shape_svg

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter,
                         QPainterPath, QPen, QRadialGradient)

NAMED = {"blue": "#4db8ff", "cyan": "#38e8ff", "green": "#3dffb0", "yellow": "#ffd24a",
         "orange": "#ff8a2a", "red": "#ff4d6a", "purple": "#b28cff", "pink": "#ff7ad9",
         "white": "#eef7ff"}
PALETTE = ["#4db8ff", "#3dffb0", "#ffd24a", "#ff8a2a", "#b28cff", "#ff7ad9", "#38e8ff", "#ff4d6a"]
TYPES = ("gauge", "number", "bar", "line", "columns", "donut", "list", "table", "text", "speedtest",
         "holo", "photo")
PHOTO_MAX_H = {"s": 160, "small": 160, "m": 240, "medium": 240, "l": 320, "large": 320, "xl": 420}
HOLO_SIZES = {"s": 120, "small": 120, "m": 170, "medium": 170, "l": 230, "large": 230,
              "xl": 290}

CARD_MIN_W, CARD_MAX_W = 220, 320
HOLO_MAX_W = 380            # holograms in a side column may be wider than cards
MAX_ROWS = 12


def _f(size, bold: bool = False) -> QFont:
    f = QFont("Bahnschrift")
    f.setPointSizeF(float(size))
    if bold:
        f.setWeight(QFont.Weight.DemiBold)
    return f


def _num(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _col(c: QColor, a: float) -> QColor:
    q = QColor(c)
    q.setAlphaF(max(0.0, min(1.0, a)) * q.alphaF())
    return q


def _fmt(v, decimals=None) -> str:
    if v is None:
        return "—"
    if isinstance(v, str):
        return v
    if decimals is None:
        decimals = 0 if abs(v) >= 100 or float(v).is_integer() else (1 if abs(v) >= 10 else 2)
    s = f"{v:,.{int(decimals)}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")   # pt-BR: 1.234,5


_URL_RE = re.compile(r"https?://[^\s<>\"'\])]+")


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return url[:40]


def _split_url(text: str) -> tuple[str, str]:
    """('text without the link', 'link') — the link is '' when there is none."""
    m = _URL_RE.search(text or "")
    if not m:
        return text, ""
    rest = (text[:m.start()] + text[m.end():]).strip(" -—:|·")
    return rest, m.group(0).rstrip(".,;")


class HudWidget:
    def __init__(self, wid: str, spec: dict):
        self.id = wid
        self.spec: dict = {}
        self.disp: list[float] = []        # eased values being shown
        self.alpha = 0.0
        self.dying = False
        self.born = time.time()
        self.rect = QRectF()
        self.close_rect = QRectF()
        self.links: list[tuple[QRectF, str]] = []   # clickable areas → URL, set while painting
        self.update(spec)

    def card_url(self) -> str:
        """The link the whole card opens: 'url'/'link', or a value that is a URL."""
        for k in ("url", "link"):
            u = str(self.spec.get(k) or "").strip()
            if u.startswith(("http://", "https://")):
                return u
        v = str(self.spec.get("value") or "").strip()
        return v if v.startswith(("http://", "https://")) and " " not in v else ""

    def update(self, spec: dict) -> None:
        self.spec.update({k: v for k, v in spec.items() if v is not None})
        if "ttl" in spec and spec["ttl"]:
            self.born = time.time()

    @property
    def type(self) -> str:
        t = str(self.spec.get("type") or "number").lower()
        return t if t in TYPES else "number"

    def targets(self) -> list[float]:
        s = self.spec
        if self.type == "speedtest":
            return [_num(s.get("down"), 0.0) or 0.0, _num(s.get("up"), 0.0) or 0.0,
                    _num(s.get("ping"), 0.0) or 0.0]
        vals = s.get("values")
        if isinstance(vals, (list, tuple)) and vals and self.type not in ("number", "gauge"):
            return [(_num(v, 0.0) or 0.0) for v in vals][:120]
        v = _num(s.get("value"))
        return [v] if v is not None else []

    def step(self, dt: float) -> None:
        self.alpha += ((0.0 if self.dying else 1.0) - self.alpha) * min(1.0, dt * 6.0)
        tg = self.targets()
        if len(self.disp) != len(tg):
            # a new series (or a new length) starts where it is, not from zero
            self.disp = [tg[i] if i >= len(self.disp) else self.disp[i] for i in range(len(tg))]
        k = min(1.0, dt * 5.0)
        self.disp = [d + (t - d) * k for d, t in zip(self.disp, tg)]

    def expired(self) -> bool:
        ttl = _num(self.spec.get("ttl"))
        return bool(ttl and time.time() - self.born > ttl)

    def height(self, w: float) -> float:
        return self._height(w) + (24 if self.type != "holo" and self.card_url() else 0)

    def _height(self, w: float) -> float:
        t, s = self.type, self.spec
        n_items = len(s.get("items") or s.get("labels") or [])
        if t == "gauge":
            return 178
        if t == "number":
            return 112
        if t == "bar":
            n = max(1, len(self.targets()))
            return 50 + min(n, MAX_ROWS) * 30
        if t in ("line", "columns"):
            return 168
        if t == "donut":
            return 60 + max(110, min(n_items, 8) * 20)
        if t in ("list", "table"):
            return 50 + min(max(1, n_items), MAX_ROWS) * 22
        if t == "text":
            fm = QFontMetrics(_f(9))
            lines = fm.boundingRect(0, 0, int(w - 28), 2000, int(Qt.TextFlag.TextWordWrap),
                                    str(s.get("text") or "")).height()
            return min(260, 52 + lines)
        if t == "speedtest":
            return 214
        if t == "holo":
            return min(w - 16, self.holo_px()) + 84
        if t == "photo":
            img = self.photo()
            iw = w - 28
            ih = iw * img.height() / max(1, img.width()) if img is not None and not img.isNull() else 120
            cap = 22 if s.get("text") or s.get("subtitle") else 0
            return 50 + min(ih, PHOTO_MAX_H.get(str(s.get("size") or "l").lower(), 320)) + cap
        return 120

    def photo(self):
        """The picture of a 'photo' card, loaded once and kept at screen size."""
        from PyQt6.QtGui import QImage
        path = str(self.spec.get("image") or "")
        if getattr(self, "_photo_src", None) != path:
            img = QImage(path)
            if not img.isNull() and max(img.width(), img.height()) > 1200:
                img = img.scaled(1200, 1200, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            self._photo, self._photo_src = img, path
        return self._photo

    def holo_px(self) -> int:
        return HOLO_SIZES.get(str(self.spec.get("size") or "m").lower(), 170)


class WidgetLayer:
    """The set of widgets on the HUD, their layout and their painting."""

    def __init__(self):
        self.items: dict[str, HudWidget] = {}
        self._n = 0

    # ── model ────────────────────────────────────────────────────────────────

    def apply(self, op: str, payload: dict) -> None:
        payload = dict(payload or {})
        wid = str(payload.pop("id", "") or "")
        if op == "show":
            if not wid:
                self._n += 1
                wid = f"w{self._n}"
            if wid in self.items and not self.items[wid].dying:
                self.items[wid].update(payload)
            else:
                self.items[wid] = HudWidget(wid, payload)
        elif op == "update":
            w = self.items.get(wid)
            if w is not None and not w.dying:
                w.update(payload)
            elif wid:
                self.items[wid] = HudWidget(wid, payload)
        elif op == "remove":
            w = self.items.get(wid)
            if w is not None:
                w.dying = True
        elif op == "clear":
            for w in self.items.values():
                w.dying = True

    def ids(self) -> list[str]:
        return [k for k, w in self.items.items() if not w.dying]

    def stage_item(self) -> HudWidget | None:
        """The hologram on the centre stage: the newest live one asked for it."""
        for w in reversed(list(self.items.values())):
            if w.type == "holo" and w.spec.get("stage") and not w.dying:
                return w
        return None

    def has_stage(self) -> bool:
        return self.stage_item() is not None

    def active(self) -> bool:
        return bool(self.items)

    def step(self, dt: float) -> None:
        dead = []
        for k, w in self.items.items():
            if w.expired():
                w.dying = True
            w.step(dt)
            if w.dying and w.alpha < 0.03:
                dead.append(k)
        for k in dead:
            self.items.pop(k, None)

    def hit_link(self, x: float, y: float) -> str | None:
        """The URL under (x, y): a link row first, then a linked card."""
        pt = QPointF(x, y)
        for w in reversed(list(self.items.values())):
            if w.dying or w.alpha < 0.3:
                continue
            for rect, url in w.links:
                if rect.contains(pt):
                    return url
        return None

    def interactive_at(self, x: float, y: float) -> bool:
        """Is there something to click at (x, y) — a link or a close box?"""
        if self.hit_link(x, y):
            return True
        pt = QPointF(x, y)
        return any(not w.dying and w.close_rect.adjusted(-4, -4, 4, 4).contains(pt)
                   for w in self.items.values())

    def hit_close(self, x: float, y: float) -> str | None:
        for k, w in self.items.items():
            if w.close_rect.adjusted(-4, -4, 4, 4).contains(QPointF(x, y)):
                w.dying = True
                return k
        return None

    # ── layout + paint ───────────────────────────────────────────────────────

    def paint(self, p: QPainter, W: float, H: float, free_left: float, free_right: float,
              top_right: float, theme: dict, x_left: float = 24.0, x_right: float | None = None,
              top: float = 24.0, bottom: float | None = None, balance: bool = False) -> None:
        """free_left / free_right: the width beside the centrepiece; top_right:
        where the right column may start (below the system block). x_left /
        x_right / top / bottom bound the columns (default: the window's edges)."""
        if not self.items:
            return
        stage = self.stage_item()
        if stage is not None:
            self._paint_stage(p, stage, W, H, theme)
        gap = 12.0
        bottom = H - 24.0 if bottom is None else bottom
        x_right = W - 24.0 if x_right is None else x_right
        yl, yr = top, max(top, top_right)
        for w in list(self.items.values()):
            if w is stage:
                continue
            wide = HOLO_MAX_W if w.type in ("holo", "photo") else CARD_MAX_W
            cw_l = max(CARD_MIN_W, min(wide, free_left - 36))
            cw_r = max(CARD_MIN_W, min(wide, free_right - 36))
            side = str(w.spec.get("side") or "").lower()
            hl, hr = w.height(cw_l), w.height(cw_r)
            fits_l = yl + hl <= bottom
            fits_r = yr + hr <= bottom
            # the side asked for, else the left column, else the right, else
            # whichever column is shorter (it will run off the bottom)
            if side == "right" and fits_r:
                use_right = True
            elif side == "left" and fits_l:
                use_right = False
            elif balance:                       # both sides of the core, shorter column first
                use_right = yr < yl
            elif fits_l:
                use_right = False
            elif fits_r:
                use_right = True
            else:
                use_right = yr <= yl
            if use_right:
                rect = QRectF(x_right - cw_r, yr, cw_r, hr)
                yr += hr + gap
            else:
                rect = QRectF(x_left, yl, cw_l, hl)
                yl += hl + gap
            w.rect = rect
            p.save()
            p.setOpacity(max(0.0, min(1.0, w.alpha)))
            try:
                if w.type == "holo":
                    self._paint_holo(p, w, rect, theme)
                else:
                    self._paint_card(p, w, rect, theme)
            except Exception as e:      # one bad spec must never take the HUD down
                p.setPen(QColor("#ff4d6a"))
                p.drawText(rect.adjusted(12, 30, -12, -8), Qt.TextFlag.TextWordWrap,
                           f"indicador inválido: {e}")
            p.restore()

    def _paint_stage(self, p: QPainter, w: HudWidget, W: float, H: float, th: dict) -> None:
        """The big hologram in the middle of the HUD, over a dark well that
        quiets the centrepiece (shrunk and dimmed behind it)."""
        overlay = bool(th.get("overlay"))
        S = max(160.0, min(W * th.get("stage_k", 0.48), H * 0.66))
        cx, cy = W / 2.0, H * 0.47
        p.save()
        p.setOpacity(max(0.0, min(1.0, w.alpha)))
        if not overlay:          # over the user's screen nothing but the hologram is drawn
            well = QRadialGradient(QPointF(cx, cy), S * 0.75)
            well.setColorAt(0.0, _col(th["bg"], 0.92))
            well.setColorAt(0.7, _col(th["bg"], 0.75))
            well.setColorAt(1.0, _col(th["bg"], 0.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(well))
            p.drawEllipse(QPointF(cx, cy), S * 0.75, S * 0.75)
        rect = QRectF(cx - S / 2, cy - S / 2 - 6, S, S + 84)
        w.rect = rect
        try:
            self._paint_holo(p, w, rect, th, px=int(S * 0.92), big=True)
        except Exception as e:
            p.setPen(QColor("#ff4d6a"))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"holograma inválido: {e}")
        p.restore()

    def _paint_card(self, p: QPainter, w: HudWidget, r: QRectF, th: dict) -> None:
        main = self._accent(w, th)
        w.links = []
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0.0, _col(th["panel2"], 0.88))
        g.setColorAt(1.0, _col(th["bg"], 0.82))
        p.setBrush(QBrush(g))
        p.setPen(QPen(_col(th["border"], 0.95), 1.1))
        p.drawRoundedRect(r, 7, 7)
        # corner brackets in the widget's colour
        L = 10
        p.setPen(QPen(_col(main, 0.9), 1.6))
        for x, y, sx, sy in ((r.left() + 3, r.top() + 3, 1, 1), (r.right() - 3, r.top() + 3, -1, 1),
                             (r.left() + 3, r.bottom() - 3, 1, -1), (r.right() - 3, r.bottom() - 3, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + sx * L, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + sy * L))
        # title + close box
        title = str(w.spec.get("title") or "").upper()
        f = _f(9, True)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        p.setFont(f)
        p.setPen(th["white"])
        p.drawText(QRectF(r.left() + 14, r.top() + 8, r.width() - 44, 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        p.setPen(QPen(_col(main, 0.9), 1.6))
        p.drawLine(QPointF(r.left() + 14, r.top() + 29), QPointF(r.left() + 54, r.top() + 29))
        p.setPen(QPen(_col(th["border"], 0.7), 1))
        p.drawLine(QPointF(r.left() + 54, r.top() + 29), QPointF(r.right() - 14, r.top() + 29))
        cr = QRectF(r.right() - 24, r.top() + 9, 12, 12)
        w.close_rect = cr
        p.setPen(QPen(_col(th["dim"], 0.9), 1.3))
        p.drawLine(cr.topLeft(), cr.bottomRight())
        p.drawLine(cr.topRight(), cr.bottomLeft())

        url = w.card_url()
        foot = 24 if url else 0
        body = QRectF(r.left() + 14, r.top() + 36, r.width() - 28, r.height() - 44 - foot)
        getattr(self, "_p_" + w.type)(p, w, body, main, th)
        if url:
            # the card's link: a footer to click (the whole card opens it too)
            fr = QRectF(r.left() + 10, r.bottom() - foot - 6, r.width() - 20, foot)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(_col(main, 0.16)))
            p.drawRoundedRect(fr, 4, 4)
            f = _f(8.5, True)
            f.setUnderline(True)
            p.setFont(f)
            p.setPen(_col(main, 1.0))
            label = "↗  ABRIR  ·  " + _domain(url)
            p.drawText(fr.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       p.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, int(fr.width() - 16)))
            w.links.append((fr, url))
            w.links.append((QRectF(r.left(), r.top() + 32, r.width(), r.height() - 32), url))

    def _paint_holo(self, p: QPainter, w: HudWidget, r: QRectF, th: dict,
                    px: int | None = None, big: bool = False) -> None:
        """A hologram: no card, just the projected drawing over its beam, with
        the caption (title · value) underneath."""
        s = w.spec
        main = self._accent(w, th)
        image = str(s.get("image") or "")
        svg = "" if image else (s.get("svg") or shape_svg(str(s.get("shape") or "")) or "")
        if not svg and not image:
            p.setPen(th["dim"])
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, f"forma desconhecida: {s.get('shape')}")
            return
        # filled from the target value, not the eased one: re-tinting is the
        # expensive step, so it happens per update, not per frame
        tv = w.targets()
        val = tv[0] if tv else _num(s.get("value"), 0.0)
        if not hasattr(w, "sprite"):
            w.sprite = HoloSprite()
        px = px or int(min(r.width() - 16, w.holo_px()))
        keep = str(s.get("colors") or "").lower() == "original"
        if image:
            w.sprite.prepare_image(image, px, main, keep, light=bool(s.get("render")))
        else:
            svg = render_template(svg, val, _num(s.get("min"), 0.0), _num(s.get("max"), 100.0),
                                  bool(s.get("charging")))
            w.sprite.prepare(svg, px, main, keep)
        if w.sprite.error:
            p.setPen(QColor(NAMED["red"]))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, w.sprite.error)
            return
        area = QRectF(r.left(), r.top() + 4, r.width(), r.height() - 50)
        w.sprite.paint(p, area, time.monotonic() + (hash(w.id) % 100), main,
                       spin=bool(s.get("spin")), beam=s.get("beam", True) is not False)
        # caption
        title = str(s.get("title") or "")
        unit = str(s.get("unit") or "")
        v = w.disp[0] if w.disp else None
        cap = title
        if v is not None and not s.get("hide_value"):
            cap = (f"{title}  ·  " if title else "") + f"{_fmt(v, s.get('decimals'))}{(' ' + unit) if unit else ''}"
        # The whole caption, never cut at the edges: up to two lines, the font
        # stepping down until it fits, the last line elided only as a last resort.
        lines = []
        for size in ((12.5, 11, 10, 9) if big else (9.5, 8.5, 8)):
            f = _f(size, True)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
            lines = self._wrap(cap.upper(), QFontMetrics(f), r.width() - 8, 2)
            if lines and not lines[-1].endswith("…"):
                break
        p.setFont(f)
        lh = QFontMetrics(f).height()
        sub = s.get("subtitle")
        y = r.bottom() - 6 - lh * len(lines) - (14 if sub else 0)
        if th.get("overlay") and lines:
            # over any program the caption needs its own dark plate to be read
            fm = QFontMetrics(f)
            tw = max(fm.horizontalAdvance(ln) for ln in lines) + 28
            plate = QRectF(r.center().x() - tw / 2, y - 4, tw, lh * len(lines) + (16 if sub else 0) + 8)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(_col(th["bg"], 0.72)))
            p.drawRoundedRect(plate, 6, 6)
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(th["white"])
        for ln in lines:
            p.drawText(QRectF(r.left(), y, r.width(), lh), Qt.AlignmentFlag.AlignCenter, ln)
            y += lh
        if sub:
            p.setFont(_f(8))
            p.setPen(th["text"])
            p.drawText(QRectF(r.left(), y + 1, r.width(), 14), Qt.AlignmentFlag.AlignCenter,
                       QFontMetrics(_f(8)).elidedText(str(sub), Qt.TextElideMode.ElideRight, int(r.width())))
        cr = QRectF(r.right() - 16, r.top() + 2, 10, 10)
        w.close_rect = cr
        p.setPen(QPen(_col(th["dim"], 0.7), 1.2))
        p.drawLine(cr.topLeft(), cr.bottomRight())
        p.drawLine(cr.topRight(), cr.bottomLeft())

    @staticmethod
    def _wrap(text: str, fm: QFontMetrics, width: float, max_lines: int) -> list[str]:
        words, lines, cur = text.split(), [], ""
        for wd in words:
            t = (cur + " " + wd).strip()
            if fm.horizontalAdvance(t) <= width or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = wd
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
        if lines and fm.horizontalAdvance(lines[-1]) > width:
            lines[-1] = fm.elidedText(lines[-1], Qt.TextElideMode.ElideRight, int(width))
        return lines

    @staticmethod
    def _accent(w: HudWidget, th: dict) -> QColor:
        c = str(w.spec.get("color") or "").strip().lower()
        base = QColor(NAMED.get(c, c)) if c else QColor(th["main"])
        if not base.isValid():
            base = QColor(th["main"])
        # thresholds: warn / crit turn the widget yellow / red (or reversed with "low_is_bad")
        v = w.disp[0] if w.disp else None
        warn, crit = _num(w.spec.get("warn")), _num(w.spec.get("crit"))
        if v is not None and (warn is not None or crit is not None):
            low_bad = bool(w.spec.get("low_is_bad"))
            hit = (lambda lim: lim is not None and (v <= lim if low_bad else v >= lim))
            if hit(crit):
                return QColor(NAMED["red"])
            if hit(warn):
                return QColor(NAMED["yellow"])
        return base

    # ── each type ────────────────────────────────────────────────────────────

    def _big(self, p, rect, text, color, size, align=Qt.AlignmentFlag.AlignCenter):
        p.setFont(_f(int(size), True))
        p.setPen(color)
        p.drawText(rect, align, text)

    def _p_gauge(self, p, w, b, main, th):
        s = w.spec
        lo, hi = _num(s.get("min"), 0.0), _num(s.get("max"), 100.0)
        v = w.disp[0] if w.disp else None
        frac = 0.0 if v is None or hi == lo else max(0.0, min(1.0, (v - lo) / (hi - lo)))
        R = min(b.width() / 2.0 - 6, b.height() * 0.62)
        cx, cy = b.center().x(), b.top() + R + 6
        arc = QRectF(cx - R, cy - R, 2 * R, 2 * R)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_col(th["border"], 0.9), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(arc, 225 * 16, -270 * 16)
        p.setPen(QPen(_col(main, 0.25), 15, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(arc, 225 * 16, int(-270 * frac * 16))
        p.setPen(QPen(main, 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(arc, 225 * 16, int(-270 * frac * 16))
        # ticks
        p.setPen(QPen(_col(th["dim"], 0.8), 1.2))
        for i in range(11):
            a = math.radians(225 - 27 * i)
            p.drawLine(QPointF(cx + math.cos(a) * (R - 13), cy - math.sin(a) * (R - 13)),
                       QPointF(cx + math.cos(a) * (R - (19 if i % 5 == 0 else 16)),
                               cy - math.sin(a) * (R - (19 if i % 5 == 0 else 16))))
        # needle tip
        a = math.radians(225 - 270 * frac)
        p.setBrush(QBrush(th["white"]))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx + math.cos(a) * R, cy - math.sin(a) * R), 4.5, 4.5)
        self._big(p, QRectF(cx - R, cy - 24, 2 * R, 34), _fmt(v, s.get("decimals")), th["white"], 20)
        p.setFont(_f(9))
        p.setPen(th["text"])
        p.drawText(QRectF(cx - R, cy + 10, 2 * R, 18), Qt.AlignmentFlag.AlignCenter, str(s.get("unit") or ""))
        p.setPen(th["dim"])
        p.setFont(_f(7.5))
        p.drawText(QRectF(b.left(), b.bottom() - 16, b.width() / 2, 14), Qt.AlignmentFlag.AlignLeft,
                   _fmt(lo))
        p.drawText(QRectF(b.center().x(), b.bottom() - 16, b.width() / 2, 14), Qt.AlignmentFlag.AlignRight,
                   _fmt(hi))
        sub = s.get("subtitle")
        if sub:
            p.drawText(QRectF(b.left(), b.bottom() - 16, b.width(), 14), Qt.AlignmentFlag.AlignCenter, str(sub))

    def _p_number(self, p, w, b, main, th):
        s = w.spec
        v = w.disp[0] if w.disp else s.get("value")
        txt = _fmt(v, s.get("decimals")) if not isinstance(v, str) else v
        unit = str(s.get("unit") or "")
        p.setFont(_f(26, True))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(txt)
        p.setPen(th["white"])
        p.drawText(QRectF(b.left(), b.top(), b.width(), 44), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, txt)
        p.setFont(_f(11))
        p.setPen(main)
        p.drawText(QRectF(b.left() + tw + 8, b.top() + 8, b.width() - tw - 8, 36),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, unit)
        sub = str(s.get("subtitle") or "")
        trend = _num(s.get("trend"))
        p.setFont(_f(8.5))
        if trend is not None:
            up = trend >= 0
            p.setPen(QColor(NAMED["green"] if up else NAMED["red"]))
            sub = f"{'▲' if up else '▼'} {_fmt(abs(trend))}%  " + sub
        else:
            p.setPen(th["text"])
        p.drawText(QRectF(b.left(), b.top() + 46, b.width(), 18), Qt.AlignmentFlag.AlignLeft, sub)

    def _p_bar(self, p, w, b, main, th):
        s = w.spec
        vals = w.disp
        labels = list(s.get("labels") or [])
        hi = _num(s.get("max"), None)
        if hi is None:
            hi = max([100.0] + vals) if all(0 <= v <= 100 for v in vals) else max(vals + [1.0])
        lo = _num(s.get("min"), 0.0)
        unit = str(s.get("unit") or "")
        y = b.top() + 2
        for i, v in enumerate(vals[:MAX_ROWS]):
            col = QColor(PALETTE[i % len(PALETTE)]) if len(vals) > 1 and not s.get("color") else main
            lab = str(labels[i]) if i < len(labels) else ""
            p.setFont(_f(8.5))
            p.setPen(th["text"])
            p.drawText(QRectF(b.left(), y, b.width() * 0.6, 14), Qt.AlignmentFlag.AlignLeft, lab)
            p.setPen(th["white"])
            p.drawText(QRectF(b.left(), y, b.width(), 14), Qt.AlignmentFlag.AlignRight, f"{_fmt(v, s.get('decimals'))} {unit}".strip())
            track = QRectF(b.left(), y + 16, b.width(), 7)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(_col(th["border"], 0.8)))
            p.drawRoundedRect(track, 3, 3)
            frac = 0.0 if hi == lo else max(0.0, min(1.0, (v - lo) / (hi - lo)))
            p.setBrush(QBrush(col))
            p.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * frac, track.height()), 3, 3)
            y += 30

    def _p_line(self, p, w, b, main, th):
        s = w.spec
        vals = w.disp if len(w.disp) > 1 else (w.disp * 2 if w.disp else [0.0, 0.0])
        lo = _num(s.get("min"), min(vals))
        hi = _num(s.get("max"), max(vals))
        if hi - lo < 1e-9:
            hi = lo + 1.0
        area = QRectF(b.left(), b.top() + 18, b.width(), b.height() - 34)
        p.setPen(QPen(_col(th["border"], 0.6), 1, Qt.PenStyle.DotLine))
        for k in range(4):
            yy = area.top() + area.height() * k / 3
            p.drawLine(QPointF(area.left(), yy), QPointF(area.right(), yy))
        pts = [QPointF(area.left() + area.width() * i / (len(vals) - 1),
                       area.bottom() - area.height() * (v - lo) / (hi - lo)) for i, v in enumerate(vals)]
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        fill = QPainterPath(path)
        fill.lineTo(QPointF(area.right(), area.bottom()))
        fill.lineTo(QPointF(area.left(), area.bottom()))
        fill.closeSubpath()
        g = QLinearGradient(area.topLeft(), area.bottomLeft())
        g.setColorAt(0.0, _col(main, 0.35))
        g.setColorAt(1.0, _col(main, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawPath(fill)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(main, 2.0))
        p.drawPath(path)
        p.setBrush(QBrush(th["white"]))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(pts[-1], 3.5, 3.5)
        unit = str(s.get("unit") or "")
        p.setFont(_f(13, True))
        p.setPen(th["white"])
        p.drawText(QRectF(b.left(), b.top() - 2, b.width(), 18), Qt.AlignmentFlag.AlignRight,
                   f"{_fmt(vals[-1], s.get('decimals'))} {unit}".strip())
        p.setFont(_f(7.5))
        p.setPen(th["dim"])
        labels = list(s.get("labels") or [])
        if labels:
            p.drawText(QRectF(b.left(), b.bottom() - 13, b.width() / 2, 12), Qt.AlignmentFlag.AlignLeft, str(labels[0]))
            p.drawText(QRectF(b.center().x(), b.bottom() - 13, b.width() / 2, 12), Qt.AlignmentFlag.AlignRight, str(labels[-1]))
        else:
            p.drawText(QRectF(b.left(), b.bottom() - 13, b.width(), 12), Qt.AlignmentFlag.AlignLeft,
                       f"mín {_fmt(min(vals))}   máx {_fmt(max(vals))}")

    def _p_columns(self, p, w, b, main, th):
        s = w.spec
        vals = w.disp or [0.0]
        labels = list(s.get("labels") or [])
        hi = _num(s.get("max"), max(vals + [1e-9]))
        n = len(vals[:24])
        area = QRectF(b.left(), b.top() + 4, b.width(), b.height() - 24)
        bw = area.width() / n
        for i, v in enumerate(vals[:24]):
            hgt = area.height() * max(0.0, min(1.0, v / hi if hi else 0))
            col = QColor(PALETTE[i % len(PALETTE)]) if s.get("multicolor") else main
            g = QLinearGradient(QPointF(0, area.bottom() - hgt), QPointF(0, area.bottom()))
            g.setColorAt(0.0, col)
            g.setColorAt(1.0, _col(col, 0.35))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawRoundedRect(QRectF(area.left() + i * bw + bw * 0.18, area.bottom() - hgt, bw * 0.64, hgt), 2, 2)
            if n <= 12:
                p.setFont(_f(7))
                p.setPen(th["white"])
                p.drawText(QRectF(area.left() + i * bw, area.bottom() - hgt - 13, bw, 12),
                           Qt.AlignmentFlag.AlignCenter, _fmt(v, s.get("decimals")))
            if i < len(labels):
                p.setFont(_f(7))
                p.setPen(th["dim"])
                p.drawText(QRectF(area.left() + i * bw, b.bottom() - 16, bw, 14),
                           Qt.AlignmentFlag.AlignCenter, str(labels[i])[:6])

    def _p_donut(self, p, w, b, main, th):
        s = w.spec
        vals = [max(0.0, v) for v in w.disp] or [1.0]
        labels = list(s.get("labels") or [])
        total = sum(vals) or 1.0
        R = min(52.0, b.height() / 2 - 4)
        c = QPointF(b.left() + R + 4, b.top() + b.height() / 2)
        rect = QRectF(c.x() - R, c.y() - R, 2 * R, 2 * R)
        start = 90.0
        for i, v in enumerate(vals):
            span = 360.0 * v / total
            p.setPen(QPen(QColor(PALETTE[i % len(PALETTE)]), 13, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(rect, int(start * 16), int(-span * 16))
            start -= span
        self._big(p, QRectF(c.x() - R, c.y() - 12, 2 * R, 24), _fmt(total, s.get("decimals")), th["white"], 12)
        x = c.x() + R + 18
        y = b.top() + 4
        for i, v in enumerate(vals[:8]):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(PALETTE[i % len(PALETTE)])))
            p.drawRoundedRect(QRectF(x, y + 4, 9, 9), 2, 2)
            p.setFont(_f(8))
            p.setPen(th["text"])
            lab = str(labels[i]) if i < len(labels) else f"#{i + 1}"
            p.drawText(QRectF(x + 14, y, b.right() - x - 14, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"{lab}  {round(100 * v / total)}%")
            y += 20

    def _rows(self, w):
        return [(k, v) for k, v, _u in self._rows_l(w)]

    def _rows_l(self, w):
        """(label, value, url) per item; the URL is taken out of the text shown."""
        out = []
        for it in (w.spec.get("items") or [])[:MAX_ROWS]:
            if isinstance(it, (list, tuple)) and len(it) >= 2:
                k, v = str(it[0]), str(it[1])
            else:
                t = str(it)
                k, sep, v = t.partition(": ")
                k, v = (k.strip(), v.strip()) if sep else (t, "")
            k, uk = _split_url(k)
            v, uv = _split_url(v)
            url = uk or uv
            if url and not (k or v):
                k = _domain(url)
            elif url and not v:
                v = _domain(url)
            out.append((k, v, url))
        return out

    def _p_list(self, p, w, b, main, th):
        y = b.top() + 2
        for k, v, url in self._rows_l(w):
            p.setBrush(QBrush(main))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(b.left() + 4, y + 9), 2.6, 2.6)
            f = _f(9)
            f.setUnderline(bool(url))
            p.setFont(f)
            p.setPen(_col(main, 1.0) if url else th["white"])
            text = (f"{k}  —  {v}" if v else k) + ("  ↗" if url else "")
            row = QRectF(b.left() + 14, y, b.width() - 14, 18)
            p.drawText(row, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(b.width() - 14)))
            if url:
                w.links.insert(0, (row.adjusted(-14, -2, 0, 2), url))
            y += 22

    def _p_table(self, p, w, b, main, th):
        y = b.top() + 2
        for i, (k, v, url) in enumerate(self._rows_l(w)):
            if i % 2 == 0:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(_col(th["border"], 0.35)))
                p.drawRect(QRectF(b.left() - 4, y, b.width() + 8, 20))
            p.setFont(_f(8.5))
            p.setPen(th["text"])
            p.drawText(QRectF(b.left(), y, b.width() * 0.55, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, k)
            f = _f(9, True)
            f.setUnderline(bool(url))
            p.setFont(f)
            p.setPen(_col(main, 1.0) if url else th["white"])
            p.drawText(QRectF(b.left(), y, b.width(), 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       v + ("  ↗" if url else ""))
            if url:
                w.links.insert(0, (QRectF(b.left() - 4, y, b.width() + 8, 20), url))
            y += 22

    def _p_photo(self, p, w, b, main, th):
        """The picture as it is — real colours, no hologram treatment. A click
        opens the full image."""
        img = w.photo()
        cap = str(w.spec.get("text") or w.spec.get("subtitle") or "")
        area = QRectF(b.left(), b.top(), b.width(), b.height() - (22 if cap else 0))
        if img is None or img.isNull():
            p.setPen(th["dim"])
            p.drawText(area, Qt.AlignmentFlag.AlignCenter, "imagem indisponível")
            return
        k = min(area.width() / img.width(), area.height() / img.height())
        dw, dh = img.width() * k, img.height() * k
        r = QRectF(area.center().x() - dw / 2, area.top(), dw, dh)
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(r, 5, 5)
        p.setClipPath(clip)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(r, img)
        p.restore()
        p.setPen(QPen(_col(main, 0.35), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 5, 5)
        if cap:
            p.setFont(_f(8.5))
            p.setPen(th["dim"])
            p.drawText(QRectF(b.left(), r.bottom() + 4, b.width(), 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       p.fontMetrics().elidedText(cap, Qt.TextElideMode.ElideRight, int(b.width())))
        from PyQt6.QtCore import QUrl
        w.links.append((r, QUrl.fromLocalFile(str(w.spec.get("image"))).toString()))

    def _p_text(self, p, w, b, main, th):
        p.setFont(_f(9))
        p.setPen(th["white"])
        p.drawText(b, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap),
                   str(w.spec.get("text") or ""))

    def _p_speedtest(self, p, w, b, main, th):
        s = w.spec
        down, up, ping = (w.disp + [0, 0, 0])[:3]
        phase = str(s.get("phase") or "")
        need = max(down * 1.15, _num(s.get("max"), 0) or 0)
        hi = next((x for x in (50, 100, 200, 300, 500, 1000, 2000, 5000, 10000) if x >= need), need)
        g = HudWidget(w.id + "_g", {"type": "gauge", "value": down, "min": 0, "max": hi,
                                    "unit": "Mbps · download", "decimals": 1})
        g.disp = [down]
        self._p_gauge(p, g, QRectF(b.left(), b.top(), b.width(), b.height() - 40), main, th)
        y = b.bottom() - 34
        cols = (("UPLOAD", f"{_fmt(up, 1)} Mbps"), ("PING", f"{_fmt(ping, 0)} ms"), ("STATUS", phase or "—"))
        cw = b.width() / 3
        for i, (k, v) in enumerate(cols):
            p.setFont(_f(7, True))
            p.setPen(th["dim"])
            p.drawText(QRectF(b.left() + i * cw, y, cw, 12), Qt.AlignmentFlag.AlignCenter, k)
            p.setFont(_f(10, True))
            p.setPen(th["white"] if i < 2 else main)
            p.drawText(QRectF(b.left() + i * cw, y + 13, cw, 18), Qt.AlignmentFlag.AlignCenter, v)
