"""
Off-grid Datacenter LCOE Model  v5.3
=====================================
Costs of powering an off-grid datacenter with solar PV, onshore wind,
battery storage, and gas backup.

v5.3 — per-technology cost of capital. The single flat WACC is replaced by
differentiated real WACC + asset life per technology (solar/wind 5.5% over 30/25 yr,
LFP battery 7%, gas 9% over 25 yr; see `rewacc_lcoe` and §8.3 of the docs). Each
component is levelised over its own life, resolving the earlier mixed-horizon
treatment. Net: cheaper RE, dearer gas → EU parity ~1 yr earlier (90% RE ≈ 2034),
US cheap-gas moat still holds. Legacy flat-7% behaviour is recovered by setting each
`wacc` to 0.07.

v5.2 — demand flexibility as SHEDDING WITH A PENALTY (supersedes the v5/v5.1
deferral-with-recovery model):
  - The v5 model let load be paused and *recovered later* by running ABOVE
    nominal during surplus — which secretly assumes over-provisioned compute
    (idle GPUs), economically irrational since GPU capex ≫ energy cost.
  - v5.2 drops the free catch-up: up to `interruptible_fraction` of a deficit
    hour's load may be SHED (compute lost, never recovered), and each shed MWh is
    charged `shed_penalty_mwh` — the value of that lost compute (deep, adjustable,
    grounded in idled IT capex). The RE target is now defined on *served* energy.
    Premium compute (high penalty) sheds only in the most extreme hours; cheap
    interruptible compute (low penalty) sheds freely. The shed fraction is reported.

v5 upgrades over v4 (rigour fixes), retained:
  (a) Honest demand-flexibility accounting — no free load-shedding into the RE
      fraction (v4 dropped ~8% of load yet counted it as renewable). v5.2's shed
      model prices every dropped MWh and reports it.
  (b) Consistent battery cost basis — LFP cells are a globally traded commodity,
      so the energy component ($/kWh) is identical across regions; only the
      power/BOS/EPC component ($/kW) carries a regional (EU) soft-cost premium.
      This removes the v4 artefact of EU storage being 2.75x cheaper than the US.
  (c) Multi-day "Dunkelflaute" structure — a persistent synoptic latent factor
      (daily AR(1)) jointly modulates wind and solar, producing correlated
      multi-day low-resource episodes. The hourly wind process now mean-reverts
      to the persistent daily level instead of to the climatological mean, so
      multi-day lulls actually persist in the trace. Marginals (and therefore
      annual-mean capacity factors) are preserved exactly; only the temporal
      clustering — which is what drives storage/backup sizing — changes.

Inherited from v4:
  - 3D Nelder-Mead optimisation over (C_solar, C_wind, B).
  - Solar-wind Gaussian copula contemporaneous correlation.
  - EU ETS logistic / linear / step carbon price trajectories.
  - DoD-weighted battery degradation and mid-life replacement NPV.

Sources: Lazard LCOE+ v18 (2025), Ember 2025, OWID learning rates,
         Way et al. Joule (2022), NREL ATB 2024, BNEF 2024/25, EU ETS, IPCC AR6.
"""
from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import minimize
from scipy.special import ndtr, betaincinv  # Gaussian copula transforms

try:
    import scienceplots  # noqa: F401
    plt.style.use(["science", "no-latex"])
except ImportError:
    plt.rcParams.update({"font.family": "serif", "axes.grid": True,
                         "grid.alpha": 0.3, "figure.dpi": 150})


# ─────────────────────────────────────────────────────────────────────────────
# 1. PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TechParams:
    """Generation technology: cost via Wright's Law learning curve."""
    name: str
    lcoe_today: float           # $/MWh generation-only LCOE (2025, no storage)
    learning_rate: float        # fractional cost reduction per doubling of capacity
    cumulative_gw_2025: float   # GW cumulative installed globally, base year
    annual_additions_gw: float  # GW added in 2025
    additions_growth_rate: float
    uncertainty_sigma: float = 0.15
    # Fraction of the (bundled) LCOE that is ongoing O&M rather than financed
    # capital — used only to split delivered cost into capex vs opex. Solar/wind
    # have no fuel, so LCOE ≈ capital recovery + fixed O&M; O&M is ~15–25% of LCOE.
    om_frac_lcoe: float = 0.18
    # Per-technology cost of capital and asset life (v5.3). The exogenous
    # `lcoe_today` is quoted at the legacy flat WACC (LEGACY_WACC); `rewacc_lcoe`
    # re-expresses it at this technology's WACC over its own life. Solar/wind are
    # low-risk, long-life infrastructure → cheaper capital than gas.
    wacc: float = 0.055
    life_yr: int = 30
    # Annual generation derating from panel/turbine degradation. DEFAULT 0.0 on
    # purpose: the exogenous Lazard `lcoe_today` already embeds module degradation
    # and inverter replacement, so adding it here would DOUBLE-COUNT. Set >0 only if
    # you supply a degradation-free input LCOE; `rewacc_lcoe` then inflates delivered
    # LCOE by 1/(1 − ½·deg·life) to reflect the lifetime-average energy loss.
    degradation_per_yr: float = 0.0


@dataclass
class BatteryParams:
    """
    LFP battery storage: costs, efficiency, and degradation.

    Cost basis (v5): the model splits installed cost into an ENERGY component
    (`capex_kwh_today`, scales with MWh) and a POWER/BOS component
    (`capex_kw_today`, scales with MW). LFP cells are a globally traded
    commodity, so the energy component is region-invariant; only the
    power/BOS/EPC component carries a regional soft-cost premium. A 4h system
    costs `4·capex_kwh + 1·capex_kw` per kW-load (e.g. US ≈ $860/kW-load →
    ~$215/kWh installed), consistent with NREL ATB 2024 / BNEF 2024-25.
    """
    name: str = "LFP Battery"
    capex_kwh_today: float = 180.0   # energy component, $/kWh (global commodity)
    capex_kw_today: float  = 140.0   # power/BOS/EPC component, $/kW (regional)
    learning_rate: float = 0.19
    cumulative_gwh_2025: float = 1800.0
    annual_additions_gwh: float = 600.0
    additions_growth_rate: float = 0.18
    roundtrip_efficiency: float = 0.924   # DC-DC LFP; sqrt each way ≈ 96.1%
    om_frac_capex: float = 0.015
    # Degradation (LFP Wöhler curve)
    calendar_deg_per_yr: float = 0.020   # 2.0%/yr calendar fade
    cycle_deg_per_fec: float   = 5e-5    # capacity loss per FEC at 100% DoD (~4000 cycles → 80%, LFP)
    dod_exponent: float        = 0.60    # FEC_eff = DoD^β per actual cycle
    replace_threshold: float   = 0.80
    uncertainty_sigma: float = 0.12
    wacc: float = 0.07   # cost of capital (mid: more risk than RE, less than gas)


@dataclass
class GasParams:
    """Gas backup: CCGT/OCGT, fuel, carbon trajectory."""
    name: str = "Gas Backup"
    ccgt_capex_kw: float   = 1100.0
    ocgt_capex_kw: float   = 500.0
    ccgt_fom_kw_yr: float  = 15.0
    ocgt_fom_kw_yr: float  = 10.0
    gas_price_mmbtu: float = 4.0
    ccgt_heat_rate: float  = 6.5    # MMBtu/MWh
    ocgt_heat_rate: float  = 9.5
    vom_mwh: float = 3.0
    carbon_intensity_ccgt: float = 0.41  # tCO2/MWh (IPCC AR6)
    carbon_intensity_ocgt: float = 0.60
    carbon_price_today: float = 0.0
    # Trajectory mode: "linear" | "logistic" | "step"
    carbon_trajectory: str = "linear"
    carbon_price_escalation: float = 0.0    # $/tCO2/yr  (linear mode)
    carbon_price_ceiling: float = 150.0     # $/tCO2 long-run cap
    carbon_trajectory_midpoint: float = 10.0
    carbon_trajectory_steepness: float = 0.4
    lifetime_years: int = 25
    uncertainty_sigma: float = 0.12
    wacc: float = 0.09   # higher cost of capital: merchant + policy/stranding risk


@dataclass
class SMRParams:
    name: str = "SMR (Nuclear)"
    lcoe_foak: float = 120.0
    lcoe_noak: float = 85.0
    years_to_noak: int = 10
    uncertainty_sigma: float = 0.25


