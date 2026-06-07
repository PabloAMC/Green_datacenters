from __future__ import annotations

"""
Per-YEAR trajectory of the fully-optimised, gas-free, zero-carbon datacenter
(solar + wind + LFP + self-produced green H₂; residual bought as green H₂).

This is the trajectory companion to `analysis.run_ldes_joint` (which co-optimises at a
single target year and stress-tests the market-H₂ price). It reuses the same
year-vectorised 2-storage dispatch (`dispatch.dispatch_h2_vec`) and the **same cost
model** as `run_ldes_joint.evaluate` (kept in sync — see the comment in `_costs`), but
solves one optimum per year (warm-started) so we can draw the optimised gas-free H₂
line in fig1 and its capex/opex breakdown in fig6. No RE target — the system is green by
construction, so it is a pure delivered-LCOE minimisation. LFP duration is fixed at the
robustly-optimal ~6 h: letting run_ldes_joint choose it freely lands at 5.5 h (2025) →
6.0 h (2040), i.e. within 0.5 h of 6 h across the whole trajectory, so fixing it drops a
dimension at negligible cost (verified: the fixed-6 h trajectory reproduces the free 5D
joint optimum to within ~0.4× overbuild and <$1/MWh per year).
"""
import numpy as np
from scipy.optimize import minimize

from .params import LDES_PRESETS, GAS_H2
from .costs import (cumulative_capacity, wright_law, rewacc_lcoe, crf,
                    h2_system_cost_split)
from .weather import solar_clearsky, generate_weather_year
from .dispatch import dispatch_h2_vec

_B_LFP = 6.0                                   # fixed diurnal optimum (matches run_ldes_joint)
_LO = np.array([0.0, 0.0, 0.0, 0.0])           # C_sol, C_win, electrolyser MW, H2-store h
# Electrolyser ceiling 4.0 (vs run_ldes_joint's 1.5): the unconstrained per-year optimum
# rises to ~1.7 MW/MW-load by 2040 and would bind at 1.5 in the late years, so we give it
# headroom. Effect is tiny (2040 LCOE 88.0→87.7) but it removes an artificial boundary.
_HI = np.array([24.0, 22.0, 4.0, 720.0])
COMP = ["gen_capex", "gen_om", "lfp_capex", "lfp_om", "elec_capex",
        "store_capex", "turbine_capex", "buy_h2"]


def _costs(x, ctx, sol2d=None, win2d=None):
    """Delivered-cost components ($/MWh-load) for design x=(C_sol,C_win,elec,H2).

    Uses the shared `costs.h2_system_cost_split` formula (single source, also used by
    `analysis.run_ldes_joint`). `sol2d`/`win2d` override the dispatch weather ensemble —
    the optimiser passes a smaller sample for speed while the reported cost is computed
    on the full ensemble (see `h2_system_trajectory`)."""
    C_sol, C_win, elec, H2 = [float(np.clip(x[j], _LO[j], _HI[j])) for j in range(4)]
    batt, ldes = ctx["batt"], ctx["ldes"]
    s2d = ctx["sol2d"] if sol2d is None else sol2d
    w2d = ctx["win2d"] if win2d is None else win2d
    lfp_pow = 1.0 if _B_LFP <= 4.0 else min(1.0, 4.0 / _B_LFP)
    resid, efc = dispatch_h2_vec(s2d, w2d, C_sol, C_win, _B_LFP,
                                 lfp_pow, batt.roundtrip_efficiency, H2, elec, 1.0,
                                 ldes.roundtrip_efficiency)
    comp = h2_system_cost_split(
        C_sol, C_win, _B_LFP, elec, H2, resid, efc,
        batt=batt, ldes=ldes, n=ctx["n"], CF_sol=ctx["CF_sol"], CF_win=ctx["CF_win"],
        lcoe_sol=ctx["lcoe_sol"], lcoe_win=ctx["lcoe_win"], om_sol=ctx["om_sol"],
        om_win=ctx["om_win"], lfp_kwh=ctx["lfp_kwh"], lfp_kw=ctx["lfp_kw"],
        ch_capex=ctx["ch_capex"], e_capex=ctx["e_capex"], dis_capex=ctx["dis_capex"],
        h2_buy=ctx["h2_buy_base"], crf_l=ctx["crf_l"], annuity=ctx["annuity"])
    return sum(comp.values()), comp, resid


def _optimize(ctx, prev_x=None, sol2d=None, win2d=None):
    def obj(x):
        return (_costs(x, ctx, sol2d, win2d)[0]
                + 1e3 * float(np.sum((x - np.clip(x, _LO, _HI)) ** 2)))
    starts = [prev_x] if prev_x is not None else [np.array([9.0, 6.0, 0.6, 60.0]),
                                                  np.array([13.0, 9.0, 1.0, 120.0])]
    best_x, best_f = None, np.inf
    for x0 in starts:
        res = minimize(obj, np.clip(x0, _LO, _HI), method="Nelder-Mead",
                       options={"xatol": 0.05, "fatol": 0.1, "maxiter": 500, "adaptive": True})
        if res.fun < best_f:
            best_f, best_x = res.fun, np.clip(res.x, _LO, _HI)
    return best_x


