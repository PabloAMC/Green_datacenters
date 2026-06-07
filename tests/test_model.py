"""
Regression & unit tests for the off-grid datacenter LCOE model.

Runs WITHOUT pytest (plain asserts + a __main__ runner) so it needs no extra
dependency, and is ALSO collectable by pytest if installed:

    python tests/test_model.py        # standalone
    pytest tests/test_model.py        # if pytest is available

The tests lock in the model's documented "verified" numbers (Wright's Law,
carbon trajectory, gas/battery LCOE), the weather-CF marginals, the dispatch
energy balance at known corner cases, the firm/economic-shed consistency, and
RE-constraint feasibility at the optimum. They are the regression net that lets
shared-code refactors be proven non-behavioural.
"""
import math
import os
import sys

import numpy as np

# Import the model regardless of where the test is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import datacenter_lcoe as m  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _tiny_sim(workload=None, grid_steps=4, n_mc=4, seed=0,
              mean_irr=5.5, mean_wind_ms=7.5, sysp=None):
    """A small, fast ChronologicalSimulator for integration-style tests."""
    workload = workload or m.FIRM
    sysp = sysp or m._sys_with(m.SYSTEM, grid_steps=grid_steps, n_mc_weather=n_mc)
    return m.ChronologicalSimulator(sysp, m.BATTERY_US, workload,
                                    mean_irr, mean_wind_ms, seed=seed)


# ── 1. Cost-trajectory formulas (documented "verified" values) ────────────────

def test_wrights_law_solar_trajectory():
    # v5.7: deployment additions grow with a *decaying* rate (S-curve), so cumulative
    # solar lands ~15.6 TW by 2040 (not the old constant-growth 38 TW) and the learning-
    # curve LCOE decline is correspondingly less aggressive at the long end.
    cum = m.cumulative_capacity(m.SOLAR, 15)
    lcoe = m.wright_law(m.SOLAR.lcoe_today, m.SOLAR.cumulative_gw_2025, cum,
                        m.SOLAR.learning_rate)
    for yr, exp in {0: 52.0, 3: 39.0, 5: 33.9, 10: 26.2, 15: 21.9}.items():
        assert abs(lcoe[yr] - exp) < 0.1, f"solar LCOE {2025+yr}: {lcoe[yr]} != {exp}"
    # cumulative stays well below the old implausible constant-growth path
    assert 14_000 < cum[15] < 17_000, f"2040 cumulative solar {cum[15]:.0f} GW off target"


def test_deployment_decay_reduces_to_legacy_when_off():
    # additions_growth_decay=1.0 must reproduce the legacy constant-growth formula exactly.
    legacy = m.replace(m.SOLAR, additions_growth_rate=0.15, additions_growth_decay=1.0)
    cum = m.cumulative_capacity(legacy, 15)
    import numpy as np
    ref = np.empty(16); ref[0] = legacy.cumulative_gw_2025
    for i in range(1, 16):
        ref[i] = ref[i-1] + legacy.annual_additions_gw * (1.0 + 0.15) ** i
    assert np.allclose(cum, ref)
    assert abs(cum[15] - 38466) < 1.0                      # the old 38 TW path


def test_carbon_trajectory_eu_logistic():
    doc = {0: 70.0, 2: 77.1, 4: 89.4, 5: 97.8, 8: 131.0,
           10: 154.2, 13: 179.6, 15: 189.0}
    for ti, exp in doc.items():
        got = m.carbon_price(m.GAS_EU, ti)
        assert abs(got - exp) < 0.5, f"carbon {2025+ti}: {got} != {exp}"


def test_carbon_trajectory_modes():
    # linear US default is flat $0
    assert m.carbon_price(m.GAS, 10) == 0.0
    # logistic is normalised so p(0) == p0 exactly
    assert abs(m.carbon_price(m.GAS_EU, 0) - m.GAS_EU.carbon_price_today) < 1e-9
    # step jumps at the midpoint
    g = m.GasParams(carbon_trajectory="step", carbon_price_today=10.0,
                    carbon_price_ceiling=200.0, carbon_trajectory_midpoint=8.0)
    assert m.carbon_price(g, 7) == 10.0
    assert m.carbon_price(g, 8) == 200.0


def test_gas_pure_lcoe():
    # At the model default gas WACC (9%, v5.3 per-tech financing)
    assert abs(m.gas_pure_lcoe(m.GAS, 0, m.GAS.wacc) - 46.05) < 0.1
    assert abs(m.gas_pure_lcoe(m.GAS_EU, 0, m.GAS_EU.wacc) - 113.75) < 0.2
    assert abs(m.gas_pure_lcoe(m.GAS_EU, 10, m.GAS_EU.wacc) - 148.29) < 0.2
    # At the legacy flat 7% it recovers the old reference (capex term only changes)
    assert abs(m.gas_pure_lcoe(m.GAS, 0, 0.07) - 43.7) < 0.1


def test_h2_dispatch_vec_monotone():
    """More electrolyser + H2 storage → less bought H2 (year-vectorised 2-storage)."""
    import numpy as np
    from lcoe.dispatch import dispatch_h2_vec
    from lcoe.weather import solar_clearsky, generate_weather_year
    cs = solar_clearsky(3.8); rng = np.random.default_rng(0)
    ny = 4; sols = np.empty((ny, 8760)); wins = np.empty((ny, 8760))
    for k in range(ny):
        s, w = generate_weather_year(cs, 7.0, rng, -0.35, 0.5, 0.85)
        sols[k] = s; wins[k] = w
    lp = min(1, 4 / 6)
    b0, _ = dispatch_h2_vec(sols, wins, 11, 10, 6, lp, 0.924, 0.0, 0.0, 1.0, 0.35)
    b1, _ = dispatch_h2_vec(sols, wins, 11, 10, 6, lp, 0.924, 48.0, 0.5, 1.0, 0.35)
    b2, _ = dispatch_h2_vec(sols, wins, 11, 10, 6, lp, 0.924, 168.0, 1.0, 1.0, 0.35)
    assert b0 > b1 > b2 and b2 < 0.02


def test_h2_system_optimises_and_is_zero_carbon():
    """The gas-free H2 system optimiser returns a finite cheaper-over-time trajectory
    whose per-year components sum to the reported LCOE (no gas/carbon band)."""
    import numpy as np
    from lcoe.params import REGIONS
    from lcoe.h2system import h2_system_trajectory
    cfg = REGIONS["eu"]
    out = h2_system_trajectory(cfg["solar"], cfg["wind"], cfg["battery"],
                               cfg["mean_irr"], cfg["mean_wind_ms"], cfg["sys"],
                               years=3, seed=42, n_mc=4)
    comps = ["gen_capex", "gen_om", "lfp_capex", "lfp_om", "elec_capex",
             "store_capex", "turbine_capex", "buy_h2"]
    assert np.allclose([sum(out[c][i] for c in comps) for i in range(4)], out["lcoe"])
    assert out["lcoe"][-1] < out["lcoe"][0]            # cost falls over time
    assert all(0.0 <= out["buy_frac"][i] <= 1.0 for i in range(4))


