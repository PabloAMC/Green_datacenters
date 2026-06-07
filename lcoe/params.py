from __future__ import annotations

"""Dataclasses, technology/region presets, and system parameters."""
import json
from dataclasses import dataclass, fields, replace

MODEL_VERSION = "5.7.0"


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
    additions_growth_rate: float   # g0: growth of annual additions in year 1
    uncertainty_sigma: float = 0.15
    # Deployment-trajectory decay (v5.7). The additions growth rate decays
    # geometrically toward `additions_growth_floor` (g_i = floor+(g0-floor)·decay^(i-1))
    # so cumulative capacity follows an S-curve rather than compounding forever. 1.0 =
    # legacy constant growth (no decay), kept as the default so unset technologies are
    # unchanged. See `costs.cumulative_capacity`.
    additions_growth_decay: float = 1.0
    additions_growth_floor: float = 0.0
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
    additions_growth_decay: float = 1.0   # v5.7 S-curve decay (1.0 = legacy constant growth)
    additions_growth_floor: float = 0.0
    roundtrip_efficiency: float = 0.924   # DC-DC LFP; sqrt each way ≈ 96.1%
    om_frac_capex: float = 0.015
    # Degradation (LFP Wöhler curve)
    calendar_deg_per_yr: float = 0.020   # 2.0%/yr calendar fade
    cycle_deg_per_fec: float   = 5e-5    # capacity loss per FEC at 100% DoD (~4000 cycles → 80%, LFP)
    dod_exponent: float        = 0.60    # FEC_eff = DoD^β per actual cycle
    replace_threshold: float   = 0.80
    uncertainty_sigma: float = 0.12
    wacc: float = 0.07   # cost of capital (mid: more risk than RE, less than gas)
    # LDES-only fields (ignored for LFP / the core 3D path). Allow power/energy to be
    # decoupled and charge (e.g. electrolyser) to differ from discharge (e.g. turbine).
    discharge_capex_kw: float = None   # $/kW discharge kit; None → same as capex_kw
    charge_power_mw: float = 1.0       # charge power, MW per MW-load (e.g. electrolyser)
    discharge_power_mw: float = 1.0    # discharge power, MW per MW-load (e.g. turbine)


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
    # IEC power-curve speeds (m/s). v5.5: rated lowered 13→11 (cut-in 3.5→3.0) for a
    # modern low-specific-power turbine, so the simulated wind CF (~0.33 US / 0.28 EU)
    # matches the onshore-CF basis (0.30–0.55) of the Lazard wind LCOE the model imports.
    wind_v_ci: float = 3.0
    wind_v_rated: float = 11.0
    wind_v_cutout: float = 25.0
    # ── Spatial diversification (geographic portfolio) ─────────────────────────
    # n_sites>1 averages CF over `n_sites` separated sites that SHARE the regional
    # synoptic factor with pairwise correlation `site_synoptic_corr`. This preserves
    # the mean CF exactly (cost basis untouched) but softens the multi-day Dunkelflaute
    # tails — the largest directional bias of the single-site default (§12). n_sites=1
    # reduces exactly to the single-site generator, so it leaves the headline unchanged.
    n_sites: int = 1               # number of geographically-separated generation sites
    site_synoptic_corr: float = 0.7  # pairwise cross-site correlation of the Dunkelflaute factor
    # Datacenter load shape (mean-normalised to 1.0, so "per MW of average load").
    # "flat" = constant load (default, reduces dispatch exactly to the headline model);
    # "cooling" adds a temperature-driven PUE overhead so peak load > average and firm
    # gas backup (sized to peak) is modestly larger (§5.7).
    load_profile: str = "flat"
    # Firm gas-plant capacity sizing. The firm backup is sized to the peak hourly
    # residual; "mean" (default, unchanged headline) sizes to the mean-across-weather-years
    # annual peak, "p90" sizes to the 1-in-10 (P90) annual peak — a more conservative
    # "never goes dark" sizing that slightly raises gas capex. Energy/fuel unaffected.
    firm_gas_sizing: str = "mean"
    # Solar system performance ratio. 1.0 (default) keeps the simulated solar CF anchored
    # to the imported-LCOE cost basis (mean_irr/24; the v5.5 CF-consistency invariant);
    # <1.0 derates toward a bottom-up specific-yield CF (then re-level the solar LCOE).
    solar_performance_ratio: float = 1.0


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

