from __future__ import annotations

"""Opt-in sensitivity analyses: flexibility, resource quality, tornado."""
import math
from dataclasses import replace
from typing import Dict

import numpy as np

from .params import (REGIONS, RESOURCE_PRESETS, LDES_PRESETS, FIRM,
                     WorkloadProfile, _sys_with)
from .costs import (cumulative_capacity, wright_law, ldes_annual_cost,
                    gas_backup_cost_scalar)
from .weather import solar_clearsky
from .dispatch import dispatch_ldes_overlay
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




def run_ldes_overlay(region_key="eu", re_target=0.90, target_year=2035,
                     ldes_tech="h2", ldes_hours=None,
                     years=15, grid_steps=15, n_mc=20, seed=42):
    """
    Does cheap long-duration storage profitably displace the residual gas at high RE?

    Solves the standard model (no LDES) for the optimal LFP + overbuild build, then —
    holding that build fixed — runs a 2-storage chronological dispatch (LFP diurnal +
    LDES multi-day, the LDES charged from otherwise-curtailed surplus) over a range of
    LDES energy capacities, and reports delivered LCOE for each. LDES competes with the
    gas backup for the multi-day firming job. Opt-in & reduced fidelity.

    This is a GREEDY overlay: the LFP/overbuild build is taken from the no-LDES optimum,
    so the LDES benefit shown is a conservative lower bound (joint co-optimisation would
    trim overbuild and add more LDES). Charge (electrolyser) and discharge (turbine)
    power are separate, taken from the LDES preset. Returns a dict.
    """
    cfg = REGIONS[region_key]
    sys = _sys_with(cfg["sys"], grid_steps=grid_steps, n_mc_weather=n_mc)
    ldes = LDES_PRESETS[ldes_tech]
    # Sweep electrolyser (charge) power × storage energy. The turbine (discharge) is
    # always full-size (1.0) so it can firm the load; the question is how big an
    # electrolyser + store drive the residual (= would-be-blackout if gas-free) → 0.
    charge_set = [0.3, 0.5, 0.75, 1.0]
    storage_set = [48.0, 168.0, 336.0] if ldes_hours is None else list(ldes_hours)
    dis_pow = 1.0
    n = sys.project_lifetime_yr

    print(f"\n[LDES overlay] {cfg['label']} | {re_target:.0%} RE | {ldes.name} | "
          f"{target_year} — solving no-LDES build, then 2-storage overlay …")
    res = run_simulation(
        solar=cfg["solar"], wind=cfg["wind"], battery=cfg["battery"], gas=cfg["gas"],
        smr=cfg["smr"], sys=sys, workload=FIRM, mean_irr=cfg["mean_irr"],
        mean_wind_ms=cfg["mean_wind_ms"], years=years, reliabilities=[re_target],
        n_cost_mc=10, seed=seed)
    yrs = res["years"]; i = int(target_year - yrs[0])
    sc = res["scenarios"][re_target]
    C_sol, C_win, B_lfp = sc["opt_csol"][i], sc["opt_cwin"][i], sc["opt_B"][i]
    gen_cost, lfp_cost = float(sc["opt_cg"][i]), float(sc["opt_cs"][i])  # fixed build

    batt = cfg["battery"]; gas = cfg["gas"]
    # Learning (conservative, IRENA/IEA): only the ELECTROLYSER (charge kit) is taken
    # to learn; the H2 turbine (mature) and the storage vessels are held flat. The LDES
    # preset's learning_rate/trajectory are tuned to a realistic ~30–35% electrolyser
    # decline by 2035, not an aggressive collapse.
    cum_ldes = cumulative_capacity(ldes, years)
    ch_capex = float(wright_law(ldes.capex_kw_today, ldes.cumulative_gwh_2025,
                                cum_ldes, ldes.learning_rate)[i])      # electrolyser
    e_capex = ldes.capex_kwh_today                                     # storage (flat)
    dis_capex = (ldes.discharge_capex_kw if ldes.discharge_capex_kw is not None
                 else ldes.capex_kw_today)                             # turbine (flat)
    lfp_pow = 1.0 if B_lfp <= 4.0 else min(1.0, 4.0 / B_lfp)

    wkw = dict(wind_solar_corr=sys.wind_solar_corr, syn_loading=sys.syn_loading,
               syn_persistence=sys.syn_persistence, cloud_ar1=sys.cloud_ar1,
               wind_ar1=sys.wind_ar1, wind_daily_share=sys.wind_daily_share,
               wind_seasonal_amp=sys.wind_seasonal_amp)
    clearsky = solar_clearsky(cfg["mean_irr"])
    rng = np.random.default_rng(seed + 7)

    # Candidate (charge_pow, storage_h) grid + a no-LDES baseline (B=0).
    cand = [(0.0, 0.0)] + [(c, s) for c in charge_set for s in storage_set]
    ch_arr = np.array([c for c, _ in cand], dtype=float)
    B_arr = np.array([s for _, s in cand], dtype=float)
    gas_frac, gas_peak, _lfp_efc, ldes_efc = dispatch_ldes_overlay(
        clearsky, cfg["mean_wind_ms"], rng, wkw, C_sol, C_win, B_lfp,
        lfp_pow, batt.roundtrip_efficiency, B_arr, ch_arr, dis_pow,
        ldes.roundtrip_efficiency, n_mc)

    rows = []
    for k, (c, s) in enumerate(cand):
        ldes_cost = 0.0 if s <= 0 else ldes_annual_cost(
            ldes, s, e_capex, ch_capex, dis_capex, c, dis_pow, ldes.wacc, n,
            float(ldes_efc[k])) / 8760.0
        gas_cost = gas_backup_cost_scalar(
            float(gas_frac[k]), gas, i, gas.wacc,
            gas_peak=float(np.clip(gas_peak[k], 0.05, 1.0)))
        rows.append({"charge": c, "h": s, "gas_frac": float(gas_frac[k]),
                     "ldes_cost": ldes_cost, "gas_cost": gas_cost,
                     "total": gen_cost + lfp_cost + ldes_cost + gas_cost})
    base = rows[0]
    best = min(rows, key=lambda r: r["total"])
    # smallest config (by total cost) that gets the residual under 1% of load
    firm_rows = [r for r in rows if r["h"] > 0 and r["gas_frac"] < 0.01]
    near_firm = min(firm_rows, key=lambda r: r["total"]) if firm_rows else None

    print(f"  Fixed build: {C_sol:.1f}× solar + {C_win:.1f}× wind + {B_lfp:.0f}h LFP "
          f"(no-LDES optimum). Storage {e_capex:.0f} $/kWh (flat), electrolyser "
          f"{ch_capex:.0f} $/kW (learned from {ldes.capex_kw_today:.0f}), turbine "
          f"{dis_capex:.0f} $/kW; RTE {ldes.roundtrip_efficiency:.0%}.")
    print(f"  Residual gas % (= would-be unserved if gas-free)  |  delivered $/MWh")
    hdr = f"    {'elec\\\\stor':>10}" + "".join(f"{int(s):>6}h" for s in storage_set)
    print(hdr); print("    " + "─" * (len(hdr) - 4))
    print(f"    {'no LDES':>10}  gas {base['gas_frac']*100:4.1f}%  →  ${base['total']:.0f}/MWh")
    for c in charge_set:
        cells = ""
        for s in storage_set:
            r = next(x for x in rows if x["charge"] == c and x["h"] == s)
            cells += f"{r['gas_frac']*100:4.1f}%/{r['total']:4.0f}"
        print(f"    {c:>9.2f}MW {cells}")
    print(f"  → Lowest cost: elec {best['charge']:.2f}MW + {best['h']:.0f}h → "
          f"${best['total']:.0f}/MWh, gas {best['gas_frac']*100:.1f}% "
          f"(vs no-LDES ${base['total']:.0f}/MWh @ {base['gas_frac']*100:.1f}% gas).")
    if near_firm:
        print(f"  → Near gas-free (<1% residual): elec {near_firm['charge']:.2f}MW + "
              f"{near_firm['h']:.0f}h → ${near_firm['total']:.0f}/MWh "
              f"(+${near_firm['total']-base['total']:.0f} for the last ~"
              f"{base['gas_frac']*100:.0f}% of firming).")
    else:
        print("  → No swept config drives the residual below 1%: a bigger electrolyser "
              "/ store (or higher discharge power) is needed for a gas-free system.")
    print("  (Greedy overlay: LFP/overbuild fixed from the no-LDES optimum; only the "
          "electrolyser is assumed to learn; reduced fidelity. cells = gas%/$tot.)\n")
    return {"region": cfg["label"], "re_target": re_target, "target_year": target_year,
            "ldes_tech": ldes.name, "build": (C_sol, C_win, B_lfp), "rows": rows,
            "base": base, "best": best, "near_firm": near_firm}
