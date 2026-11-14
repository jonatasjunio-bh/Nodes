"""
World cams — public live cameras from anywhere in the world, in the HUD.

    "abre o mapa de câmeras"                  → a world map in the HUD; every camera
                                                found sits on its place, grouped
                                                with a count when zoomed out
    "mapa de câmeras do Japão"                → the map, already zoomed there
    "mostra uma câmera ao vivo de Tóquio"     → straight to the first live camera
    "outra câmera" · "quais câmeras tem em Roma?" · "a terceira" · "me leva pra
    algum lugar do mundo" · "fecha o mapa"

WHERE THE CAMERAS COME FROM
    YouTube live streams (no key; "live now" filter, so no old recordings).
    Windy Webcams was tried and dropped: mostly timelapse photos, and almost
    none where the user lives (151 in all Brazil, none in Rio). Cameras that are
    only reachable because nobody set a password (Insecam, Shodan…) are left
    out on purpose.

WHY THE PINS ARE "PLACE" PINS
    A YouTube live has no coordinates. So in the background the plugin searches a
    list of ~110 well-known places, and a camera is pinned at a place only when
    its title names that place (the search for Copacabana also brings Cabo Frio,
    which must not be pinned on Copacabana). Searches the user makes on the map
    add their cameras to the map the same way. The result is cached for 12 hours
    (memory/worldcams_pins.json).

HOW IT PLAYS
    A YouTube live only offers HLS (m3u8), which video_player's resolver drops,
    so this picks the HLS stream itself (≤720p) and hands it to the HUD player.
    Closing and cancelling stay video_player's: the same request counter is used,
    so "fecha o vídeo" works on a camera too, even while it is still loading.

THE LOCAL SERVER
    127.0.0.1 only, a random port, and every /api call needs a token made at each
    start — no web page can make the assistant play something.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import threading
import time
import unicodedata
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

NAMESPACE = "world_cams"
BASE = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent / "_worldcams"
COUNTRIES = BASE / "memory" / "worldcams_countries.geojson"
PINS_FILE = BASE / "memory" / "worldcams_pins.json"
PINS_TTL = 12 * 3600
_UA = {"User-Agent": "MARK-LV-worldcams/1.0 (personal voice assistant)"}
_LIVE_FILTER = "EgJAAQ%253D%253D"            # YouTube's "Live" search filter

# (label, search, lat, lon, words that must appear in a title to pin it here)
PLACES = [
    ("Rio de Janeiro", "Rio de Janeiro", -22.906, -43.173, "rio de janeiro|rio cam|riocam"),
    ("Copacabana", "Copacabana Rio", -22.971, -43.182, "copacabana"),
    ("Ipanema", "Ipanema Rio", -22.987, -43.205, "ipanema|leblon"),
    ("São Paulo", "São Paulo", -23.550, -46.633, "sao paulo|paulista"),
    ("Florianópolis", "Florianópolis", -27.595, -48.548, "florianopolis|floripa"),
    ("Salvador", "Salvador Bahia", -12.971, -38.501, "salvador"),
    ("Fortaleza", "Fortaleza", -3.731, -38.526, "fortaleza"),
    ("Recife", "Recife", -8.047, -34.877, "recife|boa viagem"),
    ("Balneário Camboriú", "Balneário Camboriú", -26.990, -48.635, "camboriu"),
    ("Foz do Iguaçu", "Foz do Iguaçu cataratas", -25.547, -54.588, "iguacu|iguazu"),
    ("Cabo Frio", "Cabo Frio", -22.879, -42.019, "cabo frio|praia do forte"),
    ("Buenos Aires", "Buenos Aires", -34.604, -58.382, "buenos aires"),
    ("Santiago", "Santiago Chile", -33.449, -70.669, "santiago"),
    ("Lima", "Lima Peru", -12.046, -77.043, "lima"),
    ("Bogotá", "Bogotá", 4.711, -74.072, "bogota"),
    ("Cartagena", "Cartagena Colombia", 10.391, -75.479, "cartagena"),
    ("Montevidéu", "Montevideo", -34.901, -56.164, "montevideo"),
    ("Punta del Este", "Punta del Este", -34.962, -54.952, "punta del este"),
    ("Cidade do México", "Mexico City", 19.432, -99.133, "mexico city|ciudad de mexico|cdmx|zocalo"),
    ("Cancún", "Cancun", 21.161, -86.851, "cancun|playa del carmen"),
    ("Havana", "Havana", 23.113, -82.366, "havana|habana"),
    ("Nova York", "New York Times Square", 40.758, -73.986, "new york|times square|nyc|manhattan|brooklyn"),
    ("Miami", "Miami Beach", 25.790, -80.130, "miami"),
    ("Las Vegas", "Las Vegas Strip", 36.115, -115.173, "las vegas"),
    ("Los Angeles", "Los Angeles", 34.052, -118.244, "los angeles|hollywood|santa monica|venice beach"),
    ("San Francisco", "San Francisco Golden Gate", 37.775, -122.419, "san francisco|golden gate"),
    ("Chicago", "Chicago", 41.878, -87.630, "chicago"),
    ("Nova Orleans", "New Orleans Bourbon Street", 29.951, -90.072, "new orleans|bourbon"),
    ("Washington", "Washington DC", 38.907, -77.037, "washington"),
    ("Orlando", "Orlando Florida", 28.538, -81.379, "orlando|disney|kissimmee"),
    ("Key West", "Key West", 24.555, -81.780, "key west"),
    ("Honolulu", "Honolulu Waikiki", 21.281, -157.837, "honolulu|waikiki|hawaii"),
    ("Yellowstone", "Yellowstone Old Faithful", 44.460, -110.828, "yellowstone|old faithful"),
    ("Niágara", "Niagara Falls", 43.083, -79.074, "niagara"),
    ("Toronto", "Toronto", 43.653, -79.383, "toronto"),
    ("Vancouver", "Vancouver", 49.283, -123.121, "vancouver"),
    ("Banff", "Banff", 51.178, -115.571, "banff"),
    ("Anchorage", "Alaska", 61.218, -149.900, "alaska|anchorage"),
    ("Londres", "London", 51.507, -0.128, "london|abbey road|big ben|westminster"),
    ("Edimburgo", "Edinburgh", 55.953, -3.188, "edinburgh"),
    ("Dublin", "Dublin", 53.350, -6.260, "dublin"),
    ("Paris", "Paris Eiffel", 48.857, 2.352, "paris|eiffel"),
    ("Nice", "Nice France", 43.710, 7.262, "nice|cote d'azur|riviera"),
    ("Mônaco", "Monaco", 43.738, 7.425, "monaco|monte carlo"),
    ("Amsterdã", "Amsterdam", 52.370, 4.895, "amsterdam"),
    ("Bruxelas", "Brussels", 50.850, 4.352, "brussels|bruxelles"),
    ("Berlim", "Berlin", 52.520, 13.405, "berlin"),
    ("Munique", "Munich", 48.137, 11.576, "munich|munchen"),
    ("Hamburgo", "Hamburg", 53.551, 9.994, "hamburg"),
    ("Zurique", "Zurich", 47.377, 8.542, "zurich"),
    ("Zermatt", "Zermatt Matterhorn", 46.020, 7.749, "zermatt|matterhorn"),
    ("Viena", "Vienna", 48.208, 16.373, "vienna|wien"),
    ("Praga", "Prague", 50.076, 14.438, "prague|praha"),
    ("Budapeste", "Budapest", 47.498, 19.040, "budapest"),
    ("Varsóvia", "Warsaw", 52.230, 21.012, "warsaw|warszawa"),
    ("Cracóvia", "Krakow", 50.065, 19.945, "krakow|krakow"),
    ("Roma", "Rome", 41.903, 12.496, "rome|roma|trevi|colosseum"),
    ("Veneza", "Venice", 45.441, 12.316, "venice|venezia|rialto"),
    ("Florença", "Florence", 43.770, 11.256, "florence|firenze"),
    ("Milão", "Milan", 45.464, 9.190, "milan|milano"),
    ("Nápoles", "Naples Vesuvius", 40.852, 14.268, "naples|napoli|vesuvio|vesuvius"),
    ("Sicília · Etna", "Etna volcano", 37.751, 14.993, "etna"),
    ("Madri", "Madrid", 40.417, -3.704, "madrid"),
    ("Barcelona", "Barcelona", 41.385, 2.173, "barcelona"),
    ("Tenerife", "Tenerife", 28.292, -16.629, "tenerife"),
    ("Lisboa", "Lisbon", 38.722, -9.139, "lisbon|lisboa"),
    ("Porto", "Porto Portugal", 41.150, -8.611, "porto"),
    ("Atenas", "Athens Acropolis", 37.984, 23.728, "athens|acropolis"),
    ("Santorini", "Santorini", 36.393, 25.461, "santorini|oia"),
    ("Istambul", "Istanbul", 41.008, 28.978, "istanbul|bosphorus|bosporus"),
    ("Copenhague", "Copenhagen", 55.676, 12.568, "copenhagen|kobenhavn"),
    ("Estocolmo", "Stockholm", 59.329, 18.069, "stockholm"),
    ("Oslo", "Oslo", 59.914, 10.752, "oslo"),
    ("Helsinque", "Helsinki", 60.170, 24.938, "helsinki"),
    ("Islândia", "Iceland volcano", 64.147, -21.942, "iceland|reykjavik|islandia"),
    ("Tromsø", "Tromso northern lights", 69.649, 18.956, "tromso|aurora|northern lights"),
    ("Moscou", "Moscow", 55.756, 37.617, "moscow|moskva|москва"),
    ("Kiev", "Kyiv", 50.450, 30.523, "kyiv|kiev|київ"),
    ("Dubai", "Dubai", 25.205, 55.271, "dubai|burj"),
    ("Jerusalém", "Jerusalem Western Wall", 31.777, 35.234, "jerusalem|western wall"),
    ("Meca", "Makkah live", 21.423, 39.826, "makkah|mecca|masjid al haram|kaaba"),
    ("Cairo", "Cairo pyramids", 30.044, 31.236, "cairo|giza|pyramid"),
    ("Cidade do Cabo", "Cape Town", -33.925, 18.424, "cape town"),
    ("Quênia · safári", "Africam safari Kenya waterhole", -1.400, 35.000, "kenya|mpala|masai|safari"),
    ("África do Sul · safári", "Africam Tembe Kruger waterhole", -24.0, 31.5, "africam|tembe|kruger|olifants|naledi"),
    ("Marrakech", "Marrakech", 31.629, -7.981, "marrakech|marrakesh"),
    ("Nova Délhi", "New Delhi", 28.614, 77.209, "delhi"),
    ("Mumbai", "Mumbai", 19.076, 72.878, "mumbai"),
    ("Katmandu", "Kathmandu", 27.717, 85.324, "kathmandu|nepal|everest"),
    ("Bangkok", "Bangkok", 13.756, 100.502, "bangkok"),
    ("Phuket", "Phuket", 7.880, 98.392, "phuket|patong"),
    ("Bali", "Bali", -8.409, 115.189, "bali|kuta|seminyak"),
    ("Singapura", "Singapore", 1.352, 103.820, "singapore|marina bay"),
    ("Kuala Lumpur", "Kuala Lumpur", 3.139, 101.687, "kuala lumpur"),
    ("Manila", "Manila", 14.600, 120.984, "manila"),
    ("Hong Kong", "Hong Kong", 22.319, 114.170, "hong kong|victoria harbour"),
    ("Taipé", "Taipei", 25.033, 121.565, "taipei|taiwan"),
    ("Xangai", "Shanghai", 31.230, 121.474, "shanghai|上海"),
    ("Pequim", "Beijing", 39.904, 116.407, "beijing|北京"),
    ("Seul", "Seoul", 37.567, 126.978, "seoul|서울|gangnam"),
    ("Busan", "Busan", 35.180, 129.076, "busan|부산|haeundae"),
    ("Tóquio · Shibuya", "Shibuya crossing live", 35.659, 139.700, "shibuya|渋谷"),
    ("Tóquio · Shinjuku", "Shinjuku Kabukicho live", 35.694, 139.703, "shinjuku|kabukicho|新宿|歌舞伎町"),
    ("Tóquio", "Tokyo live camera", 35.680, 139.767, "tokyo|東京|asakusa|akihabara|ginza|odaiba"),
    ("Osaka", "Osaka Dotonbori", 34.669, 135.501, "osaka|大阪|dotonbori|道頓堀"),
    ("Kyoto", "Kyoto", 35.012, 135.768, "kyoto|京都"),
    ("Monte Fuji", "Mount Fuji live", 35.361, 138.727, "fuji|富士"),
    ("Sapporo", "Sapporo", 43.062, 141.354, "sapporo|札幌|hokkaido|北海道"),
    ("Okinawa", "Okinawa", 26.212, 127.681, "okinawa|沖縄"),
    ("Sydney", "Sydney Opera House", -33.857, 151.215, "sydney|bondi"),
    ("Melbourne", "Melbourne", -37.814, 144.963, "melbourne"),
    ("Gold Coast", "Gold Coast Surfers Paradise", -28.002, 153.431, "gold coast|surfers paradise"),
    ("Auckland", "Auckland", -36.849, 174.763, "auckland"),
    ("Queenstown", "Queenstown New Zealand", -45.031, 168.663, "queenstown"),
    ("Antártida", "Antarctica live", -77.846, 166.676, "antarctica|antartida|mcmurdo"),
    ("ISS · órbita", "ISS live earth from space", 0.0, -30.0, "iss|space station|earth from space"),
]

_state = {
    "server": None, "port": 0, "token": secrets.token_urlsafe(16),
    "results": [], "index": 0, "place": "",
    "pins": {}, "pins_ts": 0.0, "building": False, "done": 0,
}
_lock = threading.Lock()
_nomi_lock = threading.Lock()
_nomi_last = [0.0]


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return t.lower()


# ── YouTube ──────────────────────────────────────────────────────────────────

_CAMWORDS = re.compile(r"(?i)\bcam\b|webcam|live ?cam|camera|câmera|camara|ライブカメラ|カメラ|"
                       r"24/7|panorama|view|vista")
_NOT_CAM = re.compile(r"(?i)\bnews\b|notícias|noticias|jornal|telejornal|gameplay|radio|"
                      r"\bmusic\b|lofi|podcast|church|missa|culto")


def youtube_live(query: str, limit: int = 18) -> list[dict]:
    """Live-now streams for a place, cameras first."""
    import yt_dlp
    url = (f"https://www.youtube.com/results?search_query={quote_plus(query + ' live cam')}"
           f"&sp={_LIVE_FILTER}")
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                           "playlistend": limit}) as y:
        info = y.extract_info(url, download=False) or {}
    out, seen = [], set()
    for e in info.get("entries") or []:
        vid, title = e.get("id"), (e.get("title") or "").strip()
        if not vid or not title or vid in seen:
            continue
        if e.get("live_status") not in (None, "is_live"):
            continue
        seen.add(vid)
        score = (2 if _CAMWORDS.search(title) else 0) - (2 if _NOT_CAM.search(title) else 0)
        out.append({"id": vid, "title": title, "channel": e.get("channel") or "", "score": score})
    out.sort(key=lambda c: -c["score"])            # stable: YouTube's order within a score
    return out


def resolve_hls(video_id: str) -> tuple[str, str]:
    """(stream url, error). A live offers HLS only; ≤720p starts sooner."""
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception as e:
        msg = str(e)
        if "not live" in msg.lower() or "ended" in msg.lower() or "offline" in msg.lower():
            return "", "Esta transmissão não está mais ao vivo."
        return "", f"Não abriu: {msg[:120]}"
    if not info.get("is_live"):
        return "", "Esta transmissão não está mais ao vivo."
    fmts = [f for f in info.get("formats") or [] if "m3u8" in str(f.get("protocol") or "") and f.get("url")]
    if not fmts:
        return "", "A transmissão não oferece um formato que o HUD toque."
    ok = [f for f in fmts if (f.get("height") or 0) <= 720] or fmts
    best = max(ok, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0))
    return best["url"], ""


def play(player, video_id: str, title: str) -> tuple[bool, str]:
    """Resolve and play in the HUD, honouring video_player's cancel counter."""
    from actions import video_player as vp
    token = vp._begin_open()
    url, err = resolve_hls(video_id)
    if err:
        return False, err
    if not vp._still_wanted(token):
        return False, "Cancelado."
    if player is None:
        webbrowser.open(f"https://www.youtube.com/watch?v={video_id}")
        return True, ""
    player.show_video(url, f"● AO VIVO · {title}", muted=True)
    return True, ""


