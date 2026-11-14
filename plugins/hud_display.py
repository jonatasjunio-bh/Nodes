"""
HUD display — the assistant draws any indicator on the HUD, on request.

    "mostra a velocidade da internet"      → speedtest card, measured live
    "coloca o uso de CPU num gráfico"      → line chart fed by the 'cpu' source
    "mostra a cotação do dólar"            → number fed by a JSON URL
    "faz um gráfico dos meus gastos: …"    → donut / columns with the values given
    "tira os indicadores"                  → clear

A widget is a spec (type, title, value(s), unit, min/max, colour…) — the model
fills it from what it knows or looked up. To stay live it names a SOURCE, and
a feeder thread here keeps it current:

    cpu ram gpu temp disk battery          percent / °C, from the PC
    net_down net_up                        Mbps right now
    ping                                   ms to the internet (TCP to 1.1.1.1)
    procs uptime time                      process count, hours up, the clock
    speedtest                              a real download/upload/ping test
    url:<https…>#<json.path>               any number in any JSON API

Drawing is core/hud_widgets.py; this file only decides what to show.
"""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path
import threading
import time
from collections import deque

_player = None
_widgets: dict[str, dict] = {}     # id → {"source", "refresh", "next", "hist", "type"}
_lock = threading.Lock()
_feeder: threading.Thread | None = None
_n = 0

SOURCES = {
    "cpu": ("%", 0, 100), "ram": ("%", 0, 100), "gpu": ("%", 0, 100), "temp": ("°C", 20, 100),
    "disk": ("%", 0, 100), "battery": ("%", 0, 100), "net_down": ("Mbps", 0, 100),
    "net_up": ("Mbps", 0, 50), "ping": ("ms", 0, 200), "procs": ("", 0, 500),
    "uptime": ("h", 0, 48), "time": ("", None, None), "speedtest": ("Mbps", None, None),
    "wifi": ("%", 0, 100), "phone_battery": ("%", 0, 100), "phone_wifi": ("%", 0, 100),
    # hardware sensors (core/sensors.py — LibreHardwareMonitor / NVML)
    "cpu_temp": ("°C", 20, 95), "gpu_temp": ("°C", 20, 95), "gpu_hotspot": ("°C", 20, 105),
    "ram_temp": ("°C", 20, 85), "cpu_power": ("W", 0, 120), "cpu_clock": ("MHz", 0, 6000),
    "gpu_load": ("%", 0, 100), "gpu_vram": ("MB", 0, 4096), "gpu_clock": ("MHz", 0, 2000),
    "gpu_fan": ("%", 0, 100), "gpu_power": ("W", 0, 300),
}
_extra: dict[str, dict] = {}       # source → extra fields from its last reading (e.g. charging)


# ── reading sources ──────────────────────────────────────────────────────────

_net_last = None


def _net_rates() -> tuple[float, float]:
    """(down, up) in Mbps since the last call."""
    global _net_last
    import psutil
    c = psutil.net_io_counters()
    now = time.monotonic()
    if _net_last is None:
        _net_last = (now, c.bytes_recv, c.bytes_sent)
        time.sleep(0.5)
        return _net_rates()
    t0, r0, s0 = _net_last
    _net_last = (now, c.bytes_recv, c.bytes_sent)
    dt = max(1e-3, now - t0)
    return (c.bytes_recv - r0) * 8 / dt / 1e6, (c.bytes_sent - s0) * 8 / dt / 1e6


def _ping_ms(host: str = "1.1.1.1", port: int = 443, tries: int = 3) -> float | None:
    best = None
    for _ in range(tries):
        t = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=2):
                ms = (time.perf_counter() - t) * 1000
                best = ms if best is None else min(best, ms)
        except OSError:
            pass
    return best


def _pc_wifi() -> float:
    """The PC's Wi-Fi signal (%). Windows only answers this with Location on."""
    import subprocess
    out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True,
                         timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    text = out.decode("cp850", "replace")
    import re
    m = re.search(r"(?:Sinal|Signal)\s*:\s*(\d+)\s*%", text)
    if not m:
        raise ValueError("Wi-Fi do PC indisponível (ligue a Localização do Windows)")
    ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", text, re.M)
    _extra["wifi"] = {"subtitle": ssid.group(1).strip() if ssid else ""}
    return float(m.group(1))


_SVG_BLOCK = re.compile(r"<\s*(script|foreignObject|image|iframe|use)\b.*?(/>|</\s*\1\s*>)", re.S | re.I)


def _clean_svg(svg: str) -> str:
    """The assistant's own drawing, minus anything that could reach outside
    it (scripts, external images/links). QtSvg wouldn't run a script anyway."""
    svg = svg.strip()
    if svg.startswith("```"):
        svg = svg.strip("`").split("\n", 1)[-1]
    svg = _SVG_BLOCK.sub("", svg[:30000])
    svg = re.sub(r"\son\w+\s*=\s*\"[^\"]*\"", "", svg)
    svg = re.sub(r"(xlink:)?href\s*=\s*\"(?!#)[^\"]*\"", "", svg)
    if "xmlns" not in svg[:200]:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    return svg


# ── drawing anything: a text model writes the SVG ────────────────────────────
# The voice model only says WHAT to draw; a Gemini text model, much better at
# vector drawing, writes it in the background while a placeholder hologram
# "materialises". Drawings are kept, so asking again is instant.

# Measured: 3.5-flash-lite draws a clean drone or Eiffel Tower in 5–7 s;
# 3.6-flash adds detail but takes ~35 s (it thinks first) and is often busy.
_DRAW_MODELS = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash",
                "gemini-3.6-flash", "gemini-flash-latest")
_DRAW_MODELS_DETAILED = ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite",
                         "gemini-flash-latest")
