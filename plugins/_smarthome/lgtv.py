"""
LG webOS TV control over the LAN.

The TV speaks LG's SSAP protocol over a WebSocket (ports 3000/3001). The first
connection has to be approved on the TV itself, which answers with a client key;
every later connection presents that key and is let straight in. Pairing needs
LG's signed app manifest, which is why this goes through aiowebostv (the
library Home Assistant uses for these TVs) instead of a hand-rolled copy.

Turning the TV ON is the exception: a switched-off TV has no WebSocket to talk
to, so power-on is a Wake-on-LAN packet to its MAC. That only works with the
TV's "Turn on via Wi-Fi" (or "Mobile TV On") option enabled.
"""

from __future__ import annotations

import asyncio
import difflib
import socket

PAIR_TIMEOUT = 60       # seconds the user gets to press "Allow" on the TV


def available() -> bool:
    try:
        import aiowebostv  # noqa: F401
        return True
    except ImportError:
        return False


def wake_on_lan(mac: str, ip: str = "") -> str:
    try:
        raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        if len(raw) != 6:
            return "the TV's MAC address is unknown — scan the network first"
        packet = b"\xff" * 6 + raw * 16
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        targets = ["255.255.255.255"]
        if ip.count(".") == 3:
            targets.append(ip.rsplit(".", 1)[0] + ".255")
        for t in targets:
            for port in (9, 7):
                s.sendto(packet, (t, port))
        s.close()
        return ""
    except Exception as e:
        return f"could not send the wake-up packet ({e})"


def _match(query: str, options: dict[str, str]) -> str | None:
    """options: id → human label. Fuzzy-match a spoken name to an id."""
    q = query.strip().lower()
    if not q:
        return None
    for oid, label in options.items():
        if q == oid.lower() or q == label.lower():
            return oid
    for oid, label in options.items():
        if q in label.lower() or q in oid.lower():
            return oid
    best = difflib.get_close_matches(q, [l.lower() for l in options.values()], n=1, cutoff=0.6)
    if best:
        return next(oid for oid, l in options.items() if l.lower() == best[0])
    return None


async def _session(ip: str, key: str | None, command: str, value: str):
    from aiowebostv import WebOsClient
    from aiowebostv import webos_client as _wc

    client = WebOsClient(ip, client_key=key or None, connect_timeout=4)
    if not key:
        _wc.RECEIVE_TIMEOUT = PAIR_TIMEOUT     # time to press "Allow" on the TV
    try:
        await client.connect()
    finally:
        _wc.RECEIVE_TIMEOUT = 10
    try:
        st = client.tv_state
        c = command
        if c == "pair":
            out = "paired"
        elif c == "power_off":
            await client.power_off(); out = "turned off"
        elif c == "volume_up":
            await client.volume_up(); out = "volume up"
        elif c == "volume_down":
            await client.volume_down(); out = "volume down"
        elif c == "set_volume":
            v = max(0, min(100, int(float(value))))
            await client.set_volume(v); out = f"volume {v}"
        elif c in ("mute", "unmute"):
            await client.set_mute(c == "mute"); out = c + "d"
        elif c in ("play", "pause", "stop"):
            await getattr(client, c)(); out = c
        elif c in ("channel_up", "channel_down"):
            await getattr(client, c)(); out = c.replace("_", " ")
        elif c == "notify":
            await client.send_message(value or "Hello"); out = "message shown"
        elif c == "launch":
            apps = {aid: (a.get("title") or aid) for aid, a in (st.apps or {}).items()}
            aid = _match(value, apps)
            if not aid:
                return client.client_key, None, (
                    f"no app called '{value}' on the TV. Some it has: "
                    + ", ".join(sorted(apps.values())[:12]))
            await client.launch_app(aid); out = f"opened {apps[aid]}"
        elif c == "input":
            ins = {iid: (i.get("label") or iid) for iid, i in (st.inputs or {}).items()}
            q = value.replace(" ", "_").upper() if value.lower().startswith("hdmi") else value
            iid = _match(q, ins) or _match(value, ins)
            if not iid:
                return client.client_key, None, (
                    f"no input called '{value}'. Inputs: " + ", ".join(ins.values()))
            await client.set_input(iid); out = f"switched to {ins[iid]}"
        elif c == "button":
            await client.button(value.upper()); out = f"pressed {value.upper()}"
        elif c == "status":
            app = (st.apps or {}).get(st.current_app_id or "", {}).get("title", st.current_app_id)
            out = (f"on, volume {st.volume}{' (muted)' if st.muted else ''}"
                   + (f", showing {app}" if app else ""))
        else:
            return client.client_key, None, f"unknown TV command '{command}'"
        return client.client_key, out, ""
    finally:
        await client.disconnect()


def command(ip: str, key: str | None, cmd: str, value: str = "") -> tuple[str | None, str, str]:
    """(client_key, result, error). Runs its own short-lived event loop: this is
    called from the tool executor thread, never from the Live session's loop."""
    if not available():
        return key, "", "LG TV control needs the 'aiowebostv' package (pip install aiowebostv)"
    try:
        new_key, out, err = asyncio.run(asyncio.wait_for(
            _session(ip, key, cmd, value), PAIR_TIMEOUT + 15 if not key else 20))
        return new_key or key, out or "", err
    except Exception as e:
        name = type(e).__name__
        if "Pair" in name:
            return key, "", ("the TV did not accept the pairing — say 'pair the TV' again "
                             "and press Allow on the TV screen within a minute")
        if isinstance(e, (OSError, asyncio.TimeoutError)) or "Connect" in name or "Timeout" in name:
            return key, "", f"the TV at {ip} is not answering — it may be off or on another IP"
        return key, "", f"TV error: {name}: {e}"