def test_h2_cost_paths_share_one_formula():
    """The gas-free H₂ system cost is computed by ONE shared formula
    (`costs.h2_system_cost_split`), used by both `h2system._costs` (the fig1/fig6
    trajectory) and `analysis.run_ldes_joint`. Lock that `_costs` delegates to it so the
    two paths can never drift apart (they previously duplicated the math)."""
    import numpy as np
    from lcoe.params import REGIONS, LDES_PRESETS, GAS_H2
    from lcoe.h2system import _costs, _B_LFP
    from lcoe.costs import (cumulative_capacity, wright_law, rewacc_lcoe, crf,
                            h2_system_cost_split)
    from lcoe.weather import solar_clearsky, generate_weather_year
    from lcoe.dispatch import dispatch_h2_vec
    cfg = REGIONS["eu"]; batt = cfg["battery"]; ldes = LDES_PRESETS["h2"]
    n = cfg["sys"].project_lifetime_yr; i = 8
    ls = rewacc_lcoe(wright_law(cfg["solar"].lcoe_today, cfg["solar"].cumulative_gw_2025,
            cumulative_capacity(cfg["solar"], 15), cfg["solar"].learning_rate), cfg["solar"])[i]
    lw = rewacc_lcoe(wright_law(cfg["wind"].lcoe_today, cfg["wind"].cumulative_gw_2025,
            cumulative_capacity(cfg["wind"], 15), cfg["wind"].learning_rate), cfg["wind"])[i]
    cum_b = cumulative_capacity(batt, 15)
    lfp_kwh = float(wright_law(batt.capex_kwh_today, batt.cumulative_gwh_2025, cum_b, batt.learning_rate)[i])
    lfp_kw = float(wright_law(batt.capex_kw_today, batt.cumulative_gwh_2025, cum_b, batt.learning_rate)[i])
    ch = float(wright_law(ldes.capex_kw_today, ldes.cumulative_gwh_2025,
                          cumulative_capacity(ldes, 15), ldes.learning_rate)[i])
    h2buy = GAS_H2.gas_price_mmbtu * GAS_H2.ccgt_heat_rate + GAS_H2.vom_mwh
    crf_l = crf(ldes.wacc, n); annuity = (1 - (1 + ldes.wacc) ** (-n)) / ldes.wacc
    cs = solar_clearsky(cfg["mean_irr"]); rng = np.random.default_rng(0)
    S = np.array([generate_weather_year(cs, cfg["mean_wind_ms"], rng, -0.35, 0.5, 0.85)[0] for _ in range(4)])
    W = np.array([generate_weather_year(cs, cfg["mean_wind_ms"], rng, -0.35, 0.5, 0.85)[1] for _ in range(4)])
    ctx = dict(batt=batt, ldes=ldes, n=n, sol2d=S, win2d=W, CF_sol=float(S.mean()),
               CF_win=float(W.mean()), lcoe_sol=ls, lcoe_win=lw, om_sol=cfg["solar"].om_frac_lcoe,
               om_win=cfg["wind"].om_frac_lcoe, lfp_kwh=lfp_kwh, lfp_kw=lfp_kw, ch_capex=ch,
               e_capex=ldes.capex_kwh_today, dis_capex=ldes.discharge_capex_kw,
               h2_buy_base=h2buy, crf_l=crf_l, annuity=annuity)
    x = np.array([7.0, 2.0, 1.0, 120.0])
    total_costs, comp_costs, resid = _costs(x, ctx)
    # independent direct call to the shared helper at the same design + dispatch
    lfp_pow = 1.0 if _B_LFP <= 4.0 else min(1.0, 4.0 / _B_LFP)
    r2, efc2 = dispatch_h2_vec(S, W, 7.0, 2.0, _B_LFP, lfp_pow, batt.roundtrip_efficiency,
                               120.0, 1.0, 1.0, ldes.roundtrip_efficiency)
    comp_direct = h2_system_cost_split(7.0, 2.0, _B_LFP, 1.0, 120.0, r2, efc2, batt=batt,
        ldes=ldes, n=n, CF_sol=float(S.mean()), CF_win=float(W.mean()), lcoe_sol=ls,
        lcoe_win=lw, om_sol=cfg["solar"].om_frac_lcoe, om_win=cfg["wind"].om_frac_lcoe,
        lfp_kwh=lfp_kwh, lfp_kw=lfp_kw, ch_capex=ch, e_capex=ldes.capex_kwh_today,
        dis_capex=ldes.discharge_capex_kw, h2_buy=h2buy, crf_l=crf_l, annuity=annuity)
    assert abs(total_costs - sum(comp_direct.values())) < 1e-9
    assert comp_costs == comp_direct


def test_ldes_cost_and_overlay():
    """LDES cost rises with storage/power; the 2-storage overlay displaces gas
    (more LDES energy → lower gas fraction at a fixed deficit-prone build)."""
    import numpy as np
    from lcoe.costs import ldes_annual_cost
    from lcoe.dispatch import dispatch_ldes_overlay
    from lcoe.weather import solar_clearsky
    h = m.LDES_H2
    c24 = ldes_annual_cost(h, 24, 20.0, 1200.0, 1300.0, 0.35, 1.0, h.wacc, 20, 0.0)
    c96 = ldes_annual_cost(h, 96, 20.0, 1200.0, 1300.0, 0.35, 1.0, h.wacc, 20, 0.0)
    assert 0 < c24 < c96                         # more storage → more cost
    assert ldes_annual_cost(h, 0, 20, 1200, 1300, 0.35, 1.0, h.wacc, 20) == 0.0
    # overlay: a deficit-prone build (modest overbuild), LDES displaces gas
    cs = solar_clearsky(3.8)
    rng = np.random.default_rng(0)
    wkw = dict(wind_solar_corr=-0.35, syn_loading=0.5, syn_persistence=0.85)
    gas_frac, _peak, _le, _de = dispatch_ldes_overlay(
        cs, 7.0, rng, wkw, 4.0, 3.0, 6.0, 0.667, 0.924,
        np.array([0.0, 48.0, 168.0]), 0.5, 1.0, 0.40, 3)
    assert gas_frac[0] >= gas_frac[1] >= gas_frac[2]   # more LDES → less gas
    assert gas_frac[2] < gas_frac[0]