# CF-CONSISTENCY (v5.5). `lcoe_today` is the Lazard LCOE+ v18 mid-range, which is
# levelised at Lazard's own capacity-factor assumptions (utility solar 20–30%,
# onshore wind 30–55%). An LCOE is capex+FOM spread over a *specific* CF, so the
# dispatch must simulate that same CF or the cost basis is internally inconsistent.
# The default resource + weather (after the v5.5 solar cloud-double-count fix and the
# modern wind power curve) reproduce US solar ≈0.23 / wind ≈0.33 and EU solar ≈0.16 /
# wind ≈0.28 — inside Lazard's CF bands — so the imported $/MWh and the simulated MWh
# now refer to the same plant. (Pre-v5.5 the dispatch ran at ~0.15/0.22, ~½ the CF the
# LCOE assumed, overstating overbuild and biasing high-RE cost upward.)
# DEPLOYMENT TRAJECTORY (v5.7). Additions grow off the 2025 base with a *decaying*
# growth rate (S-curve), not constant compounding. The central path is a best guess that
# keeps near-term additions robust — driven by developing-world electrification and the
# AI-datacenter clean-power buildout — but lets growth taper as mature markets saturate
# and grid-integration limits bite. It lands solar ≈15.6 TW, wind ≈4.2 TW, batteries
# ≈14.5 TWh cumulative by 2040 (vs the old constant-growth 38 TW / 7 TW / 45 TWh, which
# were ~3-4× mainstream IEA WEO / BNEF NEO and pulled deep-future RE cost down too fast).
# decay/floor are documented in §3; a low/central/high band is tabulated there too.
SOLAR = TechParams("Solar PV", lcoe_today=52.0, learning_rate=0.30,
                   cumulative_gw_2025=2900.0, annual_additions_gw=650.0,
                   additions_growth_rate=0.06, additions_growth_decay=0.85,
                   om_frac_lcoe=0.15)

WIND = TechParams("Onshore Wind", lcoe_today=50.0, learning_rate=0.17,
                  cumulative_gw_2025=1300.0, annual_additions_gw=167.0,
                  additions_growth_rate=0.03, additions_growth_decay=0.85,
                  om_frac_lcoe=0.25, life_yr=25)

# Energy component identical across regions (globally traded LFP cells); EU
# carries a ~25% power/BOS/EPC soft-cost premium (higher labour/permitting, no
# IRA-equivalent manufacturing credit). EU is therefore modestly MORE expensive
# than the US — the opposite of the v4 assumption.
BATTERY_US = BatteryParams("LFP Battery (US)", capex_kwh_today=180.0, capex_kw_today=140.0,
                           additions_growth_rate=0.08, additions_growth_decay=0.85)
BATTERY_EU = BatteryParams("LFP Battery (EU)", capex_kwh_today=180.0, capex_kw_today=175.0,
                           additions_growth_rate=0.08, additions_growth_decay=0.85)

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

# Green-hydrogen firming via PURCHASED H2 (opt-in alternative to gas, --firming h2).
# Buy green H2, burn it in an H2-capable turbine: **zero combustion carbon**, pricey
# fuel. Fuel price referenced to Lazard LCOH v4.0 (June 2024): unsubsidized green H2
# (PEM) ≈ $5.25/kg; at Lazard's 8.8 kg-H2/MMBtu that is ≈ $46/MMBtu. (The IRA 45V
# credit, up to $3/kg, and self-production cut this sharply — see LDES_H2 below for the
# self-produced-from-overcapacity alternative.) Turbine + H2 handling cost more than a
# gas peaker. Reuses the gas dispatch/cost path — "a gas plant with pricey zero-carbon
# fuel". Region-invariant; every figure adjustable.
GAS_H2 = GasParams(
    name="Green H2 firming (purchased)",
    gas_price_mmbtu=46.0,          # Lazard LCOH v4.0: $5.25/kg unsubsidized ÷ 8.8 kg/MMBtu
    ccgt_capex_kw=1300.0,          # H2-ready turbine + H2 handling (vs 1100 NG, Lazard v17)
    ocgt_capex_kw=600.0,           # (vs 500 NG)
    carbon_price_today=0.0,
    carbon_trajectory="linear",
    carbon_intensity_ccgt=0.0,     # green H2 → no combustion CO2
    carbon_intensity_ocgt=0.0,
)

