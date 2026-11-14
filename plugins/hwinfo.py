"""
HWiNFO — every sensor HWiNFO64 reads, for the assistant (core/hwinfo.py).

    "qual a temperatura do SSD?"          "como estão as ventoinhas?"
    "quanto a placa de vídeo já esquentou hoje?"   (máximo desde que o HWiNFO abriu)
    "qual a tensão do processador?"       "mostra o VRM no HUD"

Quiet by design: it watches in the background and speaks up only when
something stays past a critical limit, at most once every 30 minutes per
sensor, as a card on the HUD (and by voice only if chosen). It also keeps
Shared Memory on: the free HWiNFO turns it off after 12 hours; with the
scheduled task set up (button in ⚙ → Central de Módulos → HWiNFO) it is
reopened silently and the data comes back.
"""

from __future__ import annotations

import threading
import time

NAMESPACE = "hwinfo"
DEFAULTS = {"alerts": "cartão", "cpu_max": "92", "gpu_max": "86", "ssd_max": "68", "ram_max": "75",
            "auto_reenable": "sim"}

_state = {"alerted": {}, "over": {}, "had_shm": False, "warned_shm": 0.0, "player": None}


def _cfg() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        c = get_plugin_config(NAMESPACE) or {}
    except Exception:
        c = {}
    return {**DEFAULTS, **{k: v for k, v in c.items() if v not in (None, "")}}


def _num(v, default):
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return default


def status_text() -> str:
    from core import hwinfo
    src = hwinfo.source()
    n = len(hwinfo.readings())
    if src == "shared memory":
        s = f"HWiNFO64 connected through Shared Memory: {n} readings."
    elif src == "gadget":
        s = (f"HWiNFO64 connected through the Gadget registry: only {n} chosen readings "
             f"(Shared Memory is off — in the free version it turns off after 12 hours).")
    elif hwinfo.running():
        s = ("HWiNFO64 is running but shares nothing: enable Settings → 'Shared Memory Support' "
             "and keep the sensor window open (it can be minimized).")
    else:
        s = "HWiNFO64 is not running."
    s += " Automatic re-enable: " + ("on." if hwinfo.task_exists() else "off.")
    return s


def run(parameters: dict, player=None, session_memory=None) -> str:
    from core import hwinfo
    p = parameters or {}
    action = str(p.get("action", "search")).strip().lower()
    query = str(p.get("query") or p.get("value") or "").strip()
    if action == "status":
        return status_text()
    if not hwinfo.readings():
        return status_text()
    if action in ("groups", "list", "sensors"):
        groups: dict[str, int] = {}
        for r in hwinfo.readings():
            groups[r["sensor"]] = groups.get(r["sensor"], 0) + 1
        return "HWiNFO sensors (readings each): " + "; ".join(f"{k} ({v})" for k, v in groups.items())
    if action in ("hot", "top", "hottest"):
        temps = sorted((r for r in hwinfo.readings() if r["type"] == "temperature" and r["value"] == r["value"]
                        and 0 < r["value"] < 150), key=lambda r: -r["value"])[:8]
        return "Hottest now: " + "; ".join(f"{r['sensor'].split(':', 1)[-1].strip()} · {hwinfo.fmt(r, True)}"
                                          for r in temps)
    if action == "alerts":
        c = _cfg()
        return (f"Alerts: {c['alerts']}. Limits: CPU {c['cpu_max']} °C, GPU {c['gpu_max']} °C, "
                f"SSD {c['ssd_max']} °C, RAM {c['ram_max']} °C (must hold for 20 s; at most one "
                f"alert per sensor every 30 min).")
    # search (default)
    if not query:
        return "What should I look for (query)? e.g. 'ssd temperatura', 'ventoinha', 'vrm', 'tensão cpu'."
    hits = hwinfo.find(query, limit=20)
    if not hits:
        return (f"No HWiNFO reading matches '{query}'. Try other words, or action 'groups' to list "
                f"the sensors.")
    lines = [f"{r['sensor'].split(':', 1)[-1].strip()} · {hwinfo.fmt(r, stats=True)}" for r in hits]
    return f"HWiNFO — {len(hits)} reading(s) for '{query}':\n" + "\n".join(lines)


# ── quiet background watch ───────────────────────────────────────────────────

def _limits(c: dict) -> list[tuple[str, str, str, float]]:
    """(name, label regex, sensor regex, limit)"""
    return [
        ("processador", r"^CPU \(Tctl/Tdie\)$|^CPU Die", r"CPU", _num(c["cpu_max"], 92)),
        ("placa de vídeo", r"^GPU Temperature$", r"GPU", _num(c["gpu_max"], 86)),
        ("SSD", r"^Drive Temperature", r"S\.M\.A\.R\.T|Drive", _num(c["ssd_max"], 68)),
        ("memória RAM", r"SPD Hub Temperature|^Temperature$", r"DIMM|DDR\d", _num(c["ram_max"], 75)),
    ]


