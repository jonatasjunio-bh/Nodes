"""
core/google_mail.py — send e-mail from the user's Gmail, and find an address by
a person's name.

It rides on the same Google login as the calendar (core/google_calendar.py):
one consent, one token. Three permissions are asked on top of the calendar's:

    gmail.send                 send mail as the user — and nothing else: this
                               scope cannot read, search or delete a message
    contacts.readonly          the saved contacts   } to turn "manda pro João"
    contacts.other.readonly    people written to    } into an address

In the Google Cloud project the token belongs to, "Gmail API" has to be enabled
(and "People API" for the names; without it only saved and typed addresses work).

Names → addresses, in this order: the assistant's own little book
(config/email_contacts.json, "o e-mail da Ana é …"), then the Google contacts.
Nothing here guesses an address: no match, or more than one, is handed back to
the caller to ask.
"""
from __future__ import annotations

import base64
import difflib
import json
import mimetypes
import re
import threading
import time
import unicodedata
from email.message import EmailMessage
from pathlib import Path

from core import google_calendar as gcal

BASE = Path(__file__).resolve().parent.parent
BOOK_FILE = BASE / "config" / "email_contacts.json"

GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
CONTACTS = "https://www.googleapis.com/auth/contacts.readonly"
OTHER_CONTACTS = "https://www.googleapis.com/auth/contacts.other.readonly"

MAX_ATTACH = 20 * 1024 * 1024          # Gmail takes 25 MB, base64 adds a third
MAX_RECIPIENTS = 12
_ADDRESS = re.compile(r"^[^@\s<>,;:\"']+@[^@\s<>,;:\"']+\.[A-Za-z]{2,}$")

_lock = threading.Lock()
_contacts_cache: tuple[float, list] = (0.0, [])
_CONTACTS_TTL = 3600


# ── state ────────────────────────────────────────────────────────────────────

def can_send() -> bool:
    return gcal.has_scope(GMAIL_SEND)


def why_not() -> str:
    """'' when mail can go out, otherwise what is missing — for the model to
    pass on, and for the module's test button."""
    if not gcal.CLIENT_FILE.exists():
        return ("Google is not set up yet: the OAuth client file is missing "
                "(Central de Módulos → Briefing Diário).")
    if not gcal.connected():
        return ("the Google login has expired or was never made — it has to be connected again: "
                "Central de Módulos → E-mail → TESTAR / CONECTAR GOOGLE.")
    if not can_send():
        return ("the Google login does not include permission to send e-mail yet — connect again: "
                "Central de Módulos → E-mail → TESTAR / CONECTAR GOOGLE, and tick every permission.")
    return ""


def explain(e: Exception) -> str:
    """A Google error as something the user can act on."""
    s = str(e)
    name = type(e).__name__
    if "invalid_grant" in s or name == "RefreshError":
        return "the Google login expired — connect again in Central de Módulos → E-mail"
    if "accessNotConfigured" in s or "has not been used in project" in s or "is disabled" in s:
        api = "People API" if "people" in s.lower() else "Gmail API"
        return f"the {api} is not enabled in the Google Cloud project (console.cloud.google.com → APIs → enable it)"
    if "insufficient" in s.lower() or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in s:
        return "the Google login lacks this permission — connect again and tick every permission"
    if "Invalid To header" in s or "invalidArgument" in s:
        return "Gmail refused the address as invalid"
    return f"{name}: {s[:160]}"


def _build(api: str, version: str):
    from googleapiclient.discovery import build
    creds = gcal._creds()
    if creds is None:
        raise RuntimeError("Google is not connected")
    return build(api, version, credentials=creds, cache_discovery=False)


# ── addresses ────────────────────────────────────────────────────────────────

