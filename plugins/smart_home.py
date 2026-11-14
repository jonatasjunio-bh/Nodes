"""
Smart home — find what is on the Wi-Fi, name it, and run it by voice.

    "scan the network"                → every device on the Wi-Fi, identified
    "call 192.168.1.50 living room light"
    "turn off the living room light"  → i2GO / Tuya, Sonoff (LAN), Tasmota
    "TV volume 20" / "open Netflix on the TV" / "turn the TV off"  → LG webOS
    "save scene cinema: TV on, light off" / "cinema mode"
    "warn me when a new device joins" → background watcher, on by default

Everything runs on the local network. The only exception is fetching the
local keys of Sonoff (eWeLink account) and i2GO/Tuya (Tuya IoT project)
devices, which exist only in those clouds and are fetched once when asked to
(see _smarthome/sonoff.py and _smarthome/tuya.py). Devices are remembered in
memory/smart_home.json, keyed by MAC, so a name survives the router handing
the device a new IP.

Amazon Echo devices are found and named, but Amazon offers no local control
API. Anything the user controls through Alexa today (Sonoff included) can be
controlled here directly instead.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata

from plugins._smarthome import broadlinkctl, lgtv, oui, scan, sonoff, store, tuya

NAMESPACE = "smart_home"
WATCH_EVERY = 10 * 60          # seconds between new-device checks

_COLORS = {
    "red": "FF0000", "vermelho": "FF0000", "green": "00FF00", "verde": "00FF00",
    "blue": "0000FF", "azul": "0000FF", "yellow": "FFFF00", "amarelo": "FFFF00",
    "white": "FFFFFF", "branco": "FFFFFF", "purple": "8000FF", "roxo": "8000FF",
    "pink": "FF3090", "rosa": "FF3090", "orange": "FF8000", "laranja": "FF8000",
    "cyan": "00FFFF", "ciano": "00FFFF", "warm": "FFB060", "quente": "FFB060",
}

_KIND_ORDER = ("lg_tv", "tuya", "broadlink", "sonoff", "tasmota", "smart_switch", "alexa", "chromecast",
               "lg", "printer", "router", "computer", "phone_or_pc", "unknown")


# ── device book helpers ──────────────────────────────────────────────────────

def _display(d: dict) -> str:
    return (str(d.get("alias") or "").strip() or d.get("name") or d.get("hostname")
            or d.get("label") or d.get("ip", "?"))


def _fold(text) -> str:
    """Lower case, no accents, single spaces — for matching spoken names."""
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(t.lower().split())


def _merge_scan(found: list[dict]) -> dict:
    now = time.strftime("%Y-%m-%d %H:%M")

    def apply(data):
        devs = data["devices"]
        for f in found:
            key = f["mac"] or f"ip:{f['ip']}"
            cur = devs.get(key, {"first_seen": now})
            keep = {k: cur[k] for k in ("alias", "first_seen", "lg", "sonoff", "tuya",
                                        "broadlink", "codes") if k in cur}
            cur.update({k: v for k, v in f.items() if k not in ("sonoff", "tuya", "broadlink")})
            cur.update(keep)
            if cur.get("sonoff", {}).get("key") or cur.get("lg", {}).get("key"):
                # Linked or paired earlier: that is proof of what it is, whatever
                # this scan managed to hear from it.
                cur["kind"] = "sonoff" if cur.get("sonoff", {}).get("key") else "lg_tv"
                cur["label"] = "Sonoff (eWeLink)" if cur["kind"] == "sonoff" else "LG webOS TV"
            if f.get("broadlink"):
                cur["broadlink"] = {**cur.get("broadlink", {}), **f["broadlink"]}
            if cur.get("broadlink"):
                b = cur["broadlink"]
                cur["kind"] = "broadlink"
                cur["label"] = broadlinkctl.label(b.get("type", ""), b.get("model", ""))
            if f.get("tuya"):                         # fresh broadcast facts, keep the key
                cur["tuya"] = {**cur.get("tuya", {}), **f["tuya"]}
            if cur.get("tuya", {}).get("key"):
                cur["kind"] = "tuya"
                cur["label"] = tuya.label(cur["tuya"].get("category", ""),
                                          cur["tuya"].get("product", ""))
            if f.get("sonoff"):                       # fresh mDNS facts, keep the key
                cur["sonoff"] = {**cur.get("sonoff", {}), **f["sonoff"]}
            cur["last_seen"] = now
            cur["online"] = True
            devs[key] = cur
        seen = {f["mac"] or f"ip:{f['ip']}" for f in found}
        for k, d in devs.items():
            if k not in seen:
                d["online"] = False
        return devs
    return store.update(apply)


# The last ambiguous name ("sala" → TV da sala / Luz da sala) and its candidates.
# If the user then picks one of them within a couple of minutes, the name they
# first used is learned as that device's spoken alias, so it works next time.
_AMBIG: dict = {}


def _learn_spoken(key: str, q: str) -> None:
    amb = _AMBIG
    if not amb or time.time() - amb.get("t", 0) > 150 or key not in amb.get("keys", ()):
        return
    said = amb.get("q", "")
    _AMBIG.clear()
    if not said or said == q:
        return

    def apply(data):
        d = data["devices"].get(key)
        if d is not None:
            spoken = [x for x in d.get("spoken", []) if _fold(x) != said]
            d["spoken"] = (spoken + [said])[-8:]
            # a name now means one device: no other keeps it
            for k2, d2 in data["devices"].items():
                if k2 != key and said in [_fold(x) for x in d2.get("spoken", [])]:
                    d2["spoken"] = [x for x in d2["spoken"] if _fold(x) != said]
    store.update(apply)
    print(f"[SmartHome] learned: '{said}' means {key}")


def _resolve(query: str, kinds: tuple[str, ...] = ()) -> tuple[str, dict] | str:
    """A spoken reference → (key, device), or a sentence explaining why not."""
    r = _resolve_inner(query, kinds)
    q = _fold(query)
    if isinstance(r, tuple):
        _learn_spoken(r[0], q)
    return r


def _resolve_inner(query: str, kinds: tuple[str, ...] = ()) -> tuple[str, dict] | str:
    devs = store.load()["devices"]
    if not devs:
        return "I don't know any devices yet — ask me to scan the network first."
    pool = {k: d for k, d in devs.items() if not kinds or d.get("kind") in kinds}
    q = _fold(query)
    if not q:
        if len(pool) == 1:
            return next(iter(pool.items()))
        if kinds and pool:
            names = ", ".join(_display(d) for d in pool.values())
            return f"Which one? I know: {names}."
        return "Which device?"
    for field in ("alias", "name", "hostname", "ip", "mac"):
        for k, d in pool.items():
            if _fold(d.get(field, "")) == q:
                return k, d
    for k, d in pool.items():                 # names learned from the user's choices
        if q in [_fold(x) for x in d.get("spoken", [])]:
            return k, d
    # Match at the start of a word only: a plain substring test found "ar"
    # inside "smart plug" and switched on a plug when asked for the AC.
    word_start = re.compile(rf"(?<!\w){re.escape(q)}")
    hits = [(k, d) for k, d in pool.items()
            if any(word_start.search(_fold(d.get(f, "")))
                   for f in ("alias", "name", "hostname", "label"))]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        _AMBIG.clear()
        _AMBIG.update(q=q, keys=[k for k, _ in hits], t=time.time())
        return ("That matches several devices: " + ", ".join(_display(d) for _, d in hits)
                + ". Ask which one — the choice is remembered for next time.")
    words = set(re.findall(r"\w+", q))
    scored = sorted(((len(words & set(re.findall(r"\w+", _fold(_display(d))))), k, d)
                     for k, d in pool.items()), key=lambda t: t[0], reverse=True)
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1], scored[0][2]
    return f"I don't know a device called '{query}'. Say 'list devices' to hear them."


def _save_device(key: str, **fields) -> None:
    def apply(data):
        data["devices"].setdefault(key, {}).update(fields)
    store.update(apply)


# ── actions ──────────────────────────────────────────────────────────────────

def _do_scan(player) -> str:
    if player:
        player.write_log("SYS: Scanning the Wi-Fi network for devices…")
    found = scan.full_scan()
    devs = _merge_scan(found)
    online = [d for d in devs.values() if d.get("online")]
    online.sort(key=lambda d: (_KIND_ORDER.index(d.get("kind", "unknown"))
                               if d.get("kind") in _KIND_ORDER else 99))

    lines = []
    for d in online:
        extra = []
        if d.get("kind") == "sonoff" and not d.get("sonoff", {}).get("key") \
                and d.get("sonoff", {}).get("encrypt", True):
            extra.append("needs 'sync Sonoff'")
        if d.get("kind") == "lg_tv" and not d.get("lg", {}).get("key"):
            extra.append("needs 'pair the TV'")
        if d.get("kind") == "broadlink" and d.get("broadlink", {}).get("locked"):
            extra.append("locked in the Broadlink app")
        if d.get("kind") == "broadlink" and d.get("codes"):
            extra.append(f"{len(d['codes'])} learned buttons")
        if d.get("kind") == "tuya" and not d.get("tuya", {}).get("key"):
            extra.append("needs 'sync i2GO'")
        name = _display(d)
        lines.append(f"{d['ip']:<15} {name[:30]:<30} {d.get('label', '')}"
                     + (f"  [{'; '.join(extra)}]" if extra else ""))
    if player:
        try:
            player.show_content("SMART HOME — NETWORK", "\n".join(lines))
        except Exception:
            pass

    smart = [d for d in online if d.get("kind") in ("lg_tv", "tuya", "broadlink", "sonoff", "tasmota",
                                                    "smart_switch", "alexa", "chromecast")]
    summary = "; ".join(f"{_display(d)} ({d.get('label')}, {d['ip']})" for d in smart)
    others = len(online) - len(smart)
    locked = [_display(d) for d in online if d.get("broadlink", {}).get("locked")]
    if locked:
        summary += (f". IMPORTANT: {', '.join(locked)} is locked in the Broadlink app and "
                    f"cannot be controlled until the user opens it in the Broadlink app → "
                    f"Properties → turns off 'Lock device' — tell them")
    return (f"Found {len(online)} devices on the Wi-Fi. Smart devices: {summary or 'none recognised'}."
            f" Plus {others} other devices (router, computers, phones)."
            " The full list is on screen. Offer to give the smart devices names.")


def _do_list() -> str:
    devs = store.load()["devices"]
    if not devs:
        return "No devices saved yet — ask me to scan the network."
    rows = []
    for d in sorted(devs.values(), key=lambda d: d.get("ip", "")):
        state = "online" if d.get("online") else f"last seen {d.get('last_seen', '?')}"
        rows.append(f"{_display(d)} — {d.get('label', '?')}, {d.get('ip')}, {state}")
    return "Known devices: " + "; ".join(rows)


def _do_rename(device: str, name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "What should I call it?"
    r = _resolve(device)
    if isinstance(r, str):
        return r
    key, d = r
    _save_device(key, alias=name)
    return f"Done — {d.get('label', 'the device')} at {d.get('ip')} is now '{name}'."


def _do_forget(device: str) -> str:
    r = _resolve(device)
    if isinstance(r, str):
        return r
    key, d = r
    store.update(lambda data: data["devices"].pop(key, None))
    return f"Forgot {_display(d)}."


def _power(device: str, on: bool | None) -> str:
    """on=True/False, or None for toggle."""
    # An appliance taught to a Broadlink remote wins over a fuzzy device match:
    # "turn on the AC" means the learned "ligar ar" button, not whichever
    # network device happens to share a few letters with "ar".
    if on is not None and device:
        verb = "ligar" if on else "desligar"
        for cand in (f"{verb} {device}", f"{device} {verb}",
                     f"{'turn on' if on else 'turn off'} {device}"):
            if _find_code(cand):
                return _send_code(cand)
    r = _resolve(device, ("sonoff", "tuya", "broadlink", "tasmota", "smart_switch", "lg_tv"))
    if isinstance(r, str):
        return r
    key, d = r
    kind, name = d.get("kind"), _display(d)
    word = {True: "on", False: "off", None: "toggled"}[on]

    if kind == "broadlink":
        if on is None:
            err, now = broadlinkctl.power_state(d)
            if err:
                return f"Could not toggle {name}: {err}."
            on, word = not now, ("on" if not now else "off")
        err = broadlinkctl.set_power(d, on)
        return f"{name} {word}." if not err else f"Could not switch {name}: {err}."

    if kind == "tuya":
        if on is None:
            err, now = tuya.toggle(d)
            return f"{name} turned {'on' if now else 'off'}." if not err else \
                f"Could not switch {name}: {err}."
        err = tuya.set_power(d, on)
        return f"{name} {word}." if not err else f"Could not switch {name}: {err}."

    if kind == "lg_tv":
        if on is False:
            return _tv(name, "power_off")
        if on is True:
            return _tv(name, "power_on")
        return "Say 'turn the TV on' or 'off'."
    if kind == "tasmota":
        err, _ = sonoff.tasmota(d["ip"], "Power " + {True: "On", False: "Off", None: "Toggle"}[on])
        return f"{name} {word}." if not err else f"Could not switch {name}: {err}."
    if kind in ("sonoff", "smart_switch"):
        if on is None:
            return (f"{name} does not report its state over the LAN, so I can't toggle "
                    f"it — say on or off.")
        s_ = d.get("sonoff", {})
        if not s_.get("key") and s_.get("encrypt", True) and 8081 in (d.get("ports") or [8081]):
            # A Sonoff on factory firmware only obeys commands encrypted with its
            # own key, which lives in the eWeLink account: fetch it (no login
            # prompt here), then go on — or say exactly what is missing.
            why = _sonoff_sync(None, login=False)
            d = store.load()["devices"].get(key, d)
            if not d.get("sonoff", {}).get("key"):
                return (f"{name} is a Sonoff that is not linked yet: it only obeys commands "
                        f"encrypted with its own key, which comes from the user's eWeLink account "
                        f"({why}). Tell the user, and offer to run 'sonoff_sync' — if the eWeLink "
                        f"login expired it opens the eWeLink login page on the PC once. If it is "
                        f"not in their eWeLink account yet, it must be added in the eWeLink app first.")
        err = sonoff.set_power(d, on, **_outlet(d))
        if err and sonoff.moved(err) and _relocate(key, d):
            err = sonoff.set_power(d, on, **_outlet(d))
        return f"{name} {word}." if not err else f"Could not switch {name}: {err}."
    return f"I can't switch {name} ({d.get('label')})."


def _outlet(d: dict) -> dict:
    """Single-relay plugs that report four positions (MINI R4) take outlet 0."""
    return {"outlet": 0} if d.get("sonoff", {}).get("multi") else {}


def _relocate(key: str, d: dict) -> bool:
    """The router gave the plug a new IP: find it on the LAN by its key and
    remember the address. Updates d in place; True when found."""
    s = d.get("sonoff", {})
    found = sonoff.locate([{"id": s.get("id"), "key": s.get("key")}])
    ip = found.get(s.get("id"))
    if not ip:
        return False
    d["ip"] = ip
    _save_device(key, ip=ip)
    print(f"[SmartHome] {_display(d)} moved to {ip}")
    return True


def _brightness(device: str, value: str) -> str:
    m = re.search(r"\d+", value or "")
    if not m:
        return "What brightness, from 1 to 100?"
    pct = max(1, min(100, int(m.group())))
    r = _resolve(device, ("tuya", "broadlink", "sonoff", "tasmota", "smart_switch"))
    if isinstance(r, str):
        return r
    _key, d = r
    name = _display(d)
    if d.get("kind") == "broadlink":
        err = broadlinkctl.set_brightness(d, pct)
    elif d.get("kind") == "tuya":
        err = tuya.set_brightness(d, pct)
    elif d.get("kind") == "tasmota":
        err, _ = sonoff.tasmota(d["ip"], f"Dimmer {pct}")
    else:
        err = sonoff.set_brightness(d, pct)
    return f"{name} at {pct}%." if not err else f"Could not dim {name}: {err}."


def _color(device: str, value: str) -> str:
    v = (value or "").strip().lower()
    hexv = _COLORS.get(v) or (v.lstrip("#").upper() if re.fullmatch(r"#?[0-9a-fA-F]{6}", v) else "")
    if not hexv:
        return f"I don't know the colour '{value}'. Try red, blue, warm white or a hex code."
    r = _resolve(device, ("tuya", "broadlink", "tasmota", "sonoff", "smart_switch"))
    if isinstance(r, str):
        return r
    _key, d = r
    if d.get("kind") == "broadlink":
        err = broadlinkctl.set_color(d, hexv)
        return f"{_display(d)} set to {value}." if not err else f"Could not set the colour: {err}."
    if d.get("kind") == "tuya":
        err = tuya.set_color(d, hexv)
        return f"{_display(d)} set to {value}." if not err else f"Could not set the colour: {err}."
    if d.get("kind") != "tasmota":
        return (f"{_display(d)} can't change colour over the LAN — Sonoff only exposes "
                f"colour through the cloud. i2GO and Tasmota bulbs can.")
    err, _ = sonoff.tasmota(d["ip"], f"Color {hexv}")
    return f"{_display(d)} set to {value}." if not err else f"Could not set the colour: {err}."


def _status(device: str) -> str:
    r = _resolve(device)
    if isinstance(r, str):
        return r
    _key, d = r
    name = _display(d)
    if d.get("kind") == "lg_tv":
        return _tv(name, "status")
    if d.get("kind") == "tuya":
        err, st = tuya.status(d)
        return f"{name} is {st}." if not err else f"{name}: {err}."
    if d.get("kind") == "broadlink":
        # Ask the device itself: a lock lifted in the app since the last scan
        # must not keep being reported.
        fresh = broadlinkctl.probe([d["ip"]]).get(d["ip"])
        if fresh:
            d["broadlink"] = {**d.get("broadlink", {}), **fresh}
            _save_device(_key, broadlink=d["broadlink"])
        if d.get("broadlink", {}).get("locked"):
            return (f"{name} is locked in the Broadlink app, so it refuses every local "
                    f"command. In the Broadlink app: open the device → Properties (⋯) → "
                    f"turn off 'Lock device', then scan the network again.")
        codes = sorted(d.get("codes", {}))
        if broadlinkctl.capabilities(d.get("broadlink", {}).get("type", ""))["ir"]:
            return (f"{name} is a {d.get('label')} with {len(codes)} learned buttons"
                    + (f": {', '.join(codes)}." if codes else ". Teach it with 'learn'."))
        err, now = broadlinkctl.power_state(d)
        return f"{name} is {'on' if now else 'off'}." if not err else f"{name}: {err}."
    if d.get("kind") == "tasmota":
        err, resp = sonoff.tasmota(d["ip"], "Power")
        return f"{name} is {resp.get('POWER', '?').lower()}." if not err else f"{name}: {err}."
    st = d.get("sonoff", {}).get("state", {})
    if st.get("switch"):
        return f"{name} was {st['switch']} at the last scan."
    online = "online" if d.get("online") else f"not seen since {d.get('last_seen')}"
    return f"{name} is a {d.get('label')} at {d.get('ip')}, {online}."


_TV_CMDS = {
    "power_off", "power_on", "volume_up", "volume_down", "set_volume", "mute", "unmute",
    "play", "pause", "stop", "channel_up", "channel_down", "notify", "launch", "input",
    "button", "status", "pair",
}


def _tv(device: str, value: str) -> str:
    """value: '<command> [argument]', e.g. 'launch netflix', 'set_volume 20'."""
    parts = (value or "").strip().split(None, 1)
    if not parts:
        return "What should the TV do?"
    cmd = parts[0].lower().replace("-", "_")
    arg = parts[1] if len(parts) > 1 else ""
    if cmd not in _TV_CMDS:
        return f"Unknown TV command '{cmd}'. Try: " + ", ".join(sorted(_TV_CMDS))
    r = _resolve(device, ("lg_tv",))
    if isinstance(r, str):
        return r
    key, d = r
    name = _display(d)

    if cmd == "power_on":
        err = lgtv.wake_on_lan(d.get("mac", ""), d.get("ip", ""))
        return (f"Sent the wake-up signal to {name}. If it stays off, enable 'Turn on "
                f"via Wi-Fi' in the TV's settings." if not err else f"Could not wake {name}: {err}.")

    old_key = d.get("lg", {}).get("key")
    if not old_key and cmd != "pair":
        return (f"{name} is not paired yet. Say 'pair the TV', then press Allow on the TV "
                f"screen.")
    new_key, out, err = lgtv.command(d["ip"], old_key, cmd, arg)
    if new_key and new_key != old_key:
        _save_device(key, lg={"key": new_key})
    if err:
        return f"{name}: {err}."
    if cmd == "pair":
        return f"{name} is paired. I can control it now."
    return f"{name}: {out}."


def _login_problem(err: str) -> bool:
    e = (err or "").lower()
    return any(w in e for w in ("not connected", "no longer valid", "expired", "connect / test"))


def _sonoff_sync(player, login: bool = True) -> str:
    """Fetch every Sonoff's key from the eWeLink account and link it to the
    device on the LAN. With login=True an expired / missing eWeLink login opens
    the eWeLink login page in the PC's browser and waits for it (3 minutes)."""
    from memory.config_manager import get_plugin_config
    cfg = get_plugin_config(NAMESPACE)
    err, cloud = sonoff.cloud_sync(cfg)
    if err and login and _login_problem(err):
        if player:
            try:
                player.write_log("SYS: eWeLink: login expired — opening the eWeLink login page…")
            except Exception:
                pass
        ok, msg = sonoff.oauth_connect(cfg)
        if not ok:
            return (f"Sonoff sync needs the eWeLink login, which did not complete: {msg}. The "
                    f"login page was opened in the PC's browser — the user logs in there.")
        err, cloud = sonoff.cloud_sync(cfg)
    if err:
        return f"Sonoff sync failed: {err}."

    def apply(data):
        devs = data["devices"]
        data["ewelink"] = [{k: c[k] for k in ("id", "name", "model", "channels", "mac")}
                           for c in cloud]
        matched, unmatched = [], []
        for c in cloud:
            target = devs.get(c["mac"]) if c["mac"] else None
            if target is None:   # already linked by ID from an earlier sync
                target = next((d for d in devs.values()
                               if d.get("sonoff", {}).get("id") == c["id"]), None)
            if target is None:
                unmatched.append(c)
                continue
            target["sonoff"] = {**target.get("sonoff", {}), "id": c["id"], "key": c["key"],
                                "channels": c["channels"], "model": c.get("model", ""),
                                "multi": c.get("multi", False)}
            target["kind"], target["label"] = "sonoff", "Sonoff (eWeLink)"
            if not str(target.get("alias") or "").strip():
                target["alias"] = c["name"]
            matched.append(c["name"])
        # One unlinked Sonoff on the LAN and one unlinked in the account: same device.
        lan = [d for d in devs.values()
               if d.get("kind") in ("sonoff", "smart_switch") and not d.get("sonoff", {}).get("key")]
        if len(lan) == 1 and len(unmatched) == 1:
            c = unmatched.pop()
            lan[0]["sonoff"] = {**lan[0].get("sonoff", {}), "id": c["id"], "key": c["key"],
                                "channels": c["channels"], "model": c.get("model", ""),
                                "multi": c.get("multi", False)}
            lan[0]["kind"], lan[0]["label"] = "sonoff", "Sonoff (eWeLink)"
            if not str(lan[0].get("alias") or "").strip():
                lan[0]["alias"] = c["name"]
            matched.append(c["name"])
        # The keys themselves stay in the device records only.
        data["_unlinked"] = [{"id": c["id"], "key": c["key"], "name": c["name"],
                              "channels": c["channels"], "model": c.get("model", ""),
                              "multi": c.get("multi", False)} for c in unmatched]
        return matched, unmatched
    matched, unmatched = store.update(apply)
    msg = f"eWeLink has {len(cloud)} devices. Linked for local control: {', '.join(matched) or 'none'}."
    if unmatched:
        msg += (" Not matched to a device on the network: "
                + ", ".join(c["name"] for c in unmatched)
                + ". Link each with 'link <eWeLink name> to <device>'.")
    return msg


