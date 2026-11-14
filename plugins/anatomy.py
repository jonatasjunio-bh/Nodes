"""
Anatomy — explains a technical idea as a real, working machine: an interactive
isometric figure whose moving parts perform the concept (the open-source
Anatomy skill, run by MARK itself — core/anatomy.py).

    "explica como funciona um motor a jato com o Anatomy"
    "faz uma figura do Anatomy de como funciona o cache LRU, detalhada"
    "abre a figura do motor a jato" · "quais figuras do Anatomy eu tenho?"

A figure takes several minutes (the brain writes it, MARK builds and checks it
and sends a photo back for review): fast ≈ 5–10 min, detailed ≈ 15–25 min. It
runs in the background and opens in its own window when ready.
"""

from __future__ import annotations

import threading

_running: dict[str, float] = {}
_lock = threading.Lock()


def _work(concept: str, detailed: bool, player) -> None:
    from core import anatomy

    def log(msg: str) -> None:
        try:
            if player is not None:
                player.write_log("SYS: " + msg)
        except Exception:
            pass
        print("[Anatomy]", msg)

    say = (getattr(player, "request_say_idle", None) or getattr(player, "request_say", None)) \
        if player is not None else None
    try:
        page = anatomy.make(concept, detailed, log)
        anatomy.open_window(page)
        shot = page.parent / "shot.png"
        # a holographic preview on the screen too, while the window opens
        try:
            if shot.exists() and hasattr(player, "hud_widget"):
                player.hud_widget("show", {"id": "anatomy_preview", "type": "holo",
                                           "image": str(shot), "title": concept[:50],
                                           "subtitle": "figura interativa aberta numa janela",
                                           "colors": "original", "stage": True, "ttl": 20,
                                           "hide_value": True})
        except Exception:
            pass
        log(f"🧩 Anatomy: figura pronta — {page}")
        if callable(say):
            say(f"[SYSTEM] The Anatomy figure explaining '{concept}' is ready and open in its own "
                f"window (interactive: hover or use the keys to drive it). Tell the user in one or two "
                f"short sentences.")
    except Exception as e:
        log(f"🧩 Anatomy: não consegui montar a figura ({str(e)[:120]})")
        if callable(say):
            say(f"[SYSTEM] The Anatomy figure for '{concept}' could not be built ({str(e)[:100]}). "
                f"Tell the user briefly.")
    finally:
        with _lock:
            _running.pop(concept, None)


def run(parameters: dict, player=None, session_memory=None) -> str:
    import time
    from core import anatomy
    p = parameters or {}
    action = str(p.get("action", "explain") or "explain").strip().lower()
    concept = str(p.get("concept") or p.get("value") or "").strip()

    if not anatomy.installed():
        return "The Anatomy skill is not installed (tools/anatomy is missing)."
    if action == "list":
        figs = anatomy.list_figures()
        return ("Anatomy figures: " + "; ".join(f"{f['concept']} ({f['made']})" for f in figs[:15])
                if figs else "No Anatomy figures yet.")
    if action == "status":
        with _lock:
            jobs = {k: int(time.time() - v) for k, v in _running.items()}
        return ("Building: " + "; ".join(f"'{k}' for {v // 60} min" for k, v in jobs.items())
                if jobs else "No Anatomy figure is being built.")
    if action == "open":
        page = anatomy.existing(concept) if concept else None
        if page is None:
            figs = anatomy.list_figures()
            match = [f for f in figs if concept and concept.lower() in f["concept"].lower()] or \
                    ([] if concept else figs)
            if not match:
                return f"No Anatomy figure found for '{concept}'."
            from pathlib import Path
            page = Path(match[0]["dir"]) / "page.html"
        anatomy.open_window(page)
        return "Opened the figure in its window."

    if not concept:
        return "What should the figure explain (concept)?"
    page = anatomy.existing(concept)
    if page is not None and not p.get("rebuild"):
        anatomy.open_window(page)
        return "This figure already exists — opened it in its window (say 'refaz' to rebuild it)."
    with _lock:
        if concept in _running:
            return "That figure is already being built."
        _running[concept] = time.time()
    detailed = bool(p.get("detailed"))
    threading.Thread(target=_work, args=(concept, detailed, player), daemon=True,
                     name="anatomy").start()
    mins = "15 to 25" if detailed else "5 to 10"
    return (f"Started the Anatomy figure for '{concept}'. It takes about {mins} minutes (the brain "
            f"designs a working machine, MARK builds and reviews it); it opens in its own window when "
            f"ready and you'll be told. Tell the user that in one short sentence.")


PLUGIN = {
    "name": "anatomy",
    "description": (
        "Anatomy: explains a technical concept (how an engine, algorithm, protocol, cache, circuit, "
        "mechanism… works) by building an interactive isometric figure of a REAL working machine "
        "whose moving parts perform the idea, with a live readout. Use when the user asks to "
        "explain how something works visually, 'with the Anatomy', or for an explanatory figure. "
        "action: explain (default; concept = what to explain, detailed = richer but slower, "
        "rebuild = redo an existing one) | open (concept = which one) | list | status. Takes "
        "5–10 min (detailed 15–25) in the background and opens in its own window."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "explain | open | list | status"},
            "concept": {"type": "STRING", "description": "What the figure should explain"},
            "detailed": {"type": "BOOLEAN", "description": "Richer figure, about 2–3× slower"},
            "rebuild": {"type": "BOOLEAN", "description": "Build again even if it exists"},
        },
        "required": [],
    },
}
