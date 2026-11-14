"""
Android phone — control the user's phone through the MARK Android app.

    "turn on the phone's flashlight" · "phone volume 30" · "put the phone on silent"
    "find my phone" (rings loud) · "set an alarm on the phone for 6:30"
    "timer 10 minutes on the phone" · "open Waze on the phone" · "navigate to work"
    "where is my phone?" · "phone battery"
    "call Ana" · "text João I'm on my way"            (confirmed on the HUD first)
    "what notifications do I have?" · "reply to João: ok"  (reply confirmed first)

The app keeps a link to the PC (/ws/device on the dashboard) and executes the
commands with the phone's own APIs. Calls, SMS and notification replies cannot
be taken back, so they wait for CONFIRM on the HUD — the model never decides
alone to call or text someone.
"""

from __future__ import annotations

import asyncio
import json
import time
import re

NAMESPACE = "android_phone"


def _dash():
    try:
        from dashboard.server import DashboardServer
        return getattr(DashboardServer, "instance", None)
    except Exception:
        return None


def _call(action: str, args: dict | None = None, timeout: float = 15.0) -> dict:
    """Run one command on the phone from this worker thread."""
    d = _dash()
    loop = getattr(d, "_loop", None) if d else None
    if not d or not loop:
        return {"ok": False, "result": "the phone dashboard is not running on the PC"}
    try:
        fut = asyncio.run_coroutine_threadsafe(d.device_call(action, args or {}, timeout), loop)
        return fut.result(timeout + 3)
    except Exception as e:
        return {"ok": False, "result": f"could not reach the phone ({type(e).__name__})"}


# Spoken app names the user disambiguated once ("banco" → "Banco Inter"),
# kept in memory/phone_apps.json so the question is never asked twice.
from pathlib import Path as _Path
_APPS_FILE = _Path(__file__).resolve().parent.parent / "memory" / "phone_apps.json"
try:
    _APP_CHOICES: dict = json.loads(_APPS_FILE.read_text(encoding="utf-8"))
except Exception:
    _APP_CHOICES = {}
_APP_ASK: dict = {}