# ── place pins: cameras on the map ───────────────────────────────────────────

def _jitter(vid: str, lat: float, lon: float) -> tuple[float, float]:
    """Spread a place's cameras a little around it, the same way every time."""
    h = hashlib.md5(vid.encode()).digest()
    a = h[0] / 255 * 6.283
    d = 0.006 + h[1] / 255 * 0.03
    import math
    return round(lat + d * math.sin(a), 5), round(lon + d * math.cos(a) / max(0.3, math.cos(math.radians(lat))), 5)


def _pin(cams: list[dict], label: str, lat: float, lon: float, words: str = "") -> int:
    """Pin the cameras whose title names this place. Returns how many are new."""
    pat = [w for w in (words or _fold(label)).split("|") if w]
    added = 0
    with _lock:
        for c in cams:
            t = _fold(c["title"]) + " " + c["title"].lower()
            if pat and not any(w in t for w in pat):
                continue
            if c["id"] in _state["pins"]:
                _state["pins"][c["id"]]["seen"] = time.time()      # still live
                continue
            la, lo = _jitter(c["id"], lat, lon)
            _state["pins"][c["id"]] = {"id": c["id"], "title": c["title"], "lat": la, "lon": lo,
                                       "place": label, "seen": time.time()}
            added += 1
    return added


