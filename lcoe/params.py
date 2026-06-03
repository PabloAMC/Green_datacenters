from __future__ import annotations

"""Dataclasses, technology/region presets, and system parameters."""
from dataclasses import dataclass


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

    Note: the base line represents *annual-volumetric* RE matching. A second
    reference, **24/7 CFE** (hour-by-hour carbon-free matching, the Google/Microsoft
    target), adds `cfe_premium_mwh` on top — the extra cost of matching demand with
    clean supply in *every* hour (firm clean / storage / deep overbuild). Both are
    reference lines, never part of the optimisation.
    """
    name: str = "Grid + RE PPA"
    ppa_energy_today: float = 45.0     # $/MWh contracted RE energy (LevelTen index, US)
    grid_delivery_mwh: float = 22.0    # $/MWh T&D / network charge, large C&I
    firming_premium_mwh: float = 8.0   # $/MWh balancing/standby to firm PPA to 24/7
    cfe_premium_mwh: float = 40.0      # $/MWh extra for 24/7 hourly CFE vs annual matching


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

# Green-hydrogen firming (opt-in alternative to gas, via --firming h2). Purchased
# green H2 burned in an H2-capable turbine: **zero combustion carbon**, but the fuel
# is expensive and uncertain (~$4/kg ≈ $35/MMBtu LHV) and the turbine + on-site H2
# handling cost more than a gas peaker. Reuses the entire gas dispatch/cost path —
# it is, economically, "a gas plant with pricey zero-carbon fuel". Region-invariant
# (H2 is a globally-traded commodity); every figure is adjustable.
GAS_H2 = GasParams(
    name="Green H2 firming",
    gas_price_mmbtu=35.0,          # ≈ $4/kg green H2 (LHV); adjustable
    ccgt_capex_kw=1300.0,          # H2-ready turbine + H2 handling (vs 1100 NG)
    ocgt_capex_kw=600.0,           # (vs 500 NG)
    carbon_price_today=0.0,
    carbon_trajectory="linear",
    carbon_intensity_ccgt=0.0,     # green H2 → no combustion CO2
    carbon_intensity_ocgt=0.0,
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
                            grid_delivery_mwh=33.0, firming_premium_mwh=12.0,
                            cfe_premium_mwh=55.0)

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

# Firming-resource choice (CLI --firming). "gas" keeps the region's default natural
# gas; "h2" swaps in green-hydrogen firming (zero-carbon, pricey fuel).
FIRMING_PRESETS = {"gas": None, "h2": GAS_H2}   # None → region default gas


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


