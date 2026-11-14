"""
Satellites — where every active satellite is, right now, on a 3D globe in the HUD.

    "abre o globo de satélites"                 → the globe, every active satellite
                                                  (~16 000) moving live, by category
    "onde está a ISS agora?"                    → where it is (and the globe on it)
    "quando a ISS passa aqui?"                  → its next passes over the user's city
    "quem está no espaço agora?"                → the crews of the ISS and Tiangong
    "mostra a ISS ao vivo"                      → the station's live video on the HUD
    "mostra a Terra vista do satélite"          → GOES-19's current picture (Brazil, or
                                                  the whole disk) as a photo on the HUD
    "imagens do Hubble"                         → recent Hubble pictures (NASA library)
    "quantos satélites tem acima de mim?"       → counts, by category

WHERE THE DATA COMES FROM (no keys)
    CelesTrak's GP data for every active satellite (TLE), plus a handful of its
    groups to sort them (stations, GNSS, weather, science, Earth observation, GEO,
    amateur, military). CelesTrak asks for each file to be fetched at most once
    every two hours: the satellites are cached 6 h and the groups 24 h, in
    memory/sats_*.  Positions are SGP4 propagations of those elements — in the
    page (satellite.js, in a worker) and here (python sgp4) for the voice answers.
    Crews: corquaid's people-in-space list (open-notify as fallback). Earth pictures: NOAA STAR's GOES CDN. Hubble: ESA/Hubble's image feeds.

THE LOCAL SERVER
    The same pattern as world_cams: 127.0.0.1 only, a random port, every /api call
    needs the token made at each start.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

NAMESPACE = "satellites"
BASE = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent / "_satellites"
TLE_FILE = BASE / "memory" / "sats_active.tle"
GROUPS_FILE = BASE / "memory" / "sats_groups.json"
TLE_TTL, GROUPS_TTL = 6 * 3600, 24 * 3600
_UA = {"User-Agent": "MARK-LV-satellites/1.0 (personal voice assistant)"}
_GP = "https://celestrak.org/NORAD/elements/gp.php"

# category: (label, colour, CelesTrak groups) — the order decides who wins when a
# satellite is in two groups; name patterns come first (below)
CATS = {
    "stations":   ("Estações espaciais", "#ffffff", ["stations"]),
    "starlink":   ("Starlink", "#4db8ff", []),
    "constel":    ("Outras constelações de internet", "#a78bfa", []),
    "gnss":       ("Navegação (GPS, Galileo…)", "#3dffb0", ["gnss"]),
    "weather":    ("Meteorológicos", "#ffd24a", ["weather"]),
    "science":    ("Científicos", "#ff6bd6", ["science", "geodetic"]),
    "earth":      ("Observação da Terra", "#8dff5a", ["resource", "planet", "spire"]),
    "geo":        ("Comunicação geoestacionária", "#ff8a2a", ["geo"]),
    "amateur":    ("Rádio amador e educação", "#5ff2ff", ["amateur", "education"]),
    "military":   ("Militares", "#ff3355", ["military"]),
    "other":      ("Outros", "#7d8fb3", []),
}
_BY_NAME = [
    ("starlink", re.compile(r"^STARLINK")),
    ("constel", re.compile(r"^(ONEWEB|KUIPER|QIANFAN|HULIANWANG|GUOWANG|IRIDIUM|GLOBALSTAR|ORBCOMM|"
                           r"SATELIOT|LYNK|AST SPACEMOBILE|BLUEBIRD)")),
    ("gnss", re.compile(r"^(GPS|NAVSTAR|COSMOS 2[45]\d\d \(GLONASS|GLONASS|GSAT0|GALILEO|BEIDOU|QZS|"
                        r"IRNSS|NAVIC)")),
    ("weather", re.compile(r"^(GOES|NOAA|METEOSAT|METOP|HIMAWARI|FENGYUN|FY-|METEOR-M|ELEKTRO|"
                           r"SUOMI NPP|JPSS)")),
    ("earth", re.compile(r"^(FLOCK|SKYSAT|LEMUR|SENTINEL|LANDSAT|WORLDVIEW|PLEIADES|ICEYE|CAPELLA|"
                         r"JILIN|SUPERVIEW|CBERS|AMAZONIA)")),
]

# the satellites the globe can do something with (besides info/orbit/passes)
FEATURED = [
    {"key": "iss", "name": "ISS (ZARYA)", "norad": 25544, "label": "Estação Espacial Internacional",
     "do": ["live", "crew", "passes"]},
    {"key": "css", "name": "CSS (TIANHE)", "norad": 48274, "label": "Estação espacial chinesa Tiangong",
     "do": ["crew", "passes"]},
    {"key": "hst", "name": "HST", "norad": 20580, "label": "Telescópio Hubble",
     "do": ["hubble", "passes"]},
    {"key": "goes19", "name": "GOES 19", "norad": 60133, "label": "GOES-19 (GOES-Leste), vê o Brasil",
     "do": ["earth"]},
    {"key": "goes18", "name": "GOES 18", "norad": 51850, "label": "GOES-18 (GOES-Oeste), vê o Pacífico",
     "do": ["earth"]},
]
_ALIASES = {
    "iss": 25544, "estacao espacial": 25544, "estacao espacial internacional": 25544,
    "tiangong": 48274, "estacao chinesa": 48274, "css": 48274, "tianhe": 48274,
    "hubble": 20580, "hst": 20580, "telescopio hubble": 20580,
    "goes 19": 60133, "goes-19": 60133, "goes leste": 60133, "goes 18": 51850, "goes-18": 51850,
}
EARTH = {
    "goes19": {"full": "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/FD/GEOCOLOR/1808x1808.jpg",
               "south": "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/ssa/GEOCOLOR/1800x1080.jpg",
               "north": "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/nsa/GEOCOLOR/1800x1080.jpg"},
    "goes18": {"full": "https://cdn.star.nesdis.noaa.gov/GOES18/ABI/FD/GEOCOLOR/1808x1808.jpg"},
}

_state = {"server": None, "port": 0, "token": secrets.token_urlsafe(16),
          "sats": None, "sats_ts": 0.0, "me": None}
_lock = threading.Lock()
_load_lock = threading.Lock()


def _fold(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(t.lower().split())


# ── data: CelesTrak, cached ──────────────────────────────────────────────────

def _get(url: str, **params) -> str:
    import requests
    r = requests.get(url, params=params, headers=_UA, timeout=60)
    r.raise_for_status()
    return r.text


def _fresh(path: Path, ttl: float) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < ttl


def _tle_text() -> str:
    if not _fresh(TLE_FILE, TLE_TTL):
        try:
            txt = _get(_GP, GROUP="active", FORMAT="tle")
            if txt.count("\n") > 1000:
                TLE_FILE.parent.mkdir(parents=True, exist_ok=True)
                TLE_FILE.write_text(txt, encoding="utf-8")
        except Exception as e:
            print(f"[Satellites] CelesTrak: {e} — using the cached elements")
    return TLE_FILE.read_text(encoding="utf-8") if TLE_FILE.exists() else ""


def _groups() -> dict[str, str]:
    """NORAD id → category, from CelesTrak's groups (one small file each)."""
    if _fresh(GROUPS_FILE, GROUPS_TTL):
        try:
            return json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    out: dict[str, str] = {}
    for cat, (_l, _c, groups) in CATS.items():
        for g in groups:
            try:
                txt = _get(_GP, GROUP=g, FORMAT="tle")
            except Exception as e:
                print(f"[Satellites] group {g}: {e}")
                continue
            for line in txt.splitlines():
                if line.startswith("1 "):
                    out.setdefault(line[2:7].strip(), cat)
            time.sleep(0.3)
    if out:
        GROUPS_FILE.write_text(json.dumps(out), encoding="utf-8")
    elif GROUPS_FILE.exists():
        return json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
    return out