def _load_pins() -> None:
    try:
        d = json.loads(PINS_FILE.read_text(encoding="utf-8"))
        with _lock:
            _state["pins"] = {c["id"]: c for c in d.get("cams", [])}
            _state["pins_ts"] = float(d.get("ts", 0))
    except Exception:
        pass


def _save_pins() -> None:
    try:
        with _lock:
            data = {"ts": _state["pins_ts"], "cams": list(_state["pins"].values())}
        PINS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PINS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[WorldCams] pins not saved: {e}")


def build_pins(force: bool = False) -> None:
    """Search every place in the background and pin what is found."""
    with _lock:
        if _state["building"]:
            return
        fresh = time.time() - _state["pins_ts"] < PINS_TTL and _state["pins"]
        if fresh and not force:
            return
        _state["building"], _state["done"] = True, 0

    def one(place):
        label, query, lat, lon, words = place
        try:
            _pin(youtube_live(query, 15), label, lat, lon, words)
        except Exception as e:
            print(f"[WorldCams] {label}: {str(e)[:80]}")
        with _lock:
            _state["done"] += 1

    def run():
        t0 = time.time()
        try:
            with ThreadPoolExecutor(4) as ex:
                list(ex.map(one, PLACES))
        finally:
            with _lock:
                fresh = {k: c for k, c in _state["pins"].items() if c.get("seen", 0) >= t0}
                if fresh:                        # what was not found again is no longer live
                    _state["pins"], _state["pins_ts"] = fresh, time.time()
                _state["building"] = False       # offline: keep what we had
            _save_pins()
            print(f"[WorldCams] 📍 {len(_state['pins'])} cameras on the map "
                  f"({len(PLACES)} places, {time.time() - t0:.0f}s)")

    threading.Thread(target=run, daemon=True, name="worldcams-pins").start()