def test_dispatch_h2_vec_residual_falls_with_capacity():
    """The years-vectorised joint-dispatch: a bigger electrolyser + H2 store leaves
    less for the market to cover, and the bought share is a valid fraction."""
    import numpy as np
    from lcoe.dispatch import dispatch_h2_vec
    from lcoe.weather import solar_clearsky
    cs = solar_clearsky(3.8)
    rng = np.random.default_rng(0)
    sols, wins = [], []
    for _ in range(4):
        s, w = m.generate_weather_year(cs, 7.0, rng, -0.35, 0.5, 0.85)
        sols.append(s); wins.append(w)
    sol2d, win2d = np.array(sols), np.array(wins)
    args = (sol2d, win2d, 8.0, 6.0, 6.0, 0.667, 0.924)   # build + LFP
    small, _ = dispatch_h2_vec(*args, 0.0, 0.0, 1.0, 0.35)      # no H2
    big, _ = dispatch_h2_vec(*args, 168.0, 1.0, 1.0, 0.35)      # 1 wk store, full elec
    assert 0.0 <= big <= small <= 1.0 and big < small


def test_ldes_presets():
    # tanks are the default (no cavern); cavern is much cheaper energy; asymmetric power
    assert m.LDES_H2.capex_kwh_today == 20.0          # above-ground tanks
    assert m.LDES_H2_CAVERN.capex_kwh_today < 1.0     # salt cavern ~$0.6/kWh
    assert m.LDES_H2.charge_power_mw < m.LDES_H2.discharge_power_mw  # small electrolyser
    assert set(m.LDES_PRESETS) == {"iron-air", "h2", "h2-cavern"}


def test_firm_clean_firming_presets():
    """Geothermal & hydro firming: firm, zero-carbon, no fuel, flat over time; cheap
    delivered LCOE at their baseload CF; registered for --firming."""
    assert m.GEOTHERMAL.carbon_intensity_ccgt == 0.0 and m.GEOTHERMAL.gas_price_mmbtu == 0.0
    assert m.HYDRO.carbon_intensity_ccgt == 0.0 and m.HYDRO.gas_price_mmbtu == 0.0
    geo = m.gas_pure_lcoe(m.GEOTHERMAL, 0, m.GEOTHERMAL.wacc, cf=0.88)
    hyd = m.gas_pure_lcoe(m.HYDRO, 0, m.HYDRO.wacc, cf=0.55)
    # IRENA 2023 capex; LCOE at the model's own WACC sits just below IRENA's published
    # LCOE ($71 geothermal / $57 hydro, at IRENA's ~7.5% WACC).
    assert 55.0 < geo < 72.0, f"geothermal LCOE {geo:.1f} out of band"
    assert 40.0 < hyd < 58.0, f"hydro LCOE {hyd:.1f} out of band"
    # flat over time (no fuel/carbon escalation)
    assert abs(geo - m.gas_pure_lcoe(m.GEOTHERMAL, 15, m.GEOTHERMAL.wacc, cf=0.88)) < 1e-6
    # zero-carbon → cheaper than its carbon term would ever add; and cf param defaults to 0.85
    assert m.FIRMING_PRESETS["geothermal"] is m.GEOTHERMAL
    assert m.FIRMING_PRESETS["hydro"] is m.HYDRO
    # gas_pure_lcoe default cf unchanged (backward-compat)
    assert abs(m.gas_pure_lcoe(m.GAS, 0, m.GAS.wacc) - m.gas_pure_lcoe(m.GAS, 0, m.GAS.wacc, cf=0.85)) < 1e-9


def test_h2_firming_preset():
    """Green-H2 firming: zero combustion carbon, pricier fuel, flat over time."""
    assert m.GAS_H2.carbon_intensity_ccgt == 0.0 and m.GAS_H2.carbon_intensity_ocgt == 0.0
    h2_2025 = m.gas_pure_lcoe(m.GAS_H2, 0, m.GAS_H2.wacc)
    h2_2040 = m.gas_pure_lcoe(m.GAS_H2, 15, m.GAS_H2.wacc)
    assert h2_2025 > m.gas_pure_lcoe(m.GAS, 0, m.GAS.wacc)   # pricier zero-carbon fuel
    assert abs(h2_2025 - h2_2040) < 1e-6                     # flat: no carbon escalation
    assert m.FIRMING_PRESETS["gas"] is None and m.FIRMING_PRESETS["h2"] is m.GAS_H2


def test_grid_cfe_above_ppa_by_premium():
    """24/7 CFE reference = annual-matching PPA line + a flat hourly-matching premium."""
    lcoe_sol = m.wright_law(m.SOLAR.lcoe_today, m.SOLAR.cumulative_gw_2025,
                            m.cumulative_capacity(m.SOLAR, 15), m.SOLAR.learning_rate)
    ppa = m.grid_ppa_trajectory(m.GRID_PPA, lcoe_sol)
    cfe = m.grid_cfe_trajectory(m.GRID_PPA, lcoe_sol)
    import numpy as np
    assert np.allclose(cfe - ppa, m.GRID_PPA.cfe_premium_mwh)
    assert all(cfe > ppa)


def test_per_tech_wacc_defaults_and_direction():
    # the chosen differentiated scheme
    assert m.SOLAR.wacc == 0.055 and m.WIND.wacc == 0.055
    assert m.BATTERY_US.wacc == 0.07 and m.GAS.wacc == 0.09
    # higher gas WACC raises gas cost; cheaper RE WACC lowers generation cost
    assert m.gas_pure_lcoe(m.GAS, 0, m.GAS.wacc) > m.gas_pure_lcoe(m.GAS, 0, m.LEGACY_WACC)


def test_rewacc_identity_and_monotonicity():
    import numpy as np
    base = np.array([52.0, 30.0, 13.7])
    # identity at the legacy WACC
    ident = m.TechParams("t", 52.0, 0.3, 1, 1, 0.1, om_frac_lcoe=0.15,
                         wacc=m.LEGACY_WACC, life_yr=30)
    assert np.allclose(m.rewacc_lcoe(base, ident), base)
    # cheaper capital strictly lowers the LCOE; the multiplier is constant across years
    adj = m.rewacc_lcoe(base, m.SOLAR)
    assert all(adj < base)
    assert np.allclose(adj / base, (adj / base)[0])   # same factor every year


def test_battery_delivered_cost():
    # v5.4 augmentation model (US, 7% WACC, 0.5 EFC/day)
    doc = {2: 7.4, 4: 13.1, 8: 23.6, 12: 34.7, 24: 68.6}
    for h, exp in doc.items():
        ann = m.battery_annualised_cost(m.BATTERY_US, h, 180.0, 140.0, 0.07, 20, 0.5)
        assert abs(ann / 8760.0 - exp) < 0.1, f"battery {h}h: {ann/8760:.2f} != {exp}"


def test_battery_cost_increases_with_cycling():
    """More throughput (higher EFC/day) → more augmentation → higher cost."""
    lo = m.battery_annualised_cost(m.BATTERY_US, 6, 180.0, 140.0, 0.07, 20, 0.3)
    hi = m.battery_annualised_cost(m.BATTERY_US, 6, 180.0, 140.0, 0.07, 20, 1.0)
    assert hi > lo


