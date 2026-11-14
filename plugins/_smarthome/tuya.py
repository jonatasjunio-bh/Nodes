"""
i2GO — and every other Tuya-based brand — over the LAN, through tinytuya.

i2GO's "i2GO HOME" app is Tuya's OEM app, so its plugs, bulbs and switches are
Tuya devices underneath. That means:

  Discovery   every Tuya device broadcasts a small UDP packet every few
              seconds (ports 6666 / 6667 / 7000) carrying its device ID,
              protocol version and IP. Listening for those is how the scan
              finds them, firewall or not.
  Control     local TCP on 6668, encrypted with the device's own "local key".
  Keys        the local key exists only in the Tuya cloud. It is fetched once
              from a free Tuya IoT developer project linked to the user's
              account (cloud_sync); from then on everything is local.
"""

from __future__ import annotations

import json
import select
import socket
import time

# Tuya product categories that are lights (have brightness / colour).
_LIGHTS = {"dj", "dd", "xdd", "fwd", "dc", "tgq", "tgkg"}
_CATEGORY_LABEL = {
    "dj": "light", "dd": "LED strip", "xdd": "ceiling light", "fwd": "ambient light",
    "dc": "string lights", "cz": "smart plug", "pc": "power strip", "kg": "switch",
    "tgq": "dimmer", "tgkg": "dimmer switch", "fs": "fan", "sp": "camera",
    "cl": "curtain", "wk": "thermostat", "kt": "air conditioner", "ms": "lock",
}


def available() -> bool:
    try:
        import tinytuya  # noqa: F401
        return True
    except ImportError:
        return False


def label(category: str, product: str = "") -> str:
    what = _CATEGORY_LABEL.get(category or "", "device")
    return f"i2GO/Tuya {what}" + (f" ({product})" if product else "")


def listen(seconds: float = 6.0) -> dict[str, dict]:
    """ip → {"id", "version", "product_key"} for every Tuya device that
    broadcasts within `seconds` (they announce roughly every 5 s)."""
    if not available():
        return {}
    import tinytuya

    socks = []
    for port in (6666, 6667, 7000):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", port))
            socks.append(s)
        except OSError as e:
            print(f"[SmartHome] Tuya listener on {port} unavailable: {e}")
    found: dict[str, dict] = {}
    try:
        end = time.time() + seconds
        while socks and time.time() < end:
            ready, _, _ = select.select(socks, [], [], 0.3)
            for s in ready:
                try:
                    data, (src, _p) = s.recvfrom(4096)
                    raw = tinytuya.decrypt_udp(data)
                    info = json.loads(raw if isinstance(raw, str) else raw.decode())
                except Exception:
                    continue
                ip = info.get("ip") or src
                if info.get("gwId"):
                    found[ip] = {"id": info["gwId"], "version": str(info.get("version", "3.3")),
                                 "product_key": info.get("productKey", "")}
    finally:
        for s in socks:
            s.close()
    return found


def _open(dev: dict):
    import tinytuya
    t = dev.get("tuya", {})
    if not t.get("key"):
        raise PermissionError("no local key yet — say 'sync i2GO' first "
                              "(Tuya keys in ⚙ settings)")
    cls = tinytuya.BulbDevice if t.get("category") in _LIGHTS else tinytuya.OutletDevice
    d = cls(t["id"], dev["ip"], t["key"])
    d.set_version(float(t.get("version") or 3.3))
    d.set_socketTimeout(4)
    d.set_socketRetryLimit(1)
    return d


def _check(resp) -> str:
    if isinstance(resp, dict) and resp.get("Error"):
        err = str(resp.get("Error"))
        if "decrypt" in err.lower() or "key" in err.lower():
            return "the local key does not match — say 'sync i2GO' again"
        return err
    return ""