_DRAW_DIR = Path(__file__).resolve().parent.parent / "memory" / "holo_cache"
_DRAW_SYSTEM = """You draw holograms for a sci-fi HUD (the film's J.A.R.V.I.S style).
Output ONE complete SVG and nothing else — no markdown, no explanation.

Rules:
- <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"> … </svg>, transparent background.
- Line art: main outlines in #ffffff, secondary detail in #8a8a8a (it shows as dimmer light),
  stroke-width 1.2–3.5, stroke-linecap/linejoin round, fill="none" except small solid accents.
- Make the subject instantly recognisable from its silhouette, then add technical detail:
  panel lines, inner structure, a few hatch lines, small circles/rivets — like a blueprint or
  wireframe. Use symmetry where the subject is symmetric. Fill most of the viewBox (margin ~6).
- A coloured stroke (e.g. #ff4d6a red, #3dffb0 green, #ffd24a yellow) only for a part that
  must be coloured (a warning light, a flame…). No text unless asked.
- Allowed elements: path, line, polyline, polygon, circle, ellipse, rect, g (with transform).
  No gradients, filters, masks, images, scripts, <use>, <text> (unless asked), CSS, or fonts.
- At most ~7000 characters."""


def _draw_key(prompt: str, image: str | None = None) -> str:
    import hashlib
    h = hashlib.sha1(" ".join(prompt.lower().split()).encode("utf-8"))
    if image:
        try:
            h.update(Path(image).read_bytes())
        except OSError:
            h.update(image.encode("utf-8"))
    return h.hexdigest()[:16]


# ── real images: projected as they are, or traced into a drawing ─────────────

_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def _resolve_image(ref: str) -> str:
    """'current' (the file on the HUD's drop zone), an http(s) URL (downloaded
    to the cache) or a local path → a local image file. Raises ValueError."""
    import hashlib
    r = (ref or "").strip().strip('"')
    if r.lower().startswith("url:"):
        r = r[4:].strip()
    if r.lower() in ("", "current", "atual", "arquivo", "upload", "last", "ultimo", "último"):
        cur = getattr(_player, "current_file", None) if _player else None
        if not cur:
            raise ValueError("no image is loaded on the HUD's file area — drop one there first")
        r = str(cur)
    if r.lower().startswith(("http://", "https://")):
        import requests
        resp = requests.get(r, timeout=20, headers={"User-Agent": "Mozilla/5.0 MARK-HUD"}, stream=True)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "image" not in ctype:
            raise ValueError(f"that link is not an image ({ctype or 'unknown type'})")
        data = resp.raw.read(20 * 1024 * 1024 + 1, decode_content=True)
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("image larger than 20 MB")
        ext = "." + (ctype.split("/")[-1].split(";")[0].replace("jpeg", "jpg") or "png")
        _DRAW_DIR.mkdir(parents=True, exist_ok=True)
        f = _DRAW_DIR / f"img_{hashlib.sha1(r.encode()).hexdigest()[:16]}{ext}"
        f.write_bytes(data)
        return str(f)
    f = Path(r).expanduser()
    if not f.exists():
        raise ValueError(f"image not found: {r}")
    if f.suffix.lower() not in _IMG_EXT:
        raise ValueError(f"not an image file: {f.name}")
    return str(f)


def _image_payload(path: str) -> tuple[bytes, str]:
    """The picture for a vision model: at most 1024 px, JPEG."""
    import io
    from PIL import Image
    im = Image.open(path)
    im.thumbnail((1024, 1024))
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, (0, 0, 0))
        bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
        im = bg
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=88)
    return buf.getvalue(), "image/jpeg"


def _draw_cached(prompt: str, image: str | None = None) -> str | None:
    f = _DRAW_DIR / f"{_draw_key(prompt, image)}.svg"
    try:
        return f.read_text(encoding="utf-8") if f.exists() else None
    except OSError:
        return None


def _draw_svg(prompt: str, detailed: bool = False, image: str | None = None) -> str:
    """Ask a model for the drawing; returns cleaned SVG (raises on failure).
    With `image`, the model SEES the reference and traces it."""
    pic = _image_payload(image) if image else None
    ask = (f"Trace the attached reference image as a hologram line drawing: keep its exact "
           f"composition, silhouette, proportions and distinctive details. {prompt}"
           if pic else f"Draw: {prompt}")
    # GPT first when configured (strong at SVG), then the Gemini ladder
    try:
        from core import openai_brain
        if openai_brain.enabled():
            deep, fast = openai_brain.pick_models()
            text = openai_brain.respond(_DRAW_SYSTEM, ask, deep if (detailed or pic) else fast,
                                        effort="medium" if detailed else "low", timeout=120,
                                        images=[pic] if pic else None)
            m = re.search(r"<svg\b.*?</svg>", text, re.S | re.I)
            if m:
                svg = _clean_svg(m.group(0))
                _DRAW_DIR.mkdir(parents=True, exist_ok=True)
                (_DRAW_DIR / f"{_draw_key(prompt, image)}.svg").write_text(svg, encoding="utf-8")
                print(f"[HUD] 🎨 drew '{prompt[:40]}' with {deep if (detailed or pic) else fast} (OpenAI)"
                      + (" from the reference image" if pic else ""))
                return svg
    except Exception as e:
        print(f"[HUD] OpenAI drawing failed ({str(e)[:100]}) — using Gemini")
    from core import gemini
    from google.genai import types
    key = gemini.api_key()
    if not key:
        raise RuntimeError("no Gemini key")
    cl = gemini.client(timeout_ms=45_000, key=key)
    cfg = types.GenerateContentConfig(system_instruction=_DRAW_SYSTEM, temperature=0.6)
    last = "no model answered"
    ladder = _DRAW_MODELS_DETAILED if detailed else _DRAW_MODELS
    for model in [m for m in ladder if not gemini._cooling(m)] or list(ladder):
        try:
            contents = ([types.Part.from_bytes(data=pic[0], mime_type=pic[1]), ask] if pic else ask)
            resp = cl.models.generate_content(model=model, contents=contents, config=cfg)
            text = resp.text or ""
            m = re.search(r"<svg\b.*?</svg>", text, re.S | re.I)
            if not m:
                raise RuntimeError("the answer had no SVG")
            svg = _clean_svg(m.group(0))
            from core.hud_holo import QSvgRenderer
            if QSvgRenderer is not None:
                from PyQt6.QtCore import QByteArray
                if not QSvgRenderer(QByteArray(svg.encode("utf-8"))).isValid():
                    raise RuntimeError("the SVG did not parse")
            _DRAW_DIR.mkdir(parents=True, exist_ok=True)
            (_DRAW_DIR / f"{_draw_key(prompt, image)}.svg").write_text(svg, encoding="utf-8")
            print(f"[HUD] 🎨 drew '{prompt[:40]}' with {model} ({len(svg)} chars)")
            return svg
        except Exception as e:
            msg = str(e)
            last = f"{model}: {msg[:100]}"
            if gemini.is_quota_error(msg):
                m2 = re.search(r"retryDelay'?:\s*'(\d+)", msg)
                gemini._cool(model, min(300, int(m2.group(1)) + 1) if m2 else 300)
            elif gemini.is_gone_error(msg):
                gemini._cool(model, gemini._GONE_SECONDS)
            elif gemini.is_unavailable_error(msg):
                gemini._cool(model, gemini._UNAVAILABLE_SECONDS)
            print(f"[HUD] draw failed on {last}")
    raise RuntimeError(last)


