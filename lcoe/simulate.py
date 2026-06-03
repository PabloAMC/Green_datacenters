from __future__ import annotations

"""End-to-end simulation runner and region orchestration."""
import os
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import numpy as np

from .params import (SystemParams, BatteryParams, GasParams, SMRParams,
                     GridPPAParams, WorkloadProfile, TechParams,
                     SOLAR, WIND, BATTERY_US, GAS, SMR, ENTERPRISE,
                     SYSTEM, FIRM, REGIONS, _sys_with)
from .costs import (cumulative_capacity, wright_law, rewacc_lcoe,
                    smr_trajectory, grid_ppa_trajectory, gas_pure_lcoe,
                    battery_annualised_cost)
from .dispatch import ChronologicalSimulator
from .optimize import optimal_cost_3d, delivered_cost_split
from .reporting import print_summary, export_results
from .plots import (plot_cost_trajectories, plot_reliability_sensitivity,
                    plot_optimal_mix, plot_component_breakdown)


# ─────────────────────────────────────────────────────────────────────────────
# 8. SIMULATION RUNNER
# ─────────────────────────────────────────────────────────────────────────────

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
) -> Dict:
    if reliabilities is None:
        reliabilities = [0.80, 0.90, 0.95]

    year_labels = 2025 + np.arange(years + 1)
    cum_sol  = cumulative_capacity(solar,   years)
    cum_win  = cumulative_capacity(wind,    years)
    cum_batt = cumulative_capacity(battery, years)

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

    sim      = ChronologicalSimulator(sys, battery, workload, mean_irr, mean_wind_ms, seed)
    rng_cost = np.random.default_rng(seed + 1)

    results = {
        "years": year_labels, "lcoe_solar": lcoe_solar, "lcoe_wind": lcoe_wind,
        "capex_batt_kwh": capex_batt_kwh, "gas_pure": gas_pure, "lcoe_smr": lcoe_smr,
        "gas_name": gas.name, "smr_name": smr.name, "workload_name": workload.name,
        "wind_solar_corr": sys.wind_solar_corr, "scenarios": {},
        "sim_cf": {"solar": sim.sol_cf_mean, "wind": sim.win_cf_mean},
    }
    if grid_ppa is not None:
        results["grid_ppa"] = grid_ppa_trajectory(grid_ppa, lcoe_solar)
        results["grid_ppa_name"] = grid_ppa.name

    for R in reliabilities:
        n_yr = years + 1
        opt_t  = np.zeros(n_yr); opt_l  = np.zeros(n_yr); opt_h  = np.zeros(n_yr)
        opt_cg = np.zeros(n_yr); opt_cs = np.zeros(n_yr); opt_cb = np.zeros(n_yr)
        opt_cp = np.zeros(n_yr); opt_shed = np.zeros(n_yr)
        opt_csol = np.zeros(n_yr); opt_cwin = np.zeros(n_yr); opt_B = np.zeros(n_yr)
        gen_cx = np.zeros(n_yr); gen_om = np.zeros(n_yr)
        bat_cx = np.zeros(n_yr); bat_om = np.zeros(n_yr)
        gas_cx = np.zeros(n_yr); gas_op = np.zeros(n_yr); gas_cb = np.zeros(n_yr)

        for i in range(n_yr):
            kw = dict(batt=battery, capex_batt_kwh=capex_batt_kwh[i],
                      capex_batt_kw=capex_batt_kw[i], gas=gas, year_index=i, sys=sys)

            prev_x = np.array([opt_csol[i-1], opt_cwin[i-1], opt_B[i-1]]) if i > 0 else None

            (opt_t[i], opt_cg[i], opt_cs[i], opt_cb[i], opt_cp[i],
             opt_csol[i], opt_cwin[i], opt_B[i], opt_shed[i]) = optimal_cost_3d(
                sim, R, lcoe_solar[i], lcoe_wind[i], prev_x=prev_x, **kw)

            # Per-factor capex/opex decomposition at the optimum (central unit costs)
            split = delivered_cost_split(
                sim, opt_csol[i], opt_cwin[i], opt_B[i], solar, wind,
                lcoe_solar[i], lcoe_wind[i], battery, capex_batt_kwh[i],
                capex_batt_kw[i], gas, i, sys)
            gen_cx[i] = split["gen_capex"]; gen_om[i] = split["gen_om"]
            bat_cx[i] = split["batt_capex"]; bat_om[i] = split["batt_om"]
            gas_cx[i] = split["gas_capex"]; gas_op[i] = split["gas_opex"]
            gas_cb[i] = split["gas_carbon"]

            # Cost uncertainty: hold optimal (C_sol, C_win, B) fixed from the
            # central solve; re-evaluate cost formula with lognormal-perturbed
            # unit costs. This correctly isolates capex uncertainty from the
            # capacity decision — re-running Nelder-Mead per draw would be
            # O(n_mc × n_starts × 500 iterations) = prohibitive.
            C_sol_opt = opt_csol[i]; C_win_opt = opt_cwin[i]; B_opt = opt_B[i]
            r   = battery.wacc; n_lf = sys.project_lifetime_yr   # battery MC uses battery WACC
            eff_fec_opt = sim.interp3(sim.fec_mean, C_sol_opt, C_win_opt, B_opt)
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

        scen = {
            "opt_delivered": opt_t, "opt_delivered_low": opt_l, "opt_delivered_high": opt_h,
            "opt_cg": opt_cg, "opt_cs": opt_cs, "opt_cb": opt_cb, "opt_cp": opt_cp,
            "opt_shed": opt_shed,
            "gen_capex": gen_cx, "gen_om": gen_om, "batt_capex": bat_cx, "batt_om": bat_om,
            "gas_capex": gas_cx, "gas_opex": gas_op, "gas_carbon": gas_cb,
            "opt_csol": opt_csol, "opt_cwin": opt_cwin, "opt_B": opt_B,
        }
        if design_p90:
            scen["opt_delivered_p90"] = opt_t_p90
            scen["opt_csol_p90"] = opt_csol_p90
            scen["opt_cwin_p90"] = opt_cwin_p90
            scen["opt_B_p90"] = opt_B_p90
        results["scenarios"][R] = scen

    return results


