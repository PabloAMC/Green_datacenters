from __future__ import annotations

"""Opt-in sensitivity analyses: flexibility, resource quality, tornado."""
import math
from dataclasses import replace
from typing import Dict

import numpy as np

from .params import REGIONS, RESOURCE_PRESETS, FIRM, WorkloadProfile, _sys_with
from .simulate import run_simulation


def _parity_year(yrs, series, baseline):
    """First (interpolated) year `series` drops to/below `baseline`; nan if never."""
    series = np.asarray(series, float); baseline = np.asarray(baseline, float)
    diff = series - baseline
    if diff[0] <= 0:
        return float(yrs[0])
    idxs = np.where(np.diff(np.sign(diff)))[0]
    if len(idxs) == 0:
        return float("nan")
    i = idxs[0]
    frac = diff[i] / (diff[i] - diff[i + 1])
    return float(yrs[i] + frac)


def run_flex_sensitivity(region_key="eu", re_target=0.90, target_year=2030,
                         interruptibles=None, shed_penalties=None, years=15,
                         grid_steps=15, n_mc=15, n_cost_mc=20, seed=42):
    """
    Sweep workload flexibility — interruptible_fraction × shed_penalty (value of
    lost compute) — and record delivered LCOE (at target_year) and parity year vs
    gas, at a fixed RE target. Each point re-runs the dispatch, so this uses
    REDUCED fidelity (coarser grid, fewer MC years). Opt-in & slow.
    """
    if interruptibles is None:
        interruptibles = [0.0, 0.2, 0.4, 0.7, 0.95]
    if shed_penalties is None:
        # straddle the gas variable cost (~$120/MWh in EU 2030) where shedding
        # switches on; above it, premium compute never sheds (→ firm).
        shed_penalties = [25.0, 75.0, 150.0, 300.0, 700.0]
    cfg = REGIONS[region_key]
    # The sweep spans down to 0% flexibility / very high penalty, where inflexible
    # high-RE systems need much larger overbuild than the flexible headline cases —
    # so widen the bounds here (at coarser resolution) to avoid cap-binding.
    base = cfg["sys"]
    sys_lo = _sys_with(base, grid_steps=grid_steps, n_mc_weather=n_mc,
                       c_sol_max=max(base.c_sol_max, 24.0),
                       c_win_max=max(base.c_win_max, 22.0),
                       storage_hours_max=max(base.storage_hours_max, 72.0))
    nI, nP = len(interruptibles), len(shed_penalties)
    L = np.full((nI, nP), np.nan); P = np.full((nI, nP), np.nan)
    gas_at_year = np.nan

    print(f"\n[Flex sweep] {cfg['label']} | {re_target:.0%} RE | {nI}×{nP} points "
          f"| grid={grid_steps}³, MC={n_mc} (reduced fidelity) — this takes a few minutes …")
    for i, ifrac in enumerate(interruptibles):
        for j, pen in enumerate(shed_penalties):
            wl = WorkloadProfile(f"int {ifrac:.0%} @ ${pen:.0f}",
                                 interruptible_fraction=ifrac, shed_penalty_mwh=pen)
            res = run_simulation(
                solar=cfg["solar"], wind=cfg["wind"], battery=cfg["battery"],
                gas=cfg["gas"], smr=cfg["smr"], sys=sys_lo, workload=wl,
                mean_irr=cfg["mean_irr"], mean_wind_ms=cfg["mean_wind_ms"],
                years=years, reliabilities=[re_target], n_cost_mc=n_cost_mc, seed=seed)
            yrs = res["years"]; traj = res["scenarios"][re_target]["opt_delivered"]
            gas = res["gas_pure"]; gas_at_year = float(gas[target_year - yrs[0]])
            L[i, j] = float(traj[target_year - yrs[0]])
            P[i, j] = _parity_year(yrs, traj, gas)
        print(f"  interruptible {ifrac:.0%}: LCOE@{target_year} = "
              f"{np.array2string(L[i], precision=0)}")

    ref_int_idx = int(np.argmin([abs(f - 0.40) for f in interruptibles]))
    ref_pen_idx = int(np.argmin([abs(p - 600.0) for p in shed_penalties]))
    return {
        "region": cfg["label"], "re_target": re_target, "target_year": target_year,
        "interruptibles": interruptibles, "shed_penalties": shed_penalties,
        "lcoe": L, "parity": P, "gas_at_year": gas_at_year,
        "ref_int_idx": ref_int_idx, "ref_pen_idx": ref_pen_idx,
    }


