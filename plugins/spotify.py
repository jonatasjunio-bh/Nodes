"""
Spotify — full control by voice, through the official Spotify Web API.

    "play Coldplay" / "play the album Abbey Road" / "play my playlist Treino"
    "play my liked songs" / "put on something to relax"
    "pause" · "next" · "back" · "skip 30 seconds" · "volume 40" · "louder"
    "shuffle on" · "repeat this song" · "add Bohemian Rhapsody to the queue"
    "what's playing?" · "like this song" · "add this to my playlist Road trip"
    "create a playlist called Sunday" · "what did I listen to recently?"
    "play it on the TV" / "move the music to the Echo"   (Spotify Connect)

Setup, once: create a free app at developer.spotify.com/dashboard with the
redirect URI http://127.0.0.1:8888/callback, paste its Client ID in
⚙ → plugin settings → Spotify, and press CONNECT to approve in the browser.
Authorisation uses PKCE, so there is no client secret to store; the token is
cached in memory/spotify_token.json and refreshed automatically.

Spotify only accepts playback commands from Premium accounts. On a free
account, pause/resume/next/previous fall back to the system media keys and
"play X" opens the search in the Spotify app.
"""

from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

NAMESPACE = "spotify"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
TOKEN_PATH = Path(__file__).resolve().parent.parent / "memory" / "spotify_token.json"
SCOPES = " ".join([
    "user-read-playback-state", "user-modify-playback-state", "user-read-currently-playing",
    "user-library-read", "user-library-modify", "playlist-read-private",
    "playlist-read-collaborative", "playlist-modify-private", "playlist-modify-public",
    "user-read-recently-played",
])
_OS = platform.system()
_connecting = threading.Lock()


# ── auth ─────────────────────────────────────────────────────────────────────

def _client_id(cfg: dict | None = None) -> str:
    if cfg is None:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config(NAMESPACE)
    return (cfg.get("client_id") or "").strip()


def _redirect(cfg: dict | None = None) -> str:
    """The Redirect URI registered in the Spotify app — it must match exactly.
    Spotify accepts plain http only for a loopback IP (127.0.0.1), and
    spotipy's login catcher only speaks http and needs an explicit port, so
    https://127.0.0.1/callback (what a first-time setup often enters) cannot
    work: register this one instead."""
    if cfg is None:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config(NAMESPACE)
    return (cfg.get("redirect_uri") or "").strip() or REDIRECT_URI


def _redirect_problem(uri: str) -> str:
    from urllib.parse import urlparse
    u = urlparse(uri)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "[::1]", "::1") or not u.port:
        return (f"the Redirect URI must be http, 127.0.0.1 and an explicit port, e.g. "
                f"{REDIRECT_URI} — add exactly that in the Spotify app (Edit settings → "
                f"Redirect URIs) and leave this field empty")
    return ""


def _auth(client_id: str, open_browser: bool, redirect: str = REDIRECT_URI):
    from spotipy.cache_handler import CacheFileHandler
    from spotipy.oauth2 import SpotifyPKCE
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    return SpotifyPKCE(client_id=client_id, redirect_uri=redirect, scope=SCOPES,
                       cache_handler=CacheFileHandler(cache_path=str(TOKEN_PATH)),
                       open_browser=open_browser, requests_timeout=8)


def _sp():
    """A ready client, or raise with a sentence the user can act on. Never
    starts an interactive login: without a cached token PKCE would block on
    input() — fatal inside a GUI — so that happens only through connect()."""
    try:
        import spotipy
    except ImportError:
        raise RuntimeError("the 'spotipy' package is missing (pip install spotipy)")
    cid = _client_id()
    if not cid:
        raise RuntimeError("Spotify is not set up — paste the Client ID in ⚙ → plugin "
                           "settings → Spotify and press CONNECT")
    auth = _auth(cid, open_browser=False, redirect=_redirect())
    if not auth.cache_handler.get_cached_token():
        raise RuntimeError("Spotify is not connected yet — press CONNECT in ⚙ → plugin "
                           "settings → Spotify, or say 'connect Spotify'")
    return spotipy.Spotify(auth_manager=auth, requests_timeout=8, retries=1)


