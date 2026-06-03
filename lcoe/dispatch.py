from __future__ import annotations

"""Vectorised chronological dispatch over the 3D scenario grid."""
from typing import Tuple

import numpy as np

from .params import SystemParams, BatteryParams, WorkloadProfile
from .weather import solar_clearsky, generate_weather_year


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
                 mean_wind_ms: float, seed: int = 0):
        self.sys       = sys
        self.batt      = batt
        self.workload  = workload
        self.clearsky  = solar_clearsky(mean_irr)
        self.mean_wind = mean_wind_ms

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

        print(f"  [Sim] {sys.n_mc_weather} MC years × {n_scen} scenarios "
              f"({ns}³ grid, C_sol 0–{sys.c_sol_max}×, "
              f"C_win 0–{sys.c_win_max}×, B 0–{sys.storage_hours_max}h) …")
        (self.gas_mean, self.gas_p90, self.fec_mean, self.gas_peak_mean,
         self.gas_peak_firm_mean, self.drop_mean,
         self.sol_cf_mean, self.win_cf_mean) = self._run_mc(seed)
        print("  [Sim] Done.")

    def _dispatch_one_year(self, sol_tr: np.ndarray,
                            win_tr: np.ndarray) -> Tuple[np.ndarray, np.ndarray,
                                                         np.ndarray, np.ndarray,
                                                         float, float]:
        """
        Vectorised combined dispatch for all (C_sol, C_win, B) scenarios.
        Both sources feed the same load; battery arbitrates between them.
        Flexibility = SHEDDING (v5.2): up to `interruptible_fraction` of each
        deficit hour's load may be dropped (no recovery).
        Returns gas_frac, fec_daily, gas_peak, drop_frac (each ns^3,), cf_sol, cf_win.
        """
        eta_chg = self.batt.roundtrip_efficiency ** 0.5
        eta_dis = self.batt.roundtrip_efficiency ** 0.5
        flex    = self.workload.interruptible_fraction   # max sheddable share / hour
        n       = len(self._cs)

        soc = np.zeros(n); gas = np.zeros(n); drop = np.zeros(n)
        soc_sum = np.zeros(n); soc_sum2 = np.zeros(n)
        gas_peak = np.zeros(n)        # peak residual AFTER shedding (interruptible case)
        gas_peak_firm = np.zeros(n)   # peak residual with NO shedding (firm backup sizing)

        for t in range(8760):
            g = self._cs * sol_tr[t] + self._cw * win_tr[t]
            net     = g - 1.0
            deficit = np.maximum(-net, 0.0)
            surplus = np.maximum(net, 0.0)

            # ── Deficit side ──────────────────────────────────────────────────
            # 1. Discharge battery
            dis = np.minimum(np.minimum(soc * eta_dis, self._batt_pow), deficit)
            soc -= dis / eta_dis;  deficit -= dis
            gas_peak_firm = np.maximum(gas_peak_firm, deficit)  # pre-shed (firm)

            # 2. SHED up to `flex` of the load (compute lost; NOT recovered).
            #    Whether this shed is ECONOMIC is decided later in the optimiser
            #    (shed only if compute value < gas variable cost); the dispatch
            #    just records the max-sheddable case and the no-shed (firm) case.
            shed = np.minimum(flex, deficit)
            drop += shed
            deficit -= shed

            # 3. Gas backup covers the (now reduced) residual deficit
            gas_peak = np.maximum(gas_peak, deficit)
            gas += deficit

            # ── Surplus side ──────────────────────────────────────────────────
            # 4. Charge battery with surplus (anything left is curtailed).
            chg = np.minimum(np.minimum((self._soc_max - soc) / eta_chg,
                                         self._batt_pow), surplus)
            soc += chg * eta_chg

            soc_sum  += soc
            soc_sum2 += soc * soc

        # DoD-weighted FEC
        soc_mean_yr = soc_sum / 8760.0
        soc_var_yr  = np.maximum(soc_sum2 / 8760.0 - soc_mean_yr ** 2, 0.0)
        soc_std_yr  = np.sqrt(soc_var_yr)
        with np.errstate(divide='ignore', invalid='ignore'):
            dod_eff  = np.where(self._soc_max > 0,
                                np.clip(2.0 * soc_std_yr /
                                        np.where(self._soc_max > 0, self._soc_max, 1.0),
                                        0.0, 1.0),
                                0.0)
        fec_daily = dod_eff ** self.batt.dod_exponent

        return (gas / 8760.0, fec_daily, gas_peak, gas_peak_firm, drop / 8760.0,
                float(sol_tr.mean()), float(win_tr.mean()))

    def _run_mc(self, seed: int):
        rng = np.random.default_rng(seed)
        N   = self.sys.n_mc_weather
        sh  = (len(self._cs),)

        all_gas = np.zeros((N,) + sh)
        all_fec = np.zeros((N,) + sh)
        all_gas_peak = np.zeros((N,) + sh)
        all_gas_peak_firm = np.zeros((N,) + sh)
        all_drop = np.zeros((N,) + sh)
        cf_s_list, cf_w_list = [], []

        for i in range(N):
            sol_tr, win_tr = generate_weather_year(
                self.clearsky, self.mean_wind, rng,
                wind_solar_corr=self.sys.wind_solar_corr,
                syn_loading=self.sys.syn_loading,
                syn_persistence=self.sys.syn_persistence,
                cloud_ar1=self.sys.cloud_ar1,
                wind_ar1=self.sys.wind_ar1,
                wind_daily_share=self.sys.wind_daily_share,
                wind_seasonal_amp=self.sys.wind_seasonal_amp)
            gas_f, fec_d, gas_p, gas_pf, drop_f, cf_s, cf_w = self._dispatch_one_year(sol_tr, win_tr)
            all_gas[i] = gas_f;  all_fec[i] = fec_d; all_gas_peak[i] = gas_p
            all_gas_peak_firm[i] = gas_pf; all_drop[i] = drop_f
            cf_s_list.append(cf_s);  cf_w_list.append(cf_w)

        return (all_gas.mean(0), np.percentile(all_gas, 90, axis=0),
                all_fec.mean(0), all_gas_peak.mean(0), all_gas_peak_firm.mean(0),
                all_drop.mean(0),
                float(np.mean(cf_s_list)), float(np.mean(cf_w_list)))

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


