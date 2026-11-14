"""
Sonoff control, over the LAN, without the eWeLink cloud in the path.

Sonoff devices on recent firmware listen on TCP 8081 and accept the same JSON
commands the eWeLink app sends when phone and plug share a Wi-Fi network
("LAN mode"). Two flavours:

  DIY mode     plain JSON. Enabled per device (hold the button / eWeLink's
               "DIY" switch); the device ID may be left empty.
  LAN mode     the default. The same JSON, AES-128-CBC encrypted with
               MD5(devicekey). The device ID and key are needed, and the key
               lives only in the user's eWeLink account, so it is fetched once
               from there (cloud_sync) and from then on everything is local.

Tasmota-flashed Sonoffs are handled too: they speak plain HTTP on port 80.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import time

from pathlib import Path

import requests

TIMEOUT = (1.5, 3)        # (connect, read): a Sonoff on the LAN answers in ~0.2–0.5 s

# Relay count by eWeLink uiid. Many one-relay models (MINI R3/R4, M5 one-gang…)
# report four positions in "switches"; only the first is a real relay.
_UIID_CHANNELS = {**{u: 1 for u in (77, 78, 112, 138, 160, 209)},
                  **{u: 2 for u in (2, 7, 29, 113, 126, 161, 210)},
                  **{u: 3 for u in (3, 8, 30, 114, 162, 211)},
                  **{u: 4 for u in (4, 9, 31)}}
_ONE_RELAY_MODELS = ("MINIR3", "MINIR4", "MINIR2", "MINI", "BASICR2", "BASICR3", "BASICR4", "RFR2", "RFR3")


def channel_count(uiid: int = 0, reported: int = 1, model: str = "") -> int:
    reported = max(1, int(reported or 1))
    n = _UIID_CHANNELS.get(int(uiid or 0))
    if n is None and (model or "").upper().replace(" ", "").replace("-", "") in _ONE_RELAY_MODELS:
        n = 1
    return min(n or min(reported, 4), reported)


# ── eWeLink LAN protocol ─────────────────────────────────────────────────────

def _encrypt(params: dict, devicekey: str) -> tuple[str, str]:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = hashlib.md5(devicekey.encode()).digest()
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    raw = padder.update(json.dumps(params).encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    data = enc.update(raw) + enc.finalize()
    return base64.b64encode(iv).decode(), base64.b64encode(data).decode()


UNREACHABLE = "could not reach it"

_ERRORS = {
    400: "the device rejected the request format",
    401: "the device key is wrong — run 'sync Sonoff' again",
    404: "the device ID does not match this device — re-scan, the IP may have moved",
    422: "the device does not support that command",
}


def lan_command(ip: str, command: str, params: dict, deviceid: str = "",
                devicekey: str = "", port: int = 8081) -> str:
    """POST one LAN command. Returns '' on success, else a reason."""
    body: dict = {"sequence": str(int(time.time() * 1000)), "deviceid": deviceid,
                  "selfApikey": "123"}
    if devicekey:
        iv, data = _encrypt(params, devicekey)
        body.update({"iv": iv, "encrypt": True, "data": data})
    else:
        body["data"] = params
    try:
        r = requests.post(f"http://{ip}:{port}/zeroconf/{command}", json=body, timeout=TIMEOUT)
    except requests.RequestException as e:
        return f"{UNREACHABLE} at {ip} ({type(e).__name__})"
    try:
        resp = r.json()
    except ValueError:
        if not devicekey:
            return ("it answered, but not in DIY mode — this Sonoff needs its device "
                    "ID and key: run 'sync Sonoff' (eWeLink account in ⚙ settings)")
        return f"unexpected answer: {r.text[:80]}"
    err = resp.get("error", 0)
    return "" if err == 0 else _ERRORS.get(err, f"device error {err}")


def set_power(dev: dict, on: bool, outlet: int | None = None) -> str:
    s = dev.get("sonoff", {})
    if outlet is not None or s.get("channels", 1) > 1:
        params = {"switches": [{"switch": "on" if on else "off", "outlet": outlet or 0}]}
        return lan_command(dev["ip"], "switches", params, s.get("id", ""), s.get("key", ""))
    return lan_command(dev["ip"], "switch", {"switch": "on" if on else "off"},
                       s.get("id", ""), s.get("key", ""))


def set_brightness(dev: dict, pct: int) -> str:
    """Sonoff D1 dimmer. Other Sonoff models have no brightness over LAN."""
    s = dev.get("sonoff", {})
    params = {"switch": "on", "brightness": max(1, min(100, pct)), "mode": 0,
              "brightmin": 0, "brightmax": 100}
    return lan_command(dev["ip"], "dimmable", params, s.get("id", ""), s.get("key", ""))


def moved(err: str) -> bool:
    """True when a command failed because the plug is no longer at that IP."""
    return err.startswith(UNREACHABLE) or "does not match this device" in err


def locate(devices: list[dict], local_ip: str = "") -> dict[str, str]:
    """Find Sonoffs on the LAN without mDNS (the MINI R4 does not announce
    itself reliably): every host with TCP 8081 open on the /24 is asked
    /zeroconf/info encrypted with each plug's own key — only the right plug
    answers error 0. Returns {deviceid: ip}."""
    from concurrent.futures import ThreadPoolExecutor
    want = [d for d in devices if d.get("id") and d.get("key")]
    if not want:
        return {}
    if not local_ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.168.0.1", 9))
            local_ip = s.getsockname()[0]
            s.close()
        except OSError:
            return {}
    prefix = local_ip.rsplit(".", 1)[0]

    def open_8081(ip: str) -> str:
        try:
            with socket.create_connection((ip, 8081), timeout=0.4):
                return ip
        except OSError:
            return ""

    with ThreadPoolExecutor(64) as pool:
        hosts = [ip for ip in pool.map(open_8081, (f"{prefix}.{i}" for i in range(1, 255))) if ip]
    found: dict[str, str] = {}
    for ip in hosts:
        for d in want:
            if d["id"] in found:
                continue
            if lan_command(ip, "info", {}, d["id"], d["key"]) == "":
                found[d["id"]] = ip
                break
        if len(found) == len(want):
            break
    return found


# ── Tasmota ──────────────────────────────────────────────────────────────────

def tasmota(ip: str, cmnd: str) -> tuple[str, dict]:
    """('' , response) on success, (reason, {}) on failure."""
    try:
        r = requests.get(f"http://{ip}/cm", params={"cmnd": cmnd}, timeout=TIMEOUT)
        return "", r.json()
    except requests.RequestException as e:
        return f"could not reach it at {ip} ({type(e).__name__})", {}
    except ValueError:
        return "it answered, but not like Tasmota", {}


# ── eWeLink account: fetch device IDs and keys, once ─────────────────────────
#
# dev.ewelink.cc apps sign users in with OAuth 2.0: the user logs in on
# eWeLink's own page and the app receives a code, never the password. The
# signing rules below follow CoolKit's official SDK (ewelink-api-next):
#   login page   HMAC-SHA256(secret, f"{appid}_{seq}"), base64
#   API bodies   HMAC-SHA256(secret, the exact JSON sent), base64
# The access token lasts 30 days and is renewed with the refresh token, so the
# browser step happens once.

REDIRECT_PORT = 8889
# dev.ewelink.cc only accepts HTTPS redirect URLs, so the callback is served
# over TLS with the dashboard's self-signed certificate (it lists 127.0.0.1).
# The browser warns once about the certificate; "Advanced → continue" is fine,
# the page is this computer talking to itself.
REDIRECT_URL = f"https://127.0.0.1:{REDIRECT_PORT}/ewelink/callback"
CERT_DIR = Path(__file__).resolve().parents[2] / "config" / "certs"
TOKEN_PATH = Path(__file__).resolve().parents[2] / "memory" / "ewelink_token.json"
_REFRESH_AFTER = 25 * 86400       # renew well before the 30-day expiry


def _host(region: str) -> str:
    region = (region or "us").lower()
    return f"{region}-apia.coolkit.{'cn' if region in ('cn', 'test') else 'cc'}"


def _sign(msg: str, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()


def _signed_post(region: str, path: str, body: dict, appid: str, secret: str) -> dict:
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    r = requests.post(f"https://{_host(region)}{path}", data=raw.encode(), timeout=10,
                      headers={"Content-Type": "application/json", "X-CK-Appid": appid,
                               "Authorization": f"Sign {_sign(raw, secret)}"})
    return r.json()


def redirect_url(cfg: dict) -> str:
    """The Redirect URL registered on dev.ewelink.cc — it must match exactly.
    Defaults to ours; the official demo's http://127.0.0.1:8000/redirectUrl
    works too, but 8000 is the phone dashboard's port, so ours is preferred."""
    return (cfg.get("ewelink_redirect") or "").strip() or REDIRECT_URL