def connect(cfg: dict | None = None, timeout: float = 180):
    """Open the browser for the one-time approval and wait for it (≤3 min).
    Returns (ok, message). Runs on a worker thread, never the Qt thread.

    Our own callback catcher, not spotipy's: spotipy's waits with no time
    limit, so an approval that never comes (a wrong Redirect URI stops on
    Spotify's page) kept the port forever — and, because it allows sharing the
    port, the next attempt's redirect could land on the stale one."""
    import secrets as _secrets
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    cid = _client_id(cfg)
    if not cid:
        return False, "Paste the Client ID from developer.spotify.com first."
    redirect = _redirect(cfg)
    bad = _redirect_problem(redirect)
    if bad:
        return False, bad
    if not _connecting.acquire(blocking=False):
        return False, "A Spotify approval is already waiting in the browser."
    try:
        import spotipy
        ru = urlparse(redirect)
        auth = _auth(cid, open_browser=False, redirect=redirect)
        state = _secrets.token_urlsafe(16)
        got: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                u = urlparse(self.path)
                if u.path != (ru.path or "/"):
                    self.send_response(404); self.end_headers(); return
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if q.get("state") == state:
                    got.update(q)
                ok = "code" in got
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(("<h2>Spotify conectado. Pode fechar esta janela.</h2>" if ok else
                                  "<h2>O Spotify não autorizou (" + q.get("error", "?") +
                                  ").</h2>").encode())

            def log_message(self, *a):
                pass

        class _Exclusive(HTTPServer):
            allow_reuse_address = False

            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        try:
            srv = _Exclusive(("127.0.0.1", ru.port), Handler)
        except OSError:
            return False, (f"port {ru.port} is busy — an earlier Spotify login may still be "
                           f"waiting; restart the assistant and press CONNECT again")
        srv.timeout = 1
        webbrowser.open(auth.get_authorize_url(state))
        end = time.time() + timeout
        try:
            while not got and time.time() < end:
                srv.handle_request()
        finally:
            srv.server_close()

        if not got:
            # A wrong Redirect URI never reaches us: Spotify shows the error in
            # the browser ("INVALID_CLIENT: Invalid redirect URI") and stops.
            return False, (f"No approval within 3 minutes. If the browser showed 'Invalid "
                           f"redirect URI', add exactly {redirect} under Redirect URIs in "
                           f"the Spotify app (Edit settings → Save), then press CONNECT again.")
        if "error" in got:
            return False, f"Spotify did not authorise: {got['error']}"
        try:
            auth.get_access_token(code=got["code"], check_cache=False)
            user = spotipy.Spotify(auth_manager=auth, requests_timeout=8).current_user()
        except Exception as e:
            msg = str(e)
            if "INVALID_CLIENT" in msg or "invalid_client" in msg:
                msg = "Spotify did not recognise the Client ID"
            elif "redirect" in msg.lower():
                msg = f"add {redirect} as a Redirect URI in the Spotify app settings"
            elif "403" in msg:
                msg = ("this Spotify account is not allowed on the app — in the app's "
                       "User Management add your Spotify account's e-mail")
            return False, f"Spotify connection failed: {msg}"
        plan = user.get("product", "")
        note = "" if plan == "premium" else (
            " (free account: playback control is limited — Spotify only allows it on Premium)")
        return True, f"Connected as {user.get('display_name') or user.get('id')}{note}."
    finally:
        _connecting.release()


# ── helpers ──────────────────────────────────────────────────────────────────