def test_crf():
    assert abs(m.crf(0.07, 20) - 0.0944) < 1e-4
    assert abs(m.crf(0.0, 20) - 0.05) < 1e-12   # zero-rate branch = 1/n


def test_grid_ppa_trajectory():
    """Reference line: base year = sum of components; declines with the solar
    learning curve toward a delivery+firming floor; never below that floor."""
    lcoe_sol = m.wright_law(m.SOLAR.lcoe_today, m.SOLAR.cumulative_gw_2025,
                            m.cumulative_capacity(m.SOLAR, 15), m.SOLAR.learning_rate)
    traj = m.grid_ppa_trajectory(m.GRID_PPA, lcoe_sol)
    floor = m.GRID_PPA.grid_delivery_mwh + m.GRID_PPA.firming_premium_mwh
    # base year = energy + delivery + firming
    assert abs(traj[0] - (m.GRID_PPA.ppa_energy_today + floor)) < 1e-9
    # monotone non-increasing (solar LCOE falls every year)
    assert all(traj[i + 1] <= traj[i] + 1e-9 for i in range(len(traj) - 1))
    # stays above the flat delivery+firming floor
    assert all(v >= floor - 1e-9 for v in traj)
    # 2040 energy component scales with the solar ratio
    exp_2040 = m.GRID_PPA.ppa_energy_today * (lcoe_sol[15] / lcoe_sol[0]) + floor
    assert abs(traj[15] - exp_2040) < 1e-9


def test_generation_degradation_knob():
    import numpy as np
    base = np.array([52.0, 30.0])
    # default: degradation off → identical to no-degradation transform
    assert m.SOLAR.degradation_per_yr == 0.0
    # turning it on inflates delivered LCOE by 1/(1-½·deg·life)
    degraded = m.replace(m.SOLAR, degradation_per_yr=0.005)   # 0.5%/yr
    hi = m.rewacc_lcoe(base, degraded)
    lo = m.rewacc_lcoe(base, m.SOLAR)
    factor = 1.0 / (1.0 - 0.5 * 0.005 * m.SOLAR.life_yr)
    assert np.allclose(hi, lo * factor)
    assert all(hi > lo)


def test_weather_params_promoted_with_legacy_defaults():
    # the promoted magic numbers must keep their original hardcoded values, so the
    # weather (and the whole headline) is unchanged
    s = m.SystemParams()
    assert (s.cloud_ar1, s.wind_ar1, s.wind_daily_share, s.wind_seasonal_amp) \
        == (0.35, 0.75, 0.50, 0.12)


# ── 2. DRY-refactor invariants: scalar == sum of its split ────────────────────

def test_battery_scalar_equals_split_sum():
    for h in [0.0, 2.0, 4.0, 6.0, 12.0, 24.0]:
        ann = m.battery_annualised_cost(m.BATTERY_EU, h, 180.0, 175.0, 0.07, 20, 0.37)
        cap, opx = m.battery_cost_split(m.BATTERY_EU, h, 180.0, 175.0, 0.07, 20, 0.37)
        assert abs(ann - (cap + opx)) < 1e-9, f"battery split mismatch at {h}h"


def test_gas_scalar_equals_split_sum():
    for f_gas in [0.0, 0.03, 0.05, 0.15, 0.25, 0.6]:
        for ti in [0, 10]:
            for gas in [m.GAS, m.GAS_EU]:
                scal = m.gas_backup_cost_scalar(f_gas, gas, ti, 0.07, gas_peak=0.8)
                parts = m.gas_cost_split(f_gas, gas, ti, 0.07, gas_peak=0.8)
                assert abs(scal - sum(parts.values())) < 1e-9, \
                    f"gas split mismatch f_gas={f_gas} ti={ti} {gas.name}"


def test_ccgt_ocgt_threshold():
    # OCGT below 20% gas fraction, CCGT at/above — both functions must agree.
    lo = m.gas_cost_split(0.10, m.GAS, 0, 0.07)
    hi = m.gas_cost_split(0.30, m.GAS, 0, 0.07)
    # OCGT has cheaper capex but higher heat rate than CCGT; normalise by f_gas.
    assert lo["fuel"] / 0.10 > hi["fuel"] / 0.30   # OCGT burns more fuel/MWh


# ── 3. Weather: marginals preserved; synoptic factor only re-clusters ─────────

def test_solar_cf_target():
    # v5.5: the clear-sky mean is pre-divided by the cloud mean so the EFFECTIVE
    # (post-cloud) annual CF lands at irradiance/24 — removing the v5.4 cloud
    # double-count. So clear-sky mean ≈ (irr/24)/CLOUD_MEAN, and the simulated CF ≈ irr/24.
    from lcoe.weather import CLOUD_MEAN
    cs = m.solar_clearsky(5.5)
    assert abs(cs.mean() - (5.5 / 24.0) / CLOUD_MEAN) < 0.01, f"clearsky mean {cs.mean():.3f}"
    rng = np.random.default_rng(0)
    eff = np.mean([m.generate_weather_year(cs, 7.5, rng, 0.0, 0.5, 0.82)[0].mean()
                   for _ in range(8)])
    assert abs(eff - 5.5 / 24.0) < 0.015, f"effective solar CF {eff:.3f} != {5.5/24:.3f}"


def test_cf_marginals_preserved_under_synoptic():
    """Turning the synoptic factor on (λ=0.5) vs off (λ=0) must not move the
    annual-mean capacity factors — only the temporal clustering of lows."""
    cs = m.solar_clearsky(3.8)
    def mean_cf(lam, seed):
        rng = np.random.default_rng(seed)
        s_acc, w_acc, n = 0.0, 0.0, 8
        for _ in range(n):
            s, w = m.generate_weather_year(cs, 7.0, rng, -0.35, lam, 0.85)
            s_acc += s.mean(); w_acc += w.mean()
        return s_acc / n, w_acc / n
    s_off, w_off = mean_cf(0.0, 123)
    s_on,  w_on  = mean_cf(0.5, 123)
    assert abs(s_on - s_off) < 0.01, f"solar CF moved: {s_off:.3f}->{s_on:.3f}"
    assert abs(w_on - w_off) < 0.015, f"wind CF moved: {w_off:.3f}->{w_on:.3f}"


def test_wind_cf_in_expected_band():
    # v5.5: modern low-specific-power curve (rated 11 m/s) → US wind CF ~0.33,
    # inside Lazard v18's onshore CF basis (0.30–0.55) the wind LCOE is quoted at.
    cs = m.solar_clearsky(5.5)
    rng = np.random.default_rng(7)
    w = np.mean([m.generate_weather_year(cs, 7.5, rng, 0.0, 0.5, 0.82)[1].mean()
                 for _ in range(6)])
    assert 0.30 < w < 0.38, f"US wind CF out of band: {w:.3f}"


