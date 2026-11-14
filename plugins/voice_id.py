"""
Voice ID — the owner's voice as a key (see core/voiceguard.py).

    "enrol my voice" / "cadastra minha voz"  → ~12 s of natural speech
    "voice ID only for sensitive commands"    → mode sensitive
    "only answer my voice"                    → mode strict
    "turn voice ID off"                       → mode off
    "who is speaking?" / "voice ID status"    → enrolled? mode? last match score
    "delete my voiceprint"                    → forget it (confirmed on the HUD)
"""

from __future__ import annotations

from core.voiceguard import GUARD, NAMESPACE

_MODES = {"off": "off", "desligar": "off", "sensitive": "sensitive", "sensivel": "sensitive",
          "sensível": "sensitive", "strict": "strict", "estrito": "strict", "only_me": "strict"}


def _save(values: dict) -> None:
    from memory.config_manager import save_plugin_config
    save_plugin_config(NAMESPACE, values)
    GUARD._cfg_cache = (0.0, {})         # apply at once, not after the 2 s cache


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action", "")).strip().lower()
    value = str(p.get("value", "") or "").strip().lower()

    if action == "enroll":
        if GUARD.enrolling():
            GUARD.cancel_enroll()        # asked again: start over rather than stay stuck

        def done(msg):
            say = getattr(player, "request_say", None) if player else None
            if callable(say):
                say(f"[SYSTEM] Voice enrolment finished: {msg} Tell the user in one short "
                    f"sentence, in their language.")
        GUARD.start_enroll(12.0, done)
        GUARD.preload()
        if player:
            try:
                player.write_log("SYS: 🎙️ Cadastro de voz: fale naturalmente por ~15 s "
                             "(o assistente não ouve até terminar).")
            except Exception:
                pass
        return ("Voice enrolment started. Ask the user, in one or two sentences in their language, "
                "to talk naturally for about 15 seconds — for example describing their day — in a "
                "quiet room, at their usual distance from the microphone. Then stay silent and let "
                "them talk — you hear nothing until it is done, then you will be told the result.")

    if action == "mode":
        mode = _MODES.get(value.replace(" ", "_"))
        if not mode:
            return "Mode must be off, sensitive or strict."
        if mode != "off" and not GUARD.enrolled():
            return "The owner's voice is not enrolled yet — enrol it first ('enroll')."
        _save({"mode": mode})
        return {"off": "Voice ID is off.",
                "sensitive": "Voice ID on: irreversible actions now need the owner's voice.",
                "strict": "Voice ID strict: I will only listen to the owner's voice."}[mode]

    if action == "status":
        if not GUARD.enrolled():
            return "No voice is enrolled. Voice ID is not in use."
        s = GUARD.last_score
        last = ("" if s is None else
                f" Last utterance similarity {s:.2f} (threshold {GUARD.threshold():.2f}) — "
                + ("recognised as the owner." if s >= GUARD.threshold() else "not the owner."))
        return f"Voice enrolled. Mode: {GUARD.mode()}.{last}"

    if action == "forget":
        from core import confirm

        def _do():
            GUARD.forget()
            _save({"mode": "off"})
            return "Voiceprint deleted; Voice ID is off."
        return confirm.request("voice_forget", "Delete the owner's voiceprint",
                               "Voice ID will be turned off.", _do)

    return f"Unknown Voice ID action '{action}'."


def _check(values: dict):
    mode = str(values.get("mode", "off")).lower()
    try:
        th = float(values.get("threshold") or 0.5)
    except ValueError:
        return False, "The threshold must be a number between 0.2 and 0.9."
    if mode != "off" and not GUARD.enrolled():
        return False, "Enrol your voice first: say 'cadastra minha voz' to the assistant."
    GUARD._cfg_cache = (0.0, {})
    return True, (f"Mode {mode}, threshold {th:.2f}. Lower = more tolerant (fewer false "
                  f"rejections), higher = stricter.")


PLUGIN = {
    "name": "voice_id",
    "description": (
        "Voice biometrics — recognising the owner's voice. Actions: 'enroll' "
        "(record the owner's voiceprint: they talk for ~15 s); 'mode' (value off | "
        "sensitive — irreversible actions need the owner's voice | strict — only "
        "the owner's voice is heard at all); 'status' (enrolled? mode? did the last "
        "utterance match?); 'forget' (delete the voiceprint, confirmed on screen)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "enroll | mode | status | forget"},
            "value": {"type": "STRING", "description": "mode only: off | sensitive | strict"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "Voice ID (voice biometrics)",
    "fields": [
        {"key": "mode", "label": "Mode", "type": "choice", "options": ["off", "sensitive", "strict"]},
        {"key": "threshold", "label": "Match threshold (0.2–0.9, default 0.50)", "type": "text",
         "placeholder": "0.50"},
    ],
    "action": {"label": "CHECK", "run": _check},
}
