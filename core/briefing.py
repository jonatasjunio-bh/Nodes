"""
core/briefing.py — what the assistant tells you when it opens.

Gathers, in parallel and each with its own deadline:

  news      national headlines and headlines about the user's city, from
            Google News RSS — no key, no quota, and the same feed the Google
            News app reads, so it is current to the minute.
  weather   now / today's min-max / rain chance and the hours it is likely,
            from Open-Meteo — free, no key, geocodes the city by name.
  agenda    today's events from any iCal calendar the user links (Google
            Calendar's "secret address in iCal format", Outlook, a public
            iCloud link — all just URLs, so no OAuth project to set up), from
            every private iCloud calendar over CalDAV (Apple ID + app-specific
            password), plus events noted by voice (memory/agenda.json).

Every source is optional and fails on its own: a missing calendar never costs
the weather, a slow news feed never costs the agenda. The main loop speaks
whatever came back; the plugin daily_briefing exposes the same data as a tool.

Settings live under plugin_config["daily_briefing"]: city, calendar_urls,
news_country. The city also falls back to memory (identity → city), which is
where the assistant saves it when the user says where they live.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import requests

NAMESPACE = "daily_briefing"
AGENDA_PATH = Path(__file__).resolve().parent.parent / "memory" / "agenda.json"
_agenda_lock = threading.Lock()
_UA = {"User-Agent": "Mozilla/5.0 (MARK assistant briefing)"}

# Google News edition per country: (hl, gl, ceid)
_EDITIONS = {
    "BR": ("pt-BR", "BR", "BR:pt-419"),
    "PT": ("pt-PT", "PT", "PT:pt-150"),
    "US": ("en-US", "US", "US:en"),
}


# ── settings ─────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config(NAMESPACE)
    except Exception:
        return {}


def city() -> str:
    c = (_cfg().get("city") or "").strip()
    if c:
        return c
    try:
        from memory.memory_manager import load_memory
        e = load_memory().get("identity", {}).get("city", {})
        return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()
    except Exception:
        return ""


def _country() -> str:
    c = (_cfg().get("news_country") or "BR").strip().upper()
    return c if c in _EDITIONS else "BR"


def _calendar_urls() -> list[str]:
    raw = _cfg().get("calendar_urls") or ""
    return [u for u in re.split(r"[\s,;]+", raw) if u.startswith(("http://", "https://", "webcal://"))]


# ── news ─────────────────────────────────────────────────────────────────────

def _rss(url: str, limit: int) -> list[dict]:
    r = requests.get(url, timeout=6, headers=_UA)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        src = (item.findtext("source") or "").strip()
        # Google News titles end in " - Source"; keep the headline only.
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3].strip()
        if title:
            out.append({"title": title, "source": src})
        if len(out) >= limit:
            break
    return out


def news_national(limit: int = 5) -> list[dict]:
    hl, gl, ceid = _EDITIONS[_country()]
    return _rss(f"https://news.google.com/rss?hl={hl}&gl={gl}&ceid={ceid}", limit)


def news_city(name: str, limit: int = 4) -> list[dict]:
    if not name:
        return []
    hl, gl, ceid = _EDITIONS[_country()]
    from urllib.parse import quote
    base = name.split(",")[0].strip()
    # Google News' own local section first: it is about the place, not the
    # word — a search for "São Paulo" is mostly the football club.
    try:
        items = _rss(f"https://news.google.com/rss/headlines/section/geo/{quote(base)}"
                     f"?hl={hl}&gl={gl}&ceid={ceid}", limit)
    except Exception:
        items = []
    if len(items) >= 2:
        return items
    # Smaller cities have no local section: search the name, minus the sport.
    q = quote_plus(f'"{base}" -futebol -escalação -Brasileirão -campeonato when:1d')
    seen = {i["title"] for i in items}
    for i in _rss(f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}", limit):
        if i["title"] not in seen:
            items.append(i)
    return items[:limit]


def news_topic(query: str, limit: int = 6) -> list[dict]:
    hl, gl, ceid = _EDITIONS[_country()]
    q = quote_plus(f"{query} when:2d")
    return _rss(f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}", limit)


# ── weather ──────────────────────────────────────────────────────────────────

_WMO = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
    81: "heavy rain showers", 82: "violent rain showers", 85: "snow showers",
    86: "heavy snow showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}
_geo_cache: dict[str, tuple] = {}


def _geocode(name: str):
    key = name.lower()
    if key in _geo_cache:
        return _geo_cache[key]
    # "Campinas, SP" → search "Campinas", prefer a result whose admin area matches.
    base, _, hint = name.partition(",")
    r = requests.get("https://geocoding-api.open-meteo.com/v1/search", timeout=6,
                     params={"name": base.strip(), "count": 5, "language": "pt", "format": "json"})
    res = r.json().get("results") or []
    if not res:
        return None
    hint = hint.strip().lower()
    pick = next((x for x in res if hint and hint in (x.get("admin1", "") + " " +
                                                     x.get("country_code", "")).lower()), None)
    pick = pick or next((x for x in res if x.get("country_code") == _country()), res[0])
    _geo_cache[key] = (pick["latitude"], pick["longitude"], pick.get("name", base),
                       pick.get("admin1", ""))
    return _geo_cache[key]


def weather(name: str, day: int = 0) -> dict | None:
    """day 0 = today, 1 = tomorrow."""
    if not name:
        return None
    g = _geocode(name)
    if not g:
        return {"error": f"I could not find a place called '{name}'"}
    lat, lon, place, region = g
    r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=6, params={
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "hourly": "precipitation_probability",
    })
    d = r.json()
    cur, daily, hourly = d.get("current", {}), d.get("daily", {}), d.get("hourly", {})
    out = {
        "place": place + (f" ({region})" if region else ""),
        "min": round(daily["temperature_2m_min"][day]),
        "max": round(daily["temperature_2m_max"][day]),
        "rain_chance": daily["precipitation_probability_max"][day],
        "sky": _WMO.get(daily["weather_code"][day], "mixed conditions"),
    }
    if day == 0:
        out.update(now=round(cur.get("temperature_2m", 0)),
                   feels=round(cur.get("apparent_temperature", 0)),
                   humidity=cur.get("relative_humidity_2m"),
                   sky_now=_WMO.get(cur.get("weather_code"), "mixed conditions"))
    # Hours (from now on, for today) with a ≥50% chance of rain.
    target = (date.today() + timedelta(days=day)).isoformat()
    now_h = datetime.now().hour if day == 0 else -1
    rainy = [int(t[11:13]) for t, p in zip(hourly.get("time", []),
                                           hourly.get("precipitation_probability", []))
             if t.startswith(target) and p is not None and p >= 50 and int(t[11:13]) >= now_h]
    out["rain_hours"] = rainy
    return out


def weather_text(w: dict | None, day: int = 0) -> str:
    if not w:
        return "Weather: no city set."
    if w.get("error"):
        return f"Weather: {w['error']}."
    when = "today" if day == 0 else "tomorrow"
    parts = [f"Weather in {w['place']} {when}:"]
    if day == 0 and "now" in w:
        parts.append(f"now {w['now']}°C (feels like {w['feels']}°C), {w['sky_now']};")
    parts.append(f"min {w['min']}°C, max {w['max']}°C, {w['sky']};")
    parts.append(f"rain chance up to {w['rain_chance']}%")
    if w.get("rain_hours"):
        hrs = w["rain_hours"]
        span = f"{hrs[0]}h" if len(hrs) == 1 else f"between {hrs[0]}h and {hrs[-1] + 1}h"
        parts.append(f"— rain likely {span}")
    return " ".join(parts) + "."


# ── agenda ───────────────────────────────────────────────────────────────────

def _local_events() -> list[dict]:
    with _agenda_lock:
        try:
            data = json.loads(AGENDA_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []


def _save_local_events(events: list[dict]) -> None:
    with _agenda_lock:
        AGENDA_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = AGENDA_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(events, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, AGENDA_PATH)


def add_event(title: str, day: str, at: str = "") -> str:
    title = (title or "").strip()
    if not title:
        return "What is the event?"
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return "I need the date as YYYY-MM-DD."
    if at and not re.fullmatch(r"\d{1,2}:\d{2}", at):
        return "I need the time as HH:MM."
    ev = _local_events()
    ev.append({"date": d.isoformat(), "time": at, "title": title})
    ev = [e for e in ev if e.get("date", "") >= (date.today() - timedelta(days=30)).isoformat()]
    _save_local_events(ev)
    return f"Noted: {title} on {d.strftime('%d/%m')}" + (f" at {at}." if at else ".")


def remove_event(title: str, day: str = "") -> str:
    q = (title or "").strip().lower()
    ev = _local_events()
    keep = [e for e in ev if not (q and q in e.get("title", "").lower()
                                  and (not day or e.get("date") == day))]
    if len(keep) == len(ev):
        return f"I have no event called '{title}'."
    _save_local_events(keep)
    return f"Removed {len(ev) - len(keep)} event(s) matching '{title}'."


def _expand(cal, start: datetime, end: datetime) -> list[dict]:
    """Every occurrence of every event in an icalendar.Calendar inside
    [start, end), repeating events included, in local time."""
    import recurring_ical_events
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        dt = ev.get("DTSTART").dt
        if isinstance(dt, datetime):
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)     # to local time
            at, day = dt.strftime("%H:%M"), dt.date()
        else:
            at, day = "", dt                                   # all-day
        out.append({"date": day.isoformat(), "time": at,
                    "title": str(ev.get("SUMMARY", "")).strip() or "(no title)",
                    "where": str(ev.get("LOCATION", "") or "").strip()})
    return out


def _ics_events(url: str, start: datetime, end: datetime) -> list[dict]:
    import icalendar
    url = "https://" + url[len("webcal://"):] if url.startswith("webcal://") else url
    r = requests.get(url, timeout=8, headers=_UA)
    r.raise_for_status()
    return _expand(icalendar.Calendar.from_ical(r.content), start, end)


def _icloud_creds(cfg: dict | None = None) -> tuple[str, str]:
    cfg = _cfg() if cfg is None else cfg
    return ((cfg.get("icloud_apple_id") or "").strip(),
            (cfg.get("icloud_app_password") or "").replace(" ", "").strip())


# iCloud's discovery (log in, find the principal, list the calendars) took ~4 of
# the ~7 seconds of every read, and it hardly ever changes: kept for an hour.
_ICLOUD_TTL = 3600
_icloud_cache: dict = {"key": None, "t": 0.0, "cals": None}
_icloud_lock = __import__("threading").Lock()


def _icloud_discovered(user: str, pwd: str, fresh: bool = False):
    import caldav
    import hashlib
    import time as _time
    key = (user, hashlib.sha256(pwd.encode()).hexdigest())
    with _icloud_lock:
        c = _icloud_cache
        if not fresh and c["key"] == key and c["cals"] is not None and _time.time() - c["t"] < _ICLOUD_TTL:
            return c["cals"]
        client = caldav.DAVClient(url="https://caldav.icloud.com", username=user, password=pwd, timeout=10)
        cals = client.principal().calendars()
        c.update(key=key, t=_time.time(), cals=cals)
        return cals


def icloud_events(start: datetime, end: datetime, cfg: dict | None = None) -> list[dict]:
    """Events from every iCloud calendar, over CalDAV. Needs the Apple ID and an
    app-specific password (appleid.apple.com -> Sign-In and Security ->
    App-Specific Passwords), never the account password itself: Apple refuses
    that for CalDAV whenever two-factor authentication is on."""
    import icalendar
    user, pwd = _icloud_creds(cfg)
    try:
        cals = _icloud_discovered(user, pwd)
    except Exception:
        cals = _icloud_discovered(user, pwd, fresh=True)
    out = []
    for cal in cals:
        try:
            found = cal.search(start=start, end=end, event=True)
        except Exception as e:
            if _is_auth_error(e) or "Connection" in type(e).__name__:
                # the cached discovery went stale: once more, from scratch
                _icloud_cache["cals"] = None
                raise
            # Reminders lists and shared read-only calendars may refuse a
            # search; one of them must not cost the rest.
            print(f"[Briefing] iCloud calendar skipped: {type(e).__name__}")
            continue
        cal_name = getattr(cal, "name", None) or ""
        for item in found:
            try:
                evs = _expand(icalendar.Calendar.from_ical(item.data), start, end)
            except Exception:
                continue
            for e in evs:                      # where it came from (the HUD tells shared from personal)
                e["source"], e["calendar"] = "icloud", cal_name
            out += evs
    # The same occurrence can come back from two overlapping calendars.
    seen, unique = set(), []
    for e in out:
        k = (e.get("date"), e["time"], e["title"])
        if k not in seen:
            seen.add(k)
            unique.append(e)
    return unique


def _is_auth_error(e: Exception) -> bool:
    t = f"{type(e).__name__} {e}"
    return "401" in t or "Unauthorized" in t or "Authorization" in t


MAX_RANGE_DAYS = 366


def events_between(first: date, last: date) -> dict:
    """Every event from `first` to `last` inclusive, from all sources:
    {'events': [...sorted by date and time], 'errors': [...], 'configured': bool}."""
    if last < first:
        first, last = last, first
    last = min(last, first + timedelta(days=MAX_RANGE_DAYS))
    start = datetime.combine(first, datetime.min.time())
    end = datetime.combine(last + timedelta(days=1), datetime.min.time())
    events = [{"date": e.get("date", ""), "time": e.get("time", ""),
               "title": e.get("title", ""), "where": "", "source": "noted"}
              for e in _local_events() if first.isoformat() <= e.get("date", "") <= last.isoformat()]
    errors = []
    urls = _calendar_urls()
    google = False
    try:
        from core import google_calendar
        google = google_calendar.connected()
    except Exception:
        pass
    if google:
        # its secret iCal address would list the same events a second time
        urls = [u for u in urls if "calendar.google.com" not in u]
    icloud = all(_icloud_creds())

    # every source at the same time: they are all network waits
    def g_read():
        return google_calendar.events(start, end)

    def i_read():
        try:
            return icloud_events(start, end)
        except Exception:
            return icloud_events(start, end)          # a stale cached discovery: once more

    jobs = ([("google", g_read)] if google else []) + \
           [("ics", (lambda u=u: _ics_events(u, start, end))) for u in urls] + \
           ([("icloud", i_read)] if icloud else [])
    if jobs:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = [(kind, ex.submit(fn)) for kind, fn in jobs]
            for kind, f in futs:
                try:
                    events += f.result()
                except Exception as e:
                    if kind == "google":
                        errors.append(f"the Google calendar could not be read ({type(e).__name__})")
                    elif kind == "icloud":
                        why = ("Apple refused the login - check the Apple ID and app-specific password"
                               if _is_auth_error(e) else type(e).__name__)
                        errors.append(f"the iCloud calendar could not be read ({why})")
                    else:
                        errors.append(f"a calendar could not be read ({type(e).__name__})")
    events.sort(key=lambda e: (e.get("date", ""), e["time"] or "00:00"))
    return {"events": events, "errors": errors,
            "configured": bool(urls) or google or icloud or bool(_local_events())}


def agenda(day: int = 0) -> dict:
    """Today + day — what the opening briefing reads."""
    d = date.today() + timedelta(days=day)
    return events_between(d, d)


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def range_text(a: dict, first: date, last: date) -> str:
    """Events grouped by day, for anything longer than one day."""
    head = f"Agenda {first.strftime('%d/%m')}–{last.strftime('%d/%m/%Y')}"
    if not a["events"]:
        txt = f"{head}: nothing scheduled."
    else:
        lines, cur = [head + ":"], None
        for e in a["events"]:
            if e.get("date") != cur:
                cur = e.get("date")
                d = date.fromisoformat(cur)
                lines.append(f"{_WEEKDAYS[d.weekday()]} {d.strftime('%d/%m')}:")
            lines.append(f"  {e['time'] or 'all day'} — {e['title']}"
                         + (f" ({e['where']})" if e.get("where") else ""))
        txt = "\n".join(lines)
    if a["errors"]:
        txt += "\n(" + "; ".join(a["errors"]) + ")"
    return txt


def find_events(query: str, days_ahead: int = 180, days_back: int = 30) -> str:
    q = (query or "").strip().lower()
    if not q:
        return "What should I look for?"
    today = date.today()
    a = events_between(today - timedelta(days=days_back), today + timedelta(days=days_ahead))
    hits = [e for e in a["events"] if q in e["title"].lower() or q in e.get("where", "").lower()]
    if not hits:
        return f"Nothing called '{query}' between {days_back} days ago and {days_ahead} days ahead."
    lines = []
    for e in hits[:15]:
        d = date.fromisoformat(e["date"])
        when = "past" if d < today else ("today" if d == today else f"in {(d - today).days} days")
        lines.append(f"{_WEEKDAYS[d.weekday()]} {d.strftime('%d/%m/%Y')} {e['time'] or 'all day'} — "
                     f"{e['title']} ({when})")
    return "Found:\n" + "\n".join(lines)


# ── iCloud: write access ─────────────────────────────────────────────────────

def _icloud_calendars(cfg: dict | None = None):
    import caldav
    user, pwd = _icloud_creds(cfg)
    client = caldav.DAVClient(url="https://caldav.icloud.com", username=user,
                              password=pwd, timeout=10)
    cals = []
    for c in client.principal().calendars():
        try:
            comps = c.get_supported_components()
        except Exception:
            comps = ["VEVENT"]
        if "VEVENT" in comps:
            cals.append(c)
    return cals


_DEFAULT_CAL_WORDS = {"", "icloud", "apple", "padrão", "padrao", "default", "calendário", "calendario"}


class CalendarNotFound(LookupError):
    pass


def _pick_calendar(cals, name: str = ""):
    """The iCloud calendar called `name`, or the default one when no name (or
    just "iCloud") is given. An unknown name is an error, not the default: a
    request for another calendar must not land silently in the wrong one."""
    if not cals:
        raise RuntimeError("no iCloud calendar accepts events")
    q = (name or "").strip().lower()
    if q in _DEFAULT_CAL_WORDS:
        return cals[0]
    for c in cals:
        if q in (getattr(c, "name", None) or c.get_display_name() or "").lower():
            return c
    names = ", ".join((getattr(c, "name", None) or c.get_display_name() or "?") for c in cals)
    raise CalendarNotFound(f"there is no iCloud calendar called '{name}' (iCloud calendars: {names})")


def icloud_add_event(title: str, day: str, at: str = "", minutes: int = 60,
                     where: str = "", calendar: str = "") -> str:
    """Create an event in iCloud — it shows up on the iPhone, iPad and Mac."""
    import uuid
    import icalendar
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return "I need the date as YYYY-MM-DD."
    ev = icalendar.Event()
    ev.add("uid", f"{uuid.uuid4()}@mark-assistant")
    ev.add("dtstamp", datetime.now().astimezone())
    ev.add("summary", title)
    if where:
        ev.add("location", where)
    if at:
        try:
            start = datetime.strptime(f"{day} {at}", "%Y-%m-%d %H:%M").astimezone()
        except ValueError:
            return "I need the time as HH:MM."
        ev.add("dtstart", start)
        ev.add("dtend", start + timedelta(minutes=max(5, int(minutes or 60))))
    else:
        ev.add("dtstart", d)
        ev.add("dtend", d + timedelta(days=1))
    cal = icalendar.Calendar()
    cal.add("prodid", "-//MARK assistant//EN")
    cal.add("version", "2.0")
    cal.add_component(ev)
    target = _pick_calendar(_icloud_calendars(), calendar)
    target.save_event(cal.to_ical().decode())
    cal_name = getattr(target, "name", None) or "iCloud"
    return (f"Added to your iCloud calendar '{cal_name}': {title} on {d.strftime('%d/%m/%Y')}"
            + (f" at {at}" if at else " (all day)") + (f", {where}" if where else "") + ".")


def icloud_remove_event(title: str, day: str = "") -> str:
    import icalendar
    q = (title or "").strip().lower()
    if not q:
        return "Which event?"
    if day:
        try:
            first = last = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return "I need the date as YYYY-MM-DD."
    else:
        first, last = date.today(), date.today() + timedelta(days=180)
    start = datetime.combine(first, datetime.min.time())
    end = datetime.combine(last + timedelta(days=1), datetime.min.time())
    removed = []
    for cal in _icloud_calendars():
        for item in cal.search(start=start, end=end, event=True):
            try:
                comp = icalendar.Calendar.from_ical(item.data)
                summary = next((str(c.get("SUMMARY", "")) for c in comp.walk("VEVENT")), "")
            except Exception:
                continue
            if q in summary.lower():
                # A repeating series is never deleted from here — only single events.
                if any(c.get("RRULE") for c in comp.walk("VEVENT")):
                    continue
                item.delete()
                removed.append(summary)
    if not removed:
        return f"No single iCloud event matching '{title}'" + (f" on {day}" if day else " in the next 6 months") + \
            " (repeating events are not deleted by voice)."
    return f"Deleted from iCloud: {', '.join(removed)}."


def agenda_text(a: dict, day: int = 0) -> str:
    if day == 0:
        when = "today"
    elif day == 1:
        when = "tomorrow"
    else:
        d = date.today() + timedelta(days=day)
        when = f"on {_WEEKDAYS[d.weekday()]} {d.strftime('%d/%m/%Y')}"
    if not a["events"]:
        txt = f"Agenda {when}: nothing scheduled."
        if not a["configured"]:
            txt += (" (No calendar linked — in ⚙ → plugin settings → Daily briefing the "
                    "user can paste their Google Calendar 'secret address in iCal format' "
                    "or enter their Apple ID and an app-specific password for iCloud, or "
                    "ask you to note events by voice.)")
    else:
        lines = [f"{e['time'] or 'all day'} — {e['title']}" + (f" ({e['where']})" if e["where"] else "")
                 for e in a["events"]]
        txt = f"Agenda {when}:\n" + "\n".join(lines)
    if a["errors"]:
        txt += "\n(" + "; ".join(a["errors"]) + ")"
    return txt


# ── everything at once ───────────────────────────────────────────────────────

def gather(timeout: float = 9.0) -> dict:
    """All four sources in parallel. Missing pieces are None, never raised."""
    c = city()
    jobs = {
        "national": lambda: news_national(5),
        "local": lambda: news_city(c, 4),
        "weather": lambda: weather(c),
        "agenda": lambda: agenda(0),
    }
    ex = ThreadPoolExecutor(max_workers=4)
    futs = {k: ex.submit(f) for k, f in jobs.items()}
    wait(list(futs.values()), timeout=timeout)
    out: dict = {"city": c}
    for k, f in futs.items():
        try:
            out[k] = f.result(timeout=0) if f.done() else None
        except Exception as e:
            print(f"[Briefing] {k} failed: {e}")
            out[k] = None
    ex.shutdown(wait=False, cancel_futures=True)
    return out


def render(b: dict) -> str:
    """The briefing as plain text — for the screen and for the model."""
    parts = [weather_text(b.get("weather"))]
    if b.get("agenda") is not None:
        parts.append(agenda_text(b["agenda"]))
    if b.get("national"):
        parts.append("National news:\n" + "\n".join(f"• {n['title']}" + (f" ({n['source']})" if n["source"] else "")
                                                    for n in b["national"]))
    if b.get("local"):
        parts.append(f"News from {b['city']}:\n" + "\n".join(f"• {n['title']}" + (f" ({n['source']})" if n["source"] else "")
                                                             for n in b["local"]))
    elif b.get("city"):
        parts.append(f"News from {b['city']}: nothing new in the last day.")
    return "\n\n".join(parts)