def _short_title(prompt: str) -> str:
    """'Moeda do Poder dourada do Ranger Vermelho com símbolo de Tiranossauro,
    estilo realista' → 'Moeda do Poder dourada do Ranger Vermelho'."""
    t = re.split(r",|;|\.|\s-\s|\s(?:com|em estilo|no estilo|estilo|with|in the style)\s",
                 prompt.strip(), maxsplit=1, flags=re.I)[0].strip()
    if len(t) > 60:
        t = t[:60].rsplit(" ", 1)[0]
    return (t[:1].upper() + t[1:]) if t else "Holograma"


# ── realistic holograms: an image model renders the subject as light ─────────
# A render of a glowing blue projection on pure black: the HUD turns the black
# into nothing and the rest into light (core/hud_holo.HoloSprite._light).
# Measured with gpt-image-2: quality low 13 s, medium 29 s (for "detailed").

_GEMINI_IMAGE_MODELS = ("gemini-2.5-flash-image", "gemini-3-pro-image")
_GENERIC_USED: set[str] = set()     # renders that are a generic stand-in (refused subject)


def _render_prompt(subject: str, natural_colors: bool, traced: bool) -> str:
    # In colour (the default): the subject's real colours and materials, lit as
    # a projection — luminous, faintly translucent. Blue: the classic hologram.
    if natural_colors:
        look = ("a full-colour holographic projection, in its true natural colours and materials, "
                "vivid and saturated, luminous and slightly translucent, with a soft glow and a thin "
                "light-blue holographic rim light")
    else:
        look = "a translucent glowing blue holographic projection, soft cyan rim light"
    if traced:
        return (f"Turn the subject of this image into {look}: keep its exact shape, proportions, "
                f"colours and details. {subject}. Photorealistic 3D render, clean volumetric light, "
                f"fine surface detail, bright and well lit, the whole subject in frame, centered, on a "
                f"pure black background, no floor, no text.")
    return (f"{subject}, as {look}. Photorealistic 3D render, clean volumetric light, fine surface "
            f"detail, bright and well lit (no dark areas), centered, the whole subject in frame, on a "
            f"pure black background, no floor, no text")


def _render_cached(prompt: str, image: str | None = None, natural: bool = False) -> str | None:
    f = _DRAW_DIR / f"r_{_draw_key(('n|' if natural else '') + prompt, image)}.png"
    return str(f) if f.exists() else None


_GENERIC_SYSTEM = (
    "The image model refused a request because it names a copyrighted character, franchise, brand "
    "or real person. Write a description of an ORIGINAL design in the same general category (e.g. "
    "'a superhero armour' → some original sci-fi armour) that could not be mistaken for the "
    "referenced one: no names, no logos, and deliberately avoid that character's signature colour "
    "scheme and distinctive features — choose different colours, shapes and details. Describe only "
    "type, shape, materials, colours and mood. Output only the description, in English, one sentence.")


def _generic_subject(prompt: str) -> str:
    """The image model refuses named characters/brands: describe an original,
    generic version instead (the user is told it is a generic version)."""
    from core import openai_brain
    _deep, fast = openai_brain.pick_models()
    out = openai_brain.respond(_GENERIC_SYSTEM, prompt, fast, timeout=40).strip().strip('"')
    return out[:400] or "a generic futuristic object"


