from __future__ import annotations

"""End-to-end simulation runner and region orchestration."""
import os
from dataclasses import replace
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import numpy as np

from .params import (SystemParams, BatteryParams, GasParams, SMRParams,
                     GridPPAParams, WorkloadProfile, TechParams,
                     SOLAR, WIND, BATTERY_US, GAS, SMR, ENTERPRISE,
                     SYSTEM, FIRM, REGIONS, MODEL_VERSION, _sys_with, resource_band_for)
from .costs import (cumulative_capacity, wright_law, rewacc_lcoe,
                    smr_trajectory, grid_ppa_trajectory, grid_cfe_trajectory,
                    gas_pure_lcoe, battery_annualised_cost)
from .dispatch import ChronologicalSimulator
from .optimize import optimal_cost_3d, delivered_cost_split
from .reporting import print_summary, export_results, git_commit, config_hash
from .plots import (plot_cost_trajectories, plot_reliability_sensitivity,
                    plot_optimal_mix, plot_component_breakdown, plot_h2_breakdown)
from .h2system import h2_system_trajectory
from .weather import load_weather_traces


# ─────────────────────────────────────────────────────────────────────────────
# 8. SIMULATION RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def _delivered_at_resource(mean_irr, mean_wind_ms, solar, wind, battery, gas, sys_band,
                           workload, seed, Rs, years, lcoe_solar, lcoe_wind,
                           capex_batt_kwh, capex_batt_kw):
    """Least-cost delivered trajectory per RE target at one resource level — the building
    block of the fig1 geographic/siting band. Returns {R: array(years+1)}. No cost-MC
    (the band is a resource range, not a capex CI), so it is cheap."""
    sim = ChronologicalSimulator(sys_band, battery, workload, mean_irr, mean_wind_ms, seed)
    out = {R: np.zeros(years + 1) for R in Rs}
    for R in Rs:
        prev_x = None
        for i in range(years + 1):
            kw = dict(batt=battery, capex_batt_kwh=capex_batt_kwh[i],
                      capex_batt_kw=capex_batt_kw[i], gas=gas, year_index=i, sys=sys_band)
            res = optimal_cost_3d(sim, R, lcoe_solar[i], lcoe_wind[i], prev_x=prev_x, **kw)
            out[R][i] = res[0]
            prev_x = np.array([res[5], res[6], res[7]])
    return out


