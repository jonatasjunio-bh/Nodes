"""
Broadlink over the LAN, through python-broadlink. No cloud, no keys: the
devices do a local handshake and that is all.

  RM mini / RM4 mini / RM4 Pro …  universal remotes. They LEARN a button from
        any infrared remote (and, on the Pro models, 433 MHz RF remotes) and
        replay it later — which is how an air conditioner, a fan or an old TV
        that has no network of its own becomes voice-controlled.
  SP plugs                          on / off / state.
  LB bulbs                          on / off / brightness / colour.

Learned codes are stored per device in memory/smart_home.json under the name
the user gave them ("ligar ar", "ventilador velocidade 2").

A device that was set up with "Lock device" on in the Broadlink app refuses
every local command; the error says how to turn that off.
"""

from __future__ import annotations

import base64
import time


def available() -> bool:
    try:
        import broadlink  # noqa: F401
        return True
    except ImportError:
        return False


def capabilities(dev_type: str) -> dict:
    t = (dev_type or "").upper()
    return {
        "ir": t.startswith("RM"),
        "rf": t in ("RMPRO", "RM4PRO"),
        "plug": t.startswith("SP"),
        "bulb": t.startswith("LB"),
    }


def label(dev_type: str, model: str = "") -> str:
    c = capabilities(dev_type)
    what = ("IR/RF remote" if c["rf"] else "IR remote" if c["ir"] else
            "smart plug" if c["plug"] else "smart bulb" if c["bulb"] else "device")
    return f"Broadlink {model or dev_type} ({what})"


def discover(local_ip: str, timeout: int = 4) -> dict[str, dict]:
    """ip → {"type", "model", "devtype", "mac", "locked"}."""
    if not available():
        return {}
    import broadlink
    found = {}
    try:
        for d in broadlink.discover(timeout=timeout, local_ip_address=local_ip):
            found[d.host[0]] = {
                "type": d.type, "model": getattr(d, "model", "") or d.type,
                "devtype": d.devtype, "mac": ":".join(f"{b:02X}" for b in d.mac),
                "locked": bool(getattr(d, "is_locked", False)),
            }
    except Exception as e:
        print(f"[SmartHome] Broadlink discovery failed: {e}")
    return found


def probe(ips: list[str], timeout: float = 1.5) -> dict[str, dict]:
    """Ask each address directly. Some routers drop broadcast between Wi-Fi
    clients, and then discover() hears nothing even with a Broadlink sitting
    right there — measured on this network: an RM4 Pro that never answered the
    broadcast answered a unicast hello at once."""
    if not available() or not ips:
        return {}
    import broadlink
    from concurrent.futures import ThreadPoolExecutor

    def one(ip):
        try:
            d = broadlink.hello(ip, timeout=timeout)
        except Exception:
            return ip, None
        return ip, {"type": d.type, "model": getattr(d, "model", "") or d.type,
                    "devtype": d.devtype, "mac": ":".join(f"{b:02X}" for b in d.mac),
                    "locked": bool(getattr(d, "is_locked", False))}

    with ThreadPoolExecutor(max_workers=16) as ex:
        return {ip: info for ip, info in ex.map(one, ips) if info}


def _connect(dev: dict):
    import broadlink
    try:
        d = broadlink.hello(dev["ip"], timeout=3)
    except Exception as e:
        raise ConnectionError(f"it is not answering at {dev['ip']} — re-scan if its IP changed") from e
    if getattr(d, "is_locked", False):
        raise PermissionError("it is locked in the Broadlink app — open the device there, "
                              "Properties → turn off 'Lock device', then try again")
    d.auth()
    return d


def _call(dev: dict, fn):
    """(error, result). Every failure becomes a sentence."""
    if not available():
        return "Broadlink control needs the 'broadlink' package (pip install broadlink)", None
    try:
        return "", fn(_connect(dev))
    except (ConnectionError, PermissionError) as e:
        return str(e), None
    except Exception as e:
        name = type(e).__name__
        if "Authentication" in name or "Authorization" in name:
            return ("it refused the handshake — if it is locked in the Broadlink app, turn "
                    "'Lock device' off"), None
        return f"{name}: {e}", None


# ── learning and sending ─────────────────────────────────────────────────────

LAST_POLL_ERRORS: dict[str, int] = {}     # what check_data raised during the last learn


def _poll_data(d, seconds: float, rearm=None, interval: float = 1.0):
    """Wait for a learned code. "No data yet" is raised, not returned, so every
    exception is counted by type: if the window closes with only the usual
    no-data error, nobody pressed; anything else points at the device."""
    LAST_POLL_ERRORS.clear()
    end = time.time() + seconds
    armed_at = time.time()
    while time.time() < end:
        # Once a second, as Home Assistant does: polling faster has been
        # reported to disturb a capture in progress.
        time.sleep(interval)
        # The device leaves learning mode on its own after ~30 s and from then
        # on answers "no data" forever, so a longer window must re-arm it.
        if rearm and time.time() - armed_at > 25:
            try:
                rearm()
                LAST_POLL_ERRORS["rearmed"] = LAST_POLL_ERRORS.get("rearmed", 0) + 1
            except Exception:
                pass
            armed_at = time.time()
        try:
            data = d.check_data()
            if data:
                return data
        except Exception as e:
            k = type(e).__name__
            LAST_POLL_ERRORS[k] = LAST_POLL_ERRORS.get(k, 0) + 1
    return None