# ── OpenStreetMap Nominatim (one call a second, as its policy asks) ──────────

def _nominatim(path: str, **q) -> dict | list | None:
    import requests
    with _nomi_lock:
        wait = 1.05 - (time.time() - _nomi_last[0])
        if wait > 0:
            time.sleep(wait)
        _nomi_last[0] = time.time()
        r = requests.get(f"https://nominatim.openstreetmap.org/{path}",
                         params={**q, "format": "jsonv2", "accept-language": "pt-BR",
                                 "namedetails": 1, "addressdetails": 1},
                         headers=_UA, timeout=10)
    r.raise_for_status()
    return r.json()


def _place_of(d: dict) -> dict:
    a = d.get("address") or {}
    nd = d.get("namedetails") or {}
    label = (a.get("city") or a.get("town") or a.get("village") or a.get("suburb")
             or a.get("municipality") or a.get("county") or a.get("state") or d.get("name")
             or (d.get("display_name") or "").split(",")[0])
    out = {"ok": True, "label": label, "name_en": nd.get("name:en") or label,
           "country_code": str(a.get("country_code") or "").upper(),
           "lat": float(d.get("lat")), "lon": float(d.get("lon"))}
    bb = d.get("boundingbox")
    if bb and len(bb) == 4:
        out["bbox"] = [float(x) for x in bb]          # s, n, w, e
    return out