def _premium_error(e) -> bool:
    if "Cannot control device volume" in str(e):
        return False                  # a phone refusing remote volume, not a Free account
    return getattr(e, "http_status", None) == 403 or "PREMIUM_REQUIRED" in str(e)


def _phone_volume(how: str) -> str:
    """The phone's own media volume, through the MARK app. Spotify on Android
    refuses volume over Spotify Connect ("Cannot control device volume")."""
    r = _phone_call("volume", {"stream": "media", "level": how}, 10)
    return str(r.get("result")) if r.get("ok") else f"Phone: {r.get('result')}"


def _media_key(key: str) -> bool:
    """Free-account fallback: the OS media keys reach the Spotify app."""
    try:
        import pyautogui
        pyautogui.press(key)
        return True
    except Exception:
        return False


def _open_app(uri: str = "spotify:") -> None:
    try:
        if _OS == "Windows":
            os.startfile(uri)
        elif _OS == "Darwin":
            subprocess.Popen(["open", uri])
        else:
            subprocess.Popen(["xdg-open", uri])
    except Exception as e:
        print(f"[Spotify] could not open the app: {e}")


# ── the phone ────────────────────────────────────────────────────────────────
# Asked from the MARK app (or "no celular"), the music belongs on the phone.
# Its Spotify only shows up in Spotify Connect while the app is running, so
# it is opened through the MARK app first and given a few seconds to register;
# if it never does, the phone's own Spotify is told to play instead.

_PHONE_WORDS = ("celular", "telefone", "phone", "smartphone", "android", "mobile")
_PC_WORDS = ("pc", "computador", "computer", "notebook", "desktop")


class _PhoneOffline(RuntimeError):
    """The phone should play, but its Spotify isn't reachable through Connect."""


def _from_phone() -> bool:
    """The user is talking through the MARK app right now (last 2 minutes)."""
    try:
        from dashboard.server import DashboardServer
        d = getattr(DashboardServer, "instance", None)
        return bool(d and d.device_connected()
                    and time.monotonic() - d.last_phone_activity < d.PHONE_REPLY_WINDOW)
    except Exception:
        return False


def _wants_phone(want: str) -> bool:
    w = want.lower().strip()
    if w:
        return any(x in w for x in _PHONE_WORDS)
    return _from_phone()


def _phone_call(action: str, args: dict, timeout: float = 15) -> dict:
    from plugins import android_phone
    return android_phone._call(action, args, timeout)


def _phone_device(sp) -> tuple[str, str]:
    def find():
        return next((d for d in sp.devices().get("devices", []) if d["type"] == "Smartphone"), None)
    d = find()
    if d:
        return d["id"], d["name"]
    r = _phone_call("open_app", {"name": "Spotify"})
    if not r.get("ok"):
        raise _PhoneOffline(str(r.get("result") or "the phone app is not connected"))
    for _ in range(24):                               # up to ~12 s to register
        time.sleep(0.5)
        d = find()
        if d:
            return d["id"], d["name"]
    raise _PhoneOffline("the phone's Spotify did not show up in Spotify Connect")


def _start(sp, dev_id, context_uri=None, uris=None) -> None:
    """start_playback, or — with no Connect device for the phone — the phone's
    own Spotify opening the same thing and pressing play."""
    if dev_id is None:
        uri = context_uri or (uris[0] if uris else "")
        r = _phone_call("play_music", {"uri": uri}, 25)
        if not r.get("ok"):
            raise RuntimeError(str(r.get("result") or "the phone could not play it"))
        return
    sp.start_playback(device_id=dev_id, context_uri=context_uri, uris=uris)


def _resume(sp, device: str) -> str:
    try:
        dev_id, name = _device(sp, device)
    except _PhoneOffline:
        r = _phone_call("play_music", {}, 25)
        return "Resumed on the phone." if r.get("ok") else f"Phone: {r.get('result')}"
    sp.start_playback(device_id=dev_id)
    return f"Resumed on {name}."


