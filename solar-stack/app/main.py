"""Solar Stack — Home Assistant Add-on.

Runs inside HA Supervisor. Reads inverter state directly from HA entities via the
Supervisor API, generates a half-hourly plan (Go cheap window + Agile export), and
serves it on port 8791 for the ball device + the web app.

No database, no external cloud, no HA token in config — Supervisor injects
$SUPERVISOR_TOKEN automatically.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse

import ha_reader
import planner
import rates
import carbon

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SolarStack] %(message)s")
log = logging.getLogger("addon")

# ── Config ─────────────────────────────────────────────────────────────────────
# HA Supervisor writes the add-on options to /data/options.json
_OPTIONS_FILE = Path("/data/options.json")

def _load_cfg() -> dict:
    try:
        return json.loads(_OPTIONS_FILE.read_text())
    except Exception:
        # Fallback defaults — fine for dev/testing outside HA
        return {
            "battery_kwh": 10.0, "battery_min_soc": 10, "battery_max_soc": 100,
            "max_charge_kw": 3.6, "max_discharge_kw": 3.6, "wear_cost_p": 5.0,
            "tariff": "go", "go_cheap_start": "00:00", "go_cheap_end": "05:00",
            "go_cheap_rate_p": 9.0, "go_day_rate_p": 24.0,
            "export_tariff": "agile_outgoing", "gsp_region": "K",
            "entity_soc": "sensor.fox_battery_soc",
            "entity_solar_kw": "sensor.fox_solar_total",
            "entity_battery_kw": "sensor.fox_battery_power",
            "entity_grid_kw": "sensor.fox_grid_import",
            "entity_house_kw": "sensor.fox_load_power",
        }

CFG = _load_cfg()
log.info("Config loaded: tariff=%s region=%s battery=%.1fkWh",
         CFG.get("tariff"), CFG.get("gsp_region"), CFG.get("battery_kwh", 0))

# ── State ──────────────────────────────────────────────────────────────────────
_live: dict = {}                                       # latest inverter reading
_plan_cache: dict = {}                                 # cached plan
_plan_ts: float = 0.0
_hist: collections.deque = collections.deque(maxlen=1440)  # 24h at 1/min
_PLAN_TTL = 300  # regenerate plan every 5 min

# ── Background poller ──────────────────────────────────────────────────────────
def _poll_loop():
    """Read HA entities + regenerate plan every 20s."""
    while True:
        try:
            block = ha_reader.read_live(CFG)
            if block:
                _live.update(block)
                _hist.append({"t": time.time(), "soc": block.get("soc"),
                               "pv": block.get("solar_kw")})
        except Exception as e:  # noqa: BLE001
            log.warning("poll error: %s", e)
        time.sleep(20)

def _plan_loop():
    """Regenerate the slot plan every 5 min (rate changes are slow)."""
    global _plan_cache, _plan_ts
    while True:
        try:
            imp, exp = rates.get_rates(CFG)
            soc = _live.get("soc", 50.0)
            result = planner.plan_slots(
                imp, exp,
                soc=soc,
                capacity_kwh=CFG.get("battery_kwh", 10.0),
                max_charge_kw=CFG.get("max_charge_kw", 3.6),
                max_discharge_kw=CFG.get("max_discharge_kw", 3.6),
                min_soc=CFG.get("battery_min_soc", 10),
                max_soc=CFG.get("battery_max_soc", 100),
                wear_cost_p=CFG.get("wear_cost_p", 5.0),
            )
            _plan_cache = result
            _plan_ts = time.time()
            log.info("plan regenerated: %d slots soc=%.0f%% net=£%.2f",
                     len(result.get("slots", [])),
                     soc,
                     result.get("economics", {}).get("net_gbp", 0))
        except Exception as e:  # noqa: BLE001
            log.warning("plan error: %s", e)
        time.sleep(_PLAN_TTL)

for _fn in (_poll_loop, _plan_loop):
    threading.Thread(target=_fn, daemon=True).start()

# Give the first poll a moment before serving requests
time.sleep(2)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Solar Stack Add-on", version="0.1.0")

APP_HTML = Path(__file__).parent / "app.html"


@app.get("/healthz")
def healthz():
    return {"ok": True, "tariff": CFG.get("tariff"), "soc": _live.get("soc"),
            "plan_age_s": round(time.time() - _plan_ts) if _plan_ts else None}


@app.get("/")
@app.get("/app")
def serve_app():
    if APP_HTML.exists():
        return HTMLResponse(APP_HTML.read_text())
    return JSONResponse({"error": "app.html not found"}, status_code=404)


@app.get("/v1/plan")
def get_plan():
    """The ball device fetches this every 30s."""
    plan = dict(_plan_cache)
    plan["live"] = _live or None
    # Add now/eta/tariff summary for the display
    slots = plan.get("slots", [])
    if slots:
        plan["now"] = {"action": slots[0]["action"], "reason": slots[0]["reason"],
                       "p": slots[0]["p"], "soc": _live.get("soc")}
    plan["tariff"] = {
        "type": CFG.get("tariff"),
        "cheap_start": CFG.get("go_cheap_start"),
        "cheap_end": CFG.get("go_cheap_end"),
        "cheap_p": CFG.get("go_cheap_rate_p"),
        "day_p": CFG.get("go_day_rate_p"),
    }
    try:
        plan["carbon"] = carbon.current()
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse(plan)


# Legacy path the ball firmware uses — alias to /v1/plan
@app.get("/v1/sites/demo/plan")
@app.get("/v1/sites/{site_id}/plan")
def get_plan_site(site_id: str = "demo"):
    return get_plan()


@app.get("/v1/live")
def get_live():
    return JSONResponse(_live or {"error": "no data yet"})


@app.get("/v1/history")
def get_history():
    return JSONResponse(list(_hist)[-288:])  # last 2h at 1/min = 120 points max


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8791, log_level="info")