def run_resource_sensitivity(region_key="us", re_target=0.90, years=15, seed=42,
                             grid_steps=None, n_mc=None):
    """
    How much do the headline (conservative) capacity factors matter? Re-runs the
    firm optimisation at each RESOURCE_PRESETS level for `region_key` and prints a
    side-by-side table of delivered LCOE and parity year vs gas, so the reader sees
    how far the parity conclusion moves on a modern, well-sited (good-resource)
    plant. Reuses the full model; opt-in (a couple of minutes). Returns a dict.
    """
    cfg = REGIONS[region_key]
    sys = cfg["sys"]
    overrides = {k: v for k, v in (("grid_steps", grid_steps),
                                   ("n_mc_weather", n_mc)) if v}
    if overrides:
        sys = _sys_with(sys, **overrides)
    levels = RESOURCE_PRESETS[region_key]

    print(f"\n[Resource sweep] {cfg['label']} | {re_target:.0%} RE (firm) | "
          f"levels: {', '.join(levels)} — re-runs the model per level …")
    out = {}
    for name, (mi, mw) in levels.items():
        res = run_simulation(
            solar=cfg["solar"], wind=cfg["wind"], battery=cfg["battery"],
            gas=cfg["gas"], smr=cfg["smr"], sys=sys, workload=FIRM,
            mean_irr=mi, mean_wind_ms=mw, years=years,
            reliabilities=[re_target], seed=seed, grid_ppa=cfg.get("grid_ppa"))
        yrs = res["years"]; traj = res["scenarios"][re_target]["opt_delivered"]
        gas = res["gas_pure"]
        out[name] = {"mean_irr": mi, "mean_wind_ms": mw, "cf": res["sim_cf"],
                     "years": yrs, "lcoe": traj, "gas": gas,
                     "parity_gas": _parity_year(yrs, traj, gas)}

    # ── comparison table ────────────────────────────────────────────────────────
    names = list(levels)
    yrs = out[names[0]]["years"]
    milestones = [y for y in [2025, 2030, 2035, 2040] if y <= yrs[-1]]
    print(f"\n  RESOURCE SENSITIVITY — {cfg['label']} | {re_target:.0%} RE (firm)")
    for name in names:
        cf = out[name]["cf"]; py = out[name]["parity_gas"]
        parity = ">horizon" if math.isnan(py) else f"{py:.0f}"
        print(f"    {name:<8} mean_irr={out[name]['mean_irr']:.1f}  "
              f"mean_wind={out[name]['mean_wind_ms']:.1f} m/s  →  "
              f"CF solar={cf['solar']:.3f}  wind={cf['wind']:.3f}  "
              f"|  parity vs gas: {parity}")
    hdr = "    " + f"{'Year':<6}" + "".join(f"{n+' $/MWh':>16}" for n in names) + f"{'Gas':>9}"
    print("\n" + hdr); print("    " + "─" * (len(hdr) - 4))
    for y in milestones:
        i = y - yrs[0]
        cells = "".join(f"{out[n]['lcoe'][i]:>15.1f} " for n in names)
        print(f"    {y:<6}{cells}{out[names[0]]['gas'][i]:>8.1f}")
    print()
    return out


