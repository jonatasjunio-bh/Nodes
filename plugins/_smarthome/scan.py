"""
Who is on this Wi-Fi? Local network discovery with no extra packages.

How a scan works, cheapest step first:

  1. Find this machine's subnet (capped at 1024 addresses, so a /16 office
     network never turns into a 65,000-host sweep).
  2. Send one empty UDP datagram to every address. Nobody has to answer: to
     send it at all, the OS first asks "who has this IP?" over ARP, and every
     device on the LAN answers ARP, firewall or not. One socket, no ping
     process per host.
  3. Read the OS's ARP table: every device that answered, with its MAC.
  4. Ask the devices to describe themselves, in parallel:
       mDNS  (_ewelink = Sonoff, _amzn-wplay = Echo, _googlecast = Chromecast…)
       SSDP  (LG webOS TVs announce "webos" and an LG service type)
       control ports (Sonoff LAN 8081, LG TV 3000/3001, Tasmota HTTP 80)
       reverse DNS for the hostname the router knows
  5. Classify each device from what it said, falling back to the MAC's maker.

Only devices on the user's own network are touched, and only with discovery
traffic that phones and smart speakers send all day anyway.
"""

from __future__ import annotations

import ipaddress
import json
import platform
import re
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from . import broadlinkctl, oui, tuya

_OS = platform.system()
_MAX_HOSTS = 1024
_CNW = {"creationflags": subprocess.CREATE_NO_WINDOW} if _OS == "Windows" else {}

# ── where are we ─────────────────────────────────────────────────────────────