def reverse(lat: float, lon: float) -> dict:
    d = _nominatim("reverse", lat=lat, lon=lon, zoom=10)
    if not d or d.get("error"):
        return {"ok": False, "error": "Nenhum lugar aqui."}
    return _place_of(d)


def geocode(text: str) -> dict:
    d = _nominatim("search", q=text, limit=1)
    if not d:
        return {"ok": False, "error": "Lugar não encontrado."}
    return _place_of(d[0])


# ── countries for the map (Natural Earth 110m, cached once) ──────────────────

_NE_URL = ("https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@v5.1.2/geojson/"
           "ne_110m_admin_0_countries.geojson")


def countries() -> bytes:
    if COUNTRIES.exists():
        return COUNTRIES.read_bytes()
    import requests
    gj = requests.get(_NE_URL, timeout=30, headers=_UA).json()
    keep = ("NAME_PT", "NAME_EN", "CONTINENT", "ISO_A2_EH")
    for f in gj.get("features", []):
        f["properties"] = {k: f["properties"].get(k) for k in keep}
    data = json.dumps(gj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    COUNTRIES.parent.mkdir(parents=True, exist_ok=True)
    COUNTRIES.write_bytes(data)
    return data


# ── the local server ─────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    player = None

    def log_message(self, *_a):                    # quiet
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
                return self._send(200, (HERE / "map.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/countries.geojson":
                return self._send(200, countries(), "application/geo+json")
            if not u.path.startswith("/api/"):
                return self._send(404, b"{}")
            if not secrets.compare_digest(q.get("t", ""), _state["token"]):
                return self._json({"ok": False, "error": "forbidden"}, 403)
            name = u.path[5:]
            if name == "cams":
                cams = youtube_live(q.get("q", ""))
                with _lock:
                    _state["results"], _state["index"], _state["place"] = cams, 0, q.get("label", "")
                if q.get("lat") and q.get("lon"):
                    _pin(cams, q.get("label", ""), float(q["lat"]), float(q["lon"]))
                return self._json({"ok": True, "cams": cams})
            if name == "pins":
                with _lock:
                    return self._json({"ok": True, "cams": list(_state["pins"].values()),
                                       "building": _state["building"], "done": _state["done"],
                                       "total": len(PLACES)})
            if name == "rebuild":
                build_pins(force=True)
                return self._json({"ok": True})
            if name == "reverse":
                return self._json(reverse(float(q["lat"]), float(q["lon"])))
            if name == "geocode":
                return self._json(geocode(q.get("q", "")))
            if name == "play":
                ok, err = play(self.player, q.get("id", ""), q.get("title", ""))
                return self._json({"ok": ok, "error": err})
            return self._json({"ok": False, "error": "unknown"}, 404)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)[:200]}, 500)


