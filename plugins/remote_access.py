"""
Remote access — the owner's fixed key, keys for guests, and who is paired.

    ⚙ → plugin settings → Remote access: your own fixed key (8+ characters).
        It always works to pair the Android app or log in from a browser —
        no QR code needed — and is never used up.
    "create an access key for a guest"   → one-time key, valid 10 minutes
    "which devices are connected?"        → paired apps and whether one is online
    "remove all remote devices"           → unpairs every app and browser
                                            (confirmed on the HUD first)
"""

from __future__ import annotations

import asyncio

NAMESPACE = "remote_access"


def _dash():
    try:
        from dashboard.server import DashboardServer
        return getattr(DashboardServer, "instance", None)
    except Exception:
        return None


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action", "")).strip().lower()
    d = _dash()
    if d is None:
        return "The remote dashboard is not running on this PC."

    if action in ("guest_key", "new_key", "key"):
        key = d.new_key()
        url = d.get_url()
        text = (f"Guest access key: {key}\n\nValid once, for 10 minutes.\n"
                f"Address: {url}\n\nIn the MARK app: type the address and this key. "
                f"In a browser: open the address and enter the key.")
        if player:
            try:
                player.show_content("GUEST ACCESS KEY", text)
            except Exception:
                pass
        # The key itself goes on screen, not into speech: it is read by the
        # person at the screen, not announced to the room.
        return ("A one-time guest key is on screen, valid for 10 minutes. Tell the user it is "
                "displayed; do not read the key aloud.")

    if action in ("devices", "list"):
        apps = d._app_load()
        names = [a.get("name", "?") for a in apps]
        online = d._device_info.get("device") or d._device_info.get("name") if d.device_connected() else None
        if not names:
            return "No Android app is paired."
        return (f"Paired apps: {', '.join(names)}."
                + (f" Online now: {online}." if online else " None is online right now.")
                + (" A fixed owner key is set." if d.owner_key() else " No fixed owner key is set."))

    if action in ("revoke", "revoke_all", "unpair"):
        from core import confirm

        def _do():
            loop = getattr(d, "_loop", None)
            d._app_save([])
            d._device_sessions.clear()
            for t in list(d._tokens):
                d._tokens.pop(t, None)
                d._token_keys.pop(t, None)
            if loop and d._device_ws is not None:
                asyncio.run_coroutine_threadsafe(d._device_ws.close(code=4001), loop)
            return "All remote devices were unpaired."
        return confirm.request("revoke_remote", "Unpair every phone and browser",
                               "Everyone will need a key to connect again.", _do)

    return f"Unknown remote access action '{action}'."


def _check(values: dict):
    k = str(values.get("owner_key", "") or "").strip()
    if not k:
        return True, "No fixed key — only one-time keys will work."
    if len(k) < 8:
        return False, "The fixed key needs at least 8 characters."
    return True, "Fixed key saved. Use it in the app (address + key) or in a browser."


PLUGIN = {
    "name": "remote_access",
    "description": (
        "Remote access to the assistant from phones and browsers. Actions: "
        "'guest_key' (create a one-time access key for someone else — it is shown "
        "on screen); 'devices' (which Android apps are paired / online); "
        "'revoke_all' (unpair every remote phone and browser — confirmed on "
        "screen). The owner's fixed key is set in the settings, not by voice."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "guest_key | devices | revoke_all"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "Remote access",
    "fields": [
        {"key": "owner_key", "label": "Your fixed access key (8+ characters)", "type": "password",
         "placeholder": "works for the app and the browser, never expires"},
    ],
    "action": {"label": "CHECK", "run": _check},
}
