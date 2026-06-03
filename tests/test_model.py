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
    cum = m.cumulative_capacity(m.SOLAR, 15)
    lcoe = m.wright_law(m.SOLAR.lcoe_today, m.SOLAR.cumulative_gw_2025, cum,
                        m.SOLAR.learning_rate)
    for yr, exp in {0: 52.0, 3: 37.4, 5: 31.0, 10: 20.3, 15: 13.7}.items():
        assert abs(lcoe[yr] - exp) < 0.1, f"solar LCOE {2025+yr}: {lcoe[yr]} != {exp}"


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
    # clear-sky annual mean ≈ irradiance/24; effective ≈ that × Beta(3,1.5) mean.
    cs = m.solar_clearsky(5.5)
    assert abs(cs.mean() - 5.5 / 24.0) < 0.01


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
    cs = m.solar_clearsky(5.5)
    rng = np.random.default_rng(7)
    w = np.mean([m.generate_weather_year(cs, 7.5, rng, 0.0, 0.5, 0.82)[1].mean()
                 for _ in range(6)])
    assert 0.19 < w < 0.25, f"US wind CF out of band: {w:.3f}"


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
    return m.run_simulation(
        solar=m.SOLAR, wind=m.WIND, battery=m.BATTERY_US, gas=m.GAS, smr=m.SMR,
        sys=m._sys_with(m.SYSTEM, grid_steps=8, n_mc_weather=6), workload=m.FIRM,
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
