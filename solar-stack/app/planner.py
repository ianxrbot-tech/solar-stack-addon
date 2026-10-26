"""The money engine — wear-aware battery arbitrage.

Given a home's forward import + export prices and its battery, produce a
half-hourly slot plan that chases actual pounds, not just cheap import:

  * BUY (charge) only when a higher import can be avoided later by more than the
    battery-wear cost — so we don't cycle for a few pence and shred the battery.
  * SELL/USE (discharge) when the current import price beats what the stored
    energy actually cost us (a running cost basis) plus wear — which naturally
    lets the plan cycle MORE than once a day whenever a day has several
    cheap→dear swings, and never when it doesn't pay.
  * Export prices are fetched too, so surplus sold to the grid is valued.

Every kWh cycled carries `wear_cost_p`. This is the guardrail Luke asked for and
the lesson already proven in ian-battery-brain ("the dual measures bill AND
wear"). Output slots + merged windows (for the box) + an economics summary
(what the timing earns today).
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

_LONDON = ZoneInfo("Europe/London")   # slots display in the customer's local time, not UTC
_MODE = {"free": "charge", "charge": "charge", "export": "discharge", "hold": "self_use"}
ROUNDTRIP = 0.90   # ~10% lost charging then discharging


def plan_slots(rates: list[dict], export_rates: list[dict] | None = None, *,
               soc: float, capacity_kwh: float, max_charge_kw: float,
               max_discharge_kw: float, min_soc: float = 10.0, max_soc: float = 100.0,
               wear_cost_p: float = 5.0, margin_p: float = 1.0) -> dict:
    if not rates:
        return {"slots": [], "windows": [], "economics": {}}

    exp_by = {r["start"]: r["p"] for r in (export_rates or [])}
    imports = [r["p"] for r in rates]
    n = len(imports)
    q3 = sorted(imports)[3 * n // 4] if n else 0   # never BUY in the top-price quartile

    # best (highest) import still ahead of slot i — the value of holding charge
    best_future = [0.0] * n
    run = 0.0
    for i in range(n - 1, -1, -1):
        best_future[i] = run           # max import strictly AFTER i
        run = max(run, imports[i])

    soc_kwh = soc / 100 * capacity_kwh
    min_kwh = min_soc / 100 * capacity_kwh
    max_kwh = max_soc / 100 * capacity_kwh
    chg_per = max_charge_kw * 0.5
    dis_per = max_discharge_kw * 0.5
    basis = sum(imports) / n           # cost basis of any pre-existing charge = day avg

    spent = saved = wear = chg_kwh = dis_kwh = export_earn = 0.0
    slots = []
    for i, r in enumerate(rates):
        imp = imports[i]
        exp = exp_by.get(r["start"])
        future = best_future[i]

        if (soc_kwh < max_kwh - 1e-6 and (imp <= 0 or imp < q3)
                and (future - imp - wear_cost_p) > margin_p):
            add = min(chg_per, max_kwh - soc_kwh)
            basis = (basis * soc_kwh + imp * add) / (soc_kwh + add) if soc_kwh + add > 0 else imp
            soc_kwh += add
            spent += imp * add
            chg_kwh += add
            action = "free" if imp <= 0 else "charge"
            reason = ("negative price — you get paid to charge" if imp <= 0
                      else f"cheap {imp:.1f}p — buy now, avoid ~{future:.0f}p later")
        elif soc_kwh > min_kwh + 1e-6 and (imp - basis - wear_cost_p) > margin_p:
            sub = min(dis_per, soc_kwh - min_kwh)
            soc_kwh -= sub
            dis_kwh += sub
            saved += imp * sub * ROUNDTRIP
            wear += wear_cost_p * sub
            if exp is not None:
                export_earn += exp * sub * ROUNDTRIP
            action = "export"
            reason = f"expensive {imp:.1f}p — use the battery (it cost ~{basis:.0f}p)"
        else:
            action = "hold"
            reason = "steady — nothing worth doing"

        slots.append({"t": r["start"].isoformat(), "hm": r["start"].astimezone(_LONDON).strftime("%H:%M"),
                      "p": imp, "export_p": exp, "action": action,
                      "soc_est": round(soc_kwh / capacity_kwh * 100, 1), "reason": reason})

    net = saved - spent - wear
    econ = {
        "charge_kwh": round(chg_kwh, 1), "discharge_kwh": round(dis_kwh, 1),
        "cycles": round(dis_kwh / capacity_kwh, 2) if capacity_kwh else 0,
        "spent_gbp": round(spent / 100, 2), "avoided_gbp": round(saved / 100, 2),
        "wear_gbp": round(wear / 100, 2), "export_gbp": round(export_earn / 100, 2),
        "net_gbp": round(net / 100, 2), "wear_cost_p": wear_cost_p,
    }
    return {"slots": slots, "windows": _merge_windows(slots), "economics": econ,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}


def _merge_windows(slots: list[dict]) -> list[dict]:
    windows = []
    for s in slots:
        mode = _MODE[s["action"]]
        if windows and windows[-1]["mode"] == mode:
            windows[-1]["end"] = s["t"]
            windows[-1]["target_soc"] = s["soc_est"]
        else:
            windows.append({"start": s["t"], "end": s["t"], "mode": mode,
                            "target_soc": s["soc_est"], "reason": s["reason"]})
    for w in windows:
        w["end"] = (dt.datetime.fromisoformat(w["end"]) + dt.timedelta(minutes=30)).isoformat()
    return windows
