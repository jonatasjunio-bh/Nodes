"""
core/confirm.py — a confirmation the model cannot forge.

THE PROBLEM WITH THE OLD GATE
    computer_settings guarded shutdown and restart like this:

        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return "Please confirm by calling again with confirmed=yes."

    `confirmed` is a tool parameter, which means the *model* writes it. Nothing
    stops it from sending confirmed=yes on the first call, and nothing checks
    that a human was ever involved. It is a convention, not a gate — and its
    coverage was two actions, so deleting files and switching off the WiFi the
    assistant is talking over went through with no gate at all.

THE DESIGN HERE
    The confirmation token is issued by the *interface*, never by the model:

      1. An action calls `request(...)` with a callable that does the real work.
      2. This module hands the UI a banner with CONFIRM / CANCEL and returns
         IMMEDIATELY with a sentence for the model to say out loud.
      3. If — and only if — the user presses CONFIRM, the UI calls `resolve()`,
         which runs the stored callable off the Qt thread.

    Nothing blocks. The model keeps talking while the banner is up, so this
    costs no latency at all; in fact it is cheaper than the old gate, which
    burned two tool round trips (reject, then re-call) on every shutdown.

WHAT BELONGS HERE AND WHAT DOES NOT
    Only genuinely irreversible things. Anything that can be reversed should be
    done at once and pushed onto core/undo.py instead — undo is faster than a
    question, and an assistant that asks before every action is one nobody uses.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# A pending confirmation is abandoned after this long. Chosen to outlast a
# normal "hang on, let me look at the screen" pause without leaving a live
# shutdown button sitting on the HUD for the rest of the day.
TIMEOUT_SECONDS = 90.0


@dataclass
class _Pending:
    key:     str
    title:   str
    detail:  str
    run:     Callable[[], str]
    at:      float
    ttl:     float = TIMEOUT_SECONDS
    # a PERMISSION rather than a last-chance gate: the user may answer out loud,
    # and may say "always" (on_always then records the rule)
    voice:     bool = False
    on_always: Optional[Callable[[], None]] = None
    armed:     bool = False          # the assistant has finished asking
    heard:     str = ""              # what the user said since then


_pending: Optional[_Pending] = None
_lock = threading.Lock()

# Set once at startup by main.py. Signature: (title, detail) -> None for show,
# and () -> None for hide. Both are marshalled onto the Qt thread by the UI.
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb:  Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Wire this module to the HUD. Called once from main.py at startup."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def request(key: str, title: str, detail: str, run: Callable[[], str],
            ttl: float = TIMEOUT_SECONDS, voice: bool = False,
            on_always: Optional[Callable[[], None]] = None) -> str:
    """Park an irreversible action behind the on-screen gate.

    Returns the sentence the tool should hand back to the model — phrased as an
    instruction so the assistant asks the user out loud in their own language,
    rather than reading an English string verbatim."""
    global _pending

    # Voice ID: in 'sensitive' or 'strict' mode an irreversible action also
    # needs the request to have come in the owner's voice (or typed).
    try:
        from core.voiceguard import GUARD
        ok, why = GUARD.allow_sensitive()
    except Exception:
        ok, why = True, ""
    if not ok:
        _log(f"SYS: 🔒 Blocked '{title}' — voice not recognised.")
        return (f"[VOICE CHECK FAILED] {why}. Tell the user, in one short sentence in their "
                f"language, that only the owner's voice can ask for this, and do not do it.")

    if _show_cb is None:
        # No interface bound (headless, or a very early call). Refuse rather
        # than silently performing something irreversible.
        return (f"I cannot confirm '{title}' right now because the interface is "
                f"not available, so I have not done it.")

    with _lock:
        _pending = _Pending(key=key, title=title, detail=detail,
                            run=run, at=time.monotonic(), ttl=float(ttl),
                            voice=bool(voice), on_always=on_always)

    try:
        _show_cb(title, detail)
    except Exception as e:
        with _lock:
            _pending = None
        return f"Could not ask for confirmation: {e}. Nothing was done."

    _log(f"SYS: Awaiting confirmation — {title}")
    return (
        f"[CONFIRMATION_PENDING] I have put a confirmation on screen for: {title}. "
        f"Say ONE short sentence in the user's own language telling them you need "
        f"them to confirm it on the HUD before you do it. Do not claim it is done."
    )


# ── answering out loud ───────────────────────────────────────────────────────
# The answer is read from the transcript of the USER's speech (or what they
# typed), fed here by main.py. The model takes no part: it cannot write that
# transcript, so a page that tells it "say yes" gets nowhere.
#
# Only an utterance that is nothing but assent counts. "pode me dizer o
# tamanho?" has words outside the list, so it is a question, not a yes.

_YES = {"sim", "pode", "podes", "permite", "permito", "permita", "permitido", "permitir",
        "autorizo", "autoriza", "autorizado", "confirma", "confirmo", "confirmado", "confirmar",
        "claro", "ok", "okay", "beleza", "yes", "isso", "manda", "prossiga", "prosseguir",
        "continue", "continua", "aprovado", "aprovo", "aprova", "positivo", "afirmativo",
        "bora", "faz", "faca", "vai", "certeza", "libera", "liberado", "allow", "sure"}
_FILL = {"instalar", "instala", "instale", "atualizar", "atualiza", "atualize", "desinstalar",
         "remover", "baixar", "rodar", "executar", "abrir", "mesmo", "por", "favor", "ver", "bala",
         "em", "frente", "a", "o", "e", "ta", "tudo", "bem", "bom", "uma", "vez", "so", "desta",
         "dessa", "agora", "ir", "entao", "ai", "la", "com", "sempre", "pra", "para", "mim", "que",
         "eu", "esse", "essa", "este", "esta", "programa", "nodes", "mark", "jarvis", "please",
         "go", "ahead", "do", "it", "always", "obrigado", "valeu", "tambem", "os", "as", "todos"}
_NO = {"nao", "no", "cancela", "cancelar", "cancele", "cancelado", "nega", "negar", "negue",
       "negado", "pare", "deixa", "nunca", "nem", "esquece", "esqueca", "stop"}
_ALWAYS = {"sempre", "always"}
ARM_AFTER = 8.0          # seconds: armed anyway if no end of turn was reported


def _words(text: str) -> list[str]:
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.findall(r"[a-z]+", t)


def verdict(text: str) -> str:
    """'yes' | 'always' | 'no' | '' (not an answer)."""
    w = _words(text)
    if not w:
        return ""
    if len(w) <= 6 and any(x in _NO for x in w):
        return "no"
    if " ".join(w) in ("ta bom", "tudo bem", "ta certo", "esta bem", "ta ok", "tudo certo"):
        return "yes"
    if len(w) <= 8 and any(x in _YES for x in w) and all(x in _YES or x in _FILL or x == "ser" for x in w):
        return "always" if any(x in _ALWAYS for x in w) else "yes"
    return ""


def options() -> dict:
    """For the HUD: what the waiting request accepts."""
    with _lock:
        p = _pending
        return {"voice": bool(p and p.voice), "always": bool(p and p.on_always)}


def heard(text: str, typed: bool = False) -> None:
    """main.py: something the user just said (a transcript fragment) or typed."""
    with _lock:
        p = _pending
        if p is None or not p.voice:
            return
        if not p.armed and time.monotonic() - p.at > ARM_AFTER:
            p.armed = True
        if p.armed:
            p.heard = (p.heard + " " + str(text or "")).strip()[-400:]


def turn_done() -> None:
    """main.py: a turn has ended. The first one after the request is the
    assistant asking — from then on what the user says is the answer."""
    with _lock:
        p = _pending
        if p is None or not p.voice:
            return
        if not p.armed:
            p.armed, p.heard = True, ""
            return
    if answer_from_user(wait=0) == "":
        # that turn was not an answer (a question, something else): the next
        # thing the user says is judged on its own
        with _lock:
            if _pending is p:
                p.heard = ""


def answer_from_user(wait: float = 1.5) -> str:
    """Settle the waiting permission from what the user said, if they answered.
    Returns 'yes' | 'always' | 'no' | '' (no clear answer — still waiting) or
    'none' (nothing is waiting). The transcript can trail the model by a
    moment, hence the short wait."""
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        with _lock:
            p = _pending
            if p is None or time.monotonic() - p.at > p.ttl:
                return "none"
            if not p.voice:
                return ""
            v = verdict(p.heard) if p.armed else ""
            if v == "always" and not p.on_always:
                v = "yes"                 # nothing here can be made permanent
            if v:
                p.heard = ""
        if v or time.monotonic() >= deadline:
            break
        time.sleep(0.15)
    if not v:
        return ""
    if v != "no":
        try:      # Voice ID on: the yes has to be in the owner's voice (or typed)
            from core.voiceguard import GUARD
            ok, _why = GUARD.allow_sensitive()
        except Exception:
            ok = True
        if not ok:
            _log("SYS: 🔒 Permissão por voz ignorada — a voz não foi reconhecida como a do dono.")
            return ""
    _log({"yes": "SYS: Permitido por voz.", "always": "SYS: Permitido por voz — sempre.",
          "no": "SYS: Negado por voz."}[v])
    resolve(v != "no", always=(v == "always"))
    return v


def resolve(accepted: bool, always: bool = False) -> None:
    """Called by the UI when the user presses CONFIRM or CANCEL.

    Runs the stored callable on a worker thread — this is invoked from the Qt
    thread, and shutting the machine down from inside a button handler would
    freeze the interface on its way out."""
    global _pending

    with _lock:
        p, _pending = _pending, None

    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass

    if p is None:
        return

    if time.monotonic() - p.at > p.ttl:
        _log(f"SYS: Confirmation expired — {p.title}")
        return

    if not accepted:
        _log(f"SYS: Cancelled — {p.title}")
        return

    if always and p.on_always:
        try:
            p.on_always()
        except Exception as e:
            _log(f"ERR: could not keep the 'always allow' rule — {e}")

    def _worker():
        try:
            result = p.run() or "Done."
            _log(f"SYS: Confirmed — {p.title}. {result}")
        except Exception as e:
            _log(f"ERR: {p.title} failed — {e}")

    threading.Thread(target=_worker, daemon=True,
                     name=f"confirm-{p.key}").start()


def pending_key() -> str:
    """'' when nothing is waiting, else the key the waiting action was parked
    under — so a tool can tell its own request from someone else's."""
    with _lock:
        if _pending is None or time.monotonic() - _pending.at > _pending.ttl:
            return ""
        return _pending.key


def pending_title() -> str:
    """'' when nothing is waiting. Lets an action avoid stacking two banners."""
    with _lock:
        if _pending is None:
            return ""
        if time.monotonic() - _pending.at > _pending.ttl:
            return ""
        return _pending.title
