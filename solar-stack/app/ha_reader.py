"""Read live inverter state from Home Assistant Supervisor API.

Inside an HA add-on the Supervisor provides:
  - base URL  http://supervisor/core
  - token     $SUPERVISOR_TOKEN  (injected automatically by HA, no config needed)

This replaces the Mac solarstack-ha-push/push_house.py entirely — the add-on reads
the inverter entities directly without any external pusher.
"""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("ha_reader")

_BASE = "http://supervisor/core/api"
_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"}

# Cache: entity_id -> (value, timestamp)
_cache: dict = {}
_CACHE_TTL = 30   # seconds — HA updates most sensors every 30–60s


def _state(entity_id: str, default=None):
    """Read one entity state from HA (cached 30s)."""
    now = time.time()
    if entity_id in _cache and now - _cache[entity_id][1] < _CACHE_TTL:
        return _cache[entity_id][0]
    try:
        r = requests.get(f"{_BASE}/states/{entity_id}", headers=_HEADERS, timeout=5)
        if r.status_code == 200:
            val = r.json().get("state")
            _cache[entity_id] = (val, now)
            return val
        log.warning("HA state %s → HTTP %s", entity_id, r.status_code)
    except Exception as e:  # noqa: BLE001
        log.warning("HA state %s → %s", entity_id, e)
    return default


def _float(entity_id: str, default: float = 0.0) -> float:
    v = _state(entity_id)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def read_live(cfg: dict) -> dict | None:
    """Return a live block from HA entities, or None if unavailable."""
    if not _TOKEN:
        log.warning("SUPERVISOR_TOKEN not set — running outside HA?")
        return None

    soc = _float(cfg.get("entity_soc", "sensor.fox_battery_soc"))
    solar_kw = _float(cfg.get("entity_solar_kw", "sensor.fox_solar_total"))
    battery_kw = _float(cfg.get("entity_battery_kw", "sensor.fox_battery_power"))
    grid_kw = _float(cfg.get("entity_grid_kw", "sensor.fox_grid_import"))
    house_kw = _float(cfg.get("entity_house_kw", "sensor.fox_load_power"))

    if soc == 0.0 and solar_kw == 0.0 and battery_kw == 0.0:
        # All zeros most likely means entities not found / unavailable
        log.warning("All inverter entities read 0.0 — check entity IDs in add-on config")
        return None

    # Derive action from battery power direction (positive = charging)
    if battery_kw > 0.1:
        action = "charge"
    elif battery_kw < -0.1:
        action = "discharge"
    else:
        action = "idle"

    return {
        "soc": round(soc, 1),
        "solar_kw": round(solar_kw, 2),
        "battery_kw": round(battery_kw, 2),
        "grid_kw": round(grid_kw, 2),
        "house_kw": round(house_kw, 2),
        "action": action,
        "source": "ha_supervisor",
        "ts": time.time(),
    }