def _device(sp, want: str = "") -> tuple[str, str]:
    """(device_id, name) to play on. The phone when asked from it or named;
    else the named one, else the active one, else this computer, else whatever
    exists — and if nothing is online, start the desktop app and wait for it
    to register."""
    if _wants_phone(want):
        return _phone_device(sp)
    if want.lower().strip() in _PC_WORDS:
        want = platform.node()[:8]
    for attempt in range(2):
        devs = sp.devices().get("devices", [])
        if want:
            w = want.lower()
            hit = next((d for d in devs if w in d["name"].lower() or w in d["type"].lower()), None)
            if hit:
                return hit["id"], hit["name"]
        act = next((d for d in devs if d.get("is_active")), None)
        if act and not want:
            return act["id"], act["name"]
        host = platform.node().lower()
        pc = next((d for d in devs if d["type"] == "Computer" and host[:8] in d["name"].lower()), None)
        pick = pc or next((d for d in devs if d["type"] == "Computer"), None) or (devs[0] if devs else None)
        if pick and not want:
            return pick["id"], pick["name"]
        if want and devs:
            raise RuntimeError(f"no Spotify device called '{want}'. Available: "
                               + ", ".join(d["name"] for d in devs))
        if attempt == 0:
            _open_app()
            for _ in range(20):                       # up to ~10 s for it to appear
                time.sleep(0.5)
                if sp.devices().get("devices"):
                    break
    raise RuntimeError("no Spotify device is online — open Spotify on the PC, phone or TV")


def _track_line(t: dict) -> str:
    return f"{t['name']} — {', '.join(a['name'] for a in t.get('artists', []))}"


def _find_playlist(sp, name: str):
    q = name.lower().strip()
    res = sp.current_user_playlists(limit=50)
    items = res.get("items", [])
    while res.get("next") and len(items) < 300:
        res = sp.next(res)
        items += res.get("items", [])
    items = [p for p in items if p]
    return (next((p for p in items if p["name"].lower() == q), None)
            or next((p for p in items if q in p["name"].lower()), None))


_KIND_WORDS = {"album": "album", "álbum": "album", "playlist": "playlist",
               "artist": "artist", "artista": "artist", "música": "track",
               "musica": "track", "song": "track", "track": "track"}


