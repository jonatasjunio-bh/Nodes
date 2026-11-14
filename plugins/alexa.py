"""
Alexa — the assistant drives the user's Echo devices and everything linked to
Alexa (plugins/_alexa/client.py has the how and the caveats).

    "pede pra Alexa ligar o ar"          → command: "liga o ar" (Alexa does it, and answers on the Echo)
    "anuncia na casa que o jantar tá pronto" → announce (chime + voice, every Echo)
    "fala no Echo do quarto: hora de dormir" → speak
    "toca Legião Urbana no Echo da sala"     → play
    "volume do Echo 30" / "para a música no Echo"
    "roda a rotina Bom dia"               → routine
"""

from __future__ import annotations

from plugins._alexa.client import CLIENT

NAMESPACE = "alexa"
_PROVIDERS = {"amazon": "AMAZON_MUSIC", "amazon music": "AMAZON_MUSIC", "spotify": "SPOTIFY",
              "deezer": "DEEZER", "apple": "APPLE_MUSIC", "tunein": "TUNEIN", "radio": "TUNEIN"}


def _cfg() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config(NAMESPACE)
    except Exception:
        return {}


def connect(values: dict | None = None):
    """⚙ CONNECT / 'connect Alexa': sign in to Amazon in the browser (≤5 min)."""
    v = values or _cfg()
    email = str(v.get("email", "")).strip()
    domain = (str(v.get("domain", "")).strip() or "amazon.com.br").removeprefix("www.")
    if values:
        try:
            from memory.config_manager import save_plugin_config
            save_plugin_config(NAMESPACE, {"email": email, "domain": domain})
        except Exception:
            pass
    return CLIENT.start_login(email, domain)


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action", "")).strip().lower()
    text = str(p.get("text", "") or "").strip()
    device = str(p.get("device", "") or "").strip()

    if action == "connect":
        import threading

        def _go():
            ok, msg = connect()
            say = getattr(player, "request_say_idle", None) if player else None
            if callable(say):
                say(f"[SYSTEM] Alexa sign-in finished: {msg} Tell the user in one short sentence.")
        threading.Thread(target=_go, daemon=True, name="alexa-login").start()
        return ("Amazon's sign-in page is opening in the browser on the PC. Tell the user to sign in "
                "there with their Amazon account (password and code typed by them on Amazon's page); "
                "you'll be told when it's done.")
    if action == "disconnect":
        CLIENT.logout()
        return "Alexa unlinked; the saved Amazon session was deleted."

    err = CLIENT.ensure()
    if err:
        return err

    if action in ("devices", "list"):
        devs = CLIENT.devices(refresh=True)
        if not devs:
            return "No Echo devices on this Amazon account."
        return "Echo devices: " + "; ".join(
            f"{d.get('accountName')}" + ("" if d.get("online") else " (offline)")
            + (" [group]" if d.get("deviceFamily") == "WHA" else "") for d in devs)

    if action == "announce" and not device:
        targets = [d for d in CLIENT.devices() if d.get("online") and d.get("deviceFamily") != "WHA"]
        if not targets:
            return "No Echo is online."
        dev = targets[0]
    else:
        dev = CLIENT.find(device)
        if isinstance(dev, str):
            return dev
    name = dev.get("accountName", "Echo")
    api = CLIENT.api(dev)

    try:
        if action == "command":
            if not text:
                return "Say what Alexa should do (text)."
            CLIENT.call(api.run_custom(text, queue_delay=0.1))
            return (f"Asked Alexa on '{name}': \"{text}\". Alexa carries it out and answers out "
                    f"loud on that Echo herself — don't repeat her answer.")
        if action == "announce":
            if not text:
                return "Say what to announce (text)."
            serials = None if device else [d["serialNumber"] for d in CLIENT.devices()
                                            if d.get("online") and d.get("deviceFamily") != "WHA"]
            CLIENT.call(api.send_announcement(text, targets=serials, queue_delay=0.1))
            return f"Announced on {'every Echo' if serials else name}."
        if action == "speak":
            if not text:
                return "Say what the Echo should say (text)."
            CLIENT.call(api.send_tts(text, queue_delay=0.1))
            return f"'{name}' said it."
        if action == "play":
            if not text:
                return "Say what to play (text)."
            prov = _PROVIDERS.get(str(p.get("provider", "")).strip().lower(), "AMAZON_MUSIC")
            CLIENT.call(api.play_music(prov, text, queue_delay=0.1))
            return f"Playing '{text}' on {name} ({prov.replace('_', ' ').title()})."
        if action == "volume":
            try:
                level = max(0, min(100, int(float(p.get("level", text or 0)))))
            except ValueError:
                return "Volume must be 0–100."
            CLIENT.call(api.set_volume(level / 100, queue_delay=0.1))
            return f"Volume of {name} set to {level}."
        if action in ("pause", "stop"):
            CLIENT.call(api.pause())
            return f"Paused {name}."
        if action in ("resume", "play_again"):
            CLIENT.call(api.play())
            return f"Resumed {name}."
        if action == "next":
            CLIENT.call(api.next())
            return f"Next track on {name}."
        if action == "previous":
            CLIENT.call(api.previous())
            return f"Previous track on {name}."
        if action == "routine":
            if not text:
                return "Say which routine (text = its name or phrase)."
            CLIENT.call(api.run_routine(text, queue_delay=0.1))
            return f"Ran the Alexa routine '{text}' on {name}."
    except Exception as e:
        return f"Alexa did not accept that ({type(e).__name__}: {str(e)[:120]})."
    return f"Unknown Alexa action '{action}'."


def _check(values: dict):
    if CLIENT.linked():
        err = CLIENT.ensure()
        if err:
            return False, err
        n = len(CLIENT.devices(refresh=True))
        return True, f"Linked. {n} Echo devices: " + ", ".join(
            d.get("accountName", "?") for d in CLIENT.devices())
    return connect(values)


PLUGIN = {
    "name": "alexa",
    "description": (
        "Drives the user's Amazon Alexa / Echo devices. 'command' (text = anything you could say "
        "to Alexa, e.g. 'liga o ar', 'qual a previsão', 'adiciona leite na lista' — Alexa does it "
        "and answers on the Echo; use for devices that only Alexa controls); 'announce' (text; "
        "every Echo unless device given, with a chime); 'speak' (text said by one Echo); 'play' "
        "(text = song/artist/playlist/station, provider amazon|spotify|deezer|tunein); 'volume' "
        "(level 0–100); 'pause' | 'resume' | 'next' | 'previous'; 'routine' (text = routine "
        "name); 'devices' (list the Echos); 'connect' (link the Amazon account, once); "
        "'disconnect'. device = Echo name (optional)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "command | announce | speak | play | volume | pause | resume | "
                                      "next | previous | routine | devices | connect | disconnect"},
            "text": {"type": "STRING", "description": "What to say/ask/play, or the routine name"},
            "device": {"type": "STRING", "description": "Echo name, e.g. 'Echo da sala' (optional)"},
            "level": {"type": "STRING", "description": "volume: 0–100"},
            "provider": {"type": "STRING", "description": "play: amazon | spotify | deezer | tunein"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "Alexa (Amazon Echo)",
    "fields": [
        {"key": "email", "label": "E-mail of your Amazon account", "type": "text"},
        {"key": "domain", "label": "Amazon site", "type": "text", "placeholder": "amazon.com.br"},
    ],
    "action": {"label": "CONNECT", "run": _check},
}
