"""
core/hud_holo.py — holograms on the HUD: any drawing, projected.

A hologram is an SVG drawing — one of the ready-made SHAPES below (Wi-Fi
antenna, battery cell, thermometer, globe…), or any SVG the assistant writes
on the spot — shown the way the film shows them: tinted light instead of ink,
a glow around it, scanlines, a faint flicker and the odd glitch, floating over
a projector beam, optionally turning in 3-D.

LIVE DRAWINGS
    SVG text may contain {{ expressions }} that are filled in from the
    hologram's value before drawing, e.g. a battery whose fill width is
    {{ pct * 0.62 }} or a colour {{ 'red' if pct < 20 else 'white' }}. Names:
        value  the raw value          pct   value scaled to 0–100 by min/max
        min, max, charging (bool)     on(n) 'white' when pct ≥ n, else dim grey
    and the functions min max abs round int. Nothing else is reachable — the
    expressions are parsed and walked, never handed to eval().

COST
    The SVG is rasterised and tinted (numpy) only when the drawing or its value
    changes; each frame just blits two cached images with an offset, a scale
    and an opacity, plus the beam. Holograms add well under a millisecond.
"""

from __future__ import annotations

import ast
import math
import operator
import random
import re

from PyQt6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import (QBrush, QColor, QImage, QLinearGradient, QPainter,
                         QPolygonF, QRadialGradient)

try:
    from PyQt6.QtSvg import QSvgRenderer
except Exception:                      # QtSvg missing: holograms degrade to a notice
    QSvgRenderer = None

ON, OFF = "#ffffff", "#3c3c3c"