def run_simulation(
    solar: TechParams       = SOLAR,
    wind: TechParams        = WIND,
    battery: BatteryParams  = BATTERY_US,
    gas: GasParams          = GAS,
    smr: SMRParams          = SMR,
    sys: SystemParams       = SYSTEM,
    workload: WorkloadProfile = ENTERPRISE,
    mean_irr: float         = 5.5,
    mean_wind_ms: float     = 7.0,
    years: int              = 15,
    reliabilities: Optional[List[float]] = None,
    n_cost_mc: int          = 80,
    seed: int               = 0,
    grid_ppa: Optional[GridPPAParams] = None,
    design_p90: bool        = False,
    h2_system: bool         = False,
    weather_years: Optional[list] = None,
    resource_band: Optional[list] = None,
    gas_stress_mult: Optional[float] = None,
) -> Dict:
    if reliabilities is None:
        reliabilities = [0.80, 0.90]

    year_labels = 2025 + np.arange(years + 1)
    cum_sol  = cumulative_capacity(solar,   years)
    cum_win  = cumulative_capacity(wind,    years)
    cum_batt = cumulative_capacity(battery, years)

    # Build the dispatch simulator up front so its (possibly real) capacity factor is
    # known before the LCOE trajectories are levelised.
    sim      = ChronologicalSimulator(sys, battery, workload, mean_irr, mean_wind_ms, seed,
                                      weather_years=weather_years)
    # REAL-WEATHER RE-LEVELING (v6.0). When the dispatch is driven by measured ERA5/NSRDB
    # traces (`weather_years`), the simulated CF is the *real* site CF, which generally
    # differs from the synthetic CF the imported Lazard LCOEs are levelised at. A panel /
    # turbine costs the same $/kW wherever it sits, so holding that capital fixed we
    # re-express each technology's LCOE at the real CF:
    #     lcoe_real = lcoe_today · CF_ref / CF_real
    # CF_ref = the synthetic CF for this region's resource (the Lazard-anchored cost basis);
    # CF_real = the mean CF of the supplied weather years. This preserves the v5.5
    # CF-consistency invariant — cost and energy refer to the same plant — at the real
    # site, and propagates through every downstream consumer (the RE-target lines, the
    # gas-free H₂ line, the cost-MC band) because they all derive from `solar`/`wind`.
    if weather_years is not None:
        ref = ChronologicalSimulator(sys, battery, workload, mean_irr, mean_wind_ms, seed)
        solar = replace(solar, lcoe_today=solar.lcoe_today * ref.sol_cf_mean / sim.sol_cf_mean)
        wind  = replace(wind,  lcoe_today=wind.lcoe_today  * ref.win_cf_mean / sim.win_cf_mean)

    lcoe_solar     = wright_law(solar.lcoe_today,        solar.cumulative_gw_2025,    cum_sol,  solar.learning_rate)
    lcoe_wind      = wright_law(wind.lcoe_today,         wind.cumulative_gw_2025,     cum_win,  wind.learning_rate)
    # Per-tech WACC (v5.3): re-express the bundled generation LCOE at each
    # technology's own cost of capital / life (low-risk RE → cheaper than the
    # legacy flat WACC; identity when wacc==LEGACY_WACC).
    lcoe_solar     = rewacc_lcoe(lcoe_solar, solar)
    lcoe_wind      = rewacc_lcoe(lcoe_wind,  wind)
    capex_batt_kwh = wright_law(battery.capex_kwh_today, battery.cumulative_gwh_2025, cum_batt, battery.learning_rate)
    capex_batt_kw  = wright_law(battery.capex_kw_today,  battery.cumulative_gwh_2025, cum_batt, battery.learning_rate)
    lcoe_smr       = smr_trajectory(smr, years)
    gas_pure       = np.array([gas_pure_lcoe(gas, i, gas.wacc) for i in range(years + 1)])
    # Optional "gas-stress" reference baseline: the same gas plant at a stressed fuel
    # price (e.g. AI-demand-driven tightening). A transparency line — the headline holds
    # gas fuel flat (US $4/MMBtu, $0 carbon), which makes US gas a very low, very stable
    # floor; this shows how the comparison shifts if that assumption is relaxed. Reference
    # only, never part of the optimisation. (§7 / §12.)
    gas_stress = None
    if gas_stress_mult is not None:
        gas_s = replace(gas, gas_price_mmbtu=gas.gas_price_mmbtu * gas_stress_mult)
        gas_stress = np.array([gas_pure_lcoe(gas_s, i, gas_s.wacc) for i in range(years + 1)])

    rng_cost = np.random.default_rng(seed + 1)

    results = {
        "years": year_labels, "lcoe_solar": lcoe_solar, "lcoe_wind": lcoe_wind,
        "capex_batt_kwh": capex_batt_kwh, "gas_pure": gas_pure, "lcoe_smr": lcoe_smr,
        "gas_name": gas.name, "smr_name": smr.name, "workload_name": workload.name,
        "wind_solar_corr": sys.wind_solar_corr, "scenarios": {},
        "sim_cf": {"solar": sim.sol_cf_mean, "wind": sim.win_cf_mean},
    }
    # Run provenance (deterministic — no wall-clock): code state + inputs, so any
    # exported figure/table traces to exact inputs and two identical runs match.
    _cfg = {
        "solar": [solar.lcoe_today, solar.learning_rate, solar.wacc],
        "wind": [wind.lcoe_today, wind.learning_rate, wind.wacc],
        "battery": [battery.capex_kwh_today, battery.capex_kw_today, battery.wacc],
        "gas": [gas.gas_price_mmbtu, gas.carbon_price_today,
                gas.carbon_price_ceiling, gas.carbon_trajectory, gas.wacc],
        "resource": [mean_irr, mean_wind_ms],
        "weather": [sys.wind_solar_corr, sys.syn_loading, sys.syn_persistence,
                    sys.n_sites, sys.site_synoptic_corr,
                    getattr(sys, "site_local_corr", 0.4)],
        "grid": [sys.grid_steps, sys.n_mc_weather, sys.c_sol_max, sys.c_win_max,
                 sys.storage_hours_max],
        "seed": seed, "years": years, "workload": workload.name,
    }
    results["provenance"] = {
        "model_version": MODEL_VERSION,
        "git_commit": git_commit(),
        "weather_source": "reanalysis" if weather_years is not None else "synthetic",
        "n_weather_years": (len(weather_years) if weather_years is not None
                            else sys.n_mc_weather),
        "seed": seed,
        "grid_steps": sys.grid_steps,
        "n_sites": sys.n_sites,
        "site_synoptic_corr": sys.site_synoptic_corr,
        "config_sha256": config_hash(_cfg),
    }
    if grid_ppa is not None:
        results["grid_ppa"] = grid_ppa_trajectory(grid_ppa, lcoe_solar)
        results["grid_ppa_name"] = grid_ppa.name
        results["grid_cfe"] = grid_cfe_trajectory(grid_ppa, lcoe_solar)
        results["grid_cfe_name"] = "Grid + 24/7 CFE"
    if gas_stress is not None:
        results["gas_stress"] = gas_stress
        results["gas_stress_name"] = f"{gas.name} (stressed fuel ×{gas_stress_mult:g})"

    for R in reliabilities:
        n_yr = years + 1
        opt_t  = np.zeros(n_yr); opt_l  = np.zeros(n_yr); opt_h  = np.zeros(n_yr)
        opt_cg = np.zeros(n_yr); opt_cs = np.zeros(n_yr); opt_cb = np.zeros(n_yr)
        opt_cp = np.zeros(n_yr); opt_shed = np.zeros(n_yr)
        opt_csol = np.zeros(n_yr); opt_cwin = np.zeros(n_yr); opt_B = np.zeros(n_yr)
        opt_re = np.zeros(n_yr)   # achieved renewable fraction at the optimum (firm: 1−f_gas)
        gen_cx = np.zeros(n_yr); gen_om = np.zeros(n_yr)
        bat_cx = np.zeros(n_yr); bat_om = np.zeros(n_yr)
        gas_cx = np.zeros(n_yr); gas_op = np.zeros(n_yr); gas_cb = np.zeros(n_yr)
        tiebreak_diag = {}   # continuity tie-break observability (§8.4)

        for i in range(n_yr):
            kw = dict(batt=battery, capex_batt_kwh=capex_batt_kwh[i],
                      capex_batt_kw=capex_batt_kw[i], gas=gas, year_index=i, sys=sys)

            prev_x = np.array([opt_csol[i-1], opt_cwin[i-1], opt_B[i-1]]) if i > 0 else None

            (opt_t[i], opt_cg[i], opt_cs[i], opt_cb[i], opt_cp[i],
             opt_csol[i], opt_cwin[i], opt_B[i], opt_shed[i]) = optimal_cost_3d(
                sim, R, lcoe_solar[i], lcoe_wind[i], prev_x=prev_x, diag=tiebreak_diag, **kw)

            # Per-factor capex/opex decomposition at the optimum (central unit costs)
            split = delivered_cost_split(
                sim, opt_csol[i], opt_cwin[i], opt_B[i], solar, wind,
                lcoe_solar[i], lcoe_wind[i], battery, capex_batt_kwh[i],
                capex_batt_kw[i], gas, i, sys)
            gen_cx[i] = split["gen_capex"]; gen_om[i] = split["gen_om"]
            bat_cx[i] = split["batt_capex"]; bat_om[i] = split["batt_om"]
            gas_cx[i] = split["gas_capex"]; gas_op[i] = split["gas_opex"]
            gas_cb[i] = split["gas_carbon"]
            # Achieved renewable fraction at the optimum, on the FIRM convention:
            # gas serves the would-be-shed energy too (gas + drop), matching the
            # constraint's firm branch. Exact for the firm default and for
            # firm-collapsing premium workloads; for genuinely shed-economic cheap
            # workloads it understates served-RE (conservative). Pre-v5.8 the drop
            # term was omitted, overstating RE for interruptible workloads. Lets
            # callers detect when a high RE target was infeasible at a poor-resource
            # site (the returned build is then the penalty optimum, < target).
            # v5.9: read off EXACT dispatch at the optimum (cached), not the surface.
            _st = sim.exact_point(opt_csol[i], opt_cwin[i], opt_B[i])
            opt_re[i] = 1.0 - (_st["gas_mean"] + _st["drop_mean"])

            # Cost uncertainty: hold optimal (C_sol, C_win, B) fixed from the
            # central solve; re-evaluate cost formula with lognormal-perturbed
            # unit costs. This correctly isolates capex uncertainty from the
            # capacity decision — re-running Nelder-Mead per draw would be
            # O(n_mc × n_starts × 500 iterations) = prohibitive.
            C_sol_opt = opt_csol[i]; C_win_opt = opt_cwin[i]; B_opt = opt_B[i]
            r   = battery.wacc; n_lf = sys.project_lifetime_yr   # battery MC uses battery WACC
            eff_fec_opt = _st["fec_mean"]   # exact at the optimum (v5.9, cached)
            # Gas + shed-penalty cost are independent of the perturbed (solar/wind/
            # battery) capex, so hold them fixed at the optimiser's values.
            c_gas_pen_opt = opt_cb[i] + opt_cp[i]
            gen_s_opt  = C_sol_opt * sim.sol_cf_mean
            gen_w_opt  = C_win_opt * sim.win_cf_mean

            sigma_sol  = solar.uncertainty_sigma
            sigma_win  = wind.uncertainty_sigma
            sigma_batt = battery.uncertainty_sigma
            draws_arr  = np.empty(n_cost_mc)
            z_sol  = rng_cost.normal(0, sigma_sol,  n_cost_mc)
            z_win  = rng_cost.normal(0, sigma_win,  n_cost_mc)
            z_bkwh = rng_cost.normal(0, sigma_batt, n_cost_mc)
            z_bkw  = rng_cost.normal(0, sigma_batt, n_cost_mc)
            s_sols  = lcoe_solar[i]     * np.exp(z_sol  - sigma_sol**2/2)
            s_wins  = lcoe_wind[i]      * np.exp(z_win  - sigma_win**2/2)
            s_bkwhs = capex_batt_kwh[i] * np.exp(z_bkwh - sigma_batt**2/2)
            s_bkws  = capex_batt_kw[i]  * np.exp(z_bkw  - sigma_batt**2/2)
            for mc in range(n_cost_mc):
                c_gen_mc  = gen_s_opt * s_sols[mc] + gen_w_opt * s_wins[mc]
                c_stor_mc = battery_annualised_cost(
                    battery, B_opt, s_bkwhs[mc], s_bkws[mc], r, n_lf,
                    effective_fec_per_day=eff_fec_opt) / 8760.0
                draws_arr[mc] = c_gen_mc + c_stor_mc + c_gas_pen_opt
            opt_l[i], opt_h[i] = np.percentile(draws_arr, [10, 90])

        # Optional robustness-design series: re-optimise against the 1-in-10 (P90)
        # weather surface, so the RE target is met even in a bad weather year. A
        # firm, always-on datacenter arguably *should* size against tail weather,
        # not the mean. Opt-in: no extra optimiser calls when design_p90=False.
        opt_t_p90    = np.zeros(n_yr)
        opt_csol_p90 = np.zeros(n_yr); opt_cwin_p90 = np.zeros(n_yr); opt_B_p90 = np.zeros(n_yr)
        if design_p90:
            for i in range(n_yr):
                kw = dict(batt=battery, capex_batt_kwh=capex_batt_kwh[i],
                          capex_batt_kw=capex_batt_kw[i], gas=gas, year_index=i, sys=sys)
                prev_x = (np.array([opt_csol_p90[i-1], opt_cwin_p90[i-1], opt_B_p90[i-1]])
                          if i > 0 else None)
                (opt_t_p90[i], _, _, _, _, opt_csol_p90[i], opt_cwin_p90[i],
                 opt_B_p90[i], _) = optimal_cost_3d(
                    sim, R, lcoe_solar[i], lcoe_wind[i],
                    use_p90=True, prev_x=prev_x, **kw)

        if tiebreak_diag.get("fired"):
            d = tiebreak_diag["deltas"]
            print(f"  [diag] {R:.0%} RE: continuity tie-break fired {tiebreak_diag['fired']}/"
                  f"{n_yr-1} yrs; cost change {min(d):+.2f}…{max(d):+.2f} $/MWh "
                  f"(≤1% bound; suppresses cosmetic mix flips, §8.4).")

        scen = {
            "opt_delivered": opt_t, "opt_delivered_low": opt_l, "opt_delivered_high": opt_h,
            "opt_cg": opt_cg, "opt_cs": opt_cs, "opt_cb": opt_cb, "opt_cp": opt_cp,
            "opt_shed": opt_shed,
            "gen_capex": gen_cx, "gen_om": gen_om, "batt_capex": bat_cx, "batt_om": bat_om,
            "gas_capex": gas_cx, "gas_opex": gas_op, "gas_carbon": gas_cb,
            "opt_csol": opt_csol, "opt_cwin": opt_cwin, "opt_B": opt_B, "opt_re": opt_re,
        }
        if design_p90:
            scen["opt_delivered_p90"] = opt_t_p90
            scen["opt_csol_p90"] = opt_csol_p90
            scen["opt_cwin_p90"] = opt_cwin_p90
            scen["opt_B_p90"] = opt_B_p90
        results["scenarios"][R] = scen

    # Optional: fully-optimised gas-free green-H₂ system (no RE target — minimise LCOE
    # over solar/wind/LFP/electrolyser/H₂-storage; residual bought as green H₂). Feeds
    # the fig1 H₂ line and the fig6 breakdown.
    if h2_system:
        results["h2_system"] = h2_system_trajectory(
            solar, wind, battery, mean_irr, mean_wind_ms, sys, years, seed=seed,
            weather_years=weather_years)

    # Geographic / siting band: re-solve each trajectory (every RE target + the H₂ system)
    # at a poor and a good site for the region, and store the min–max envelope. This is the
    # spread from *where* you build — the dominant uncertainty for an off-grid siting
    # decision — and it is what fig1 shades (a resource RANGE, not a capex CI; the capex
    # P10–P90 stays in opt_delivered_low/high). Skipped under supplied reanalysis weather
    # (the resource is then fixed) and at reduced MC since the band is illustrative.
    if resource_band and weather_years is None:
        sys_band = _sys_with(sys, n_mc_weather=min(sys.n_mc_weather, 25))
        deliv = [_delivered_at_resource(mi, mw, solar, wind, battery, gas, sys_band,
                                        workload, seed, reliabilities, years, lcoe_solar,
                                        lcoe_wind, capex_batt_kwh, capex_batt_kw)
                 for (mi, mw) in resource_band]
        for R in reliabilities:
            stack = np.stack([d[R] for d in deliv])           # (n_extremes, years+1)
            results["scenarios"][R]["opt_delivered_reslo"] = stack.min(0)   # good site
            results["scenarios"][R]["opt_delivered_reshi"] = stack.max(0)   # poor site
        if h2_system:
            h2b = np.stack([h2_system_trajectory(solar, wind, battery, mi, mw, sys, years,
                                                 seed=seed)["lcoe"] for (mi, mw) in resource_band])
            results["h2_system"]["lcoe_reslo"] = h2b.min(0)
            results["h2_system"]["lcoe_reshi"] = h2b.max(0)

    _enforce_re_monotonicity(results, reliabilities, years)
    return results