def _render_image(prompt: str, detailed: bool = False, image: str | None = None,
                  natural: bool = False) -> str:
    """The hologram as a realistic render → cached PNG path. GPT Image first,
    Gemini's image models next. Raises when neither can."""
    ask = _render_prompt(prompt, natural, bool(image))
    ref = _image_payload(image) if image else None
    out = _DRAW_DIR / f"r_{_draw_key(('n|' if natural else '') + prompt, image)}.png"
    errors = []
    try:
        from core import openai_brain
        if openai_brain.enabled() and openai_brain.pick_image_model():
            t0 = time.time()
            try:
                data = openai_brain.generate_image(ask, "medium" if detailed else "low", ref)
            except RuntimeError as e:
                if "safety" not in str(e).lower() and "rejected" not in str(e).lower():
                    raise
                # a named character / brand: render an original, generic version
                generic = _generic_subject(prompt)
                print(f"[HUD] image model refused '{prompt[:40]}' — generic version: {generic[:80]}")
                _GENERIC_USED.add(str(out))
                out.with_suffix(".generic").write_text(generic, encoding="utf-8")
                data = openai_brain.generate_image(_render_prompt(generic, natural, False),
                                                   "medium" if detailed else "low", None)
            _DRAW_DIR.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            print(f"[HUD] 🖼 rendered '{prompt[:40]}' with {openai_brain.pick_image_model()} "
                  f"in {time.time() - t0:.0f}s")
            return str(out)
    except Exception as e:
        errors.append(f"OpenAI: {str(e)[:100]}")
    try:
        from core import gemini
        from google.genai import types
        cl = gemini.client(timeout_ms=90_000)
        cfg = types.GenerateContentConfig(response_modalities=["IMAGE"])
        contents = [types.Part.from_bytes(data=ref[0], mime_type=ref[1]), ask] if ref else ask
        for model in _GEMINI_IMAGE_MODELS:
            if gemini._cooling(model):
                continue
            try:
                resp = cl.models.generate_content(model=model, contents=contents, config=cfg)
                for part in (resp.candidates[0].content.parts or []):
                    blob = getattr(part, "inline_data", None)
                    if blob is not None and blob.data:
                        _DRAW_DIR.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(blob.data)
                        print(f"[HUD] 🖼 rendered '{prompt[:40]}' with {model}")
                        return str(out)
                raise RuntimeError("no image in the answer")
            except Exception as e:
                msg = str(e)
                if gemini.is_quota_error(msg):
                    gemini._cool(model)
                errors.append(f"{model}: {msg[:80]}")
    except Exception as e:
        errors.append(str(e)[:80])
    raise RuntimeError("; ".join(errors) or "no image model available")


def _materialise(wid: str, prompt: str, spin: bool, detailed: bool = False,
                 image: str | None = None, realistic: bool = True, natural: bool = False) -> None:
    if realistic:
        try:
            path = _render_image(prompt, detailed, image, natural)
            note = ("versão genérica — personagens e marcas não são gerados"
                    if path in _GENERIC_USED or Path(path).with_suffix(".generic").exists() else "")
            _send("update", {"id": wid, "image": path, "render": True, "subtitle": note, "spin": spin})
            if note:
                _say("[SYSTEM] The image model does not render copyrighted characters or brands, so the "
                     "hologram shows an original, generic version of it. Tell the user in one short sentence.")
            return
        except Exception as e:
            print(f"[HUD] realistic render failed ({str(e)[:160]}) — drawing it as lines")
            _send("update", {"id": wid, "subtitle": "desenhando em linhas…"})
    try:
        svg = _draw_svg(prompt, detailed, image)
        _send("update", {"id": wid, "svg": svg, "subtitle": "", "spin": spin})
    except Exception as e:
        _send("update", {"id": wid, "subtitle": "não consegui desenhar agora"})
        _say(f"[SYSTEM] The hologram '{prompt}' could not be drawn ({str(e)[:80]}). "
             f"Tell the user briefly.")


def _json_path(data, path: str):
    for part in [p for p in path.replace("[", ".").replace("]", "").split(".") if p]:
        if isinstance(data, list):
            data = data[int(part)]
        else:
            data = data[part]
    return data


def read_source(name: str):
    """The current value of a source (number, or text for 'time')."""
    n = name.strip()
    low = n.lower()
    if low.startswith("url:"):
        import requests
        url, _, path = n[4:].partition("#")
        r = requests.get(url.strip(), timeout=8, headers={"User-Agent": "MARK-HUD/1.0"})
        r.raise_for_status()
        v = _json_path(r.json(), path.strip()) if path.strip() else r.json()
        try:
            return float(v)
        except (TypeError, ValueError):
            return str(v)
    if low in ("phone_battery", "phone_wifi"):
        from plugins import android_phone
        r = android_phone._call("battery" if low == "phone_battery" else "wifi", {}, 10)
        if not r.get("ok"):
            raise ValueError(str(r.get("result") or "celular offline")[:60])
        d = r.get("data") or {}
        if low == "phone_battery":
            _extra[low] = {"charging": bool(d.get("charging"))}
        else:
            _extra[low] = {"subtitle": str(d.get("ssid") or "")}
        return d.get("percent")
    if low == "wifi":
        return _pc_wifi()
    if low.startswith("hw:"):
        from core import hwinfo
        r = hwinfo.best(n[3:])
        if r is None:
            raise ValueError("leitura do HWiNFO não encontrada")
        return r["value"]
    from core import sensors
    if low in sensors.VALUES and low not in ("cpu_load", "ram_load", "ram_used"):
        v = sensors.value(low)
        if v is None:
            raise ValueError("sensor indisponível (sensores de temperatura desligados)"
                             if low in ("cpu_temp", "ram_temp") else "sensor indisponível")
        return v
    import psutil
    if low == "cpu":
        return psutil.cpu_percent(interval=0.3)
    if low == "ram":
        return psutil.virtual_memory().percent
    if low == "disk":
        import os
        return psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").percent
    if low == "battery":
        b = psutil.sensors_battery()
        if b:
            _extra[low] = {"charging": bool(b.power_plugged)}
        return b.percent if b else None
    if low in ("gpu", "temp"):
        try:
            import ui
            m = ui._metrics
            v = m.gpu if low == "gpu" else m.tmp
            return v if v is not None and v >= 0 else None
        except Exception:
            return None
    if low in ("net_down", "net_up"):
        d, u = _net_rates()
        return d if low == "net_down" else u
    if low == "ping":
        return _ping_ms()
    if low == "procs":
        return len(psutil.pids())
    if low == "uptime":
        return (time.time() - psutil.boot_time()) / 3600
    if low == "time":
        return time.strftime("%H:%M:%S")
    raise ValueError(f"unknown source '{name}'")


# ── internet speed test (Cloudflare's public speed endpoints) ────────────────