def local_ip() -> str:
    """The address this machine uses to reach the outside world. Connecting
    a UDP socket sends nothing; it only makes the OS pick a route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def local_network(ip: str) -> ipaddress.IPv4Network:
    prefix = 24
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and a.address == ip and a.netmask:
                    prefix = ipaddress.IPv4Network(f"0.0.0.0/{a.netmask}").prefixlen
    except Exception:
        pass
    net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
    if net.num_addresses > _MAX_HOSTS:
        net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
    return net


def gateway() -> str:
    try:
        if _OS == "Windows":
            out = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True,
                                 text=True, timeout=4, **_CNW).stdout
            m = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)", out, re.M)
        elif _OS == "Darwin":
            out = subprocess.run(["route", "-n", "get", "default"], capture_output=True,
                                 text=True, timeout=4).stdout
            m = re.search(r"gateway:\s*(\d+\.\d+\.\d+\.\d+)", out)
        else:
            out = subprocess.run(["ip", "route", "show", "default"], capture_output=True,
                                 text=True, timeout=4).stdout
            m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else ""
    except Exception:
        return ""

# ── who answered ARP ─────────────────────────────────────────────────────────

def _nudge(net: ipaddress.IPv4Network, me: str) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        for host in net.hosts():
            h = str(host)
            if h == me:
                continue
            try:
                s.sendto(b"", (h, 9))      # port 9 = discard
            except (BlockingIOError, OSError):
                time.sleep(0.002)
    finally:
        s.close()


def _is_unicast(mac: str) -> bool:
    return mac not in ("FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00") and not (
        int(mac[:2], 16) & 0x1)


def arp_table(me: str, net: ipaddress.IPv4Network) -> dict[str, str]:
    """ip → MAC for every neighbour the OS currently knows on our subnet."""
    pairs: list[tuple[str, str]] = []
    try:
        if _OS == "Windows":
            out = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                                 timeout=6, **_CNW).stdout
            section = None
            for line in out.splitlines():
                m = re.match(r"\S+:\s*(\d+\.\d+\.\d+\.\d+)\s+---", line.strip())
                if m:
                    section = m.group(1)
                    continue
                m = re.match(r"\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})", line)
                if m and section == me:
                    pairs.append((m.group(1), m.group(2)))
        elif _OS == "Linux":
            with open("/proc/net/arp", encoding="ascii") as f:
                for line in f.readlines()[1:]:
                    cols = line.split()
                    if len(cols) >= 4 and cols[2] != "0x0":
                        pairs.append((cols[0], cols[3]))
        else:
            out = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=6).stdout
            for m in re.finditer(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]+)", out):
                pairs.append((m.group(1), m.group(2)))
    except Exception as e:
        print(f"[SmartHome] ARP table unavailable: {e}")

    table = {}
    for ip, mac in pairs:
        mac = oui.normalize_mac(mac)
        try:
            if ipaddress.IPv4Address(ip) in net and _is_unicast(mac):
                table[ip] = mac
        except ValueError:
            continue
    return table


def quick_scan() -> dict[str, str]:
    """ARP only, a couple of seconds. What the new-device watcher uses."""
    me = local_ip()
    net = local_network(me)
    _nudge(net, me)
    time.sleep(1.5)
    return arp_table(me, net)

# ── mDNS (RFC 6762), just enough to ask "what services do you have?" ─────────

MDNS_SERVICES = (
    "_ewelink._tcp.local",        # Sonoff / eWeLink LAN mode and DIY mode
    "_amzn-wplay._tcp.local",     # Amazon Echo
    "_amzn-alexa._tcp.local",
    "_googlecast._tcp.local",     # Chromecast, Google/Nest speakers, Android TV
    "_airplay._tcp.local",        # Apple TV, many smart TVs (LG included)
    "_hap._tcp.local",            # HomeKit accessories
    "_spotify-connect._tcp.local",
    "_http._tcp.local",
    "_ipp._tcp.local",            # printers
)


def _qname(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"


def _read_name(buf: bytes, off: int) -> tuple[str, int]:
    labels, jumped, end = [], False, off
    for _ in range(64):                       # guards against pointer loops
        n = buf[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = ((n & 0x3F) << 8) | buf[off + 1]
            if not jumped:
                end = off + 2
            jumped, off = True, ptr
            continue
        labels.append(buf[off + 1: off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n
    return ".".join(labels), (end if jumped else off)


def _parse_mdns(buf: bytes) -> list[tuple[str, int, object]]:
    """(name, type, value) for every answer/additional record."""
    _id, _flags, qd, an, ns, ar = struct.unpack("!6H", buf[:12])
    off = 12
    for _ in range(qd):
        _, off = _read_name(buf, off)
        off += 4
    out = []
    for _ in range(an + ns + ar):
        name, off = _read_name(buf, off)
        rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", buf[off:off + 10])
        off += 10
        rdata = buf[off:off + rdlen]
        if rtype == 12:                                   # PTR
            val, _ = _read_name(buf, off)
        elif rtype == 16:                                 # TXT
            val, i = {}, 0
            while i < len(rdata):
                ln = rdata[i]
                item = rdata[i + 1:i + 1 + ln].decode("utf-8", "replace")
                k, _, v = item.partition("=")
                val[k] = v
                i += 1 + ln
        elif rtype == 33:                                 # SRV
            port = struct.unpack("!H", rdata[4:6])[0]
            target, _ = _read_name(buf, off + 6)
            val = (target, port)
        elif rtype == 1 and rdlen == 4:                   # A
            val = socket.inet_ntoa(rdata)
        else:
            val = None
        out.append((name, rtype, val))
        off += rdlen
    return out


def _absorb(d: dict, records) -> None:
    for name, rtype, val in records:
        svc = next((x for x in MDNS_SERVICES if name.endswith(x)), None)
        if rtype == 12 and isinstance(val, str):
            svc = next((x for x in MDNS_SERVICES if val.endswith(x) or name == x), None)
            if svc:
                d["services"].add(svc.split(".")[0])
                inst = val[: -len(svc) - 1] if val.endswith(svc) else ""
                if inst:
                    d["names"].add(inst)
        elif rtype == 16 and svc and isinstance(val, dict):
            d["txt"][svc.split(".")[0]] = val


def mdns_browse(timeout: float = 2.5) -> dict[str, dict]:
    """ip → {"services": set, "names": set, "txt": {service: dict}}.
    Asked from an ordinary port, so responders answer us directly (RFC 6762
    §6.7) and nothing has to bind 5353, which Windows itself often holds."""
    pkt = struct.pack("!6H", 0, 0, len(MDNS_SERVICES), 0, 0, 0)
    pkt += b"".join(_qname(s) + struct.pack("!HH", 12, 1) for s in MDNS_SERVICES)
    found: dict[str, dict] = {}

    # Two sockets. The ephemeral one gets the direct (unicast) replies that
    # full mDNS stacks send to a "legacy" asker. Many small embedded stacks —
    # the ESP firmware in Sonoff, Echo — ignore that and only ever answer to
    # the group, so a second socket joins 224.0.0.251:5353 to hear those.
    socks = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    socks.append(s)
    try:
        g = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        g.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        g.bind(("", 5353))
        iface = socket.inet_aton(local_ip())
        g.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton("224.0.0.251") + iface)
        g.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
        g.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        socks.append(g)
    except OSError as e:
        print(f"[SmartHome] mDNS group listener unavailable ({e}) — unicast replies only")

    import select
    try:
        for _ in range(2):                     # one retry: multicast gets dropped
            for sk in socks:
                sk.sendto(pkt, ("224.0.0.251", 5353))
            time.sleep(0.05)
        end = time.time() + timeout
        while time.time() < end:
            ready, _, _ = select.select(socks, [], [], 0.3)
            for sk in ready:
                try:
                    data, (src, _p) = sk.recvfrom(9000)
                except OSError:
                    continue
                if len(data) < 12 or data[2] & 0x80 == 0:   # a query, not an answer
                    continue
                try:
                    records = _parse_mdns(data)
                except Exception:
                    continue
                _absorb(found.setdefault(src, {"services": set(), "names": set(), "txt": {}}),
                        records)
    finally:
        for sk in socks:
            sk.close()
    return found

# ── SSDP / UPnP ──────────────────────────────────────────────────────────────

def ssdp_search(timeout: float = 2.5) -> dict[str, dict]:
    """ip → {"server": str, "st": set, "location": str}."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n').encode()
    found: dict[str, dict] = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.3)
        for _ in range(2):
            s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, (src, _p) = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            hdr = {}
            for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
                k, _, v = line.partition(":")
                hdr[k.strip().lower()] = v.strip()
            d = found.setdefault(src, {"server": "", "st": set(), "location": ""})
            d["server"] = d["server"] or hdr.get("server", "")
            d["location"] = d["location"] or hdr.get("location", "")
            if hdr.get("st"):
                d["st"].add(hdr["st"])
    finally:
        s.close()
    return found