def test_cf_consistent_with_lazard_basis():
    """v5.5 invariant: the simulated CFs sit inside the Lazard v18 CF bands the
    imported generation LCOEs are levelised at (utility solar 0.20–0.30, onshore
    wind 0.30–0.55) — so cost basis and dispatch refer to the same plant."""
    import numpy as np
    def cf(mi, mw, corr, phi):
        cs = m.solar_clearsky(mi); rng = np.random.default_rng(1)
        sw = [m.generate_weather_year(cs, mw, rng, corr, 0.5, phi) for _ in range(8)]
        return np.mean([s.mean() for s, _ in sw]), np.mean([w.mean() for _, w in sw])
    s_us, w_us = cf(5.5, 7.5, 0.0, 0.82)
    s_eu, w_eu = cf(3.8, 7.0, -0.35, 0.85)
    assert 0.20 <= s_us <= 0.30, f"US solar CF {s_us:.3f} outside Lazard basis"
    assert 0.30 <= w_us <= 0.55, f"US wind CF {w_us:.3f} outside Lazard basis"
    assert 0.13 <= s_eu <= 0.22, f"EU solar CF {s_eu:.3f}"   # lower-irradiance EU site
    assert 0.25 <= w_eu <= 0.45, f"EU wind CF {w_eu:.3f}"


# ── 4. Dispatch energy balance at known corner cases ──────────────────────────

def test_zero_build_is_all_gas():
    """With no generation, every hour is a full deficit served by gas:
    gas fraction = 1.0, peak = 1.0, no shed, no cycling — at any battery size."""
    sim = _tiny_sim(grid_steps=4, n_mc=2)
    # (C_sol, C_win, B) = (0,0,0) and (0,0,B_max): gas must be 100% either way.
    assert abs(sim.interp3(sim.gas_mean, 0.0, 0.0, 0.0) - 1.0) < 1e-9
    assert abs(sim.interp3(sim.gas_mean, 0.0, 0.0, m.SYSTEM.storage_hours_max) - 1.0) < 1e-6
    assert abs(sim.interp3(sim.gas_peak_firm_mean, 0.0, 0.0, 0.0) - 1.0) < 1e-9
    assert abs(sim.interp3(sim.fec_mean, 0.0, 0.0, 0.0)) < 1e-9


def test_more_overbuild_reduces_gas():
    """Gas fraction must be monotonically non-increasing in solar/wind overbuild."""
    sim = _tiny_sim(grid_steps=5, n_mc=3)
    g0 = sim.interp3(sim.gas_mean, 0.0, 0.0, 6.0)
    g1 = sim.interp3(sim.gas_mean, m.SYSTEM.c_sol_max / 2, m.SYSTEM.c_win_max / 2, 6.0)
    g2 = sim.interp3(sim.gas_mean, m.SYSTEM.c_sol_max, m.SYSTEM.c_win_max, 6.0)
    assert g0 >= g1 >= g2, f"gas not monotone in overbuild: {g0:.3f},{g1:.3f},{g2:.3f}"
    assert g0 == 1.0 and g2 < 0.5


def test_firm_workload_never_sheds():
    sim = _tiny_sim(workload=m.FIRM, grid_steps=4, n_mc=2)
    assert np.allclose(sim.drop_mean, 0.0), "FIRM workload shed a nonzero fraction"
    # firm peak == post-shed peak when nothing is sheddable
    assert np.allclose(sim.gas_peak_mean, sim.gas_peak_firm_mean)


def test_interruptible_sheds_and_reduces_gas():
    """A high interruptible fraction must drop load and cut the gas fraction vs firm
    at the same build (the dispatch records the max-sheddable case)."""
    firm = _tiny_sim(workload=m.FIRM, grid_steps=4, n_mc=3, seed=1)
    flex = _tiny_sim(workload=m.BEST_EFFORT, grid_steps=4, n_mc=3, seed=1)
    # at a modest build there is real deficit to shed
    pt = (2.0, 1.0, 4.0)
    assert flex.interp3(flex.drop_mean, *pt) > 0.0
    assert flex.interp3(flex.gas_mean, *pt) <= firm.interp3(firm.gas_mean, *pt) + 1e-9


# ── 5. Firm reconstruction & economic-shed logic ──────────────────────────────

def test_firm_reconstruction_identity():
    """For an interruptible build, gas-if-firm == gas-with-shed + shed, exactly
    (shedding never perturbs the battery trajectory)."""
    sim = _tiny_sim(workload=m.BEST_EFFORT, grid_steps=4, n_mc=3, seed=2)
    for pt in [(1.0, 1.0, 4.0), (3.0, 2.0, 6.0), (0.5, 0.5, 2.0)]:
        g_shed = sim.interp3(sim.gas_mean, *pt)
        drop = sim.interp3(sim.drop_mean, *pt)
        g_firm = sim.interp3(sim.gas_peak_firm_mean, *pt)  # peak, sanity only
        # reconstructed firm energy fraction:
        # gas_with_shed + dropped energy == gas a firm system would have burned
        # (compare against an independently dispatched firm sim at the same point)
        firm = _tiny_sim(workload=m.FIRM, grid_steps=4, n_mc=3, seed=2)
        g_firm_direct = firm.interp3(firm.gas_mean, *pt)
        assert abs((g_shed + drop) - g_firm_direct) < 1e-6, \
            f"firm reconstruction off at {pt}: {g_shed+drop:.5f} vs {g_firm_direct:.5f}"
        assert g_firm <= 1.0 + 1e-9


# ── 6. Optimiser feasibility ──────────────────────────────────────────────────

def test_optimum_meets_re_constraint():
    """The returned optimum must satisfy the served-RE target (within tolerance)."""
    sim = _tiny_sim(workload=m.FIRM, grid_steps=8, n_mc=4, seed=3)
    cum = m.cumulative_capacity(m.SOLAR, 0)
    lcoe_sol = m.wright_law(m.SOLAR.lcoe_today, m.SOLAR.cumulative_gw_2025, cum, m.SOLAR.learning_rate)[0]
    lcoe_win = m.wright_law(m.WIND.lcoe_today, m.WIND.cumulative_gw_2025,
                            m.cumulative_capacity(m.WIND, 0), m.WIND.learning_rate)[0]
    for R in [0.70, 0.85]:
        out = m.optimal_cost_3d(sim, R, lcoe_sol, lcoe_win, m.BATTERY_US,
                                180.0, 140.0, m.GAS, 0, sim.sys)
        total, c_gen, c_stor, c_gas, c_pen, C_sol, C_win, B, f_drop = out
        # recompute served-RE at the returned build
        f_gas = sim.interp3(sim.gas_mean, C_sol, C_win, B)
        f_re = 1.0 - f_gas  # firm, f_drop==0
        assert f_re >= R - 0.02, f"RE target {R} not met: f_re={f_re:.3f}"
        assert total > 0 and C_sol >= 0 and C_win >= 0 and B >= 0