@dataclass
class GridPPAParams:
    """
    Grid-connected renewable-PPA reference (NOT off-grid) — a reference line only,
    like SMR; it is never part of the optimisation.

    The real-world alternative to building an off-grid plant is usually to sit on
    the grid and sign a renewable Power Purchase Agreement. Delivered cost ($/MWh)
    is modelled as three transparent, adjustable components:

        delivered(t) = ppa_energy(t) + grid_delivery + firming_premium

    where `ppa_energy(t)` tracks the region's *solar* learning curve (so the
    contracted-energy component declines with RE costs over time), while the grid
    network charge and the firming/balancing premium (the cost of leaning on the
    grid to make an intermittent PPA reliable for a 24/7 load) are flat in real
    terms — a hard floor the all-in price cannot fall below. Stylised; every figure
    is an adjustable assumption, grounded in the LevelTen PPA Price Index, Lazard
    v18, and typical large-C&I network tariffs.

    Note: this represents *annual-volumetric* RE matching, not hour-by-hour 24/7
    carbon-free energy; true 100% 24/7 CFE on the grid would cost more (see docs).
    """
    name: str = "Grid + RE PPA"
    ppa_energy_today: float = 45.0     # $/MWh contracted RE energy (LevelTen index, US)
    grid_delivery_mwh: float = 22.0    # $/MWh T&D / network charge, large C&I
    firming_premium_mwh: float = 8.0   # $/MWh balancing/standby to firm PPA to 24/7


@dataclass
class WorkloadProfile:
    """
    Demand flexibility via SHEDDING WITH A PENALTY (v5.2).

    v5/v5.1 modelled flexibility as deferral *with recovery* (pause now, catch up
    later from surplus). That implicitly required compute hardware sized ABOVE the
    average load — idle GPUs waiting to over-run during cheap hours — which is
    economically irrational, since GPU capex dwarfs energy cost. v5.2 drops the
    free catch-up: paused work is simply NOT done (compute is lost), and each shed
    MWh is charged a penalty equal to the value of that lost compute.

    Two intuitive knobs:

    `interruptible_fraction` — **how big a slice of the datacenter you may switch
        off** during a deficit hour (the rest is must-run). There is NO recovery;
        the shed compute is gone.

    `shed_penalty_mwh` — **how valuable that compute is** ($/MWh of shed load). This
        is the deep, adjustable penalty for not serving demand. Grounded in idled
        IT capex: an H100-class server (~$25/W installed, ~4yr life) implies
        ≈ $600–900/MWh of compute-energy just in amortised hardware sitting idle —
        so premium training should set this high (it will then shed only in the
        most extreme hours), while interruptible/spot/research workloads set it low.

    interruptible_fraction = 0 → a fully firm (always-on) workload; the penalty is
    then irrelevant. The shed fraction is reported so reliability stays transparent.
    """
    name: str = "Enterprise IT"
    interruptible_fraction: float = 0.05   # max share of load sheddable per hour (no recovery)
    shed_penalty_mwh: float = 2000.0       # $/MWh value of lost compute (deep, adjustable)


@dataclass
class SystemParams:
    load_mw: float = 100.0
    # Legacy flat WACC. Superseded by per-technology WACC (v5.3): generation,
    # battery, and gas each finance at their own `wacc` field; this value is no
    # longer used for costing and is kept only for reference / backward-compat.
    discount_rate: float = 0.07
    project_lifetime_yr: int = 20
    n_mc_weather: int = 50      # synthetic weather years (Dunkelflaute → wider tails)
    # Optimiser bounds (3D: C_sol, C_win, B). v5.1: right-sized so the lattice
    # resolves the region where optima actually live (~5–9h storage, ~3–7× wind).
    # Oversized bounds waste resolution and pin the optimum onto coarse grid nodes
    # (the v5 "flat line" artefact). A boundary-binding guard (optimal_cost_3d)
    # warns if any optimum reaches a max, so caps can never silently bind again.
    c_sol_max: float = 18.0
    c_win_max: float = 18.0     # firm (no-shed) high-RE is wind-heavy
    storage_hours_max: float = 60.0
    grid_steps: int = 21        # per-axis steps; total scenarios = grid_steps^3
    wind_solar_corr: float = 0.0  # contemporaneous ρ ∈ [-1,1]; negative = windy when overcast
    # ── Synoptic "Dunkelflaute" latent factor (v5) ─────────────────────────────
    # A persistent daily AR(1) common factor loads (positively) on BOTH the cloud
    # and wind daily draws, so its low excursions = multi-day joint low-resource
    # episodes. syn_loading λ sets how much daily variance is synoptic/common;
    # syn_persistence φ sets episode length (e-folding ≈ 1/(1-φ) days).
    # λ must satisfy λ² ≤ (ρ+1)/2 so the residual contemporaneous corr stays valid.
    syn_loading: float = 0.50      # λ: common-factor loading on cloud & wind
    syn_persistence: float = 0.82  # φ: daily AR(1) persistence (≈5–6 day episodes)
    # Weather micro-structure (promoted from in-function constants; defaults unchanged).
    cloud_ar1: float = 0.35        # ρ_c: short-scale day-to-day cloud persistence
    wind_ar1: float = 0.75         # ρ_w: hourly wind AR(1) within-day persistence (~4h memory)
    wind_daily_share: float = 0.50 # a_w²: share of wind variance at the daily/synoptic scale
    wind_seasonal_amp: float = 0.12  # winter wind amplification amplitude


# ── Workload presets ──────────────────────────────────────────────────────────

# A spectrum of flexibility regimes, ordered by how *willing/able* the workload is
# to drop compute when power is scarce — set by (interruptible_fraction, shed_penalty).
# A high penalty means premium compute that sheds only in the most extreme hours;
# a low penalty means cheap, interruptible compute that sheds whenever power is dear.
#   FIRM         always-on: nothing is sheddable; every MWh must be met.
#   ENTERPRISE   user-facing, tight SLA: tiny sheddable slice, very high value.
#   AI_TRAINING  premium training cluster: large sheddable slice but high-value compute.
#   INTERRUPTIBLE batch/research: much of the load is interruptible, lower-value.
#   BEST_EFFORT  spot/preemptible: nearly all interruptible, low-value — sheds eagerly.
# All values are adjustable assumptions, not measured constants.
# shed_penalty grounding: an H100-class server (~$25/W installed, ~4yr life,
# ~10% WACC → CRF≈0.315) implies ≈ $900/MWh of compute-energy in idled hardware
# alone. So premium training ≈ $900 (capex floor; frontier/strategic value is
# higher — raise it). User-facing enterprise loses revenue too → much higher.
# Interruptible/spot compute is valued far below its hardware cost.
FIRM          = WorkloadProfile("Firm",             interruptible_fraction=0.00, shed_penalty_mwh=0.0)
ENTERPRISE    = WorkloadProfile("Enterprise IT",    interruptible_fraction=0.05, shed_penalty_mwh=2500.0)
AI_TRAINING   = WorkloadProfile("AI Training",      interruptible_fraction=0.40, shed_penalty_mwh=900.0)
INTERRUPTIBLE = WorkloadProfile("Interruptible",    interruptible_fraction=0.60, shed_penalty_mwh=150.0)
BEST_EFFORT   = WorkloadProfile("Best-effort/spot", interruptible_fraction=0.90, shed_penalty_mwh=40.0)

WORKLOAD_PRESETS = {
    "firm": FIRM, "enterprise": ENTERPRISE, "training": AI_TRAINING,
    "interruptible": INTERRUPTIBLE, "best-effort": BEST_EFFORT,
}

# ── Technology defaults ───────────────────────────────────────────────────────

SOLAR = TechParams("Solar PV", lcoe_today=52.0, learning_rate=0.30,
                   cumulative_gw_2025=2900.0, annual_additions_gw=650.0,
                   additions_growth_rate=0.15, om_frac_lcoe=0.15)

WIND = TechParams("Onshore Wind", lcoe_today=50.0, learning_rate=0.17,
                  cumulative_gw_2025=1300.0, annual_additions_gw=167.0,
                  additions_growth_rate=0.10, om_frac_lcoe=0.25, life_yr=25)