DEFAULT_RF_MHZ = 433.92     # the usual frequency for gates, blinds and fans in Brazil
LAST_RF_MHZ: float | None = None   # what the last RF learn locked on


def learn(dev: dict, signal: str = "ir", say=None, seconds: float = 25,
          frequency: float | None = DEFAULT_RF_MHZ) -> tuple[str, str]:
    """Learn one button. Returns (error, base64 code). `say(text)` tells the
    user what to do at each step.

    RF with a known frequency listens on it directly. The automatic sweep is
    used only when frequency is None: it is the unreliable part of RF learning
    (it needs the button held for seconds and still misses short bursts), and
    a 433.92 MHz remote that would not sweep was learned at once by the app,
    which tunes straight to the frequency."""
    caps = capabilities(dev.get("broadlink", {}).get("type", ""))
    if signal == "rf" and not caps["rf"]:
        return "this Broadlink only learns infrared (RF needs an RM Pro / RM4 Pro)", ""
    if not caps["ir"]:
        return "this Broadlink is not a remote, so it cannot learn buttons", ""

    def go(d):
        if signal == "rf":
            # Stage 1 — sweep. The user HOLDS the button; the device scans until
            # it locks on the carrier. On the RM4 Pro this is what tunes the
            # receiver: jumping straight to capture with a frequency is only
            # honoured by some models, and that is how a 433.92 MHz remote
            # "listened" for minutes and never landed.
            global LAST_RF_MHZ
            d.sweep_frequency()
            if say:
                say("Step 1: hold the remote's button down, close to the Broadlink, "
                    "and keep holding until I say.")
            found = None
            end = time.time() + 25
            while time.time() < end:
                time.sleep(1)
                try:
                    ok, mhz = d.check_frequency()
                    if ok:
                        found = mhz
                        break
                except Exception:
                    continue
            if found:
                d.find_rf_packet()                     # stay on the frequency it locked
            elif frequency:
                try:
                    d.cancel_sweep_frequency()
                except Exception:
                    pass
                found = frequency                      # fall back to the known one
                d.find_rf_packet(frequency)
            else:
                try:
                    d.cancel_sweep_frequency()
                except Exception:
                    pass
                raise TimeoutError("no RF signal found — hold the button longer, right next "
                                   "to the Broadlink")
            LAST_RF_MHZ = found
            # Stage 2 — capture. No re-arming here: re-sending the command in
            # the middle of a press throws the half-captured packet away.
            if say:
                say(f"Step 2: got {found:g} MHz. Let go, then press the button briefly, "
                    f"a few times, a second apart.")
            data = _poll_data(d, 30)
            if not data:
                raise TimeoutError(f"the frequency was found ({found:g} MHz) but no button "
                                   f"press was captured — try again with short presses")
            return base64.b64encode(data).decode()
        else:
            d.enter_learning()
            if say:
                say("Point the remote at the Broadlink and press the button once.")
        data = _poll_data(d, seconds, rearm=None if signal == "rf" else d.enter_learning)
        if not data:
            raise TimeoutError("no button press received in time")
        return base64.b64encode(data).decode()

    err, code = _call(dev, go)
    if err and "TimeoutError" in err:
        err = err.split(": ", 1)[-1]
    return err, code or ""


def send(dev: dict, code_b64: str, repeat: int = 1) -> str:
    raw = base64.b64decode(code_b64)

    def go(d):
        for i in range(max(1, min(repeat, 10))):
            if i:
                time.sleep(0.3)
            d.send_data(raw)
    return _call(dev, go)[0]


# ── plugs and bulbs ──────────────────────────────────────────────────────────

def set_power(dev: dict, on: bool) -> str:
    caps = capabilities(dev.get("broadlink", {}).get("type", ""))
    if caps["plug"]:
        return _call(dev, lambda d: d.set_power(on))[0]
    if caps["bulb"]:
        return _call(dev, lambda d: d.set_state(pwr=on))[0]
    return ("a Broadlink remote has no power of its own — teach it the appliance's "
            "on/off buttons and send those")


def power_state(dev: dict) -> tuple[str, bool | None]:
    caps = capabilities(dev.get("broadlink", {}).get("type", ""))
    if caps["plug"]:
        return _call(dev, lambda d: bool(d.check_power()))
    if caps["bulb"]:
        err, st = _call(dev, lambda d: d.get_state())
        return err, (bool(st.get("pwr")) if isinstance(st, dict) else None)
    return "it does not report a power state", None


def set_brightness(dev: dict, pct: int) -> str:
    if not capabilities(dev.get("broadlink", {}).get("type", ""))["bulb"]:
        return "only Broadlink bulbs have brightness"
    return _call(dev, lambda d: d.set_state(pwr=True, brightness=max(1, min(100, pct))))[0]


def set_color(dev: dict, hexv: str) -> str:
    if not capabilities(dev.get("broadlink", {}).get("type", ""))["bulb"]:
        return "only Broadlink bulbs have colour"
    r, g, b = (int(hexv[i:i + 2], 16) for i in (0, 2, 4))
    return _call(dev, lambda d: d.set_state(pwr=True, red=r, green=g, blue=b, bulb_colormode=0))[0]