def _nearest_re(results, target: float) -> float:
    """The RE-target scenario closest to `target` that the run actually computed."""
    return min(results["scenarios"].keys(), key=lambda R: abs(R - target))


def run_region(region, solar, wind, battery, gas, smr, sys, workload,
               mean_irr, mean_wind_ms, reliabilities, prefix, seed=0, grid_ppa=None,
               design_p90=False):
    print(f"\n{'━'*42} {region} {'━'*42}")
    results = run_simulation(
        solar=solar, wind=wind, battery=battery, gas=gas, smr=smr,
        sys=sys, workload=workload, mean_irr=mean_irr,
        mean_wind_ms=mean_wind_ms, reliabilities=reliabilities, seed=seed,
        grid_ppa=grid_ppa, design_p90=design_p90,
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
    for name, fig in figs.items():
        fig.savefig(f"figs/{name}.png", dpi=200, bbox_inches="tight")
        plt.close(fig)
    return results


def run_region_key(region_key, workload, reliabilities, prefix,
                   sys_overrides=None, seed=42, design_p90=False,
                   mean_irr=None, mean_wind_ms=None):
    """Run a region (from REGIONS) with a chosen workload; thin wrapper over run_region.

    `mean_irr` / `mean_wind_ms` override the region's default resource (used by the
    resource-quality sensitivity); `design_p90` adds the robustness-design series."""
    cfg = REGIONS[region_key]
    sys = _sys_with(cfg["sys"], **sys_overrides) if sys_overrides else cfg["sys"]
    label = f"{cfg['label']} ({workload.name})"
    return run_region(label, cfg["solar"], cfg["wind"], cfg["battery"], cfg["gas"],
                      cfg["smr"], sys, workload,
                      cfg["mean_irr"] if mean_irr is None else mean_irr,
                      cfg["mean_wind_ms"] if mean_wind_ms is None else mean_wind_ms,
                      reliabilities, prefix, seed=seed, grid_ppa=cfg.get("grid_ppa"),
                      design_p90=design_p90)


def run_full_suite():
    """
    Default suite = FIRM (always-on, gas backup sized to 100% of load, nothing ever
    shed → capped opex). This is the right model for any valuable workload: an
    economic shed only happens when compute is worth LESS than the gas variable
    cost of serving it, so premium/AI workloads never shed and collapse to firm.
    The interruptible regime (cheap compute) is explored via `--flex-sweep`.
    """
    reliabilities = [0.70, 0.80, 0.85, 0.90, 0.95]
    run_region_key("us", FIRM, reliabilities, prefix="us_firm", seed=42)
    run_region_key("eu", FIRM, reliabilities, prefix="eu_firm", seed=42)
    print("\nDone — figures saved in figs/")


