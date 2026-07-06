from __future__ import annotations

"""Vectorised chronological dispatch over the 3D scenario grid."""
from typing import Optional, Tuple

import numpy as np

from .params import SystemParams, BatteryParams, WorkloadProfile
from .weather import (solar_clearsky, generate_weather_year,
                      generate_weather_portfolio, load_profile)


# ─────────────────────────────────────────────────────────────────────────────
# 5. CHRONOLOGICAL DISPATCH SIMULATOR  (3D grid: C_sol × C_win × B)
# ─────────────────────────────────────────────────────────────────────────────

class ChronologicalSimulator:
    """
    Precomputes 3D gas-fraction AND shed-fraction surfaces over (C_sol, C_win, B)
    via MC dispatch. The combined dispatch correctly accounts for both sources
    serving the load simultaneously — solar and wind dispatch into the same battery
    and load — and (v5.2) for demand flexibility as SHEDDING: up to
    `interruptible_fraction` of a deficit hour's load may be dropped (not recovered),
    which reduces the gas/storage needed but loses compute (priced by a penalty in
    the optimiser, not here).

    The 3D grid (ns = grid_steps per axis → ns^3 scenarios) is vectorised over
    all scenarios in parallel and is O(ns^3 × 8760 × n_mc). With the defaults
    (ns=21 → 9261 scenarios, n_mc=50) this is roughly a minute per region.

    The Nelder-Mead optimiser then interpolates trilinearly into this surface
    at negligible cost per evaluation.
    """

    def __init__(self, sys: SystemParams, batt: BatteryParams,
                 workload: WorkloadProfile, mean_irr: float,
                 mean_wind_ms: float, seed: int = 0,
                 weather_years: "Optional[list]" = None):
        self.sys       = sys
        self.batt      = batt
        self.workload  = workload
        self.clearsky  = solar_clearsky(mean_irr, getattr(sys, "solar_performance_ratio", 1.0))
        self.mean_wind = mean_wind_ms
        # Datacenter load shape (mean 1.0). "flat" → all-ones → constant-load model.
        self.load = load_profile(getattr(sys, "load_profile", "flat"))
        # REANALYSIS HOOK (v5.5). `weather_years`, if given, is an explicit list of
        # (solar_cf[8760], wind_cf[8760]) hourly traces — e.g. real ERA5 / NSRDB years
        # loaded via `weather.load_weather_traces` — used INSTEAD of the synthetic
        # generator. This is the single seam to swap stylised weather for measured
        # reanalysis: the dispatch, optimiser, costing and figures are unchanged; only
        # the source of the 8760-h CF traces differs. n_mc_weather is then ignored (the
        # supplied set is used as-is). None → synthesise from the marginals + synoptic
        # factor as before.
        self.weather_years = weather_years

        ns = sys.grid_steps   # per-axis; total scenarios = ns^3
        self.C_sol_grid = np.linspace(0.0, sys.c_sol_max, ns)
        self.C_win_grid = np.linspace(0.0, sys.c_win_max, ns)
        self.B_grid     = np.linspace(0.0, sys.storage_hours_max, ns)

        # Flatten 3D grid → 1D vectors for vectorised dispatch
        cs, cw, bg = np.meshgrid(self.C_sol_grid, self.C_win_grid, self.B_grid,
                                  indexing='ij')
        self._cs = cs.ravel()   # shape (ns^3,)
        self._cw = cw.ravel()
        self._bg = bg.ravel()
        n_scen = len(self._cs)

        # Battery power limit per scenario (same formula as before)
        with np.errstate(divide='ignore', invalid='ignore'):
            safe_b = np.where(self._bg > 0, self._bg, 1.0)
            self._batt_pow = np.where(self._bg <= 0, 0.0,
                             np.where(self._bg <= 4.0, 1.0,
                                      np.minimum(1.0, 4.0 / safe_b)))
        self._soc_max = self._bg.copy()

        if self.weather_years is not None:
            n_years, src = len(self.weather_years), "reanalysis"
        else:
            n_years, src = sys.n_mc_weather, "MC"
        sites = f", {sys.n_sites} sites" if sys.n_sites > 1 and self.weather_years is None else ""
        print(f"  [Sim] {n_years} {src} years × {n_scen} scenarios "
              f"({ns}³ grid{sites}, C_sol 0–{sys.c_sol_max}×, "
              f"C_win 0–{sys.c_win_max}×, B 0–{sys.storage_hours_max}h) …")
        (self.gas_mean, self.gas_p90, self.fec_mean, self.gas_peak_mean,
         self.gas_peak_firm_mean, self.drop_mean,
         self.sol_cf_mean, self.win_cf_mean,
         self.gas_peak_firm_p90,
         self.drop_p90, self.gas_peak_p90, self.gas_firm_p90) = self._run_mc(seed)
        print("  [Sim] Done.")

    def _dispatch_one_year(self, sol_tr: np.ndarray,
                            win_tr: np.ndarray,
                            scen=None) -> Tuple[np.ndarray, np.ndarray,
                                                np.ndarray, np.ndarray,
                                                float, float]:
        """
        Vectorised combined dispatch for all (C_sol, C_win, B) scenarios.
        Both sources feed the same load; battery arbitrates between them.
        Flexibility = SHEDDING (v5.2): up to `interruptible_fraction` of each
        deficit hour's load may be dropped (no recovery).
        Returns gas_frac, fec_daily, gas_peak, drop_frac (each ns^3,), cf_sol, cf_win.

        `scen` (v5.9): optional (cs, cw, batt_pow, soc_max) scenario-vector override
        — used by `exact_point` to dispatch ONE build across all weather years at
        once (the years become the vector axis; `sol_tr`/`win_tr` are then (N, 8760)
        per-"scenario" traces). None → the precomputed ns³ grid (unchanged).
        """
        eta_chg = self.batt.roundtrip_efficiency ** 0.5
        eta_dis = self.batt.roundtrip_efficiency ** 0.5
        flex    = self.workload.interruptible_fraction   # max sheddable share / hour
        if scen is None:
            cs, cw, batt_pow, soc_max = self._cs, self._cw, self._batt_pow, self._soc_max
        else:
            cs, cw, batt_pow, soc_max = scen
        n = len(cs)
        per_scen = sol_tr.ndim == 2   # (n, 8760) per-scenario traces (exact_point)

        soc = np.zeros(n); gas = np.zeros(n); drop = np.zeros(n)
        dis_sum = np.zeros(n)         # annual cell discharge throughput (for EFC)
        gas_peak = np.zeros(n)        # peak residual AFTER shedding (interruptible case)
        gas_peak_firm = np.zeros(n)   # peak residual with NO shedding (firm backup sizing)

        load = self.load
        for t in range(8760):
            s_t = sol_tr[:, t] if per_scen else sol_tr[t]
            w_t = win_tr[:, t] if per_scen else win_tr[t]
            g = cs * s_t + cw * w_t
            net     = g - load[t]          # load[t]=1.0 for the flat (default) profile
            deficit = np.maximum(-net, 0.0)
            surplus = np.maximum(net, 0.0)

            # ── Deficit side ──────────────────────────────────────────────────
            # 1. Discharge battery
            dis = np.minimum(np.minimum(soc * eta_dis, batt_pow), deficit)
            soc -= dis / eta_dis;  deficit -= dis
            dis_sum += dis                                       # accumulate throughput
            gas_peak_firm = np.maximum(gas_peak_firm, deficit)  # pre-shed (firm)

            # 2. SHED up to `flex` of the load (compute lost; NOT recovered).
            #    Whether this shed is ECONOMIC is decided later in the optimiser
            #    (shed only if compute value < gas variable cost); the dispatch
            #    just records the max-sheddable case and the no-shed (firm) case.
            shed = np.minimum(flex * load[t], deficit)   # flex is a share of that hour's load
            drop += shed
            deficit -= shed

            # 3. Gas backup covers the (now reduced) residual deficit
            gas_peak = np.maximum(gas_peak, deficit)
            gas += deficit

            # ── Surplus side ──────────────────────────────────────────────────
            # 4. Charge battery with surplus (anything left is curtailed).
            chg = np.minimum(np.minimum((soc_max - soc) / eta_chg,
                                         batt_pow), surplus)
            soc += chg * eta_chg

        # Throughput-based equivalent full cycles (v5.4): annual cell-discharge
        # throughput ÷ rated energy capacity — a standard, robust cycle count that
        # replaces the earlier 2σ(SoC) heuristic. (Full rainflow half-cycle counting
        # with Wöhler/DoD weighting is the further refinement; throughput EFCs are
        # the widely-used proxy and are exact for symmetric daily cycling.)
        with np.errstate(divide='ignore', invalid='ignore'):
            efc_year = np.where(soc_max > 0,
                                (dis_sum / eta_dis)
                                / np.where(soc_max > 0, soc_max, 1.0),
                                0.0)
        fec_daily = efc_year / 365.0

        return (gas / 8760.0, fec_daily, gas_peak, gas_peak_firm, drop / 8760.0,
                float(sol_tr.mean()), float(win_tr.mean()))

    def _run_mc(self, seed: int):
        rng = np.random.default_rng(seed)
        # Real reanalysis traces (if supplied) are used as-is; otherwise synthesise N.
        N   = len(self.weather_years) if self.weather_years is not None else self.sys.n_mc_weather
        sh  = (len(self._cs),)

        all_gas = np.zeros((N,) + sh)
        all_fec = np.zeros((N,) + sh)
        all_gas_peak = np.zeros((N,) + sh)
        all_gas_peak_firm = np.zeros((N,) + sh)
        all_drop = np.zeros((N,) + sh)
        cf_s_list, cf_w_list = [], []
        traces = []

        for i in range(N):
            if self.weather_years is not None:
                sol_tr, win_tr = self.weather_years[i]
                sol_tr = np.asarray(sol_tr, float); win_tr = np.asarray(win_tr, float)
            else:
                # Spatial diversification: portfolio-average over n_sites sites that
                # share the regional synoptic factor (site_synoptic_corr). n_sites=1
                # reduces exactly to the single-site generator (draw order preserved).
                sol_tr, win_tr = generate_weather_portfolio(
                    self.clearsky, self.mean_wind, rng,
                    n_sites=self.sys.n_sites,
                    site_synoptic_corr=self.sys.site_synoptic_corr,
                    site_local_corr=getattr(self.sys, "site_local_corr", 0.4),
                    wind_solar_corr=self.sys.wind_solar_corr,
                    syn_loading=self.sys.syn_loading,
                    syn_persistence=self.sys.syn_persistence,
                    cloud_ar1=self.sys.cloud_ar1,
                    wind_ar1=self.sys.wind_ar1,
                    wind_daily_share=self.sys.wind_daily_share,
                    wind_seasonal_amp=self.sys.wind_seasonal_amp,
                    wind_v_ci=self.sys.wind_v_ci, wind_v_rated=self.sys.wind_v_rated,
                    wind_v_cutout=self.sys.wind_v_cutout)
            gas_f, fec_d, gas_p, gas_pf, drop_f, cf_s, cf_w = self._dispatch_one_year(sol_tr, win_tr)
            all_gas[i] = gas_f;  all_fec[i] = fec_d; all_gas_peak[i] = gas_p
            all_gas_peak_firm[i] = gas_pf; all_drop[i] = drop_f
            cf_s_list.append(cf_s);  cf_w_list.append(cf_w)
            traces.append((sol_tr, win_tr))

        # Keep the weather (v5.9) so `exact_point` can re-dispatch the FINAL optimum
        # exactly (no interpolation) — ~7 MB at 50 years, regenerating would be slower.
        self._traces_s = np.stack([s for s, _ in traces])
        self._traces_w = np.stack([w for _, w in traces])
        self._exact_cache = {}

        return (all_gas.mean(0), np.percentile(all_gas, 90, axis=0),
                all_fec.mean(0), all_gas_peak.mean(0), all_gas_peak_firm.mean(0),
                all_drop.mean(0),
                float(np.mean(cf_s_list)), float(np.mean(cf_w_list)),
                np.percentile(all_gas_peak_firm, 90, axis=0),
                # v5.9.1 P90 coherence: the shed-branch P90 surfaces, and the FIRM
                # gas-energy P90 taken on the per-year SUM gas+drop (a coherent
                # year-percentile, not P90(gas)+mean(drop)).
                np.percentile(all_drop, 90, axis=0),
                np.percentile(all_gas_peak, 90, axis=0),
                np.percentile(all_gas + all_drop, 90, axis=0))

    def exact_point(self, C_sol: float, C_win: float, B: float) -> dict:
        """
        EXACT MC dispatch statistics at one (C_sol, C_win, B) — no interpolation
        (v5.9). Re-runs the stored weather years through the same dispatch waterfall
        with the years as the vector axis, so the final reported optimum is confirmed
        on true dispatch rather than read off the trilinear surface (whose convexity
        bias overstates gas between grid nodes; §8.4/§12). Cached per point.

        Returns the same statistics as the precomputed surfaces:
        {gas_mean, gas_p90, fec_mean, gas_peak_mean, gas_peak_firm_mean,
         gas_peak_firm_p90, drop_mean}.
        """
        key = (round(float(C_sol), 9), round(float(C_win), 9), round(float(B), 9))
        hit = self._exact_cache.get(key)
        if hit is not None:
            return hit
        N = self._traces_s.shape[0]
        B = float(B)
        bp = 0.0 if B <= 0 else (1.0 if B <= 4.0 else min(1.0, 4.0 / B))
        scen = (np.full(N, float(C_sol)), np.full(N, float(C_win)),
                np.full(N, bp), np.full(N, B))
        gas_f, fec_d, gas_p, gas_pf, drop_f, _, _ = self._dispatch_one_year(
            self._traces_s, self._traces_w, scen=scen)
        out = {
            "gas_mean": float(gas_f.mean()),
            "gas_p90": float(np.percentile(gas_f, 90)),
            "fec_mean": float(fec_d.mean()),
            "gas_peak_mean": float(gas_p.mean()),
            "gas_peak_firm_mean": float(gas_pf.mean()),
            "gas_peak_firm_p90": float(np.percentile(gas_pf, 90)),
            "drop_mean": float(drop_f.mean()),
            "drop_p90": float(np.percentile(drop_f, 90)),
            "gas_peak_p90": float(np.percentile(gas_p, 90)),
            "gas_firm_p90": float(np.percentile(gas_f + drop_f, 90)),
        }
        self._exact_cache[key] = out
        return out

    def interp3(self, surface: np.ndarray, C_sol: float,
                C_win: float, B: float) -> float:
        """
        Trilinear interpolation into a precomputed 3D surface.
        surface has shape (ns_sol, ns_win, ns_B), flattened to 1D in _run_mc.
        We reshape on-the-fly for indexing.
        """
        ns = self.sys.grid_steps
        surf3 = surface.reshape(ns, ns, ns)

        C_sol = float(np.clip(C_sol, self.C_sol_grid[0], self.C_sol_grid[-1]))
        C_win = float(np.clip(C_win, self.C_win_grid[0], self.C_win_grid[-1]))
        B     = float(np.clip(B,     self.B_grid[0],     self.B_grid[-1]))

        def idx(grid, v):
            i = int(np.clip(np.searchsorted(grid, v, 'right') - 1, 0, len(grid) - 2))
            d = (v - grid[i]) / (grid[i+1] - grid[i] + 1e-12)
            return i, float(d)

        is_, ds = idx(self.C_sol_grid, C_sol)
        iw, dw = idx(self.C_win_grid, C_win)
        ib, db = idx(self.B_grid,     B)

        # Trilinear: 8 corners
        def c(s, w, b):
            return surf3[is_+s, iw+w, ib+b]

        return float(
            (1-ds)*(1-dw)*(1-db)*c(0,0,0) + ds*(1-dw)*(1-db)*c(1,0,0) +
            (1-ds)*  dw  *(1-db)*c(0,1,0) + ds*  dw  *(1-db)*c(1,1,0) +
            (1-ds)*(1-dw)*  db  *c(0,0,1) + ds*(1-dw)*  db  *c(1,0,1) +
            (1-ds)*  dw  *  db  *c(0,1,1) + ds*  dw  *  db  *c(1,1,1)
        )