# Energy component identical across regions (globally traded LFP cells); EU
# carries a ~25% power/BOS/EPC soft-cost premium (higher labour/permitting, no
# IRA-equivalent manufacturing credit). EU is therefore modestly MORE expensive
# than the US — the opposite of the v4 assumption.
BATTERY_US = BatteryParams("LFP Battery (US)", capex_kwh_today=180.0, capex_kw_today=140.0)
BATTERY_EU = BatteryParams("LFP Battery (EU)", capex_kwh_today=180.0, capex_kw_today=175.0)

GAS = GasParams(name="Gas Backup (US)", gas_price_mmbtu=4.0,
                carbon_price_today=0.0, carbon_trajectory="linear")

GAS_EU = GasParams(
    name="Gas Backup (EU)",
    gas_price_mmbtu=10.0,
    carbon_price_today=70.0,
    carbon_trajectory="logistic",
    carbon_price_ceiling=200.0,
    carbon_trajectory_midpoint=8.0,
    carbon_trajectory_steepness=0.35,
)

SOLAR_EU = TechParams("Solar PV (EU)", lcoe_today=60.0, learning_rate=0.30,
                      cumulative_gw_2025=2900.0, annual_additions_gw=650.0,
                      additions_growth_rate=0.15, om_frac_lcoe=0.15)
WIND_EU  = TechParams("Onshore Wind (EU)", lcoe_today=48.0, learning_rate=0.17,
                      cumulative_gw_2025=1300.0, annual_additions_gw=167.0,
                      additions_growth_rate=0.10, om_frac_lcoe=0.25, life_yr=25)

SMR    = SMRParams()
SMR_EU = SMRParams(name="SMR (EU)", lcoe_foak=140.0, lcoe_noak=85.0, years_to_noak=12)

# Grid + renewable-PPA reference. EU energy & network charges run higher than the
# US (pricier PPAs, higher network tariffs), mirroring the off-grid cost gap.
GRID_PPA    = GridPPAParams()
GRID_PPA_EU = GridPPAParams(name="Grid + RE PPA (EU)", ppa_energy_today=72.0,
                            grid_delivery_mwh=33.0, firming_premium_mwh=12.0)

SYSTEM    = SystemParams()
SYSTEM_EU = SystemParams(
    c_sol_max=22.0, c_win_max=20.0, storage_hours_max=60.0,
    wind_solar_corr=-0.35,   # N. Europe: cyclonic days are windy-and-overcast
    syn_loading=0.50,        # λ²=0.25 ≤ (−0.35+1)/2=0.325 → valid residual corr
    syn_persistence=0.85,    # longer winter blocking episodes than the US
)

# ── Region bundles (tech + gas + system + resource) ─────────────────────────────
# One entry per geography; the CLI and the flexibility sweep both resolve regions
# through this dict so a region is described in exactly one place.
REGIONS = {
    "us": dict(label="US", solar=SOLAR, wind=WIND, battery=BATTERY_US, gas=GAS,
               smr=SMR, grid_ppa=GRID_PPA, sys=SYSTEM, mean_irr=5.5, mean_wind_ms=7.5),
    "eu": dict(label="Europe", solar=SOLAR_EU, wind=WIND_EU, battery=BATTERY_EU,
               gas=GAS_EU, smr=SMR_EU, grid_ppa=GRID_PPA_EU, sys=SYSTEM_EU,
               mean_irr=3.8, mean_wind_ms=7.0),
}


# Resource-quality presets: (mean_irr [kWh/m²/day], mean_wind_ms). "default" is the
# conservative average-site resource used for the headline suite; "good" represents a
# modern, well-sited plant (higher irradiance, high-hub-height / low-specific-power
# turbines on a strong wind resource). Raising these lifts the simulated capacity
# factors toward real-world fleets and shows how much sooner RE reaches parity. Note
# the solar CF stays moderate even at "good" because the Beta(3,1.5) cloud model is
# conservative; the larger mover is wind. Used by `run_resource_sensitivity` / --resource.
RESOURCE_PRESETS = {
    "us": {"default": (5.5, 7.5), "good": (6.8, 9.0)},
    "eu": {"default": (3.8, 7.0), "good": (4.6, 8.5)},
}


def _sys_with(sys: SystemParams, **overrides) -> SystemParams:
    """Copy a SystemParams with selected fields overridden (e.g. coarser grid for sweeps)."""
    return SystemParams(**{**sys.__dict__, **overrides})


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
# 3. WEATHER GENERATION  (Gaussian copula solar-wind correlation)
# ─────────────────────────────────────────────────────────────────────────────

def solar_clearsky(mean_irr_kwh_m2_day: float) -> np.ndarray:
    hours = np.arange(8760)
    doy   = hours // 24
    hod   = hours % 24
    seasonal = 1.0 + 0.35 * np.cos(2 * np.pi * (doy - 172) / 365)
    diurnal  = np.clip(np.sin((hod - 6) * np.pi / 12), 0, 1) ** 1.1
    raw = diurnal * seasonal
    target_cf = mean_irr_kwh_m2_day / 24.0
    mean_raw  = raw.mean()
    return np.clip(raw * (target_cf / mean_raw if mean_raw > 0 else 1), 0, 1)