def _alert(player, text: str, speak: bool) -> None:
    try:
        player.write_log(f"SYS: 🌡 {text}")
    except Exception:
        pass
    try:
        player.hud_widget("show", {"id": "hwinfo_alert", "type": "text", "title": "⚠ HARDWARE",
                                   "text": text, "color": "red", "ttl": 90})
    except Exception:
        pass
    if speak:
        say = getattr(player, "request_say_idle", None)
        if callable(say):
            say(f"[SYSTEM] Hardware alert from HWiNFO: {text} Tell the user in one short sentence, "
                f"in their language, without alarm.")


def _watch() -> None:
    import re
    from core import hwinfo
    while True:
        time.sleep(15)
        player = _state["player"]
        try:
            c = _cfg()
            src = hwinfo.source()
            # Shared Memory switched itself off (free version, 12 h): reopen HWiNFO
            if src == "shared memory":
                _state["had_shm"] = True
            elif _state["had_shm"] and hwinfo.running():
                _state["had_shm"] = False
                if c["auto_reenable"] == "sim" and hwinfo.restart_hwinfo():
                    print("[HWiNFO] Shared Memory went off — HWiNFO reopened to turn it back on")
                elif time.time() - _state["warned_shm"] > 6 * 3600 and player is not None:
                    _state["warned_shm"] = time.time()
                    try:
                        player.write_log("SYS: 🌡 HWiNFO desligou o Shared Memory (limite de 12 h da versão "
                                         "gratuita). Ative o religamento automático em ⚙ → HWiNFO.")
                    except Exception:
                        pass
            if c["alerts"] == "desligado" or player is None:
                continue
            data = hwinfo.readings()
            now = time.time()
            for name, lab, sen, limit in _limits(c):
                for r in data:
                    if r["type"] != "temperature" or r["value"] != r["value"]:
                        continue
                    if not (re.search(lab, r["label"]) and re.search(sen, r["sensor"])):
                        continue
                    key = f"{r['sensor']}|{r['label']}"
                    if r["value"] >= limit:
                        first = _state["over"].setdefault(key, now)
                        if now - first >= 20 and now - _state["alerted"].get(key, 0) > 1800:
                            _state["alerted"][key] = now
                            _alert(player, f"{name} em {r['value']:.0f} °C (limite {limit:.0f} °C) — "
                                           f"{r['sensor'].split(':', 1)[-1].strip()}",
                                   speak=c["alerts"] == "cartão e voz")
                    else:
                        _state["over"].pop(key, None)
        except Exception as e:
            print(f"[HWiNFO] watch: {type(e).__name__}: {e}")


def start(player) -> None:
    _state["player"] = player
    threading.Thread(target=_watch, daemon=True, name="hwinfo-watch").start()


PLUGIN = {
    "name": "hwinfo",
    "description": (
        "Any hardware sensor HWiNFO64 reads — hundreds: CPU per-core clocks/voltages/power, each CCD, "
        "every RAM stick, GPU (hot spot, memory junction, fans, power), motherboard (VRM, chipset, "
        "every fan header and voltage rail), drives (SSD temperature, remaining life, errors), with "
        "min/max/average since HWiNFO started. Use for detailed hardware questions beyond pc_sensors "
        "(which covers the basics). action: search (query = words, Portuguese or English, e.g. "
        "'ssd temperatura', 'ventoinha', 'vrm', 'tensão cpu') | hot (hottest parts now) | groups "
        "(list the sensors) | status | alerts. To SHOW one on the HUD use hud_display with "
        "source 'hw:<words>' (e.g. 'hw:ssd temperatura')."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "search | hot | groups | status | alerts"},
            "query": {"type": "STRING", "description": "search: words naming the reading"},
        },
        "required": [],
    },
}


def _setup(values: dict):
    """Settings button: turn on automatic re-enable (one administrator prompt)."""
    from core import hwinfo
    msgs = [status_text()]
    if str(values.get("auto_reenable") or "sim") == "sim" and not hwinfo.task_exists():
        ok, msg = hwinfo.create_task()
        msgs.append(("Religamento automático ativado." if ok else f"Religamento automático: {msg}."))
        return ok, " | ".join(msgs)
    return bool(hwinfo.readings()), " | ".join(msgs)


PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "HWiNFO64 — sensores completos e alertas discretos",
    "fields": [
        {"key": "alerts", "label": "Alertas de temperatura crítica", "type": "choice",
         "options": ["cartão", "cartão e voz", "desligado"]},
        {"key": "cpu_max", "label": "Limite do processador (°C)", "type": "text", "placeholder": "92"},
        {"key": "gpu_max", "label": "Limite da placa de vídeo (°C)", "type": "text", "placeholder": "86"},
        {"key": "ssd_max", "label": "Limite dos SSDs (°C)", "type": "text", "placeholder": "68"},
        {"key": "ram_max", "label": "Limite da memória RAM (°C)", "type": "text", "placeholder": "75"},
        {"key": "auto_reenable", "label": "Religar o Shared Memory sozinho (limite de 12 h da versão gratuita)",
         "type": "choice", "options": ["sim", "não"]},
    ],
    "action": {"label": "VERIFICAR / ATIVAR RELIGAMENTO", "run": _setup},
}