def h2_system_trajectory(solar, wind, battery, mean_irr, mean_wind_ms, sys, years,
                         seed=42, ldes_tech="h2", n_mc=None, n_mc_opt=20,
                         weather_years=None):
    """Per-year optimum of the gas-free H₂ system → trajectory + cost breakdown
    (year-indexed arrays of delivered LCOE, build, and the fig6 capex/opex bands).

    Weather fidelity (v5.7): the reported cost and purchased-H₂ share are evaluated on
    the FULL ensemble (`n_mc`, defaulting to `sys.n_mc_weather` so the fig1 H₂ line
    matches the fidelity of the RE-target lines it is drawn against), while the build is
    *optimised* on a smaller `n_mc_opt`-year subsample for speed. The H₂-store/electrolyser
    sizing therefore sees enough Dunkelflaute years and the reported number is honest;
    previously both used a hardcoded 6 years, under-sampling exactly the multi-day lull
    tail that sizes the electrolyser.

    `weather_years`: optional list of (solar_cf[8760], wind_cf[8760]) real reanalysis
    years (e.g. from `weather.load_weather_traces`). When given, the dispatch ensemble is
    these real years INSTEAD of the synthetic generator — so the H₂/storage sizing sees
    real cloud/Dunkelflaute structure and real interannual variability. `mean_irr` /
    `mean_wind_ms` / `n_mc` are then ignored for the weather draw."""
    batt = battery
    ldes = LDES_PRESETS[ldes_tech]
    n = sys.project_lifetime_yr
    lcoe_sol = rewacc_lcoe(wright_law(solar.lcoe_today, solar.cumulative_gw_2025,
                                      cumulative_capacity(solar, years), solar.learning_rate), solar)
    lcoe_win = rewacc_lcoe(wright_law(wind.lcoe_today, wind.cumulative_gw_2025,
                                      cumulative_capacity(wind, years), wind.learning_rate), wind)
    cum_b = cumulative_capacity(batt, years)
    lfp_kwh = wright_law(batt.capex_kwh_today, batt.cumulative_gwh_2025, cum_b, batt.learning_rate)
    lfp_kw = wright_law(batt.capex_kw_today, batt.cumulative_gwh_2025, cum_b, batt.learning_rate)
    ch = wright_law(ldes.capex_kw_today, ldes.cumulative_gwh_2025,
                    cumulative_capacity(ldes, years), ldes.learning_rate)
    e_capex = ldes.capex_kwh_today
    dis_capex = ldes.discharge_capex_kw if ldes.discharge_capex_kw is not None else ldes.capex_kw_today
    h2_buy_base = GAS_H2.gas_price_mmbtu * GAS_H2.ccgt_heat_rate + GAS_H2.vom_mwh
    crf_l = crf(ldes.wacc, n); annuity = (1.0 - (1.0 + ldes.wacc) ** (-n)) / ldes.wacc

    if weather_years is not None:
        sol2d = np.asarray([np.asarray(s, float) for s, _ in weather_years])
        win2d = np.asarray([np.asarray(w, float) for _, w in weather_years])
    else:
        n_eval = n_mc if n_mc is not None else sys.n_mc_weather
        rng = np.random.default_rng(seed + 5)
        cs = solar_clearsky(mean_irr, getattr(sys, "solar_performance_ratio", 1.0))
        sol2d = np.empty((n_eval, 8760)); win2d = np.empty((n_eval, 8760))
        for k in range(n_eval):
            s, w = generate_weather_year(
                cs, mean_wind_ms, rng, wind_solar_corr=sys.wind_solar_corr,
                syn_loading=sys.syn_loading, syn_persistence=sys.syn_persistence,
                cloud_ar1=sys.cloud_ar1, wind_ar1=sys.wind_ar1,
                wind_daily_share=sys.wind_daily_share, wind_seasonal_amp=sys.wind_seasonal_amp,
                wind_v_ci=sys.wind_v_ci, wind_v_rated=sys.wind_v_rated,
                wind_v_cutout=sys.wind_v_cutout)
            sol2d[k] = s; win2d[k] = w
    CF_sol, CF_win = float(sol2d.mean()), float(win2d.mean())
    # Optimise the build on a subsample (speed); report cost on the full ensemble (fidelity).
    n_opt = min(int(n_mc_opt), sol2d.shape[0])
    sol_opt, win_opt = sol2d[:n_opt], win2d[:n_opt]

    out = {c: np.zeros(years + 1) for c in COMP}
    out.update({k: np.zeros(years + 1) for k in
                ("lcoe", "C_sol", "C_win", "B_lfp", "P_elec", "B_h2", "buy_frac")})
    prev = None
    for i in range(years + 1):
        ctx = dict(batt=batt, ldes=ldes, n=n, sol2d=sol2d, win2d=win2d,
                   CF_sol=CF_sol, CF_win=CF_win, lcoe_sol=float(lcoe_sol[i]),
                   lcoe_win=float(lcoe_win[i]), om_sol=solar.om_frac_lcoe,
                   om_win=wind.om_frac_lcoe, lfp_kwh=float(lfp_kwh[i]),
                   lfp_kw=float(lfp_kw[i]), ch_capex=float(ch[i]), e_capex=e_capex,
                   dis_capex=dis_capex, h2_buy_base=h2_buy_base, crf_l=crf_l, annuity=annuity)
        x = _optimize(ctx, prev_x=prev, sol2d=sol_opt, win2d=win_opt); prev = x
        total, comp, resid = _costs(x, ctx)          # full-ensemble cost & buy_frac
        out["lcoe"][i] = total; out["buy_frac"][i] = resid
        out["C_sol"][i], out["C_win"][i] = x[0], x[1]
        out["P_elec"][i], out["B_h2"][i] = x[2], x[3]
        out["B_lfp"][i] = _B_LFP
        for c in COMP:
            out[c][i] = comp[c]
    out["ldes_name"] = ldes.name
    return out