def _play(sp, query: str, kind: str, device: str) -> str:
    q = query.strip()
    k = (kind or "").lower()
    m = re.match(r"^(album|álbum|playlist|artist|artista|música|musica|song|track)\s+(.+)", q, re.I)
    if m and not k:
        k, q = _KIND_WORDS[m.group(1).lower()], m.group(2)
    try:
        dev_id, dev_name = _device(sp, device)
    except _PhoneOffline as e:
        print(f"[Spotify] phone via Connect unavailable ({e}) — asking the phone's app")
        dev_id, dev_name = None, "the phone"
    where = f" on {dev_name}"

    if re.fullmatch(r"(my )?(liked|saved) songs|(minhas )?(músicas )?curtidas|favoritas", q, re.I):
        items = sp.current_user_saved_tracks(limit=50).get("items", [])
        uris = [i["track"]["uri"] for i in items if i.get("track")]
        if not uris:
            return "You have no liked songs yet."
        if dev_id:
            sp.shuffle(True, device_id=dev_id)
        _start(sp, dev_id, uris=uris)
        return f"Playing your liked songs, shuffled{where}."

    if k in ("", "playlist"):
        mine = _find_playlist(sp, re.sub(r"^(my|minha)\s+(playlist\s+)?", "", q, flags=re.I))
        if mine:
            _start(sp, dev_id, context_uri=mine["uri"])
            return f"Playing your playlist {mine['name']}{where}."

    types = {"track": "track", "album": "album", "artist": "artist", "playlist": "playlist"}
    search_type = types.get(k, "track,artist,album,playlist")
    res = sp.search(q=q, type=search_type, limit=5)
    tracks = [t for t in res.get("tracks", {}).get("items", []) if t]
    artists = [a for a in res.get("artists", {}).get("items", []) if a]
    albums = [a for a in res.get("albums", {}).get("items", []) if a]
    lists = [p for p in res.get("playlists", {}).get("items", []) if p]

    def exact(items):
        return next((x for x in items if x["name"].lower() == q.lower()), None)

    if k == "artist" or (not k and exact(artists)):
        a = exact(artists) or (artists[0] if artists else None)
        if a:
            _start(sp, dev_id, context_uri=a["uri"])
            return f"Playing {a['name']}{where}."
    if k == "album" or (not k and exact(albums)):
        a = exact(albums) or (albums[0] if albums else None)
        if a:
            _start(sp, dev_id, context_uri=a["uri"])
            return f"Playing the album {a['name']} by {a['artists'][0]['name']}{where}."
    if k == "playlist" and lists:
        _start(sp, dev_id, context_uri=lists[0]["uri"])
        return f"Playing the playlist {lists[0]['name']}{where}."
    if tracks and k in ("", "track"):
        _start(sp, dev_id, uris=[tracks[0]["uri"]])
        return f"Playing {_track_line(tracks[0])}{where}."
    # A mood or genre ("something to relax", "rock anos 80") reads best as a playlist.
    if lists:
        _start(sp, dev_id, context_uri=lists[0]["uri"])
        return f"Playing the playlist {lists[0]['name']}{where}."
    if artists:
        _start(sp, dev_id, context_uri=artists[0]["uri"])
        return f"Playing {artists[0]['name']}{where}."
    return f"I found nothing on Spotify for '{query}'."


def _now(sp) -> str:
    cur = sp.current_playback()
    if not cur or not cur.get("item"):
        return "Nothing is playing on Spotify right now."
    t = cur["item"]
    pos, dur = cur.get("progress_ms", 0) // 1000, t.get("duration_ms", 0) // 1000
    state = "Playing" if cur.get("is_playing") else "Paused"
    dev = cur.get("device", {})
    extra = []
    if cur.get("shuffle_state"):
        extra.append("shuffle on")
    if cur.get("repeat_state") not in (None, "off"):
        extra.append(f"repeat {cur['repeat_state']}")
    return (f"{state}: {_track_line(t)} (album {t.get('album', {}).get('name', '?')}), "
            f"{pos // 60}:{pos % 60:02d} of {dur // 60}:{dur % 60:02d}, on {dev.get('name', '?')} "
            f"at volume {dev.get('volume_percent', '?')}" + (f", {', '.join(extra)}" if extra else "") + ".")


def _current_track(sp):
    cur = sp.current_playback()
    if not cur or not cur.get("item") or cur["item"].get("type") != "track":
        raise RuntimeError("nothing is playing right now")
    return cur["item"]


