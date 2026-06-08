from __future__ import annotations

"""3D Nelder-Mead optimiser and capex/opex delivered-cost decomposition."""
from typing import Optional, Tuple

import numpy as np
from scipy.optimize import minimize

from .params import SystemParams, BatteryParams, GasParams
from .costs import (battery_annualised_cost, battery_cost_split,
                    gas_backup_cost_scalar, gas_cost_split, carbon_price)
from .dispatch import ChronologicalSimulator


# ─────────────────────────────────────────────────────────────────────────────
# 7. 3D NELDER-MEAD OPTIMISER
# ─────────────────────────────────────────────────────────────────────────────

def _warn_if_binding(value: float, vmax: float, name: str, grid_steps: int,
                     r_target: float, year_index: int) -> None:
    """Warn if an optimum lands within one grid-step of its max bound (cap binding)."""
    if vmax <= 0:
        return
    step = vmax / max(grid_steps - 1, 1)
    if value >= vmax - step:
        print(f"  [WARN] {name}={value:.2f} is within one grid-step of its max "
              f"({vmax:.1f}) at {2025+year_index} / {r_target:.0%} RE — "
              f"optimum may be capped; raise {name}_max.")


def optimal_cost_3d(
    sim: ChronologicalSimulator,
    r_target: float,
    lcoe_sol: float,
    lcoe_win: float,
    batt: BatteryParams,
    capex_batt_kwh: float,
    capex_batt_kw: float,
    gas: GasParams,
    year_index: int,
    sys: SystemParams,
    use_p90: bool = False,
    prev_x: Optional[np.ndarray] = None,
    diag: Optional[dict] = None,
) -> Tuple[float, float, float, float, float, float, float, float, float]:
    """
    Minimise system LCOE over (C_sol, C_win, B) subject to a renewable target.

    RE target (v5.2): defined on *served* energy — at least `r_target` of the
    energy actually delivered must come from renewables+storage, i.e.
        f_RE_served = 1 − f_gas/(1 − f_drop) ≥ r_target,
    where f_drop is the shed (lost-compute) fraction of demand. Shedding therefore
    helps meet the target with less firm build, but each shed MWh is charged the
    workload's `shed_penalty_mwh` (the value of the lost compute).

    Trilinear interpolation into the precomputed gas/drop/fec surfaces makes each
    objective evaluation ~microseconds. Multi-start Nelder-Mead with exterior penalty.

    Returns (total, c_gen, c_stor, c_gas, c_pen, C_sol*, C_win*, B*, f_drop*).
    """
    n         = sys.project_lifetime_yr
    surface   = sim.gas_p90 if use_p90 else sim.gas_mean
    penalty_w = 2000.0   # $/MWh per unit RE shortfall (quadratic below)
    shed_val  = sim.workload.shed_penalty_mwh

    # Economic shed test: shed a deficit hour only if the lost compute is worth
    # LESS than the gas variable cost of serving it (fuel + carbon + VOM). If
    # compute is more valuable than gas-to-serve, never shed → revert to firm
    # (gas covers the full residual, sized to the no-shed peak). This is what makes
    # premium workloads collapse to the always-on / capped-opex case.
    gas_var = (gas.gas_price_mmbtu * gas.ccgt_heat_rate + gas.vom_mwh
               + carbon_price(gas, year_index) * gas.carbon_intensity_ccgt)
    shed_is_economic = shed_val < gas_var

    def evaluate(C_sol, C_win, B):
        """Cost components and served-RE fraction at a point (no constraint penalty)."""
        f_gas_shed = sim.interp3(surface, C_sol, C_win, B)
        f_drop_max = sim.interp3(sim.drop_mean, C_sol, C_win, B)
        fec        = sim.interp3(sim.fec_mean, C_sol, C_win, B)
        if shed_is_economic:
            f_gas, f_drop = f_gas_shed, f_drop_max
            f_peak = sim.interp3(sim.gas_peak_mean, C_sol, C_win, B)
        else:   # firm: gas serves the would-be-shed energy too; size to firm peak
            f_gas, f_drop = f_gas_shed + f_drop_max, 0.0
            firm_surface = (sim.gas_peak_firm_p90
                            if getattr(sys, "firm_gas_sizing", "mean") == "p90"
                            else sim.gas_peak_firm_mean)
            f_peak = sim.interp3(firm_surface, C_sol, C_win, B)
        served = max(1.0 - f_drop, 1e-6)
        f_re_served = 1.0 - f_gas / served
        c_gen  = C_sol * sim.sol_cf_mean * lcoe_sol + C_win * sim.win_cf_mean * lcoe_win
        c_stor = battery_annualised_cost(batt, B, capex_batt_kwh, capex_batt_kw,
                                         batt.wacc, n, effective_fec_per_day=fec) / 8760.0
        c_gas  = gas_backup_cost_scalar(f_gas, gas, year_index, gas.wacc, gas_peak=f_peak)
        c_pen  = shed_val * f_drop          # value of lost compute, $/MWh of demand
        return c_gen, c_stor, c_gas, c_pen, f_re_served, f_drop

    def objective(x: np.ndarray) -> float:
        C_sol = float(np.clip(x[0], 0.0, sys.c_sol_max))
        C_win = float(np.clip(x[1], 0.0, sys.c_win_max))
        B     = float(np.clip(x[2], 0.0, sys.storage_hours_max))

        c_gen, c_stor, c_gas, c_pen, f_re_served, _ = evaluate(C_sol, C_win, B)
        violation = max(0.0, r_target - f_re_served)
        # Quadratic penalty: ramps up sharply as violation grows,
        # preventing the optimizer from accepting small-RE solutions cheaply.
        penalty = penalty_w * violation + penalty_w * 5.0 * violation ** 2

        # (v5.4) The previous path-regularization penalty was removed: it biased the
        # objective toward the prior year's build, introducing year-to-year hysteresis
        # in the reported optimal mix. prev_x is still used purely as a warm-start
        # candidate below, which speeds convergence without distorting the cost.
        return c_gen + c_stor + c_gas + c_pen + penalty

    starts = [
        np.array([sys.c_sol_max * 0.2, sys.c_win_max * 0.1, sys.storage_hours_max * 0.05]),
        np.array([sys.c_sol_max * 0.1, sys.c_win_max * 0.2, sys.storage_hours_max * 0.05]),
        np.array([sys.c_sol_max * 0.4, sys.c_win_max * 0.2, sys.storage_hours_max * 0.1]),
        np.array([sys.c_sol_max * 0.6, sys.c_win_max * 0.1, sys.storage_hours_max * 0.1]),
        np.array([sys.c_sol_max * 0.6, sys.c_win_max * 0.2, sys.storage_hours_max * 0.2]),
        np.array([sys.c_sol_max * 0.8, sys.c_win_max * 0.2, sys.storage_hours_max * 0.2]),
        np.array([sys.c_sol_max * 0.2, sys.c_win_max * 0.6, sys.storage_hours_max * 0.1]),
    ]
    if prev_x is not None:
        starts.append(prev_x)

    best_val, best_x = np.inf, starts[0]
    for x0 in starts:
        x0  = np.clip(x0, 0, [sys.c_sol_max, sys.c_win_max, sys.storage_hours_max])
        res = minimize(objective, x0, method="Nelder-Mead",
                       options={"xatol": 0.05, "fatol": 0.05,
                                "maxiter": 600, "adaptive": True})
        if res.fun < best_val:
            best_val, best_x = res.fun, res.x

    C_sol = float(np.clip(best_x[0], 0.0, sys.c_sol_max))
    C_win = float(np.clip(best_x[1], 0.0, sys.c_win_max))
    B     = float(np.clip(best_x[2], 0.0, sys.storage_hours_max))

    # Post-optimisation feasibility refinement:
    # If the served-RE constraint is still violated (penalty couldn't escape a flat
    # plateau), do a targeted 1D scan over B at the optimal (C_sol, C_win) to find
    # the minimum-cost feasible storage.
    _, _, _, _, f_re_check, _ = evaluate(C_sol, C_win, B)
    if f_re_check < r_target - 0.005:  # 0.5% tolerance
        B_candidates = np.linspace(0.0, sys.storage_hours_max, 150)
        best_feasible_cost = np.inf
        best_B_feas = B
        for B_cand in B_candidates:
            cg, cs_, cgz, cp, f_re_c, _ = evaluate(C_sol, C_win, float(B_cand))
            if f_re_c >= r_target - 0.005:
                c_total = cg + cs_ + cgz + cp
                if c_total < best_feasible_cost:
                    best_feasible_cost = c_total
                    best_B_feas = float(B_cand)
        B = best_B_feas

    # Continuity tie-break (v5.5.1): the RE-feasibility region is IDENTICAL every year
    # — the dispatch surfaces (gas/drop/fec vs build) depend only on weather + capacities,
    # not on costs — and learning curves only lower unit costs over time. So last year's
    # optimal build is always still feasible this year and never costs more than it did.
    # The cost surface is also flat and near-degenerate around the optimum (solar↔wind
    # substitution, sharpened by the EU's negative wind-solar correlation), so multi-start
    # Nelder-Mead otherwise reports whichever near-equal-cost grid vertex happens to win by
    # numerical noise — producing spurious staircase jumps in the reported mix and a
    # non-monotone (and occasionally rising) cost trajectory that is an optimiser artifact,
    # not economics. Fix: evaluate the *previous-year* build directly at this year's costs;
    # if it is feasible and within `cont_tol` of this year's freshly-optimised cost, keep it.
    # This is a pure SELECTION among near-optimal builds (the reported cost stays within
    # cont_tol of the minimum, and where Nelder-Mead was trapped on a worse vertex it
    # actually lowers the cost), NOT a path-cost penalty — so unlike the v5.4-removed path
    # regularisation it does not bias the LCOE, it only suppresses cosmetic year-to-year flips.
    if prev_x is not None:
        cont_tol = 0.01
        pcs = float(np.clip(prev_x[0], 0.0, sys.c_sol_max))
        pcw = float(np.clip(prev_x[1], 0.0, sys.c_win_max))
        pB  = float(np.clip(prev_x[2], 0.0, sys.storage_hours_max))
        pc_gen, pc_stor, pc_gas, pc_pen, p_re, _ = evaluate(pcs, pcw, pB)
        prev_total = pc_gen + pc_stor + pc_gas + pc_pen
        cg0, cs0, cgz0, cp0, _, _ = evaluate(C_sol, C_win, B)
        cur_total = cg0 + cs0 + cgz0 + cp0
        if p_re >= r_target - 0.005 and prev_total <= cur_total * (1.0 + cont_tol):
            # Diagnostic (opt-in): record that the continuity tie-break fired and the
            # cost change it introduced (prev−cur; ≤ cont_tol·cur, i.e. ≤1%), so the
            # bound stays visible and can't silently drift. Pure observation; no effect.
            if diag is not None:
                diag["fired"] = diag.get("fired", 0) + 1
                diag.setdefault("deltas", []).append(prev_total - cur_total)
            C_sol, C_win, B = pcs, pcw, pB

    # Boundary-binding guard: warn if any optimum reaches its max bound (within
    # one grid-step). A binding cap means the true optimum may lie beyond the grid
    # and the cost is understated — raise the corresponding *_max in SystemParams.
    _warn_if_binding(C_sol, sys.c_sol_max, "C_sol", sys.grid_steps, r_target, year_index)
    _warn_if_binding(C_win, sys.c_win_max, "C_win", sys.grid_steps, r_target, year_index)
    _warn_if_binding(B,     sys.storage_hours_max, "B", sys.grid_steps, r_target, year_index)

    c_gen, c_stor, c_gas, c_pen, f_re_final, f_drop = evaluate(C_sol, C_win, B)
    # Infeasibility guard: a firm, battery-only system cannot exceed ≈0.94 RE (EU) /
    # ≈0.95 (US) — multi-day Dunkelflaute plus the min(1,4/B) battery power cap leave a
    # residual gas slice no build within bounds can close. If the target can't be met,
    # the returned build is the penalty-minimising point at the achievable maximum, NOT a
    # feasible R-meeting build, and its cost understates a true R build. Warn loudly so
    # the number is not mistaken for a feasible optimum (the firm suite omits 95% for this
    # reason; >~94% needs LDES / H₂ firming — see --ldes / --firming h2).
    if f_re_final < r_target - 0.005:
        print(f"  [WARN] RE target {r_target:.0%} INFEASIBLE at {2025+year_index}: "
              f"max achievable ≈ {f_re_final:.1%} (firm battery-only ceiling). Reported "
              f"cost is for the {f_re_final:.0%}-RE penalty optimum, not a {r_target:.0%} build "
              f"— use --ldes / --firming h2 for higher RE.")
    total = c_gen + c_stor + c_gas + c_pen
    return total, c_gen, c_stor, c_gas, c_pen, C_sol, C_win, B, f_drop