def _category(name: str, norad: str, groups: dict) -> str:
    for cat, pat in _BY_NAME:
        if pat.search(name):
            return cat
    return groups.get(norad, "other")


def sats() -> list[dict]:
    """Every active satellite: name, NORAD id, category, the two TLE lines."""
    with _load_lock:
        with _lock:
            if _state["sats"] and time.time() - _state["sats_ts"] < TLE_TTL:
                return _state["sats"]
        lines = [l.rstrip() for l in _tle_text().splitlines() if l.strip()]
        groups = _groups()
        out = []
        for i in range(0, len(lines) - 2, 3):
            name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
            if not (l1.startswith("1 ") and l2.startswith("2 ")):
                continue
            norad = l1[2:7].strip()
            out.append({"name": name, "norad": int(norad), "cat": _category(name, norad, groups),
                        "l1": l1, "l2": l2})
        with _lock:
            _state["sats"], _state["sats_ts"] = out, time.time()
        print(f"[Satellites] {len(out)} active satellites loaded")
        return out


def find(query: str) -> dict | None:
    q = _fold(query)
    if not q:
        return None
    all_ = sats()
    norad = _ALIASES.get(q) or next((v for k, v in _ALIASES.items() if k in q), None)
    if norad:
        return next((s for s in all_ if s["norad"] == norad), None)
    if q.isdigit():
        return next((s for s in all_ if s["norad"] == int(q)), None)
    exact = [s for s in all_ if _fold(s["name"]) == q]
    if exact:
        return exact[0]
    part = [s for s in all_ if q in _fold(s["name"])]
    return min(part, key=lambda s: len(s["name"])) if part else None