def _tuya_sync() -> str:
    """Fetch i2GO/Tuya local keys and names from the user's Tuya IoT project
    and attach them to the devices the scan found — matched by Tuya device ID
    (which the devices broadcast), or by MAC."""
    from memory.config_manager import get_plugin_config
    err, cloud = tuya.cloud_sync(get_plugin_config(NAMESPACE))
    if err:
        return f"i2GO sync failed: {err}."

    def apply(data):
        devs = data["devices"]
        linked, offline = [], []
        for c in cloud:
            target = next((d for d in devs.values()
                           if d.get("tuya", {}).get("id") == c["id"]), None)
            if target is None and c["mac"]:
                target = devs.get(c["mac"])
            if target is None:
                offline.append(c["name"] or c["id"])
                continue
            target["tuya"] = {**target.get("tuya", {}), "id": c["id"], "key": c["key"],
                              "category": c["category"], "product": c["product"]}
            target["kind"] = "tuya"
            target["label"] = tuya.label(c["category"], c["product"])
            target.setdefault("alias", c["name"])
            linked.append(target.get("alias") or c["name"])
        return linked, offline
    linked, offline = store.update(apply)
    msg = (f"The Tuya account has {len(cloud)} devices. Ready for local control: "
           f"{', '.join(linked) or 'none'}.")
    if offline:
        msg += (" Not found on the network right now: " + ", ".join(offline)
                + " — make sure they are powered and scan again, then sync.")
    return msg