def _creds(cfg: dict) -> tuple[str, str]:
    return ((cfg.get("ewelink_appid") or "").strip(),
            (cfg.get("ewelink_appsecret") or "").strip())


def _load_token() -> dict:
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_token(tok: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    os.replace(tmp, TOKEN_PATH)


def oauth_connect(cfg: dict, timeout: float = 180) -> tuple[bool, str]:
    """Open eWeLink's login page and wait for the redirect with the code.
    Runs on a worker thread (the settings button); returns (ok, message)."""
    import secrets as _secrets
    import threading
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, quote, urlparse

    appid, secret = _creds(cfg)
    if not (appid and secret):
        return False, ("eWeLink's OAuth login needs the APP SECRET too — on dev.ewelink.cc "
                       "open Console → your app → View: it shows APPID and APP SECRET. If "
                       "the secret is missing, the developer account may still be under review")
    redirect = redirect_url(cfg)
    ru = urlparse(redirect)
    if ru.hostname not in ("127.0.0.1", "localhost") or ru.scheme not in ("http", "https"):
        return False, (f"the Redirect URL must point to this computer, e.g. {REDIRECT_URL} "
                       f"— set the same address on dev.ewelink.cc")
    port = ru.port or (443 if ru.scheme == "https" else 80)
    path = ru.path or "/"
    state = _secrets.token_urlsafe(12)
    got: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            if u.path != path:
                self.send_response(404); self.end_headers(); return
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            ok = q.get("state") == state and q.get("code")
            if ok:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<h2>eWeLink conectado. Pode fechar esta janela.</h2>" if ok else
                              "<h2>Resposta inesperada do eWeLink.</h2>").encode())

        def log_message(self, *a):
            pass

    try:
        class _Exclusive(HTTPServer):
            # Python's default lets a second server share the port on Windows,
            # and the redirect then lands on whichever one answers — an older
            # login window still waiting. Refuse to share instead.
            allow_reuse_address = False

            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        srv = _Exclusive(("127.0.0.1", port), Handler)
    except OSError as e:
        return False, (f"port {port} is busy — another eWeLink login window may still be "
                       f"waiting; wait 3 minutes or restart the assistant ({e})"
                       + (" — 8000 is the phone dashboard's port; "
                       f"register {REDIRECT_URL} on dev.ewelink.cc instead" if port == 8000 else ""))
    if ru.scheme == "https":
        import ssl
        crt, key = CERT_DIR / "jarvis.crt", CERT_DIR / "jarvis.key"
        if not (crt.exists() and key.exists()):
            srv.server_close()
            return False, ("the local HTTPS certificate is missing — open the phone "
                           "dashboard once (it creates config/certs), then try again")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(crt), str(key))
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        # The browser's first attempt fails on the self-signed certificate
        # until the user clicks through; that failed handshake must not print
        # a traceback or end the wait.
        srv.handle_error = lambda *a: None
    srv.timeout = 1
    # The URL carries the signature unencoded (as CoolKit's SDK does), and a
    # bare "+" in a query string is read back as a space — which breaks the
    # signature. Nudging seq by a millisecond gives a different signature;
    # pick the first one without a "+".
    seq_n = int(time.time() * 1000)
    while "+" in _sign(f"{appid}_{seq_n}", secret):
        seq_n += 1
    seq = str(seq_n)
    params = {"clientId": appid, "redirectUrl": redirect,
              "grantType": "authorization_code", "state": state,
              "nonce": _secrets.token_hex(4), "seq": seq,
              "authorization": _sign(f"{appid}_{seq}", secret)}
    # Built exactly like CoolKit's SDK (createLoginUrl): values unencoded.
    url = "https://c2ccdn.coolkit.cc/oauth/index.html?" + "&".join(
        f"{k}={v}" for k, v in params.items())
    webbrowser.open(url)
    end = time.time() + timeout
    try:
        while not got and time.time() < end:
            srv.handle_request()
    finally:
        srv.server_close()
    if not got:
        return False, "no eWeLink login within 3 minutes — press the button again"

    region = got.get("region") or "us"
    try:
        resp = _signed_post(region, "/v2/user/oauth/token",
                            {"redirectUrl": redirect, "code": got["code"],
                             "grantType": "authorization_code"}, appid, secret)
    except Exception as e:
        return False, f"could not reach eWeLink ({type(e).__name__})"
    if resp.get("error") != 0:
        msg = resp.get("msg") or f"error {resp.get('error')}"
        if "redirect" in str(msg).lower() or resp.get("error") == 405:
            msg += f" — the app's Redirect URL on dev.ewelink.cc must be exactly {redirect}"
        return False, f"eWeLink token request failed: {msg}"
    d = resp.get("data") or {}
    _save_token({"at": d.get("accessToken"), "rt": d.get("refreshToken"),
                 "region": d.get("region") or region, "issued": time.time(),
                 "appid": appid})
    user = d.get("user") or {}
    return True, f"eWeLink connected ({user.get('email') or user.get('phoneNumber') or 'account'})."