# ── tool ─────────────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action", "")).strip().lower().replace(" ", "_")
    value = str(p.get("value", "") or "").strip()
    device = str(p.get("device", "") or "").strip()
    kind = str(p.get("kind", "") or "").strip()

    if action == "connect":
        threading.Thread(target=lambda: print("[Spotify]", connect()), daemon=True).start()
        return ("I opened the Spotify approval page in the browser — the user must log in "
                "and press Agree; it stays valid from then on.")

    try:
        sp = _sp()
    except RuntimeError as e:
        return str(e) + "."

    try:
        if action == "play":
            if not value:
                return _resume(sp, device)
            return _play(sp, value, kind, device)
        if action in ("pause", "stop"):
            sp.pause_playback()
            return "Paused."
        if action == "resume":
            return _resume(sp, device)
        if action == "next":
            sp.next_track()
            time.sleep(0.6)
            return "Skipped. " + _now(sp)
        if action == "previous":
            sp.previous_track()
            return "Going back."
        if action == "seek":
            cur = sp.current_playback() or {}
            pos = cur.get("progress_ms", 0)
            m = re.fullmatch(r"([+-]?)(\d+)(?::(\d{2}))?", value.replace(" ", ""))
            if not m:
                return "Say how far, e.g. '+30' (seconds), '-15' or '1:20'."
            secs = int(m.group(2)) * 60 + int(m.group(3)) if m.group(3) else int(m.group(2))
            target = pos + secs * 1000 * (1 if m.group(1) == "+" else -1) if m.group(1) else secs * 1000
            sp.seek_track(max(0, target))
            return f"Moved to {max(0, target) // 60000}:{max(0, target) // 1000 % 60:02d}."
        if action == "volume":
            cur = sp.current_playback() or {}
            dev = cur.get("device") or {}
            now = dev.get("volume_percent") or 50
            v = value.lower()
            up = v in ("up", "louder", "aumentar", "mais alto", "+")
            down = v in ("down", "lower", "quieter", "baixar", "diminuir", "mais baixo", "-")
            m = re.search(r"\d+", v)
            if not (up or down or m):
                return f"Volume is {now}. Say a number from 0 to 100, or up/down."
            if dev.get("type") == "Smartphone" or (not dev and _wants_phone(device)):
                return _phone_volume("up" if up else "down" if down else m.group())
            new = max(0, min(100, now + 15 if up else now - 15 if down else int(m.group())))
            try:
                sp.volume(new)
            except Exception as e:
                if "Cannot control device volume" not in str(e):
                    raise
                if _wants_phone(device):
                    return _phone_volume("up" if up else "down" if down else str(new))
                return (f"{dev.get('name', 'This device')} doesn't let Spotify change its "
                        f"volume remotely — use its own volume control.")
            return f"Spotify volume {new}."
        if action == "shuffle":
            on = value.lower() not in ("off", "false", "desligar", "desligado", "no", "0")
            sp.shuffle(on)
            return f"Shuffle {'on' if on else 'off'}."
        if action == "repeat":
            v = value.lower()
            mode = ("track" if v in ("track", "song", "música", "musica", "this", "esta", "one")
                    else "off" if v in ("off", "false", "desligar", "no", "0") else "context")
            sp.repeat(mode)
            return {"track": "Repeating this song.", "context": "Repeating the album/playlist.",
                    "off": "Repeat off."}[mode]
        if action == "queue":
            res = sp.search(q=value, type="track", limit=1).get("tracks", {}).get("items", [])
            if not res:
                return f"I found no song for '{value}'."
            sp.add_to_queue(res[0]["uri"])
            return f"Added {_track_line(res[0])} to the queue."
        if action in ("now_playing", "status"):
            return _now(sp)
        if action == "like":
            t = _current_track(sp)
            sp.current_user_saved_tracks_add([t["uri"]])
            return f"Saved {_track_line(t)} to your liked songs."
        if action == "unlike":
            t = _current_track(sp)
            sp.current_user_saved_tracks_delete([t["uri"]])
            return f"Removed {_track_line(t)} from your liked songs."
        if action == "devices":
            devs = sp.devices().get("devices", [])
            if not devs:
                return "No Spotify device is online. Open Spotify on the PC, phone or TV."
            return "Spotify devices: " + "; ".join(
                f"{d['name']} ({d['type']}{', playing' if d.get('is_active') else ''})" for d in devs)
        if action == "transfer":
            dev_id, name = _device(sp, value or device)
            sp.transfer_playback(dev_id, force_play=True)
            return f"Moved the music to {name}."
        if action == "add_to_playlist":
            pl = _find_playlist(sp, value)
            if not pl:
                return f"You have no playlist called '{value}'."
            t = _current_track(sp)
            sp.playlist_add_items(pl["id"], [t["uri"]])
            return f"Added {_track_line(t)} to {pl['name']}."
        if action == "create_playlist":
            if not value:
                return "What should the playlist be called?"
            me = sp.current_user()["id"]
            pl = sp.user_playlist_create(me, value, public=False,
                                         description="Created by voice assistant")
            return f"Created the private playlist {pl['name']}."
        if action == "playlists":
            res = sp.current_user_playlists(limit=30).get("items", [])
            return "Your playlists: " + ", ".join(p["name"] for p in res if p) if res else \
                "You have no playlists."
        if action == "recent":
            items = sp.current_user_recently_played(limit=8).get("items", [])
            return "Recently played: " + "; ".join(_track_line(i["track"]) for i in items) \
                if items else "Nothing played recently."
        return f"Unknown Spotify action '{action}'."
    except Exception as e:
        if _premium_error(e):
            # Free account: the app still obeys the media keys.
            keys = {"pause": "playpause", "stop": "playpause", "resume": "playpause",
                    "next": "nexttrack", "previous": "prevtrack"}
            if action in keys and _media_key(keys[action]):
                return f"Done with the media keys ({action}) — this account is not Premium."
            if action == "play" and value and _wants_phone(device):
                r = _phone_call("play_music", {"query": value}, 25)
                if r.get("ok"):
                    return f"Playing '{value}' on the phone's Spotify."
            if action == "play" and value:
                from urllib.parse import quote
                _open_app("spotify:search:" + quote(value))
                return (f"This Spotify account is not Premium, so I can't start a song "
                        f"directly — I opened the search for '{value}' in Spotify.")
            return ("Spotify only allows that on Premium accounts. Free accounts can use "
                    "pause, next and previous.")
        msg = str(e)
        if "NO_ACTIVE_DEVICE" in msg or "Device not found" in msg:
            return "No Spotify device is active — open Spotify on the PC, phone or TV."
        if isinstance(e, RuntimeError):
            return msg[:1].upper() + msg[1:] + "."
        print(f"[Spotify] {action} failed: {e}")
        return f"Spotify: {action} failed — {getattr(e, 'msg', msg)[:160]}"


