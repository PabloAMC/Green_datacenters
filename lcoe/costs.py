from __future__ import annotations

"""Learning-curve, WACC, carbon, battery and gas cost functions."""
import math

import numpy as np

from .params import TechParams, BatteryParams, GasParams, GridPPAParams


# ─────────────────────────────────────────────────────────────────────────────
# 2. COST TRAJECTORY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def cumulative_capacity(tech, years: int) -> np.ndarray:
    """
    Cumulative installed capacity trajectory feeding the Wright's-Law learning curve.

    Annual additions grow off the 2025 base, but the *growth rate of additions
    decays* over the horizon (v5.7): real technology adoption is an S-curve, not
    perpetual compounding — the largest markets saturate and grid-integration limits
    bite, so a constant growth rate runs cumulative capacity to physically implausible
    levels by 2040 (the old constant-15% solar path reached 38 TW, ~3-4× mainstream
    IEA/BNEF projections, which made the learning-curve cost decline too fast). The
    year-i additions growth is

        g_i = floor + (g0 - floor)·decay^(i-1)      (g_1 = g0; → floor as i grows)

    and additions_i = add·Π_{j≤i}(1+g_j). With `additions_growth_decay = 1.0`
    (the default) this reduces EXACTLY to the legacy constant-growth formula
    add·(1+g0)^i, so technologies that don't set a decay (e.g. the LDES presets) are
    unchanged.
    """
    if isinstance(tech, BatteryParams):
        base, add, gr = tech.cumulative_gwh_2025, tech.annual_additions_gwh, tech.additions_growth_rate
    else:
        base, add, gr = tech.cumulative_gw_2025, tech.annual_additions_gw, tech.additions_growth_rate
    decay = getattr(tech, "additions_growth_decay", 1.0)
    floor = getattr(tech, "additions_growth_floor", 0.0)
    cum = np.empty(years + 1)
    cum[0] = base
    prod = 1.0
    for i in range(1, years + 1):
        g = floor + (gr - floor) * decay ** (i - 1)   # year-1 growth = gr; decays toward floor
        prod *= (1.0 + g)
        cum[i] = cum[i - 1] + add * prod
    return cum


def wright_law(cost_today: float, cum_today: float, cum_future: np.ndarray,
               lr: float) -> np.ndarray:
    return cost_today * (cum_future / cum_today) ** math.log2(1.0 - lr)


def crf(r: float, n: int) -> float:
    if r == 0:
        return 1.0 / n
    return r * (1 + r) ** n / ((1 + r) ** n - 1)


# The flat WACC at which the exogenous generation LCOEs (Lazard v18 etc.) are taken
# to be quoted. `rewacc_lcoe` re-expresses a bundled LCOE at a technology's own WACC.
LEGACY_WACC = 0.07


def rewacc_lcoe(lcoe, tech: TechParams):
    """
    Re-express an exogenous (bundled) generation LCOE at the technology's own cost
    of capital and life (v5.3, per-tech WACC). A no-fuel LCOE splits into a
    capital-recovery part (fraction 1−`om_frac_lcoe`) and a fixed-O&M part
    (`om_frac_lcoe`); only the capital part rescales with the WACC, by the ratio
    CRF(wacc, life) / CRF(LEGACY_WACC, life). O&M is unchanged. At wacc=LEGACY_WACC
    this is the identity, so the legacy flat-WACC results are recovered exactly.

    If `tech.degradation_per_yr > 0` (off by default — Lazard LCOE already embeds
    degradation + inverter replacement), delivered LCOE is additionally inflated by
    1/(1 − ½·deg·life) for the lifetime-average energy loss.
    """
    k = ((1.0 - tech.om_frac_lcoe) * crf(tech.wacc, tech.life_yr)
         / crf(LEGACY_WACC, tech.life_yr) + tech.om_frac_lcoe)
    if tech.degradation_per_yr > 0.0:
        avg_avail = max(1.0 - 0.5 * tech.degradation_per_yr * tech.life_yr, 1e-6)
        k /= avg_avail
    return lcoe * k


def smr_trajectory(smr: SMRParams, years: int) -> np.ndarray:
    t = np.arange(years + 1)
    return np.where(t < smr.years_to_noak,
                    smr.lcoe_foak + (smr.lcoe_noak - smr.lcoe_foak) * t / smr.years_to_noak,
                    smr.lcoe_noak)


