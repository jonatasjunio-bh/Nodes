"""
core/hud_data.py — the live numbers behind the clean HUD, on the PC and the phone.

One background thread gathers, once a second:

    performance   CPU (load, per-core load, clock, power, temperature), GPU
                  (load, temperature, VRAM), memory (use, each stick's
                  temperature), the system SSD (use, temperature, wear) and a
                  minute of CPU / GPU load history — from core/sensors.py
                  (HWiNFO64 when it shares, else LibreHardwareMonitor / NVML)
    network       download / upload rate and a TCP ping, with a minute of history
    agenda        today's events, personal (Google) and shared (iCloud) in one
                  list by time, plus the first one tomorrow — every 5 minutes
    month         every event of the current month, day by day, each day's
                  events grouped by calendar (personal, then shared, then notes)
    assistant     its state and the last sentence it heard (set by the UI)

snapshot() hands back a plain dict: the PC HUD paints from it and main.py sends
the same dict to the phone, so both always show the same thing.
"""

from __future__ import annotations

import html
import socket
import json
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta

HISTORY = 60

_lock = threading.Lock()
_data: dict = {
    "cpu": {}, "gpu": {}, "mem": {}, "ssd": {}, "net": {}, "agenda": [], "tomorrow": None,
    "month": [], "month_name": "",
    "cpu_hist": [], "gpu_hist": [], "net_hist": [], "state": "LISTENING", "muted": False,
    "speaking": False, "heard": "", "name": "JARVIS", "online": True, "t": 0.0,
}
_cpu_hist: deque = deque([0.0] * HISTORY, maxlen=HISTORY)
_gpu_hist: deque = deque([0.0] * HISTORY, maxlen=HISTORY)
_net_hist: deque = deque([0.0] * HISTORY, maxlen=HISTORY)
_started = False


# ── what the UI tells us ─────────────────────────────────────────────────────

def note_heard(text: str) -> None:
    with _lock:
        _data["heard"] = str(text or "").strip()[:160]


def note_state(state: str, speaking: bool, muted: bool) -> None:
    with _lock:
        _data.update(state=str(state or ""), speaking=bool(speaking), muted=bool(muted))


def note_name(name: str) -> None:
    with _lock:
        _data["name"] = str(name or "JARVIS")


def snapshot() -> dict:
    with _lock:
        d = dict(_data)
        d["cpu_hist"], d["gpu_hist"], d["net_hist"] = list(_cpu_hist), list(_gpu_hist), list(_net_hist)
        return d


# ── gathering ────────────────────────────────────────────────────────────────