def _link(device: str, ewelink_name: str) -> str:
    data = store.load()
    pending = data.get("_unlinked", [])
    q = (ewelink_name or "").strip().lower()
    c = next((x for x in pending if x["name"].lower() == q or x["id"].lower() == q), None) \
        or next((x for x in pending if q and q in x["name"].lower()), None)
    if not c:
        names = ", ".join(x["name"] for x in pending) or "none — run 'sync Sonoff' first"
        return f"No unlinked eWeLink device called '{ewelink_name}'. Unlinked: {names}."
    r = _resolve(device)
    if isinstance(r, str):
        return r
    key, d = r

    def apply(data):
        dev = data["devices"][key]
        dev["sonoff"] = {**dev.get("sonoff", {}), "id": c["id"], "key": c["key"],
                         "channels": c["channels"], "model": c.get("model", ""),
                         "multi": c.get("multi", False)}
        dev["kind"], dev["label"] = "sonoff", "Sonoff (eWeLink)"
        if not str(dev.get("alias") or "").strip():
            dev["alias"] = c["name"]
        data["_unlinked"] = [x for x in data.get("_unlinked", []) if x["id"] != c["id"]]
    store.update(apply)
    return f"Linked '{c['name']}' to the device at {d.get('ip')}. It's ready for local control."