def me() -> dict:
    """The user's place (the briefing's city), for passes and 'above me'."""
    if _state["me"]:
        return _state["me"]
    try:
        from core import briefing
        g = briefing._geocode(briefing.city())
        if g:
            _state["me"] = {"lat": g[0], "lon": g[1], "city": g[2]}
    except Exception:
        pass
    _state["me"] = _state["me"] or {"lat": -22.906, "lon": -43.182, "city": "Rio de Janeiro"}
    return _state["me"]


# ── orbits (python sgp4) ─────────────────────────────────────────────────────

_RE = 6378.137                    # WGS-84
_F = 1 / 298.257223563


def _gmst(jd: float) -> float:
    t = (jd - 2451545.0) / 36525.0
    g = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t - t ** 3 / 38710000
    return math.radians(g % 360)


def _jd(dt: datetime):
    from sgp4.api import jday
    return jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second + dt.microsecond / 1e6)


def _eci(sat: dict, dt: datetime):
    from sgp4.api import Satrec
    rec = sat.get("_rec") or Satrec.twoline2rv(sat["l1"], sat["l2"])
    sat["_rec"] = rec
    jd, fr = _jd(dt)
    err, r, v = rec.sgp4(jd, fr)
    return (None, None, jd + fr) if err else (r, v, jd + fr)


def _geodetic(r, jd) -> tuple[float, float, float]:
    th = _gmst(jd)
    x = r[0] * math.cos(th) + r[1] * math.sin(th)
    y = -r[0] * math.sin(th) + r[1] * math.cos(th)
    z = r[2]
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    e2 = _F * (2 - _F)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(5):
        n = _RE / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * n * math.sin(lat), p)
    n = _RE / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - n
    return math.degrees(lat), (math.degrees(lon) + 540) % 360 - 180, alt


def position(sat: dict, dt: datetime | None = None) -> dict | None:
    dt = dt or datetime.now(timezone.utc)
    r, v, jd = _eci(sat, dt)
    if r is None:
        return None
    lat, lon, alt = _geodetic(r, jd)
    return {"lat": lat, "lon": lon, "alt": alt, "speed": math.sqrt(sum(c * c for c in v))}


def _elevation(sat: dict, dt: datetime, obs: dict) -> float | None:
    r, _v, jd = _eci(sat, dt)
    if r is None:
        return None
    th = _gmst(jd)
    la, lo = math.radians(obs["lat"]), math.radians(obs["lon"])
    # observer in ECI
    n = _RE / math.sqrt(1 - _F * (2 - _F) * math.sin(la) ** 2)
    ox = n * math.cos(la) * math.cos(lo + th)
    oy = n * math.cos(la) * math.sin(lo + th)
    oz = n * (1 - _F * (2 - _F)) * math.sin(la)
    dx, dy, dz = r[0] - ox, r[1] - oy, r[2] - oz
    up = (math.cos(la) * math.cos(lo + th), math.cos(la) * math.sin(lo + th), math.sin(la))
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    return math.degrees(math.asin((dx * up[0] + dy * up[1] + dz * up[2]) / rng))