# ── 7. P90 robustness-design & resource-quality sensitivity ───────────────────

def _quick_sim(mean_irr=5.5, mean_wind_ms=7.5, design_p90=False, years=2,
               R=0.80, seed=5):
    # grid_steps=12: the v5.5 recalibration shifts optima to lower capacity (higher CF),
    # where a coarse 8-step lattice over the wide 0–18× bounds places nodes too sparsely
    # and can pin a worse node for one resource than another — a grid artifact, not an
    # economic one. 12 steps resolves the low-capacity region adequately for the asserts.
    return m.run_simulation(
        solar=m.SOLAR, wind=m.WIND, battery=m.BATTERY_US, gas=m.GAS, smr=m.SMR,
        sys=m._sys_with(m.SYSTEM, grid_steps=12, n_mc_weather=6), workload=m.FIRM,
        mean_irr=mean_irr, mean_wind_ms=mean_wind_ms, years=years,
        reliabilities=[R], n_cost_mc=8, seed=seed, design_p90=design_p90)


def test_p90_design_at_least_mean_cost():
    """Designing against the 1-in-10 (P90) weather surface is a strictly harder
    constraint, so the P90-designed cost can never be below the mean-designed cost
    (proof: cost_p90(x) ≥ cost_mean(x) pointwise, and the P90 build is mean-feasible)."""
    res = _quick_sim(design_p90=True)
    sc = res["scenarios"][0.80]
    assert "opt_delivered_p90" in sc
    mean, p90 = sc["opt_delivered"], sc["opt_delivered_p90"]
    assert all(p90[i] >= mean[i] - 0.5 for i in range(len(mean))), \
        f"P90-designed below mean-designed: {list(zip(mean, p90))}"
    # default run does NOT carry the P90 series (opt-in only)
    assert "opt_delivered_p90" not in _quick_sim(design_p90=False)["scenarios"][0.80]


def test_weather_years_hook_overrides_synthetic():
    """v5.5 reanalysis seam: supplying explicit (solar, wind) traces makes the
    simulator dispatch THOSE years (and report their CF) instead of synthesising —
    the single integration point for real ERA5/NSRDB data."""
    import numpy as np
    cs = m.solar_clearsky(5.5)
    rng = np.random.default_rng(11)
    years = [m.generate_weather_year(cs, 7.5, rng, 0.0, 0.5, 0.82) for _ in range(3)]
    sim = m.ChronologicalSimulator(
        m._sys_with(m.SYSTEM, grid_steps=4, n_mc_weather=99),   # n_mc ignored when traces given
        m.BATTERY_US, m.FIRM, 5.5, 7.5, seed=0, weather_years=years)
    exp_s = float(np.mean([s.mean() for s, _ in years]))
    exp_w = float(np.mean([w.mean() for _, w in years]))
    assert abs(sim.sol_cf_mean - exp_s) < 1e-9 and abs(sim.win_cf_mean - exp_w) < 1e-9
    # zero-build is still all-gas under supplied weather (sanity of dispatch path)
    assert abs(sim.interp3(sim.gas_mean, 0.0, 0.0, 0.0) - 1.0) < 1e-9


def test_resource_presets_consistent():
    """`default` resource must equal the region's headline resource; `good` is
    strictly more energetic on both axes."""
    for rk in ("us", "eu"):
        d_mi, d_mw = m.RESOURCE_PRESETS[rk]["default"]
        g_mi, g_mw = m.RESOURCE_PRESETS[rk]["good"]
        assert d_mi == m.REGIONS[rk]["mean_irr"]
        assert d_mw == m.REGIONS[rk]["mean_wind_ms"]
        assert g_mi > d_mi and g_mw > d_mw


def test_good_resource_lifts_cf_and_lowers_cost():
    d_mi, d_mw = m.RESOURCE_PRESETS["us"]["default"]
    g_mi, g_mw = m.RESOURCE_PRESETS["us"]["good"]
    rd = _quick_sim(d_mi, d_mw, years=1, seed=4)
    rg = _quick_sim(g_mi, g_mw, years=1, seed=4)
    assert rg["sim_cf"]["wind"] > rd["sim_cf"]["wind"]
    assert rg["sim_cf"]["solar"] > rd["sim_cf"]["solar"]
    # a better resource cannot raise the least-cost firm build at a fixed RE target
    assert rg["scenarios"][0.80]["opt_delivered"][0] <= \
        rd["scenarios"][0.80]["opt_delivered"][0] + 0.5


# ── Tier-1 additions: spatial diversification, config sites, weather ingest ─────

def test_spatial_diversification_identity_mean_and_tails():
    """Portfolio weather: n_sites=1 is byte-identical to the single-site generator;
    more sites preserve the mean CF (cost basis untouched) but soften multi-day lulls."""
    import numpy as np
    cs = m.solar_clearsky(5.5)
    kw = dict(wind_solar_corr=0.0, syn_loading=0.5, syn_persistence=0.82)
    s1, w1 = m.generate_weather_year(cs, 7.5, np.random.default_rng(0), **kw)
    s2, w2 = m.generate_weather_portfolio(cs, 7.5, np.random.default_rng(0), n_sites=1, **kw)
    assert np.array_equal(s1, s2) and np.array_equal(w1, w2)   # exact reduction

    def worst3(sol, win):                                       # deepest 3-day lull
        daily = (6.0 * sol + 5.0 * win).reshape(365, 24).mean(1)
        return np.convolve(daily, np.ones(3) / 3, "valid").min()

    cf1s, cf1w, cf6s, cf6w, lull1, lull6 = [], [], [], [], [], []
    for seed in range(8):
        s, w = m.generate_weather_portfolio(cs, 7.5, np.random.default_rng(seed),
                                            n_sites=1, site_synoptic_corr=0.7, **kw)
        cf1s.append(s.mean()); cf1w.append(w.mean()); lull1.append(worst3(s, w))
        s, w = m.generate_weather_portfolio(cs, 7.5, np.random.default_rng(seed),
                                            n_sites=6, site_synoptic_corr=0.7, **kw)
        cf6s.append(s.mean()); cf6w.append(w.mean()); lull6.append(worst3(s, w))
    assert abs(np.mean(cf1s) - np.mean(cf6s)) < 0.01           # mean solar CF preserved
    assert abs(np.mean(cf1w) - np.mean(cf6w)) < 0.01           # mean wind CF preserved
    assert np.mean(lull6) > np.mean(lull1)                     # tails softened


def test_spatial_diversification_default_is_single_site():
    """The shipped default must be n_sites=1 so headline results are unchanged."""
    assert m.SYSTEM.n_sites == 1 and m.SYSTEM_EU.n_sites == 1