def _speedtest(wid: str) -> None:
    import requests
    send = _send
    try:
        send("update", {"id": wid, "phase": "ping…"})
        ping = _ping_ms("speed.cloudflare.com", 443, 5)
        send("update", {"id": wid, "ping": ping or 0, "phase": "download…"})

        # download: 25 MB pieces (Cloudflare refuses bigger ones with a 403)
        # for up to ~8 s, reporting the running rate
        t0 = time.perf_counter()
        got, last = 0, t0
        while time.perf_counter() - t0 < 8 and wid in _widgets:
            with requests.get("https://speed.cloudflare.com/__down?bytes=25000000", stream=True,
                              timeout=10) as r:
                r.raise_for_status()
                for chunk in r.iter_content(256 * 1024):
                    got += len(chunk)
                    now = time.perf_counter()
                    if now - last > 0.25:
                        send("update", {"id": wid, "down": got * 8 / (now - t0) / 1e6})
                        last = now
                    if now - t0 > 8 or wid not in _widgets:
                        break
        down = got * 8 / max(1e-3, time.perf_counter() - t0) / 1e6
        send("update", {"id": wid, "down": down, "phase": "upload…"})

        # upload: push ~25 MB in pieces, measuring as it goes
        piece = b"0" * (512 * 1024)
        state = {"sent": 0, "t0": time.perf_counter(), "last": 0.0}

        def body():
            while state["sent"] < 25 * 1024 * 1024 and time.perf_counter() - state["t0"] < 8:
                state["sent"] += len(piece)
                now = time.perf_counter()
                if now - state["last"] > 0.25:
                    send("update", {"id": wid, "up": state["sent"] * 8 / (now - state["t0"]) / 1e6})
                    state["last"] = now
                yield piece
        requests.post("https://speed.cloudflare.com/__up", data=body(), timeout=20)
        up = state["sent"] * 8 / max(1e-3, time.perf_counter() - state["t0"]) / 1e6
        send("update", {"id": wid, "up": up, "phase": "concluído"})
        with _lock:
            if wid in _widgets:
                _widgets[wid]["result"] = {"down": round(down, 1), "up": round(up, 1),
                                           "ping": round(ping or 0)}
        _say(f"[SYSTEM] Internet speed test finished: download {down:.1f} Mbps, upload "
             f"{up:.1f} Mbps, ping {ping or 0:.0f} ms. Tell the user in one short sentence.")
    except Exception as e:
        send("update", {"id": wid, "phase": "falhou"})
        _say(f"[SYSTEM] The internet speed test failed ({type(e).__name__}). Tell the user briefly.")


# ── plumbing ─────────────────────────────────────────────────────────────────

def _send(op: str, payload: dict) -> None:
    fn = getattr(_player, "hud_widget", None) if _player else None
    if callable(fn):
        fn(op, payload)


def _say(text: str) -> None:
    fn = (getattr(_player, "request_say_idle", None) or getattr(_player, "request_say", None)) if _player else None
    if callable(fn):
        fn(text)


def _feed_loop() -> None:
    while True:
        time.sleep(0.5)
        try:
            on_screen = set(_player.hud_widget_ids()) if _player and hasattr(_player, "hud_widget_ids") else None
        except Exception:
            on_screen = None
        now = time.monotonic()
        with _lock:
            if on_screen is not None:        # closed with ✕ on the HUD
                for k in [k for k in _widgets if k not in on_screen and now - _widgets[k]["born"] > 3]:
                    _widgets.pop(k, None)
            due = [(k, dict(w)) for k, w in _widgets.items()
                   if w.get("source") and w["source"] != "speedtest" and now >= w["next"]]
            for k, _w in due:
                _widgets[k]["next"] = now + _widgets[k]["refresh"]
        for k, w in due:
            try:
                v = read_source(w["source"])
            except Exception as e:
                _send("update", {"id": k, "subtitle": f"fonte indisponível: {str(e)[:40]}"})
                continue
            if v is None:
                continue
            upd = {"id": k, **_extra.get(w["source"].lower(), {})}
            if w["type"] in ("line", "columns"):
                with _lock:
                    h = _widgets.get(k, {}).get("hist")
                    if h is None:
                        continue
                    h.append(float(v) if not isinstance(v, str) else 0.0)
                    upd["values"] = list(h)
            elif isinstance(v, str):
                upd["value"] = v
            else:
                upd["value"] = float(v)
            _send("update", upd)


def _ensure_feeder() -> None:
    global _feeder
    if _feeder is None or not _feeder.is_alive():
        _feeder = threading.Thread(target=_feed_loop, daemon=True, name="hud-feeder")
        _feeder.start()