# ─────────────────────────────────────────────────────────────────────────────
# TWO-STORAGE OVERLAY  (LFP diurnal + LDES multi-day)  — for run_ldes_overlay
# ─────────────────────────────────────────────────────────────────────────────

def dispatch_ldes_overlay(clearsky, mean_wind, rng, weather_kwargs,
                          C_sol, C_win, B_lfp, lfp_pow, lfp_rte,
                          B_ldes, ldes_charge_pow, ldes_discharge_pow, ldes_rte, n_mc):
    """
    Chronological 2-storage dispatch at a FIXED build, vectorised over a vector of
    candidate LDES energy capacities `B_ldes` (hours, MWh per MW-load).

    Priority each hour — deficit: LFP discharge → LDES discharge → gas; surplus:
    LFP charge → LDES charge → curtail. So LFP keeps doing the cheap diurnal cycle
    and LDES soaks up the multi-day surplus (otherwise curtailed) to cover multi-day
    deficits. The LFP trajectory is independent of LDES (LDES acts only on the LFP
    residual), so it is computed once (scalar) and shared across all candidates.

    LDES charge and discharge power are **separate** (`ldes_charge_pow`,
    `ldes_discharge_pow`): the self-produced-H2 case has a *small electrolyser*
    charging slowly from surplus over many sunny hours, plus a *full-size turbine*
    to cover the load during lulls — exactly the "make H2 on sunny days" design.

    Returns (per-candidate, mean over MC years):
        gas_frac[K]   residual deficit served by gas backup (fraction of load)
        gas_peak[K]   mean annual peak residual (for gas-capacity sizing)
        lfp_efc       LFP throughput equivalent-full-cycles/day (scalar)
        ldes_efc[K]   LDES throughput equivalent-full-cycles/day
    """
    K = len(B_ldes)
    B_ldes = np.asarray(B_ldes, dtype=float)
    eta_l = lfp_rte ** 0.5      # LFP charge/discharge (symmetric split)
    eta_d = ldes_rte ** 0.5     # LDES charge/discharge (symmetric split of RTE)
    gas_acc = np.zeros(K); peak_acc = np.zeros(K); ldes_dis_acc = np.zeros(K)
    lfp_dis_acc = 0.0
    soc_ldes_max = B_ldes       # MWh per MW-load

    for _ in range(int(n_mc)):
        # v5.9.1: portfolio seam (n_sites=1 reduces exactly to the single-site
        # generator), so the LDES overlay sees the same diversified weather as
        # the main path when a multi-site config is in use.
        sol, win = generate_weather_portfolio(clearsky, mean_wind, rng,
                                              **weather_kwargs)
        soc_lfp = 0.0
        soc_ld = np.zeros(K)
        gas = np.zeros(K); peak = np.zeros(K); ldes_dis = np.zeros(K)
        lfp_dis = 0.0
        for t in range(8760):
            g = C_sol * sol[t] + C_win * win[t]
            net = g - 1.0
            if net < 0.0:
                deficit = -net
                # 1. LFP discharge (scalar, shared)
                d_lfp = min(min(soc_lfp * eta_l, lfp_pow), deficit)
                soc_lfp -= d_lfp / eta_l
                lfp_dis += d_lfp
                resid = deficit - d_lfp                       # scalar residual
                # 2. LDES discharge (per candidate), limited by turbine power
                d_ld = np.minimum(np.minimum(soc_ld * eta_d, ldes_discharge_pow), resid)
                soc_ld -= d_ld / eta_d
                ldes_dis += d_ld
                resid_k = resid - d_ld                        # length-K
                peak = np.maximum(peak, resid_k)
                gas += resid_k
            else:
                surplus = net
                # 3. LFP charge (scalar)
                c_lfp = min(min((B_lfp - soc_lfp) / eta_l, lfp_pow), surplus)
                soc_lfp += c_lfp * eta_l
                resid_s = surplus - c_lfp                     # scalar surplus left
                # 4. LDES charge (per candidate), limited by electrolyser power;
                #    remainder curtailed.
                c_ld = np.minimum(np.minimum((soc_ldes_max - soc_ld) / eta_d,
                                             ldes_charge_pow), resid_s)
                soc_ld += c_ld * eta_d
        gas_acc += gas / 8760.0
        peak_acc += peak
        ldes_dis_acc += ldes_dis
        lfp_dis_acc += lfp_dis

    gas_frac = gas_acc / n_mc
    gas_peak = peak_acc / n_mc
    lfp_efc = (lfp_dis_acc / eta_l / max(B_lfp, 1e-9)) / 365.0 / n_mc if B_lfp > 0 else 0.0
    with np.errstate(divide='ignore', invalid='ignore'):
        ldes_efc = np.where(soc_ldes_max > 0,
                            (ldes_dis_acc / eta_d)
                            / np.where(soc_ldes_max > 0, soc_ldes_max, 1.0) / 365.0 / n_mc,
                            0.0)
    return gas_frac, gas_peak, lfp_efc, ldes_efc