# Scenes: a list of steps, each {"device", "action", "value"}, run in order.
_SCENE_ACTIONS = {"on", "off", "brightness", "color", "tv", "send"}


# ── Broadlink: learned remote buttons ────────────────────────────────────────

def _remotes() -> dict[str, dict]:
    return {k: d for k, d in store.load()["devices"].items()
            if d.get("kind") == "broadlink"
            and broadlinkctl.capabilities(d.get("broadlink", {}).get("type", ""))["ir"]}


# Spoken forms → the infinitive buttons are usually named with, and articles
# dropped, so "desliga o ar" finds the button "desligar ar".
_VERB_FORMS = {"liga": "ligar", "desliga": "desligar", "aumenta": "aumentar",
               "diminui": "diminuir", "abaixa": "abaixar", "abre": "abrir",
               "fecha": "fechar", "muda": "mudar", "coloca": "colocar", "põe": "pôr"}
_ARTICLES = {"o", "a", "os", "as", "do", "da", "dos", "das", "the"}


def _norm_command(text: str) -> str:
    words = [w for w in re.findall(r"[\wÀ-ÿ]+", (text or "").lower()) if w not in _ARTICLES]
    if words:
        words[0] = _VERB_FORMS.get(words[0], words[0])
    return " ".join(words)


