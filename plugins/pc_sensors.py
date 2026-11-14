"""
PC sensors — the assistant reads the computer's temperatures, loads, clocks,
power and fans (core/sensors.py does the reading).

    "qual a temperatura do processador?"   → cpu temp (Tctl/Tdie) + CCD
    "como está a memória?"                 → use + each DIMM's temperature
    "e a placa de vídeo?"                  → GPU temp, hot spot, load, VRAM, fan
    "relatório completo dos sensores"      → everything, board and fans included

CPU and memory temperatures come from LibreHardwareMonitor (tools/), which
needs administrator rights: MARK starts it when it opens and Windows asks for
confirmation. If it isn't running, everything else still answers.
"""

from __future__ import annotations

import threading
import time


def run(parameters: dict, player=None, session_memory=None) -> str:
    from core import sensors
    p = parameters or {}
    action = str(p.get("action", "read")).strip().lower()
    if action in ("start", "enable", "iniciar"):
        ok, msg = sensors.start_lhm()
        return ("Temperature sensors are on." if ok else f"Could not start the sensors: {msg}.")
    if action == "status":
        from core import hwinfo
        if hwinfo.readings():
            return f"Sensors from HWiNFO64 ({hwinfo.source()}): {len(hwinfo.readings())} readings available."
        return ("LibreHardwareMonitor is running — all temperatures available." if sensors.lhm_running()
                else "LibreHardwareMonitor is not running: CPU and memory temperatures unavailable "
                     "(action 'start' opens it; Windows asks for administrator confirmation).")
    if action == "value":
        key = str(p.get("sensor") or "").strip().lower()
        if key not in sensors.VALUES:
            return f"Unknown sensor '{key}'. Sensors: {', '.join(sensors.VALUES)}."
        v = sensors.value(key)
        unit = sensors.VALUES[key][2]
        return f"{key}: {'unavailable' if v is None else f'{v:.1f} {unit}'}"
    return sensors.report(full=action in ("full", "all", "completo", "tudo"))


def start(player) -> None:
    """When MARK opens: bring the temperature sensors up (UAC prompt), in the
    background so nothing waits on the user's click."""
    def go():
        from core import sensors, hwinfo
        # HWiNFO64 sees everything and needs no prompt from MARK: give it a
        # moment (it may be starting with Windows) before falling back to
        # LibreHardwareMonitor, which asks for administrator rights.
        for _ in range(6):
            if hwinfo.readings():
                try:
                    player.write_log(f"SYS: 🌡 Sensores pelo HWiNFO64 ({len(hwinfo.readings())} leituras).")
                except Exception:
                    pass
                return
            time.sleep(5)
        if not sensors.lhm_installed() or sensors.lhm_running():
            return
        ok, msg = sensors.start_lhm(wait=40)
        try:
            player.write_log("SYS: 🌡 Sensores de temperatura ativos." if ok
                             else f"SYS: 🌡 Sensores de temperatura indisponíveis ({msg}).")
        except Exception:
            pass
    threading.Thread(target=go, daemon=True, name="sensors-start").start()


PLUGIN = {
    "name": "pc_sensors",
    "description": (
        "Reads the PC's hardware sensors: CPU temperature (Tctl/Tdie, CCD), load, clock and power; "
        "GPU temperature, hot spot, load, VRAM, clock, fan; memory use and each RAM stick's "
        "temperature (with warning/critical limits); motherboard temperatures and fan speeds. "
        "action: read (default — CPU, GPU, memory) | full (everything) | value (sensor = cpu_temp, "
        "gpu_temp, gpu_hotspot, ram_temp, cpu_load, gpu_load, cpu_power, cpu_clock, gpu_clock, "
        "gpu_vram, gpu_fan, ram_load, ram_used) | status | start (turn the temperature sensors on). "
        "Use for any question about the computer's temperature, heat, fans or performance. To SHOW "
        "them on the HUD use hud_display with source cpu_temp / gpu_temp / ram_temp etc."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "read | full | value | status | start"},
            "sensor": {"type": "STRING", "description": "value: which sensor (see description)"},
        },
        "required": [],
    },
}