def _ping_ms() -> float | None:
    """TCP connect time to a public resolver — no admin rights, no console window."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=1.5):
            return (time.perf_counter() - t0) * 1000.0
    except OSError:
        return None


_WEEK = ("SEG", "TER", "QUA", "QUI", "SEX", "SÁB", "DOM")
_MONTHS = ("JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO", "JULHO", "AGOSTO",
           "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO")
_KIND_ORDER = {"pessoal": 0, "compartilhada": 1, "nota": 2}


def _agenda() -> tuple[list[dict], dict | None, list[dict], str]:
    """(today, first tomorrow, the month day by day, the month's name)."""
    from core import briefing
    today = date.today()
    first = today.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    a = briefing.events_between(first, max(last, today + timedelta(days=1)))
    if a.get("errors"):
        print(f"[HUD data] agenda: {'; '.join(map(str, a['errors']))[:300]}")
    out, first_tomorrow = [], None
    days: dict[str, list[dict]] = {}
    for e in a.get("events", []):
        src = e.get("source") or ""
        kind = "compartilhada" if src == "icloud" else "pessoal" if src in ("google", "ics") else "nota"
        item = {"time": e.get("time") or "dia todo", "title": html.unescape(e.get("title", "") or ""),
                "kind": kind,
                "calendar": html.unescape(e.get("calendar") or ("Google" if src == "google" else ""))}
        d = e.get("date") or ""
        if d == today.isoformat():
            out.append(item)
        elif first_tomorrow is None and d == (today + timedelta(days=1)).isoformat():
            first_tomorrow = item
        if first.isoformat() <= d <= last.isoformat():
            day = days.setdefault(d, [])
            if item not in day:          # the same holiday from two Google calendars
                day.append(item)
    # today always has a row, so it can be highlighted even when it is free
    days.setdefault(today.isoformat(), [])
    month = []
    for d in sorted(days):
        dd = date.fromisoformat(d)
        evs = sorted(days[d], key=lambda i: (_KIND_ORDER.get(i["kind"], 9), i.get("calendar") or "",
                                             i["time"] != "dia todo", i["time"]))
        month.append({"date": d, "label": f"{_WEEK[dd.weekday()]} {dd.day:02d}",
                      "today": dd == today, "past": dd < today, "events": evs})
    return out, first_tomorrow, month, _MONTHS[today.month - 1]


def _knowledge() -> tuple[int | None, list[dict]]:
    """How much the assistant knows, for the golden sphere to grow with: the
    memories it keeps (secrets left out) plus every time it learned or
    corrected one (memory/memory.db) — and that history itself, oldest first,
    for the sphere's nodes to stand for. Read-only, a few milliseconds."""
    try:
        import sqlite3
        from pathlib import Path
        db = Path(__file__).resolve().parent.parent / "memory" / "memory.db"
        if not db.exists():
            return None, []
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            kept = c.execute("SELECT COUNT(*) FROM entries WHERE cat NOT IN ('security')").fetchone()[0]
            changes = c.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            rows = c.execute("SELECT id, ts, cat, key, action, old, new, source FROM history "
                             "WHERE cat NOT IN ('security') AND source != 'secure' ORDER BY id").fetchall()
        finally:
            c.close()
        items = []
        for hid, ts, cat, key, action, old, new, source in rows:
            raw = new if new else old
            try:
                v = json.loads(raw) if raw else ""
                v = v.get("value", "") if isinstance(v, dict) else v
            except Exception:
                v = raw or ""
            items.append({"id": hid, "ts": ts, "cat": cat, "key": key, "action": action,
                          "value": str(v)[:240], "source": source})
        return int(kept) + int(changes) // 3, items
    except Exception:
        return None, []


def _loop() -> None:
    import psutil
    from core import sensors
    net0 = psutil.net_io_counters()
    t_net = time.monotonic()
    last_ping = last_agenda = last_known = 0.0
    ping = None
    psutil.cpu_percent(percpu=True)               # prime the counters
    while True:
        time.sleep(1.0)
        try:
            s = sensors.snapshot(max_age=0.9)
            per = psutil.cpu_percent(percpu=True) or []
            # 16 threads → 8 cores: the two threads of a core, averaged
            cores = [round((per[i] + per[i + 1]) / 2) for i in range(0, len(per) - 1, 2)] if len(per) >= 2 else per
            c, g, m = s.get("cpu", {}), s.get("gpu", {}), s.get("memory", {})
            vm = psutil.virtual_memory()
            try:
                du = psutil.disk_usage("C:\\")
                disk_pct = du.percent
            except Exception:
                disk_pct = None
            drive = (s.get("drives") or [{}])[0]
            now = time.monotonic()
            io = psutil.net_io_counters()
            dt = max(0.2, now - t_net)
            down = (io.bytes_recv - net0.bytes_recv) * 8 / dt / 1e6
            up = (io.bytes_sent - net0.bytes_sent) * 8 / dt / 1e6
            net0, t_net = io, now
            if now - last_ping > 15:
                ping, last_ping = _ping_ms(), now
            cpu_load = c.get("load") or 0.0
            gpu_load = g.get("load") or 0.0
            with _lock:
                _data["cpu"] = {"load": cpu_load, "temp": c.get("temp"), "clock": c.get("clock"),
                                "power": c.get("power"), "cores": cores, "name": c.get("name")}
                _data["gpu"] = {"load": gpu_load, "temp": g.get("temp"), "vram_used": g.get("vram_used"),
                                "vram_total": g.get("vram_total"), "name": g.get("name")}
                _data["mem"] = {"used_gb": vm.used / 2**30, "total_gb": vm.total / 2**30, "pct": vm.percent,
                                "temps": [d.get("temp") for d in m.get("dimms", []) if d.get("temp") is not None]}
                _data["ssd"] = {"pct": disk_pct, "temp": drive.get("temp"), "life": drive.get("life"),
                                "name": drive.get("name")}
                _data["net"] = {"down": down, "up": up, "ping": ping}
                _data["t"] = time.time()
                _cpu_hist.append(float(cpu_load))
                _gpu_hist.append(float(gpu_load))
                _net_hist.append(float(down))
            if now - last_known > 20 or last_known == 0.0:
                last_known = now
                known, items = _knowledge()
                with _lock:
                    _data["knowledge"] = known
                    _data["knowledge_items"] = items
            if now - last_agenda > 300 or last_agenda == 0.0:
                last_agenda = now
                threading.Thread(target=_refresh_agenda, daemon=True, name="hud-agenda").start()
        except Exception as e:
            print(f"[HUD data] {type(e).__name__}: {e}")


_agenda_lock = threading.Lock()     # one calendar read at a time: the Google client is not thread-safe


def _refresh_agenda() -> None:
    try:
        with _agenda_lock:
            items, tomorrow, month, month_name = _agenda()
        with _lock:
            _data["agenda"], _data["tomorrow"] = items, tomorrow
            _data["month"], _data["month_name"] = month, month_name
    except Exception as e:
        print(f"[HUD data] agenda: {type(e).__name__}: {e}")


def refresh_agenda_soon() -> None:
    """After an event is added or removed: show it without waiting 5 minutes."""
    threading.Thread(target=_refresh_agenda, daemon=True, name="hud-agenda").start()


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="hud-data").start()