def _find_code(name: str, device: str = ""):
    """(device key, device, code name) for a learned button, matched loosely
    across every Broadlink remote unless a device is named."""
    import difflib
    q = _norm_command(name)
    if not q:
        return None
    pool = _remotes()
    if device:
        # The device named is usually the appliance ("ventilador"), not the
        # Broadlink that holds its buttons: when it isn't a remote, it is only
        # a hint and every remote is searched.
        r = _resolve(device, ("broadlink",))
        if not isinstance(r, str) and r[0] in pool:
            pool = {r[0]: r[1]}
    names = [(k, d, c) for k, d in pool.items() for c in d.get("codes", {})]
    for k, d, c in names:
        if _norm_command(c) == q:
            return k, d, c
    # Fuzzy only among buttons that start with the same word: "desligar ar"
    # is 84% similar to "ligar ar", and pressing ON when asked for OFF is the
    # one mistake this must never make.
    first = q.split()[0]
    same_verb = [_norm_command(c) for _, _, c in names if _norm_command(c).split()[0] == first]
    best = difflib.get_close_matches(q, same_verb, n=1, cutoff=0.72)
    if best:
        return next((k, d, c) for k, d, c in names if _norm_command(c) == best[0])
    # A toggle button names both ways ("acender e apagar a luz"): when every
    # word asked for is in exactly one button, that is the button. Whole words,
    # so "ligar ar" never lands inside "desligar ar".
    words = set(q.split())
    inside = [(k, d, c) for k, d, c in names if words <= set(_norm_command(c).split())]
    if len(inside) == 1:
        return inside[0]
    return None