def dispatch_h2_vec(sol2d, win2d, C_sol, C_win, B_lfp, lfp_pow, lfp_rte,
                    B_h2, elec_pow, turb_pow, h2_rte):
    """
    Single-design 2-storage (LFP + self-produced H2) chronological dispatch,
    **vectorised over weather years** (sol2d/win2d have shape (Y, 8760)). Years are
    independent, so they run in parallel as length-Y state vectors — making this fast
    and deterministic enough to serve as a Nelder-Mead objective for joint
    co-optimisation. The residual the H2 store cannot cover is bought as green H2 from
    the market (no blackout).

    Returns (resid_frac, ldes_efc): mean annual share of load served by purchased H2,
    and the H2 store's throughput equivalent-full-cycles/day (for augmentation costing).
    """
    Y, T = sol2d.shape
    eta_l = lfp_rte ** 0.5
    eta_h = h2_rte ** 0.5
    soc_l = np.zeros(Y); soc_h = np.zeros(Y)
    bought = np.zeros(Y); h2_dis = np.zeros(Y)
    for t in range(T):
        g = C_sol * sol2d[:, t] + C_win * win2d[:, t]
        net = g - 1.0
        deficit = np.maximum(-net, 0.0)
        surplus = np.maximum(net, 0.0)
        # LFP discharge (diurnal)
        d_l = np.minimum(np.minimum(soc_l * eta_l, lfp_pow), deficit)
        soc_l -= d_l / eta_l; resid = deficit - d_l
        # H2 turbine discharge (multi-day)
        d_h = np.minimum(np.minimum(soc_h * eta_h, turb_pow), resid)
        soc_h -= d_h / eta_h; h2_dis += d_h; resid -= d_h
        bought += resid                                  # remainder bought (green H2)
        # LFP charge, then electrolyser charge from leftover surplus (rest curtailed)
        c_l = np.minimum(np.minimum((B_lfp - soc_l) / eta_l, lfp_pow), surplus)
        soc_l += c_l * eta_l; surplus -= c_l
        c_h = np.minimum(np.minimum((B_h2 - soc_h) / eta_h, elec_pow), surplus)
        soc_h += c_h * eta_h
    resid_frac = (bought.sum() / Y) / 8760.0
    ldes_efc = ((h2_dis.sum() / Y / eta_h) / max(B_h2, 1e-9) / 365.0) if B_h2 > 0 else 0.0
    return resid_frac, ldes_efc
