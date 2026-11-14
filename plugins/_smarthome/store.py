"""
The smart-home device book: memory/smart_home.json.

One record per device, keyed by MAC address, because the MAC is the one thing
about a device on a home network that does not change. The router hands out a
new IP whenever it likes, and the hostname is whatever the firmware felt like.
Scenes live in the same file.

It holds pairing secrets (an LG TV's client key, a Sonoff's device key), so it
sits next to long_term.json and is git-ignored like it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "memory" / "smart_home.json"
_lock = threading.RLock()


def _empty() -> dict:
    return {"devices": {}, "scenes": {}}


def load() -> dict:
    with _lock:
        if not PATH.exists():
            return _empty()
        try:
            data = json.loads(PATH.read_text(encoding="utf-8"))
        except Exception as e:
            # Never let an unreadable file be overwritten by an empty one: set
            # it aside so aliases and pairing keys can still be recovered.
            bad = PATH.with_name(f"smart_home.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json")
            try:
                os.replace(PATH, bad)
            except Exception:
                pass
            print(f"[SmartHome] ⚠️ Unreadable device book moved to {bad.name}: {e}")
            return _empty()
        if not isinstance(data, dict):
            return _empty()
        data.setdefault("devices", {})
        data.setdefault("scenes", {})
        return data


def save(data: dict) -> None:
    with _lock:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, PATH)


def update(fn):
    """Read-modify-write under one lock: fn(data) mutates data in place."""
    with _lock:
        data = load()
        result = fn(data)
        save(data)
        return result