def passes(sat: dict, hours: float = 48, min_el: float = 10.0, limit: int = 5) -> list[dict]:
    """Next passes over the user's place: rise, highest point, set (UTC)."""
    obs = me()
    t = datetime.now(timezone.utc)
    end = t + timedelta(hours=hours)
    out, cur, step = [], None, timedelta(seconds=30)
    while t < end and len(out) < limit:
        el = _elevation(sat, t, obs)
        if el is None:
            break
        if el > 0:
            if cur is None:
                cur = {"rise": t, "max_el": el, "max_at": t}
            elif el > cur["max_el"]:
                cur["max_el"], cur["max_at"] = el, t
        elif cur is not None:
            cur["set"] = t
            if cur["max_el"] >= min_el:
                out.append(cur)
            cur = None
        t += step
    return out


# ── extras: crews, live video, Earth pictures, Hubble ────────────────────────

def crew() -> dict[str, list[str]]:
    """Who is in space now, by station. A community-kept list first (open-notify
    still listed crews that came home in 2025), open-notify as a fallback."""
    import requests
    out: dict[str, list[str]] = {}
    try:
        d = requests.get("https://corquaid.github.io/international-space-station-APIs/JSON/"
                         "people-in-space.json", headers=_UA, timeout=10).json()
        for p in d.get("people", []):
            ship = str(p.get("spacecraft") or "")
            where = ("ISS" if p.get("iss") else
                     "Tiangong" if re.search(r"(?i)shenzhou|tiangong", ship) else ship or "?")
            who = p.get("name") or "?"
            if p.get("agency"):
                who += f" ({p['agency']})"
            out.setdefault(where, []).append(who)
        if out:
            return out
    except Exception as e:
        print(f"[Satellites] crew list: {e}")
    d = requests.get("http://api.open-notify.org/astros.json", headers=_UA, timeout=10).json()
    for p in d.get("people", []):
        out.setdefault(p.get("craft") or "?", []).append(p.get("name") or "?")
    return out


def iss_live(player) -> tuple[bool, str]:
    from plugins import world_cams as wc
    cams = wc.youtube_live("ISS live earth from space station")
    good = [c for c in cams if re.search(r"(?i)\biss\b|space station|esta[cç][aã]o espacial", c["title"])]
    if not good:
        return False, "Nenhuma transmissão ao vivo da ISS agora."
    c = good[0]
    return wc.play(player, c["id"], c["title"])


_hubble_cache: dict = {"t": 0.0, "items": []}


def hubble(n: int = 12) -> list[dict]:
    """Recent Hubble pictures, from ESA/Hubble's own feeds (the latest releases and
    the picture of the month). NASA's image library was tried first: a search for
    "hubble" brought the Roman telescope's launch photos."""
    import requests
    from email.utils import parsedate_to_datetime
    if time.time() - _hubble_cache["t"] < 3 * 3600 and _hubble_cache["items"]:
        return _hubble_cache["items"][:n]
    out, seen = [], set()
    for feed in ("https://esahubble.org/images/feed/", "https://esahubble.org/images/potm/feed/"):
        try:
            xml = requests.get(feed, headers=_UA, timeout=40).text
        except Exception as e:
            print(f"[Satellites] Hubble feed: {e}")
            continue
        for title, date, url in re.findall(r"<item>.*?<title>(.*?)</title>.*?<pubDate>(.*?)</pubDate>"
                                           r".*?<enclosure[^>]*url=\"(.*?)\"", xml, re.S):
            if re.search(r"(?i)newsletter|banner|logo|poster|calendar|brochure|workshop|conference|"
                         r"meeting|symposium", title + url):
                continue
            if re.search(r"(?i)webb|jwst|nircam|miri|nirspec", title) and not re.search(r"(?i)hubble", title):
                continue                       # the same site publishes Webb's pictures too
            img = url.rsplit("/", 1)[-1]
            if img in seen:
                continue
            seen.add(img)
            try:
                d = parsedate_to_datetime(date).strftime("%Y-%m-%d")
            except Exception:
                d = ""
            title = re.sub(r"&#?\w+;", lambda m: {"&amp;": "&", "&quot;": '"', "&#39;": "'"}.get(m.group(0), ""), title)
            out.append({"title": title, "date": d, "url": url,
                        "thumb": url.replace("/images/screen/", "/images/thumb300y/")})
    out.sort(key=lambda x: x["date"], reverse=True)
    if out:
        _hubble_cache.update(t=time.time(), items=out)
    return out[:n]


