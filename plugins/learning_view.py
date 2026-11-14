"""
Learning view — a page in the HUD that shows what the assistant has learned.

    click the golden sphere (or one of its nodes)   → this page (on that item)
    "mostra o que você aprendeu"                    → this page

It reads memory/memory.db only, and shows:

    the timeline     every change to the memory, newest first, with where it came
                     from (your correction, something it chose to keep, the daily
                     tidy-up, a default) and what it replaced — each can be undone
    lessons          each rule, since when, and how many times it was broken again
                     (the best sign of whether a lesson is sticking)
    habits           how it does each recurring job now, and every rewrite
    what it knows    how many things per category
    growth           the sphere's synapses day by day

The server is the same pattern as world_cams / satellites: 127.0.0.1 only, a
random port, and a token made at each start on every /api call.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent / "_learning"

_state = {"server": None, "port": 0, "token": secrets.token_urlsafe(16)}
_lock = threading.Lock()
_HIDDEN = ("security",)                  # never shown: the voice keyword lives there


def _rows(sql: str, args=()) -> list[tuple]:
    import sqlite3
    db = BASE / "memory" / "memory.db"
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def _val(raw) -> str:
    if not raw:
        return ""
    try:
        v = json.loads(raw)
        return str(v.get("value", "") if isinstance(v, dict) else v)
    except Exception:
        return str(raw)


def data() -> dict:
    hist = _rows("SELECT id, ts, cat, key, action, old, new, source FROM history "
                 "WHERE cat NOT IN ('security') AND source != 'secure' ORDER BY id")
    timeline = [{"id": h[0], "ts": h[1], "cat": h[2], "key": h[3], "action": h[4],
                 "old": _val(h[5]), "new": _val(h[6]), "source": h[7]} for h in hist]

    entries = _rows("SELECT cat, key, value, updated, created FROM entries WHERE cat NOT IN ('security')")
    cats: dict[str, int] = {}
    for cat, *_ in entries:
        cats[cat] = cats.get(cat, 0) + 1
    try:
        broken = dict(_rows("SELECT key, violations FROM lesson_stats"))
    except Exception:
        broken = {}
    lessons = sorted(({"key": k, "value": _val(v) or v, "updated": u, "created": c,
                       "broken": int(broken.get(k, 0))}
                      for cat, k, v, u, c in entries if cat == "lessons"),
                     key=lambda x: (x["broken"], x["updated"] or ""), reverse=True)
    try:
        from memory.learning import DEFAULT_HABITS
    except Exception:
        DEFAULT_HABITS = {}
    habits = []
    for cat, k, v, u, c in entries:
        if cat != "habits":
            continue
        changes = [t for t in timeline if t["cat"] == "habits" and t["key"] == k]
        habits.append({"key": k, "value": _val(v) or v, "default": DEFAULT_HABITS.get(k, ""),
                       "changes": changes, "learned": any(t["source"] not in ("default",) for t in changes)})

    # the sphere's size day by day: what was kept + every three changes
    alive, n_changes, by_day = set(), 0, {}
    for t in timeline:
        n_changes += 1
        if t["action"] == "delete":
            alive.discard((t["cat"], t["key"]))
        else:
            alive.add((t["cat"], t["key"]))
        by_day[t["ts"][:10]] = min(360, int(48 + 1.5 * (len(alive) + n_changes // 3)))
    growth = [{"day": d, "synapses": n} for d, n in sorted(by_day.items())]

    week = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    stats = {"entries": len(entries), "lessons": len(lessons),
             "habits_learned": sum(1 for h in habits if h["learned"]),
             "week": sum(1 for t in timeline if t["ts"][:10] >= week),
             "corrections": sum(1 for t in timeline if t["source"] == "correction"),
             "synapses": growth[-1]["synapses"] if growth else 48}
    return {"ok": True, "timeline": timeline[::-1][:600], "lessons": lessons, "habits": habits,
            "cats": cats, "growth": growth, "stats": stats}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if not u.path.startswith("/api/"):
                return self._send(404, b"{}")
            if not secrets.compare_digest(q.get("t", ""), _state["token"]):
                return self._json({"ok": False, "error": "forbidden"}, 403)
            name = u.path[5:]
            if name == "data":
                return self._json(data())
            if name == "revert":
                from memory import memory_manager as mm
                hid = int(q.get("id", "0"))
                row = _rows("SELECT cat FROM history WHERE id=?", (hid,))
                if not row or row[0][0] in _HIDDEN:
                    return self._json({"ok": False, "error": "Essa mudança não pode ser desfeita aqui."})
                return self._json({"ok": True, "result": mm.revert_change(hid)})
            return self._json({"ok": False, "error": "unknown"}, 404)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)[:200]}, 500)


def url(focus: int | None = None) -> str:
    """Start the server once; the page's URL (token included, focused on a change)."""
    with _lock:
        if _state["server"] is None:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            srv.daemon_threads = True
            _state["server"], _state["port"] = srv, srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True, name="learning-http").start()
    return f"http://127.0.0.1:{_state['port']}/?t={_state['token']}" + (f"&focus={int(focus)}" if focus else "")


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "open").strip().lower()
    if action == "close":
        if player is not None and hasattr(player, "close_web"):
            player.close_web()
        return "Closed the learning page."
    page = url()
    if player is not None and getattr(player, "web_available", lambda: False)():
        player.show_web(page, "Aprendizado")
        return "The learning page is open on the HUD."
    webbrowser.open(page)
    return "The learning page opened in the browser."


PLUGIN = {
    "name": "learning_view",
    "description": (
        "Opens a page on the HUD showing WHAT THE ASSISTANT HAS LEARNED: the timeline of memory "
        "changes (with their source and undo), lessons and how often each was broken again, "
        "habits and their rewrites, what it knows per category, growth. Use for 'mostra o que você "
        "aprendeu', 'abre o aprendizado', 'quero ver suas lições', 'fecha o aprendizado'. "
        "action: open | close."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {"action": {"type": "STRING", "description": "open | close"}},
        "required": [],
    },
}
