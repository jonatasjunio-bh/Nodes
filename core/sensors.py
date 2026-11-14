"""
core/sensors.py — the PC's hardware sensors: temperatures, loads, clocks, power, fans.

WHERE THE NUMBERS COME FROM
    CPU and memory temperatures need a kernel driver on Windows; the sensor
    API ordinary programs can reach doesn't have them (and this board exposes
    no ACPI thermal zones). So the source is LibreHardwareMonitor — open
    source, in tools/LibreHardwareMonitor — running in the tray with its local
    web server on 127.0.0.1:8085. It needs administrator rights, so starting it
    shows Windows' UAC prompt; MARK starts it when it opens (plugins/pc_sensors.py).

    Without it: GPU from NVIDIA's driver (NVML), loads and memory use from the
    OS — everything except the CPU and DIMM temperatures.

Read on this PC (Ryzen 7 9700X, Corsair DDR5, GTX 1050 Ti, ASUS X670E):
    CPU Tctl/Tdie + CCD temperature, package power, per-core clock and load;
    each DIMM's temperature with its warning (55 °C) and critical (85 °C) limits;
    GPU core and hot-spot temperature, load, VRAM, clocks; board sensors, fans.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LHM_DIR = BASE / "tools" / "LibreHardwareMonitor"
LHM_EXE = LHM_DIR / "LibreHardwareMonitor.exe"
LHM_URL = "http://127.0.0.1:8085/data.json"

_cache: tuple[float, dict | None] = (0.0, None)
_snap: tuple[float, dict | None] = (0.0, None)


# ── LibreHardwareMonitor ─────────────────────────────────────────────────────

def lhm_installed() -> bool:
    return LHM_EXE.exists()


def lhm_running() -> bool:
    return _lhm_tree(max_age=0) is not None


def start_lhm(wait: float = 25.0) -> tuple[bool, str]:
    """Start LibreHardwareMonitor elevated (Windows asks for confirmation) and
    wait for its sensor server. Returns (ok, message)."""
    if lhm_running():
        return True, "sensors already available"
    if not lhm_installed():
        return False, "LibreHardwareMonitor is not installed in tools/"
    already = False
    try:
        import psutil
        already = any((p.info.get("name") or "").lower() == "librehardwaremonitor.exe"
                      for p in psutil.process_iter(["name"]))
    except Exception:
        pass
    if not already:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(LHM_EXE), None, str(LHM_DIR), 7)
        if rc <= 32:
            return False, ("the administrator prompt was declined" if rc == 5
                           else f"could not start it (code {rc})")
    end = time.time() + wait
    while time.time() < end:
        if lhm_running():
            return True, "sensors started"
        time.sleep(1.0)
    return False, "LibreHardwareMonitor started but its sensor server didn't answer"


def _lhm_tree(max_age: float = 1.5) -> dict | None:
    global _cache
    at, tree = _cache
    if tree is not None and time.monotonic() - at < max_age:
        return tree
    try:
        import requests
        r = requests.get(LHM_URL, timeout=1.5)
        r.raise_for_status()
        tree = r.json()
    except Exception:
        tree = None
    _cache = (time.monotonic(), tree)
    return tree


def _num(text) -> float | None:
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(text or ""))
    return float(m.group(0).replace(",", ".")) if m else None


def _flat(tree: dict) -> list[dict]:
    """Every sensor: {hw, kind, type, name, value, sid}."""
    out = []

    def kind_of(sid: str, img: str) -> str:
        if sid.startswith(("/amdcpu", "/intelcpu")) or "cpu.png" in img:
            return "cpu"
        if sid.startswith("/gpu") or any(k in img for k in ("nvidia", "ati.png", "intel.png")):
            return "gpu"
        if sid.startswith("/memory"):
            return "dimm"
        if sid.startswith("/ram"):
            return "ram"
        if sid.startswith("/vram"):
            return "virtual"
        if sid.startswith("/lpc") or "mainboard" in img:
            return "board"
        return "other"

    # icons of hardware nodes (sensor-group nodes — Voltages, Clocks… — have
    # icons too, which is why this is an allow-list)
    hw_icons = ("cpu", "nvidia", "ati", "amd", "intel", "ram", "mainboard", "chip", "hdd",
                "nic", "ssd", "gpu", "battery", "psu", "bigng", "cooler")

    def walk(node, hw=None, hw_img=""):
        img = node.get("ImageURL", "") or ""
        icon = img.rsplit("/", 1)[-1].split(".")[0].lower()
        if "images_icon/" in img and icon in hw_icons and not (icon == "chip" and hw_img):
            hw, hw_img = node.get("Text", ""), img
        sid = node.get("SensorId") or ""
        if sid and node.get("Type"):
            out.append({"hw": re.sub(r"[\x00-\x1f\x7f-\x9f].*", "", hw or "").strip(),
                        "kind": kind_of(sid, hw_img), "type": node["Type"],
                        "name": node.get("Text", ""), "value": _num(node.get("Value")),
                        "unit": re.sub(r"^[-\d.,\s]+", "", str(node.get("Value") or "")).strip(),
                        "sid": sid})
        for c in node.get("Children") or []:
            walk(c, hw, hw_img)
    walk(tree)
    return out


# ── NVML (GPU without LibreHardwareMonitor) ──────────────────────────────────

def _nvml() -> dict | None:
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(h)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        out = {"name": name.decode() if isinstance(name, bytes) else name,
               "temp": float(pynvml.nvmlDeviceGetTemperature(h, 0)),
               "load": float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu),
               "vram_used": mem.used / 2**20, "vram_total": mem.total / 2**20}
        for key, fn in (("fan", lambda: pynvml.nvmlDeviceGetFanSpeed(h)),
                        ("clock", lambda: pynvml.nvmlDeviceGetClockInfo(h, 0)),
                        ("power", lambda: pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0)):
            try:
                out[key] = float(fn())
            except Exception:
                pass
        return out
    except Exception:
        return None


# ── the snapshot ─────────────────────────────────────────────────────────────

def _from_hwinfo(s: dict) -> bool:
    """Fill the snapshot from HWiNFO64 (core/hwinfo.py). False when it has nothing."""
    try:
        from core import hwinfo
        data = hwinfo.readings()
    except Exception:
        return False
    if not data:
        return False
    pk = hwinfo.pick
    s["source"] = "HWiNFO64 (" + hwinfo.source() + ")"

    def v(r):
        return None if r is None else r["value"]

    t = pk(r"^CPU \(Tctl/Tdie\)$", r"Tctl/Tdie", r"^CPU Die", r"^CPU Package$", r"^CPU$", typ="temperature")
    s["cpu"].update({
        "name": (t["sensor"].split(":", 1)[1].split(":")[0].strip() if t and ":" in t["sensor"] else None),
        "temp": v(t),
        "ccd_temps": [r["value"] for r in data if re.match(r"CPU CCD\d+", r["label"]) and r["type"] == "temperature"],
        "load": v(pk(r"^Total CPU Usage$", typ="usage")) or s["cpu"]["load"],
        "clock": v(pk(r"^Core Clocks \(avg\)$", r"Core Clocks", typ="clock")),
        "clock_effective": v(pk(r"Core Effective Clocks \(avg\)", r"Average Effective Clock", typ="clock")),
        "power": v(pk(r"^CPU Package Power$", r"^CPU PPT$", r"CPU Package Power", typ="power")),
    })
    g_t = pk(r"^GPU Temperature$", typ="temperature")
    if g_t:
        fan = pk(r"^GPU Fan\d*$", r"GPU Fan", typ="fan")
        s["gpu"] = {"name": (g_t["sensor"].split(":", 1)[1].strip() if ":" in g_t["sensor"] else g_t["sensor"]),
                    "temp": v(g_t),
                    "hotspot": v(pk(r"Hot ?Spot", typ="temperature", sensor=r"GPU")),
                    "load": v(pk(r"^GPU Core Load$", r"GPU Utilization", typ="usage")),
                    "vram_used": v(pk(r"^GPU Memory Allocated$", r"GPU D3D Memory Dedicated")),
                    "vram_total": None,
                    "clock": v(pk(r"^GPU Clock$", typ="clock")),
                    "mem_clock": v(pk(r"^GPU Memory Clock$", typ="clock")),
                    "power": v(pk(r"^GPU Power$", r"GPU Power", typ="power")),
                    "fan": None, "fan_rpm": v(fan)}
    for r in data:
        if r["type"] == "temperature" and re.search(r"DIMM|DDR\d", r["sensor"]) and r["value"] == r["value"]:
            m = re.search(r"DIMM\s*\[#?(\d+)\]", r["sensor"]) or re.search(r"#(\d+)", r["sensor"])
            s["memory"]["dimms"].append({"name": f"DIMM {int(m.group(1)) + 1}" if m else r["sensor"][:24],
                                         "module": r["sensor"], "temp": r["value"],
                                         "warn": None, "crit": None})
    temps = [d["temp"] for d in s["memory"]["dimms"] if d["temp"] is not None]
    s["memory"]["temp"] = max(temps) if temps else None
    board = [r for r in data if r["type"] == "temperature"
             and re.search(r"Nuvoton|NCT\d|ITE|Super ?I/O|ASUS|ROG|Motherboard|EC:", r["sensor"], re.I)
             and r["value"] == r["value"] and 15 < r["value"] < 110]
    if board:
        s["board"] = {"name": board[0]["sensor"], "temps": {r["label"]: r["value"] for r in board}}
    s["fans"] = [{"name": r["label"], "rpm": r["value"]} for r in data
                 if r["type"] == "fan" and r["value"] == r["value"] and r["value"] > 0]
    drives = {}
    for r in data:
        if re.search(r"S\.M\.A\.R\.T|Drive", r["sensor"], re.I) and r["value"] == r["value"]:
            name = r["sensor"].split(":", 1)[-1].strip()
            d = drives.setdefault(name, {"name": name})
            if r["type"] == "temperature" and "temp" not in d:
                d["temp"] = r["value"]
            elif re.search(r"Remaining Life", r["label"], re.I):
                d["life"] = r["value"]
    s["drives"] = [d for d in drives.values() if len(d) > 1]
    return True


def snapshot(max_age: float = 1.5) -> dict:
    """Everything known right now, grouped by component."""
    global _snap
    at, s = _snap
    if s is not None and time.monotonic() - at < max_age:
        return s
    import psutil
    vm = psutil.virtual_memory()
    s = {"source": "os", "cpu": {"load": psutil.cpu_percent(interval=None)},
         "gpu": {}, "memory": {"load": vm.percent, "used_gb": vm.used / 2**30,
                               "total_gb": vm.total / 2**30, "dimms": []},
         "board": {}, "fans": [], "drives": [], "missing": []}
    # HWiNFO64 first: it sees everything, needs no prompt from MARK
    tree = None if _from_hwinfo(s) else _lhm_tree()
    if s["source"].startswith("HWiNFO"):
        pass
    elif tree:
        s["source"] = "LibreHardwareMonitor"
        sensors = _flat(tree)

        def pick(kind, typ, *names):
            for nm in names:
                for x in sensors:
                    if x["kind"] == kind and x["type"] == typ and x["name"] == nm and x["value"] is not None:
                        return x["value"]
            return None

        cpu = [x for x in sensors if x["kind"] == "cpu"]
        s["cpu"].update({
            "name": cpu[0]["hw"] if cpu else None,
            "temp": pick("cpu", "Temperature", "Core (Tctl/Tdie)", "CPU Package", "Core (Tctl)",
                         "Core Max", "Core (Tdie)"),
            "ccd_temps": [x["value"] for x in cpu if x["type"] == "Temperature" and x["name"].startswith("CCD")],
            "load": pick("cpu", "Load", "CPU Total") or s["cpu"]["load"],
            "clock": pick("cpu", "Clock", "Cores (Average)"),
            "clock_effective": pick("cpu", "Clock", "Cores (Average Effective)"),
            "power": pick("cpu", "Power", "Package", "CPU Package"),
        })
        gpu = [x for x in sensors if x["kind"] == "gpu"]
        if gpu:
            s["gpu"] = {"name": gpu[0]["hw"],
                        "temp": pick("gpu", "Temperature", "GPU Core"),
                        "hotspot": pick("gpu", "Temperature", "GPU Hot Spot"),
                        "load": pick("gpu", "Load", "GPU Core"),
                        "vram_used": pick("gpu", "SmallData", "GPU Memory Used"),
                        "vram_total": pick("gpu", "SmallData", "GPU Memory Total"),
                        "clock": pick("gpu", "Clock", "GPU Core"),
                        "mem_clock": pick("gpu", "Clock", "GPU Memory"),
                        "power": pick("gpu", "Power", "GPU Package", "GPU Power"),
                        "fan": pick("gpu", "Control", "GPU Fan") or pick("gpu", "Fan", "GPU Fan")}
        for x in sensors:
            if x["kind"] == "dimm" and x["type"] == "Temperature" and x["name"].startswith("DIMM"):
                hi = next((y["value"] for y in sensors if y["sid"].startswith(x["sid"].rsplit("/", 1)[0])
                           and y["name"] == "Thermal Sensor High Limit"), None)
                crit = next((y["value"] for y in sensors if y["sid"].startswith(x["sid"].rsplit("/", 1)[0])
                             and y["name"] == "Thermal Sensor Critical High Limit"), None)
                s["memory"]["dimms"].append({"name": x["name"], "module": x["hw"], "temp": x["value"],
                                             "warn": hi, "crit": crit})
        temps = [d["temp"] for d in s["memory"]["dimms"] if d["temp"] is not None]
        s["memory"]["temp"] = max(temps) if temps else None
        board = [x for x in sensors if x["kind"] == "board"]
        if board:
            s["board"] = {"name": board[0]["hw"],
                          "temps": {x["name"]: x["value"] for x in board
                                    if x["type"] == "Temperature" and x["value"] is not None
                                    and 15 < x["value"] < 110}}
        s["fans"] = [{"name": f"{x['hw']} · {x['name']}" if x["kind"] != "board" else x["name"],
                      "rpm": x["value"]} for x in sensors
                     if x["type"] == "Fan" and x["value"] is not None and x["value"] > 0]
    else:
        s["missing"].append("cpu_temp")
        s["missing"].append("ram_temp")
    if s["gpu"] and s["gpu"].get("vram_total") is None:
        g = _nvml()            # HWiNFO reports VRAM in use, NVML knows the total
        if g:
            s["gpu"]["vram_total"] = g.get("vram_total")
    if not s["gpu"]:
        g = _nvml()
        if g:
            s["gpu"] = g
    _snap = (time.monotonic(), s)
    return s


VALUES = {
    "cpu_temp": ("cpu", "temp", "°C"), "cpu_load": ("cpu", "load", "%"),
    "cpu_power": ("cpu", "power", "W"), "cpu_clock": ("cpu", "clock", "MHz"),
    "gpu_temp": ("gpu", "temp", "°C"), "gpu_hotspot": ("gpu", "hotspot", "°C"),
    "gpu_load": ("gpu", "load", "%"), "gpu_vram": ("gpu", "vram_used", "MB"),
    "gpu_clock": ("gpu", "clock", "MHz"), "gpu_power": ("gpu", "power", "W"),
    "gpu_fan": ("gpu", "fan", "%"), "ram_temp": ("memory", "temp", "°C"),
    "ram_load": ("memory", "load", "%"), "ram_used": ("memory", "used_gb", "GB"),
}


def value(key: str):
    """One reading by name (see VALUES), or None when not available."""
    grp, field, _unit = VALUES[key]
    return snapshot().get(grp, {}).get(field)


def report(full: bool = False) -> str:
    """A plain-text reading for the assistant to speak from."""
    s = snapshot(max_age=0)

    def f(v, unit, d=0):
        return "n/d" if v is None else f"{v:.{d}f} {unit}".strip()

    c, g, m = s["cpu"], s["gpu"], s["memory"]
    lines = [f"CPU {c.get('name') or ''}: temperatura {f(c.get('temp'), '°C', 1)}"
             + (f" (CCD {', '.join(f'{t:.0f} °C' for t in c['ccd_temps'])})" if c.get("ccd_temps") else "")
             + f", uso {f(c.get('load'), '%')}, clock {f(c.get('clock'), 'MHz')}"
             + (f", consumo {f(c.get('power'), 'W', 1)}" if c.get("power") is not None else "")]
    if g:
        lines.append(f"GPU {g.get('name') or ''}: temperatura {f(g.get('temp'), '°C', 1)}"
                     + (f" (hot spot {f(g.get('hotspot'), '°C', 1)})" if g.get("hotspot") is not None else "")
                     + f", uso {f(g.get('load'), '%')}, VRAM {f(g.get('vram_used'), 'MB')}"
                     + f" de {f(g.get('vram_total'), 'MB')}, clock {f(g.get('clock'), 'MHz')}"
                     + (f", ventoinha {f(g.get('fan'), '%')}" if g.get("fan") is not None else "")
                     + (f", ventoinha {f(g.get('fan_rpm'), 'rpm')}" if g.get("fan_rpm") else ""))
    mem = (f"Memória: {m['used_gb']:.1f} de {m['total_gb']:.0f} GB em uso ({m['load']:.0f}%)")
    if m.get("dimms"):
        mem += "; temperatura dos pentes: " + ", ".join(
            f"{d['name']} {f(d['temp'], '°C', 1)}" for d in m["dimms"])
        if m["dimms"][0].get("warn"):
            mem += f" (alerta a {m['dimms'][0]['warn']:.0f} °C, crítico a {m['dimms'][0]['crit']:.0f} °C)"
    lines.append(mem)
    if full and s.get("board", {}).get("temps"):
        lines.append(f"Placa-mãe {s['board']['name']}: " + ", ".join(
            f"{k} {v:.0f} °C" for k, v in s["board"]["temps"].items()))
    if full and s.get("fans"):
        lines.append("Ventoinhas: " + ", ".join(f"{x['name']} {x['rpm']:.0f} rpm" for x in s["fans"]))
    if full and s.get("drives"):
        lines.append("Discos: " + "; ".join(
            d["name"] + (f" {d['temp']:.0f} °C" if d.get("temp") is not None else "")
            + (f", vida útil {d['life']:.0f}%" if d.get("life") is not None else "") for d in s["drives"]))
    if s["missing"]:
        lines.append("Temperaturas do processador e da memória indisponíveis: nem o HWiNFO64 (com "
                     "Shared Memory ligado) nem o LibreHardwareMonitor estão fornecendo dados.")
    return "\n".join(lines)
