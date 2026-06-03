from __future__ import annotations

"""Learning-curve, WACC, carbon, battery and gas cost functions."""
import math

import numpy as np

from .params import TechParams, BatteryParams, GasParams, GridPPAParams


# ─────────────────────────────────────────────────────────────────────────────
# 2. COST TRAJECTORY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def cumulative_capacity(tech, years: int) -> np.ndarray:
    if isinstance(tech, BatteryParams):
        base, add, gr = tech.cumulative_gwh_2025, tech.annual_additions_gwh, tech.additions_growth_rate
    else:
        base, add, gr = tech.cumulative_gw_2025, tech.annual_additions_gw, tech.additions_growth_rate
    cum = np.empty(years + 1)
    cum[0] = base
    for i in range(1, years + 1):
        cum[i] = cum[i - 1] + add * (1.0 + gr) ** i
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
) -> float:
    """
    Annualised battery cost per MW of load including capacity augmentation.

    effective_fec_per_day: throughput equivalent-full-cycles/day from dispatch.
    Thin wrapper over `battery_cost_split` (single source of the cost formula).
    """
    capex, opex = battery_cost_split(batt, storage_hours, capex_kwh, capex_kw,
                                     r, n_yr, effective_fec_per_day)
    return capex + opex


def battery_cost_split(batt, storage_hours, capex_kwh, capex_kw, r, n_yr,
                       effective_fec_per_day=0.5):
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
    """
    if storage_hours <= 0:
        return 0.0, 0.0
    fec_per_yr     = effective_fec_per_day * 365
    total_deg_rate = batt.calendar_deg_per_yr + batt.cycle_deg_per_fec * fec_per_yr
    power_rating = 1.0 if storage_hours <= 4.0 else min(1.0, 4.0 / storage_hours)
    energy_capex = storage_hours * capex_kwh * 1e3     # cells — degrade, augmented
    power_capex  = power_rating * capex_kw * 1e3       # inverter/BOS — built once
    cost_one = energy_capex + power_capex
    # NPV = initial build + discounted stream of annual energy augmentation
    annuity = (1.0 - (1.0 + r) ** (-n_yr)) / r if r > 0 else float(n_yr)
    npv = cost_one + total_deg_rate * energy_capex * annuity
    return npv * crf(r, n_yr), cost_one * batt.om_frac_capex


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


