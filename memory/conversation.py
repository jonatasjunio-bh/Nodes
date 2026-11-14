"""
memory/conversation.py — what was said lately, kept across restarts.

long_term.json holds FACTS about the user; it never held the conversation
itself, so every launch started a blank chat: the assistant knew your name
and your GPS app, but not that ten minutes ago you were in the middle of
planning dinner. This file keeps the last turns on disk, written as each
turn completes, and hands them back as context when a new session starts.

Bounded on purpose: the last MAX_TURNS turns, none older than MAX_AGE_DAYS,
and at most PROMPT_CHARS of them in the prompt — enough to pick the thread
back up, not a transcript that grows the prompt forever.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

PATH = Path(__file__).resolve().parent / "conversation.json"
MAX_TURNS = 60
MAX_AGE_DAYS = 3
PROMPT_CHARS = 3500
_lock = threading.Lock()


def _load() -> list[dict]:
    try:
        turns = json.loads(PATH.read_text(encoding="utf-8")).get("turns", [])
        return turns if isinstance(turns, list) else []
    except Exception:
        return []


def append(who: str, text: str) -> None:
    """who: 'user' or 'assistant'. Called once per completed turn."""
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        turns = _load()
        turns.append({"t": time.time(), "who": who, "text": text[:1500]})
        cutoff = time.time() - MAX_AGE_DAYS * 86400
        turns = [x for x in turns if x.get("t", 0) >= cutoff][-MAX_TURNS:]
        try:
            tmp = PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"turns": turns}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, PATH)
        except Exception as e:
            print(f"[Conversation] could not save: {e}")


def _when(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    days = (datetime.now().date() - d.date()).days
    day = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"
    return f"{day} {d.strftime('%H:%M')}"


def prompt_block(asst_name: str = "Assistant") -> str:
    """The recent conversation for the system prompt, newest turns kept first
    when trimming, or '' when there is nothing recent."""
    with _lock:
        turns = _load()
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    turns = [x for x in turns if x.get("t", 0) >= cutoff]
    if not turns:
        return ""
    lines, used = [], 0
    for x in reversed(turns):
        who = "User" if x.get("who") == "user" else asst_name
        line = f"[{_when(x['t'])}] {who}: {x['text']}"
        if used + len(line) > PROMPT_CHARS:
            break
        lines.append(line)
        used += len(line)
    lines.reverse()
    return ("[RECENT CONVERSATION — from before you were last restarted]\n"
            "This is what you and the user said most recently. Treat it as your own memory "
            "of the conversation: continue naturally when the user refers to it (\"as I was "
            "saying\", \"that thing from before\"), but do not repeat it, summarise it or "
            "mention it unprompted.\n" + "\n".join(lines) + "\n\n")


def clear() -> None:
    with _lock:
        try:
            PATH.unlink()
        except FileNotFoundError:
            pass