PLUGIN = {
    "name": "spotify",
    "description": (
        "Full control of the user's Spotify. Actions: 'play' (value=what to play — "
        "song, artist, album, a playlist of theirs, 'liked songs', or a mood/genre "
        "like 'something to relax'; kind=track|artist|album|playlist when the user "
        "says which; device=where, optional — when the user is talking from the "
        "phone app it plays on the phone by itself, opening Spotify there if needed; "
        "never use android_phone open_app to play music; empty value resumes); 'pause'; "
        "'resume'; 'next'; 'previous'; 'seek' (value '+30', '-15' seconds or "
        "'1:20'); 'volume' (value 0-100, 'up' or 'down'); 'shuffle' (value on|off); "
        "'repeat' (value track|context|off); 'queue' (value=song to add); "
        "'now_playing' (what is playing); 'like' / 'unlike' (current song); "
        "'devices'; 'transfer' (value=device name — 'play it on the TV'); "
        "'add_to_playlist' (value=playlist name, adds the current song); "
        "'create_playlist' (value=name); 'playlists'; 'recent'; 'connect' "
        "(first-time approval in the browser). Use this for any music request — "
        "not video_player and not computer_settings volume."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of the actions above."},
            "value": {"type": "STRING", "description": "Query, number, name or mode for the action."},
            "kind": {"type": "STRING", "description": "play only: track | artist | album | playlist"},
            "device": {"type": "STRING", "description": "play/resume: Spotify device to use, optional"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "Spotify",
    "fields": [
        {"key": "client_id", "label": "Client ID (developer.spotify.com/dashboard)", "type": "text"},
        {"key": "redirect_uri", "label": "Redirect URI registered in the Spotify app",
         "type": "text", "placeholder": REDIRECT_URI},
    ],
    "action": {"label": "CONNECT", "run": connect},
}