def _enforce_re_monotonicity(results, reliabilities, years):
    """The optimal delivered cost MUST be non-decreasing in the renewable target R: a
    looser target's feasible set contains a tighter one's, so the looser optimum can always
    reuse the tighter build and cost no more. In the very flat US cost valley the multi-start
    Nelder–Mead occasionally lands the looser problem on a slightly worse grid node than the
    tighter one (e.g. US 70% came out ~$0.2-1.2/MWh *above* 80% in 2034-36), which would draw
    economically-impossible crossings. Repair it the only way that is provably correct: in any
    (year, R) where a looser target costs more than a tighter one, adopt the tighter target's
    cheaper, still-feasible slice wholesale (cost, build, splits, and bands stay consistent)."""
    Rs = sorted(results["scenarios"])
    if len(Rs) < 2:
        return
    keys = [k for k, v in results["scenarios"][Rs[0]].items()
            if isinstance(v, np.ndarray) and v.shape == (years + 1,)]
    for i in range(years + 1):
        src = Rs[-1]                                   # tightest target = upper bound on cost
        for R in reversed(Rs[:-1]):                    # next-tightest down to loosest
            sc = results["scenarios"][R]
            if sc["opt_delivered"][i] <= results["scenarios"][src]["opt_delivered"][i] + 1e-9:
                src = R                                # genuinely (weakly) cheaper — new bound
            else:
                bsc = results["scenarios"][src]        # violation → adopt the tighter slice
                for k in keys:
                    sc[k][i] = bsc[k][i]