def generate_weather_year(
    clearsky: np.ndarray,
    mean_wind_ms: float,
    rng: np.random.Generator,
    wind_solar_corr: float = 0.0,
    syn_loading: float = 0.50,
    syn_persistence: float = 0.82,
    cloud_ar1: float = 0.35,
    wind_ar1: float = 0.75,
    wind_daily_share: float = 0.50,
    wind_seasonal_amp: float = 0.12,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Synthetic 8760h solar + wind capacity factors with (v5) a persistent
    synoptic factor that produces correlated multi-day "Dunkelflaute" episodes.

    Daily latent structure — a two-factor Gaussian model at the daily scale:

        f_d         : common synoptic factor, AR(1) with persistence φ
        z1_d = λ·f_d + √(1-λ²)·g1_d   → drives cloud (Beta(3,1.5) marginal)
        z2_d = λ·f_d + √(1-λ²)·g2_d   → drives wind  (Weibull(k=2.1) marginal)

    where (g1_d, g2_d) are contemporaneous bivariate-normal with correlation
        ρ_g = (ρ - λ²) / (1 - λ²)
    chosen so corr(z1_d, z2_d) = ρ (the requested contemporaneous coupling)
    while BOTH variables load positively on the persistent f_d. Hence:

      • f_d ≪ 0 for several days  →  low z1 AND low z2  →  joint multi-day
        low-sun-and-low-wind episode (Dunkelflaute);
      • the residual (g1,g2) carries the cyclonic ρ<0 ("windy when overcast")
        on top of the synoptic mode.

    Because z1_d, z2_d remain *standard normal*, the Beta and Weibull marginals
    — and therefore the annual-mean capacity factors — are preserved exactly;
    only the temporal clustering of lows changes (what storage/backup must cover).
    """
    rho = float(np.clip(wind_solar_corr, -0.999, 0.999))
    lam = float(np.clip(syn_loading, 0.0, math.sqrt(max((rho + 1.0) / 2.0, 0.0)) - 1e-6))
    phi = float(np.clip(syn_persistence, 0.0, 0.999))

    # Persistent synoptic common factor f_d ~ N(0,1), AR(1) (φ) — daily scale.
    f = np.empty(365)
    f[0] = rng.standard_normal()
    sig_f = math.sqrt(1 - phi ** 2)
    for d in range(1, 365):
        f[d] = phi * f[d - 1] + sig_f * rng.standard_normal()

    # Residual contemporaneous pair (g1,g2): corr ρ_g so net corr(z1,z2)=ρ.
    one_m = 1.0 - lam ** 2
    rho_g = float(np.clip((rho - lam ** 2) / one_m if one_m > 1e-9 else 0.0,
                          -0.999, 0.999))
    g1 = rng.standard_normal(365)
    g2 = rho_g * g1 + math.sqrt(max(1 - rho_g ** 2, 0)) * rng.standard_normal(365)

    z1 = lam * f + math.sqrt(one_m) * g1   # → cloud,  Var=1
    z2 = lam * f + math.sqrt(one_m) * g2   # → wind,   Var=1

    # Cloud factor: Beta(3, 1.5) marginal via probability integral transform
    u_cloud = np.clip(ndtr(z1), 1e-6, 1 - 1e-6)
    daily_raw = betaincinv(3.0, 1.5, u_cloud)

    # Wind: Weibull(k=2.1) marginal
    k = 2.1
    c = mean_wind_ms / math.gamma(1 + 1 / k)

    # ── Solar ─────────────────────────────────────────────────────────────────
    # Short-scale cloud autocorrelation on top of the synoptic persistence in z1.
    rho_c = cloud_ar1
    daily_cloud = np.empty(365)
    daily_cloud[0] = daily_raw[0]
    for d in range(1, 365):
        daily_cloud[d] = rho_c * daily_cloud[d - 1] + (1 - rho_c) * daily_raw[d]
    solar = np.clip(clearsky * np.repeat(np.clip(daily_cloud, 0, 1), 24), 0, 1)

    # ── Wind: hourly AR(1) that MEAN-REVERTS to the persistent daily level ──────
    # v4 reverted hourly wind to the *climatological* mean (0 in normal space), so
    # a daily lull decayed within hours (0.75^24 ≈ 0) and multi-day lulls never
    # persisted. v5 reverts to a daily mean m_d carried by the synoptic z2:
    #
    #     z_h = m_day + dev_h ,  dev_h = ρ_w·dev_{h-1} + c_dev·ε_h
    #
    # with Var(m_d)=a_w² and stationary Var(dev)=1-a_w², so z_h ~ N(0,1) exactly
    # (Weibull marginal preserved). a_w² = share of wind variance at the daily/
    # synoptic scale; the rest is intra-day texture (ρ_w=0.75 → ~4h memory).
    rho_w = wind_ar1
    a_w2  = wind_daily_share
    a_w   = math.sqrt(a_w2)
    c_dev = math.sqrt((1 - a_w2) * (1 - rho_w ** 2))   # so stationary Var(dev)=1-a_w²
    m_day = a_w * z2                                    # persistent daily mean (365,)

    z_wind = np.empty(8760)
    eps = rng.standard_normal(8760)
    dev = math.sqrt(1 - a_w2) * rng.standard_normal()  # stationary-variance seed
    z_wind[0] = m_day[0] + dev
    for h in range(1, 8760):
        dev = rho_w * dev + c_dev * eps[h]
        z_wind[h] = m_day[h // 24] + dev

    # Transform normal → uniform → Weibull (preserves exact Weibull marginal)
    u_hourly = np.clip(ndtr(z_wind), 1e-6, 1 - 1e-6)
    speeds = c * (-np.log(1 - u_hourly)) ** (1 / k)

    # Seasonal modulation (NH land: ~12% stronger in winter)
    doy = np.arange(8760) // 24
    speeds *= 1.0 + wind_seasonal_amp * np.cos(2 * np.pi * (doy - 15) / 365)

    v_ci, v_r, v_co = 3.5, 13.0, 25.0
    wind = np.where(speeds < v_ci, 0.0,
           np.where(speeds >= v_co, 0.0,
           np.where(speeds >= v_r, 1.0,
                    ((speeds - v_ci) / (v_r - v_ci)) ** 3)))

    return solar, wind


# ─────────────────────────────────────────────────────────────────────────────
# 4. BATTERY DEGRADATION  (DoD-weighted)
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
    Annualised battery cost per MW of load including mid-life replacements.

    effective_fec_per_day: DoD-weighted FEC/day from dispatch (replaces fixed 1.5).
    Thin wrapper over `battery_cost_split` (single source of the cost formula).
    """
    capex, opex = battery_cost_split(batt, storage_hours, capex_kwh, capex_kw,
                                     r, n_yr, effective_fec_per_day)
    return capex + opex


def battery_cost_split(batt, storage_hours, capex_kwh, capex_kw, r, n_yr,
                       effective_fec_per_day=0.5):
    """(capex, opex) split of the annualised battery cost: capital recovery
    (incl. mid-life replacements) vs the fixed O&M term. The single source of the
    battery cost formula — `battery_annualised_cost` returns the sum of the two."""
    if storage_hours <= 0:
        return 0.0, 0.0
    fec_per_yr     = effective_fec_per_day * 365
    total_deg_rate = batt.calendar_deg_per_yr + batt.cycle_deg_per_fec * fec_per_yr
    replace_interval = max(1.0, (1 - batt.replace_threshold) / total_deg_rate)
    power_rating = 1.0 if storage_hours <= 4.0 else min(1.0, 4.0 / storage_hours)
    cost_one = storage_hours * capex_kwh * 1e3 + power_rating * capex_kw * 1e3
    npv = cost_one
    t = replace_interval
    while t < n_yr:
        npv += cost_one / (1 + r) ** t
        t += replace_interval
    return npv * crf(r, n_yr), cost_one * batt.om_frac_capex


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
            f_peak = sim.interp3(sim.gas_peak_firm_mean, C_sol, C_win, B)
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

        # Path regularization penalty to prevent bouncing in flat cost valley
        penalty_smooth = 0.0
        if prev_x is not None:
            # Scale battery capacity deviation so it's comparable to C_sol/C_win
            delta = (x - prev_x) * np.array([1.0, 1.0, 0.1])
            penalty_smooth = 0.001 * np.sum(delta ** 2)

        return c_gen + c_stor + c_gas + c_pen + penalty + penalty_smooth

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

    # Boundary-binding guard: warn if any optimum reaches its max bound (within
    # one grid-step). A binding cap means the true optimum may lie beyond the grid
    # and the cost is understated — raise the corresponding *_max in SystemParams.
    _warn_if_binding(C_sol, sys.c_sol_max, "C_sol", sys.grid_steps, r_target, year_index)
    _warn_if_binding(C_win, sys.c_win_max, "C_win", sys.grid_steps, r_target, year_index)
    _warn_if_binding(B,     sys.storage_hours_max, "B", sys.grid_steps, r_target, year_index)

    c_gen, c_stor, c_gas, c_pen, _, f_drop = evaluate(C_sol, C_win, B)
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
        f_peak = sim.interp3(sim.gas_peak_firm_mean, C_sol, C_win, B)

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


# ─────────────────────────────────────────────────────────────────────────────
# 9. PLOTTING
# ─────────────────────────────────────────────────────────────────────────────

C_OPT = "#3A86FF"; C_GAS = "#6B705C"; C_BATT = "#9D4EDD"; C_SMR = "#E71D36"
C_SOL = "#FF9F1C"; C_WIN = "#2EC4B6"; C_PPA = "#06D6A0"
REFS  = "Lazard v18 · Way et al. Joule 2022 · NREL ATB 2024 · EU ETS · IPCC AR6"
PALETTE = ["#3A86FF", "#FF9F1C", "#2EC4B6", "#9D4EDD", "#FB5607", "#E71D36"]


def _crossings(ax, years, series, baseline, color, label):
    diff = series - baseline
    idxs = np.where(np.diff(np.sign(diff)))[0]
    for idx in idxs:
        frac = diff[idx] / (diff[idx] - diff[idx+1])
        cx = years[idx] + frac
        cy = baseline[idx] + frac * (baseline[idx+1] - baseline[idx])
        ax.plot(cx, cy, "o", color=color, ms=7, zorder=5)
        ax.annotate(f"{label} {cx:.0f}", xy=(cx, cy), xytext=(10, 8),
                    textcoords="offset points", fontsize=7, color=color,
                    arrowprops=dict(arrowstyle="->", color=color, lw=0.8))


def plot_cost_trajectories(results, region="US"):
    yrs = results["years"]
    Rs  = sorted(results["scenarios"].keys())
    fig, ax = plt.subplots(figsize=(7, 5))
    for R, col in zip(Rs, PALETTE):
        sc = results["scenarios"][R]
        ax.plot(yrs, sc["opt_delivered"], color=col, lw=2, label=f"Optimal ({R:.0%} RE)")
        ax.fill_between(yrs, sc["opt_delivered_low"], sc["opt_delivered_high"],
                        color=col, alpha=0.12, edgecolor="none")
        _crossings(ax, yrs, sc["opt_delivered"], results["gas_pure"], col, f"{R:.0%}")
    ax.plot(yrs, results["gas_pure"], color=C_GAS, lw=2, ls="--", label=results["gas_name"])
    ax.plot(yrs, results["lcoe_smr"], color=C_SMR, lw=2, ls="-.", label=results["smr_name"])
    if "grid_ppa" in results:
        ax.plot(yrs, results["grid_ppa"], color=C_PPA, lw=2, ls=":",
                label=results["grid_ppa_name"])
    ax.set(xlabel="Year", ylabel="Delivered cost ($/MWh)",
           title=f"Delivered cost trajectory — {region}",
           xlim=(yrs[0], yrs[-1]), ylim=(0, None))
    ax.legend(fontsize=9, frameon=True, facecolor="white", framealpha=1)
    ax.text(0.01, 0.01, REFS, transform=ax.transAxes, fontsize=6, va="bottom",
            alpha=0.5, family="monospace")
    fig.tight_layout(); return fig


def plot_reliability_sensitivity(results, target_year=2030, region="US"):
    idx   = target_year - results["years"][0]
    Rs    = sorted(results["scenarios"].keys())
    r_pct = [r * 100 for r in Rs]
    vals  = [results["scenarios"][R]["opt_delivered"][idx] for R in Rs]
    los   = [results["scenarios"][R]["opt_delivered_low"][idx] for R in Rs]
    his   = [results["scenarios"][R]["opt_delivered_high"][idx] for R in Rs]
    fig, ax = plt.subplots(figsize=(6, 4.5))
    ax.plot(r_pct, vals, "o-", color=C_OPT, lw=2, label="Optimal blend")
    ax.fill_between(r_pct, los, his, color=C_OPT, alpha=0.15, edgecolor="none")
    g = results["gas_pure"][idx]; s = results["lcoe_smr"][idx]
    ax.axhline(g, color=C_GAS, ls="--", lw=2, label=f"Gas CCGT (${g:.0f}/MWh)")
    ax.axhline(s, color=C_SMR, ls="-.", lw=2, label=f"SMR (${s:.0f}/MWh)")
    if "grid_ppa" in results:
        gp = results["grid_ppa"][idx]
        ax.axhline(gp, color=C_PPA, ls=":", lw=2, label=f"Grid+RE PPA (${gp:.0f}/MWh)")
    ax.set(xlabel="Renewable fraction (%)", ylabel="Delivered cost ($/MWh)",
           title=f"Cost vs. RE fraction at {target_year} — {region}",
           ylim=(0, None))
    ax.legend(fontsize=9, frameon=True, facecolor="white", loc="lower right")
    ax.text(0.01, 0.01, REFS, transform=ax.transAxes, fontsize=6, va="bottom",
            alpha=0.5, family="monospace")
    fig.tight_layout(); return fig


def plot_optimal_mix(results, region="US"):
    Rs = sorted(results["scenarios"].keys()); yrs = results["years"]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4))
    for R, col in zip(Rs, PALETTE):
        sc = results["scenarios"][R]; lbl = f"{R:.0%} RE"
        axes[0].plot(yrs, sc["opt_csol"], color=col, lw=2, label=lbl)
        axes[1].plot(yrs, sc["opt_cwin"], color=col, lw=2)
        axes[2].plot(yrs, sc["opt_B"],    color=col, lw=2)
    axes[0].set(xlabel="Year", ylabel="Solar overbuild (×load)", ylim=(0,None), title="Solar")
    axes[1].set(xlabel="Year", ylabel="Wind overbuild (×load)", ylim=(0,None), title="Wind")
    axes[2].set(xlabel="Year", ylabel="Storage duration (h)", ylim=(0,None), title="Battery")
    axes[0].legend(fontsize=9, title="RE target")
    fig.suptitle(f"Optimal capacity mix — {region}", fontsize=12)
    fig.tight_layout(); return fig


