"""
Alexa through Amazon's own web API — the one the Alexa app uses — via alexapy
(the library behind Home Assistant's "Alexa Media Player").

Amazon publishes no API for controlling an Echo, so this is unofficial: it can
break when Amazon changes something, and the fix is usually a newer alexapy.

LOGIN, ONCE
    The user signs in on Amazon's real login page, opened in their own browser
    through a small local proxy (127.0.0.1:8891) that watches for the moment
    the login succeeds. The password and any 2-step code are typed by the user
    on Amazon's page; nothing here stores them. What is kept, in memory/alexa/,
    is the device registration Amazon issues (a refresh token), which renews
    the session silently from then on — like the Alexa app on a phone.

alexapy is asyncio/aiohttp; plugins run on worker threads. So this keeps one
event loop in a thread of its own and every call is handed to it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

BASE = Path(__file__).resolve().parents[2]
DIR = BASE / "memory" / "alexa"
STATE = DIR / "auth.json"
PROXY_PORT = 8891
LOGIN_TIMEOUT = 300

# Families that are speakers/screens you can talk through (not phones, not
# the Alexa app, not smart plugs). WHA = a multi-room speaker group.
SPEAKER_FAMILIES = {"ECHO", "KNIGHT", "ROOK", "WHA", "TABLET", "FIRE_TV", "THIRD_PARTY_AVS_MEDIA_DISPLAY"}


class AlexaClient:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock = threading.Lock()
        self.login = None
        self._devices: list[dict] = []
        self._devices_at = 0.0
        self._login_flow: dict | None = None     # a proxy login in progress
        self.last_error = ""

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, daemon=True,
                                 name="alexa-loop").start()
            return self._loop

    def call(self, coro, timeout: float = 30):
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result(timeout)

    @staticmethod
    def _output(name: str) -> str:
        p = DIR / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)

    @staticmethod
    def _load_state() -> dict:
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self) -> None:
        lg = self.login
        DIR.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({
            "email": lg.email, "domain": lg.url, "uuid": lg.uuid,
            "oauth": {"access_token": lg.access_token, "refresh_token": lg.refresh_token,
                      "mac_dms": lg.mac_dms, "expires_in": lg.expires_in,
                      "code_verifier": lg.code_verifier, "code_challenge": lg.code_challenge},
        }), encoding="utf-8")

    def _new_login(self, email: str, domain: str, oauth: dict | None = None, uuid: str | None = None):
        from alexapy import AlexaLogin
        return AlexaLogin(url=domain, email=email, password="", outputpath=self._output,
                          oauth=oauth or {}, uuid=uuid, oauth_login=True)

    # ── session ──────────────────────────────────────────────────────────────

    def linked(self) -> bool:
        return bool(self._load_state().get("oauth", {}).get("refresh_token"))

    def ensure(self) -> str | None:
        """Logged in (restoring the saved registration if needed). Returns an
        error sentence, or None when ready."""
        if self.login is not None:
            return None
        st = self._load_state()
        if not st.get("oauth", {}).get("refresh_token"):
            return ("Alexa is not linked yet — press CONNECT in ⚙ → plugin settings → Alexa "
                    "(or say 'connect Alexa') and sign in to Amazon in the browser.")
        try:
            ok = self.call(self._restore(st), timeout=45)
        except Exception as e:
            ok, self.last_error = False, f"{type(e).__name__}: {e}"
        if not ok:
            return ("Could not open the Alexa session — the Amazon link may have expired. "
                    "Press CONNECT in ⚙ → Alexa to sign in again." +
                    (f" ({self.last_error[:120]})" if self.last_error else ""))
        return None

    async def _restore(self, st: dict) -> bool:
        # The refresh-token path only — never the password path: with no
        # password stored that would just poke Amazon's login form.
        lg = self._new_login(st["email"], st.get("domain", "amazon.com.br"),
                             oauth=st.get("oauth"), uuid=st.get("uuid"))
        ok = (await lg.refresh_access_token()
              and await lg.exchange_token_for_cookies()
              and await lg.get_csrf()
              and await lg.test_loggedin(rebuild_session=False))
        if not ok:
            await lg.close()
            return False
        self.login = lg
        self._save_state()                      # Amazon may rotate the tokens
        return True

    def logout(self) -> None:
        if self.login is not None:
            try:
                self.call(self.login.close(), timeout=10)
            except Exception:
                pass
        self.login = None
        self._devices = []
        for f in DIR.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass

    # ── one-time login through the proxy ─────────────────────────────────────

    def start_login(self, email: str, domain: str) -> tuple[bool, str]:
        """Open Amazon's sign-in page (through the local proxy) in the browser
        and wait up to 5 minutes for the user to finish it."""
        if self._login_flow:
            return False, "An Amazon sign-in is already open in the browser — finish it there."
        if not email or "@" not in email:
            return False, "Fill in the e-mail of your Amazon account first."
        self._login_flow = {"done": threading.Event(), "ok": False, "msg": ""}
        try:
            url = self.call(self._open_proxy(email, domain), timeout=30)
        except Exception as e:
            self._login_flow = None
            return False, f"Could not start the sign-in page: {type(e).__name__}: {e}"
        webbrowser.open(url)
        flow = self._login_flow
        flow["done"].wait(LOGIN_TIMEOUT)
        try:
            self.call(self._close_proxy(), timeout=15)
        except Exception:
            pass
        self._login_flow = None
        if not flow["done"].is_set():
            return False, "No sign-in within 5 minutes. Press CONNECT to try again."
        return flow["ok"], flow["msg"]

    async def _open_proxy(self, email: str, domain: str) -> str:
        from alexapy import AlexaProxy
        from yarl import URL
        lg = self._new_login(email, domain, uuid=uuid4().hex.upper())
        proxy = AlexaProxy(lg, f"http://127.0.0.1:{PROXY_PORT}/")
        original = proxy.test_amazon_url
        flow = self._login_flow

        async def watch(resp, data, query):
            landed = URL(str(resp.url)).path in ("/ap/maplanding", "/spa/index.html")
            out = await original(resp, data, query)
            if landed:
                asyncio.get_running_loop().create_task(self._finish_login(lg, flow))
                return ("<h2 style='font-family:sans-serif'>Pronto — conta Amazon ligada ao "
                        "MARK. Pode fechar esta janela.</h2>")
            return out

        proxy.tests = {"test_amazon_url": watch}
        await proxy.start_proxy(host="127.0.0.1")
        flow["proxy"] = proxy
        return str(proxy.access_url())

    async def _close_proxy(self):
        proxy = (self._login_flow or {}).get("proxy")
        if proxy is not None:
            await proxy.stop_proxy()

    async def _finish_login(self, lg, flow: dict) -> None:
        try:
            ok = await lg.test_loggedin()          # tokens, device registration, cookies
            if ok:
                self.login = lg
                self._save_state()
                n = len(await self._fetch_devices())
                flow["ok"], flow["msg"] = True, f"Alexa linked ({lg.email}); found {n} Echo devices."
            else:
                flow["msg"] = "Amazon accepted the sign-in but the Alexa session could not be confirmed."
        except Exception as e:
            flow["msg"] = f"Finishing the Alexa sign-in failed: {type(e).__name__}: {e}"
        flow["done"].set()

    # ── devices ──────────────────────────────────────────────────────────────

    async def _fetch_devices(self) -> list[dict]:
        from alexapy import AlexaAPI
        raw = await AlexaAPI.get_devices(self.login) or []
        self._devices = [d for d in raw if d.get("deviceFamily") in SPEAKER_FAMILIES]
        self._devices_at = time.monotonic()
        return self._devices

    def devices(self, refresh: bool = False) -> list[dict]:
        if refresh or not self._devices or time.monotonic() - self._devices_at > 600:
            self.call(self._fetch_devices())
        return self._devices

    def find(self, name: str = "") -> dict | str:
        """A spoken Echo name → its device record (or a sentence why not).
        No name: the only Echo, or the first one online."""
        devs = self.devices()
        if not devs:
            return "No Echo devices were found on this Amazon account."
        q = (name or "").strip().lower()
        if not q:
            online = [d for d in devs if d.get("online") and d.get("deviceFamily") != "WHA"]
            return (online or devs)[0]
        for d in devs:
            if d.get("accountName", "").lower() == q:
                return d
        hits = [d for d in devs if q in d.get("accountName", "").lower()]
        if len(hits) == 1:
            return hits[0]
        names = ", ".join(d.get("accountName", "?") for d in devs)
        return f"{'Which one' if hits else 'No Echo called ' + repr(name)}? I know: {names}."

    def api(self, dev: dict):
        from alexapy import AlexaAPI
        shim = SimpleNamespace(
            _device_type=dev.get("deviceType"),
            device_serial_number=dev.get("serialNumber"),
            _device_serial_number=dev.get("serialNumber"),
            _device_family=dev.get("deviceFamily"),
            _cluster_members=dev.get("clusterMembers") or [],
            _locale=dev.get("locale") or "pt-BR",
            _device_owner_customer_id=dev.get("deviceOwnerCustomerId"),
            _customer_id=dev.get("deviceOwnerCustomerId"),
            name=dev.get("accountName"),
        )
        return AlexaAPI(shim, self.login)


CLIENT = AlexaClient()