def delivered_cost_split(sim, C_sol, C_win, B, solar, wind, lcoe_sol, lcoe_win,
                         batt, capex_kwh, capex_kw, gas, year_index, sys):
    """
    Decompose the delivered cost ($/MWh of load) at a given build into capex vs
    opex categories. Mirrors the optimiser's economic shed test so the gas /
    shed split matches the chosen optimum.

    Returns a per-factor dict ($/MWh of load) so the breakdown can show capex vs
    opex within each technology:
      gen_capex, gen_om          generation capital recovery / fixed O&M
      batt_capex, batt_om        battery capital recovery (incl. replacements) / O&M
      gas_capex                  gas plant capacity capital recovery
      gas_opex                   gas fixed O&M + fuel + VOM
      gas_carbon                 gas carbon cost
      shed                       value of lost compute (interruptible only)
    """
    n = sys.project_lifetime_yr
    f_gas_shed = sim.interp3(sim.gas_mean, C_sol, C_win, B)
    f_drop_max = sim.interp3(sim.drop_mean, C_sol, C_win, B)
    fec        = sim.interp3(sim.fec_mean, C_sol, C_win, B)
    shed_val   = sim.workload.shed_penalty_mwh
    gas_var = (gas.gas_price_mmbtu * gas.ccgt_heat_rate + gas.vom_mwh
               + carbon_price(gas, year_index) * gas.carbon_intensity_ccgt)
    if shed_val < gas_var:   # shedding is economic
        f_gas, f_drop = f_gas_shed, f_drop_max
        f_peak = sim.interp3(sim.gas_peak_mean, C_sol, C_win, B)
    else:                    # firm: gas serves all; size to firm peak
        f_gas, f_drop = f_gas_shed + f_drop_max, 0.0
        firm_surface = (sim.gas_peak_firm_p90
                        if getattr(sys, "firm_gas_sizing", "mean") == "p90"
                        else sim.gas_peak_firm_mean)
        f_peak = sim.interp3(firm_surface, C_sol, C_win, B)

    gen_s = C_sol * sim.sol_cf_mean * lcoe_sol
    gen_w = C_win * sim.win_cf_mean * lcoe_win
    gen_capex = gen_s * (1 - solar.om_frac_lcoe) + gen_w * (1 - wind.om_frac_lcoe)
    gen_om    = gen_s * solar.om_frac_lcoe + gen_w * wind.om_frac_lcoe

    b_capex, b_opex = battery_cost_split(batt, B, capex_kwh, capex_kw, batt.wacc, n, fec)
    b_capex /= 8760.0; b_opex /= 8760.0
    g = gas_cost_split(f_gas, gas, year_index, gas.wacc, gas_peak=f_peak)

    return {
        "gen_capex":  gen_capex, "gen_om": gen_om,
        "batt_capex": b_capex,   "batt_om": b_opex,
        "gas_capex":  g["capex"], "gas_opex": g["fom"] + g["fuel"],
        "gas_carbon": g["carbon"], "shed": shed_val * f_drop,
    }


