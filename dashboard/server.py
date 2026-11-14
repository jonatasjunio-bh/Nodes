"""
dashboard/server.py — JARVIS Local HTTP Dashboard

HTTPS on port 8000 with a self-signed certificate generated on first run, plus
an HTTPS alias on port 8001 for manually typed URLs. Falls back to plain HTTP on
port 8000 when the `cryptography` package is missing. Binds to the LAN so a
phone on the same network can connect (PIN or QR login → bearer token).
Commands additionally use AES-256-CBC keyed from the one-time PIN.
CryptoJS is served from dashboard/static/crypto-js.min.js — never downloaded.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import json
import platform
import re
import secrets
import shutil
import socket
import string
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
MAX_UPLOAD_MB = 500
UPLOAD_QUOTA_MB = 2048            # total size cap for the uploads folder

# Session lifetimes and login throttling
TOKEN_TTL_SECS        = 12 * 3600       # bearer token (one browser session)
DEVICE_TOKEN_TTL_SECS = 30 * 24 * 3600  # "remember this phone" token
LOGIN_MAX_FAILS       = 5               # failures per IP within the window…
LOGIN_FAIL_WINDOW     = 600
LOGIN_LOCK_SECS       = 300             # …lock that IP out for this long


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── CryptoJS (served from the local static folder only) ──────────────────────
# Nothing is fetched from the network at import time: an unpinned download that
# is then served to the phone as trusted script would be a supply-chain hole.
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(ports: list[int]) -> None:
    """Cross-platform, best-effort: allow the dashboard's TCP ports through the
    OS firewall so a phone on the LAN can connect.

    Runs in a background thread — never blocks uvicorn startup. Never changes
    network profiles, never adds program-wide rules and never self-elevates:
    when it lacks the rights it only prints the command to run by hand.

    Windows : one netsh rule for exactly these TCP ports, Private profile only.
    macOS   : prints a hint if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys, subprocess

    port_list = ",".join(str(p) for p in ports)

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        rule = "JARVIS Dashboard Ports"
        try:
            r = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and "No rules match" not in r.stdout:
                return  # already configured
        except Exception:
            pass

        add_cmd = ["netsh", "advfirewall", "firewall", "add", "rule",
                   f"name={rule}", "dir=in", "action=allow", "protocol=TCP",
                   f"localport={port_list}", "profile=private", "enable=yes"]
        try:
            # List arguments, no shell — succeeds only when already elevated.
            r = subprocess.run(add_cmd, capture_output=True, text=True, timeout=8)
            if r.returncode == 0:
                print(f"[Dashboard] Firewall rule added for TCP {port_list} (Private networks).")
                return
        except Exception:
            pass

        print(f"[Dashboard] If your phone can't connect, allow TCP {port_list} once from an "
              "elevated prompt:")
        print(f'[Dashboard]   netsh advfirewall firewall add rule name="{rule}" dir=in '
              f"action=allow protocol=TCP localport={port_list} profile=private")
        print("[Dashboard] (This only applies when Windows treats your Wi-Fi as a Private network.)")
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do
            print("[Dashboard] macOS firewall is on — accept the 'allow incoming connections' "
                  "prompt for Python if your phone can't connect.")
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            for port in ports:
                if _privileged(["ufw", "allow", f"{port}/tcp"]):
                    print(f"[Dashboard] ufw: port {port} allowed.")
                else:
                    print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = all(_privileged(["firewall-cmd", "--add-port", f"{p}/tcp", "--permanent"])
                     for p in ports) and _privileged(["firewall-cmd", "--reload"])
            if ok:
                print(f"[Dashboard] firewalld: ports {port_list} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port=<port>/tcp "
                      f"--permanent (for {port_list}) && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            for port in ports:
                if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                    print(f"[Dashboard] iptables: port {port} opened.")
                else:
                    print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do


if not _CRYPTOJS_FILE.exists():
    print(f"[Dashboard] WARNING: {_CRYPTOJS_FILE} is missing — the phone page cannot "
          "encrypt commands. Place crypto-js 4.2.0 (crypto-js.min.js) there.")


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


# ── Tailscale: reach the PC from anywhere, with a certificate phones trust ────
#
# Tailscale links the phone and the PC into a private network that works from
# any connection, with no router ports opened. With "HTTPS Certificates" on in
# the tailnet's DNS settings it also issues a real Let's Encrypt certificate for
# the PC's name (e.g. meu-pc.tail1234.ts.net) — which is what Chrome needs to
# install the dashboard as an app and to allow the microphone without warnings.

_TS_RENEW_DAYS = 60          # certificates last 90 days


def _tailscale_exe() -> str:
    exe = shutil.which("tailscale")
    if exe:
        return exe
    for c in (r"C:\Program Files\Tailscale\tailscale.exe",
              "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if Path(c).exists():
            return c
    return ""


def _saved_tailscale_cert() -> tuple[str, str, str] | None:
    """The Tailscale certificate issued earlier, while it is still valid.

    The phone app connects to the PC's Tailscale name (meu-pc….ts.net) and
    trusts only that certificate. When MARK opened before Tailscale had finished
    connecting (right after Windows starts), the dashboard used to fall back to
    the self-signed one for the whole run — and the app could not connect from
    anywhere until MARK was restarted. The saved certificate is just as valid
    while Tailscale is still coming up."""
    certs = BASE_DIR / "config" / "certs"
    crt, key = certs / "tailscale.crt", certs / "tailscale.key"
    if not (crt.exists() and key.exists()):
        return None
    try:
        import datetime
        from cryptography import x509
        c = x509.load_pem_x509_certificate(crt.read_bytes())
        if c.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc) < datetime.timedelta(days=1):
            return None
        names = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value \
            .get_values_for_type(x509.DNSName)
        return (names[0], str(crt), str(key)) if names else None
    except Exception:
        return None


def _tailscale_tls() -> tuple[str, str, str] | None:
    """(hostname, cert file, key file) when Tailscale can provide a certificate,
    else None. Fetches or renews it with `tailscale cert` as needed."""
    exe = _tailscale_exe()
    if not exe:
        return _saved_tailscale_cert()
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if platform.system() == "Windows" else {}
    st = {}
    for attempt in range(6):          # Tailscale may still be connecting (Windows just started)
        try:
            st = json.loads(subprocess.run([exe, "status", "--json"], capture_output=True,
                                           text=True, timeout=8, **kw).stdout or "{}")
        except Exception as e:
            print(f"[Dashboard] Tailscale status unavailable: {e}")
            st = {}
        if st.get("BackendState") == "Running":
            break
        time.sleep(5)
    if st.get("BackendState") != "Running":
        saved = _saved_tailscale_cert()
        if saved:
            print(f"[Dashboard] Tailscale not connected yet — using its saved certificate for {saved[0]}.")
            return saved
        print("[Dashboard] Tailscale is installed but not connected — using the local certificate.")
        return None
    host = (st.get("Self", {}).get("DNSName") or "").rstrip(".")
    if not host or not st.get("CertDomains"):
        print("[Dashboard] Tailscale HTTPS certificates are off — enable them in the Tailscale "
              "admin console (DNS → HTTPS Certificates) for a trusted dashboard address.")
        return None
    certs = BASE_DIR / "config" / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    crt, key = certs / "tailscale.crt", certs / "tailscale.key"
    fresh = crt.exists() and key.exists() and (time.time() - crt.stat().st_mtime) < _TS_RENEW_DAYS * 86400
    if not fresh:
        r = subprocess.run([exe, "cert", "--cert-file", str(crt), "--key-file", str(key), host],
                           capture_output=True, text=True, timeout=90, **kw)
        if r.returncode != 0 or not crt.exists():
            print(f"[Dashboard] tailscale cert failed: {(r.stderr or r.stdout).strip()[:200]}")
            return None
        print(f"[Dashboard] Tailscale certificate issued for {host}")
    return host, str(crt), str(key)


def _ensure_certs() -> bool:
    """
    Make sure config/certs holds a TLS key pair, generating a self-signed one the
    first time the dashboard runs.

    The pair is deliberately NOT shipped in the repository. A private key that
    every user downloads is the same as having no private key at all: anyone can
    present a certificate that matches it. Generating locally gives each install
    its own key, costs about a second, and happens exactly once.

    Returns True when a usable pair exists afterwards; False leaves the caller on
    plain HTTP, which still works — the QR code simply encodes http:// instead.
    """
    certs = BASE_DIR / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if key_p.exists() and crt_p.exists():
        return True

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("[Dashboard] cryptography not installed — serving over plain HTTP.")
        print("[Dashboard] For HTTPS run:  pip install cryptography")
        return False

    try:
        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        who = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Dashboard"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
        ])

        # The SAN has to cover every address the phone might use: the LAN IP the
        # QR code encodes, plus localhost when testing on the machine itself.
        alt = [x509.DNSName("localhost"),
               x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
        try:
            lan = _local_ip()
            if not lan.startswith("127."):
                alt.append(x509.IPAddress(ipaddress.IPv4Address(lan)))
        except Exception:
            pass          # no LAN address resolvable — localhost entries still work

        # Timezone-aware UTC: datetime.utcnow() is deprecated from Python 3.12 on,
        # and the builder normalises aware values to UTC itself.
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(who)
            .issuer_name(who)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )

        key_p.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        try:
            import os as _os
            _os.chmod(key_p, 0o600)   # best effort — largely a no-op on Windows
        except Exception:
            pass

        print(f"[Dashboard] Generated a self-signed certificate for this machine: {certs}")
        return True
    except Exception as e:
        print(f"[Dashboard] Certificate generation failed ({e}) — serving over plain HTTP.")
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._tokens: dict[str, float]    = {}   # auth_token → issued-at
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._clients: set[WebSocket]     = set()
        self._ws_tokens: dict[WebSocket, str] = {}  # every open socket → its auth_token
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue()   # read directly by main.py
        self._wake_callback               = None
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}   # at most one live PIN → expiry
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key, issued}
        self._login_fails: dict[str, list[float]] = {}  # client IP → failure times
        self._login_locked: dict[str, float]      = {}  # client IP → locked until
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        # Phone as the assistant's speaker: sockets that want reply audio, and
        # when the phone was last used to talk to it (main.py routes replies
        # to the phone only while that is recent — see phone_wants_audio).
        self._speakers: set = set()
        self._native_audio: set = set()     # the app's native voice links (/ws/device-audio)
        self.phone_bt = False               # the phone has Bluetooth audio / Android Auto up
        self.last_phone_activity = 0.0
        # Native Android app: permanent pairings (hashed tokens on disk, so a
        # PC restart doesn't unpair the phone), the live command link, and the
        # requests waiting for the phone's answer.
        self._app_devices_path = BASE_DIR / "config" / "app_devices.json"
        self._device_ws = None
        self._device_info: dict = {}
        self._device_pending: dict = {}
        self._notification_cb = None
        self.recent_notifications: list = []
        DashboardServer.instance = self
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        # Only one PIN is ever valid: issuing a new one invalidates the old.
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys = {key: time.time() + expiry_secs}
        return key

    def _consume_key(self, entered: str) -> bool:
        """Constant-time check of a PIN against the live one; consumes it on match."""
        now = time.time()
        for k, exp in list(self._pending_keys.items()):
            if exp > now and secrets.compare_digest(k.encode(), entered.encode()):
                self._pending_keys.pop(k, None)          # one-time use
                return True
        # The owner's fixed key (⚙ → Remote access): never consumed, so it
        # always works for re-pairing or a browser login. Same lock-out after 5
        # wrong tries as the one-time keys; at least 8 characters is enforced.
        owner = self.owner_key()
        if owner and secrets.compare_digest(owner.upper().encode(), entered.upper().encode()):
            return True
        return False

    @staticmethod
    def owner_key() -> str:
        try:
            from memory.config_manager import get_plugin_config
            k = str(get_plugin_config("remote_access").get("owner_key", "") or "").strip()
        except Exception:
            return ""
        return k if len(k) >= 8 else ""

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_url(self) -> str:
        # The Tailscale name works at home and away, with a trusted certificate.
        if getattr(self, "_ts_host", ""):
            return f"https://{self._ts_host}:{PORT}"
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    # ── sessions ─────────────────────────────────────────────────────────

    def _prune_sessions(self) -> None:
        """Drop expired bearer/device tokens and any AES keys nothing refers to."""
        now = time.time()
        for tok, issued in list(self._tokens.items()):
            if now - issued > TOKEN_TTL_SECS:
                self._tokens.pop(tok, None)
                self._token_keys.pop(tok, None)
        for dev, info in list(self._device_sessions.items()):
            if now - info.get("issued", 0) > DEVICE_TOKEN_TTL_SECS:
                self._device_sessions.pop(dev, None)
        live = set(self._token_keys.values())
        live.update(d["session_key"] for d in self._device_sessions.values())
        for sk in list(self._aes_cache):
            if sk not in live:
                self._aes_cache.pop(sk, None)

    def _token_valid(self, tok: str) -> bool:
        if not tok:
            return False
        issued = self._tokens.get(tok)
        if issued is None:
            return False
        if time.time() - issued > TOKEN_TTL_SECS:
            self._prune_sessions()
            return False
        return True

    def _issue_token(self, session_key: str) -> str:
        self._prune_sessions()
        tok = secrets.token_urlsafe(32)
        self._tokens[tok] = time.time()
        self._token_keys[tok] = session_key
        self._aes_key(session_key)                       # pre-derive & cache
        return tok

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    # ── login throttling ─────────────────────────────────────────────────

    @staticmethod
    def _client_ip(conn) -> str:
        return conn.client.host if conn.client else "?"

    def _is_locked(self, ip: str) -> bool:
        until = self._login_locked.get(ip)
        if until is None:
            return False
        if until <= time.time():
            self._login_locked.pop(ip, None)
            return False
        return True

    def _record_fail(self, ip: str) -> None:
        now = time.time()
        # Forget stale entries so the map can't grow without bound.
        for k, times in list(self._login_fails.items()):
            if not times or now - times[-1] > LOGIN_FAIL_WINDOW:
                self._login_fails.pop(k, None)
        times = [t for t in self._login_fails.get(ip, []) if now - t <= LOGIN_FAIL_WINDOW]
        times.append(now)
        if len(times) >= LOGIN_MAX_FAILS:
            self._login_locked[ip] = now + LOGIN_LOCK_SECS
            self._login_fails.pop(ip, None)
            print(f"[Dashboard] Too many failed logins from {ip} — locked for "
                  f"{LOGIN_LOCK_SECS // 60} min.")
        else:
            self._login_fails[ip] = times

    def _clear_fails(self, ip: str) -> None:
        self._login_fails.pop(ip, None)

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    # ── native app: pairing and command link ─────────────────────────────

    def _app_load(self) -> list:
        try:
            return json.loads(self._app_devices_path.read_text(encoding="utf-8")).get("devices", [])
        except Exception:
            return []

    def _app_save(self, devices: list) -> None:
        self._app_devices_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._app_devices_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"devices": devices}, indent=2), encoding="utf-8")
        tmp.replace(self._app_devices_path)

    def _app_pair(self, name: str) -> str:
        token = secrets.token_urlsafe(40)
        devs = [d for d in self._app_load() if d.get("name") != name]   # re-pairing replaces
        devs.append({"name": name[:60] or "Android", "sha256": hashlib.sha256(token.encode()).hexdigest(),
                     "created": time.time()})
        self._app_save(devs)
        return token

    def _app_device(self, token: str) -> dict | None:
        if not token:
            return None
        h = hashlib.sha256(token.encode()).hexdigest()
        return next((d for d in self._app_load() if secrets.compare_digest(d.get("sha256", ""), h)), None)

    def set_upload_callback(self, fn) -> None:
        """A file sent from the phone (📎): fn(path) once it is saved."""
        self._upload_cb = fn

    def set_control_callback(self, fn) -> None:
        """The phone HUD's buttons: fn(action) with action 'interrupt' or 'mute'."""
        self._control_cb = fn

    def set_notification_callback(self, fn) -> None:
        self._notification_cb = fn

    def device_connected(self) -> bool:
        return self._device_ws is not None

    async def device_call(self, action: str, args: dict | None = None, timeout: float = 15.0) -> dict:
        """Ask the phone to do something; returns its {"ok", "result", ...} reply."""
        ws = self._device_ws
        if ws is None:
            return {"ok": False, "result": "the phone app is not connected — open MARK on the "
                                           "phone (and Tailscale, when away from home)"}
        cid = secrets.token_hex(8)
        fut = asyncio.get_running_loop().create_future()
        self._device_pending[cid] = fut
        try:
            await ws.send_json({"type": "cmd", "id": cid, "action": action, "args": args or {}})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "result": "the phone did not answer in time"}
        except Exception as e:
            return {"ok": False, "result": f"could not reach the phone ({type(e).__name__})"}
        finally:
            self._device_pending.pop(cid, None)

    # ── phone speaker ────────────────────────────────────────────────────

    PHONE_REPLY_WINDOW = 120.0     # seconds after the last phone interaction

    def touch_phone(self) -> None:
        self.last_phone_activity = time.monotonic()

    def phone_wants_audio(self) -> bool:
        """True when the reply should play on the phone: a phone speaker is
        connected and the phone was used (voice or text) in the last two
        minutes — or the phone has a Bluetooth headset / car / Android Auto
        connected, which means the owner is listening there, not at the PC.
        Otherwise replies stay on the PC's speakers."""
        if not self._speakers:
            return False
        if self.phone_bt and (self._native_audio & self._speakers):
            return True
        return time.monotonic() - self.last_phone_activity < self.PHONE_REPLY_WINDOW

    async def send_phone_audio(self, pcm: bytes) -> None:
        """24 kHz 16-bit mono PCM of the assistant's voice, to every phone speaker."""
        dead = set()
        for ws in list(self._speakers):
            try:
                await ws.send_bytes(pcm)
            except Exception:
                dead.add(ws)
        self._speakers -= dead

    async def send_phone_control(self, msg: dict) -> None:
        """Control messages for the phone player, e.g. {"type": "flush"} on
        interrupt so it stops talking at once."""
        dead = set()
        for ws in list(self._speakers):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._speakers -= dead

    # ── broadcast ────────────────────────────────────────────────────────

    async def hud_push(self, msg: dict) -> None:
        """A HUD card change, to every connected screen. Not kept in the
        message history: a screen that connects later gets the current cards
        from core/hud_mirror.snapshot() instead of a replay of every update."""
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead
        # The native app shows the conversation on the car screen / media
        # notification: hand it the transcript and the assistant's state.
        if msg.get("type") in ("log", "status") and self._native_audio:
            for ws in list(self._native_audio):
                try:
                    await ws.send_json(msg)
                except Exception:
                    self._native_audio.discard(ws)
                    self._speakers.discard(ws)

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _bearer(req: Request) -> str:
            return req.headers.get("authorization", "").removeprefix("Bearer ").strip()

        def _auth(req: Request) -> bool:
            return self._token_valid(_bearer(req))

        def _origin_ok(websocket: WebSocket) -> bool:
            # Browsers always send Origin on a WebSocket handshake, so a foreign
            # page trying to ride a token is caught here. A missing Origin means a
            # non-browser client, which still has to present a valid token.
            origin = websocket.headers.get("origin")
            if not origin:
                return True
            host = websocket.headers.get("host", "")
            return bool(host) and urlparse(origin).netloc.lower() == host.lower()

        @app.middleware("http")
        async def security_headers(request: Request, call_next):
            resp = await call_next(request)
            host = request.headers.get("host", "")
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["X-Frame-Options"]        = "DENY"
            resp.headers["Referrer-Policy"]        = "no-referrer"
            # The pages rely on inline <script> blocks and onclick= handlers, so
            # 'unsafe-inline' stays; the policy still blocks any third-party
            # script and any network connection outside this server. blob: is for
            # the mic AudioWorklet module that app.html builds in memory.
            ws_src = f" ws://{host} wss://{host}" if host else ""
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' blob:; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                f"connect-src 'self'{ws_src}; "
                "object-src 'none'; base-uri 'none'; form-action 'self'; "
                "frame-ancestors 'none'"
            )
            return resp

        # serve CryptoJS from the local static folder — no CDN fallback
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            print(f"[Dashboard] /static/crypto.js requested but {_CRYPTOJS_FILE} is missing.")
            return JSONResponse({"error": "crypto-js not installed on server"},
                                status_code=404)

        # ── Installable app (PWA) ───────────────────────────────────────────
        # Chrome on Android offers "Install app" for a page with a manifest, a
        # service worker and a trusted HTTPS origin; the Tailscale certificate
        # provides the last one (see _tls_files).
        _STATIC = Path(__file__).parent / "static"

        @app.get("/manifest.webmanifest")
        async def manifest():
            return JSONResponse({
                "name": "MARK — assistente pessoal",
                "short_name": "MARK",
                "start_url": "/",
                "scope": "/",
                "display": "standalone",
                "orientation": "portrait",
                "background_color": "#07090f",
                "theme_color": "#07090f",
                "icons": [
                    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {"src": "/static/icon-512-maskable.png", "sizes": "512x512",
                     "type": "image/png", "purpose": "maskable"},
                ],
            }, media_type="application/manifest+json")

        @app.get("/static/{name}.png")
        async def icon(name: str):
            f = _STATIC / f"{name}.png"
            if not re.fullmatch(r"icon-\d+(-maskable)?", name) or not f.exists():
                return JSONResponse({"error": "not found"}, status_code=404)
            return FileResponse(str(f), media_type="image/png",
                                headers={"Cache-Control": "public, max-age=604800"})

        # The native Android app, built from android/ — downloadable from the
        # dashboard so it installs without a cable. It holds no secrets: pairing
        # still needs a one-time key from the PC.
        _APK = BASE_DIR / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"

        @app.get("/app.apk")
        async def android_apk():
            if not _APK.exists():
                return JSONResponse({"error": "the Android app has not been built on this PC"},
                                    status_code=404)
            return FileResponse(str(_APK), media_type="application/vnd.android.package-archive",
                                filename="MARK.apk")

        @app.get("/sw.js")
        async def service_worker():
            # Network first, always: this app is a remote control for the PC,
            # so stale data is worse than none. Only the icons and CryptoJS are
            # cached, and an offline launch shows a clear "PC unreachable" page.
            js = """
const CACHE = 'mark-shell-v1';
const SHELL = ['/static/crypto.js', '/static/icon-192.png', '/static/icon-512.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return;
  if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/uploads/')) return;
  if (SHELL.includes(u.pathname)) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
    return;
  }
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() => new Response(
      '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">' +
      '<body style="background:#07090f;color:#8ffcff;font-family:sans-serif;padding:32px;text-align:center">' +
      '<h2>Sem conexão com o PC</h2><p>O assistente não respondeu. Confira se o PC está ligado ' +
      'com o MARK aberto e, fora de casa, se o Tailscale está ativo no celular.</p>' +
      '<button onclick="location.reload()" style="margin-top:16px;padding:12px 24px">Tentar de novo</button>',
      {headers: {'Content-Type': 'text/html; charset=utf-8'}})));
  }
});
"""
            return Response(js, media_type="application/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html)

        @app.post("/login")
        async def login(req: Request):
            ip = self._client_ip(req)
            if self._is_locked(ip):
                return JSONResponse({"ok": False, "error": "Too many attempts — try again later"},
                                    status_code=429)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            entered = str(body.get("pin", "")).strip().upper()
            if entered and self._consume_key(entered):
                self._clear_fails(ip)
                tok = self._issue_token(entered)
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                return JSONResponse({"ok": True, "token": tok})
            self._record_fail(ip)
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(req: Request, key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            ip = self._client_ip(req)
            locked = self._is_locked(ip)
            if locked or not key or not self._consume_key(key):
                if not locked:
                    self._record_fail(ip)
                title = "Too Many Attempts" if locked else "Link Expired"
                return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  h2{{color:#f87171;margin-bottom:12px}}p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body><div><h2>{title}</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in JARVIS to get a new QR code.</p>
</div></body></html>""", status_code=429 if locked else 200)

            self._clear_fails(ip)
            tok     = self._issue_token(key)
            dev_tok = secrets.token_urlsafe(32)
            self._device_sessions[dev_tok] = {"session_key": key, "issued": time.time()}

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            # tok/dev_tok are url-safe base64 and key is from _KEY_CHARS, so
            # interpolating them into the script below cannot break out of it.
            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            ip = self._client_ip(req)
            if self._is_locked(ip):
                return JSONResponse({"ok": False}, status_code=429)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            self._prune_sessions()                       # expires stale device tokens
            dev_tok = str(body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                self._record_fail(ip)
                return JSONResponse({"ok": False}, status_code=401)
            self._clear_fails(ip)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = self._issue_token(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            # The session key has to go back: app.html derives its AES key from it
            # and a fresh browser session has nothing else to derive it from. The
            # PIN it came from was already consumed, so it can't be used to log in.
            return JSONResponse({"ok": True, "token": tok, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens and every other active session.

            The caller's own bearer token is kept so the admin isn't logged out by
            their own click; everyone else (and their open sockets) is cut off.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            caller = _bearer(req)
            count = len(self._device_sessions) + len(self._app_load())
            self._device_sessions.clear()
            self._app_save([])                            # unpair native apps too
            if self._device_ws is not None:
                try:
                    await self._device_ws.close(code=4001)
                except Exception:
                    pass
            dropped = [t for t in self._tokens if t != caller]
            for t in dropped:
                self._tokens.pop(t, None)
                self._token_keys.pop(t, None)
            self._prune_sessions()
            for ws, t in list(self._ws_tokens.items()):
                if t != caller:
                    try:
                        await ws.close(code=4001)
                    except Exception:
                        pass
            return JSONResponse({"ok": True, "revoked": count, "sessions_closed": len(dropped)})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad request"}, status_code=400)
            token = _bearer(req)
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                self.touch_phone()
                await self._command_queue.put(text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not _origin_ok(websocket):
                await websocket.close(code=4003)
                return
            if not self._token_valid(tok):
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._ws_tokens[websocket] = tok
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    if tok not in self._tokens:          # revoked mid-stream
                        await websocket.close(code=4001)
                        break
                    self.touch_phone()
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except (WebSocketDisconnect, RuntimeError):
                pass              # RuntimeError: socket closed server-side (revoke/expiry)
            finally:
                self._ws_tokens.pop(websocket, None)
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        @app.post("/api/app-pair")
        async def app_pair(req: Request):
            """Native app pairing: a one-time PIN (from the QR code) buys a
            permanent device token. Only its hash is kept on the PC."""
            ip = self._client_ip(req)
            if self._is_locked(ip):
                return JSONResponse({"ok": False, "error": "Too many attempts — try again later"},
                                    status_code=429)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "Bad request"}, status_code=400)
            pin = str(body.get("pin", "")).strip().upper()
            if not pin or not self._consume_key(pin):
                self._record_fail(ip)
                return JSONResponse({"ok": False, "error": "Invalid or expired key"}, status_code=401)
            self._clear_fails(ip)
            token = self._app_pair(str(body.get("device", "Android")))
            asyncio.create_task(self.broadcast({"type": "sys", "text": "Android app paired."}))
            return JSONResponse({"ok": True, "app_token": token,
                                 "host": getattr(self, "_ts_host", "") or self._ip, "port": PORT})

        @app.post("/app-login")
        async def app_login(req: Request):
            """The app opens the dashboard UI with its device token (POSTed, so it
            never sits in a URL): a fresh browser session, like the QR login."""
            form = await req.form()
            if not self._app_device(str(form.get("app_token", ""))):
                return HTMLResponse("<h3 style='font-family:sans-serif'>This phone is no longer "
                                    "paired — pair it again from the app.</h3>", status_code=401)
            key = "".join(secrets.choice(_KEY_CHARS) for _ in range(6))
            tok = self._issue_token(key)
            return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head><body>
<script>sessionStorage.setItem('jarvis_token','{tok}');sessionStorage.setItem('jarvis_key','{key}');
location.replace('/');</script></body></html>""")

        @app.websocket("/ws/device")
        async def device_ws(websocket: WebSocket):
            """Command link to the native app: commands go down, results and
            notifications come up. Authenticated by the device token in the
            Authorization header."""
            auth = websocket.headers.get("authorization", "")
            dev = self._app_device(auth.removeprefix("Bearer ").strip())
            if not dev:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            if self._device_ws is not None:              # one phone link at a time
                try:
                    await self._device_ws.close(code=4000)
                except Exception:
                    pass
            self._device_ws = websocket
            self._device_info = {"name": dev.get("name")}
            # Console only: a reconnect after Wi-Fi ↔ 4G is routine, and every
            # broadcast lands in the feed history every client replays.
            print(f"[Dashboard] Android link up: {dev.get('name')}")
            try:
                while True:
                    msg = await websocket.receive_json()
                    t = msg.get("type") if isinstance(msg, dict) else None
                    if t == "result":
                        fut = self._device_pending.get(msg.get("id"))
                        if fut and not fut.done():
                            fut.set_result(msg)
                    elif t == "hello":
                        self._device_info.update({k: msg.get(k) for k in ("device", "caps", "version")})
                    elif t == "notification":
                        n = {k: str(msg.get(k, ""))[:500] for k in
                             ("app", "package", "title", "text", "key")}
                        n["can_reply"] = bool(msg.get("can_reply"))
                        n["at"] = time.time()
                        self.recent_notifications = (self.recent_notifications + [n])[-30:]
                        if self._notification_cb:
                            try:
                                self._notification_cb(n)
                            except Exception as e:
                                print(f"[Dashboard] notification callback: {e}")
                    elif t == "ping":
                        await websocket.send_json({"type": "pong"})
            except (WebSocketDisconnect, RuntimeError):
                pass
            except Exception as e:
                print(f"[Dashboard] device link error: {e}")
            finally:
                if self._device_ws is websocket:
                    self._device_ws = None
                for fut in list(self._device_pending.values()):
                    if not fut.done():
                        fut.set_result({"ok": False, "result": "the phone disconnected"})

        @app.websocket("/ws/device-audio")
        async def device_audio_ws(websocket: WebSocket):
            """The native app's voice link, used for Bluetooth and Android Auto
            (the WebView can only play while it is on screen). Authenticated
            by the device token, like /ws/device.

              phone → PC  binary: mic, 16 kHz 16-bit mono PCM
                          {"type":"say","text"}       a typed / car-screen command
                          {"type":"speaker","on"}     play replies here or not
                          {"type":"route","bt","name"} Bluetooth / car audio up?
                          {"type":"listen","on"}      mic opened / closed
              PC → phone  binary: the assistant's voice, 24 kHz 16-bit mono PCM
                          {"type":"flush"} {"type":"log",…} {"type":"status",…}
            """
            auth = websocket.headers.get("authorization", "")
            dev = self._app_device(auth.removeprefix("Bearer ").strip())
            if not dev:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._native_audio.add(websocket)
            try:
                while True:
                    m = await websocket.receive()
                    if m.get("type") == "websocket.disconnect":
                        break
                    data = m.get("bytes")
                    if data:
                        self.touch_phone()
                        try:
                            self._phone_audio_queue.put_nowait({"data": data, "mime_type": "audio/pcm"})
                        except asyncio.QueueFull:
                            pass
                        continue
                    try:
                        msg = json.loads(m.get("text") or "{}")
                    except ValueError:
                        continue
                    t = msg.get("type")
                    if t == "say":
                        text = str(msg.get("text", "")).strip()[:2000]
                        if text:
                            self.touch_phone()
                            await self._command_queue.put(text)
                            if self._wake_callback:
                                self._wake_callback()
                    elif t == "speaker":
                        if msg.get("on"):
                            self._speakers.add(websocket)
                        else:
                            self._speakers.discard(websocket)
                    elif t == "route":
                        bt = bool(msg.get("bt"))
                        if bt != self.phone_bt:
                            name = str(msg.get("name", ""))[:60]
                            print(f"[Dashboard] phone audio: {'Bluetooth/car ' + name if bt else 'own speaker'}")
                        self.phone_bt = bt
                    elif t == "listen":
                        if msg.get("on"):
                            self.touch_phone()
                            if self._wake_callback:
                                self._wake_callback()
                    elif t == "ping":
                        await websocket.send_json({"type": "pong"})
            except (WebSocketDisconnect, RuntimeError):
                pass
            except Exception as e:
                print(f"[Dashboard] device audio error: {e}")
            finally:
                self._native_audio.discard(websocket)
                self._speakers.discard(websocket)
                if not self._native_audio:
                    self.phone_bt = False

        @app.websocket("/ws/phone-speaker")
        async def phone_speaker_ws(websocket: WebSocket, token: str = ""):
            """The phone's speaker: receives the assistant's reply audio while
            the app is open. Nothing is read from it except keep-alives."""
            tok = token.strip()
            if not _origin_ok(websocket):
                await websocket.close(code=4003)
                return
            if not self._token_valid(tok):
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._ws_tokens[websocket] = tok
            self._speakers.add(websocket)
            try:
                while True:
                    await websocket.receive_text()          # pings from the app
                    if tok not in self._tokens:
                        await websocket.close(code=4001)
                        break
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                self._speakers.discard(websocket)
                self._ws_tokens.pop(websocket, None)

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        def _uploads_used() -> int:
            total = 0
            try:
                for p in self._uploads_dir.iterdir():
                    if p.is_file():
                        total += p.stat().st_size
            except Exception:
                pass
            return total

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                quota = UPLOAD_QUOTA_MB * 1024 * 1024
                used  = _uploads_used()
                if used >= quota:
                    return JSONResponse(
                        {"error": f"Upload folder full (limit {UPLOAD_QUOTA_MB} MB)"},
                        status_code=507,
                    )

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes or used + size > quota:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                if size > max_bytes:
                                    return JSONResponse(
                                        {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                        status_code=413,
                                    )
                                return JSONResponse(
                                    {"error": f"Upload folder full (limit {UPLOAD_QUOTA_MB} MB)"},
                                    status_code=507,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    print(f"[Dashboard] Upload of {safe!r} failed: {exc}")
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": "Upload failed"}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                # hand it to the assistant, like a file loaded on the PC
                cb = getattr(self, "_upload_cb", None)
                if callable(cb):
                    try:
                        cb(str(dest))
                    except Exception as exc:
                        print(f"[Dashboard] upload hand-off: {exc}")
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            # Auth via query param — browser <a download> can't send custom headers
            if not self._token_valid(token.strip()):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.get("/hud/img/{tok}")
        async def hud_image(tok: str, token: str = ""):
            # A hologram / picture card's image: only files the HUD showed
            # (core/hud_mirror hands out the tokens), only to signed-in screens.
            if not self._token_valid(token.strip()):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import hud_mirror
            path = hud_mirror.image_path(tok)
            if not path or not Path(path).is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not _origin_ok(websocket):
                await websocket.close(code=4003)
                return
            if not self._token_valid(tok):
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            self._ws_tokens[websocket] = tok
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:                          # the cards on the HUD right now
                from core import hud_mirror
                for entry in hud_mirror.snapshot():
                    await websocket.send_json(entry)
            except Exception:
                pass
            try:
                while True:
                    data = await websocket.receive_json()
                    if isinstance(data, dict) and data.get("type") == "control":
                        # the phone HUD's interrupt / mic buttons — signed-in screens only
                        act = str(data.get("action") or "")
                        cb = getattr(self, "_control_cb", None)
                        if self._token_valid(tok) and act in ("interrupt", "mute") and callable(cb):
                            cb(act)
                        continue
                    if isinstance(data, dict) and data.get("type") == "command":
                        if not self._token_valid(tok):   # expired or revoked
                            await websocket.close(code=4001)
                            break
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            self.touch_phone()
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except (WebSocketDisconnect, RuntimeError):
                pass              # RuntimeError: socket closed server-side (revoke/expiry)
            finally:
                self._clients.discard(websocket)
                self._ws_tokens.pop(websocket, None)

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning", log_config=None,
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting on subprocess timeouts. One call covers both ports.
        asyncio.get_event_loop().run_in_executor(
            None, _ensure_network_access, [PORT, PORT + 1])

        # Generate the TLS pair on first run so no private key ships in the repo.
        _ensure_certs()

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        # Tailscale's trusted certificate on the main port when available; the
        # alias port keeps the self-signed one for plain LAN-IP access.
        self._ts_host = ""
        ts = await asyncio.get_event_loop().run_in_executor(None, _tailscale_tls)
        if ts:
            self._ts_host, ts_crt, ts_key = ts
            ssl_cert, ssl_key, use_ssl = Path(ts_crt), Path(ts_key), True

        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            # log_config=None: uvicorn's own logging setup writes to a console
            # stream, and under pythonw.exe there is none — dictConfig raised
            # and the dashboard silently never started.
            self.app, host="0.0.0.0", port=PORT, log_level="warning", log_config=None,
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ts_host or self._ip}:{PORT}")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        await uvicorn.Server(cfg).serve()
