from __future__ import annotations

"""Opt-in sensitivity analyses: flexibility, resource quality, tornado."""
import math
from dataclasses import replace
from typing import Dict

import numpy as np
from scipy.optimize import minimize

from .params import (REGIONS, RESOURCE_PRESETS, LDES_PRESETS, GAS_H2, FIRM,
                     WorkloadProfile, _sys_with)
from .costs import (cumulative_capacity, wright_law, rewacc_lcoe, crf,
                    h2_system_cost_split)
from .weather import solar_clearsky, generate_weather_year
from .dispatch import dispatch_ldes_overlay, dispatch_h2_vec
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
        # Carbon-price introduction/shock — the lever that matters most where the base
        # carbon price is low (the US baseline is $0). "low" overrides ADD $40/tCO₂ to the
        # base (raising gas → helping RE, i.e. a lower gap); "high" is the base price.
        ("Carbon +$40 / base",
         dict(gas=replace(g, carbon_price_today=g.carbon_price_today + 40.0)),
         dict(gas=g)),
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
    Self-produced green H₂ vs buying it: how much of a high-RE datacenter's firming can
    it make for itself from RE overcapacity, and is that cheaper than purchasing H₂?

    Assumption (per design choice): **the firming turbine always has fuel** — it burns
    self-produced H₂ when the store has it and **purchased green H₂ (market) otherwise**.
    So there are *no blackouts*; the rare deep lulls that would drain the store become a
    *cost* (occasional expensive market H₂), not a reliability failure. Everything here
    is **zero-carbon** (green H₂ either way) — this is a fully-firm, gas-free datacenter.

    Method: solve the standard model (no LDES) for the optimal LFP+overbuild build, then
    — holding it fixed — run the 2-storage chronological dispatch (LFP diurnal +
    self-produced-H₂ multi-day, charged from otherwise-curtailed surplus) over an
    electrolyser-power × storage grid. Cost = generation + LFP (fixed) + a firming
    turbine (always) + electrolyser + H₂ storage + **purchased green H₂** for the
    residual the store could not cover. Greedy overlay (LFP/overbuild fixed → the
    self-production benefit is a conservative lower bound); reduced fidelity.
    """
    cfg = REGIONS[region_key]
    sys = _sys_with(cfg["sys"], grid_steps=grid_steps, n_mc_weather=n_mc)
    ldes = LDES_PRESETS[ldes_tech]
    # Electrolyser-power × storage-energy grid. Turbine (discharge) is always full-size
    # (1.0) so the load is always firmed (self-produced or purchased H₂). charge=0 is
    # the buy-all-H₂ baseline (a turbine + market H₂, no self-production).
    charge_set = [0.3, 0.5, 0.75, 1.0]
    storage_set = [48.0, 168.0, 336.0] if ldes_hours is None else list(ldes_hours)
    dis_pow = 1.0
    n = sys.project_lifetime_yr

    print(f"\n[LDES overlay] {cfg['label']} | {re_target:.0%} RE | {ldes.name} | "
          f"{target_year} — solving no-LDES build, then self-produced-vs-bought H₂ …")
    res = run_simulation(
        solar=cfg["solar"], wind=cfg["wind"], battery=cfg["battery"], gas=cfg["gas"],
        smr=cfg["smr"], sys=sys, workload=FIRM, mean_irr=cfg["mean_irr"],
        mean_wind_ms=cfg["mean_wind_ms"], years=years, reliabilities=[re_target],
        n_cost_mc=10, seed=seed)
    yrs = res["years"]; i = int(target_year - yrs[0])
    sc = res["scenarios"][re_target]
    C_sol, C_win, B_lfp = sc["opt_csol"][i], sc["opt_cwin"][i], sc["opt_B"][i]
    gen_cost, lfp_cost = float(sc["opt_cg"][i]), float(sc["opt_cs"][i])  # fixed build

    batt = cfg["battery"]
    # Learning (conservative, IRENA/IEA): only the ELECTROLYSER learns; the H2 turbine
    # (mature) and the storage vessels are held flat.
    cum_ldes = cumulative_capacity(ldes, years)
    ch_capex = float(wright_law(ldes.capex_kw_today, ldes.cumulative_gwh_2025,
                                cum_ldes, ldes.learning_rate)[i])      # electrolyser
    e_capex = ldes.capex_kwh_today                                     # storage (flat)
    dis_capex = (ldes.discharge_capex_kw if ldes.discharge_capex_kw is not None
                 else ldes.capex_kw_today)                             # turbine (flat)
    lfp_pow = 1.0 if B_lfp <= 4.0 else min(1.0, 4.0 / B_lfp)
    # Purchased green H₂ through the turbine: ZERO carbon, Lazard LCOH v4.0 (~$5.25/kg
    # ≈ $46/MMBtu) × CCGT-class H₂-turbine heat rate (consistent with the ~0.54 turbine
    # leg of the 0.35 round-trip). $/MWh-electric served from market H₂.
    h2_buy_var = GAS_H2.gas_price_mmbtu * GAS_H2.ccgt_heat_rate + GAS_H2.vom_mwh

    wkw = dict(wind_solar_corr=sys.wind_solar_corr, syn_loading=sys.syn_loading,
               syn_persistence=sys.syn_persistence, cloud_ar1=sys.cloud_ar1,
               wind_ar1=sys.wind_ar1, wind_daily_share=sys.wind_daily_share,
               wind_seasonal_amp=sys.wind_seasonal_amp, wind_v_ci=sys.wind_v_ci,
               wind_v_rated=sys.wind_v_rated, wind_v_cutout=sys.wind_v_cutout)
    clearsky = solar_clearsky(cfg["mean_irr"])
    rng = np.random.default_rng(seed + 7)

    cand = [(0.0, 0.0)] + [(c, s) for c in charge_set for s in storage_set]
    ch_arr = np.array([c for c, _ in cand], dtype=float)
    B_arr = np.array([s for _, s in cand], dtype=float)
    # `resid` = share of load the store could not cover → served by PURCHASED H₂.
    resid, _peak, _lfp_efc, ldes_efc = dispatch_ldes_overlay(
        clearsky, cfg["mean_wind_ms"], rng, wkw, C_sol, C_win, B_lfp,
        lfp_pow, batt.roundtrip_efficiency, B_arr, ch_arr, dis_pow,
        ldes.roundtrip_efficiency, n_mc)

    crf_l = crf(ldes.wacc, n)
    annuity = (1.0 - (1.0 + ldes.wacc) ** (-n)) / ldes.wacc

    def cfg_cost(c, B, efc):
        # turbine is always installed (firms the load); electrolyser + storage scale
        # with the self-production design. Augmentation applies to the storage energy.
        p_cap = (c * ch_capex + dis_pow * dis_capex) * 1e3      # electrolyser + turbine
        e_cap = B * e_capex * 1e3                               # H2 storage
        cost_one = p_cap + e_cap
        deg = ldes.calendar_deg_per_yr + ldes.cycle_deg_per_fec * efc * 365
        npv = cost_one + deg * e_cap * annuity
        return (npv * crf_l + cost_one * ldes.om_frac_capex) / 8760.0

    rows = []
    for k, (c, s) in enumerate(cand):
        cap_cost = cfg_cost(c, s, float(ldes_efc[k]))
        buy_cost = float(resid[k]) * h2_buy_var          # purchased green H2 (0 carbon)
        rows.append({"charge": c, "h": s, "buy_frac": float(resid[k]),
                     "cap_cost": cap_cost, "buy_cost": buy_cost,
                     "total": gen_cost + lfp_cost + cap_cost + buy_cost})
    base = rows[0]                                        # buy ALL firming H2
    best = min(rows, key=lambda r: r["total"])

    print(f"  Fixed build: {C_sol:.1f}× solar + {C_win:.1f}× wind + {B_lfp:.0f}h LFP "
          f"(no-LDES optimum). Storage {e_capex:.0f} $/kWh, electrolyser {ch_capex:.0f} "
          f"$/kW (learned from {ldes.capex_kw_today:.0f}), turbine {dis_capex:.0f} $/kW, "
          f"RTE {ldes.roundtrip_efficiency:.0%}. Market H₂ {h2_buy_var:.0f} $/MWh-e "
          f"(Lazard, zero-carbon). No blackouts — all firming is green H₂.")
    print(f"  Share of load firmed by PURCHASED H₂ (rest self-produced)  |  $/MWh delivered")
    hdr = f"    {'elec/stor':>10}" + "".join(f"{int(s):>7}h" for s in storage_set)
    print(hdr); print("    " + "─" * (len(hdr) - 4))
    print(f"    {'buy all':>10}  {base['buy_frac']*100:4.1f}% → ${base['total']:.0f}/MWh")
    for c in charge_set:
        cells = ""
        for s in storage_set:
            r = next(x for x in rows if x["charge"] == c and x["h"] == s)
            cells += f"{r['buy_frac']*100:4.1f}%/{r['total']:4.0f}"
        print(f"    {c:>8.2f}MW {cells}")
    print(f"  → Cheapest: elec {best['charge']:.2f}MW + {best['h']:.0f}h store → "
          f"${best['total']:.0f}/MWh (buy {best['buy_frac']*100:.1f}% of firming), "
          f"vs ${base['total']:.0f}/MWh buying all H₂ ({base['buy_frac']*100:.1f}%).")
    if best["charge"] <= 0:
        print("  → Self-production does NOT pay here: buying all green H₂ is cheaper "
              "than electrolyser + storage capex (but both are zero-carbon).")
    else:
        print(f"  → Self-producing saves ${base['total']-best['total']:.0f}/MWh vs "
              f"buying all H₂, by trading market fuel for electrolyser+storage capex.")
    print("  (Greedy overlay: LFP/overbuild fixed from the no-LDES optimum; only the "
          "electrolyser learns; reduced fidelity. cells = bought-H₂%/$tot.)\n")
    return {"region": cfg["label"], "re_target": re_target, "target_year": target_year,
            "ldes_tech": ldes.name, "build": (C_sol, C_win, B_lfp), "rows": rows,
            "base": base, "best": best, "h2_buy_var": h2_buy_var}


def run_ldes_joint(region_key="eu", target_year=2035, ldes_tech="h2",
                   h2_price_mults=(1.0, 2.0, 4.0), n_mc=8, years=15, seed=42):
    """
    JOINT co-optimisation of a 100%-firm, gas-free, ZERO-CARBON datacenter:
    minimise delivered LCOE over (C_sol, C_win, B_lfp, electrolyser_power,
    H2_storage_hours), with the residual the system can't self-supply bought as green
    H2 from the market. Unlike the greedy overlay (which fixes the no-LDES build),
    here overbuild, LFP and H2 trade off directly. No RE target — everything is green
    (self-produced or purchased H2), so it is purely a cost minimisation.

    Swept over `h2_price_mults` — multipliers on the market H2 price — to stress the
    deep-lull spike: when continent-wide Dunkelflaute makes bought H2 dear, the optimum
    shifts toward self-production (more electrolyser / storage / overbuild). Multi-start
    Nelder-Mead on a years-vectorised chronological dispatch (fixed weather → smooth
    objective). Opt-in & reduced fidelity. Returns a dict (one entry per multiplier).
    """
    cfg = REGIONS[region_key]; sysp = cfg["sys"]; ldes = LDES_PRESETS[ldes_tech]
    n = sysp.project_lifetime_yr; i = int(target_year - 2025)
    batt = cfg["battery"]

    # ── unit costs at the target year ───────────────────────────────────────────
    lcoe_sol = rewacc_lcoe(wright_law(cfg["solar"].lcoe_today, cfg["solar"].cumulative_gw_2025,
                                      cumulative_capacity(cfg["solar"], years),
                                      cfg["solar"].learning_rate), cfg["solar"])[i]
    lcoe_win = rewacc_lcoe(wright_law(cfg["wind"].lcoe_today, cfg["wind"].cumulative_gw_2025,
                                      cumulative_capacity(cfg["wind"], years),
                                      cfg["wind"].learning_rate), cfg["wind"])[i]
    cum_b = cumulative_capacity(batt, years)
    lfp_kwh = float(wright_law(batt.capex_kwh_today, batt.cumulative_gwh_2025, cum_b, batt.learning_rate)[i])
    lfp_kw  = float(wright_law(batt.capex_kw_today,  batt.cumulative_gwh_2025, cum_b, batt.learning_rate)[i])
    cum_l = cumulative_capacity(ldes, years)
    ch_capex = float(wright_law(ldes.capex_kw_today, ldes.cumulative_gwh_2025, cum_l, ldes.learning_rate)[i])
    e_capex = ldes.capex_kwh_today
    dis_capex = ldes.discharge_capex_kw if ldes.discharge_capex_kw is not None else ldes.capex_kw_today
    h2_buy_base = GAS_H2.gas_price_mmbtu * GAS_H2.ccgt_heat_rate + GAS_H2.vom_mwh
    crf_l = crf(ldes.wacc, n); annuity = (1.0 - (1.0 + ldes.wacc) ** (-n)) / ldes.wacc

    # ── pre-generate fixed weather (Y, 8760) so the NM objective is smooth ───────
    rng = np.random.default_rng(seed)
    sols, wins = [], []
    for _ in range(n_mc):
        s, w = generate_weather_year(
            solar_clearsky(cfg["mean_irr"]), cfg["mean_wind_ms"], rng,
            wind_solar_corr=sysp.wind_solar_corr, syn_loading=sysp.syn_loading,
            syn_persistence=sysp.syn_persistence, cloud_ar1=sysp.cloud_ar1,
            wind_ar1=sysp.wind_ar1, wind_daily_share=sysp.wind_daily_share,
            wind_seasonal_amp=sysp.wind_seasonal_amp, wind_v_ci=sysp.wind_v_ci,
            wind_v_rated=sysp.wind_v_rated, wind_v_cutout=sysp.wind_v_cutout)
        sols.append(s); wins.append(w)
    sol2d = np.array(sols); win2d = np.array(wins)
    CF_sol = float(sol2d.mean()); CF_win = float(win2d.mean())

    lo = np.array([0.0, 0.0, 0.0, 0.0, 0.0])
    # elec ceiling 4.0: the unconstrained optimum reaches ~1.7 MW/MW-load by 2040 and would
    # bind at the old 1.5; headroom removes the artefact (kept in sync with h2system._HI).
    hi = np.array([24.0, 22.0, 24.0, 4.0, 720.0])   # C_sol, C_win, B_lfp, elec, H2-store

    def evaluate(x, mult):
        C_sol, C_win, B_lfp, elec, H2 = [float(np.clip(x[j], lo[j], hi[j])) for j in range(5)]
        lfp_pow = 1.0 if B_lfp <= 4.0 else min(1.0, 4.0 / B_lfp)
        resid, efc = dispatch_h2_vec(sol2d, win2d, C_sol, C_win, B_lfp, lfp_pow,
                                     batt.roundtrip_efficiency, H2, elec, 1.0,
                                     ldes.roundtrip_efficiency)
        # Shared cost formula (single source — also used by h2system.h2_system_trajectory).
        comp = h2_system_cost_split(
            C_sol, C_win, B_lfp, elec, H2, resid, efc,
            batt=batt, ldes=ldes, n=n, CF_sol=CF_sol, CF_win=CF_win,
            lcoe_sol=lcoe_sol, lcoe_win=lcoe_win, om_sol=cfg["solar"].om_frac_lcoe,
            om_win=cfg["wind"].om_frac_lcoe, lfp_kwh=lfp_kwh, lfp_kw=lfp_kw,
            ch_capex=ch_capex, e_capex=e_capex, dis_capex=dis_capex,
            h2_buy=h2_buy_base * mult, crf_l=crf_l, annuity=annuity)
        gen = comp["gen_capex"] + comp["gen_om"]
        lfp = comp["lfp_capex"] + comp["lfp_om"]
        cap = comp["elec_capex"] + comp["store_capex"] + comp["turbine_capex"]
        buy = comp["buy_h2"]
        return gen + lfp + cap + buy, dict(C_sol=C_sol, C_win=C_win, B_lfp=B_lfp,
                                           elec=elec, H2=H2, buy_frac=resid,
                                           gen=gen, lfp=lfp, cap=cap, buy=buy)

    starts = [np.array(s) for s in ([8, 8, 6, 0.4, 48], [12, 11, 6, 0.7, 120],
                                    [15, 14, 6, 1.0, 300], [6, 6, 3, 0.3, 24],
                                    [18, 16, 6, 1.0, 480])]
    out = {}
    for mult in h2_price_mults:
        best_v, best_x = np.inf, starts[0]
        for x0 in starts:
            res = minimize(lambda x: evaluate(x, mult)[0], x0, method="Nelder-Mead",
                           options={"xatol": 0.02, "fatol": 0.02, "maxiter": 1200,
                                    "adaptive": True})
            if res.fun < best_v:
                best_v, best_x = res.fun, res.x
        total, d = evaluate(best_x, mult)
        d["total"] = total; d["mult"] = mult; d["h2_price"] = h2_buy_base * mult
        out[mult] = d

    print(f"\n[LDES joint co-opt] {cfg['label']} | gas-free zero-carbon firm DC | "
          f"{target_year} | {ldes.name} — minimising 24/7 green LCOE, market-H₂ stress …")
    print(f"  Market green H₂ base {h2_buy_base:.0f} $/MWh-e (Lazard). CF sol {CF_sol:.3f} "
          f"wind {CF_win:.3f}. Joint vars: solar× wind× LFP-h electrolyser-MW H₂-store-h.")
    print(f"    {'H₂×':>4}{'$/MWh-e':>9}{'C_sol':>7}{'C_win':>7}{'LFP h':>7}"
          f"{'elec':>7}{'H₂ h':>7}{'buy%':>7}{'LCOE':>8}")
    print("    " + "─" * 64)
    for mult in h2_price_mults:
        d = out[mult]
        print(f"    {mult:>3.0f}×{d['h2_price']:>8.0f}{d['C_sol']:>7.1f}{d['C_win']:>7.1f}"
              f"{d['B_lfp']:>7.1f}{d['elec']:>7.2f}{d['H2']:>7.0f}{d['buy_frac']*100:>6.1f}%"
              f"{d['total']:>8.0f}")
    d1 = out[h2_price_mults[0]]
    print(f"  → At base price: ${d1['total']:.0f}/MWh, fully gas-free & zero-carbon, "
          f"self-producing {100*(1-d1['buy_frac']):.0f}% of firming "
          f"({d1['elec']:.2f} MW electrolyser + {d1['H2']:.0f}h H₂ store).")
    if len(h2_price_mults) > 1:
        dN = out[h2_price_mults[-1]]
        print(f"  → At {h2_price_mults[-1]:.0f}× H₂ spike: ${dN['total']:.0f}/MWh; the optimum "
              f"shifts to {dN['elec']:.2f} MW electrolyser + {dN['H2']:.0f}h store, "
              f"buying only {dN['buy_frac']*100:.1f}% — self-production hedges the spike.")
    print("  (Joint Nelder-Mead on a years-vectorised dispatch; reduced fidelity.)\n")
    return {"region": cfg["label"], "target_year": target_year, "ldes_tech": ldes.name,
            "h2_buy_base": h2_buy_base, "by_mult": out}


def run_firming_comparison(region_key="eu", re_target=0.90, years=15,
                           grid_steps=None, n_mc=None, seed=42):
    """
    Re-optimise the same firm datacenter with two firming choices — natural **gas**
    vs **green H₂** (zero-carbon, purchased) — at a fixed RE target, and return both
    delivered-cost trajectories plus each firming resource's pure reference. Shows the
    cost (and convergence, as EU carbon climbs) of going zero-carbon-firm. Each call
    runs the full optimiser twice; reduced fidelity via grid_steps/n_mc if desired.
    """
    cfg = REGIONS[region_key]
    sysp = cfg["sys"]
    ov = {k: v for k, v in (("grid_steps", grid_steps), ("n_mc_weather", n_mc)) if v}
    if ov:
        sysp = _sys_with(sysp, **ov)
    out = {}
    for tag, gas in (("Gas-backed", cfg["gas"]), ("Green-H₂-firmed", GAS_H2)):
        res = run_simulation(
            solar=cfg["solar"], wind=cfg["wind"], battery=cfg["battery"], gas=gas,
            smr=cfg["smr"], sys=sysp, workload=FIRM, mean_irr=cfg["mean_irr"],
            mean_wind_ms=cfg["mean_wind_ms"], years=years, reliabilities=[re_target],
            seed=seed)
        out[tag] = {"years": res["years"],
                    "lcoe": res["scenarios"][re_target]["opt_delivered"],
                    "firm_ref": res["gas_pure"], "firm_name": res["gas_name"]}
    yrs = out["Gas-backed"]["years"]
    print(f"\n  FIRMING COMPARISON — {cfg['label']} | {re_target:.0%} RE (firm)")
    print(f"    {'Year':<6}{'gas-backed':>12}{'green-H₂':>12}{'Δ (H₂−gas)':>12}")
    for y in [yy for yy in (2025, 2030, 2035, 2040) if yy <= yrs[-1]]:
        k = y - yrs[0]
        g = out["Gas-backed"]["lcoe"][k]; h = out["Green-H₂-firmed"]["lcoe"][k]
        print(f"    {y:<6}{g:>11.1f} {h:>11.1f} {h-g:>+11.1f}")
    print()
    return {"region": cfg["label"], "re_target": re_target, "series": out}