def plot_component_breakdown(results, reliability=0.90, region="US"):
    """Delivered-cost breakdown by factor (generation / battery / gas), each split
    into capex vs opex. Solid fill = capex, hatched = opex, within a colour family."""
    sc = results["scenarios"][reliability]; yrs = results["years"]
    z = np.zeros_like(sc["gen_capex"])
    # (values, label, colour, hatch)  — capex solid, opex hatched, per factor
    bands = [
        (sc["gen_capex"],          "Generation — capex", C_SOL,  None),
        (sc["gen_om"],             "Generation — O&M",   C_SOL,  "////"),
        (sc["batt_capex"],         "Battery — capex",    C_BATT, None),
        (sc["batt_om"],            "Battery — O&M",      C_BATT, "////"),
        (sc["gas_capex"],          "Gas — capex",        C_GAS,  None),
        (sc["gas_opex"],           "Gas — fuel + O&M",   C_GAS,  "////"),
        (sc["gas_carbon"],         "Gas — carbon",       "#2B2D42", "xx"),
        (sc.get("opt_cp", z),      "Lost compute (shed)", C_SMR, ".."),
    ]
    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    bottom = np.zeros_like(yrs, dtype=float)
    for vals, lbl, col, hatch in bands:
        if np.allclose(vals, 0):   # skip empty bands (e.g. shed for firm)
            continue
        ax.fill_between(yrs, bottom, bottom + vals, label=lbl, facecolor=col,
                        alpha=0.85, hatch=hatch, edgecolor="white", linewidth=0.3)
        bottom = bottom + vals
    ax.plot(yrs, results["gas_pure"], color="#E07A5F", lw=2, ls="--", label="Gas CCGT (pure)")
    ax.set(xlabel="Year", ylabel="Delivered cost ($/MWh)",
           title=f"Cost breakdown (capex/opex by factor) at {reliability:.0%} RE — {region}",
           xlim=(yrs[0], yrs[-1]), ylim=(0, None))
    ax.legend(fontsize=8, frameon=True, facecolor="white",
              loc="upper left", bbox_to_anchor=(1.01, 1.0),
              title="solid = capex · hatched = opex")
    ax.text(0.01, 0.01, REFS, transform=ax.transAxes, fontsize=6, va="bottom",
            alpha=0.5, family="monospace")
    fig.tight_layout(); return fig


def plot_flex_heatmap(sweep: Dict) -> "plt.Figure":
    """
    2D flexibility surface: delivered LCOE (left) and parity year vs gas (right)
    over interruptible-fraction × shed-penalty (value of lost compute).
    """
    it = sweep["interruptibles"]; pen = sweep["shed_penalties"]
    L = sweep["lcoe"]; P = sweep["parity"]; yr = sweep["target_year"]; region = sweep["region"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))

    def _heat(ax, M, title, cmap_name, fmt):
        cmap = plt.get_cmap(cmap_name)
        finite = M[np.isfinite(M)]
        vmin, vmax = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
        rng = vmax - vmin or 1.0
        im = ax.imshow(M, origin="lower", aspect="auto", cmap=cmap, vmin=vmin, vmax=vmax,
                       extent=[-0.5, len(pen) - 0.5, -0.5, len(it) - 0.5])
        ax.set_xticks(range(len(pen))); ax.set_xticklabels([f"{p:.0f}" for p in pen])
        ax.set_yticks(range(len(it))); ax.set_yticklabels([f"{f:.0%}" for f in it])
        ax.set(xlabel="Value of lost compute (\\$/MWh)",
               ylabel="Interruptible fraction", title=title)
        for i in range(len(it)):
            for j in range(len(pen)):
                v = M[i, j]
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    ax.text(j, i, "—", ha="center", va="center", fontsize=8, color="0.3")
                    continue
                # adaptive text colour: dark text on light cells, white on dark
                r, g, b, _ = cmap((v - vmin) / rng)
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                ax.text(j, i, fmt(v), ha="center", va="center", fontsize=8,
                        color="black" if lum > 0.55 else "white")
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)

    _heat(axes[0], L, f"Delivered LCOE {yr} (\\$/MWh) — {region}", "viridis_r",
          lambda v: f"{v:.0f}")
    _heat(axes[1], P, f"Parity year vs gas — {region}", "RdYlGn_r",
          lambda v: f"{v:.0f}")
    fig.suptitle(f"Flexibility trade-off surface — {region} — {sweep['re_target']:.0%} RE",
                 fontsize=12)
    fig.tight_layout(); return fig