# Ready-made drawings, 100×100, white on transparent (brightness = intensity).
SHAPES: dict[str, str] = {
    "wifi": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" fill="none" stroke-linecap="round">
  <path d="M10 42 A58 58 0 0 1 90 42" stroke="{{on(76)}}" stroke-width="7"/>
  <path d="M22 55 A41 41 0 0 1 78 55" stroke="{{on(51)}}" stroke-width="7"/>
  <path d="M34 68 A24 24 0 0 1 66 68" stroke="{{on(26)}}" stroke-width="7"/>
  <circle cx="50" cy="80" r="6" fill="{{on(1)}}" stroke="none"/>
</svg>""",
    "signal": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
  <rect x="12" y="70" width="12" height="18" rx="2" fill="{{on(1)}}"/>
  <rect x="30" y="56" width="12" height="32" rx="2" fill="{{on(21)}}"/>
  <rect x="48" y="42" width="12" height="46" rx="2" fill="{{on(41)}}"/>
  <rect x="66" y="28" width="12" height="60" rx="2" fill="{{on(61)}}"/>
  <rect x="84" y="14" width="12" height="74" rx="2" fill="{{on(81)}}"/>
</svg>""",
    "battery": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
  <rect x="8" y="30" width="78" height="40" rx="7" fill="none" stroke="#ffffff" stroke-width="5"/>
  <rect x="88" y="42" width="7" height="16" rx="2" fill="#ffffff"/>
  <rect x="15" y="37" width="{{max(0, pct) * 0.64}}" height="26" rx="3"
        fill="{{'#ff4d6a' if pct < 20 else '#ffffff'}}"/>
  <path d="M50 33 L40 52 L49 52 L45 67 L58 46 L49 46 Z" fill="#7fd8ff" stroke="#000000"
        stroke-width="1.5" opacity="{{1 if charging else 0}}"/>
</svg>""",
    "thermometer": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
  <rect x="42" y="8" width="16" height="64" rx="8" fill="none" stroke="#ffffff" stroke-width="4"/>
  <circle cx="50" cy="80" r="13" fill="none" stroke="#ffffff" stroke-width="4"/>
  <circle cx="50" cy="80" r="8" fill="#ffffff"/>
  <rect x="47" y="{{70 - pct * 0.56}}" width="6" height="{{pct * 0.56 + 4}}" fill="#ffffff"/>
  <path d="M62 20 H70 M62 32 H68 M62 44 H70 M62 56 H68" stroke="#9a9a9a" stroke-width="2.5"/>
</svg>""",
    "globe": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="#ffffff" stroke-width="2">
  <circle cx="50" cy="50" r="40" stroke-width="3"/>
  <ellipse cx="50" cy="50" rx="14" ry="40"/><ellipse cx="50" cy="50" rx="28" ry="40" stroke="#9a9a9a"/>
  <path d="M10 50 H90 M15 30 H85 M15 70 H85" stroke="#9a9a9a"/>
  <path d="M50 10 V90"/>
</svg>""",
    "cpu": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="#ffffff" stroke-width="4">
  <rect x="26" y="26" width="48" height="48" rx="5"/>
  <rect x="38" y="38" width="24" height="24" rx="2" fill="{{on(50)}}" stroke="none"/>
  <path d="M36 26V12 M50 26V12 M64 26V12 M36 74V88 M50 74V88 M64 74V88 M26 36H12 M26 50H12 M26 64H12 M74 36H88 M74 50H88 M74 64H88" stroke-width="3"/>
</svg>""",
    "heart": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
  <path d="M50 86 L16 52 A19 19 0 0 1 50 26 A19 19 0 0 1 84 52 Z" fill="none" stroke="#ffffff" stroke-width="5"/>
  <path d="M18 54 H36 L42 42 L50 66 L57 48 L62 54 H82" fill="none" stroke="#ffffff" stroke-width="3"/>
</svg>""",
    "sun": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" stroke="#ffffff" stroke-width="4" fill="none">
  <circle cx="50" cy="50" r="18" fill="#ffffff"/>
  <path d="M50 8V20 M50 80V92 M8 50H20 M80 50H92 M20 20L28 28 M72 72L80 80 M80 20L72 28 M28 72L20 80"/>
</svg>""",
    "cloud": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
  <path d="M26 70 A16 16 0 0 1 28 38 A22 22 0 0 1 70 34 A18 18 0 0 1 76 70 Z" fill="none" stroke="#ffffff" stroke-width="5"/>
</svg>""",
    "rain": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="#ffffff">
  <path d="M26 58 A16 16 0 0 1 28 28 A22 22 0 0 1 70 24 A18 18 0 0 1 76 58 Z" stroke-width="5"/>
  <path d="M34 68 L28 84 M52 68 L46 84 M70 68 L64 84" stroke-width="4" stroke-linecap="round"/>
</svg>""",
    "bolt": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
  <path d="M58 8 L24 56 H48 L40 92 L76 42 H52 Z" fill="none" stroke="#ffffff" stroke-width="5" stroke-linejoin="round"/>
</svg>""",
    "house": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="#ffffff" stroke-width="5" stroke-linejoin="round">
  <path d="M12 48 L50 14 L88 48"/><path d="M22 40 V86 H78 V40"/><rect x="42" y="60" width="16" height="26"/>
</svg>""",
    "lock": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="#ffffff" stroke-width="5">
  <rect x="22" y="44" width="56" height="42" rx="6"/><path d="M34 44 V32 A16 16 0 0 1 66 32 V44"/>
  <circle cx="50" cy="64" r="5" fill="#ffffff"/>
</svg>""",
    "warning": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
  <path d="M50 10 L92 86 H8 Z" fill="none" stroke="#ffffff" stroke-width="5" stroke-linejoin="round"/>
  <path d="M50 38 V62" stroke="#ffffff" stroke-width="7" stroke-linecap="round"/><circle cx="50" cy="74" r="4.5" fill="#ffffff"/>
</svg>""",
    "check": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="#ffffff" stroke-width="6">
  <circle cx="50" cy="50" r="40"/><path d="M30 52 L44 66 L72 36" stroke-linecap="round" stroke-linejoin="round"/>
</svg>""",
    "rocket": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="#ffffff" stroke-width="4" stroke-linejoin="round">
  <path d="M50 8 C66 22 70 44 64 66 H36 C30 44 34 22 50 8 Z"/><circle cx="50" cy="38" r="7"/>
  <path d="M36 54 L22 72 L36 68 M64 54 L78 72 L64 68"/><path d="M42 72 L50 92 L58 72" stroke="#9a9a9a"/>
</svg>""",
    "atom": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="#ffffff" stroke-width="3">
  <ellipse cx="50" cy="50" rx="42" ry="15"/><ellipse cx="50" cy="50" rx="42" ry="15" transform="rotate(60 50 50)"/>
  <ellipse cx="50" cy="50" rx="42" ry="15" transform="rotate(120 50 50)"/><circle cx="50" cy="50" r="6" fill="#ffffff"/>
</svg>""",
    "car": """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="#ffffff" stroke-width="4" stroke-linejoin="round">
  <path d="M10 64 V52 L22 48 L32 32 H66 L78 48 L90 52 V64 Z"/><path d="M36 36 H62 L70 48 H28 Z" stroke="#9a9a9a"/>
  <circle cx="28" cy="66" r="8" fill="#000"/><circle cx="72" cy="66" r="8" fill="#000"/>
</svg>""",
}
SHAPE_ALIASES = {"antena": "wifi", "antenna": "wifi", "pilha": "battery", "bateria": "battery",
                 "termometro": "thermometer", "termômetro": "thermometer", "terra": "globe",
                 "world": "globe", "mundo": "globe", "coracao": "heart", "coração": "heart",
                 "sol": "sun", "nuvem": "cloud", "chuva": "rain", "raio": "bolt", "casa": "house",
                 "cadeado": "lock", "alerta": "warning", "ok": "check", "foguete": "rocket",
                 "atomo": "atom", "átomo": "atom", "carro": "car", "sinal": "signal"}


# ── {{ expression }} templating, without eval() ──────────────────────────────

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.FloorDiv: operator.floordiv}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}
_FUNCS = {"min": min, "max": max, "abs": abs, "round": round, "int": int}


def _eval(node, env):
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str, bool)):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise ValueError(f"unknown name {node.id}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        a, b = _eval(node.left, env), _eval(node.right, env)
        if isinstance(node.op, ast.Pow) and abs(b) > 8:
            raise ValueError("exponent too large")
        return _BIN[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        v = _eval(node.operand, env)
        return -v if isinstance(node.op, ast.USub) else (+v if isinstance(node.op, ast.UAdd) else not v)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMP:
        return _CMP[type(node.ops[0])](_eval(node.left, env), _eval(node.comparators[0], env))
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, env) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.IfExp):
        return _eval(node.body, env) if _eval(node.test, env) else _eval(node.orelse, env)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fn = _FUNCS.get(node.func.id) or (env.get(node.func.id) if callable(env.get(node.func.id)) else None)
        if fn is None or node.keywords:
            raise ValueError(f"function {node.func.id} not allowed")
        return fn(*[_eval(a, env) for a in node.args])
    raise ValueError(f"expression not allowed: {type(node).__name__}")


def render_template(svg: str, value, lo=0.0, hi=100.0, charging=False) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    lo = 0.0 if lo is None else float(lo)
    hi = 100.0 if hi is None else float(hi)
    pct = 0.0 if hi == lo else max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))
    # min / max are values here; called as functions they are the builtins
    # (_eval looks calls up in _FUNCS first)
    env = {"value": v, "v": v, "pct": pct, "min": lo, "max": hi, "lo": lo, "hi": hi,
           "charging": bool(charging), "on": lambda n: ON if pct >= float(n) else OFF}

    def sub(m):
        try:
            out = _eval(ast.parse(m.group(1).strip(), mode="eval"), env)
        except Exception:
            return "0"
        return f"{out:.2f}" if isinstance(out, float) else str(out)
    return re.sub(r"\{\{(.+?)\}\}", sub, svg, flags=re.S)


def shape_svg(name: str) -> str | None:
    key = (name or "").strip().lower()
    return SHAPES.get(SHAPE_ALIASES.get(key, key))


# ── the sprite: rasterise + tint once, project every frame ───────────────────

class HoloSprite:
    def __init__(self):
        self._key = None
        self.img: QImage | None = None
        self.glow: QImage | None = None
        self.error = ""
        self.clean = False        # rendered holograms: softer scanlines, rare glitches
        self._rng = random.Random()

    def prepare(self, svg_text: str, size: int, color: QColor, keep_colors: bool) -> None:
        key = (svg_text, size, color.rgba(), keep_colors)
        if key == self._key:
            return
        self._key = key
        self.error = ""
        if QSvgRenderer is None:
            self.img = self.glow = None
            self.error = "QtSvg indisponível"
            return
        r = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
        if not r.isValid():
            self.img = self.glow = None
            self.error = "desenho SVG inválido"
            return
        vb = r.viewBoxF()
        ar = (vb.width() / vb.height()) if vb.height() > 0 else 1.0
        w, h = (size, max(1, int(size / ar))) if ar >= 1 else (max(1, int(size * ar)), size)
        raw = QImage(QSize(w, h), QImage.Format.Format_ARGB32_Premultiplied)
        raw.fill(Qt.GlobalColor.transparent)
        p = QPainter(raw)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r.render(p, QRectF(0, 0, w, h))
        p.end()
        self.img = self._tint(raw, color, keep_colors)
        small = self.img.scaled(max(1, w // 5), max(1, h // 5), Qt.AspectRatioMode.IgnoreAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
        self.glow = small.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)

    def prepare_image(self, path: str, size: int, color: QColor, keep_colors: bool,
                      light: bool = False) -> None:
        """A real picture (photo, screenshot, logo) as a hologram: scaled to fit,
        a plain background cut away, then the same light treatment as drawings."""
        import os
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        key = ("img", path, mtime, size, color.rgba(), keep_colors, light)
        if key == self._key:
            return
        self._key = key
        self.error = ""
        src = QImage(path)
        if src.isNull():
            self.img = self.glow = None
            self.error = "não consegui abrir a imagem"
            return
        self.clean = light
        if light:
            # a render of light on black: black becomes nothing, the rest glows;
            # done at full resolution, cropped to the subject, then fitted
            lit = self._light(src.convertToFormat(QImage.Format.Format_ARGB32))
            self.img = lit.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
        else:
            src = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            src = self._cut_background(src.convertToFormat(QImage.Format.Format_ARGB32))
            self.img = self._tint(src, color, keep_colors)
        w, h = self.img.width(), self.img.height()
        small = self.img.scaled(max(1, w // 5), max(1, h // 5), Qt.AspectRatioMode.IgnoreAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
        self.glow = small.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)

    @staticmethod
    def _light(img: QImage) -> QImage:
        """Light-on-black → light on nothing: each pixel's brightness becomes its
        opacity and its colour is un-dimmed by the same amount (like additive
        blending), with faint scanlines. The render keeps its own colours."""
        import numpy as np
        w, h = img.width(), img.height()
        ptr = img.bits()
        ptr.setsize(h * img.bytesPerLine())
        a = np.frombuffer(ptr, np.uint8).reshape(h, img.bytesPerLine())[:, :w * 4] \
            .reshape(h, w, 4).astype(np.float32)
        rgb = a[..., :3]
        m = rgb.max(axis=2)
        # the black floor drops out; the curve keeps darker colours (deep reds,
        # browns) solid enough to read as colour, not as faint glow
        alpha = np.clip((m - 6.0) / 249.0, 0.0, 1.0) ** 0.6
        out = np.zeros((h, w, 4), np.float32)
        out[..., :3] = np.clip(rgb * (255.0 / np.maximum(m, 1.0))[..., None], 0, 255)
        # crop to the subject (renders leave wide black margins), so it fills
        # the projection and sits on the beam instead of floating in a square
        rows = np.where((alpha > 0.06).any(axis=1))[0]
        cols = np.where((alpha > 0.06).any(axis=0))[0]
        if rows.size and cols.size:
            pad = int(0.03 * max(w, h))
            y0, y1 = max(0, rows[0] - pad), min(h, rows[-1] + pad + 1)
            x0, x1 = max(0, cols[0] - pad), min(w, cols[-1] + pad + 1)
            out, alpha = out[y0:y1, x0:x1].copy(), alpha[y0:y1, x0:x1].copy()
            h, w = alpha.shape
        alpha[::3, :] *= 0.82                                        # faint scanlines
        out[..., 3] = alpha * 255.0
        res = QImage(np.ascontiguousarray(out.astype(np.uint8)).tobytes(), w, h, w * 4,
                     QImage.Format.Format_ARGB32)
        return res.copy()

    @staticmethod
    def _cut_background(img: QImage) -> QImage:
        """If the picture sits on a plain background (a product photo on white,
        a logo on black), make that background transparent — otherwise it would
        project as a glowing rectangle."""
        import numpy as np
        w, h = img.width(), img.height()
        ptr = img.bits()
        ptr.setsize(h * img.bytesPerLine())
        a = np.frombuffer(ptr, np.uint8).reshape(h, img.bytesPerLine())[:, :w * 4] \
            .reshape(h, w, 4).astype(np.float32).copy()
        b = max(2, min(w, h) // 25)
        border = np.concatenate([a[:b].reshape(-1, 4), a[-b:].reshape(-1, 4),
                                 a[:, :b].reshape(-1, 4), a[:, -b:].reshape(-1, 4)])[:, :3]
        if border.std(axis=0).mean() > 28:          # busy edges: a real scene, keep it whole
            return img
        dist = np.sqrt(((a[..., :3] - border.mean(axis=0)) ** 2).sum(axis=2))
        a[..., 3] *= np.clip((dist - 22.0) / 45.0, 0.0, 1.0)
        out = QImage(a.astype(np.uint8).tobytes(), w, h, w * 4, QImage.Format.Format_ARGB32)
        return out.copy()

    @staticmethod
    def _tint(raw: QImage, color: QColor, keep_colors: bool) -> QImage:
        """Ink → light: brightness becomes opacity, everything takes the
        hologram's colour (bright parts going white-hot), and every third row
        is dimmed for the scanline look."""
        import numpy as np
        img = raw.convertToFormat(QImage.Format.Format_ARGB32)
        w, h = img.width(), img.height()
        ptr = img.bits()
        ptr.setsize(h * img.bytesPerLine())
        a = np.frombuffer(ptr, np.uint8).reshape(h, img.bytesPerLine())[:, :w * 4].reshape(h, w, 4).astype(np.float32)
        b, g, r, al = a[..., 0], a[..., 1], a[..., 2], a[..., 3] / 255.0
        lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        out = np.zeros((h, w, 4), np.float32)
        if keep_colors:
            out[..., 0], out[..., 1], out[..., 2] = b, g, r
            alpha = al * 0.92
        else:
            cr, cg, cb = color.red(), color.green(), color.blue()
            hot = np.clip(lum, 0, 1) ** 2.2          # the brightest strokes run white
            out[..., 2] = cr + (255 - cr) * hot * 0.75
            out[..., 1] = cg + (255 - cg) * hot * 0.75
            out[..., 0] = cb + (255 - cb) * hot * 0.75
            alpha = al * (0.18 + 0.82 * lum)
            # Coloured parts stay coloured light (a red low battery must stay
            # red): blend back toward the drawing's own colour by saturation.
            mx = np.maximum(np.maximum(r, g), b)
            mn = np.minimum(np.minimum(r, g), b)
            sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0.0)
            k = np.clip((sat - 0.35) / 0.4, 0, 1)
            lift = np.clip(255.0 / np.maximum(mx, 1), 1, 2.2)  # as bright as light, same hue
            for ch, src in ((2, r), (1, g), (0, b)):
                out[..., ch] = out[..., ch] * (1 - k) + np.clip(src * lift, 0, 255) * k
            alpha = np.maximum(alpha, al * 0.95 * k)
        alpha[::3, :] *= 0.55                          # scanlines
        out[..., 3] = np.clip(alpha * 255, 0, 255)
        res = QImage(out.astype(np.uint8).tobytes(), w, h, w * 4, QImage.Format.Format_ARGB32)
        return res.copy()

    def paint(self, p: QPainter, area: QRectF, t: float, color: QColor, spin: bool = False,
              beam: bool = True) -> QRectF:
        """Project into `area` (the hologram floats in its upper part, the
        beam rises from the bottom). Returns the drawing's rect."""
        if self.img is None:
            return QRectF()
        iw, ih = self.img.width(), self.img.height()
        base_y = area.bottom() - 10
        bob = math.sin(t * 1.6) * 4.0
        cx = area.center().x()
        # renders sit low, close to the beam; line drawings float mid-air
        k = 0.8 if self.clean else 0.5
        top = area.top() + max(0.0, (area.height() - 26 - ih) * k) + bob
        sx = abs(math.cos(t * 0.9)) * 0.85 + 0.15 if spin else 1.0
        dw = iw * sx
        rect = QRectF(cx - dw / 2, top, dw, ih)

        if beam:
            # projector: a glowing disc and a cone of light up to the drawing
            disc = QRadialGradient(QPointF(cx, base_y), iw * 0.45)
            disc.setColorAt(0.0, _a(color, 0.55))
            disc.setColorAt(1.0, _a(color, 0.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(disc))
            p.drawEllipse(QPointF(cx, base_y), iw * 0.45, 9)
            cone = QLinearGradient(QPointF(cx, base_y), QPointF(cx, rect.top()))
            cone.setColorAt(0.0, _a(color, 0.16))
            cone.setColorAt(1.0, _a(color, 0.0))
            p.setBrush(QBrush(cone))
            p.drawPolygon(QPolygonF([QPointF(cx - iw * 0.12, base_y), QPointF(cx + iw * 0.12, base_y),
                                     QPointF(rect.right(), rect.bottom() - ih * 0.15),
                                     QPointF(rect.left(), rect.bottom() - ih * 0.15)]))
            p.setBrush(Qt.BrushStyle.NoBrush)

        flick = 0.88 + 0.12 * math.sin(t * 23.0) * math.sin(t * 7.3)
        o0 = p.opacity()
        if self.clean:                      # a calmer projection for realistic renders
            flick = 0.96 + 0.04 * math.sin(t * 9.0)
        p.setOpacity(o0 * (0.45 if self.clean else 0.75) * flick)
        p.drawImage(rect.adjusted(-6, -6, 6, 6), self.glow)
        p.setOpacity(o0 * flick)
        p.drawImage(rect, self.img)
        p.setOpacity(o0)
        # a bright band sweeping down the drawing: the drawing's own slice,
        # added on top of itself, so only the lines light up — not the air
        band = (t * 0.55) % 1.4 - 0.2
        if 0.0 <= band <= 1.0:
            sy = band * ih
            sh = max(2.0, ih * 0.05)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            p.setOpacity(o0 * (0.35 if self.clean else 0.6))
            p.drawImage(QRectF(rect.left(), rect.top() + sy, rect.width(), sh), self.img,
                        QRectF(0, sy, iw, sh))
            p.setOpacity(o0)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        # the odd glitch: a slice of the drawing jumps sideways for a frame
        if self._rng.random() < (0.004 if self.clean else 0.025):
            sy = self._rng.uniform(0, ih - 6)
            sh = self._rng.uniform(3, 9)
            off = self._rng.uniform(-8, 8)
            p.drawImage(QRectF(rect.left() + off, rect.top() + sy, rect.width(), sh), self.img,
                        QRectF(0, sy, iw, sh))
        return rect


def _a(c: QColor, alpha: float) -> QColor:
    q = QColor(c)
    q.setAlphaF(max(0.0, min(1.0, alpha)))
    return q