def _nearest_re(results, target: float) -> float:
    """The RE-target scenario closest to `target` that the run actually computed."""
    return min(results["scenarios"].keys(), key=lambda R: abs(R - target))


def run_region(region, solar, wind, battery, gas, smr, sys, workload,
               mean_irr, mean_wind_ms, reliabilities, prefix, seed=0, grid_ppa=None,
               design_p90=False, h2_system=False, weather_years=None, resource_band=None,
               gas_stress_mult=None):
    print(f"\n{'━'*42} {region} {'━'*42}")
    results = run_simulation(
        solar=solar, wind=wind, battery=battery, gas=gas, smr=smr,
        sys=sys, workload=workload, mean_irr=mean_irr,
        mean_wind_ms=mean_wind_ms, reliabilities=reliabilities, seed=seed,
        grid_ppa=grid_ppa, design_p90=design_p90, h2_system=h2_system,
        weather_years=weather_years, resource_band=resource_band,
        gas_stress_mult=gas_stress_mult,
    )
    print_summary(results, region=region)
    export_results(results, region=region, prefix=prefix)
    os.makedirs("figs", exist_ok=True)
    # Breakdown figures: fig4 at 70% RE, fig5 at 85% RE. Pick the nearest available
    # scenario to each target so the choice is robust when the run's RE set does not
    # contain them exactly — previously both silently fell back to 90% (and so the
    # two breakdown figures could end up identical and mislabelled).
    bd1 = _nearest_re(results, 0.70)
    bd2 = _nearest_re(results, 0.85)
    figs = {
        f"{prefix}_fig1_trajectories":  plot_cost_trajectories(results, region),
        f"{prefix}_fig2_reliability":   plot_reliability_sensitivity(results, 2030, region),
        f"{prefix}_fig3_optimal_mix":   plot_optimal_mix(results, region),
        f"{prefix}_fig4_breakdown":     plot_component_breakdown(results, bd1, region),
        f"{prefix}_fig5_breakdown":     plot_component_breakdown(results, bd2, region),
    }
    if "h2_system" in results:   # fig6 = fully-optimised gas-free green-H₂ system breakdown
        figs[f"{prefix}_fig6_h2system"] = plot_h2_breakdown(results, region)
    for name, fig in figs.items():
        fig.savefig(f"figs/{name}.png", dpi=200, bbox_inches="tight")
        plt.close(fig)
    return results