def _show_photo(player, url: str, title: str) -> str:
    try:
        from plugins import hud_display
        return hud_display.run({"action": "show", "type": "photo", "image": url, "title": title,
                                "size": "l"}, player=player)
    except Exception as e:
        return f"Could not show it: {e}"


# ── the local server ─────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    player = None

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
                return self._send(200, (HERE / "globe.html").read_bytes(), "text/html; charset=utf-8")
            if not u.path.startswith("/api/"):
                return self._send(404, b"{}")
            if not secrets.compare_digest(q.get("t", ""), _state["token"]):
                return self._json({"ok": False, "error": "forbidden"}, 403)
            name = u.path[5:]
            if name == "sats":
                rows = [[s["name"], s["norad"], s["cat"], s["l1"], s["l2"]] for s in sats()]
                cats = {k: {"label": v[0], "color": v[1]} for k, v in CATS.items()}
                return self._json({"ok": True, "sats": rows, "cats": cats, "featured": FEATURED,
                                   "me": me(), "age_h": round((time.time() - TLE_FILE.stat().st_mtime) / 3600, 1)
                                   if TLE_FILE.exists() else None})
            if name == "crew":
                return self._json({"ok": True, "crew": crew()})
            if name == "live":
                ok, err = iss_live(self.player)
                return self._json({"ok": ok, "error": err})
            if name == "earth":
                urls = EARTH.get(q.get("sat", "goes19"), EARTH["goes19"])
                return self._json({"ok": True, "views": urls})
            if name == "hubble":
                return self._json({"ok": True, "images": hubble()})
            if name == "hud_photo":
                return self._json({"ok": True, "result": _show_photo(self.player, q.get("url", ""),
                                                                    q.get("title", ""))})
            return self._json({"ok": False, "error": "unknown"}, 404)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)[:200]}, 500)


def _serve(player) -> str:
    with _lock:
        if _state["server"] is None:
            _Handler.player = player
            srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            srv.daemon_threads = True
            _state["server"], _state["port"] = srv, srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True, name="satellites-http").start()
        elif player is not None:
            _Handler.player = player
    return f"http://127.0.0.1:{_state['port']}/?t={_state['token']}"


def _open(player, focus: str = "") -> str:
    url = _serve(player) + (f"&focus={quote_plus(focus)}" if focus else "")
    if player is not None and getattr(player, "web_available", lambda: False)():
        player.show_web(url, "Satélites em tempo real")
        return "The satellite globe is open on the HUD" + (f", following {focus}." if focus else ".")
    webbrowser.open(url)
    return "The satellite globe opened in the browser."


# ── voice actions ────────────────────────────────────────────────────────────