def _serve(player) -> str:
    """Start the server once; return the map's base URL (token included)."""
    with _lock:
        if _state["server"] is None:
            _Handler.player = player
            srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            srv.daemon_threads = True
            _state["server"], _state["port"] = srv, srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True, name="worldcams-http").start()
        elif player is not None:
            _Handler.player = player
    return f"http://127.0.0.1:{_state['port']}/?t={_state['token']}"


# ── voice actions ────────────────────────────────────────────────────────────

def _open_map(player, place: str = "") -> str:
    if not _state["pins"]:
        _load_pins()
    build_pins()
    url = _serve(player) + (f"&place={quote_plus(place)}" if place else "")
    if player is not None and getattr(player, "web_available", lambda: False)():
        player.show_web(url, "Câmeras ao vivo do mundo")
        where = f" already zoomed on {place}" if place else ""
        return f"The world cameras map is open on the HUD{where}."
    webbrowser.open(url)
    return "The world cameras map opened in the browser (PyQt6-WebEngine is not installed)."


def _search(place: str) -> list[dict]:
    cams = youtube_live(place)
    with _lock:
        _state["results"], _state["index"], _state["place"] = cams, 0, place
    hit = next((p for p in PLACES if _fold(place) in _fold(p[0]) or _fold(place) in _fold(p[1])), None)
    if hit:
        _pin(cams, hit[0], hit[2], hit[3], hit[4])
    return cams