def _fold(s: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(t.lower().split())


def _learn_app_choice(asked: str, r: dict) -> None:
    text = str(r.get("result") or "")
    if text.startswith("[ASK THE USER]"):
        choices = (r.get("data") or {}).get("choices") or []
        _APP_ASK.clear()
        _APP_ASK.update(q=_fold(asked), choices=[_fold(c) for c in choices], label=choices,
                        t=time.time())
        return
    if not r.get("ok") or not _APP_ASK or time.time() - _APP_ASK.get("t", 0) > 180:
        return
    q, pick = _APP_ASK["q"], _fold(asked)
    if pick in _APP_ASK["choices"] and pick != q:
        _APP_CHOICES[q] = _APP_ASK["label"][_APP_ASK["choices"].index(pick)]
        _APP_ASK.clear()
        try:
            _APPS_FILE.write_text(json.dumps(_APP_CHOICES, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[Phone] learned: '{q}' opens {_APP_CHOICES[q]}")
        except Exception:
            pass


def _say(r: dict, ok_prefix: str = "") -> str:
    text = str(r.get("result") or ("done" if r.get("ok") else "failed"))
    return (ok_prefix + text) if r.get("ok") else f"Phone: {text}"


def _confirm(key: str, title: str, detail: str, action: str, args: dict) -> str:
    from core import confirm
    return confirm.request(key, title, detail, lambda: _say(_call(action, args, 30)))


def _num(v, default=None):
    m = re.search(r"-?\d+", str(v or ""))
    return int(m.group()) if m else default


def _parse_time(v: str):
    m = re.search(r"(\d{1,2})(?:[:h.](\d{2}))?", v or "")
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2) or 0)
    return (h, mnt) if 0 <= h < 24 and 0 <= mnt < 60 else None


def _seconds(v: str):
    """'10 minutes', '1h30', '90s', '25' (minutes) → seconds."""
    v = (v or "").lower()
    total = 0
    for n, unit in re.findall(r"(\d+)\s*(h|hora|hour|m|min|s|seg|sec)?", v):
        n = int(n)
        total += n * (3600 if unit in ("h", "hora", "hour") else
                      1 if unit in ("s", "seg", "sec") else 60)
    return total or None


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    a = str(p.get("action", "")).strip().lower().replace(" ", "_")
    v = str(p.get("value", "") or "").strip()
    text = str(p.get("text", "") or "").strip()

    if a in ("flashlight", "torch", "lanterna"):
        on = v.lower() not in ("off", "desligar", "desliga", "false", "0")
        return _say(_call("flashlight", {"on": on}), f"Flashlight {'on' if on else 'off'}. ")
    if a == "volume":
        stream = str(p.get("stream", "") or "media").lower()
        level = v.lower() if v.lower() in ("up", "down", "max", "mute") else _num(v)
        if level is None:
            return "What volume, 0 to 100, or up/down?"
        return _say(_call("volume", {"stream": stream, "level": level}))
    if a in ("silent", "vibrate", "normal", "ringer"):
        mode = a if a != "ringer" else (v.lower() or "normal")
        return _say(_call("ringer", {"mode": mode}))
    if a in ("find", "find_phone", "ring"):
        return _say(_call("find_phone", {"seconds": _num(v, 30)}))
    if a in ("stop", "stop_ring"):
        return _say(_call("stop_ring"))
    if a == "alarm":
        t = _parse_time(v)
        if not t:
            return "What time for the alarm (HH:MM)?"
        return _say(_call("alarm", {"hour": t[0], "minute": t[1], "label": text}))
    if a == "timer":
        secs = _seconds(v)
        if not secs:
            return "How long — e.g. '10 minutes'?"
        return _say(_call("timer", {"seconds": secs, "label": text}))
    if a == "open_app":
        name = _APP_CHOICES.get(_fold(v), v)
        r = _call("open_app", {"name": name})
        _learn_app_choice(v, r)
        return _say(r)
    if a in ("media", "play", "pause", "next", "previous"):
        key = v.lower() if a == "media" else a
        return _say(_call("media", {"key": key or "play"}))
    if a == "open_url":
        # app links (waze://, spotify:, intent:…) go as they are; bare addresses get https
        url = v if re.match(r"^[a-z][a-z0-9+.-]*:", v, re.I) else f"https://{v}"
        return _say(_call("open_url", {"url": url}))
    if a == "navigate":
        app = str(p.get("app") or "").strip()
        return _say(_call("navigate", {"destination": v, "app": app}))
    if a == "battery":
        return _say(_call("battery"))
    if a in ("location", "where"):
        r = _call("location", {}, 25)
        if r.get("ok") and isinstance(r.get("data"), dict) and player:
            d = r["data"]
            try:
                player.show_content("PHONE LOCATION",
                                    f"{r.get('result')}\n\nhttps://maps.google.com/?q={d.get('lat')},{d.get('lon')}")
            except Exception:
                pass
        return _say(r)
    if a == "contacts":
        return _say(_call("contacts", {"query": v}))
    if a == "call":
        if not v:
            return "Who should I call?"
        return _confirm("phone_call", f"Call {v} from the phone", v, "call", {"to": v})
    if a == "sms":
        if not (v and text):
            return "To whom, and what should the message say?"
        return _confirm("phone_sms", f"Send SMS to {v}", text[:200], "sms", {"to": v, "text": text})
    if a == "notifications":
        r = _call("notifications")
        if r.get("ok"):
            return _say(r)
        d = _dash()
        recent = (d.recent_notifications if d else [])[-10:]
        if not recent:
            return _say(r)
        return "Recent phone notifications: " + "; ".join(
            f"{n['app']}: {n['title']} — {n['text'][:80]}" for n in reversed(recent))
    if a == "reply":
        if not (v and text):
            return "Reply to which conversation, and with what?"
        d = _dash()
        recent = [n for n in (d.recent_notifications if d else []) if n.get("can_reply")]
        q = v.lower()
        hit = next((n for n in reversed(recent) if q in n["title"].lower() or q in n["app"].lower()), None)
        if not hit:
            return f"I have no recent notification from '{v}' that can be answered."
        return _confirm("phone_reply", f"Reply to {hit['title']} ({hit['app']})", text[:200],
                        "reply", {"key": hit["key"], "text": text})
    if a == "notification_alerts":
        from memory.config_manager import save_plugin_config
        on = v.lower() not in ("off", "false", "0", "desligar", "não", "nao")
        save_plugin_config(NAMESPACE, {"relay": "on" if on else "off"})
        return ("I'll tell you when a message arrives on the phone." if on
                else "Phone message alerts are off.")
    if a == "status":
        d = _dash()
        if not d or not d.device_connected():
            return "The phone app is not connected."
        info = d._device_info
        return f"Connected to {info.get('device') or info.get('name')}. " + _say(_call("battery"))
    return f"Unknown phone action '{a}'."


PLUGIN = {
    "name": "android_phone",
    "description": (
        "Controls the user's Android phone through the MARK app. Actions: "
        "'flashlight' (value on|off); 'volume' (value 0-100|up|down|max|mute, "
        "stream media|ring|alarm — when the user talks from the phone app, 'turn it "
        "up/down' means this, music included); 'silent' / 'vibrate' / 'normal'; 'find_phone' "
        "(rings loudly to find it; value=seconds) and 'stop_ring'; 'alarm' "
        "(value=HH:MM, text=label); 'timer' (value like '10 minutes', text=label); "
        "'open_app' (value=app name — ANY app installed, matched by its name even if misheard; when "
        "several match, the reply starts [ASK THE USER]: ask which one and call again with that exact name); 'media' (value play|pause|next|previous — the "
        "phone's current music app, like a headset button; for Spotify use the spotify "
        "tool, which plays on the phone by itself); 'open_url'; 'navigate' (value=destination — "
        "'casa'/'home' and 'trabalho' go to the app's saved Home / Work; app=waze|maps — ALWAYS "
        "pass the app the user names, e.g. 'abre o Waze e vai pra casa' = ONE call navigate "
        "value=casa app=waze, not open_app; without app: the only navigation app, or with several "
        "the phone answers [ASK THE USER] — ask which, then call again with app); 'battery'; 'location' ('where is my phone?'); "
        "'contacts' (value=name to look up); 'call' (value=contact name or "
        "number); 'sms' (value=contact, text=message); 'notifications' (recent "
        "ones); 'reply' (value=contact or app of a recent notification, "
        "text=reply); 'notification_alerts' (value on|off — announce incoming "
        "messages); 'status'. Calls, SMS and replies ask the user to confirm on "
        "screen first. Use this whenever the user says 'phone'/'celular' — not "
        "computer_settings or open_app, which act on the PC."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of the actions above."},
            "value": {"type": "STRING", "description": "Main argument (on/off, level, time, name, number, destination…)."},
            "text": {"type": "STRING", "description": "Message text for sms/reply, or a label for alarm/timer."},
            "stream": {"type": "STRING", "description": "volume only: media | ring | alarm"},
            "app": {"type": "STRING", "description": "navigate only: waze | maps — the app the user named."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "Android phone",
    "fields": [
        {"key": "relay", "label": "Announce incoming phone messages", "type": "choice",
         "options": ["on", "off"]},
        {"key": "relay_apps", "label": "Apps to announce (comma separated)", "type": "text",
         "placeholder": "whatsapp, telegram, messages, signal, instagram"},
    ],
}