# ─────────────────────────────────────────────────────────────────────────────
# 10. SUMMARY TABLE
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(results, region="US"):
    yrs = results["years"]
    milestones = [y for y in [2025, 2028, 2030, 2035, 2040] if y <= yrs[-1]]
    cf = results["sim_cf"]
    print(f"\n{'═'*102}")
    print(f"  DATACENTER POWER COST — {region}  |  ρ(sol,wind)={results['wind_solar_corr']:.2f}")
    print(f"  Simulated CF:  Solar={cf['solar']:.3f}   Wind={cf['wind']:.3f}")
    if "grid_ppa" in results:
        gp = results["grid_ppa"]
        ref = "  ".join(f"{y}:${gp[y-yrs[0]]:.0f}" for y in milestones)
        print(f"  Grid+RE PPA reference (on-grid alt., $/MWh):  {ref}")
    print(f"{'═'*102}")
    for R in sorted(results["scenarios"].keys()):
        sc = results["scenarios"][R]
        print(f"\n  RE target: {R:.0%} of served energy  |  Gas ≤{1-R:.0%} of served")
        print(f"  {'Year':<6}{'Optimal':>10}{'P10':>8}{'P90':>8}"
              f"{'Gas':>9}{'SMR':>7}{'vs Gas':>8}  {'C_sol':>6}{'C_win':>6}{'B(h)':>6}{'Shed':>6}")
        print(f"  {'─'*94}")
        for yr in milestones:
            i = yr - yrs[0]
            t = sc["opt_delivered"][i]; lo = sc["opt_delivered_low"][i]
            hi = sc["opt_delivered_high"][i]
            g  = results["gas_pure"][i]; s = results["lcoe_smr"][i]
            vs = (t / g - 1) * 100
            cs = sc["opt_csol"][i]; cw = sc["opt_cwin"][i]; b = sc["opt_B"][i]
            shed = sc.get("opt_shed", np.zeros_like(sc["opt_csol"]))[i] * 100
            print(f"  {yr:<6}${t:>7.1f}   ${lo:>5.1f}  ${hi:>5.1f}"
                  f"   ${g:>6.1f}  ${s:>4.1f}  {vs:>+7.1f}%"
                  f"  {cs:>5.1f}× {cw:>5.1f}× {b:>4.0f}h {shed:>4.1f}%")
        if "opt_delivered_p90" in sc:
            # Robustness-design series: cost of sizing for a 1-in-10 bad weather year.
            p90 = sc["opt_delivered_p90"]; mean = sc["opt_delivered"]
            cells = "  ".join(
                f"{y}:${p90[y-yrs[0]]:.0f}(+{(p90[y-yrs[0]]/mean[y-yrs[0]]-1)*100:.0f}%)"
                for y in milestones)
            print(f"  P90-designed (size for 1-in-10 weather, premium vs mean):  {cells}")
    print(f"\n{'═'*102}\n")


# ─────────────────────────────────────────────────────────────────────────────
# 10b. MACHINE-READABLE RESULTS EXPORT
# ─────────────────────────────────────────────────────────────────────────────

# Per-year, per-factor fields written to CSV/JSON. Keys are the `results`
# scenario arrays; the CSV column order follows this list.
_EXPORT_FIELDS = [
    ("lcoe",       "opt_delivered"),
    ("lcoe_p10",   "opt_delivered_low"),
    ("lcoe_p90",   "opt_delivered_high"),
    ("c_solar",    "opt_csol"),
    ("c_wind",     "opt_cwin"),
    ("battery_h",  "opt_B"),
    ("shed_frac",  "opt_shed"),
    ("gen_capex",  "gen_capex"),
    ("gen_om",     "gen_om"),
    ("batt_capex", "batt_capex"),
    ("batt_om",    "batt_om"),
    ("gas_capex",  "gas_capex"),
    ("gas_opex",   "gas_opex"),
    ("gas_carbon", "gas_carbon"),
    ("lcoe_p90design", "opt_delivered_p90"),   # present only when design_p90=True
]


def export_results(results: Dict, region: str, prefix: str,
                   outdir: str = "output") -> Tuple[str, str]:
    """
    Write the scenario results to a tidy CSV (one row per RE-target × year) and a
    structured JSON (with run metadata). Purely additive — emits the exact numbers
    already computed so downstream analysis and the documentation tables can be
    regenerated programmatically instead of hand-transcribed.

    Returns (csv_path, json_path).
    """
    os.makedirs(outdir, exist_ok=True)
    yrs = [int(y) for y in results["years"]]
    gas = [float(v) for v in results["gas_pure"]]
    smr = [float(v) for v in results["lcoe_smr"]]
    ppa = ([float(v) for v in results["grid_ppa"]] if "grid_ppa" in results
           else [None] * len(yrs))
    Rs  = sorted(results["scenarios"].keys())

    csv_path  = os.path.join(outdir, f"{prefix}_results.csv")
    json_path = os.path.join(outdir, f"{prefix}_results.json")

    # ── CSV (long / tidy format) ────────────────────────────────────────────────
    cols = (["region", "re_target", "year"]
            + [name for name, _ in _EXPORT_FIELDS] + ["gas_pure", "smr", "grid_ppa"])
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for R in Rs:
            sc = results["scenarios"][R]
            for i, yr in enumerate(yrs):
                row = [region, f"{R:.2f}", yr]
                for _, key in _EXPORT_FIELDS:
                    arr = sc.get(key)
                    row.append(f"{float(arr[i]):.4f}" if arr is not None else "")
                row += [f"{gas[i]:.4f}", f"{smr[i]:.4f}",
                        f"{ppa[i]:.4f}" if ppa[i] is not None else ""]
                w.writerow(row)

    # ── JSON (structured, with metadata) ────────────────────────────────────────
    payload = {
        "region": region,
        "workload": results.get("workload_name"),
        "wind_solar_corr": float(results.get("wind_solar_corr", 0.0)),
        "simulated_cf": {k: float(v) for k, v in results["sim_cf"].items()},
        "years": yrs,
        "gas_pure": gas,
        "smr": smr,
        "grid_ppa": ppa if "grid_ppa" in results else None,
        "scenarios": {
            f"{R:.2f}": {name: [round(float(v), 4) for v in results["scenarios"][R][key]]
                         for name, key in _EXPORT_FIELDS
                         if key in results["scenarios"][R]}
            for R in Rs
        },
    }
    with open(json_path, "w") as fh:
        json.dump(payload, fh, indent=2)

    print(f"  [Export] wrote {csv_path} and {json_path}")
    return csv_path, json_path


# ─────────────────────────────────────────────────────────────────────────────
# 11. MAIN
# ─────────────────────────────────────────────────────────────────────────────

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


def _parity_year(yrs, series, baseline):
    """First (interpolated) year `series` drops to/below `baseline`; nan if never."""
    series = np.asarray(series, float); baseline = np.asarray(baseline, float)
    diff = series - baseline
    if diff[0] <= 0:
        return float(yrs[0])
    idxs = np.where(np.diff(np.sign(diff)))[0]
    if len(idxs) == 0:
        return float("nan")
    i = idxs[0]
    frac = diff[i] / (diff[i] - diff[i + 1])
    return float(yrs[i] + frac)


def run_flex_sensitivity(region_key="eu", re_target=0.90, target_year=2030,
                         interruptibles=None, shed_penalties=None, years=15,
                         grid_steps=15, n_mc=15, n_cost_mc=20, seed=42):
    """
    Sweep workload flexibility — interruptible_fraction × shed_penalty (value of
    lost compute) — and record delivered LCOE (at target_year) and parity year vs
    gas, at a fixed RE target. Each point re-runs the dispatch, so this uses
    REDUCED fidelity (coarser grid, fewer MC years). Opt-in & slow.
    """
    if interruptibles is None:
        interruptibles = [0.0, 0.2, 0.4, 0.7, 0.95]
    if shed_penalties is None:
        # straddle the gas variable cost (~$120/MWh in EU 2030) where shedding
        # switches on; above it, premium compute never sheds (→ firm).
        shed_penalties = [25.0, 75.0, 150.0, 300.0, 700.0]
    cfg = REGIONS[region_key]
    # The sweep spans down to 0% flexibility / very high penalty, where inflexible
    # high-RE systems need much larger overbuild than the flexible headline cases —
    # so widen the bounds here (at coarser resolution) to avoid cap-binding.
    base = cfg["sys"]
    sys_lo = _sys_with(base, grid_steps=grid_steps, n_mc_weather=n_mc,
                       c_sol_max=max(base.c_sol_max, 24.0),
                       c_win_max=max(base.c_win_max, 22.0),
                       storage_hours_max=max(base.storage_hours_max, 72.0))
    nI, nP = len(interruptibles), len(shed_penalties)
    L = np.full((nI, nP), np.nan); P = np.full((nI, nP), np.nan)
    gas_at_year = np.nan

    print(f"\n[Flex sweep] {cfg['label']} | {re_target:.0%} RE | {nI}×{nP} points "
          f"| grid={grid_steps}³, MC={n_mc} (reduced fidelity) — this takes a few minutes …")
    for i, ifrac in enumerate(interruptibles):
        for j, pen in enumerate(shed_penalties):
            wl = WorkloadProfile(f"int {ifrac:.0%} @ ${pen:.0f}",
                                 interruptible_fraction=ifrac, shed_penalty_mwh=pen)
            res = run_simulation(
                solar=cfg["solar"], wind=cfg["wind"], battery=cfg["battery"],
                gas=cfg["gas"], smr=cfg["smr"], sys=sys_lo, workload=wl,
                mean_irr=cfg["mean_irr"], mean_wind_ms=cfg["mean_wind_ms"],
                years=years, reliabilities=[re_target], n_cost_mc=n_cost_mc, seed=seed)
            yrs = res["years"]; traj = res["scenarios"][re_target]["opt_delivered"]
            gas = res["gas_pure"]; gas_at_year = float(gas[target_year - yrs[0]])
            L[i, j] = float(traj[target_year - yrs[0]])
            P[i, j] = _parity_year(yrs, traj, gas)
        print(f"  interruptible {ifrac:.0%}: LCOE@{target_year} = "
              f"{np.array2string(L[i], precision=0)}")

    ref_int_idx = int(np.argmin([abs(f - 0.40) for f in interruptibles]))
    ref_pen_idx = int(np.argmin([abs(p - 600.0) for p in shed_penalties]))
    return {
        "region": cfg["label"], "re_target": re_target, "target_year": target_year,
        "interruptibles": interruptibles, "shed_penalties": shed_penalties,
        "lcoe": L, "parity": P, "gas_at_year": gas_at_year,
        "ref_int_idx": ref_int_idx, "ref_pen_idx": ref_pen_idx,
    }