def run_region_cfg(cfg, workload, reliabilities, prefix,
                   sys_overrides=None, seed=42, design_p90=False,
                   mean_irr=None, mean_wind_ms=None, gas=None, h2_system=False,
                   weather_years=None, resource_band=None, gas_stress_mult=None):
    """Run a region BUNDLE (a REGIONS-style dict) with a chosen workload.

    Shared by `run_region_key` (built-in regions) and the `--site` path (a custom
    site loaded from JSON), so a region and a user-supplied site are described and run
    through exactly one code path. `weather_years`, if given, drives the dispatch with
    real reanalysis CF traces instead of the synthetic generator."""
    sys = _sys_with(cfg["sys"], **sys_overrides) if sys_overrides else cfg["sys"]
    label = f"{cfg['label']} ({workload.name})"
    return run_region(label, cfg["solar"], cfg["wind"], cfg["battery"],
                      cfg["gas"] if gas is None else gas,
                      cfg["smr"], sys, workload,
                      cfg["mean_irr"] if mean_irr is None else mean_irr,
                      cfg["mean_wind_ms"] if mean_wind_ms is None else mean_wind_ms,
                      reliabilities, prefix, seed=seed, grid_ppa=cfg.get("grid_ppa"),
                      design_p90=design_p90, h2_system=h2_system,
                      weather_years=weather_years, resource_band=resource_band,
                      gas_stress_mult=gas_stress_mult)


