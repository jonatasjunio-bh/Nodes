"""
core/google_calendar.py — the user's Google Calendar, read AND write.

Google's "secret address in iCal format" is read-only by design, so writing
needs the Calendar API. One-time setup by the user (their Google account, so
the assistant cannot do it for them):

  1. console.cloud.google.com → new project → enable "Google Calendar API"
  2. OAuth consent screen → External → add their own e-mail as a test user
  3. Credentials → Create → OAuth client ID → "Desktop app" → download the JSON
  4. ⚙ → Central de Módulos → Briefing Diário → paste the JSON's path →
     TESTAR / CONECTAR GOOGLE → approve once in the browser

The client file is copied to config/client_secret_google.json and the token
kept in config/token_google_calendar.json — both names covered by .gitignore.
The token refreshes itself; the browser is needed only once.
"""

from __future__ import annotations

import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CLIENT_FILE = BASE / "config" / "client_secret_google.json"
TOKEN_FILE = BASE / "config" / "token_google_calendar.json"
# One login for everything Google: the calendar, sending mail (send only — this
# scope cannot read the inbox) and the contacts, to find an address by name
# (core/google_mail.py). A token made before a scope was added keeps working
# for what it was given: has_scope() says what it covers.
SCOPES = ["https://www.googleapis.com/auth/calendar",
          "https://www.googleapis.com/auth/gmail.send",
          "https://www.googleapis.com/auth/contacts.readonly",
          "https://www.googleapis.com/auth/contacts.other.readonly"]

# How the user refers to their Google calendar ("my personal/main calendar").
GOOGLE_WORDS = ("google", "gmail", "pessoal", "principal", "primary", "minha agenda")

def _words() -> tuple[str, ...]:
    """GOOGLE_WORDS plus the user's own names for their main calendar
    (plugin_config daily_briefing → google_aliases, comma-separated): Google
    calls the primary calendar by the e-mail address, people call it by name."""
    try:
        from memory.config_manager import get_plugin_config
        raw = (get_plugin_config("daily_briefing") or {}).get("google_aliases") or ""
    except Exception:
        raw = ""
    return GOOGLE_WORDS + tuple(a.strip().lower() for a in raw.split(",") if a.strip())


_lock = threading.Lock()
_service = None
_cal_cache: tuple[float, list] = (0.0, [])


# ── connection ───────────────────────────────────────────────────────────────

def _creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    if not TOKEN_FILE.exists():
        return None
    # the scopes the file itself records (what was granted), not today's list:
    # refreshing an older token while asking for more than it has is refused
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds if creds and creds.valid else None


def has_scope(scope: str) -> bool:
    """Whether the login in use was given this permission."""
    try:
        creds = _creds()
        return bool(creds and scope in (creds.scopes or []))
    except Exception:
        return False


def account_email() -> str:
    """The account's address: the primary calendar's id is it."""
    for c in calendars():
        if c.get("primary"):
            return str(c.get("id") or "")
    return ""


def connected() -> bool:
    try:
        return _creds() is not None
    except Exception:
        return False


def service():
    global _service
    with _lock:
        if _service is None:
            from googleapiclient.discovery import build
            creds = _creds()
            if creds is None:
                raise RuntimeError("Google Calendar is not connected")
            _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return _service


def install_client_file(path: str) -> None:
    """Copy the downloaded OAuth client JSON into config/."""
    src = Path(str(path).strip().strip('"')).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"file not found: {src}")
    import json
    data = json.loads(src.read_text(encoding="utf-8"))
    if "installed" not in data:
        raise ValueError("this is not a 'Desktop app' OAuth client file (no 'installed' section)")
    CLIENT_FILE.parent.mkdir(exist_ok=True)
    if src.resolve() != CLIENT_FILE.resolve():
        shutil.copyfile(src, CLIENT_FILE)


def connect(timeout: int = 240) -> str:
    """Open the browser for the one-time consent and keep the token. Blocks
    until the user approves (run it off the UI thread)."""
    global _service
    global _cal_cache
    import json
    import os
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not CLIENT_FILE.exists():
        raise FileNotFoundError("no OAuth client file yet")
    # the user may untick a permission on Google's screen: take what was given
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=timeout,
                                  authorization_prompt_message="",
                                  success_message="Pronto — o assistente já pode usar sua agenda "
                                                  "e enviar e-mails pela sua conta do Google. "
                                                  "Pode fechar esta aba.")
    data = json.loads(creds.to_json())
    granted = getattr(creds, "granted_scopes", None) or data.get("scopes") or SCOPES
    if isinstance(granted, str):
        granted = granted.split()
    data["scopes"] = [s for s in granted if s.startswith("https://")]   # what was given, not what was asked
    TOKEN_FILE.write_text(json.dumps(data), encoding="utf-8")
    with _lock:
        _service = None
    _cal_cache = (0.0, [])
    return primary_name()


def disconnect() -> None:
    global _service
    TOKEN_FILE.unlink(missing_ok=True)
    with _lock:
        _service = None


# ── calendars ────────────────────────────────────────────────────────────────