def _access_token(cfg: dict) -> tuple[str, str, str]:
    """(error, access token, region). Renews the token when it is getting old."""
    appid, secret = _creds(cfg)
    tok = _load_token()
    if not tok.get("at") or tok.get("appid") != appid:
        return ("eWeLink is not connected — in ⚙ → plugin settings → Smart Home press "
                "CONNECT / TEST and log in on the eWeLink page"), "", ""
    if time.time() - tok.get("issued", 0) > _REFRESH_AFTER and tok.get("rt"):
        try:
            resp = _signed_post(tok["region"], "/v2/user/refresh", {"rt": tok["rt"]}, appid, secret)
            if resp.get("error") == 0:
                d = resp.get("data") or {}
                tok.update(at=d.get("at") or tok["at"], rt=d.get("rt") or tok["rt"],
                           issued=time.time())
                _save_token(tok)
            elif time.time() - tok.get("issued", 0) > 30 * 86400:
                return "the eWeLink login expired — press CONNECT / TEST again", "", ""
        except Exception:
            pass                                  # try the current token anyway
    return "", tok["at"], tok["region"]


def has_oauth_token(cfg: dict) -> bool:
    return bool(_load_token().get("at")) and _load_token().get("appid") == _creds(cfg)[0]


def cloud_sync(cfg: dict) -> tuple[str, list[dict]]:
    """List the user's eWeLink devices with their LAN keys, through the OAuth
    token. Returns ('', devices) or (reason, [])."""
    appid, secret = _creds(cfg)
    if not (appid and secret):
        return ("the eWeLink app is not set up — fill in ⚙ → plugin settings → Smart "
                "Home: App ID and App Secret from dev.ewelink.cc, then press CONNECT / TEST"), []
    err, at, region = _access_token(cfg)
    if err:
        return err, []
    try:
        r = requests.get(f"https://{_host(region)}/v2/device/thing", timeout=10,
                         params={"num": 0},
                         headers={"X-CK-Appid": appid, "Authorization": f"Bearer {at}"})
        resp = r.json()
    except Exception as e:
        return f"could not list eWeLink devices ({type(e).__name__})", []
    if resp.get("error") not in (0, None):
        if resp.get("error") in (401, 402, 406):
            return "the eWeLink login is no longer valid — press CONNECT / TEST again", []
        return f"eWeLink error: {resp.get('msg') or resp.get('error')}", []
    things = (resp.get("data") or {}).get("thingList", [])

    out = []
    for t in things:
        item = t.get("itemData") or {}
        if not item.get("deviceid"):
            continue
        params = item.get("params") or {}
        model = (item.get("productModel") or "") + ""
        out.append({
            "id": item["deviceid"],
            "key": item.get("devicekey", ""),
            "name": (item.get("name", "") or "").strip(),
            "model": model,
            "channels": channel_count((item.get("extra") or {}).get("uiid", 0),
                                      len(params.get("switches", [])) or 1, model),
            "multi": bool(params.get("switches")),
            "mac": (params.get("staMac") or "").upper(),
        })
    return "", out