def _call(dev: dict, fn) -> str:
    """Run fn(device) and turn every failure into a sentence."""
    if not available():
        return "i2GO control needs the 'tinytuya' package (pip install tinytuya)"
    try:
        return _check(fn(_open(dev)))
    except PermissionError as e:
        return str(e)
    except Exception as e:
        return f"could not reach it at {dev.get('ip')} ({type(e).__name__}: {e})"


def set_power(dev: dict, on: bool) -> str:
    return _call(dev, lambda d: d.turn_on() if on else d.turn_off())


def toggle(dev: dict) -> tuple[str, bool | None]:
    st = {}
    err = _call(dev, lambda d: st.update(d.status() or {}) or st)
    if err:
        return err, None
    now = _is_on(st.get("dps", {}))
    if now is None:
        return "it did not report whether it is on", None
    return set_power(dev, not now), (not now)


def _is_on(dps: dict) -> bool | None:
    for k in ("1", "20"):              # plugs/switches use 1, most bulbs 20
        if isinstance(dps.get(k), bool):
            return dps[k]
    return None


def set_brightness(dev: dict, pct: int) -> str:
    if dev.get("tuya", {}).get("category") not in _LIGHTS:
        return "it is not a light, so it has no brightness"
    return _call(dev, lambda d: d.set_brightness_percentage(max(1, min(100, pct))))


def set_color(dev: dict, hexv: str) -> str:
    if dev.get("tuya", {}).get("category") not in _LIGHTS:
        return "it is not a light, so it has no colour"
    r, g, b = (int(hexv[i:i + 2], 16) for i in (0, 2, 4))
    if (r, g, b) == (255, 255, 255):
        return _call(dev, lambda d: d.set_white_percentage(100, 50))
    return _call(dev, lambda d: d.set_colour(r, g, b))


def status(dev: dict) -> tuple[str, str]:
    st = {}
    err = _call(dev, lambda d: st.update(d.status() or {}) or st)
    if err:
        return err, ""
    on = _is_on(st.get("dps", {}))
    return "", ("on" if on else "off") if on is not None else "online"


# ── Tuya cloud: fetch local keys, once ───────────────────────────────────────

REGIONS = ("us", "us-e", "eu", "eu-w", "in", "sg", "cn")


def cloud_sync(cfg: dict) -> tuple[str, list[dict]]:
    """List the devices of the Tuya IoT project the user linked to their app
    account, with their local keys. Returns ('', devices) or (reason, [])."""
    key = (cfg.get("tuya_api_key") or "").strip()
    secret = (cfg.get("tuya_api_secret") or "").strip()
    region = (cfg.get("tuya_region") or "us").strip().lower()
    if not (key and secret):
        return ("the Tuya account is not set up — fill in ⚙ → plugin settings → Smart "
                "Home (Access ID and Secret from your project at iot.tuya.com)"), []
    if not available():
        return "the 'tinytuya' package is missing (pip install tinytuya)", []
    import tinytuya
    try:
        c = tinytuya.Cloud(apiRegion=region, apiKey=key, apiSecret=secret)
        if getattr(c, "error", None):
            return f"Tuya login failed: {c.error.get('Payload', c.error)}", []
        devs = c.getdevices(False)
    except Exception as e:
        return f"could not reach the Tuya cloud ({type(e).__name__}: {e})", []
    if isinstance(devs, dict) and devs.get("Error"):
        msg = str(devs.get("Payload") or devs.get("Error"))
        if "permission" in msg.lower() or "1106" in msg:
            msg += (" — in the iot.tuya.com project, link your app account under "
                    "Devices → Link App Account, and check the data centre/region")
        return f"Tuya cloud error: {msg}", []
    out = []
    for d in devs or []:
        if not d.get("id"):
            continue
        out.append({"id": d["id"], "key": d.get("key", ""), "name": d.get("name", ""),
                    "category": d.get("category", ""),
                    "product": d.get("product_name", ""),
                    "mac": (d.get("mac") or "").upper()})
    return "", out