def _learn(device: str, name: str, signal: str, player, frequency: str = "") -> str:
    name = (name or "").strip().lower()
    if not name:
        return "What should I call this button? (e.g. 'ligar ar', 'ventilador velocidade 2')"
    pool = _remotes()
    if device:
        r = _resolve(device, ("broadlink",))
        if isinstance(r, str):
            return r
        key, d = r
    elif len(pool) == 1:
        key, d = next(iter(pool.items()))
    elif not pool:
        return "I don't know any Broadlink remote yet — scan the network first."
    else:
        return "Which Broadlink? I know: " + ", ".join(_display(x) for x in pool.values()) + "."

    def say(text: str) -> None:
        if player:
            try:
                player.write_log(f"SYS: Broadlink — {text}")
                cb = getattr(player, "request_say", None)
                if callable(cb):
                    cb(f"[SYSTEM] Tell the user, in one short sentence in their language: {text}")
            except Exception:
                pass

    f = str(frequency or "").strip().lower().replace(",", ".").replace("mhz", "").strip()
    signal = "rf" if str(signal).lower() == "rf" or f else "ir"
    if f == "auto":
        freq = None                                   # sweep
    elif f:
        try:
            freq = float(f)
        except ValueError:
            return f"'{frequency}' is not a frequency — say e.g. 433.92 or 315."
    else:
        # The frequency that worked last time on this device, else 433.92.
        freq = d.get("broadlink", {}).get("rf_mhz") or broadlinkctl.DEFAULT_RF_MHZ
    err, code = broadlinkctl.learn(d, signal, say=say, frequency=freq if signal == "rf" else None,
                                   seconds=25)
    if err:
        return f"Could not learn '{name}': {err}."

    def apply(data):
        dev = data["devices"][key]
        dev.setdefault("codes", {})[name] = {"code": code, "signal": signal}
        locked = broadlinkctl.LAST_RF_MHZ or freq
        if signal == "rf" and locked:
            dev.setdefault("broadlink", {})["rf_mhz"] = locked
    store.update(apply)
    return (f"Learned '{name}' on {_display(d)}. Say it any time, or use it in a scene. "
            f"Offer to test it now.")


def _send_code(name: str, device: str = "") -> str:
    m = re.match(r"(.*?)\s*(?:x|vezes|times)\s*(\d+)$", (name or "").strip(), re.I)
    repeat = int(m.group(2)) if m else 1
    base = m.group(1) if m else name
    hit = _find_code(base, device)
    if not hit:
        known = sorted({c for d in _remotes().values() for c in d.get("codes", {})})
        return (f"I have no learned button called '{base}'. "
                + (f"Learned: {', '.join(known)}." if known else
                   "Nothing is learned yet — use 'learn' to teach a remote button."))
    _k, d, c = hit
    err = broadlinkctl.send(d, d["codes"][c]["code"], repeat)
    return f"Sent '{c}'" + (f" ×{repeat}" if repeat > 1 else "") + "." if not err \
        else f"Could not send '{c}': {err}."


def _list_codes() -> str:
    pool = _remotes()
    if not pool:
        return "I don't know any Broadlink remote yet — scan the network first."
    return " ".join(f"{_display(d)}: " + (", ".join(sorted(d.get("codes", {}))) or "nothing learned")
                    + "." for d in pool.values())


def _forget_code(name: str, device: str = "") -> str:
    hit = _find_code(name, device)
    if not hit:
        return f"No learned button called '{name}'."
    k, _d, c = hit
    store.update(lambda data: data["devices"][k].get("codes", {}).pop(c, None))
    return f"Forgot the button '{c}'."


def _scene_save(name: str, steps_raw: str) -> str:
    name = (name or "").strip().lower()
    if not name:
        return "What is the scene called?"
    try:
        steps = json.loads(steps_raw) if isinstance(steps_raw, str) else steps_raw
        assert isinstance(steps, list) and steps
        steps = [{"device": str(s.get("device", "")), "action": str(s["action"]).lower(),
                  "value": str(s.get("value", ""))} for s in steps]
    except Exception:
        return ("Scene steps must be a JSON list like "
                '[{"device":"tv","action":"tv","value":"launch netflix"},'
                '{"device":"living room light","action":"off"}].')
    bad = [s["action"] for s in steps if s["action"] not in _SCENE_ACTIONS]
    if bad:
        return f"Scenes can use: {', '.join(sorted(_SCENE_ACTIONS))} — not {', '.join(bad)}."
    store.update(lambda data: data["scenes"].__setitem__(name, steps))
    return f"Scene '{name}' saved with {len(steps)} steps."


def _scene_run(name: str) -> str:
    scenes = store.load()["scenes"]
    q = (name or "").strip().lower()
    key = q if q in scenes else next((k for k in scenes if q and q in k), None)
    if not key:
        return f"No scene called '{name}'. Scenes: {', '.join(scenes) or 'none yet'}."
    results = []
    for s in scenes[key]:
        a = s["action"]
        if a in ("on", "off"):
            results.append(_power(s["device"], a == "on"))
        elif a == "brightness":
            results.append(_brightness(s["device"], s["value"]))
        elif a == "color":
            results.append(_color(s["device"], s["value"]))
        elif a == "tv":
            results.append(_tv(s["device"], s["value"]))
        elif a == "send":
            results.append(_send_code(s["value"], s["device"]))
    return f"Scene '{key}': " + " ".join(results)


# ── new-device watcher ───────────────────────────────────────────────────────

_watch_started = False


def _alerts_enabled() -> bool:
    return store.load().get("settings", {}).get("alerts", True)


