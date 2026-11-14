"""
Who made this network card? An offline hint from the MAC address's first three
bytes (the OUI).

Deliberately small and offline. A full IEEE registry is tens of megabytes, and
the online lookup services would mean sending every MAC address in the house to
a stranger. This covers the makers that matter for this skill: the chip inside
almost every Sonoff (Espressif), Amazon's Echo line, LG, Google and TP-Link.
It is a HINT. The scan trusts what a device says about itself (mDNS, SSDP, an
open control port) over this table.
"""

from __future__ import annotations

_VENDORS: dict[str, tuple[str, ...]] = {
    # Espressif makes the ESP8266/ESP32 inside Sonoff, Tasmota and most Wi-Fi
    # switches and bulbs; "Espressif" on a home network almost always means a
    # smart plug, switch or bulb.
    "Espressif (Sonoff / smart switch)": (
        "08:3A:F2", "10:52:1C", "18:FE:34", "24:0A:C4", "24:62:AB", "24:6F:28",
        "2C:3A:E8", "30:AE:A4", "34:94:54", "3C:71:BF", "40:F5:20", "48:3F:DA",
        "4C:11:AE", "50:02:91", "58:BF:25", "5C:CF:7F", "60:01:94", "68:C6:3A",
        "78:21:84", "7C:9E:BD", "80:7D:3A", "84:0D:8E", "84:CC:A8", "84:F3:EB",
        "8C:AA:B5", "94:B9:7E", "98:F4:AB", "9C:9C:1F", "A0:20:A6", "A4:CF:12",
        "A8:48:FA", "AC:67:B2", "B4:E6:2D", "BC:DD:C2", "C4:4F:33", "C8:2B:96",
        "C8:C9:A3", "CC:50:E3", "D8:A0:1D", "DC:4F:22", "E0:98:06", "E8:DB:84",
        "E8:6B:EA", "EC:FA:BC", "F4:CF:A2",
    ),
    # Hangzhou BroadLink: RM remotes, SP plugs. E8:16:56 confirmed on this
    # network (an RM4 Pro); the rest are the company's other registrations.
    "Broadlink": (
        "24:DF:A7", "34:EA:34", "78:0F:77", "A0:43:B0", "B4:43:0D", "C8:F7:42",
        "E8:16:56", "EC:0B:AE",
    ),
    "Amazon (Echo / Alexa)": (
        "00:FC:8B", "08:84:9D", "0C:47:C9", "18:74:2E", "1C:12:B0", "34:D2:70",
        "38:F7:3D", "40:B4:CD", "44:65:0D", "4C:EF:C0", "50:DC:E7", "50:F5:DA",
        "5C:41:5A", "68:37:E9", "68:54:FD", "6C:56:97", "74:75:48", "74:C2:46",
        "84:D6:D0", "88:71:E5", "A0:02:DC", "AC:63:BE", "B4:7C:9C", "F0:27:2D",
        "F0:81:73", "FC:65:DE", "FC:A1:83",
    ),
    "LG Electronics": (
        "00:1E:75", "00:E0:91", "10:68:3F", "2C:59:8A", "34:FC:EF", "38:8C:50",
        "3C:BD:D8", "40:B0:FA", "58:A2:B5", "64:99:5D", "64:E4:A5", "6C:D6:8A", "74:40:BE",
        "78:5D:C8", "7C:1C:4E", "88:C9:D0", "A0:39:F7", "A8:23:FE", "C4:36:6C",
        "CC:2D:8C", "E8:5B:5B",
    ),
    "Google (Chromecast / Nest)": (
        "1C:F2:9A", "30:FD:38", "3C:5A:B4", "44:07:0B", "48:D6:D5", "54:60:09",
        "6C:AD:F8", "A4:77:33", "D8:6C:63", "E4:F0:42", "F4:F5:D8", "F4:F5:E8",
    ),
    "TP-Link": (
        "1C:3B:F3", "30:DE:4B", "50:C7:BF", "54:AF:97", "60:32:B1", "98:DA:C4",
        "AC:84:C6", "B0:BE:76", "C0:06:C3",
    ),
}

_BY_PREFIX = {p: name for name, prefixes in _VENDORS.items() for p in prefixes}


def normalize_mac(mac: str) -> str:
    """'aa-bb-cc-dd-ee-ff' / 'aa:bb:..' / 'a:b:..' → 'AA:BB:CC:DD:EE:FF'."""
    parts = mac.replace("-", ":").split(":")
    if len(parts) != 6:
        return mac.upper()
    return ":".join(p.zfill(2) for p in parts).upper()


def is_random(mac: str) -> bool:
    """A locally administered address: phones, tablets and laptops invent one
    per network for privacy, so no maker can be read from it."""
    try:
        return bool(int(normalize_mac(mac)[1], 16) & 0x2)
    except Exception:
        return False


def vendor(mac: str) -> str:
    mac = normalize_mac(mac)
    if is_random(mac):
        return "private address (phone / laptop)"
    return _BY_PREFIX.get(mac[:8], "")