def test_site_config_inherits_and_overrides():
    import json, os, tempfile
    spec = {"label": "T", "based_on": "us", "mean_irr": 5.8, "mean_wind_ms": 8.5,
            "gas_price_mmbtu": 3.2, "n_sites": 3, "site_synoptic_corr": 0.6}
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(spec, f); f.close()
    cfg = m.load_site_config(f.name); os.unlink(f.name)
    assert cfg["mean_irr"] == 5.8 and cfg["mean_wind_ms"] == 8.5
    assert cfg["gas"].gas_price_mmbtu == 3.2                   # GasParams override applied
    assert cfg["sys"].n_sites == 3 and cfg["sys"].site_synoptic_corr == 0.6
    assert cfg["solar"] is m.REGIONS["us"]["solar"]            # inherits un-overridden tech
    # the region's own gas is not mutated by the override
    assert m.REGIONS["us"]["gas"].gas_price_mmbtu == 4.0


def test_site_config_rejects_unknown_keys_and_bad_base():
    import json, os, tempfile

    def write(spec):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(spec, f); f.close()
        return f.name

    for bad in ({"based_on": "us", "nonsense": 1}, {"based_on": "mars"}):
        p = write(bad)
        try:
            m.load_site_config(p)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
        finally:
            os.unlink(p)


def test_example_site_file_loads():
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "sites", "example_texas.json")
    cfg = m.load_site_config(path)
    assert cfg["sys"].n_sites >= 1 and cfg["mean_irr"] > 0


def test_weather_npz_roundtrip_and_shape_guard():
    import numpy as np, os, tempfile
    solar = np.random.default_rng(0).uniform(0, 1, (2, 8760))
    wind = np.random.default_rng(1).uniform(0, 1, (2, 8760))
    f = tempfile.NamedTemporaryFile(suffix=".npz", delete=False); f.close()
    np.savez(f.name, solar=solar, wind=wind)
    years = m.load_weather_traces(f.name); os.unlink(f.name)
    assert len(years) == 2 and years[0][0].shape == (8760,)
    assert np.allclose(years[1][1], wind[1])                   # round-trips exactly
    bad = tempfile.NamedTemporaryFile(suffix=".npz", delete=False); bad.close()
    np.savez(bad.name, solar=np.zeros((2, 100)), wind=np.zeros((2, 100)))
    try:
        m.load_weather_traces(bad.name)
        raise AssertionError("expected ValueError on wrong shape")
    except ValueError:
        pass
    finally:
        os.unlink(bad.name)


# ── Tier-3 additions: load profile, external validation, synoptic calibration ───

def test_load_profile_shapes_and_default():
    import numpy as np
    flat = m.load_profile("flat")
    assert flat.shape == (8760,) and np.all(flat == 1.0)
    cool = m.load_profile("cooling")
    assert abs(cool.mean() - 1.0) < 1e-9                  # mean-1: a pure shape
    assert cool.max() > 1.10                              # peak load exceeds average
    assert m.SYSTEM.load_profile == "flat"                # default = constant-load headline


def test_cooling_load_raises_firm_gas_peak():
    """A peaky (cooling) load needs a bigger firm gas plant, since gas is sized to peak."""
    base = _tiny_sim()                                    # flat, seed 0
    cool = _tiny_sim(sysp=m._sys_with(m.SYSTEM, grid_steps=4, n_mc_weather=4,
                                      load_profile="cooling"))
    b = (6.0, 5.0, 6.0)
    assert cool.interp3(cool.gas_peak_firm_mean, *b) > base.interp3(base.gas_peak_firm_mean, *b)


def test_external_validation_published_bands():
    """Sanity-anchor the headline cost inputs against published external ranges
    (Lazard v18 unsubsidised; EIA gas) — a guard that the model stays in the real world."""
    sol = m.rewacc_lcoe(m.wright_law(m.SOLAR.lcoe_today, m.SOLAR.cumulative_gw_2025,
            m.cumulative_capacity(m.SOLAR, 15), m.SOLAR.learning_rate), m.SOLAR)[0]
    win = m.rewacc_lcoe(m.wright_law(m.WIND.lcoe_today, m.WIND.cumulative_gw_2025,
            m.cumulative_capacity(m.WIND, 15), m.WIND.learning_rate), m.WIND)[0]
    assert 29.0 <= sol <= 96.0, f"US solar 2025 ${sol:.1f} outside Lazard v18 band"
    assert 27.0 <= win <= 75.0, f"US wind 2025 ${win:.1f} outside Lazard v18 band"
    gas = m.gas_pure_lcoe(m.GAS, 0, m.GAS.wacc)
    assert 40.0 <= gas <= 110.0, f"US gas ${gas:.1f} outside EIA/Lazard CCGT band"
    r = _quick_sim(5.5, 7.5, years=0, seed=42)
    assert 0.20 <= r["sim_cf"]["solar"] <= 0.30, r["sim_cf"]["solar"]   # Lazard solar CF
    assert 0.30 <= r["sim_cf"]["wind"] <= 0.55, r["sim_cf"]["wind"]     # Lazard wind CF


def test_synoptic_calibration_recovers_direction():
    """The calibrator is a moment estimator: monotone but attenuated. Assert the robust
    properties — it ranks persistence and recovers the sign of the wind-solar correlation."""
    import numpy as np
    from lcoe.weather import solar_clearsky, generate_weather_year
    from tools.calibrate_synoptic import estimate
    cs = solar_clearsky(3.8)

    def paired(phi, rho, Y=18, seed=1):
        rng = np.random.default_rng(seed)
        S, W = [], []
        for _ in range(Y):
            s, w = generate_weather_year(cs, 7.0, rng, wind_solar_corr=rho,
                                         syn_loading=0.5, syn_persistence=phi)
            S.append(s); W.append(w)
        return estimate(np.array(S), np.array(W))

    lo, hi = paired(0.70, 0.0), paired(0.92, 0.0)
    assert hi["syn_persistence"] > lo["syn_persistence"]           # ranks persistence
    assert 0.0 < hi["syn_persistence"] < 1.0
    assert paired(0.85, -0.35)["wind_solar_corr"] < 0.0            # recovers sign of ρ


def test_synoptic_calibration_site_corr_monotone():
    """Multi-site: a more correlated portfolio yields a higher estimated site corr."""
    import math
    import numpy as np
    from lcoe.weather import solar_clearsky, generate_weather_year, _ar1_series
    from tools.calibrate_synoptic import estimate
    cs = solar_clearsky(3.8)

    def multisite(c, S=3, Y=10, seed=3):
        rng = np.random.default_rng(seed)
        sol = np.zeros((S, Y, 8760)); win = np.zeros((S, Y, 8760))
        a, b = math.sqrt(c), math.sqrt(1 - c)
        for y in range(Y):
            fc = _ar1_series(0.85, 365, rng)
            for s in range(S):
                fsite = a * fc + b * _ar1_series(0.85, 365, rng)
                so, wi = generate_weather_year(cs, 7.0, rng, wind_solar_corr=-0.35,
                                               syn_loading=0.5, syn_persistence=0.85,
                                               synoptic_f=fsite)
                sol[s, y] = so; win[s, y] = wi
        return estimate(sol, win)["site_synoptic_corr"]

    assert multisite(0.85) > multisite(0.30)                       # monotone in true c