def _watch(player) -> None:
    time.sleep(90)                      # let the app finish starting
    while True:
        try:
            if _alerts_enabled():
                table = scan.quick_scan()
                now = time.strftime("%Y-%m-%d %H:%M")
                new = []

                def apply(data):
                    devs = data["devices"]
                    baseline = not devs
                    by_mac = {k for k in devs if not k.startswith("ip:")}
                    for ip, mac in table.items():
                        if not mac:
                            continue
                        if mac in by_mac:
                            devs[mac].update(ip=ip, last_seen=now, online=True)
                            continue
                        devs[mac] = {"mac": mac, "ip": ip, "vendor": oui.vendor(mac),
                                     "kind": "phone_or_pc" if oui.is_random(mac) else "unknown",
                                     "label": oui.vendor(mac) or "unknown device",
                                     "first_seen": now, "last_seen": now, "online": True}
                        if not baseline:
                            new.append(devs[mac])
                store.update(apply)
                if any(any(w in str(d.get("vendor") or "") for w in ("Espressif", "Sonoff", "ITEAD"))
                       for d in new):
                    threading.Thread(target=_quiet_sonoff_link, args=(player,), daemon=True,
                                     name="sonoff-autolink").start()
                for d in new:
                    who = d["vendor"] or "unknown maker"
                    msg = f"New device on the Wi-Fi: {d['ip']} ({who}, MAC {d['mac']})"
                    print(f"[SmartHome] {msg}")
                    if player:
                        try:
                            player.write_log(f"SYS: {msg}")
                            say = getattr(player, "request_say", None)
                            if callable(say):
                                say(f"[SYSTEM ALERT] A device the user has never had on their "
                                    f"Wi-Fi just connected: IP {d['ip']}, maker: {who}. Tell "
                                    f"them in one short sentence, in their language, and offer "
                                    f"to scan the network to identify it.")
                        except Exception:
                            pass
        except Exception as e:
            print(f"[SmartHome] watcher: {e}")
        time.sleep(WATCH_EVERY)


def _quiet_sonoff_link(player) -> None:
    """A new Espressif chip joined the Wi-Fi: identify it (port 8081 = eWeLink)
    and, if the eWeLink login is still valid, link its key without asking."""
    try:
        found = scan.full_scan()
        _merge_scan(found)
        msg = _sonoff_sync(None, login=False)
        print(f"[SmartHome] auto-link: {msg}")
        if player and "Linked for local control" in msg:
            player.write_log(f"SYS: {msg}")
    except Exception as e:
        print(f"[SmartHome] auto-link: {e}")


def start(player=None) -> None:
    """Called once by the plugin loader when the app starts."""
    global _watch_started
    if _watch_started:
        return
    _watch_started = True
    try:
        store.update(_fix_sonoff_records)
    except Exception as e:
        print(f"[SmartHome] sonoff records: {e}")
    threading.Thread(target=_watch, args=(player,), daemon=True, name="smart-home-watch").start()


def _fix_sonoff_records(data: dict) -> None:
    """One-relay plugs saved as 4-channel (MINI R4 reports four positions) and
    names with stray spaces, from syncs made before the relay table."""
    models = {e.get("id"): e.get("model", "") for e in data.get("ewelink", []) or []}
    for d in data.get("devices", {}).values():
        s = d.get("sonoff")
        if not s or not s.get("id"):
            continue
        model = s.get("model") or models.get(s["id"], "")
        reported = int(s.get("channels") or 1)
        n = sonoff.channel_count(0, reported, model)
        if n != reported:
            s["channels"], s["model"], s["multi"] = n, model, True
        if isinstance(d.get("alias"), str) and d["alias"] != d["alias"].strip():
            d["alias"] = d["alias"].strip()


def _alerts(value: str) -> str:
    on = (value or "").strip().lower() not in ("off", "false", "0", "no", "desligar", "não", "nao")
    store.update(lambda data: data.setdefault("settings", {}).__setitem__("alerts", on))
    return ("I'll tell you when a new device joins the Wi-Fi (checked every 10 minutes)."
            if on else "New-device alerts are off.")


# ── tool entry point ─────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action", "")).strip().lower().replace(" ", "_")
    device = str(p.get("device", "") or "")
    value = str(p.get("value", "") or "")
    try:
        if action == "scan":
            return _do_scan(player)
        if action == "list":
            return _do_list()
        if action == "rename":
            return _do_rename(device, value)
        if action == "forget":
            return _do_forget(device)
        if action in ("on", "off", "toggle"):
            return _power(device, {"on": True, "off": False, "toggle": None}[action])
        if action == "brightness":
            return _brightness(device, value)
        if action == "color":
            return _color(device, value)
        if action == "status":
            return _status(device)
        if action == "tv":
            return _tv(device, value)
        if action == "pair":
            # Pairing waits up to a minute for a button press on the TV, so
            # say what to do now, not after the wait.
            say = getattr(player, "request_say", None) if player else None
            if callable(say):
                say("[SYSTEM] Pairing with the TV now. Tell the user in one short "
                    "sentence, in their language, to press 'Allow' on the TV screen.")
            if player:
                player.write_log("SYS: Pairing with the TV — press Allow on the TV.")
            return _tv(device, "pair")
        if action in ("tuya_sync", "i2go_sync"):
            return _tuya_sync()
        if action == "sonoff_sync":
            return _sonoff_sync(player)
        if action == "link":
            return _link(device, value)
        if action == "scene_save":
            return _scene_save(value, p.get("steps", ""))
        if action == "scene_run":
            return _scene_run(value)
        if action == "scene_list":
            sc = store.load()["scenes"]
            return ("Scenes: " + "; ".join(f"{k} ({len(v)} steps)" for k, v in sc.items())
                    if sc else "No scenes saved yet.")
        if action == "scene_delete":
            q = value.strip().lower()
            gone = store.update(lambda data: data["scenes"].pop(q, None))
            return f"Scene '{q}' deleted." if gone else f"No scene called '{value}'."
        if action == "alerts":
            return _alerts(value)
        if action == "learn":
            return _learn(device, value, str(p.get("signal", "ir")), player,
                          str(p.get("frequency", "") or ""))
        if action == "send":
            return _send_code(value, device)
        if action == "codes":
            return _list_codes()
        if action == "forget_code":
            return _forget_code(value, device)
        return f"Unknown smart-home action '{action}'."
    except Exception as e:
        print(f"[SmartHome] {action} failed: {e}")
        return f"Smart home: {action} failed — {e}"