def _when(dt: datetime) -> str:
    local = dt.astimezone()
    today = datetime.now().astimezone().date()
    day = ("hoje" if local.date() == today else "amanhã" if local.date() == today + timedelta(days=1)
           else local.strftime("%d/%m"))
    return f"{day} às {local.strftime('%H:%M')}"


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "open").strip().lower()
    name = str(p.get("satellite") or p.get("name") or "").strip()
    try:
        if action in ("open", "map", "globe"):
            return _open(player, name)
        if action == "close":
            if player is not None and hasattr(player, "close_web"):
                player.close_web()
            return "Closed the satellite globe."
        if action == "where":
            s = find(name or "iss")
            if not s:
                return f"No active satellite called '{name}'."
            pos = position(s)
            if not pos:
                return f"{s['name']}: its orbit could not be computed."
            place = ""
            try:
                from plugins import world_cams as wc
                r = wc.reverse(round(pos["lat"], 3), round(pos["lon"], 3))
                place = f", over {r['label']}" if r.get("ok") else ", over the ocean"
            except Exception:
                pass
            if player is not None and getattr(player, "web_available", lambda: False)():
                _open(player, s["name"])
            return (f"{s['name']} is at {pos['lat']:.1f}°, {pos['lon']:.1f}°{place}, "
                    f"{pos['alt']:.0f} km up, at {pos['speed']:.1f} km/s. Shown on the globe.")
        if action == "passes":
            s = find(name or "iss")
            if not s:
                return f"No active satellite called '{name}'."
            ps = passes(s)
            city = me()["city"]
            if not ps:
                return f"{s['name']} does not pass over {city} in the next 48 hours (above 10°)."
            rows = [f"{_when(x['rise'])}: up for {int((x['set'] - x['rise']).total_seconds() // 60)} min, "
                    f"highest {x['max_el']:.0f}° at {x['max_at'].astimezone().strftime('%H:%M')}"
                    for x in ps]
            return (f"Next passes of {s['name']} over {city} (local time):\n" + "\n".join(rows)
                    + "\n(Seen with the naked eye only at dusk or dawn, when it is sunlit and the sky is dark.)")
        if action == "crew":
            c = crew()
            total = sum(len(v) for v in c.values())
            return f"{total} people in space now:\n" + "\n".join(f"{k}: {', '.join(v)}" for k, v in c.items())
        if action == "live":
            ok, err = iss_live(player)
            return "status=opening the ISS live video" if ok else err
        if action == "earth":
            sat = "goes18" if "18" in name or "pacif" in _fold(name) else "goes19"
            view = _fold(p.get("view") or "")
            key = ("full" if any(w in view for w in ("inteir", "disco", "full", "terra", "planeta"))
                   else "north" if "norte" in view else "south")
            url = EARTH[sat].get(key) or EARTH[sat]["full"]
            title = {"full": "A Terra agora", "south": "América do Sul agora",
                     "north": "Norte da América do Sul agora"}[key] + f" · {sat.upper()}"
            _show_photo(player, url, title)
            return f"Showing {title} on the HUD (a picture from minutes ago, updated every 10 minutes)."
        if action == "hubble":
            imgs = hubble(6)
            if not imgs:
                return "No recent Hubble pictures found."
            _show_photo(player, imgs[0]["url"], f"Hubble · {imgs[0]['title'][:40]}")
            return "Showing a recent Hubble picture on the HUD: " + imgs[0]["title"] + \
                   "\nOthers: " + "; ".join(i["title"] for i in imgs[1:5])
        if action == "count":
            all_ = sats()
            counts: dict[str, int] = {}
            for s in all_:
                counts[s["cat"]] = counts.get(s["cat"], 0) + 1
            rows = [f"{CATS[k][0]}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])]
            above = ""
            if p.get("above") or "acima" in _fold(name):
                now, obs = datetime.now(timezone.utc), me()
                n = sum(1 for s in all_ if (_elevation(s, now, obs) or -1) > 0)
                above = f"\nAbove {obs['city']} right now: {n}."
            return f"{len(all_)} active satellites:\n" + "\n".join(rows) + above
        return f"Unknown action '{action}'."
    except Exception as e:
        return f"Satellites: {action} failed — {e}"


def start(player) -> None:
    """Warm the data in the background so the globe opens at once."""
    threading.Thread(target=lambda: (sats(), me()), daemon=True, name="satellites-warm").start()


PLUGIN = {
    "name": "satellites",
    "description": (
        "SATELLITES in real time on a 3D globe in the HUD: every active satellite (~16 000, by "
        "category: stations, Starlink, GPS, weather, science…). Use for 'abre o globo/mapa de "
        "satélites', 'onde está a ISS agora?', 'quando a ISS passa aqui?', 'quem está no espaço?', "
        "'mostra a ISS ao vivo', 'mostra a Terra vista do satélite / imagem de satélite do Brasil "
        "agora', 'imagens do Hubble', 'quantos satélites tem acima de mim?', 'fecha o globo'. "
        "action: open (satellite optional: follow it), close, where (satellite), passes (satellite, "
        "default ISS), crew, live (ISS video), earth (view: brasil|terra inteira; satellite goes19|"
        "goes18), hubble, count (above=true for over the user)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "open | close | where | passes | crew | live | earth | hubble | count"},
            "satellite": {"type": "STRING", "description": "Name or NORAD id: ISS, Tiangong, Hubble, "
                                                           "GOES 19, STARLINK-1234…"},
            "view": {"type": "STRING", "description": "For earth: brasil (default) | terra inteira"},
            "above": {"type": "BOOLEAN", "description": "For count: only those above the user now"},
        },
        "required": ["action"],
    },
}
