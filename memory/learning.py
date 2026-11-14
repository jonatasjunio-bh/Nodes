"""
memory/learning.py — how the assistant gets better without being asked.

Two jobs, both off the conversation's critical path:

  LESSONS FROM CORRECTIONS
    The assistant only remembered what it chose to save, and it rarely chose to
    save the moment it was told it got something wrong — so the same mistake
    came back the next day. Now every user line is checked (a regex, no model)
    for the shape of a correction: "não, é o Waze", "errou", "de novo", "eu
    disse…", "not that". When it matches, a side model reads the last few turns
    and the tools just used, and writes ONE short rule ("Navigation: always use
    Waze") into the 'lessons' category, which rides in every session's prompt.

  CONSOLIDATION
    The store only ever grew: duplicates ("escritório dia" = "escritório
    noite"), facts that stopped being true ("current task: fixing the app"),
    values the user later corrected ("velocidad 3"). Once a day, at startup and
    in the background, a side model reviews the whole store and returns edits —
    merge, update, move to the right category, delete trivia. They are applied
    conservatively (identity and security are never touched by it, lessons only
    updated, never dropped) after a dated backup of the file.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from memory import memory_manager as mm

STATE_PATH = mm.BASE_DIR / "memory" / "learning_state.json"
BACKUP_DIR = mm.BASE_DIR / "memory" / "backups"
CONSOLIDATE_EVERY_H = 20
_lock = threading.Lock()

# ── corrections ──────────────────────────────────────────────────────────────

_CORRECTION = re.compile(
    r"(?ix)"
    r"(^|\b)("
    r"n[aã]o[,!.\s]+(é|era|foi|quero|queria|pedi|esse|essa|isso|o|a|no|na|pelo|pela|com)\b"
    r"|n[aã]o\s+(é|era|foi)\s+(isso|esse|essa|assim)"
    r"|errad[oa]s?|errou|erra(ndo)?\b|est[aá]\s+errado"
    r"|de\s+novo|outra\s+vez|j[aá]\s+(te\s+)?(falei|disse|pedi|expliquei)"
    r"|eu\s+(te\s+)?(disse|falei|pedi)"
    r"|na\s+verdade|corrigindo|corrige|o\s+certo\s+[eé]"
    r"|n[aã]o\s+[eé]\s+(o|a)\s+\w+[,\s]+[eé]\s+(o|a)"
    r"|prefiro|sempre\s+(use|usa|abra|abre|fa[cç]a)|nunca\s+(use|usa|abra|abre|fa[cç]a)"
    # how the user wants it done from now on ("quantifique antes de informar")
    r"|da\s+pr[oó]xima\s+vez|a\s+partir\s+de\s+agora|daqui\s+pra\s+frente|toda\s+vez\s+que"
    r"|sempre\s+que|pare\s+de|para\s+de|evite"
    r"|n[aã]o\s+precisa\s+(dizer|falar|repetir|explicar|detalhar|ler|contar|mencionar|perguntar|lembrar)"
    r"|s[oó]\s+(me\s+)?(diga|fale|d[eê]|mostre|responda)|em\s+poucas\s+palavras|mais\s+curt[oa]"
    r"|muito\s+(long[oa]|comprid[oa])|falando\s+demais|fala\s+demais|sem\s+(enrolar|explicar)"
    r"|quantifi\w*|enumer\w*|uma\s+(de\s+cada|por)\s+vez|um\s+(de\s+cada|por)\s+vez"
    r"|antes\s+de\s+(falar|informar|dizer|come[cç]ar|ler|dar)|n[aã]o\s+entendi\s+nada"
    r"|uma\s+em\s+cima\s+da\s+outra|um\s+em\s+cima\s+do\s+outro|fal(e|a)\s+mais\s+devagar"
    r"|quero\s+que\s+voc[eê]\s+(sempre|fale|diga|me)|j[aá]\s+pedi"
    r"|that'?s\s+(wrong|not)|not\s+that|wrong|i\s+said|i\s+told\s+you|actually\b|again\b"
    r")"
)


def is_correction(text: str) -> bool:
    t = (text or "").strip()
    return 3 <= len(t) <= 400 and bool(_CORRECTION.search(t))


# words that say a lesson is about how things are presented / spoken
_PRESENTING = re.compile(
    r"(?i)news|not[ií]cia|headline|manchete|briefing|resum|list|count|quantif|how many"
    r"|one (at a time|by one)|ordinal|enumerat|alert|notification|pause"
    r"|style|closing question|item")


# ── habits ───────────────────────────────────────────────────────────────────
# How the assistant does its recurring jobs. They start as these defaults and
# from then on live in memory (category "habits"): a correction rewrites them
# and the code only reads them — so a mistake the user corrected is never built
# back in by the code. Never in the system prompt (they would crowd the lessons
# out); each is handed over where its job happens.
DEFAULT_HABITS = {
    "greeting": ("First words of the day: a warm greeting right for the time of day, the time, "
                 "that you are getting the weather, agenda and news, and briefly what you did "
                 "together last time. Two short sentences at most."),
    "briefing": ("Morning briefing, about 40 seconds: the weather (temperature now, min/max, whether "
                 "and when it rains); today's agenda, each event with its time, or that the day is "
                 "free; the three most important national headlines; the one or two most relevant "
                 "local headlines. Skip routine sports and any local headline that repeats a national "
                 "one, never read out news sources, and end by saying the full list is on screen."),
    "topic_alerts": "New developments on the topics the user follows: all of them together, one short sentence each.",
    "notices": ("Notices from the apps (a task finished, a device event, a warning, a result that "
                "arrived late): one short sentence, in the user's language."),
    "still_working": ("When a task takes more than a few seconds: one short sentence saying you are "
                      "checking and will tell them in a moment — never answer from memory or guess meanwhile."),
    "goodbye": "When shutting down: a brief, natural goodbye.",
}


def habit(key: str) -> str:
    """The current way of doing `key`: the learned one, else the default."""
    try:
        v = mm._entry_value((mm.load_memory().get("habits", {}) or {}).get(key))
        if v:
            return v
    except Exception:
        pass
    return DEFAULT_HABITS.get(key, "")


def seed_habits() -> None:
    """Put the defaults in memory once, so they can be seen, corrected and
    rewritten like anything else the assistant knows."""
    try:
        have = mm.load_memory().get("habits", {}) or {}
        missing = {k: {"value": v} for k, v in DEFAULT_HABITS.items() if k not in have}
        if missing:
            with mm.changes_by("default"):
                mm.update_memory({"habits": missing})
    except Exception as e:
        print(f"[Learning] habits: {e}")


def guidance() -> str:
    """What the user taught about presenting things, to ride at the end of any
    instruction the application sends — the last word, so it wins."""
    taught = lessons_about_presenting()
    if not taught:
        return ""
    return ("\n\nTHE USER'S LESSONS — they override any format, length or style asked for "
            "above:\n" + "\n".join(f"- {r}" for r in taught))


def lessons_about_presenting() -> list[str]:
    """The lessons on how to present and speak things — for prompts that carry
    their own format (the morning briefing), so what the user taught wins."""
    try:
        return [v for k, v in _existing_lessons().items()
                if v and (_PRESENTING.search(v) or _PRESENTING.search(k))][:8]
    except Exception:
        return []


def _existing_lessons() -> dict[str, str]:
    return {k: mm._entry_value(v) for k, v in (mm.load_memory().get("lessons", {}) or {}).items()}


def learn_from_correction(turns: list[str], tools: list[str]) -> str | None:
    """Ask a side model what rule the user's correction implies and save it as
    a lesson. Returns the lesson saved, or None. Blocking — run in a thread."""
    from core import gemini
    lessons = _existing_lessons()
    habits = {k: habit(k) for k in DEFAULT_HABITS}
    convo = "\n".join(turns[-10:])
    prompt = (
        "You maintain the rule book of a personal voice assistant. The user just reacted to "
        "something the assistant did. Decide if the user CORRECTED the assistant or stated how "
        "they want something done from now on. If so, write ONE short, general, reusable rule in "
        "English (max 160 characters) that would have prevented the mistake — concrete names "
        "included (apps, devices, people, words). If the user was not correcting anything, or the "
        "rule would only apply to this one moment, answer NONE.\n"
        "Write the rule for the whole KIND of situation the user meant, naming it, not only for "
        "the words they used: a complaint about how the news were read covers the morning "
        "briefing and every list of news, alerts or items; one about a device covers that "
        "device in every request.\n"
        "If an existing lesson covers the same thing, reuse its key so it is replaced.\n\n"
        f"Existing lessons (key: rule):\n{json.dumps(lessons, ensure_ascii=False)}\n\n"
        "The assistant's HABITS — how it does its recurring jobs (key: how):\n"
        f"{json.dumps(habits, ensure_ascii=False)}\n"
        "If the correction is about how one of these jobs is done, rewrite that habit instead: "
        'answer {"habit": "its_key", "rule": "the whole habit rewritten with the correction '
        'applied (max 400 characters)"}.\n\n'
        f"Tools the assistant just used:\n{chr(10).join(tools[-6:]) or '(none)'}\n\n"
        f"Conversation (most recent last):\n{convo}\n\n"
        'Answer ONLY with JSON {"key": "short_snake_case", "rule": "..."} or the word NONE.'
    )
    try:
        out = (gemini.text(prompt, gemini.FAST, None, 30_000) or "").strip()
    except Exception as e:
        print(f"[Learning] correction: {e}")
        return None
    if not out or out.upper().startswith("NONE"):
        return None
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    hk = str(d.get("habit") or "").strip().lower()
    if hk in DEFAULT_HABITS:
        text = str(d.get("rule") or "").strip()[:420]
        if len(text) < 8:
            return None
        with mm.changes_by("correction"):
            mm.update_memory({"habits": {hk: {"value": text}}})
        print(f"[Learning] 🔁 habit {hk}: {text}")
        return text
    key = re.sub(r"[^a-z0-9_]+", "_", str(d.get("key") or "").lower()).strip("_")[:48]
    rule = str(d.get("rule") or "").strip()[:200]
    if not key or len(rule) < 8:
        return None
    if key in lessons:                      # the same mistake again: it counts
        try:
            n = mm._db().lesson_violated(key)
            print(f"[Learning] lesson {key} broken again ({n}×)")
        except Exception:
            pass
    with mm.changes_by("correction"):
        mm.update_memory({"lessons": {key: {"value": rule}}})
    print(f"[Learning] 📘 lesson {key}: {rule}")
    return rule


# ── consolidation ────────────────────────────────────────────────────────────

_PROTECTED = {"identity", "security"}          # never edited by consolidation
_MOVABLE = ["home", "preferences", "relationships", "projects", "wishes", "notes",
            "personal", "lessons"]


def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, indent=2), encoding="utf-8")


def _backup() -> Path | None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"memory-{datetime.now():%Y%m%d-%H%M%S}.db"
    try:
        mm._db().backup(dst)
    except Exception as e:
        print(f"[Learning] backup: {e}")
        return None
    for old in sorted(BACKUP_DIR.glob("memory-*.db"))[:-10]:   # keep the last 10
        try:
            old.unlink()
        except Exception:
            pass
    return dst


def consolidate() -> str:
    """Review the whole store with a side model and apply its edits. Blocking."""
    from core import gemini
    with _lock:
        memory = mm.load_memory()
        view = {c: {k: mm._entry_value(v) for k, v in items.items() if not mm.is_secret(c, k)}
                for c, items in memory.items()
                if isinstance(items, dict) and c not in _PROTECTED and items}
        view = {c: v for c, v in view.items() if v}
        if sum(len(v) for v in view.values()) < 6:
            return "nothing to consolidate"
        prompt = (
            "You curate the long-term memory of a personal voice assistant (JSON below: category "
            "-> key -> value). Make it accurate, deduplicated and useful. Categories: home (the "
            "house, rooms, devices, how to operate them, routines/scenes), preferences, "
            "relationships, projects (only ACTIVE ones), wishes, notes, personal, lessons (rules "
            "from the user's corrections).\n"
            "Return edits as JSON with these optional lists:\n"
            '  "delete": ["cat/key", ...]   — trivia with no future use (one-off states like '
            '"did pilates", "rent paid: true", "status: entering game mode"), stale facts, '
            "placeholders (\"pending user input\")\n"
            '  "update": [{"key": "cat/key", "value": "..."}] — fix values contradicted by other '
            "entries or by a lesson (e.g. a sequence saved with a word the user later corrected)\n"
            '  "merge":  [{"into": "cat/key", "value": "...", "remove": ["cat/key", ...]}] — '
            "duplicates or near-duplicates become one entry\n"
            '  "move":   [{"from": "cat/key", "to": "cat/key"}] — e.g. device knowledge to home\n'
            "Rules: be conservative — when unsure, leave it. Never delete or contradict a lesson "
            "(you may reword one). Values stay in English, concise. Do not invent facts.\n\n"
            f"MEMORY:\n{json.dumps(view, ensure_ascii=False, indent=1)}\n\n"
            "Answer ONLY with the JSON object."
        )
        try:
            out = (gemini.text(prompt, gemini.SMART, None, 90_000) or "").strip()
        except Exception as e:
            return f"failed: {e}"
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return "no edits returned"
        try:
            ops = json.loads(m.group(0))
        except Exception as e:
            return f"unreadable edits: {e}"

        def split(ref: str):
            c, _, k = str(ref or "").partition("/")
            return c.strip(), k.strip()

        def ok_cat(c: str) -> bool:
            return c in _MOVABLE and c not in _PROTECTED

        def ok_key(c: str, k: str) -> bool:
            return ok_cat(c) and not mm.is_secret(c, k)

        today = datetime.now().strftime("%Y-%m-%d")
        done = {"delete": 0, "update": 0, "merge": 0, "move": 0}
        backup = _backup()
        memory = mm.load_memory()                       # fresh: a save may have landed meanwhile

        for ref in ops.get("delete") or []:
            c, k = split(ref)
            if ok_key(c, k) and c != "lessons" and k in memory.get(c, {}):
                del memory[c][k]
                done["delete"] += 1
        for u in ops.get("update") or []:
            c, k = split(u.get("key"))
            v = str(u.get("value") or "").strip()
            if ok_key(c, k) and k in memory.get(c, {}) and v:
                memory[c][k] = {"value": mm._truncate_value(v), "updated": today}
                done["update"] += 1
        for g in ops.get("merge") or []:
            c, k = split(g.get("into"))
            v = str(g.get("value") or "").strip()
            if not (ok_key(c, k) and k and v):
                continue
            memory.setdefault(c, {})[k] = {"value": mm._truncate_value(v), "updated": today}
            for ref in g.get("remove") or []:
                c2, k2 = split(ref)
                if ok_key(c2, k2) and c2 != "lessons" and (c2, k2) != (c, k) and k2 in memory.get(c2, {}):
                    del memory[c2][k2]
            done["merge"] += 1
        for mv in ops.get("move") or []:
            c1, k1 = split(mv.get("from"))
            c2, k2 = split(mv.get("to"))
            if ok_key(c1, k1) and ok_key(c2, k2) and c1 != "lessons" and k1 in memory.get(c1, {}) and k2:
                memory.setdefault(c2, {})[k2] = memory[c1].pop(k1)
                done["move"] += 1

        if any(done.values()):
            with mm.changes_by("consolidation"):
                mm.save_memory(memory)
        summary = ", ".join(f"{n} {k}" for k, n in done.items() if n) or "no changes"
        print(f"[Learning] 🧹 memory consolidated: {summary}"
              + (f" (backup {backup.name})" if backup and any(done.values()) else ""))
        return summary


def update_routines() -> list[str]:
    """Routines found in the action log (memory/db.py events) become 'home'
    entries the assistant reads: "around 22:10 on weekdays the user switches
    the office on". Ones no longer seen are removed. Local, no model."""
    try:
        found = mm._db().routines()[:6]
    except Exception as e:
        print(f"[Learning] routines: {e}")
        return []
    memory = mm.load_memory()
    home = memory.setdefault("home", {})
    today = datetime.now().strftime("%Y-%m-%d")
    keep, lines = set(), []
    for r in found:
        key = "routine_" + re.sub(r"[^a-z0-9]+", "_",
                                  f"{r['name']}_{r['action']}_{r['target']}".lower()).strip("_")[:50]
        what = f"{r['name']} {r['action']}" + (f" {r['target']}" if r["target"] else "")
        val = (f"Habit: around {r['time']} ({r['weekdays']}) the user asks for: {what} — seen on "
               f"{r['days']} days in the last 3 weeks. Offer it proactively near that time.")
        keep.add(key)
        if mm._entry_value(home.get(key)) != val:
            home[key] = {"value": val, "updated": today}
        lines.append(val)
    for k in [k for k in home if k.startswith("routine_") and k not in keep]:
        del home[k]
    with mm.changes_by("routines"):
        mm.save_memory(memory)
    if lines:
        print(f"[Learning] 🔁 {len(lines)} routine(s) recognised")
    return lines


def consolidate_if_due() -> None:
    """At startup, in a background thread: once every CONSOLIDATE_EVERY_H hours."""
    st = _state()
    if time.time() - float(st.get("consolidated_at", 0)) < CONSOLIDATE_EVERY_H * 3600:
        return
    update_routines()
    try:
        result = consolidate()
    except Exception as e:
        result = f"failed: {e}"
        print(f"[Learning] consolidation: {e}")
    if not result.startswith("failed"):
        st["consolidated_at"] = time.time()
        st["last_result"] = result
        _save_state(st)


def start_background() -> None:
    seed_habits()
    def run():
        time.sleep(45)                 # let the session connect first
        consolidate_if_due()
    threading.Thread(target=run, daemon=True, name="memory-consolidate").start()