def calendars(refresh: bool = False) -> list[dict]:
    """The user's calendar list (cached 10 min)."""
    import time
    global _cal_cache
    if not refresh and time.monotonic() - _cal_cache[0] < 600 and _cal_cache[1]:
        return _cal_cache[1]
    items, token = [], None
    while True:
        r = service().calendarList().list(pageToken=token).execute()
        items += r.get("items", [])
        token = r.get("nextPageToken")
        if not token:
            break
    _cal_cache = (time.monotonic(), items)
    return items


def primary_name() -> str:
    for c in calendars(refresh=True):
        if c.get("primary"):
            return c.get("summaryOverride") or c.get("summary") or c.get("id", "Google")
    return "Google"


def _name(c: dict) -> str:
    return c.get("summaryOverride") or c.get("summary") or c.get("id", "")


def find_calendar(name: str) -> dict | None:
    """A writable calendar by name; the primary one for generic words."""
    q = (name or "").strip().lower()
    cals = [c for c in calendars() if c.get("accessRole") in ("owner", "writer")]
    if not q or any(w in q for w in _words()):
        return next((c for c in cals if c.get("primary")), cals[0] if cals else None)
    for c in cals:
        if q in _name(c).lower():
            return c
    return None


def has_calendar(name: str) -> bool:
    """Does `name` refer to a Google calendar (a generic word or a real name)?"""
    n = (name or "").strip().lower()
    if not n:
        return False
    if any(w in n for w in _words()):
        return True
    try:
        return find_calendar(name) is not None
    except Exception:
        return False


# ── events ───────────────────────────────────────────────────────────────────

def _local(ev_time: dict) -> tuple[str, str]:
    """(YYYY-MM-DD, HH:MM or '') in local time."""
    if "dateTime" in ev_time:
        dt = datetime.fromisoformat(ev_time["dateTime"].replace("Z", "+00:00")).astimezone()
        return dt.date().isoformat(), dt.strftime("%H:%M")
    return ev_time.get("date", ""), ""


def events(start: datetime, end: datetime) -> list[dict]:
    """Occurrences from every calendar shown in the user's Google Calendar."""
    tmin = start.astimezone().isoformat()
    tmax = end.astimezone().isoformat()
    out = []
    for c in calendars():
        if not c.get("selected", c.get("primary", False)):
            continue
        token = None
        while True:
            r = service().events().list(calendarId=c["id"], timeMin=tmin, timeMax=tmax,
                                        singleEvents=True, orderBy="startTime",
                                        pageToken=token, maxResults=250).execute()
            for ev in r.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                day, at = _local(ev.get("start", {}))
                out.append({"date": day, "time": at,
                            "title": (ev.get("summary") or "(no title)").strip(),
                            "where": (ev.get("location") or "").strip(), "source": "google"})
            token = r.get("nextPageToken")
            if not token:
                break
    return out


def add_event(title: str, day: str, at: str = "", minutes: int = 60,
              where: str = "", calendar: str = "") -> str:
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return "I need the date as YYYY-MM-DD."
    cal = find_calendar(calendar)
    if cal is None:
        names = ", ".join(_name(c) for c in calendars() if c.get("accessRole") in ("owner", "writer"))
        return (f"NOT ADDED — nothing was saved: there is no Google calendar called '{calendar}' "
                f"(Google calendars: {names}).")
    body = {"summary": title}
    if where:
        body["location"] = where
    if at:
        try:
            start = datetime.strptime(f"{day} {at}", "%Y-%m-%d %H:%M").astimezone()
        except ValueError:
            return "I need the time as HH:MM."
        body["start"] = {"dateTime": start.isoformat()}
        body["end"] = {"dateTime": (start + timedelta(minutes=max(5, int(minutes or 60)))).isoformat()}
    else:
        body["start"] = {"date": d.isoformat()}
        body["end"] = {"date": (d + timedelta(days=1)).isoformat()}
    service().events().insert(calendarId=cal["id"], body=body).execute()
    return (f"Added to your Google calendar '{_name(cal)}': {title} on {d.strftime('%d/%m/%Y')}"
            + (f" at {at}" if at else " (all day)") + (f", {where}" if where else "") + ".")


def matches(title: str, day: str = "") -> list[tuple[str, dict]]:
    """Single (non-repeating) events whose title contains `title`: [(calendarId, event)]."""
    q = (title or "").strip().lower()
    if not q:
        return []
    if day:
        first = last = datetime.strptime(day, "%Y-%m-%d").date()
    else:
        first, last = date.today(), date.today() + timedelta(days=180)
    tmin = datetime.combine(first, datetime.min.time()).astimezone().isoformat()
    tmax = datetime.combine(last + timedelta(days=1), datetime.min.time()).astimezone().isoformat()
    found = []
    for c in calendars():
        if c.get("accessRole") not in ("owner", "writer"):
            continue
        r = service().events().list(calendarId=c["id"], timeMin=tmin, timeMax=tmax, q=title,
                                    singleEvents=True, maxResults=50).execute()
        for ev in r.get("items", []):
            if q in (ev.get("summary") or "").lower() and not ev.get("recurringEventId"):
                found.append((c["id"], ev))
    return found


def remove_events(found: list[tuple[str, dict]]) -> str:
    done = []
    for cal_id, ev in found:
        service().events().delete(calendarId=cal_id, eventId=ev["id"]).execute()
        done.append(ev.get("summary") or "(no title)")
    return f"Deleted from Google: {', '.join(done)}." if done else "Nothing deleted from Google."
