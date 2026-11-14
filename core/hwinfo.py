"""
core/hwinfo.py — every sensor HWiNFO64 sees, for the assistant.

HWiNFO reads far more than anything else on this PC: CPU (Tctl/Tdie, each CCD,
each core's clock and effective clock, package/SoC power, voltages), every
DDR5 stick's SPD hub temperature, the GPU (core, hot spot, memory junction,
fans, power), the motherboard's Super-I/O (VRM, chipset, every fan header,
every rail), the drives (S.M.A.R.T. temperature, wear, errors) and more —
hundreds of readings, each with its minimum, maximum and average since start.

Two ways out of HWiNFO, both read here:

  Shared Memory   (Settings → "Shared Memory Support") — the whole sensor
                  table. In the free version HWiNFO switches it off after 12
                  hours of run time; restarting HWiNFO switches it back on, and
                  restart_hwinfo() does that through a scheduled task (no UAC
                  prompt) when one has been set up.
  Gadget          (Settings → HWiNFO Gadget, + "Report value in Gadget" per
                  sensor) — only the chosen readings, in the registry
                  (HKCU\\Software\\HWiNFO64\\VSB), with no time limit.

Nothing here writes to HWiNFO; it only reads.
"""

from __future__ import annotations

import ctypes
import re
import struct
import subprocess
import threading
import time
import unicodedata

SHM_NAMES = ("Global\\HWiNFO_SENS_SM2", "HWiNFO_SENS_SM2")
SIGNATURE = 0x53695748                      # 'HWiS'
TASK_NAME = "MARK HWiNFO"                   # scheduled task that restarts HWiNFO elevated
TYPES = {0: "none", 1: "temperature", 2: "voltage", 3: "fan", 4: "current",
         5: "power", 6: "clock", 7: "usage", 8: "other"}

_lock = threading.Lock()
_cache: tuple[float, list[dict], str] = (0.0, [], "")


# ── Shared Memory ────────────────────────────────────────────────────────────

def _read_shm() -> list[dict] | None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenFileMappingW.restype = ctypes.c_void_p
    k32.OpenFileMappingW.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p)
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_uint32, ctypes.c_size_t)
    k32.UnmapViewOfFile.argtypes = (ctypes.c_void_p,)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    FILE_MAP_READ = 0x0004
    h = None
    for name in SHM_NAMES:
        h = k32.OpenFileMappingW(FILE_MAP_READ, False, name)
        if h:
            break
    if not h:
        return None
    view = k32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
    try:
        if not view:
            return None
        # header: signature, version, revision, poll time (int64), then the
        # offset / element size / count of the sensor and reading tables
        head = ctypes.string_at(view, 44)
        (sig, _ver, _rev, _poll, s_off, s_size, s_num,
         r_off, r_size, r_num) = struct.unpack_from("<IIIqIIIIII", head, 0)
        if sig != SIGNATURE or not r_num or r_size < 316:
            return None
        total = max(s_off + s_size * s_num, r_off + r_size * r_num)
        buf = ctypes.string_at(view, total)
    finally:
        if view:
            k32.UnmapViewOfFile(view)
        k32.CloseHandle(h)
    return parse(buf, s_off, s_size, s_num, r_off, r_size, r_num)


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("cp1252", "replace").strip()


def parse(buf: bytes, s_off: int, s_size: int, s_num: int,
          r_off: int, r_size: int, r_num: int) -> list[dict]:
    """The sensor and reading tables (layout of HWiNFO's shared memory v2)."""
    sensors = []
    for i in range(s_num):
        o = s_off + i * s_size
        orig, user = _cstr(buf[o + 8:o + 136]), _cstr(buf[o + 136:o + 264])
        sensors.append(user or orig)
    out = []
    for i in range(r_num):
        o = r_off + i * r_size
        typ, s_idx, _rid = struct.unpack_from("<III", buf, o)
        label = _cstr(buf[o + 140:o + 268]) or _cstr(buf[o + 12:o + 140])
        unit = _cstr(buf[o + 268:o + 284])
        val, vmin, vmax, vavg = struct.unpack_from("<dddd", buf, o + 284)
        out.append({"sensor": sensors[s_idx] if s_idx < len(sensors) else "",
                    "label": label, "unit": unit, "type": TYPES.get(typ, "other"),
                    "value": val, "min": vmin, "max": vmax, "avg": vavg})
    return out


# ── Gadget (registry) ────────────────────────────────────────────────────────

def _read_gadget() -> list[dict] | None:
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\HWiNFO64\VSB")
    except OSError:
        return None
    vals = {}
    try:
        i = 0
        while True:
            name, data, _t = winreg.EnumValue(key, i)
            vals[name] = data
            i += 1
    except OSError:
        pass
    finally:
        winreg.CloseKey(key)
    out, n = [], 0
    while f"Label{n}" in vals:
        raw = str(vals.get(f"ValueRaw{n}", "")).replace(",", ".")
        shown = str(vals.get(f"Value{n}", ""))
        try:
            v = float(raw)
        except ValueError:
            m = re.search(r"-?\d+(?:[.,]\d+)?", shown)
            v = float(m.group(0).replace(",", ".")) if m else float("nan")
        unit = re.sub(r"^[\s\-\d.,]+", "", shown).strip()
        out.append({"sensor": str(vals.get(f"Sensor{n}", "")), "label": str(vals[f"Label{n}"]),
                    "unit": unit, "type": "other", "value": v, "min": None, "max": None, "avg": None})
        n += 1
    return out or None


# ── public ───────────────────────────────────────────────────────────────────

