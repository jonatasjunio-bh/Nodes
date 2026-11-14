"""
memory/db.py — the assistant's long-term memory as a local SQLite database.

One file, memory/memory.db, on this computer: no server, no cloud. It replaced
long_term.json (imported once, the JSON kept beside it as
long_term.migrated.json) and memory_manager keeps its old functions on top of
it, so nothing else had to change.

What the database adds over the JSON file:

  entries        the memory itself: (category, key) → the entry as it always was
  history        every change — old value, new value, when, and who made it
                 (the assistant, a correction, the daily consolidation, the
                 user in the panel) — so one edit can be undone on its own
  entries_fts    full-text search over keys and values, accents ignored
                 ("ventilador" finds "Ventilador da suíte"), for recall_memory
  events         what the assistant did and when (tool, action, device) — the
                 raw material for routines: "around 22h you switch the office
                 on" is found here, not guessed
  lesson_stats   how often each lesson was broken again: a lesson the user had
                 to repeat goes to the top of the prompt, marked as such
  meta           small values (the session summaries list)

All access goes through one connection behind one lock: the store is small, the
writers are few, and a single connection is the simplest thing that is safe
from every thread the app runs.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
DB_PATH: Path | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    cat     TEXT NOT NULL,
    key     TEXT NOT NULL,
    data    TEXT NOT NULL,          -- the entry as JSON ({"value", "updated"} or any dict)
    value   TEXT NOT NULL DEFAULT '',
    updated TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (cat, key)
);
CREATE TABLE IF NOT EXISTS history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    cat     TEXT NOT NULL,
    key     TEXT NOT NULL,
    action  TEXT NOT NULL,          -- set | delete
    old     TEXT,                   -- JSON of the entry before (NULL when new)
    new     TEXT,                   -- JSON after (NULL when deleted)
    source  TEXT NOT NULL DEFAULT 'app'
);
CREATE INDEX IF NOT EXISTS history_key ON history (cat, key);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5 (
    cat, key, value, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,          -- tool | ...
    name    TEXT NOT NULL,          -- e.g. smart_home
    action  TEXT NOT NULL DEFAULT '',
    target  TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS lesson_stats (
    key            TEXT PRIMARY KEY,
    violations     INTEGER NOT NULL DEFAULT 0,
    last_violation TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def open_db(path: Path, legacy_json: Path | None = None) -> None:
    """Open (and create) the database; import the old JSON store once."""
    global _conn, DB_PATH
    with _lock:
        if _conn is not None and DB_PATH == path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(_SCHEMA)
        DB_PATH = path
        empty = _conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        if empty and legacy_json is not None and legacy_json.exists():
            _import_json(legacy_json)


def _c() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("memory database not open")
    return _conn


def _value_of(entry) -> str:
    if isinstance(entry, dict):
        if "value" in entry:
            return str(entry.get("value") or "")
        return " ".join(str(v) for v in entry.values() if isinstance(v, (str, int, float)))
    return str(entry or "")


def _import_json(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[MemoryDB] ⚠️ could not read {path.name} for import: {e}")
        return
    if not isinstance(data, dict):
        return
    n = 0
    c = _c()
    c.execute("BEGIN")
    try:
        for cat, items in data.items():
            if cat == "sessions":
                c.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('sessions', ?)",
                          (json.dumps(items if isinstance(items, list) else [], ensure_ascii=False),))
                continue
            if not isinstance(items, dict):
                continue
            for key, entry in items.items():
                _upsert(c, cat, key, entry, source="import")
                n += 1
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    try:
        path.rename(path.with_name("long_term.migrated.json"))
    except Exception:
        pass
    print(f"[MemoryDB] imported {n} entries from {path.name} into {DB_PATH.name}")


def _upsert(c, cat: str, key: str, entry, source: str) -> bool:
    data = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    row = c.execute("SELECT data FROM entries WHERE cat=? AND key=?", (cat, key)).fetchone()
    if row and row[0] == data:
        return False
    val = _value_of(entry)
    upd = (entry.get("updated", "") if isinstance(entry, dict) else "") or ""
    if row:
        c.execute("UPDATE entries SET data=?, value=?, updated=? WHERE cat=? AND key=?",
                  (data, val, upd, cat, key))
        c.execute("DELETE FROM entries_fts WHERE cat=? AND key=?", (cat, key))
    else:
        c.execute("INSERT INTO entries (cat, key, data, value, updated, created) VALUES (?,?,?,?,?,?)",
                  (cat, key, data, val, upd, _now()))
    c.execute("INSERT INTO entries_fts (cat, key, value) VALUES (?,?,?)",
              (cat, key.replace("_", " "), val))
    c.execute("INSERT INTO history (ts, cat, key, action, old, new, source) VALUES (?,?,?,?,?,?,?)",
              (_now(), cat, key, "set", row[0] if row else None, data, source))
    return True


def _delete(c, cat: str, key: str, source: str) -> bool:
    row = c.execute("SELECT data FROM entries WHERE cat=? AND key=?", (cat, key)).fetchone()
    if not row:
        return False
    c.execute("DELETE FROM entries WHERE cat=? AND key=?", (cat, key))
    c.execute("DELETE FROM entries_fts WHERE cat=? AND key=?", (cat, key))
    c.execute("INSERT INTO history (ts, cat, key, action, old, new, source) VALUES (?,?,?,?,?,?,?)",
              (_now(), cat, key, "delete", row[0], None, source))
    return True


# ── the memory as a dict (what memory_manager always handed out) ─────────────

def read_all() -> dict:
    with _lock:
        out: dict = {}
        for cat, key, data in _c().execute("SELECT cat, key, data FROM entries ORDER BY cat, key"):
            try:
                out.setdefault(cat, {})[key] = json.loads(data)
            except Exception:
                continue
        row = _c().execute("SELECT v FROM meta WHERE k='sessions'").fetchone()
        try:
            out["sessions"] = json.loads(row[0]) if row else []
        except Exception:
            out["sessions"] = []
        return out


def write_all(memory: dict, source: str = "app") -> int:
    """Make the database match `memory`: changed entries updated, missing ones
    deleted — each change recorded in history. Returns the number of changes."""
    with _lock:
        c = _c()
        changes = 0
        c.execute("BEGIN")
        try:
            have = {(cat, key) for cat, key in c.execute("SELECT cat, key FROM entries")}
            want = set()
            for cat, items in memory.items():
                if cat == "sessions":
                    c.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('sessions', ?)",
                              (json.dumps(items if isinstance(items, list) else [], ensure_ascii=False),))
                    continue
                if not isinstance(items, dict):
                    continue
                for key, entry in items.items():
                    want.add((cat, key))
                    changes += _upsert(c, cat, key, entry, source)
            for cat, key in have - want:
                changes += _delete(c, cat, key, source)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return changes


# ── search ───────────────────────────────────────────────────────────────────

def fts_search(words: list[str], limit: int = 20) -> list[tuple[str, str, str]]:
    """(cat, key, value) rows matching any of `words` (prefix, accents ignored)."""
    terms = [w.replace('"', "") for w in words if len(w) > 1]
    if not terms:
        return []
    q = " OR ".join(f'"{t}"*' for t in terms)
    with _lock:
        try:
            rows = _c().execute(
                "SELECT cat, key, value FROM entries_fts WHERE entries_fts MATCH ? "
                "ORDER BY bm25(entries_fts) LIMIT ?", (q, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
    return [(c, k.replace(" ", "_"), v) for c, k, v in rows]


# ── history ──────────────────────────────────────────────────────────────────

def history(limit: int = 30, cat: str | None = None, key: str | None = None,
            source: str | None = None) -> list[dict]:
    sql, args = "SELECT id, ts, cat, key, action, old, new, source FROM history", []
    cond = []
    if cat:
        cond.append("cat=?"); args.append(cat)
    if key:
        cond.append("key=?"); args.append(key)
    if source:
        cond.append("source=?"); args.append(source)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = _c().execute(sql, args).fetchall()
    return [{"id": r[0], "ts": r[1], "cat": r[2], "key": r[3], "action": r[4],
             "old": _value_of(json.loads(r[5])) if r[5] else None,
             "new": _value_of(json.loads(r[6])) if r[6] else None, "source": r[7]} for r in rows]


def revert(history_id: int) -> str:
    """Undo one recorded change: put back the value it replaced (or remove what
    it created)."""
    with _lock:
        c = _c()
        r = c.execute("SELECT cat, key, action, old FROM history WHERE id=?", (history_id,)).fetchone()
        if not r:
            return f"No change #{history_id}."
        cat, key, action, old = r
        c.execute("BEGIN")
        try:
            if old is None:
                _delete(c, cat, key, "revert")
            else:
                _upsert(c, cat, key, json.loads(old), "revert")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return f"Reverted change #{history_id} ({cat}/{key})."


def backup(dst: Path) -> None:
    """A consistent copy of the whole database (SQLite's online backup)."""
    with _lock:
        dst.parent.mkdir(parents=True, exist_ok=True)
        out = sqlite3.connect(str(dst))
        try:
            _c().backup(out)
        finally:
            out.close()


# ── events and routines ──────────────────────────────────────────────────────

def log_event(kind: str, name: str, action: str = "", target: str = "", detail: str = "") -> None:
    with _lock:
        try:
            _c().execute("INSERT INTO events (ts, kind, name, action, target, detail) VALUES (?,?,?,?,?,?)",
                         (_now(), kind, name, action[:60], target[:80], detail[:300]))
        except Exception:
            pass


def routines(days: int = 21, min_days: int = 3, window_min: int = 45) -> list[dict]:
    """Actions repeated on several different days at about the same time of
    day: (name, action, target) seen on >= min_days distinct days within a
    ±window around their median minute of day."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        rows = _c().execute("SELECT ts, name, action, target FROM events WHERE kind='tool' AND ts >= ?",
                            (since,)).fetchall()
    groups: dict[tuple, list[tuple[str, int]]] = {}
    for ts, name, action, target in rows:
        if not action:
            continue
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        groups.setdefault((name, action, target), []).append((dt.strftime("%Y-%m-%d"),
                                                              dt.hour * 60 + dt.minute))
    found = []
    for (name, action, target), hits in groups.items():
        mins = sorted(m for _, m in hits)
        med = mins[len(mins) // 2]
        near_days = {d for d, m in hits if abs(m - med) <= window_min}
        if len(near_days) >= min_days:
            found.append({"name": name, "action": action, "target": target,
                          "time": f"{med // 60:02d}:{med % 60:02d}", "days": len(near_days),
                          "weekdays": _weekday_note(near_days)})
    found.sort(key=lambda r: -r["days"])
    return found


def _weekday_note(days: set[str]) -> str:
    wd = {datetime.strptime(d, "%Y-%m-%d").weekday() for d in days}
    if wd and wd <= {0, 1, 2, 3, 4}:
        return "weekdays"
    if wd and wd <= {5, 6}:
        return "weekends"
    return "any day"


# ── lesson statistics ────────────────────────────────────────────────────────

def lesson_violated(key: str) -> int:
    with _lock:
        c = _c()
        c.execute("INSERT INTO lesson_stats (key, violations, last_violation) VALUES (?, 1, ?) "
                  "ON CONFLICT(key) DO UPDATE SET violations = violations + 1, last_violation = ?",
                  (key, _now(), _now()))
        return c.execute("SELECT violations FROM lesson_stats WHERE key=?", (key,)).fetchone()[0]


def lesson_stats() -> dict[str, int]:
    with _lock:
        return {k: v for k, v in _c().execute("SELECT key, violations FROM lesson_stats")}