def run_region_key(region_key, workload, reliabilities, prefix,
                   sys_overrides=None, seed=42, design_p90=False,
                   mean_irr=None, mean_wind_ms=None, gas=None, h2_system=False,
                   weather_years=None, resource_band=False, gas_stress_mult=None):
    """Run a region (from REGIONS) with a chosen workload; thin wrapper over run_region_cfg.

    `mean_irr` / `mean_wind_ms` override the region's default resource (used by the
    resource-quality sensitivity); `design_p90` adds the robustness-design series;
    `gas` overrides the firming resource (e.g. green-H2 via --firming h2); `h2_system`
    adds the fully-optimised gas-free H₂ system (fig1 line + fig6); `resource_band=True`
    adds the fig1 geographic/siting band (poor↔good site for the region)."""
    band = resource_band_for(region_key) if resource_band else None
    return run_region_cfg(REGIONS[region_key], workload, reliabilities, prefix,
                          sys_overrides=sys_overrides, seed=seed, design_p90=design_p90,
                          mean_irr=mean_irr, mean_wind_ms=mean_wind_ms, gas=gas,
                          h2_system=h2_system, weather_years=weather_years,
                          resource_band=band, gas_stress_mult=gas_stress_mult)


def run_full_suite():
    """
    Default suite = FIRM (always-on, gas backup sized to 100% of load, nothing ever
    shed → capped opex). This is the right model for any valuable workload: an
    economic shed only happens when compute is worth LESS than the gas variable
    cost of serving it, so premium/AI workloads never shed and collapse to firm.
    The interruptible regime (cheap compute) is explored via `--flex-sweep`.
    """
    # NB: 95% RE is omitted — it is infeasible for a firm, battery-only off-grid system.
    # During a multi-day Dunkelflaute neither sun nor wind produces and the min(1,4/B)
    # battery power cap can't bridge days, so a few % of annual energy always falls to gas;
    # the maximum achievable RE is ≈0.94 (EU) / ≈0.95 (US). Pushing past ~94% requires
    # long-duration storage or H₂ firming — see the --ldes / --firming h2 overlays (fig6).
    reliabilities = [0.70, 0.80, 0.85, 0.90]
    # HEADLINE WEATHER (v6.0). The firm suite is driven by MEASURED ERA5 reanalysis at a
    # single representative data-center market per region — US: ERCOT Texas, EU: France —
    # rather than the synthetic generator. One real site (no spurious geographic smoothing
    # a single datacenter does not get), with its actual hourly cloud / Dunkelflaute / sun↔
    # wind structure and real interannual spread. The imported Lazard LCOEs are re-levelled
    # from their synthetic reference CF to each site's real CF inside run_simulation, so the
    # cost basis stays consistent (§4 CF-invariant). Override with --weather, or drop the
    # weather_years below to fall back to the synthetic headline. The synthetic generator
    # still backs every sensitivity/siting analysis (resource band, tornado, --resource …),
    # which need the resource as a free knob; the resource band is auto-skipped here since
    # the real-weather resource is fixed (one site).
    HEADLINE_WEATHER = {"us": "output/era5/texas.npz", "eu": "output/era5/france.npz"}
    us_weather = load_weather_traces(HEADLINE_WEATHER["us"])
    eu_weather = load_weather_traces(HEADLINE_WEATHER["eu"])
    # gas_stress_mult=1.6: a transparency reference line showing the gas baseline at a
    # 60%-higher fuel price (e.g. AI-demand-driven tightening) — the headline holds gas
    # fuel flat, so this makes the "cheap-gas moat" assumption visible (§7 / §12).
    run_region_key("us", FIRM, reliabilities, prefix="us_firm", seed=42,
                   h2_system=True, resource_band=True, gas_stress_mult=1.6,
                   weather_years=us_weather)
    run_region_key("eu", FIRM, reliabilities, prefix="eu_firm", seed=42,
                   h2_system=True, resource_band=True, gas_stress_mult=1.6,
                   weather_years=eu_weather)
    print("\nDone — figures saved in figs/")