def readings(max_age: float = 1.0) -> list[dict]:
    """Every reading available right now (cached briefly). [] without HWiNFO."""
    global _cache
    with _lock:
        at, data, _src = _cache
        if data and time.monotonic() - at < max_age:
            return data
        src, data = "", []
        try:
            data = _read_shm() or []
            src = "shared memory" if data else ""
        except Exception:
            data = []
        if not data:
            try:
                data = _read_gadget() or []
                src = "gadget" if data else ""
            except Exception:
                data = []
        _cache = (time.monotonic(), data, src)
        return data


def source() -> str:
    """'shared memory', 'gadget' or '' — where the readings come from."""
    readings()
    return _cache[2]


def running() -> bool:
    try:
        import psutil
        return any((p.info.get("name") or "").lower() in ("hwinfo64.exe", "hwinfo.exe")
                   for p in psutil.process_iter(["name"]))
    except Exception:
        return False


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


# Portuguese → the words HWiNFO uses
_SYN = {
    "temperatura": "temp", "ventoinha": "fan", "ventilador": "fan", "cooler": "fan",
    "tensao": "voltage", "voltagem": "voltage", "consumo": "power", "potencia": "power",
    "processador": "cpu", "placa de video": "gpu", "video": "gpu", "memoria": "dimm",
    "pente": "dimm", "ssd": "drive", "nvme": "drive", "disco": "drive", "hd": "drive",
    "placa-mae": "motherboard", "placa mae": "motherboard", "chipset": "chipset",
    "uso": "usage", "carga": "load", "clock": "clock", "frequencia": "clock",
    "vida util": "life", "saude": "life", "desgaste": "life", "erros": "error",
}


def find(query: str, limit: int = 25) -> list[dict]:
    """Readings whose sensor or label matches every word of `query`."""
    q = _fold(query or "")
    for pt, en in sorted(_SYN.items(), key=lambda kv: -len(kv[0])):
        q = q.replace(pt, en)
    words = [w for w in re.split(r"[\s,;/]+", q) if w and w not in ("de", "do", "da", "o", "a", "e")]
    out = []
    for r in readings():
        hay = _fold(f"{r['sensor']} {r['label']} {r['type']}")
        if all(w in hay for w in words):
            out.append(r)
    return out[:limit]


def best(query: str) -> dict | None:
    hits = [r for r in find(query, 200) if r["value"] == r["value"]]      # drop NaN
    return hits[0] if hits else None


def fmt(r: dict, stats: bool = False) -> str:
    def n(v):
        if v is None or v != v:
            return "n/d"
        return f"{v:.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:.2f}".rstrip("0").rstrip(".")
    s = f"{r['label']}: {n(r['value'])} {r['unit']}".rstrip()
    if stats and r.get("max") is not None:
        s += f" (mín {n(r['min'])}, máx {n(r['max'])}, média {n(r['avg'])})"
    return s


def pick(*patterns: str, typ: str | None = None, sensor: str | None = None):
    """The first reading whose label matches one of the regex patterns (in order)."""
    data = readings()
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for r in data:
            if typ and r["type"] != typ:
                continue
            if sensor and not re.search(sensor, r["sensor"], re.I):
                continue
            if rx.search(r["label"]) and r["value"] == r["value"]:
                return r
    return None


# ── keeping Shared Memory on (free version: off after 12 h) ─────────────────

def task_exists() -> bool:
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True,
                           creationflags=0x08000000)      # bytes: the console code page varies
        return r.returncode == 0
    except Exception:
        return False


def restart_hwinfo() -> bool:
    """Reopen HWiNFO through the scheduled task, which re-enables Shared Memory.
    The task itself closes the running copy first: HWiNFO runs elevated, so a
    normal process could not. No UAC prompt. False without the task."""
    if not task_exists():
        return False
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True,
                           creationflags=0x08000000)
        return r.returncode == 0
    except Exception:
        return False


def hwinfo_exe() -> str:
    import os
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        p = os.path.join(base, "HWiNFO64", "HWiNFO64.EXE")
        if os.path.exists(p):
            return p
    return ""


def create_task() -> tuple[bool, str]:
    """Set up the scheduled task, once (Windows asks for administrator rights
    this one time): at logon it opens HWiNFO with administrator rights and no
    prompt, and run on demand it closes and reopens it — which is how Shared
    Memory comes back after the free version's 12-hour limit."""
    import os
    import tempfile
    from xml.sax.saxutils import escape
    exe = hwinfo_exe()
    if not exe:
        return False, "HWiNFO64 is not installed in Program Files"
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    args = (f'/c taskkill /IM HWiNFO64.EXE /F >nul 2>&1 & timeout /t 2 /nobreak >nul '
            f'& start "" "{exe}"')
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>MARK: opens HWiNFO64 at logon and reopens it when its Shared Memory turns off.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId><Delay>PT20S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>
    <Hidden>true</Hidden>
  </Settings>
  <Actions Context="Author"><Exec><Command>cmd.exe</Command><Arguments>{escape(args)}</Arguments></Exec></Actions>
</Task>"""
    path = os.path.join(tempfile.gettempdir(), "mark_hwinfo_task.xml")
    with open(path, "w", encoding="utf-16") as f:
        f.write(xml)
    params = f'/Create /TN "{TASK_NAME}" /XML "{path}" /F'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "schtasks.exe", params, None, 0)
    if rc <= 32:
        return False, ("the administrator prompt was declined" if rc == 5 else f"could not create it (code {rc})")
    for _ in range(20):
        time.sleep(0.5)
        if task_exists():
            try:
                os.remove(path)
            except OSError:
                pass
            return True, "automatic re-enable is on"
    return False, "the task was not created"
