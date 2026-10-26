"""Forward rate builder — supports Octopus Go (fixed cheap window) + Agile.

For Go the import rates are deterministic: cheap_rate_p during the cheap window,
day_rate_p otherwise. No Octopus API call needed for import.

Export (Agile Outgoing) is still fetched live from the Octopus public API.
"""
from __future__ import annotations

import datetime as dt
import time

import requests

_LONDON = dt.timezone(dt.timedelta(hours=0))  # UTC, converted to London below
try:
    from zoneinfo import ZoneInfo
    _LONDON = ZoneInfo("Europe/London")
except ImportError:
    pass

BASE = "https://api.octopus.energy/v1"
_export_cache: dict = {}   # region -> (rates, ts)
_code_cache: dict = {}


def _find_export_product() -> str | None:
    cached = _code_cache.get("EXPORT")
    if cached and time.time() - cached[1] < 6 * 3600:
        return cached[0]
    url = f"{BASE}/products/?brand=OCTOPUS_ENERGY"
    best = None
    for _ in range(6):
        d = requests.get(url, timeout=15).json()
        for p in d.get("results", []):
            if p.get("direction") == "EXPORT" and p.get("code", "").startswith("AGILE"):
                af = p.get("available_from") or ""
                if best is None or af > best[1]:
                    best = (p["code"], af)
        url = d.get("next")
        if not url:
            break
    code = best[0] if best else None
    _code_cache["EXPORT"] = (code, time.time())
    return code


def go_import_rates(cfg: dict, hours: int = 30) -> list[dict]:
    """Generate half-hourly rate dicts for Octopus Go tariff."""
    cheap_start = cfg.get("go_cheap_start", "00:00")
    cheap_end = cfg.get("go_cheap_end", "05:00")
    cheap_p = float(cfg.get("go_cheap_rate_p", 9.0))
    day_p = float(cfg.get("go_day_rate_p", 24.0))

    ch, cm = (int(x) for x in cheap_start.split(":"))
    eh, em = (int(x) for x in cheap_end.split(":"))

    now = dt.datetime.now(dt.timezone.utc)
    # round down to nearest half-hour
    slot = now.replace(minute=30 * (now.minute // 30), second=0, microsecond=0)

    rates = []
    for _ in range(hours * 2):
        local = slot.astimezone(_LONDON)
        mins = local.hour * 60 + local.minute
        cheap_start_m = ch * 60 + cm
        cheap_end_m = eh * 60 + em
        in_cheap = cheap_start_m <= mins < cheap_end_m
        rates.append({
            "start": slot,
            "end": slot + dt.timedelta(minutes=30),
            "p": cheap_p if in_cheap else day_p,
        })
        slot += dt.timedelta(minutes=30)
    return rates


def agile_export_rates(region: str, hours: int = 30) -> list[dict]:
    """Forward Agile Outgoing export rates for a GSP region."""
    cached = _export_cache.get(region)
    if cached and time.time() - cached[1] < 1800:  # cache 30 min
        return cached[0]

    code = _find_export_product()
    if not code:
        return []
    r = region.lstrip("_")
    tcode = f"E-1R-{code}-{r}"
    now = dt.datetime.now(dt.timezone.utc)
    params = {
        "period_from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period_to": (now + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page_size": 96,
    }
    url = f"{BASE}/products/{code}/electricity-tariffs/{tcode}/standard-unit-rates/"
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        rows = resp.json().get("results", [])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in rows:
        try:
            s = dt.datetime.fromisoformat(row["valid_from"].replace("Z", "+00:00"))
            e = dt.datetime.fromisoformat(row["valid_to"].replace("Z", "+00:00"))
            out.append({"start": s, "end": e, "p": round(float(row["value_inc_vat"]), 2)})
        except (KeyError, ValueError, TypeError):
            continue
    result = sorted(out, key=lambda x: x["start"])
    _export_cache[region] = (result, time.time())
    return result


def get_rates(cfg: dict) -> tuple[list[dict], list[dict]]:
    """Return (import_rates, export_rates) for a site's tariff config."""
    tariff = cfg.get("tariff", "go")
    region = cfg.get("gsp_region", "K")
    if tariff == "go":
        imp = go_import_rates(cfg)
    else:
        # Agile import — fetch live (used for non-Go customers)
        from octopus import forward_rates  # noqa: PLC0415
        imp = forward_rates(region)
    exp = agile_export_rates(region)
    return imp, exp