def run_resource_sensitivity(region_key="us", re_target=0.90, years=15, seed=42,
                             grid_steps=None, n_mc=None):
    """
    How much do the headline (conservative) capacity factors matter? Re-runs the
    firm optimisation at each RESOURCE_PRESETS level for `region_key` and prints a
    side-by-side table of delivered LCOE and parity year vs gas, so the reader sees
    how far the parity conclusion moves on a modern, well-sited (good-resource)
    plant. Reuses the full model; opt-in (a couple of minutes). Returns a dict.
    """
    cfg = REGIONS[region_key]
    sys = cfg["sys"]
    overrides = {k: v for k, v in (("grid_steps", grid_steps),
                                   ("n_mc_weather", n_mc)) if v}
    if overrides:
        sys = _sys_with(sys, **overrides)
    levels = RESOURCE_PRESETS[region_key]

    print(f"\n[Resource sweep] {cfg['label']} | {re_target:.0%} RE (firm) | "
          f"levels: {', '.join(levels)} — re-runs the model per level …")
    out = {}
    for name, (mi, mw) in levels.items():
        res = run_simulation(
            solar=cfg["solar"], wind=cfg["wind"], battery=cfg["battery"],
            gas=cfg["gas"], smr=cfg["smr"], sys=sys, workload=FIRM,
            mean_irr=mi, mean_wind_ms=mw, years=years,
            reliabilities=[re_target], seed=seed, grid_ppa=cfg.get("grid_ppa"))
        yrs = res["years"]; traj = res["scenarios"][re_target]["opt_delivered"]
        gas = res["gas_pure"]
        out[name] = {"mean_irr": mi, "mean_wind_ms": mw, "cf": res["sim_cf"],
                     "years": yrs, "lcoe": traj, "gas": gas,
                     "parity_gas": _parity_year(yrs, traj, gas)}

    # ── comparison table ────────────────────────────────────────────────────────
    names = list(levels)
    yrs = out[names[0]]["years"]
    milestones = [y for y in [2025, 2030, 2035, 2040] if y <= yrs[-1]]
    print(f"\n  RESOURCE SENSITIVITY — {cfg['label']} | {re_target:.0%} RE (firm)")
    for name in names:
        cf = out[name]["cf"]; py = out[name]["parity_gas"]
        parity = ">horizon" if math.isnan(py) else f"{py:.0f}"
        print(f"    {name:<8} mean_irr={out[name]['mean_irr']:.1f}  "
              f"mean_wind={out[name]['mean_wind_ms']:.1f} m/s  →  "
              f"CF solar={cf['solar']:.3f}  wind={cf['wind']:.3f}  "
              f"|  parity vs gas: {parity}")
    hdr = "    " + f"{'Year':<6}" + "".join(f"{n+' $/MWh':>16}" for n in names) + f"{'Gas':>9}"
    print("\n" + hdr); print("    " + "─" * (len(hdr) - 4))
    for y in milestones:
        i = y - yrs[0]
        cells = "".join(f"{out[n]['lcoe'][i]:>15.1f} " for n in names)
        print(f"    {y:<6}{cells}{out[names[0]]['gas'][i]:>8.1f}")
    print()
    return out


def run_tornado(region_key="eu", re_target=0.90, target_year=2030, years=15,
                grid_steps=11, n_mc=12, seed=42):
    """
    One-at-a-time sensitivity of the **parity gap** (firm RE delivered LCOE − gas
    LCOE, $/MWh, at `target_year`) to the key assumptions. Negative gap = RE beats
    gas. Each lever is swung low/high around the v5.3 base; the result is the classic
    tornado ranking of what most moves competitiveness. Reduced fidelity (coarser
    grid, fewer MC years) — opt-in & slow; treat magnitudes as indicative.
    """
    cfg = REGIONS[region_key]
    # Widen bounds (as in the flex sweep) so the coarse reduced-fidelity grid does
    # not bind the wind/solar cap and bias the gap, esp. for the low-resource swings.
    base_sys = cfg["sys"]
    sysx = _sys_with(base_sys, grid_steps=grid_steps, n_mc_weather=n_mc,
                     c_sol_max=max(base_sys.c_sol_max, 24.0),
                     c_win_max=max(base_sys.c_win_max, 24.0),
                     storage_hours_max=max(base_sys.storage_hours_max, 72.0))
    s, w, b, g = cfg["solar"], cfg["wind"], cfg["battery"], cfg["gas"]
    base = dict(solar=s, wind=w, battery=b, gas=g,
                mean_irr=cfg["mean_irr"], mean_wind_ms=cfg["mean_wind_ms"])

    def gap(**over):
        p = {**base, **over}
        res = run_simulation(
            solar=p["solar"], wind=p["wind"], battery=p["battery"], gas=p["gas"],
            smr=cfg["smr"], sys=sysx, workload=FIRM, mean_irr=p["mean_irr"],
            mean_wind_ms=p["mean_wind_ms"], years=years, reliabilities=[re_target],
            n_cost_mc=10, seed=seed)
        yi = target_year - res["years"][0]
        return float(res["scenarios"][re_target]["opt_delivered"][yi] - res["gas_pure"][yi])

    base_gap = gap()
    # (label, low-overrides, high-overrides). "low" = the variant expected to lower
    # the gap (help RE); ordering is normalised when plotting anyway.
    levers = [
        ("Gas price ∓25%",
         dict(gas=replace(g, gas_price_mmbtu=g.gas_price_mmbtu * 1.25)),
         dict(gas=replace(g, gas_price_mmbtu=g.gas_price_mmbtu * 0.75))),
        ("RE WACC 4% / 7%",
         dict(solar=replace(s, wacc=0.04), wind=replace(w, wacc=0.04)),
         dict(solar=replace(s, wacc=0.07), wind=replace(w, wacc=0.07))),
        ("Gas WACC 11% / 7%",
         dict(gas=replace(g, wacc=0.11)),
         dict(gas=replace(g, wacc=0.07))),
        ("Wind resource ±10%",
         dict(mean_wind_ms=cfg["mean_wind_ms"] * 1.10),
         dict(mean_wind_ms=cfg["mean_wind_ms"] * 0.90)),
        ("Solar resource ±10%",
         dict(mean_irr=cfg["mean_irr"] * 1.10),
         dict(mean_irr=cfg["mean_irr"] * 0.90)),
        ("Solar learning 35% / 25%",
         dict(solar=replace(s, learning_rate=0.35)),
         dict(solar=replace(s, learning_rate=0.25))),
        ("Battery capex ∓20%",
         dict(battery=replace(b, capex_kwh_today=b.capex_kwh_today * 0.80,
                              capex_kw_today=b.capex_kw_today * 0.80)),
         dict(battery=replace(b, capex_kwh_today=b.capex_kwh_today * 1.20,
                              capex_kw_today=b.capex_kw_today * 1.20))),
        ("Carbon ceiling ±25%",
         dict(gas=replace(g, carbon_price_ceiling=g.carbon_price_ceiling * 1.25)),
         dict(gas=replace(g, carbon_price_ceiling=g.carbon_price_ceiling * 0.75))),
    ]
    print(f"\n[Tornado] {cfg['label']} | {re_target:.0%} RE | gap @ {target_year} "
          f"(base {base_gap:+.1f} $/MWh) | grid={grid_steps}³ MC={n_mc} — a few minutes …")
    rows = []
    for name, lo, hi in levers:
        lo_v, hi_v = gap(**lo), gap(**hi)
        rows.append((name, lo_v, hi_v))
        print(f"  {name:<26} {lo_v:+7.1f} … {hi_v:+7.1f}  (swing {abs(hi_v-lo_v):4.1f})")
    rows.sort(key=lambda r: abs(r[2] - r[1]))
    return {"region": cfg["label"], "re_target": re_target,
            "target_year": target_year, "base": base_gap, "rows": rows}