def _play_index(player, i: int) -> str:
    with _lock:
        cams, place = _state["results"], _state["place"]
    if not cams:
        return "No camera list yet — say a place first."
    i = i % len(cams)
    with _lock:
        _state["index"] = i
    c = cams[i]
    threading.Thread(target=_play_bg, args=(player, c), daemon=True, name="worldcams-play").start()
    return f"status=opening camera {i + 1} of {len(cams)} in {place}: {c['title']}"


def _play_bg(player, c: dict) -> None:
    ok, err = play(player, c["id"], c["title"])
    if not ok and err != "Cancelado." and player is not None and hasattr(player, "request_say"):
        player.request_say(f"[SYSTEM] The live camera '{c['title']}' could not be opened: {err}")


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "map").strip().lower()
    place = str(p.get("place") or "").strip()
    try:
        if action == "map":
            return _open_map(player, place)
        if action == "close_map":
            if player is not None and hasattr(player, "close_web"):
                player.close_web()
            return "Closed the cameras map."
        if action == "watch":
            if not place:
                return "Which place?"
            cams = _search(place)
            if not cams:
                return f"No live camera found for {place} right now."
            return _play_index(player, 0)
        if action == "next":
            with _lock:
                i = _state["index"] + 1
            return _play_index(player, i)
        if action == "pick":
            try:
                n = int(p.get("number") or 1)
            except (TypeError, ValueError):
                n = 1
            return _play_index(player, n - 1)
        if action == "list":
            cams = _search(place) if place else _state["results"]
            if not cams:
                return f"No live camera found{' for ' + place if place else ''} right now."
            rows = [f"{i + 1}. {c['title']}" for i, c in enumerate(cams[:8])]
            return (f"{len(cams)} live cameras{' in ' + place if place else ''}:\n" + "\n".join(rows)
                    + "\n(The user can pick one by number.)")
        if action == "random":
            label, query, *_ = random.choice(PLACES)
            cams = _search(query)
            with _lock:
                _state["place"] = label
            if not cams:
                return f"Tried {label}, but no live camera answered — try again."
            return _play_index(player, 0)
        return f"Unknown action '{action}'."
    except Exception as e:
        return f"World cams: {action} failed — {e}"


def start(player) -> None:
    """At launch: load yesterday's pins; refresh them later, quietly."""
    _load_pins()


PLUGIN = {
    "name": "world_cams",
    "description": (
        "PUBLIC LIVE CAMERAS from anywhere in the world, shown on the HUD. Use it whenever the "
        "user wants to SEE a place live right now ('mostra uma câmera ao vivo de Tóquio', 'quero "
        "ver Copacabana agora', 'abre o mapa de câmeras', 'mapa de câmeras do Japão', 'outra "
        "câmera', 'quais câmeras tem em Roma?', 'a terceira', 'me leva pra algum lugar do mundo', "
        "'fecha o mapa'). Prefer this over video_player for live views of places. action: map "
        "(place optional: open the world map, zoomed there), close_map, watch (place), next, "
        "pick (number), list (place), random. Closing the camera itself is video_player stop."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "map | close_map | watch | next | pick | list | random"},
            "place": {"type": "STRING", "description": "City, beach, landmark, country or continent"},
            "number": {"type": "INTEGER", "description": "For pick: which camera of the last list (1 = first)"},
        },
        "required": ["action"],
    },
}