def _test_clouds(values: dict):
    """Test whichever accounts are filled in: eWeLink (Sonoff), Tuya (i2GO)."""
    results, ok = [], True
    if values.get("ewelink_appid"):
        # First press logs in (OAuth page in the browser); later presses test.
        if not sonoff.has_oauth_token(values):
            good, msg = sonoff.oauth_connect(values)
            if not good:
                ok = False
                results.append(f"eWeLink: {msg}")
        if sonoff.has_oauth_token(values):
            err, cloud = sonoff.cloud_sync(values)
            ok &= not err
            results.append(f"eWeLink: {err or f'{len(cloud)} devices — say sync Sonoff'}")
    if values.get("tuya_api_key"):
        err, cloud = tuya.cloud_sync(values)
        ok &= not err
        results.append(f"Tuya/i2GO: {err or f'{len(cloud)} devices'}")
    if not results:
        return False, "Fill in the eWeLink or the Tuya fields first."
    return ok, " | ".join(results)


PLUGIN = {
    "name": "smart_home",
    "description": (
        "Finds and controls the smart-home devices on the user's Wi-Fi: i2GO "
        "(Tuya) plugs, bulbs and switches, Broadlink remotes / plugs / bulbs, Sonoff switches, LG webOS TV, Tasmota "
        "devices; also identifies Amazon Echo, "
        "Chromecast, router, phones and computers. Actions: "
        "'scan' (discover everything on the network — use for 'what is connected "
        "to my Wi-Fi'); 'list' (saved devices); 'rename' (device + value=new name, "
        "e.g. 'call the Sonoff living room light'); 'forget'; 'on' / 'off' / "
        "'toggle' (device); 'brightness' (device, value=1-100); 'color' (device, "
        "value=colour name or hex); 'status' (device); 'tv' (device optional, "
        "value='<command> [arg]' with command one of power_on, power_off, "
        "volume_up, volume_down, set_volume N, mute, unmute, play, pause, stop, "
        "channel_up, channel_down, launch <app>, input <HDMI 1>, notify <text>, "
        "button <HOME|BACK|ENTER|UP|DOWN|LEFT|RIGHT>, status); 'pair' (first-time "
        "LG TV pairing — the user presses Allow on the TV); 'sonoff_sync' "
        "(fetch Sonoff keys from the user's eWeLink account — needed for every NEW Sonoff; if the "
        "eWeLink login expired it opens the login page on the PC and waits for the user); 'tuya_sync' "
        "(fetch i2GO/Tuya keys from the user's Tuya account, once — use for "
        "'sync i2GO'); 'link' "
        "(device + value=eWeLink device name); 'scene_save' (value=scene name, "
        "steps=JSON list of {device, action: on|off|brightness|color|tv|send, value}); "
        "'scene_run' / 'scene_delete' (value=scene name); 'scene_list'; "
        "'alerts' (value on|off — warn when a new device joins the Wi-Fi). "
        "BROADLINK remotes control appliances with no network (air conditioner, "
        "fan, old TV, sound system) by replaying learned remote buttons: "
        "'learn' (value=button name like 'ligar ar', 'ventilador velocidade 2'; "
        "signal='rf' for radio remotes — gates, blinds, ceiling fans, wireless "
        "switches — on RM Pro models, with frequency in MHz if known (default "
        "433.92; 'auto' to search); the user points the remote and presses the "
        "button when told);'send' (value=button "
        "name, optionally ending 'x3' to repeat — e.g. 'aumentar temperatura x3'); "
        "'codes' (list learned buttons); 'forget_code'. For 'turn on/off the "
        "<appliance>', use 'send' with the learned button name. "
        "'device' is the name the user uses, or an IP. Use this — not "
        "computer_settings — for lights, plugs and the TV. Amazon Echo cannot be "
        "controlled (Amazon has no local API): say so if asked."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of the actions listed above."},
            "device": {"type": "STRING", "description": "Device name, alias or IP."},
            "value": {"type": "STRING",
                      "description": "New name, brightness, colour, TV command, scene name or on/off."},
            "steps": {"type": "STRING",
                      "description": "scene_save only: JSON list of steps."},
            "signal": {"type": "STRING",
                       "description": "learn only: 'ir' (default) or 'rf'."},
            "frequency": {"type": "STRING",
                          "description": "learn rf only: MHz, e.g. '433.92' (default) or 'auto'."},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "Smart Home — eWeLink (Sonoff) and Tuya (i2GO)",
    "fields": [
        {"key": "ewelink_appid", "label": "eWeLink — APPID (dev.ewelink.cc)", "type": "text",
         "placeholder": f"app's Redirect URL: {sonoff.REDIRECT_URL}"},
        {"key": "ewelink_appsecret", "label": "eWeLink — APP SECRET (Console → app → View)",
         "type": "password"},
        {"key": "ewelink_redirect", "label": "eWeLink — Redirect URL registered on the app",
         "type": "text", "placeholder": sonoff.REDIRECT_URL},
        {"key": "tuya_api_key", "label": "i2GO/Tuya — Access ID (iot.tuya.com)", "type": "text"},
        {"key": "tuya_api_secret", "label": "i2GO/Tuya — Access Secret", "type": "password"},
        {"key": "tuya_region", "label": "Tuya data centre (Brazil = us)", "type": "choice",
         "options": list(tuya.REGIONS)},
    ],
    "action": {"label": "CONNECT / TEST", "run": _test_clouds},
}