# ── Long-duration energy storage (LDES) presets — for the --ldes overlay ────────
# A second storage tier the optimiser overlay can add ON TOP of LFP: LFP keeps doing
# the cheap diurnal cycling, LDES soaks up multi-day RE *overcapacity* (otherwise
# curtailed, hence ~free to charge) and discharges it during multi-day Dunkelflaute,
# competing with the gas/H2 backup. Reuses BatteryParams; the decoupled power rating
# is passed explicitly to the cost function. All figures adjustable; references below.
#
# For all: capex_kw_today = CHARGE kit ($/kW), discharge_capex_kw = DISCHARGE kit;
# charge_power_mw / discharge_power_mw = installed power per MW-load. Energy capex is
# per kWh of stored (mid-cycle) capacity. Augmentation/degradation as for LFP.
#
# (a) Iron-air (e.g. Form Energy): cheap energy, pricey power, low round-trip, ~100h.
#     Energy ≈ $20/kWh, symmetric power BOP ≈ $1,500/kW (Form Energy public targets /
#     NREL ATB 2024 LDES); RTE ≈ 50%.
LDES_IRONAIR = BatteryParams(
    name="LDES (iron-air)",
    capex_kwh_today=20.0, capex_kw_today=1500.0, discharge_capex_kw=1500.0,
    charge_power_mw=1.0, discharge_power_mw=1.0,
    # CONSERVATIVE learning (IRENA/IEA): ~15%/doubling on the power kit, modest
    # deployment → ≈30–35% decline by 2035 (energy held flat in the overlay).
    learning_rate=0.15, cumulative_gwh_2025=20.0, annual_additions_gwh=6.0,
    additions_growth_rate=0.12, roundtrip_efficiency=0.50,
    calendar_deg_per_yr=0.005, cycle_deg_per_fec=1e-5, om_frac_capex=0.02, wacc=0.08,
)
# (b) Self-produced green H2 (power→H2→power), the user's "make H2 on sunny days"
#     case: a SMALL electrolyser (charge_power 0.35 MW/MW-load) slowly fills storage
#     from surplus over many sunny hours; a FULL-SIZE H2 turbine (discharge 1.0)
#     covers the load during lulls. Round-trip ≈ 35% (electrolysis ~65% × turbine
#     ~55%). DEFAULT storage is above-ground tanks — NO geological cavern assumed —
#     at ≈ $20/kWh-H2 (DOE/NREL bulk compressed H2); electrolyser ≈ $1,200/kW and
#     H2 turbine ≈ $1,300/kW (Lazard LCOH v4.0 / NREL).
LDES_H2 = BatteryParams(
    name="LDES (self-produced H2, tanks)",
    capex_kwh_today=20.0,           # above-ground tank H2 storage, $/kWh-H2 (no cavern)
    capex_kw_today=1200.0,          # electrolyser (charge), $/kW  — Lazard LCOH/NREL
    discharge_capex_kw=1300.0,      # H2 turbine/fuel cell (discharge), $/kW
    charge_power_mw=0.35,           # nominal; the --ldes overlay sweeps electrolyser size
    discharge_power_mw=1.0,         # turbine sized to firm the load
    # CONSERVATIVE electrolyser learning: ~15%/doubling (IRENA Green Hydrogen Cost
    # Reduction 2020 cites 16–21%), modest deployment → ≈35% decline by 2035. NOT an
    # aggressive collapse. Turbine (mature) and tank storage are held flat in the overlay.
    learning_rate=0.15, cumulative_gwh_2025=20.0, annual_additions_gwh=6.0,
    additions_growth_rate=0.12, roundtrip_efficiency=0.35,
    calendar_deg_per_yr=0.010, cycle_deg_per_fec=1e-5, om_frac_capex=0.03, wacc=0.08,
)
# (c) Same, but with a SALT CAVERN for storage — SPECULATIVE / geology-dependent (most
#     sites have no cavern): energy ≈ $0.6/kWh-H2 (Lazard LCOH v4.0: $20/kg ÷ 33.3
#     kWh/kg). Provided only to bound the optimistic end; the tank case above is the
#     realistic default.
LDES_H2_CAVERN = BatteryParams(
    name="LDES (self-produced H2, salt cavern — speculative)",
    capex_kwh_today=0.6, capex_kw_today=1200.0, discharge_capex_kw=1300.0,
    charge_power_mw=0.35, discharge_power_mw=1.0,
    learning_rate=0.15, cumulative_gwh_2025=20.0, annual_additions_gwh=6.0,
    additions_growth_rate=0.12, roundtrip_efficiency=0.35,
    calendar_deg_per_yr=0.010, cycle_deg_per_fec=1e-5, om_frac_capex=0.03, wacc=0.08,
)
LDES_PRESETS = {"iron-air": LDES_IRONAIR, "h2": LDES_H2, "h2-cavern": LDES_H2_CAVERN}


SOLAR_EU = TechParams("Solar PV (EU)", lcoe_today=60.0, learning_rate=0.30,
                      cumulative_gw_2025=2900.0, annual_additions_gw=650.0,
                      additions_growth_rate=0.06, additions_growth_decay=0.85,
                      om_frac_lcoe=0.15)
