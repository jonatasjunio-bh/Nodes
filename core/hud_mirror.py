"""
core/hud_mirror.py — the HUD's cards, mirrored to the phone.

Every card the assistant puts on the HUD (indicators, lists with links,
holograms, alerts) goes through JarvisUI.hud_widget, which also hands it here.
This module keeps the current set of cards and sends each change to the
dashboard, which forwards it to every connected screen — the phone app shows
the dashboard, so the cards appear there too, tappable.

Kept here, apart from the plugin and the UI, so the state survives the phone
connecting later: a newly opened app first receives snapshot().

What a browser can't use is translated on the way out:
  image  a local file path → a /hud/img/<token> URL the dashboard serves
  shape  a ready-made hologram shape name → its SVG
"""

from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

_lock = threading.Lock()
_state: dict[str, dict] = {}            # id → merged spec
_born: dict[str, float] = {}
_images: dict[str, str] = {}            # token → local path
_by_path: dict[str, str] = {}           # local path → token
_sink = None                            # callable(msg: dict), thread-safe


def set_sink(fn) -> None:
    """main.py: how to reach the dashboard (called from any thread)."""
    global _sink
    _sink = fn


def _token_for(path: str) -> str | None:
    try:
        p = Path(path).resolve()
    except Exception:
        return None
    # only picture files the HUD actually showed get a URL (an unguessable
    # token, served to signed-in screens only)
    if not p.is_file() or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return None
    key = str(p)
    tok = _by_path.get(key)
    if tok is None:
        tok = secrets.token_urlsafe(12)
        _by_path[key], _images[tok] = tok, key
    return tok


def image_path(token: str) -> str | None:
    """The file behind a /hud/img/<token> URL — only ones this module handed out."""
    return _images.get(token)


def _webify(spec: dict) -> dict:
    out = {}
    for k, v in spec.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [x if isinstance(x, (str, int, float, bool)) else str(x) for x in v][:200]
    img = spec.get("image")
    if isinstance(img, str) and img and not img.startswith(("http://", "https://", "data:")):
        tok = _token_for(img)
        out["image"] = f"/hud/img/{tok}" if tok else None
    shape = spec.get("shape")
    if shape and not spec.get("svg"):
        try:
            from core.hud_holo import shape_svg
            svg = shape_svg(str(shape))
            if svg:
                out["svg"] = svg
        except Exception:
            pass
    return out


def publish(op: str, payload: dict | None) -> None:
    p = dict(payload or {})
    wid = str(p.get("id") or "")
    with _lock:
        if op in ("show", "update") and wid:
            if op == "show" and wid not in _state:
                _state[wid] = {}
                _born[wid] = time.time()
            _state.setdefault(wid, {}).update({k: v for k, v in p.items() if v is not None})
            if p.get("ttl"):
                _born[wid] = time.time()
            spec = dict(_state[wid])
        elif op == "remove" and wid:
            _state.pop(wid, None)
            _born.pop(wid, None)
            spec = {"id": wid}
        elif op == "clear":
            _state.clear()
            _born.clear()
            spec = {}
        else:
            return
    sink = _sink
    if sink is not None:
        try:
            sink({"type": "hud", "op": op, "w": _webify(spec)})
        except Exception as e:
            print(f"[HUD mirror] {e}")


def snapshot() -> list[dict]:
    """The cards on the HUD right now, as 'show' messages, for a screen that
    just connected. Cards past their ttl are left out."""
    now = time.time()
    with _lock:
        items = []
        for wid, spec in list(_state.items()):
            ttl = spec.get("ttl")
            try:
                if ttl and now - _born.get(wid, now) > float(ttl):
                    _state.pop(wid, None)
                    continue
            except (TypeError, ValueError):
                pass
            items.append({"type": "hud", "op": "show", "w": _webify(spec)})
    return items
