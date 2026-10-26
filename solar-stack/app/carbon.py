"""Grid carbon intelligence — National Grid ESO Carbon Intensity API (free, no key).

The Solar Stack already knows the cheapest time to use power; this adds the
GREENEST time. Same idea as the price plan but for gCO2/kWh, so a customer can
see "the grid is dirty now, your battery is covering you" and "greenest window
is 02:30". On brand for The Solar House, and it costs nothing to serve.
"""
from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

import requests

BASE = "https://api.carbonintensity.org.uk"
_LONDON = ZoneInfo("Europe/London")
_cache: dict = {}   # {"ts": epoch, "data": snapshot}


def snapshot() -> dict | None:
    """Current grid carbon + the greenest upcoming half-hour (next 24h). Cached 20min."""
    c = _cache.get("data")
    if c and time.time() - _cache.get("ts", 0) < 1200:
        return c
    now = dt.datetime.now(dt.timezone.utc)
    url = f"{BASE}/intensity/{now.strftime('%Y-%m-%dT%H:%MZ')}/fw48h"
    try:
        rows = requests.get(url, timeout=12).json().get("data", [])
    except Exception:  # noqa: BLE001 - carbon is a bonus, never break the plan
        return None
    if not rows:
        return None
    cur = rows[0]["intensity"]
    horizon = rows[:48]  # next ~24h
    greenest = min(horizon, key=lambda x: x["intensity"]["forecast"] or 9999)
    ghm = dt.datetime.fromisoformat(greenest["from"].replace("Z", "+00:00")).astimezone(_LONDON).strftime("%H:%M")
    out = {
        "now_g": cur.get("forecast"),
        "index": cur.get("index", "?"),          # very low .. very high
        "greenest_hm": ghm,
        "greenest_g": greenest["intensity"].get("forecast"),
    }
    _cache.update(ts=time.time(), data=out)
    return out