WIND_EU  = TechParams("Onshore Wind (EU)", lcoe_today=48.0, learning_rate=0.17,
                      cumulative_gw_2025=1300.0, annual_additions_gw=167.0,
                      additions_growth_rate=0.03, additions_growth_decay=0.85,
                      om_frac_lcoe=0.25, life_yr=25)

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
# average-site resource used for the headline suite; "good" represents a modern,
# well-sited plant (higher irradiance, high-hub-height / low-specific-power turbines on
# a strong wind resource). With the v5.5 CF recalibration the *default* already sits in
# Lazard's CF bands — US solar ≈0.23 / wind ≈0.33, EU solar ≈0.16 / wind ≈0.28 — so it
# is no longer artificially pessimistic; "good" pushes toward the top of those bands
# (US ≈0.26 / 0.45, EU ≈0.19 / 0.41). Used by `run_resource_sensitivity` / --resource.
# `low` is a poor-but-plausible site in the region (cloudier / lower wind — e.g. the US
# Ohio Valley/Southeast, or a weak northern-European site); `good` a modern well-sited
# plant. The pair (low, good) brackets the geographic/siting range used for the fig1
# resource band (the `default` central line sits between them).
RESOURCE_PRESETS = {
    "us": {"low": (4.5, 6.5), "default": (5.5, 7.5), "good": (6.8, 9.0)},
    "eu": {"low": (3.2, 6.0), "default": (3.8, 7.0), "good": (4.6, 8.5)},
}


def resource_band_for(region_key: str):
    """(poor-site, good-site) resource pairs for the fig1 siting band, or None."""
    p = RESOURCE_PRESETS.get(region_key)
    if not p or "low" not in p or "good" not in p:
        return None
    return [p["low"], p["good"]]


def _sys_with(sys: SystemParams, **overrides) -> SystemParams:
    """Copy a SystemParams with selected fields overridden (e.g. coarser grid for sweeps)."""
    return SystemParams(**{**sys.__dict__, **overrides})


# ── Config-driven custom sites (CLI --site) ─────────────────────────────────────
# A site is described by a small JSON file that *inherits* a built-in region's tech,
# battery, SMR and grid-PPA defaults (`based_on`: "us" | "eu") and overrides only the
# things that actually vary by location: the resource (mean_irr, mean_wind_ms), the gas
# market (any GasParams field, e.g. gas_price_mmbtu, carbon_price_today), and any
# SystemParams field (e.g. wind_solar_corr, n_sites, site_synoptic_corr, syn_persistence,
# the optimiser bounds). This means a new geography is a *data* file, not a code change.
# Example (sites/example_texas.json):
#   { "label": "Texas (ERCOT)", "based_on": "us", "mean_irr": 5.8, "mean_wind_ms": 8.5,
#     "gas_price_mmbtu": 3.2, "n_sites": 3, "site_synoptic_corr": 0.6 }
_SITE_META_KEYS = {"label", "based_on", "mean_irr", "mean_wind_ms"}


def load_site_config(path: str) -> dict:
    """Load a JSON site file → a REGIONS-style bundle consumable by `run_region_cfg`.

    Unknown keys raise (so typos surface immediately rather than being silently
    ignored). Any `GasParams` / `SystemParams` field name is a valid override key.
    """
    with open(path) as fh:
        spec = json.load(fh)
    if not isinstance(spec, dict):
        raise ValueError(f"site config {path!r} must be a JSON object")

    base_key = spec.get("based_on", "us")
    if base_key not in REGIONS:
        raise ValueError(f"site 'based_on' must be one of {list(REGIONS)}; got {base_key!r}")
    cfg = dict(REGIONS[base_key])   # shallow copy of the region bundle

    gas_fields = {f.name for f in fields(GasParams)}
    sys_fields = {f.name for f in fields(SystemParams)}
    unknown = set(spec) - _SITE_META_KEYS - gas_fields - sys_fields
    if unknown:
        raise ValueError(f"unknown site config keys {sorted(unknown)}; "
                         f"valid keys are {sorted(_SITE_META_KEYS | gas_fields | sys_fields)}")

    cfg["label"] = spec.get("label", cfg["label"])
    if "mean_irr" in spec:
        cfg["mean_irr"] = float(spec["mean_irr"])
    if "mean_wind_ms" in spec:
        cfg["mean_wind_ms"] = float(spec["mean_wind_ms"])

    gas_ov = {k: spec[k] for k in spec if k in gas_fields}
    if gas_ov:
        cfg["gas"] = replace(cfg["gas"], **gas_ov)
    sys_ov = {k: spec[k] for k in spec if k in sys_fields}
    if sys_ov:
        cfg["sys"] = _sys_with(cfg["sys"], **sys_ov)
    return cfg