def _floats(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = [x for x in v.replace(";", ",").split(",") if x.strip()]
    out = []
    for x in v if isinstance(v, (list, tuple)) else [v]:
        try:
            out.append(float(str(x).replace(",", ".")) if isinstance(x, str) else float(x))
        except (TypeError, ValueError):
            pass
    return out


def _strings(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = [x.strip() for x in v.split("|" if "|" in v else ",") if x.strip()]
    return [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]


def run(parameters: dict, player=None, session_memory=None) -> str:
    global _player, _n
    if player is not None:
        _player = player
    if _player is None or not hasattr(_player, "hud_widget"):
        return "The HUD is not available."
    p = dict(parameters or {})
    action = str(p.pop("action", "show") or "show").strip().lower()
    wid = str(p.pop("id", "") or "").strip()

    if action == "clear":
        with _lock:
            _widgets.clear()
        _send("clear", {})
        return "Cleared every indicator from the HUD."
    if action == "list":
        with _lock:
            items = [f"{k} ({w['type']}{', source ' + w['source'] if w.get('source') else ''})"
                     for k, w in _widgets.items()]
        return ("On the HUD: " + "; ".join(items)) if items else "No indicators on the HUD."
    if action == "target":
        v = str(p.get("value") or p.get("target") or "").strip().lower()
        tgt = "hud" if v in ("hud", "janela", "window", "mark") else "screen"
        try:
            from memory.config_manager import save_plugin_config
            save_plugin_config("hud_display", {"target": tgt})
        except Exception:
            pass
        return ("Projections now appear over the whole screen, on top of any program."
                if tgt == "screen" else "Projections now appear inside the MARK window's HUD.")
    if action == "shapes":
        from core.hud_holo import SHAPES
        return "Hologram shapes: " + ", ".join(SHAPES) + " — or any SVG you write (param svg)."
    if action == "sources":
        return ("Live sources: " + ", ".join(SOURCES) +
                ", and url:<https URL returning JSON>#<path.to.number> for any API.")
    if action == "remove":
        if not wid:
            return "Which indicator (id)? Use action 'list'."
        with _lock:
            _widgets.pop(wid, None)
        _send("remove", {"id": wid})
        return f"Removed {wid}."
    if action == "result":
        with _lock:
            r = (_widgets.get(wid) or {}).get("result")
        return json.dumps(r) if r else "No result yet."

    # show / update
    if action == "update" and wid and wid not in _widgets:
        action = "show"            # "update" of a card that was never shown: make it
    wtype = str(p.get("type") or "").strip().lower()
    if wtype in ("hologram", "holograma", "holografia"):
        wtype = "holo"
    if wtype in ("image", "foto", "imagem", "picture", "picture_card"):
        wtype = "photo"
    source = str(p.get("source") or "").strip()
    from core.hud_widgets import TYPES as _TYPES
    if wtype and wtype not in _TYPES:
        return (f"Unknown type '{wtype}'. Types: {', '.join(_TYPES)} (photo = a real picture, "
                f"holo = a hologram). Nothing was shown — call again with one of these.")
    # a real picture: shown as it is in a card (photo, the default), projected
    # as a hologram (type holo), or traced (draw + image)
    image_ref = p.get("image")
    if wtype == "photo" and not image_ref and source:
        image_ref, source = source, ""
    image_path = None
    as_photo = False
    if image_ref or wtype == "photo":
        try:
            image_path = _resolve_image(str(image_ref or "current"))
        except Exception as e:
            return f"Could not get the image: {e}. Nothing was shown."
        if not p.get("draw"):
            as_photo = wtype != "holo"
            wtype = "photo" if as_photo else "holo"
    if source.lower() == "speedtest" or wtype == "speedtest":
        wtype, source = "speedtest", "speedtest"
    # Links: 'url' opens when the card is clicked. A link sent as the value or
    # inside the text (models do that) becomes the card's link instead of
    # being shown as a long string.
    url = str(p.get("url") or p.get("link") or "").strip()
    v0 = p.get("value")
    if not url and isinstance(v0, str) and re.fullmatch(r"\s*https?://\S+\s*", v0):
        url, p["value"] = v0.strip(), None
    if not url and isinstance(p.get("text"), str):
        m = re.search(r"https?://[^\s<>\"']+", p["text"])
        if m:
            url = m.group(0).rstrip(".,;)")
    if url and not url.startswith(("http://", "https://")):
        url = ""
    spec = {
        "type": wtype or None,
        "url": url or None,
        "title": p.get("title"), "unit": p.get("unit"), "subtitle": p.get("subtitle"),
        "color": p.get("color"), "side": p.get("side"), "text": p.get("text"),
        "min": p.get("min"), "max": p.get("max"), "decimals": p.get("decimals"),
        "warn": p.get("warn"), "crit": p.get("crit"), "low_is_bad": p.get("low_is_bad"),
        "trend": p.get("trend"), "ttl": p.get("ttl"), "multicolor": p.get("multicolor"),
        "shape": p.get("shape"), "size": p.get("size"), "spin": p.get("spin"),
        "colors": p.get("colors"), "charging": p.get("charging"),
    }
    if p.get("svg"):
        spec["svg"] = _clean_svg(str(p["svg"]))
    if image_path and as_photo:
        spec["image"] = image_path
        spec["type"] = "photo"
        spec["title"] = spec["title"] or Path(image_path).stem.replace("_", " ")[:40]
        spec["hide_value"] = True
    elif image_path and not p.get("draw"):
        spec["image"] = image_path
        spec["type"] = "holo"
        spec["title"] = spec["title"] or Path(image_path).stem.replace("_", " ")[:40]
        spec["hide_value"] = True
        if not spec.get("side") and p.get("stage") is not False:
            spec["stage"] = True
    draw = str(p.get("draw") or "").strip()
    pending_draw = False
    style = str(p.get("style") or "realistic").strip().lower()
    realistic = style not in ("line", "lines", "linhas", "vetorial", "vector", "wireframe", "svg")
    # realistic holograms are in colour unless the classic blue one is asked for
    natural = str(p.get("colors") or "").strip().lower() not in ("blue", "azul", "mono", "classic",
                                                                 "classico", "clássico")
    if draw and not spec.get("svg"):
        cached_png = _render_cached(draw, image_path, natural) if realistic else None
        cached = None if (realistic and not cached_png) else _draw_cached(draw, image_path)
        if cached_png:
            spec.update({"image": cached_png, "render": True})
            if Path(cached_png).with_suffix(".generic").exists():
                spec["subtitle"] = "versão genérica — personagens e marcas não são gerados"
        elif cached and not realistic:
            spec["svg"] = cached
        else:                 # a placeholder materialises while the drawing is made
            spec.update({"shape": "atom", "subtitle": "materializando…"})
            pending_draw = True
        spec["title"] = spec["title"] or _short_title(draw)
        # drawings take the centre stage unless a side was asked for
        if not spec.get("side") and p.get("stage") is not False:
            spec["stage"] = True
        spec["hide_value"] = spec.get("value") is None and not source
    if not as_photo and (spec["shape"] or spec.get("svg") or spec.get("image") or draw):
        wtype = wtype if wtype and wtype != "number" else "holo"
        spec["type"] = "holo"
    if spec["shape"]:
        from core.hud_holo import shape_svg, SHAPES
        if not shape_svg(str(spec["shape"])) and not spec.get("svg"):
            return (f"No ready-made shape '{spec['shape']}'. Shapes: {', '.join(SHAPES)} — or draw "
                    f"it yourself and pass the SVG in 'svg'.")
    vals = _floats(p.get("values"))
    if vals:
        spec["values"] = vals
    if p.get("value") is not None:
        v = p.get("value")
        try:
            spec["value"] = float(str(v).replace(",", ".")) if isinstance(v, str) else float(v)
        except (TypeError, ValueError):
            spec["value"] = str(v)
    labels = _strings(p.get("labels"))
    if labels:
        spec["labels"] = labels
    items = _strings(p.get("items"))
    if items:
        spec["items"] = items

    if action == "update":
        if not wid:
            return "Which indicator to update (id)?"
        _send("update", {"id": wid, **{k: v for k, v in spec.items() if v is not None}})
        return f"Updated {wid}."

    # sensible defaults from the source
    if source and source.lower().startswith("hw:"):
        try:
            from core import hwinfo
            r = hwinfo.best(source[3:])
        except Exception:
            r = None
        if r is not None:
            lo, hi = {"temperature": (20, 100), "usage": (0, 100), "fan": (0, 3000),
                      "power": (0, 200), "clock": (0, 6000)}.get(r["type"], (None, None))
            spec["unit"] = spec["unit"] if spec["unit"] is not None else r["unit"]
            spec["title"] = spec["title"] or r["label"]
            if spec["min"] is None and lo is not None:
                spec["min"] = lo
            if spec["max"] is None and hi is not None:
                spec["max"] = hi
    if source and source.lower() in SOURCES:
        unit, lo, hi = SOURCES[source.lower()]
        spec["unit"] = spec["unit"] if spec["unit"] is not None else unit
        if spec["min"] is None and lo is not None:
            spec["min"] = lo
        if spec["max"] is None and hi is not None:
            spec["max"] = hi
    if not spec["type"]:
        spec["type"] = ("line" if vals and len(vals) > 3 else "list" if items else
                        "text" if spec["text"] else "gauge" if source and spec["max"] is not None else "number")
    if spec["type"] == "speedtest":
        spec.setdefault("title", None)
        spec["title"] = spec["title"] or "Velocidade da internet"
        spec.update({"down": 0, "up": 0, "ping": 0, "phase": "iniciando…"})

    with _lock:
        _n += 1
        wid = wid or f"w{_n}"
        if spec.get("stage"):           # one hologram on stage: the previous one steps aside
            for k, w in _widgets.items():
                if w.get("stage") and k != wid:
                    w["stage"] = False
                    _send("update", {"id": k, "stage": False, "size": "m"})
        refresh = max(1.0, float(p.get("refresh") or (30 if source.lower().startswith("url:") else 2)))
        _widgets[wid] = {"source": source, "refresh": refresh, "next": 0.0, "type": spec["type"],
                         "stage": bool(spec.get("stage")),
                         "hist": deque(vals or [], maxlen=int(p.get("history") or 60)), "born": time.monotonic()}
    _send("show", {"id": wid, **{k: v for k, v in spec.items() if v is not None},
                   **({"spin": True} if pending_draw else {})})
    if pending_draw:
        threading.Thread(target=_materialise,
                         args=(wid, draw, bool(p.get("spin")), bool(p.get("detailed")), image_path,
                               realistic, natural),
                         daemon=True, name="hud-draw").start()
        secs = ("15–30" if realistic and not p.get("detailed") else "30–45") if realistic else "5–10"
        return (f"Creating '{draw}' as a hologram on the HUD (id {wid}) — it materialises in about "
                f"{secs} seconds. Tell the user it's being projected; don't describe it in detail.")
    if source == "speedtest":
        threading.Thread(target=_speedtest, args=(wid,), daemon=True, name="hud-speedtest").start()
        return (f"Speed test started on the HUD (id {wid}). It takes ~20 s; the result will be "
                f"announced by itself — just tell the user it's measuring.")
    if source:
        _ensure_feeder()
        return f"Showing '{spec.get('title') or source}' on the HUD (id {wid}), live from '{source}'."
    return f"Showing '{spec.get('title') or spec['type']}' on the HUD (id {wid})."


def start(player) -> None:
    global _player
    _player = player


PLUGIN = {
    "name": "hud_display",
    "description": (
        "REAL IMAGES: 'image' = a local file path, an http(s) image URL, or 'current' (the file "
        "on the HUD's file area). TWO WAYS TO SHOW IT: type photo (the DEFAULT) shows the real "
        "picture as it is, in a card, real colours — 'mostra a imagem / a foto', a screenshot, a "
        "document, a photo of people or a place, anything to read or look at. type holo PROJECTS it "
        "as a hologram — ONLY when the user asks for a hologram/projection, or when you judge it "
        "fits: a single object worth admiring (a car, a gadget, a building, an anatomy model), never "
        "screenshots, text or photos of people. With 'draw' the drawing model SEES it and traces it — "
        "for 'make a hologram like this image'. size s|m|l|xl sets a photo's height. Never describe "
        "an image you can't see — pass it. "
        "DRAW ANYTHING (realistic and IN COLOUR by default — a photorealistic render of the "
        "subject's real colours shown as a hologram; colors 'blue' for the classic blue hologram; "
        "style 'line' for vector line art): pass 'draw' = a short "
        "description of it (e.g. 'drone quadricóptero', 'Torre Eiffel', 'coração anatômico', "
        "'mapa do Brasil') — a drawing model creates it; prefer this over writing SVG yourself. "
        "WHERE: by default everything is projected over the WHOLE COMPUTER SCREEN, on top of any "
        "open program (only the projection itself is drawn; clicks pass through); action 'target' "
        "value hud keeps projections inside the MARK window, value screen goes back. "
        "HOLOGRAMS: draw anything on the HUD as a hologram (blue light, glow, scanlines, floating "
        "over a projector beam): shape = wifi | signal | battery | thermometer | globe | cpu | heart "
        "| sun | cloud | rain | bolt | house | lock | warning | check | rocket | atom | car — these "
        "react to value/source (wifi bars, battery fill, thermometer level); OR draw ANYTHING else "
        "yourself: pass a complete SVG in 'svg' (viewBox 0 0 100 100, white strokes/fills on "
        "transparent — brightness becomes light; grey for dim parts). SVG may embed live values as "
        "{{ pct * 0.6 }} or {{ 'red' if pct < 20 else 'white' }} (names: value, pct, min, max, "
        "charging, on(n)). size s|m|l|xl, spin true to turn it in 3-D, colors 'original' to keep "
        "the SVG's own colours. E.g. phone battery → shape battery + source phone_battery; Wi-Fi → "
        "shape wifi + source wifi (PC) or phone_wifi. "
        "CARDS: Draw ANY indicator on the HUD, right when asked — 'show me X', 'put Y on screen', "
        "'make a chart of…'. action: show (default) | update | remove | clear | list | sources. "
        "type: gauge (dial, needs min/max) | number (big readout, optional trend %) | bar (one or "
        "more bars: values + labels) | line (chart: values, or a live source's history) | columns "
        "(vertical bars: values + labels) | donut (shares: values + labels) | list (items) | table "
        "(items as 'label: value') | text | speedtest (real internet speed test, live). Fill "
        "value/values/labels/items from what you know or just looked up (web_search etc.). To keep "
        "it LIVE give a source: cpu, ram, gpu, temp, disk, battery, net_down, net_up, ping, procs, "
        "uptime, time, speedtest, wifi, phone_battery, phone_wifi, cpu_temp, gpu_temp, gpu_hotspot, "
        "ram_temp, cpu_power, cpu_clock, gpu_load, gpu_vram, gpu_clock, gpu_fan, or url:<https JSON API>#<path.to.number> (e.g. currency or crypto "
        "prices) with refresh seconds. Optional: title, unit, min, max, decimals, color "
        "(blue|cyan|green|yellow|orange|red|purple|pink or #hex), warn/crit thresholds, side "
        "left|right, ttl seconds. Internet speed → type speedtest. LINKS: 'url' makes the card "
        "clickable (opens in the browser — e.g. a product or store page); in list/table, an item "
        "that contains a URL ('Kabum — R$ 9.999 https://…') becomes a clickable row, one per store. "
        "Keep titles short, in the user's "
        "language. Several indicators can be on screen at once; give each an id to update it later. "
        "NEVER make a card for the agenda / calendar / events: the HUD already shows the whole "
        "month's agenda permanently and refreshes it by itself after any change. Never invent a "
        "url source (no placeholders such as YOUR_TOKEN) — use url only with a real public JSON API."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "show | update | remove | clear | list | sources | shapes | target"},
            "id": {"type": "STRING", "description": "Short id, e.g. 'cpu' or 'dolar' (to update/remove)"},
            "type": {"type": "STRING", "description": "photo (a real picture, as it is) | holo (hologram) | gauge | number | bar | line | columns | donut | list | table | text | speedtest"},
            "title": {"type": "STRING", "description": "Short title shown on the card"},
            "value": {"type": "STRING", "description": "Single value (number, or text for 'number')"},
            "values": {"type": "ARRAY", "items": {"type": "NUMBER"}, "description": "Series for bar/line/columns/donut"},
            "labels": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Labels matching values"},
            "items": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Rows for list/table ('label: value')"},
            "text": {"type": "STRING", "description": "Paragraph for type text"},
            "url": {"type": "STRING", "description": "Link opened when the card is clicked (https://…)"},
            "unit": {"type": "STRING", "description": "Unit, e.g. Mbps, %, R$, °C"},
            "min": {"type": "NUMBER"}, "max": {"type": "NUMBER"},
            "decimals": {"type": "NUMBER"},
            "source": {"type": "STRING", "description": "Live data source (see description)"},
            "refresh": {"type": "NUMBER", "description": "Seconds between live updates"},
            "color": {"type": "STRING"},
            "warn": {"type": "NUMBER"}, "crit": {"type": "NUMBER"},
            "trend": {"type": "NUMBER", "description": "number: change in % (▲/▼)"},
            "subtitle": {"type": "STRING"},
            "side": {"type": "STRING", "description": "left | right"},
            "ttl": {"type": "NUMBER", "description": "Remove by itself after N seconds"},
            "shape": {"type": "STRING", "description": "Hologram shape (see description)"},
            "image": {"type": "STRING", "description": "Image path / URL / 'current': shown as a photo by default; type holo projects it; with draw it is traced"},
            "draw": {"type": "STRING", "description": "Describe anything to draw as a hologram "
                                                     "(shown big, centre stage)"},
            "detailed": {"type": "BOOLEAN", "description": "draw: higher quality, about twice as slow"},
            "style": {"type": "STRING", "description": "draw: realistic (default, a rendered hologram) | line (vector line art)"},
            "svg": {"type": "STRING", "description": "Your own SVG drawing for a hologram"},
            "size": {"type": "STRING", "description": "Hologram size: s | m | l | xl"},
            "spin": {"type": "BOOLEAN", "description": "Turn the hologram in 3-D"},
            "colors": {"type": "STRING", "description": "draw: colour (default) | blue (classic hologram); svg: 'original' keeps its colours"},
            "charging": {"type": "BOOLEAN", "description": "battery shape: show the charging bolt"},
        },
        "required": [],
    },
}


PLUGIN_SETTINGS = {
    "namespace": "hud_display",
    "title": "Projeções — hologramas e indicadores",
    "fields": [
        {"key": "target", "label": "Onde projetar", "type": "choice", "options": ["screen", "hud"]},
    ],
}