def _friendly_name(location: str) -> str:
    try:
        import requests
        xml = requests.get(location, timeout=1.5).text
        m = re.search(r"<friendlyName>([^<]{1,80})</friendlyName>", xml)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""

# ── probes ───────────────────────────────────────────────────────────────────

PROBE_PORTS = (80, 3000, 3001, 8081)


def _open(ip: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tasmota_status(ip: str) -> dict | None:
    try:
        import requests
        r = requests.get(f"http://{ip}/cm", params={"cmnd": "Status"}, timeout=1.2)
        data = r.json()
        st = data.get("Status") if isinstance(data, dict) else None
        return st if isinstance(st, dict) else None
    except Exception:
        return None


def _reverse_dns(ip: str) -> str:
    try:
        name = socket.gethostbyaddr(ip)[0]
        return name.split(".")[0] if name and name != ip else ""
    except Exception:
        return ""

# ── the full scan ────────────────────────────────────────────────────────────

def _classify(dev: dict) -> tuple[str, str]:
    """(kind, label) from what the device said about itself."""
    svc = dev["services"]
    server = dev.get("ssdp_server", "").lower()
    st = " ".join(dev.get("ssdp_st", [])).lower()
    ports = dev["ports"]
    vend = dev["vendor"]
    if dev.get("is_self"):
        return "computer", "this computer"
    if dev.get("tuya"):
        return "tuya", "i2GO/Tuya device"
    if dev.get("broadlink"):
        b = dev["broadlink"]
        return "broadlink", broadlinkctl.label(b.get("type", ""), b.get("model", ""))
    name = (dev.get("name") or "").lower()
    # 8081 is the eWeLink LAN port; nothing else on a home network uses it,
    # and new Espressif batches appear faster than any OUI table can follow.
    if "_ewelink" in svc or 8081 in ports:
        return "sonoff", "Sonoff (eWeLink)"
    if dev.get("tasmota"):
        return "tasmota", "Tasmota switch"
    if "webos" in server or "lge-com" in st or "webos" in name or (
            (vend == "LG Electronics" or "[lg]" in name) and (3000 in ports or 3001 in ports)):
        return "lg_tv", "LG webOS TV"
    if "_amzn-wplay" in svc or "_amzn-alexa" in svc or vend.startswith("Amazon"):
        return "alexa", "Amazon Echo (Alexa)"
    if "_googlecast" in svc:
        return "chromecast", "Google Cast device"
    if dev.get("is_gateway"):
        return "router", "Wi-Fi router"
    if "_ipp" in svc:
        return "printer", "printer"
    if vend.startswith("Espressif"):
        return "smart_switch", "smart switch/plug (ESP)"
    if vend == "LG Electronics":
        return "lg", "LG device"
    if oui.is_random(dev["mac"]):
        return "phone_or_pc", "phone / laptop (private MAC)"
    return "unknown", vend or "unknown device"


def _best_name(m: dict) -> str:
    """The most human of the names a device announced: Cast's own "friendly
    name" first, then the shortest instance name without a long hex id."""
    fn = m.get("txt", {}).get("_googlecast", {}).get("fn")
    if fn:
        return fn
    names = sorted(m.get("names", []), key=lambda n: (bool(re.search(r"[0-9a-f]{16}", n)), len(n)))
    return names[0] if names else ""


def full_scan(progress=None) -> list[dict]:
    t0 = time.time()
    me = local_ip()
    net = local_network(me)
    gw = gateway()
    if progress:
        progress(f"Scanning {net} …")

    # Tuya / i2GO devices announce themselves every ~5 s; listen for the whole
    # scan instead of adding a wait of its own.
    tuya_found: dict = {}
    t_tuya = threading.Thread(target=lambda: tuya_found.update(tuya.listen(6.0)),
                              daemon=True)
    t_tuya.start()
    # Broadlink answers its own discovery broadcast; run it alongside too.
    bl_found: dict = {}
    t_bl = threading.Thread(target=lambda: bl_found.update(broadlinkctl.discover(me, 4)),
                            daemon=True)
    t_bl.start()

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_mdns = ex.submit(mdns_browse)
        f_ssdp = ex.submit(ssdp_search)
        _nudge(net, me)
        time.sleep(1.5)
        table = arp_table(me, net)
        mdns, ssdp = f_mdns.result(), f_ssdp.result()

    # mDNS/SSDP responders the ARP table missed (it can lag behind).
    for ip in set(mdns) | set(ssdp):
        if ip not in table and ip != me:
            table.setdefault(ip, "")
    if table and any(not m for m in table.values()):
        table.update({k: v for k, v in arp_table(me, net).items() if k in table})

    try:
        import uuid
        my_mac = ":".join(f"{(uuid.getnode() >> s) & 0xFF:02X}" for s in range(40, -1, -8))
    except Exception:
        my_mac = ""
    table[me] = my_mac

    devices = []
    for ip, mac in table.items():
        m = mdns.get(ip, {})
        s = ssdp.get(ip, {})
        devices.append({
            "ip": ip, "mac": mac, "vendor": oui.vendor(mac) if mac else "",
            "hostname": "", "name": "" if ip == me else _best_name(m),
            "services": sorted(m.get("services", [])),
            "txt": m.get("txt", {}),
            "ssdp_server": s.get("server", ""), "ssdp_st": sorted(s.get("st", [])),
            "ssdp_location": s.get("location", ""),
            "ports": [], "is_self": ip == me, "is_gateway": ip == gw,
        })

    # Probes and names, all in parallel, with one overall deadline so a host
    # that never answers cannot hold up the result.
    others = [d for d in devices if not d["is_self"]]
    # Not a `with` block: leaving one waits for every future, which would undo
    # the deadline. Stragglers are abandoned instead.
    ex = ThreadPoolExecutor(max_workers=64)
    try:
        port_f = {ex.submit(_open, d["ip"], p): (d, p) for d in others for p in PROBE_PORTS}
        dns_f = {ex.submit(_reverse_dns, d["ip"]): d for d in devices}
        loc_f = {ex.submit(_friendly_name, d["ssdp_location"]): d
                 for d in others if d["ssdp_location"]}
        wait(list(port_f) + list(dns_f) + list(loc_f), timeout=6)
        for f, (d, p) in port_f.items():
            if f.done() and f.result():
                d["ports"].append(p)
        for f, d in dns_f.items():
            if f.done():
                d["hostname"] = f.result()
        for f, d in loc_f.items():
            if f.done() and f.result() and not d["name"]:
                d["name"] = f.result()
        tas_f = {ex.submit(_tasmota_status, d["ip"]): d for d in others
                 if 80 in d["ports"] and not d["is_gateway"]
                 and not oui.is_random(d["mac"] or "02")}
        wait(list(tas_f), timeout=3)
        for f, d in tas_f.items():
            st = f.result() if f.done() else None
            if st:
                d["tasmota"] = True
                fn = st.get("FriendlyName")
                d["name"] = d["name"] or (fn[0] if isinstance(fn, list) and fn else
                                          st.get("DeviceName", ""))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    # The Tuya listener ran through all of the above; collect it last so it
    # costs no time of its own. A device heard only there joins the list now.
    t_tuya.join(timeout=7)
    t_bl.join(timeout=6)
    known = {d["ip"] for d in devices}
    for ip, info in list(tuya_found.items()) + list(bl_found.items()):
        if ip not in known and ip != me:
            known.add(ip)
            mac = arp_table(me, net).get(ip, "") or info.get("mac", "")
            devices.append({"ip": ip, "mac": mac, "vendor": oui.vendor(mac) if mac else "",
                            "hostname": "", "name": "", "services": [], "txt": {},
                            "ssdp_server": "", "ssdp_st": [], "ssdp_location": "",
                            "ports": [], "is_self": False, "is_gateway": False})
    # Unicast fallback for Broadlink: anything still unidentified (and not a
    # phone or a known kind) gets a direct hello — see broadlinkctl.probe.
    pending = [d["ip"] for d in devices
               if not d["is_self"] and not d["is_gateway"]
               and d["ip"] not in bl_found and d["ip"] not in tuya_found
               and not d["services"] and not d.get("tasmota")
               and not (d["mac"] and oui.is_random(d["mac"]))
               and not d["vendor"].startswith(("Amazon", "Google", "LG"))]
    bl_found.update(broadlinkctl.probe(pending))

    for d in devices:
        if d["ip"] in tuya_found:
            d["tuya"] = tuya_found[d["ip"]]
        if d["ip"] in bl_found:
            d["broadlink"] = bl_found[d["ip"]]
        d["kind"], d["label"] = _classify(d)
        ew = d["txt"].get("_ewelink")
        if ew:
            d["sonoff"] = {"id": ew.get("id", ""), "type": ew.get("type", ""),
                           "encrypt": str(ew.get("encrypt", "")).lower() == "true",
                           "port": 8081}
            # Unencrypted (DIY mode) announcements carry the current state.
            if not d["sonoff"]["encrypt"]:
                try:
                    data = json.loads("".join(ew.get(f"data{i}", "") for i in range(1, 5)))
                    d["sonoff"]["state"] = data
                except Exception:
                    pass
        d.pop("txt", None)

    devices.sort(key=lambda d: tuple(int(x) for x in d["ip"].split(".")))
    print(f"[SmartHome] Scan of {net}: {len(devices)} devices in {time.time() - t0:.1f}s")
    return devices