def clean_address(text: str) -> str:
    """An address as it was typed or dictated ('joao arroba gmail ponto com'),
    or '' when it is not one."""
    t = str(text or "").strip().strip("<>").strip()
    m = re.search(r"<([^<>]+@[^<>]+)>", str(text or ""))
    if m:
        t = m.group(1).strip()
    if "@" not in t and re.search(r"\barroba\b|\bat\b", t, re.I):
        t = re.sub(r"\s+(?:arroba|at)\s+", "@", t, flags=re.I)
        t = re.sub(r"\s+(?:ponto|dot)\s+", ".", t, flags=re.I)
        t = re.sub(r"\s+(?:underline|sublinhado)\s+", "_", t, flags=re.I)
        t = re.sub(r"\s+(?:h[ií]fen|tra[cç]o)\s+", "-", t, flags=re.I)
        t = t.replace(" ", "")
    t = t.lower() if "@" in t else t
    return t if _ADDRESS.match(t) else ""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def book() -> dict:
    """The assistant's own book: {name: address}."""
    try:
        d = json.loads(BOOK_FILE.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:
        return {}


def book_save(name: str, address: str) -> None:
    with _lock:
        d = book()
        for k in [k for k in d if _norm(k) == _norm(name)]:
            del d[k]
        d[name.strip()] = address
        BOOK_FILE.parent.mkdir(exist_ok=True)
        BOOK_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def book_forget(name: str) -> bool:
    with _lock:
        d = book()
        hit = [k for k in d if _norm(k) == _norm(name)]
        for k in hit:
            del d[k]
        if hit:
            BOOK_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        return bool(hit)


def google_contacts(refresh: bool = False) -> list[dict]:
    """Every (name, address) in the Google contacts and in 'other contacts'
    (people written to). Kept in memory for an hour — never on disk. [] when
    the permission or the People API is not there."""
    global _contacts_cache
    with _lock:
        t, items = _contacts_cache
        if not refresh and items and time.time() - t < _CONTACTS_TTL:
            return items
    out, seen = [], set()

    def take(people, source):
        for p in people or []:
            names = [n.get("displayName") for n in p.get("names") or [] if n.get("displayName")]
            nick = [n.get("value") for n in p.get("nicknames") or [] if n.get("value")]
            for e in p.get("emailAddresses") or []:
                addr = clean_address(e.get("value") or "")
                if not addr or addr in seen:
                    continue
                seen.add(addr)
                out.append({"name": names[0] if names else addr.split("@")[0], "email": addr,
                            "also": names[1:] + nick, "source": source})

    try:
        svc = _build("people", "v1")
    except Exception:
        return []
    if gcal.has_scope(CONTACTS):
        try:
            token = None
            while True:
                r = svc.people().connections().list(
                    resourceName="people/me", pageSize=1000, pageToken=token,
                    personFields="names,nicknames,emailAddresses").execute()
                take(r.get("connections"), "contatos")
                token = r.get("nextPageToken")
                if not token:
                    break
        except Exception as e:
            print(f"[Mail] contacts: {explain(e)}")
    if gcal.has_scope(OTHER_CONTACTS):
        try:
            token = None
            while True:
                r = svc.otherContacts().list(pageSize=1000, pageToken=token,
                                             readMask="names,emailAddresses").execute()
                take(r.get("otherContacts"), "já escreveu")
                token = r.get("nextPageToken")
                if not token:
                    break
        except Exception as e:
            print(f"[Mail] other contacts: {explain(e)}")
    with _lock:
        _contacts_cache = (time.time(), out)
    return out


def _score(q: str, name: str, address: str) -> int:
    n = _norm(name)
    if not q or not n:
        return 0
    if n == q:
        return 100
    qt, nt = q.split(), n.split()
    if all(any(t.startswith(x) for t in nt) for x in qt):
        return 90 if nt[0].startswith(qt[0]) else 80
    if difflib.SequenceMatcher(None, q, n).ratio() >= 0.84:
        return 72
    # speech recognition spells names its own way ("Tiago" / "Thiago")
    if all(any(difflib.SequenceMatcher(None, x, t).ratio() >= 0.84 for t in nt) for x in qt):
        return 64
    local = _norm(address.split("@")[0]).replace(" ", "")
    if len(q) >= 4 and q.replace(" ", "") in local:
        return 50
    return 0


def find(name: str) -> list[dict]:
    """Who 'name' could be, best first: [{"name", "email", "source", "score"}]."""
    q = _norm(name)
    found: dict[str, dict] = {}
    for n, addr in book().items():
        s = _score(q, n, addr)
        if s:
            found[addr] = {"name": n, "email": addr, "source": "salvo", "score": s + 5}
    for c in google_contacts():
        s = max([_score(q, c["name"], c["email"])] + [_score(q, a, c["email"]) for a in c.get("also") or []])
        if c["source"] == "contatos":
            s += 2 if s else 0
        if s and (c["email"] not in found or found[c["email"]]["score"] < s):
            if c["email"] in found:                     # saved by the assistant: keep that name
                found[c["email"]]["score"] = max(found[c["email"]]["score"], s)
            else:
                found[c["email"]] = {"name": c["name"], "email": c["email"], "source": c["source"], "score": s}
    return sorted(found.values(), key=lambda c: -c["score"])


def my_address() -> str:
    try:
        return clean_address(gcal.account_email())
    except Exception:
        return ""


# ── sending ──────────────────────────────────────────────────────────────────

def check_attachment(path: str) -> tuple[Path | None, str]:
    p = Path(str(path).strip().strip('"')).expanduser()
    if not p.is_file():
        return None, f"file not found: {p}"
    size = p.stat().st_size
    if size > MAX_ATTACH:
        return None, f"{p.name} is {size / 1048576:.0f} MB — over the 20 MB an e-mail can carry"
    return p, ""


def send(to: list[str], subject: str, body: str, cc: list[str] | None = None,
         bcc: list[str] | None = None, attachments: list[Path] | None = None) -> str:
    """Send it. Raises on failure; returns Gmail's message id."""
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg.set_content(body or "")
    for p in attachments or []:
        kind = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        main, _, sub = kind.partition("/")
        msg.add_attachment(p.read_bytes(), maintype=main, subtype=sub or "octet-stream", filename=p.name)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    r = _build("gmail", "v1").users().messages().send(userId="me", body={"raw": raw}).execute()
    return str(r.get("id") or "")