def run_tornado(region_key="eu", re_target=0.90, target_year=2030, years=15,
                grid_steps=11, n_mc=12, seed=42):
    """
    One-at-a-time sensitivity of the **parity gap** (firm RE delivered LCOE − gas
    LCOE, $/MWh, at `target_year`) to the key assumptions. Negative gap = RE beats
    gas. Each lever is swung low/high around the v5.3 base; the result is the classic
    tornado ranking of what most moves competitiveness. Reduced fidelity (coarser
    grid, fewer MC years) — opt-in & slow; treat magnitudes as indicative.
    """
    cfg = REGIONS[region_key]
    # Widen bounds (as in the flex sweep) so the coarse reduced-fidelity grid does
    # not bind the wind/solar cap and bias the gap, esp. for the low-resource swings.
    base_sys = cfg["sys"]
    sysx = _sys_with(base_sys, grid_steps=grid_steps, n_mc_weather=n_mc,
                     c_sol_max=max(base_sys.c_sol_max, 24.0),
                     c_win_max=max(base_sys.c_win_max, 24.0),
                     storage_hours_max=max(base_sys.storage_hours_max, 72.0))
    s, w, b, g = cfg["solar"], cfg["wind"], cfg["battery"], cfg["gas"]
    base = dict(solar=s, wind=w, battery=b, gas=g,
                mean_irr=cfg["mean_irr"], mean_wind_ms=cfg["mean_wind_ms"])

    def gap(**over):
        p = {**base, **over}
        res = run_simulation(
            solar=p["solar"], wind=p["wind"], battery=p["battery"], gas=p["gas"],
            smr=cfg["smr"], sys=sysx, workload=FIRM, mean_irr=p["mean_irr"],
            mean_wind_ms=p["mean_wind_ms"], years=years, reliabilities=[re_target],
            n_cost_mc=10, seed=seed)
        yi = target_year - res["years"][0]
        return float(res["scenarios"][re_target]["opt_delivered"][yi] - res["gas_pure"][yi])

    base_gap = gap()
    # (label, low-overrides, high-overrides). "low" = the variant expected to lower
    # the gap (help RE); ordering is normalised when plotting anyway.
    levers = [
        ("Gas price ∓25%",
         dict(gas=replace(g, gas_price_mmbtu=g.gas_price_mmbtu * 1.25)),
         dict(gas=replace(g, gas_price_mmbtu=g.gas_price_mmbtu * 0.75))),
        ("RE WACC 4% / 7%",
         dict(solar=replace(s, wacc=0.04), wind=replace(w, wacc=0.04)),
         dict(solar=replace(s, wacc=0.07), wind=replace(w, wacc=0.07))),
        ("Gas WACC 11% / 7%",
         dict(gas=replace(g, wacc=0.11)),
         dict(gas=replace(g, wacc=0.07))),
        ("Wind resource ±10%",
         dict(mean_wind_ms=cfg["mean_wind_ms"] * 1.10),
         dict(mean_wind_ms=cfg["mean_wind_ms"] * 0.90)),
        ("Solar resource ±10%",
         dict(mean_irr=cfg["mean_irr"] * 1.10),
         dict(mean_irr=cfg["mean_irr"] * 0.90)),
        ("Solar learning 35% / 25%",
         dict(solar=replace(s, learning_rate=0.35)),
         dict(solar=replace(s, learning_rate=0.25))),
        ("Battery capex ∓20%",
         dict(battery=replace(b, capex_kwh_today=b.capex_kwh_today * 0.80,
                              capex_kw_today=b.capex_kw_today * 0.80)),
         dict(battery=replace(b, capex_kwh_today=b.capex_kwh_today * 1.20,
                              capex_kw_today=b.capex_kw_today * 1.20))),
        ("Carbon ceiling ±25%",
         dict(gas=replace(g, carbon_price_ceiling=g.carbon_price_ceiling * 1.25)),
         dict(gas=replace(g, carbon_price_ceiling=g.carbon_price_ceiling * 0.75))),
    ]
    print(f"\n[Tornado] {cfg['label']} | {re_target:.0%} RE | gap @ {target_year} "
          f"(base {base_gap:+.1f} $/MWh) | grid={grid_steps}³ MC={n_mc} — a few minutes …")
    rows = []
    for name, lo, hi in levers:
        lo_v, hi_v = gap(**lo), gap(**hi)
        rows.append((name, lo_v, hi_v))
        print(f"  {name:<26} {lo_v:+7.1f} … {hi_v:+7.1f}  (swing {abs(hi_v-lo_v):4.1f})")
    rows.sort(key=lambda r: abs(r[2] - r[1]))
    return {"region": cfg["label"], "re_target": re_target,
            "target_year": target_year, "base": base_gap, "rows": rows}


