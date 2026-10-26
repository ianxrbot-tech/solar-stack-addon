"""Everything a Solar Stack display needs, as ONE small JSON — and all of the "intelligence".

Pure functions (no I/O) so every rule is unit-tested (test_display.py). Displays and the app stay dumb: fix or improve
a rule here and every unit in the field gets smarter without a reflash.

Conventions (same as the live block): battery_kw  + = charging, - = discharging;  grid_kw  + = import, - = export.
"""
from __future__ import annotations

import datetime as dt
import math
import time
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
STEP = 1800                                            # Agile slot length (s)
ACT_CHAR = {"charge": "c", "export": "e", "discharge": "d", "self_use": "s", "hold": "h"}


def hm(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, LONDON).strftime("%H:%M")


def to_epoch(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso).timestamp())


def tier(p):
    """Price tier for a p/kWh import rate -> (key, word)."""
    if p is None:
        return "normal", "NORMAL"
    if p < 0:
        return "paid", "PAID"
    if p == 0:
        return "free", "FREE"
    if p <= 12:
        return "cheap", "CHEAP"
    if p <= 25:
        return "normal", "NORMAL"
    return "peak", "PEAK"


def fmt_dur(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def eta(live, cap, now, min_soc=10.0):
    """How long until the battery is full / empty at the CURRENT rate. Honest: 'none' when there's no clear answer."""
    none = {"kind": "none", "minutes": 0, "at": None, "text": ""}
    if not live or live.get("soc") is None or not cap:
        return none
    soc = float(live["soc"]); bat = float(live.get("battery_kw") or 0); house = float(live.get("house_kw") or 0)
    if bat >= 0.1 and soc < 99.5:
        hrs, kind = ((100 - soc) / 100 * cap) / bat, "full"
    elif bat <= -0.1 and soc > min_soc + 0.5:
        hrs, kind = ((soc - min_soc) / 100 * cap) / (-bat), "empty"
    elif abs(bat) < 0.1 and house >= 0.1 and soc > min_soc + 0.5:
        hrs, kind = ((soc - min_soc) / 100 * cap) / house, "reserve"
    else:
        return none
    if hrs > 36:
        return none
    mins = hrs * 60
    text = {"full": f"full in {fmt_dur(mins)}", "empty": f"lasts ~{fmt_dur(mins)}", "reserve": f"covers ~{fmt_dur(mins)} of use"}[kind]
    return {"kind": kind, "minutes": int(round(mins)), "at": hm(now + mins * 60), "text": text}


def why(live) -> str:
    """One plain-English sentence for what the system is doing right now and why."""
    if not live:
        return "Waiting for your inverter to report in."
    a = live.get("action") or "hold"
    solar = float(live.get("solar_kw") or 0); house = float(live.get("house_kw") or 0)
    grid = float(live.get("grid_kw") or 0); imp = live.get("import_rate_p"); exp = live.get("export_rate_p")
    if imp is not None and imp < 0:
        return f"The grid is paying you {abs(imp):.1f}p/kWh to use power right now."
    if a == "charge" and solar > house + 0.05:
        return "Free solar is charging your battery for later."
    if a == "charge":
        return f"Charging from the grid while power is cheap ({imp:.1f}p)." if imp is not None else "Charging while power is cheap."
    if a == "export" or grid < -0.3:
        return f"Selling surplus to the grid at {exp:.1f}p/kWh." if exp is not None else "Selling surplus to the grid."
    if a == "discharge" and imp is not None and imp >= 25:
        return f"Peak rates ({imp:.0f}p) — your battery is covering the home so you don't buy dear power."
    if a == "discharge":
        return "Powering your home from stored energy instead of buying from the grid."
    if solar > house + 0.05:
        return "Solar is covering your whole home."
    return "Holding steady — nothing worth doing right now."


def why_short(live) -> str:
    """Same logic as why(), squeezed to one line (<= 30 chars) for a round display."""
    if not live:
        return "waiting for inverter"
    a = live.get("action") or "hold"
    solar = float(live.get("solar_kw") or 0); house = float(live.get("house_kw") or 0); grid = float(live.get("grid_kw") or 0)
    imp, exp = live.get("import_rate_p"), live.get("export_rate_p")
    if imp is not None and imp < 0:
        return "paid to use power"
    if a == "charge" and solar > house + 0.05:
        return "free solar filling battery"
    if a == "charge":
        return f"cheap grid charge - {imp:.0f}p" if imp is not None else "charging while cheap"
    if a == "export" or grid < -0.3:
        return f"selling surplus - {exp:.0f}p" if exp is not None else "selling surplus"
    if a == "discharge" and imp is not None and imp >= 25:
        return "battery beating peak rates"
    if a == "discharge":
        return "running on stored energy"
    if solar > house + 0.05:
        return "solar covers your home"
    return "holding steady"


def cover(live):
    """What is powering the home right now, in whole percent: solar / battery / grid."""
    if not live:
        return None
    house = float(live.get("house_kw") or 0)
    if house < 0.05:
        return None
    solar = max(0.0, float(live.get("solar_kw") or 0)); bat = float(live.get("battery_kw") or 0)
    s = min(solar, house)
    b = min(max(0.0, -bat), max(0.0, house - s))
    g = max(0.0, house - s - b)
    tot = s + b + g
    if tot <= 0:
        return None
    ps, pb = round(100 * s / tot), round(100 * b / tot)
    return {"solar": ps, "battery": pb, "grid": max(0, 100 - ps - pb)}


def cheapest(p, t0, n):
    """Cheapest contiguous n-slot window over prices p (slot 0 starts at t0)."""
    if len(p) < n or n < 1:
        return None
    best = min(range(len(p) - n + 1), key=lambda i: sum(p[i:i + n]))
    return {"start": t0 + best * STEP, "end": t0 + (best + n) * STEP, "avg": round(sum(p[best:best + n]) / n, 2)}


def rates_block(slots):
    p = [round(float(s["p"]), 2) for s in slots[:48]]
    t0 = to_epoch(slots[0]["t"])
    pk_i = max(range(len(p)), key=lambda i: p[i])
    return {"t0": t0, "step": STEP, "p": p,
            "cheap": cheapest(p, t0, 8),                # cheapest 4 h — car charging
            "topup": cheapest(p, t0, 3),                # cheapest 1.5 h — appliances / quick top-up
            "peak": {"at": t0 + pk_i * STEP, "p": p[pk_i]}}


def timeline(slots, n=24) -> str:
    return "".join(ACT_CHAR.get(s.get("action"), "h") for s in slots[:n])


def changes(slots, limit=4):
    out, prev = [], (slots[0].get("action") if slots else None)
    for s in slots[1:]:
        if s.get("action") != prev:
            out.append({"hm": s["hm"], "a": s.get("action"), "p": round(float(s["p"]), 1), "reason": s.get("reason", "")})
            prev = s.get("action")
            if len(out) >= limit:
                break
    return out


def insights(live, rates, e, now, soc=None):
    """Up to 4 short, specific, useful lines — ranked. All deterministic (no LLM): every claim is checkable."""
    out = []
    p = rates["p"]; t0 = rates["t0"]
    cur = (live or {}).get("import_rate_p")
    if cur is None:
        cur = p[0]
    neg = [i for i, x in enumerate(p) if x < 0]
    if cur < 0:
        out.append((1, "You're being paid to use power right now — run everything you can."))
    elif neg and t0 + neg[0] * STEP > now:
        out.append((1, f"Negative prices from {hm(t0 + neg[0] * STEP)} — you'll be paid to use power."))
    tp, cw = rates["topup"], rates["cheap"]
    if tp:
        if tp["start"] <= now < tp["end"]:
            out.append((2, f"Cheap power right now until {hm(tp['end'])} ({tp['avg']:.1f}p) — good time for appliances."))
        elif tp["start"] > now:
            pct = int(round((1 - tp["avg"] / cur) * 100)) if cur > 0 and tp["avg"] < cur else 0
            t = f"Best time for appliances: {hm(tp['start'])}–{hm(tp['end'])} at {tp['avg']:.1f}p"
            out.append((2, t + (f" — {pct}% cheaper than now." if pct >= 20 else ".")))
    pk = rates["peak"]
    if pk["at"] > now and pk["p"] >= max(20.0, 1.3 * cur):
        soc = (live or {}).get("soc", soc)
        tail = "" if soc is None else (" — your battery will cover it." if soc >= 40 else " — top your battery up before then.")
        out.append((3, f"Peak at {hm(pk['at'])} · {pk['p']:.0f}p" + (tail or ".")))
    if cw and cw["start"] > now and (not tp or cw["start"] != tp["start"]):
        out.append((4, f"Car charging: cheapest 4-hour slot starts {hm(cw['start'])} (avg {cw['avg']:.1f}p)."))
    if live and live.get("value_today") is not None:
        made = float(live["value_today"]); bill = float(live.get("bill_today") or 0)
        out.append((5, f"Your system has made £{made:.2f} today" + (f" against a £{bill:.2f} grid bill." if bill > 0 else " and you're in credit.")))
    if e["kind"] in ("full", "empty") and e["at"]:
        out.append((6, f"Battery {'full' if e['kind'] == 'full' else 'empty'} around {e['at']} at the current rate."))
    out.sort(key=lambda x: x[0])
    seen, res = set(), []
    for _, t in out:
        if t not in seen:
            seen.add(t); res.append(t)
    return res[:4]


def history(rows, now, step=900):
    """Regular grid of soc % and solar (kW x10) — -1 = no data. Window grows with what we have (6..24 h)."""
    if not rows:
        return {"t0": (now // step) * step, "step": step, "hours": 0, "soc": [], "pv": []}
    first = int(rows[0]["b"])
    hours = int(min(24, max(6, math.ceil((now - first) / 3600) + 1)))
    n = hours * 3600 // step
    t0 = (now // step) * step - (n - 1) * step
    soc, pv = [-1] * n, [-1] * n
    for r in rows:
        i = (int(r["b"]) - t0) // step
        if 0 <= i < n:
            soc[i] = int(round(r["soc"])) if r["soc"] is not None else -1
            pv[i] = int(round(float(r["pv"] or 0) * 10)) if r["pv"] is not None else -1
    return {"t0": t0, "step": step, "hours": hours, "soc": soc, "pv": pv}


def charge_target(windows, now):
    """Target SoC of the next (or current) planned charge window, else None — drives 'CHARGING -> 90%'."""
    for w in windows or []:
        if w.get("mode") == "charge" and to_epoch(w["end"]) > now:
            t = w.get("target_soc")
            return int(round(t)) if t is not None else None
    return None


def build(*, label, region, slots, live, soc_used, tariff, carbon, hist_rows, cap, now=None, windows=None):
    now = int(now or time.time())
    rates = rates_block(slots)
    imp = (live or {}).get("import_rate_p")
    imp = rates["p"][0] if imp is None else imp
    tkey, tword = tier(imp)
    e = eta(live, cap, now)
    lv = live or {}
    return {
        "ts": now, "site": {"label": label, "region": region},
        "live": bool(live),
        "now": {"soc": lv.get("soc", soc_used), "action": lv.get("action") or slots[0].get("action", "hold"),
                "solar_kw": lv.get("solar_kw"), "house_kw": lv.get("house_kw"), "battery_kw": lv.get("battery_kw"), "grid_kw": lv.get("grid_kw"),
                "import_p": imp, "export_p": lv.get("export_rate_p"), "tier": tkey, "tier_word": tword, "cap_kwh": cap,
                "target_soc": charge_target(windows, now)},
        "eta": e, "why": why(live), "why_short": why_short(live), "cover": cover(live),
        "today": {"solar_kwh": lv.get("solar_today_kwh"), "made": lv.get("value_today"), "bill": lv.get("bill_today"),
                  "import": lv.get("import_cost_today"), "export": lv.get("export_earn_today")},
        "rates": rates,
        "timeline": {"t0": rates["t0"], "a": timeline(slots)},
        "changes": changes(slots),
        "hist": history(hist_rows, now),
        "insights": insights(live, rates, e, now, soc_used),
        "tariff": tariff, "carbon": carbon,
    }
