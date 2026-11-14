"""
Agenda view — the calendar screen of the NODES HUD ("Ver agenda").

    the month as a grid (dots on the days with something), or one week; the day
    picked at the right with its events — edit, delete, remind — and what comes
    next; a new event; back to the HUD.

Everything goes through the same paths the assistant uses (plugins/
daily_briefing.py): adding picks Google / iCloud / a local note the same way,
deleting from a real calendar asks for confirmation on the HUD first, and a
reminder is actions/reminder.py. Reading is core/briefing.events_between.

Server: the same pattern as the other HUD pages — 127.0.0.1, a random port, a
token made at each start on every /api call.
"""
from __future__ import annotations

import html
import json
import secrets
import threading
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent / "_agenda"
_state = {"server": None, "port": 0, "token": secrets.token_urlsafe(16)}
_lock = threading.Lock()
on_close = None              # set by the HUD: closes its web page


def _kind(e: dict) -> str:
    src = e.get("source") or ""
    return "compartilhada" if src == "icloud" else "pessoal" if src in ("google", "ics") else "nota"


# months already read: the page opens at once on what is there and refreshes
# behind it (a full read of the calendars takes seconds)
_MONTH_TTL = 300
_months: dict = {}               # (y, m) → (time, data)
_refreshing: set = set()


def _key_add(y: int, m: int, k: int) -> tuple[int, int]:
    n = y * 12 + (m - 1) + k
    return n // 12, n % 12 + 1


def month(y: int, m: int) -> dict:
    """The month from the cache when there is one (refreshing it in the
    background when it is old), read now when there is not."""
    import time as _time
    hit = _months.get((y, m))
    if hit is None:
        data = _read_month(y, m)
    else:
        data = dict(hit[1])
        stale = _time.time() - hit[0] > _MONTH_TTL
        data["stale"] = stale
        if stale:
            _refresh(y, m)
    for k in (-1, 1):                       # the months either side, for ◀ ▶
        ny, nm = _key_add(y, m, k)
        if (ny, nm) not in _months:
            _refresh(ny, nm)
    return data


def _refresh(y: int, m: int) -> None:
    if (y, m) in _refreshing:
        return
    _refreshing.add((y, m))

    def go():
        try:
            _read_month(y, m)
        except Exception as e:
            print(f"[Agenda] {y}-{m:02d}: {e}")
        finally:
            _refreshing.discard((y, m))
    threading.Thread(target=go, daemon=True, name="agenda-month").start()


def forget(date_s: str) -> None:
    """An event changed on this date: that month is read again next time."""
    try:
        y, m = int(date_s[:4]), int(date_s[5:7])
        _months.pop((y, m), None)
        _refresh(y, m)
    except Exception:
        _months.clear()


def _read_month(y: int, m: int) -> dict:
    import time as _time
    from core import briefing
    first = date(y, m, 1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    # a few days either side, for the week view at the month's edges
    a = briefing.events_between(first - timedelta(days=7), last + timedelta(days=7))
    seen, events = set(), []
    for e in a.get("events", []):
        item = {"date": e.get("date") or "", "time": e.get("time") or "",
                "title": html.unescape(e.get("title") or ""), "where": html.unescape(e.get("where") or ""),
                "calendar": html.unescape(e.get("calendar") or ("Google" if e.get("source") == "google" else "")),
                "kind": _kind(e)}
        k = (item["date"], item["time"], item["title"])
        if k not in seen:                       # the same holiday from two calendars
            seen.add(k)
            events.append(item)
    data = {"ok": True, "year": y, "month": m, "today": date.today().isoformat(), "events": events,
            "errors": [str(x)[:160] for x in a.get("errors") or []], "stale": False}
    _months[(y, m)] = (_time.time(), data)
    return data


def calendars() -> list[str]:
    out = []
    try:
        from core import google_calendar as g
        if g.connected():
            out += [g._name(c) for c in g.calendars() if c.get("accessRole") in ("owner", "writer")]
    except Exception:
        pass
    try:
        from core import briefing
        if all(briefing._icloud_creds()):
            out.append("iCloud (compartilhada)")
    except Exception:
        pass
    out.append("Nota local")
    return out


def _brief(**p) -> str:
    from plugins import daily_briefing
    return daily_briefing.run(p)


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
            if name == "month":
                return self._json(month(int(q["y"]), int(q["m"])))
            if name == "calendars":
                return self._json({"ok": True, "calendars": calendars()})
            if name == "add":
                cal = q.get("calendar", "")
                cal = "icloud" if cal.startswith("iCloud") else "local" if cal.startswith("Nota") else cal
                r = _brief(action="add_event", value=q.get("title", ""), date=q.get("date", ""),
                           time=q.get("time", ""), minutes=q.get("minutes", "60"), calendar=cal,
                           where=q.get("where", ""))
                forget(q.get("date", ""))
                return self._json({"ok": not r.upper().startswith(("NOT ADDED", "COULD NOT")), "result": r})
            if name == "remove":
                r = _brief(action="remove_event", value=q.get("title", ""), date=q.get("date", ""))
                forget(q.get("date", ""))
                return self._json({"ok": True, "result": r})
            if name == "remind":
                from actions.reminder import reminder
                r = reminder({"date": q.get("date", ""), "time": q.get("time", ""),
                              "message": q.get("message", "Lembrete")})
                return self._json({"ok": True, "result": r})
            if name == "close":
                if callable(on_close):
                    on_close()
                return self._json({"ok": True})
            return self._json({"ok": False, "error": "unknown"}, 404)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)[:200]}, 500)


def url() -> str:
    with _lock:
        if _state["server"] is None:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            srv.daemon_threads = True
            _state["server"], _state["port"] = srv, srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True, name="agenda-http").start()
    return f"http://127.0.0.1:{_state['port']}/?t={_state['token']}"


def start(player) -> None:
    """At launch: read this month and the ones either side, quietly, so the
    calendar opens at once."""
    t = date.today()
    for k in (0, -1, 1):
        _refresh(*_key_add(t.year, t.month, k))


def run(parameters: dict, player=None, session_memory=None) -> str:
    global on_close
    p = parameters or {}
    if str(p.get("action") or "open").lower() == "close":
        if player is not None and hasattr(player, "close_web_panel"):
            player.close_web_panel()
        return "Closed the calendar."
    if player is not None and getattr(player, "web_available", lambda: False)():
        # a panel over the HUD, not the whole of it
        on_close = player.close_web_panel
        player.show_web_panel(url(), "Agenda")
        return "The calendar is open on the HUD."
    webbrowser.open(url())
    return "The calendar opened in the browser."


PLUGIN = {
    "name": "agenda_view",
    "description": (
        "Opens the CALENDAR SCREEN on the HUD (month grid, week, the day's events with edit / delete "
        "/ remind, new event). Use for 'abre a agenda', 'mostra o calendário', 'ver agenda do mês', "
        "'fecha a agenda'. To add, find or remove an event by voice keep using daily_briefing. "
        "action: open | close."
    ),
    "parameters": {"type": "OBJECT",
                   "properties": {"action": {"type": "STRING", "description": "open | close"}},
                   "required": []},
}