def plot_tornado(t: Dict) -> "plt.Figure":
    """Horizontal tornado of the parity-gap sensitivity (negative = RE beats gas)."""
    rows = t["rows"]; base = t["base"]
    labels = [r[0] for r in rows]
    fig, ax = plt.subplots(figsize=(8, 0.5 * len(rows) + 1.6))
    for i, (name, lo, hi) in enumerate(rows):
        left, right = min(lo, hi), max(lo, hi)
        ax.barh(i, right - base, left=base, color=C_GAS, alpha=0.85)
        ax.barh(i, base - left, left=left, color=C_OPT, alpha=0.85)
        ax.text(left, i, f"{lo:+.0f}", va="center", ha="right", fontsize=7)
        ax.text(right, i, f"{hi:+.0f}", va="center", ha="left", fontsize=7)
    ax.axvline(base, color="black", lw=1.2, ls="--",
               label=f"base {base:+.0f} $/MWh")
    ax.axvline(0, color="#888", lw=1, ls=":")
    ax.set_yticks(range(len(rows))); ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("Parity gap: firm RE LCOE − gas LCOE ($/MWh) — negative = RE wins")
    ax.set_title(f"Tornado — {t['region']} — {t['re_target']:.0%} RE @ {t['target_year']}")
    ax.legend(fontsize=8, loc="lower right")
    ax.text(0.01, 0.01, REFS, transform=ax.transAxes, fontsize=6, va="bottom",
            alpha=0.5, family="monospace")
    fig.tight_layout(); return fig


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


def build_arg_parser():
    import argparse
    p = argparse.ArgumentParser(
        description="Off-grid datacenter LCOE model (v5.2). No args → firm US+EU suite.")
    p.add_argument("--region", choices=list(REGIONS), help="us | eu")
    p.add_argument("--workload", choices=list(WORKLOAD_PRESETS),
                   help="flexibility preset for a single-scenario run "
                        "(default: training). With no scenario args at all, the "
                        "model instead runs the firm US+EU suite.")
    p.add_argument("--interruptible", type=float,
                   help="override: interruptible (sheddable) fraction of load [0–1]")
    p.add_argument("--shed-penalty", type=float,
                   help="override: value of lost compute, $/MWh shed (deep = firm)")
    p.add_argument("--re", type=float, nargs="+",
                   help="RE targets, e.g. --re 0.8 0.9 0.95")
    p.add_argument("--years", type=int, default=15, help="projection horizon (default 15)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--flex-sweep", action="store_true",
                   help="run the flexibility sensitivity (interruptible × compute-value heatmap)")
    p.add_argument("--design-p90", action="store_true",
                   help="also report a robustness-design series sized against the "
                        "1-in-10 (P90) weather year (single-scenario runs)")
    p.add_argument("--resource", choices=["default", "good"],
                   help="resource quality for a single-scenario run: conservative "
                        "default site, or a modern well-sited 'good' resource")
    p.add_argument("--resource-sweep", action="store_true",
                   help="compare default vs good-site resource (LCOE + parity table)")
    p.add_argument("--tornado", action="store_true",
                   help="parity-gap tornado: sensitivity of RE-vs-gas competitiveness "
                        "to key assumptions → figure")
    p.add_argument("--grid-steps", type=int, help="advanced: optimiser grid resolution")
    p.add_argument("--mc", type=int, help="advanced: Monte-Carlo weather years")
    return p


def _validate_args(parser, args) -> None:
    """Reject out-of-range CLI inputs with a clean error (not a deep stack trace)."""
    if args.interruptible is not None and not (0.0 <= args.interruptible <= 1.0):
        parser.error("--interruptible must be a fraction in [0, 1]")
    if args.shed_penalty is not None and args.shed_penalty < 0.0:
        parser.error("--shed-penalty must be ≥ 0 ($/MWh of lost compute)")
    if args.re is not None and any(not (0.0 < r < 1.0) for r in args.re):
        parser.error("--re targets must each be strictly between 0 and 1")
    if args.years is not None and args.years < 1:
        parser.error("--years must be ≥ 1")
    if args.grid_steps is not None and args.grid_steps < 2:
        parser.error("--grid-steps must be ≥ 2 (need ≥2 nodes per axis to interpolate)")
    if args.mc is not None and args.mc < 1:
        parser.error("--mc must be ≥ 1")


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    os.makedirs("figs", exist_ok=True)

    # Flexibility sensitivity sweep
    if args.flex_sweep:
        region = args.region or "eu"
        re_t = (args.re or [0.90])[0]
        sweep = run_flex_sensitivity(
            region_key=region, re_target=re_t, target_year=2030,
            grid_steps=args.grid_steps or 15, n_mc=args.mc or 15, seed=args.seed)
        name = f"{region}_flex_heatmap"
        fig = plot_flex_heatmap(sweep)
        fig.savefig(f"figs/{name}.png", dpi=200, bbox_inches="tight"); plt.close(fig)
        print(f"\nDone — flexibility figure saved: figs/{name}.png")
        return

    # Resource-quality sensitivity (default vs good site)
    if args.resource_sweep:
        region = args.region or "us"
        re_t = (args.re or [0.90])[0]
        run_resource_sensitivity(region_key=region, re_target=re_t,
                                 years=args.years, seed=args.seed,
                                 grid_steps=args.grid_steps, n_mc=args.mc)
        return

    # Parity-gap tornado sensitivity
    if args.tornado:
        region = args.region or "eu"
        re_t = (args.re or [0.90])[0]
        t = run_tornado(region_key=region, re_target=re_t, target_year=2030,
                        grid_steps=args.grid_steps or 11, n_mc=args.mc or 12,
                        seed=args.seed)
        name = f"{region}_tornado"
        fig = plot_tornado(t)
        fig.savefig(f"figs/{name}.png", dpi=200, bbox_inches="tight"); plt.close(fig)
        print(f"\nDone — tornado figure saved: figs/{name}.png")
        return

    # Single custom scenario
    if args.region or args.workload or args.interruptible is not None \
            or args.shed_penalty is not None or args.re or args.design_p90 or args.resource:
        region = args.region or "us"
        wl = WORKLOAD_PRESETS[args.workload] if args.workload else AI_TRAINING
        if args.interruptible is not None or args.shed_penalty is not None:
            ifrac = args.interruptible if args.interruptible is not None else wl.interruptible_fraction
            pen = args.shed_penalty if args.shed_penalty is not None else wl.shed_penalty_mwh
            wl = WorkloadProfile(f"custom {ifrac:.0%} @ ${pen:.0f}",
                                 interruptible_fraction=ifrac, shed_penalty_mwh=pen)
        reliabilities = args.re or [0.70, 0.80, 0.90, 0.95]
        sys_ov = {}
        if args.grid_steps:
            sys_ov["grid_steps"] = args.grid_steps
        if args.mc:
            sys_ov["n_mc_weather"] = args.mc
        mi = mw = None
        if args.resource:
            mi, mw = RESOURCE_PRESETS[region][args.resource]
        prefix = f"cli_{region}_{args.workload or 'training'}"
        run_region_key(region, wl, reliabilities, prefix=prefix,
                       sys_overrides=sys_ov or None, seed=args.seed,
                       design_p90=args.design_p90, mean_irr=mi, mean_wind_ms=mw)
        print(f"\nDone — figures saved with prefix figs/{prefix}_*.png")
        return

    # Default: full suite
    run_full_suite()


if __name__ == "__main__":
    main()