def grid_ppa_trajectory(ppa: GridPPAParams, lcoe_solar: np.ndarray) -> np.ndarray:
    """
    Delivered $/MWh for a grid-connected datacenter on a renewable PPA (reference
    only). The contracted-energy component scales with the regional *solar*
    learning curve relative to the base year, so it falls as RE costs fall; the
    grid network charge and the firming premium are flat in real terms (a floor).
    """
    ratio = lcoe_solar / lcoe_solar[0]
    return (ppa.ppa_energy_today * ratio
            + ppa.grid_delivery_mwh + ppa.firming_premium_mwh)


def grid_cfe_trajectory(ppa: GridPPAParams, lcoe_solar: np.ndarray) -> np.ndarray:
    """
    Delivered $/MWh for grid-connected **24/7 carbon-free energy** matching (every
    hour matched with clean supply), = the annual-matching PPA line plus a flat
    `cfe_premium_mwh` for hourly matching. Reference only; stylised & adjustable.
    """
    return grid_ppa_trajectory(ppa, lcoe_solar) + ppa.cfe_premium_mwh


def carbon_price(gas: GasParams, year_index: int) -> float:
    """
    Carbon price at year t under three trajectory modes.

    linear   : p(t) = p0 + escalation × t
    logistic : sigmoid from p0 to ceiling, inflecting at midpoint year.
               Matches EU ETS Fit-for-55 non-linear tightening.
    step     : jumps from p0 to ceiling at midpoint year.
    """
    p0 = gas.carbon_price_today
    t  = float(year_index)

    if gas.carbon_trajectory == "linear":
        return p0 + gas.carbon_price_escalation * t

    elif gas.carbon_trajectory == "logistic":
        k    = gas.carbon_trajectory_steepness
        tmid = gas.carbon_trajectory_midpoint
        cap  = gas.carbon_price_ceiling
        sig0 = 1.0 / (1.0 + math.exp(k * tmid))
        sigt = 1.0 / (1.0 + math.exp(-k * (t - tmid)))
        return p0 + (cap - p0) * (sigt - sig0) / (1.0 - sig0)

    elif gas.carbon_trajectory == "step":
        return gas.carbon_price_ceiling if t >= gas.carbon_trajectory_midpoint else p0

    else:
        raise ValueError(f"Unknown carbon_trajectory: {gas.carbon_trajectory!r}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. BATTERY COST  (augmentation model, throughput-cycled)
# ─────────────────────────────────────────────────────────────────────────────

def battery_annualised_cost(
    batt: BatteryParams,
    storage_hours: float,
    capex_kwh: float,
    capex_kw: float,
    r: float,
    n_yr: int,
    effective_fec_per_day: float = 0.5,
    power_rating: float = None,
) -> float:
    """
    Annualised battery cost per MW of load including capacity augmentation.

    effective_fec_per_day: throughput equivalent-full-cycles/day from dispatch.
    power_rating: MW per MW-load (override). None → LFP default min(1, 4/B);
    set explicitly for technologies (e.g. LDES) whose power is decoupled from energy.
    Thin wrapper over `battery_cost_split` (single source of the cost formula).
    """
    capex, opex = battery_cost_split(batt, storage_hours, capex_kwh, capex_kw,
                                     r, n_yr, effective_fec_per_day, power_rating)
    return capex + opex


def battery_cost_split(batt, storage_hours, capex_kwh, capex_kw, r, n_yr,
                       effective_fec_per_day=0.5, power_rating=None):
    """
    (capex, opex) split of the annualised battery cost. The single source of the
    battery cost formula — `battery_annualised_cost` returns the sum of the two.

    Augmentation model (v5.4): rather than replacing the whole system at end-of-life,
    the operator tops up the **energy (cell)** capacity each year to offset fade,
    holding usable capacity at nameplate — standard industry practice and cheaper
    than full replacement. Annual augmentation = fade rate × energy capex, where the
    fade rate is calendar + cycle (throughput EFCs from dispatch). The power/BOS
    component is built once (inverter mid-life replacement is folded into O&M).
    Augmentation is priced at today's capex (future cells are cheaper, so this is
    conservative).

    `power_rating` (MW per MW-load): None → LFP default min(1, 4/B); pass an explicit
    value for power/energy-decoupled technologies such as LDES.
    """
    if storage_hours <= 0:
        return 0.0, 0.0
    fec_per_yr     = effective_fec_per_day * 365
    total_deg_rate = batt.calendar_deg_per_yr + batt.cycle_deg_per_fec * fec_per_yr
    if power_rating is None:
        power_rating = 1.0 if storage_hours <= 4.0 else min(1.0, 4.0 / storage_hours)
    energy_capex = storage_hours * capex_kwh * 1e3     # cells — degrade, augmented
    power_capex  = power_rating * capex_kw * 1e3       # inverter/BOS — built once
    cost_one = energy_capex + power_capex
    # NPV = initial build + discounted stream of annual energy augmentation
    annuity = (1.0 - (1.0 + r) ** (-n_yr)) / r if r > 0 else float(n_yr)
    npv = cost_one + total_deg_rate * energy_capex * annuity
    return npv * crf(r, n_yr), cost_one * batt.om_frac_capex


def ldes_annual_cost(ldes, storage_hours, capex_kwh, charge_capex_kw,
                     discharge_capex_kw, charge_pow, discharge_pow, r, n_yr,
                     effective_fec_per_day=0.0):
    """
    Annualised LDES cost ($/MW-load/yr) with power/energy fully decoupled and the
    charge kit (e.g. electrolyser) priced separately from the discharge kit (e.g. H2
    turbine). Energy capex is per kWh of stored (mid-cycle) capacity; the energy
    component is augmented yearly to offset fade (as for LFP, §6). Used by the
    `--ldes` overlay; not part of the core 3D optimisation.
    """
    if storage_hours <= 0:
        return 0.0
    energy_capex = storage_hours * capex_kwh * 1e3
    power_capex = charge_pow * charge_capex_kw * 1e3 + discharge_pow * discharge_capex_kw * 1e3
    cost_one = energy_capex + power_capex
    deg = ldes.calendar_deg_per_yr + ldes.cycle_deg_per_fec * effective_fec_per_day * 365
    annuity = (1.0 - (1.0 + r) ** (-n_yr)) / r if r > 0 else float(n_yr)
    npv = cost_one + deg * energy_capex * annuity
    return npv * crf(r, n_yr) + cost_one * ldes.om_frac_capex


def h2_system_cost_split(C_sol, C_win, B_lfp, elec, H2, resid, efc, *,
                         batt, ldes, n, CF_sol, CF_win, lcoe_sol, lcoe_win,
                         om_sol, om_win, lfp_kwh, lfp_kw, ch_capex, e_capex,
                         dis_capex, h2_buy, crf_l, annuity):
    """
    Per-MWh-load delivered-cost components of the fully gas-free, zero-carbon system
    (solar + wind + LFP + self-produced green H₂; residual bought as green H₂).

    SINGLE SOURCE shared by `h2system.h2_system_trajectory` (the fig1 line / fig6
    breakdown) and `analysis.run_ldes_joint` (the co-optimisation) so the two can
    never drift apart — they previously duplicated this formula behind a "keep in
    sync" comment. `resid` (purchased-H₂ share of load) and `efc` (H₂-store
    throughput cycles/day) come from `dispatch.dispatch_h2_vec`. The full-system
    turbine is always installed (firms the load); only the electrolyser and store
    scale with the self-production design. Augmentation applies to the H₂-store energy.
    """
    gen_s = C_sol * CF_sol * lcoe_sol
    gen_w = C_win * CF_win * lcoe_win
    gen_capex = gen_s * (1.0 - om_sol) + gen_w * (1.0 - om_win)
    gen_om    = gen_s * om_sol + gen_w * om_win
    lfp_cap, lfp_om = battery_cost_split(batt, B_lfp, lfp_kwh, lfp_kw, batt.wacc, n)
    lfp_cap /= 8760.0; lfp_om /= 8760.0
    om = ldes.om_frac_capex
    elec_one  = elec * ch_capex * 1e3
    turb_one  = 1.0 * dis_capex * 1e3
    store_one = H2 * e_capex * 1e3
    deg = ldes.calendar_deg_per_yr + ldes.cycle_deg_per_fec * efc * 365
    elec_cap  = (elec_one * crf_l + elec_one * om) / 8760.0
    turb_cap  = (turb_one * crf_l + turb_one * om) / 8760.0
    store_cap = ((store_one + deg * store_one * annuity) * crf_l + store_one * om) / 8760.0
    return {"gen_capex": gen_capex, "gen_om": gen_om, "lfp_capex": lfp_cap,
            "lfp_om": lfp_om, "elec_capex": elec_cap, "store_capex": store_cap,
            "turbine_capex": turb_cap, "buy_h2": resid * h2_buy}


# ─────────────────────────────────────────────────────────────────────────────
# 6. GAS BACKUP COST
# ─────────────────────────────────────────────────────────────────────────────

def _gas_plant_params(gas_frac: float, gas: GasParams, year_index: int, r: float,
                      gas_peak: float):
    """
    Shared CCGT/OCGT selection and common cost factors for the gas backup, so the
    scalar LCOE and the {capex, fom, fuel, carbon} split use one source for the
    technology choice (CCGT at ≥20% gas fraction, else OCGT peaker).

    Returns (cf, crf_g, p_c, cap_backup, capex_kw, fom_kw, heat_rate, carbon_int).
    """
    cf    = max(gas_frac, 1e-9)
    crf_g = crf(r, gas.lifetime_years)
    p_c   = carbon_price(gas, year_index)
    cap_backup = float(np.clip(gas_peak, 0.05, 1.0))
    if gas_frac >= 0.20:   # CCGT
        return (cf, crf_g, p_c, cap_backup, gas.ccgt_capex_kw, gas.ccgt_fom_kw_yr,
                gas.ccgt_heat_rate, gas.carbon_intensity_ccgt)
    # OCGT peaker
    return (cf, crf_g, p_c, cap_backup, gas.ocgt_capex_kw, gas.ocgt_fom_kw_yr,
            gas.ocgt_heat_rate, gas.carbon_intensity_ocgt)


def gas_backup_cost_scalar(gas_frac: float, gas: GasParams,
                           year_index: int, r: float, gas_peak: float = 1.0) -> float:
    if gas_frac < 1e-9:
        return 0.0
    cf, crf_g, p_c, cap_backup, capex_kw, fom_kw, hr, ci = _gas_plant_params(
        gas_frac, gas, year_index, r, gas_peak)
    lcoe = (((capex_kw * 1e3 * crf_g) * cap_backup) / (8760 * cf)
            + ((fom_kw * 1e3) * cap_backup) / (8760 * cf)
            + gas.gas_price_mmbtu * hr + gas.vom_mwh
            + p_c * ci)
    return lcoe * gas_frac


def gas_cost_split(gas_frac: float, gas: GasParams, year_index: int, r: float,
                   gas_peak: float = 1.0):
    """Split gas backup cost ($/MWh-load) into {capex, fom, fuel, carbon} — same
    formula and CCGT/OCGT selection as gas_backup_cost_scalar."""
    if gas_frac < 1e-9:
        return {"capex": 0.0, "fom": 0.0, "fuel": 0.0, "carbon": 0.0}
    cf, crf_g, p_c, cap_backup, capex_kw, fom_kw, hr, ci = _gas_plant_params(
        gas_frac, gas, year_index, r, gas_peak)
    return {
        "capex":  (capex_kw * 1e3 * crf_g * cap_backup) / (8760 * cf) * gas_frac,
        "fom":    (fom_kw   * 1e3 * cap_backup)         / (8760 * cf) * gas_frac,
        "fuel":   (gas.gas_price_mmbtu * hr + gas.vom_mwh)            * gas_frac,
        "carbon": (p_c * ci)                                         * gas_frac,
    }


def gas_pure_lcoe(gas: GasParams, year_index: int, r: float) -> float:
    crf_g = crf(r, gas.lifetime_years)
    p_c   = carbon_price(gas, year_index)
    return ((gas.ccgt_capex_kw * 1e3 * crf_g) / (8760 * 0.85)
            + (gas.ccgt_fom_kw_yr * 1e3) / (8760 * 0.85)
            + gas.gas_price_mmbtu * gas.ccgt_heat_rate + gas.vom_mwh
            + p_c * gas.carbon_intensity_ccgt)