def test_resource_band_brackets_central():
    """The fig1 geographic/siting band re-optimises at a poor and a good site; the
    default-resource central line must sit inside it, good cheaper than poor."""
    from lcoe.params import resource_band_for
    # grid_steps≥12 so the optimiser resolves the resource ordering (coarser grids are too
    # noisy for the ~$5/MWh good-vs-default gap; the headline runs at 21).
    r = m.run_simulation(
        solar=m.SOLAR, wind=m.WIND, battery=m.BATTERY_US, gas=m.GAS, smr=m.SMR,
        sys=m._sys_with(m.SYSTEM, grid_steps=12, n_mc_weather=12), workload=m.FIRM,
        mean_irr=5.5, mean_wind_ms=7.5, years=0, reliabilities=[0.80], seed=1,
        resource_band=resource_band_for("us"))
    sc = r["scenarios"][0.80]
    assert "opt_delivered_reslo" in sc                       # band computed
    lo, c, hi = (sc["opt_delivered_reslo"][0], sc["opt_delivered"][0],
                 sc["opt_delivered_reshi"][0])
    assert lo < hi                                           # good site < poor site
    assert lo - 1.0 <= c <= hi + 1.0                         # central inside (grid-noise tol)


def test_re_monotonicity_enforced():
    """Optimal delivered cost must be non-decreasing in the renewable target (a looser
    target can always reuse a tighter target's build), so a looser target can never come
    out more expensive. _enforce_re_monotonicity repairs optimiser/grid noise that violates
    this (the cause of the nonsensical US '80% crosses gas before 70%')."""
    import numpy as np
    from lcoe.simulate import _enforce_re_monotonicity
    res = {"scenarios": {
        0.70: {"opt_delivered": np.array([50., 49., 48.]), "opt_csol": np.array([2., 2., 2.])},
        0.80: {"opt_delivered": np.array([51., 48., 49.]), "opt_csol": np.array([3., 3., 3.])}}}
    _enforce_re_monotonicity(res, [0.70, 0.80], years=2)
    a, b = res["scenarios"][0.70]["opt_delivered"], res["scenarios"][0.80]["opt_delivered"]
    assert all(a[i] <= b[i] + 1e-9 for i in range(3))    # non-decreasing in the target
    assert list(a) == [50., 48., 48.]                    # adopts the cheaper feasible slice
    assert res["scenarios"][0.70]["opt_csol"][1] == 3.0  # ...and its build, only where needed
    assert res["scenarios"][0.70]["opt_csol"][0] == 2.0  # untouched where already monotone


def test_firm_gas_sizing_p90_at_least_mean():
    """The P90 firm gas-sizing option sizes the backup to the 1-in-10 annual peak, which
    is pointwise ≥ the mean-annual-peak surface, so it never lowers the firm delivered cost."""
    import numpy as np
    sim = _tiny_sim(grid_steps=5, n_mc=6)
    assert hasattr(sim, "gas_peak_firm_p90")
    assert np.all(sim.gas_peak_firm_p90 >= sim.gas_peak_firm_mean - 1e-9)
    cum = m.cumulative_capacity(m.SOLAR, 0)
    ls = m.wright_law(m.SOLAR.lcoe_today, m.SOLAR.cumulative_gw_2025, cum, m.SOLAR.learning_rate)[0]
    lw = m.wright_law(m.WIND.lcoe_today, m.WIND.cumulative_gw_2025,
                      m.cumulative_capacity(m.WIND, 0), m.WIND.learning_rate)[0]
    sim_p90 = _tiny_sim(grid_steps=5, n_mc=6,
                        sysp=m._sys_with(m.SYSTEM, grid_steps=5, n_mc_weather=6,
                                         firm_gas_sizing="p90"))
    kw = dict(batt=m.BATTERY_US, capex_batt_kwh=180.0, capex_batt_kw=140.0,
              gas=m.GAS, year_index=0)
    mean = m.optimal_cost_3d(sim, 0.80, ls, lw, sys=sim.sys, **kw)[0]
    p90 = m.optimal_cost_3d(sim_p90, 0.80, ls, lw, sys=sim_p90.sys, **kw)[0]
    assert p90 >= mean - 0.5 and m.SYSTEM.firm_gas_sizing == "mean"   # default unchanged


def test_solar_performance_ratio():
    import numpy as np
    base = m.solar_clearsky(5.5, 1.0).mean()
    der = m.solar_clearsky(5.5, 0.8).mean()
    # PR scales the clear-sky mean (≈, modulo the summer-noon clip at CF=1.0)
    assert abs(der / base - 0.8) < 0.02
    assert m.SYSTEM.solar_performance_ratio == 1.0          # default = cost-basis-anchored
    # a PR<1 lowers the simulated effective solar CF in the dispatch
    s1 = _tiny_sim(grid_steps=4, n_mc=3)
    s08 = _tiny_sim(grid_steps=4, n_mc=3,
                    sysp=m._sys_with(m.SYSTEM, grid_steps=4, n_mc_weather=3,
                                     solar_performance_ratio=0.8))
    assert s08.sol_cf_mean < s1.sol_cf_mean


def test_gas_stress_reference():
    """The gas-stress reference baseline is the same plant at a higher fuel price; it sits
    above the headline gas baseline and is a reference only (not in the optimisation)."""
    import numpy as np
    res = m.run_simulation(
        solar=m.SOLAR, wind=m.WIND, battery=m.BATTERY_US, gas=m.GAS, smr=m.SMR,
        sys=m._sys_with(m.SYSTEM, grid_steps=5, n_mc_weather=3), workload=m.FIRM,
        mean_irr=5.5, mean_wind_ms=7.5, years=1, reliabilities=[0.80], seed=1,
        gas_stress_mult=1.6)
    assert "gas_stress" in res and np.all(res["gas_stress"] > res["gas_pure"])
    # default run carries no stress line (opt-in only)
    res0 = m.run_simulation(
        solar=m.SOLAR, wind=m.WIND, battery=m.BATTERY_US, gas=m.GAS, smr=m.SMR,
        sys=m._sys_with(m.SYSTEM, grid_steps=5, n_mc_weather=3), workload=m.FIRM,
        mean_irr=5.5, mean_wind_ms=7.5, years=1, reliabilities=[0.80], seed=1)
    assert "gas_stress" not in res0


# ── runner ────────────────────────────────────────────────────────────────────

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
