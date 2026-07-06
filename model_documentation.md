# Off-grid Datacenter LCOE Model — Technical Documentation

**Model version:** v5.7  
**Code file:** `lcoe/` package (entry point `datacenter_lcoe.py`)  
**Last verified:** June 2026  
**All numerical values cross-checked against model output (`output/*_results.json`)**

> **New here? Read this first.** This is the *technical reference* — every equation,
> parameter, and data source behind the model. You do **not** need to read it top to
> bottom or follow every derivation. For the plain-language story and headline numbers,
> start with [`README.md`](README.md). Here, §1 is the overview, and each of the harder
> sections below opens with an *Intuition* line that states the idea in words before any
> math. Power-systems or energy-finance background helps but isn't required: the one
> load-bearing concept is that a generator's cost (its **LCOE**) is its capital plus
> running cost spread over the energy it actually produces — so *how much it produces*
> (its **capacity factor**) and *what it costs* are two sides of the same coin.

### How the model works, end to end

The model answers one question: *what is the cheapest way to run an always-on, off-grid
datacenter on mostly-renewable power, and in what year does that beat simply burning
gas?* It gets there in five steps, each a section below:

1. **Project technology costs forward** (§3) — solar, wind and batteries get cheaper as
   the world builds more of them (Wright's Law learning curves), giving a cost for every
   year from 2025 to 2040.
2. **Generate a year of weather** (§4) — synthetic but realistic hour-by-hour solar and
   wind output, including the multi-day wind-and-sun lulls ("Dunkelflaute") that
   high-renewable systems live or die by. (Real ERA5/NSRDB weather can be dropped in
   unchanged.)
3. **Simulate dispatch** (§5) — for a given build (how much solar, wind, and battery),
   step through all 8,760 hours of the year: renewables serve the load first, the battery
   fills the gaps, and gas covers whatever is left. This yields the renewable share and
   the gas burned.
4. **Cost the build and find the cheapest one** (§6–§8) — price each component, then
   search for the least-cost build that still hits the target renewable fraction, using a
   fast optimiser over a precomputed grid of builds.
5. **Quantify uncertainty** (§9) — over weather years and cost assumptions, plus the
   optional sensitivity analyses (flexibility, resource quality, tornado; §7.5–7.6, §9).

The output is a delivered cost in dollars per megawatt-hour ($/MWh) for each year and
each target renewable fraction — and the year that cost crosses below the gas baseline.

---

## Table of Contents

1. [Overview and scope](#1-overview-and-scope)
   — *then [Version history & rationale](#version-history--rationale) (skippable on a first read)*
2. [Decision variables and optimisation](#2-decision-variables-and-optimisation)
3. [Cost learning curves — Wright's Law](#3-cost-learning-curves--wrights-law)
4. [Weather generation](#4-weather-generation)
5. [Chronological dispatch](#5-chronological-dispatch)
6. [Battery degradation, augmentation, and cost](#6-battery-degradation-augmentation-and-cost)
7. [Gas backup cost and carbon trajectory](#7-gas-backup-cost-and-carbon-trajectory)
8. [System LCOE and 3D optimisation](#8-system-lcoe-and-3d-optimisation)
9. [Monte Carlo uncertainty](#9-monte-carlo-uncertainty)
10. [Parameter tables](#10-parameter-tables)
11. [Key results](#11-key-results)
12. [Known limitations](#12-known-limitations)
13. [References](#13-references)

---

## 1. Overview and Scope

This model computes the least-cost combination of solar PV, onshore wind, battery storage, and gas backup to power an off-grid datacenter at a user-specified renewable energy fraction. The system is fully off-grid — no grid import is available.

**What the model does:**
- Optimises over three continuous variables: solar overbuild $C_{\text{sol}}$, wind overbuild $C_{\text{win}}$, and battery storage duration $B$ — simultaneously for the first time in v4.
- Projects cost trajectories from 2025 to 2040 using Wright's Law learning curves.
- Quantifies uncertainty via Monte Carlo over both stochastic weather and capex parameters.
- Accounts for battery degradation via capacity augmentation (throughput-cycled), holding usable capacity at nameplate.
- Defaults to a **firm (always-on)** datacenter with gas backup sized to 100% of load, and optionally models interruptible workloads that shed load only when the value of lost compute is below the gas variable cost (§5.5).
- Covers two regions (US, EU) with region-specific resource, gas price/carbon, and battery soft-costs.
- Uses dynamic backup generator capacity sizing (firm → 100% of load).

**Normalisation:** All quantities are per MW of constant datacenter load. Delivered costs are in real 2025 USD per MWh of load served.

---

## Version history & rationale

*Most recent first. This is a changelog of what each model version changed and why; safe to skip on a first read — the methodology it refers to is documented in the sections below.*

**v5.9 — weather-statistics and optimizer hardening (closes the v5.8 audit findings).**
Implements the accuracy items the v5.8 full-machinery audit surfaced (§12), all empirically
validated. (a) **Cloud persistence moved into z-space** (§4.2–4.3): the short-scale cloud
AR(1) now lives in the *latent* normal (a stationary unit-variance residual chain) instead of
being filtered after the Beta transform. This makes the Beta(3,1.5) cloud marginal hold
**exactly** (deep-overcast days back to the intended 5.2% of days, from the ~1.3% the old
filter left) and restores the realized daily wind–solar correlation to the configured ρ
(EU −0.35 now realizes ≈−0.28 vs the old ≈−0.18; US 0 realizes ≈0.00 vs the old +0.07 bias —
the residual EU gap is the inherent Pearson attenuation of monotone transforms). Net
direction: EU gains back the calibrated wind-when-overcast complementarity (cheaper high-RE);
solar-heavy builds face honestly more frequent 1-day droughts (dearer). (b) **Exact solar-CF
anchor** (§4.1): the clear-sky renormalisation iterates to tolerance, so the effective CF
equals mean_irr/24 even where summer-noon clipping binds (US was ~1% short). (c) **Mid-summer
seam** (§4.6): the daily latent series are rolled 182 days so the AR-chain seam falls in early
July — the winter Dunkelflaute season is one contiguous stretch and Dec→Jan lulls are no
longer severed at the trace boundary. (d) **Multi-site local-weather sharing** (§4.7,
`site_local_corr`, default 0.4): cross-site correlation now includes the local cloud/wind
residuals, not just the synoptic factor (pre-v5.9 the effective cross-site daily correlation
was only λ²·c ≈ 0.18, overstating diversification; 0 recovers the old behaviour).
(e) **Exact-dispatch confirmation** (§2, §8.4): the final reported optimum (cost, RE fraction,
breakdown, FEC) is re-evaluated on **true dispatch** over the stored weather years
(`ChronologicalSimulator.exact_point`) — the trilinear surface is now used only *inside* the
optimisation loop, so the one-sided convexity bias (≈$1–3/MWh between nodes) no longer touches
reported numbers. (f) **RE-feasibility tolerance tightened 0.5% → 0.2%** (`RE_TOL`), capping
the admissible one-sided cost understatement at ≈$2/MWh (safe because exact RE ≥ interpolated
RE). All headline outputs regenerated; four new regression tests lock the invariants
(realized-ρ, Beta marginal, CF anchor, exact-point ≡ surface at grid nodes).

*v5.9.1 — the four residual audit items.* (g) **P90 coherence** (opt-in `use_p90` paths):
the shed branch gains true P90 surfaces (`drop_p90`, `gas_peak_p90`), and the firm branch's
P90 gas energy is now the percentile of the per-year **gas+drop sum** (`gas_firm_p90`) with
P90 capacity sizing implied — pre-v5.9.1 the gas energy was P90 but the shed add-back and
capacity peak were means. (h) The **fig1 H₂ optimiser multi-starts every year** (the two cold
starts now run alongside the warm start, closing the unmonitored warm-start-drift gap).
(i) `run_ldes_overlay`'s locally-duplicated LDES annualisation now **delegates to
`costs.ldes_annual_cost`** (the last duplicated cost formula; the B=0 buy-all-H₂ candidate
keeps its explicit power-only form since the turbine is installed regardless). (j) The
**H₂/LDES analysis paths use the multi-site portfolio seam** (`n_sites=1` default reduces
exactly to single-site), removing the last fidelity asymmetry vs the main dispatch path.

**v5.8 — June-2026 input recalibration (gas capex shock, battery reality, wind repricing,
real-WACC consistency).** A pure *input* recalibration — no methodology changes — fixing the
places where the world moved away from the mid-2025 calibration, all verified against current
sources. The flagged staleness was strongly *correlated*: gas firming was understated while
batteries were overstated, both biases working against the RE+storage build. (a) **Gas capex
2×** (§7): the post-2024 turbine shortage has new CCGT orders at \$2,000–2,500/kW with ~2030
delivery (GridLab 2025; EIA AEO2025) vs the Lazard-v17-era \$1,100; defaults move to the
order-book values CCGT \$2,000 / OCGT \$1,000 (H₂-ready \$2,200/\$1,100) — a datacenter
deciding *today* buys at today's prices. US pure-gas reference rises \$46→\$58/MWh, EU
\$114→\$122 (2025). (b) **Battery recalibration** (§3, §6): 4h installed cost drops to ≈\$130/kWh
US / ≈\$120 EU (energy \$90/kWh + power US \$160 / EU \$120/kW), anchored to BNEF's 2025 survey
(US \$108 / EU \$101 turnkey + soft-cost margin) — and the regional premium **flips to the US**
(Section 301 tariffs outweigh EU soft costs), reversing v5's EU-premium assumption. The
Wright's-Law driver is restated on the honest basis: **global Li-ion production (EV +
stationary)**, ≈5,000 GWh cumulative / ≈1,600 GWh added 2025 (the old 1,800/600 matched neither
stationary-only nor all-Li-ion); the trajectory shape is nearly unchanged (additions/base ratio
0.32 vs 0.33). (c) **Onshore wind repriced UP**: Lazard LCOE+ 2025 moved onshore wind to
\$37–86/MWh (mid ≈\$61) on supply-chain/tariff inflation — `lcoe_today` 50→61 US, 48→56 EU.
(d) **Solar learning rate 0.30→0.25**, the defensible central of module (~20–24%, ITRPV) and
system-level estimates; 2040 solar \$21.9→\$25.9/MWh. (e) **Real-WACC consistency**:
`LEGACY_WACC` (the basis the imported LCOEs are quoted at) 0.07→0.055 — the model works in
real 2025\$, and Lazard levelises at ≈7.7% *nominal* ≈ 5.5% *real*; the old 0.07 treated
nominal as real, so `rewacc_lcoe` granted RE a ~12% discount that was inflation counted twice.
At the default solar/wind WACC (5.5% real) the re-WACC is now the identity (delivered solar
\$45.5→\$52, wind \$43.6→\$61). (f) **Combustion-only carbon intensity** (0.41→0.345 CCGT,
0.60→0.50 OCGT, tCO₂/MWh): the model charges the ETS-style carbon price, whose scope is stack
CO₂; the old values implicitly priced some upstream methane at the ETS rate. (g) Smaller:
grid-PPA energy \$45→\$55/MWh (LevelTen 2025), SMR FOAK \$120→\$150 US / \$140→\$175 EU (recent
FOAK datapoints). Net direction: dearer gas + dearer delivered RE generation + cheaper storage;
all §11 tables and figures regenerated. Also in v5.8: a **full-machinery audit** (weather
statistics, dispatch/MC, optimisation/cost composition — see §12 for the verified-clean list
and the remaining accuracy items with bias directions), which fixed three opt-in-path bugs:
the gas cost layer's backup-capacity clip at 1.0× load (voided the documented cooling-profile
upsizing), `opt_re` omitting the firm shed add-back (overstated achieved RE for interruptible
workloads), and the LDES/H₂ analysis paths ignoring `solar_performance_ratio`. None affects
the headline firm/flat results.

**v5.7 — defensible deployment trajectory (S-curve learning), H₂-line fidelity, gas-baseline
transparency, and observability.** The headline-moving change is the **deployment trajectory**
(§3): through v5.6 cumulative capacity compounded annual additions at a *constant* growth rate,
which ran solar to ~38 TW by 2040 (~3–4× mainstream IEA WEO / BNEF NEO) and so made the
learning-curve cost decline too fast at the long end. v5.7 lets the additions growth *decay*
(an S-curve: `additions_growth_decay`), with a best-guess central path that keeps near-term
additions robust — developing-world electrification + the AI-datacenter clean-power buildout —
but tapers as mature markets saturate. Central 2040 cumulatives land **solar ≈15.6 TW, wind
≈4.2 TW, batteries ≈14.5 TWh**; the year-0 (2025) costs and all capacity factors are unchanged,
so every **2025 headline number and optimal build is identical** — only *future* unit costs
rise. Net effect: deep-future RE costs lift ~25–40% at 2040 (e.g. solar generation LCOE 2040
$13.7→$21.9), **US high-RE no longer crosses cheap flat gas within the horizon** (the
moderate-RE mid-2030s crossings of v5.5/5.6 disappear — they return only against a stressed-gas
baseline), and **EU 90% parity moves ~2029→~2030**. The directional conclusions are unchanged
(and the US cheap-gas moat *strengthens*). Also: (a) the **fig1 gas-free H₂ line** is now
evaluated on the full weather ensemble (`sys.n_mc_weather`, default 50) while the build is
optimised on a 20-year subsample, fixing a fidelity asymmetry where it ran on just 6 synthetic
years; (b) a **gas-stress reference line** (`gas_stress_mult`, default ×1.6 in the suite) and a
**carbon-introduction lever** in the tornado make the asymmetric gas baseline (US flat $4/$0
carbon) explicit (§7); (c) the duplicated gas-free-H₂ cost formula is unified into one shared
function (`costs.h2_system_cost_split`, used by both the fig1/fig6 trajectory and the joint
co-optimisation), locked by a regression test; (d) optional **P90 firm gas-sizing**
(`firm_gas_sizing="p90"`), an explicit **solar `performance_ratio`** knob, and a **continuity
tie-break diagnostic** (§8.4) round out the observability/robustness additions. All opt-in
extras default off, so the headline = the recalibrated central.

**v5.6 — geographic diversification, real-weather pipeline, config-driven sites
(all opt-in; headline numbers unchanged).** Three usability/fidelity additions that the
default suite does **not** trigger, so all v5.5 results reproduce bit-for-bit. (a)
**Spatial diversification** (§4.7): `n_sites`>1 portfolio-averages over geographically
separated sites that share the regional Dunkelflaute factor with pairwise correlation
`site_synoptic_corr`, preserving the mean CF exactly while softening the multi-day lulls
that dominate high-RE cost — a large effect (~40% off EU 90%-RE delivered cost for 3–5
sites). `n_sites=1` (default) reduces byte-for-byte to the single-site generator. This
closes the §12 "largest *directional* bias". (b) **Real-weather pipeline**: the v5.5
reanalysis seam is now wired end to end — `--weather PATH.npz` drives the dispatch with
measured ERA5/NSRDB years, and `tools/ingest_weather.py` builds that file from provider
data (documented ERA5/NSRDB→CF recipe; `demo` mode for a try-out). (c) **Config-driven
sites**: `--site PATH.json` (`load_site_config`) describes a new geography as a data file
that inherits a built-in region's defaults and overrides only the location-specific knobs
— so siting a new location is no longer a code change.

Also in v5.6, reproducibility & fidelity tooling (none of it changes the headline): a
`pyproject.toml` (pip-installable, `datacenter-lcoe` entry point) and a pinned
`requirements-lock.txt` + `Makefile`; **run provenance** in every JSON export (model
version, git commit, seed, grid, and a deterministic config hash); a **doc-drift guard**
(`tools/check_doc_tables.py`) plus GitHub Actions CI that fail if the §11 tables drift from
`output/`; an **external-validation** test anchoring the cost inputs to published Lazard/EIA
bands; a **non-flat load profile** (`load_profile`, §5.7; default `flat` = identical) where
`cooling` adds a temperature-driven PUE overhead so firm gas sizes to peak, not average; and
a **synoptic calibration tool** (`tools/calibrate_synoptic.py`) that fits φ/λ/ρ/`site_synoptic_corr`
from real weather, converting the §12 "calibrated, not fitted" caveat into a measured input;
and a **fig1 geographic/siting band** (§9.3) that shades every trajectory — including the
gas-free H₂ line, which previously had none — over a poor↔good site for the region.

**v5.5 — capacity-factor ↔ cost-basis consistency, and a reanalysis seam.** The model
imports Lazard v18 generation LCOEs, which are levelised at Lazard's own capacity factors
(utility solar 0.20–0.30, onshore wind 0.30–0.55). An LCOE is capex+FOM spread over a
*specific* CF, so the dispatch must simulate that same CF — but through v5.4 it ran at
≈0.15 solar / 0.22 wind, ~half the CF the cost assumed, so the imported \$/MWh and the
simulated MWh referred to different plants and high-RE overbuild was overstated. v5.5 fixes
both halves: a **solar cloud double-count** (the Beta cloud derate was applied on top of an
already cloud-inclusive $\bar I$; effective CF 0.153→**0.227** US, 0.105→**0.158** EU; §4.1–4.2)
and a **high-specific-power wind curve** (rated 13→11 m/s, cut-in 3.5→3.0; CF 0.22→**0.33**
US, 0.18→**0.29** EU; §4.4–4.5). The US CFs and EU wind now sit inside Lazard's CF bands;
EU solar (0.158) sits just below the US band, consistent with its weaker irradiance and its
own EU-specific LCOE basis (§4.2). Net: high-RE
delivered LCOE falls ~20–30% and parity moves earlier — **EU 90% RE ≈ 2029;
US 70–80% ≈ 2036, 85% ≈ 2040** (high-RE US still does not beat cheap gas in the horizon). A
**reanalysis hook** (`ChronologicalSimulator(weather_years=…)` / `weather.load_weather_traces`)
lets real ERA5/NSRDB years drive the dispatch unchanged. Tables below are regenerated
against this baseline.

**v5.4 — battery augmentation + throughput cycle counting; no optimiser hysteresis.**
The battery cost moves from lumpy full-system replacement to **capacity augmentation**
(top up the faded energy/cell capacity each year — standard practice and cheaper; §6),
and degradation is driven by **throughput equivalent-full-cycles** from dispatch rather
than the old 2σ(SoC) proxy. The optimiser's path-regularisation penalty was also removed
(it caused year-to-year hysteresis in the reported mix; §8.4); storage is ~30–35% cheaper.

**v5.3 — per-technology cost of capital.** A single flat WACC is replaced by
differentiated, technology-specific financing (§8.3): solar/wind **5.5%** (low-risk,
long-life infrastructure, 30 / 25-yr lives), LFP battery **7%**, gas **9%** (merchant +
policy/stranding risk, 25-yr life). The exogenous generation LCOEs are re-annualised at
the new WACC via `rewacc_lcoe`. Net effect: cheaper RE and dearer gas, so **EU parity
moves a year or two earlier** (90% RE ≈ 2034) while the **US cheap-gas moat still holds**
(no parity within the horizon, though the gap narrows). All headline tables below are
regenerated against this baseline; the legacy flat-7% behaviour is recoverable by setting
each `wacc` to 0.07.

**Headline model (v5.2): FIRM, always-on datacenter with capped opex.** The default
workload never shuts down — gas turbines are sized to cover **100% of load** when
sun/wind are absent and batteries are exhausted, so the worst case is bounded by a
known gas running cost. Solar + wind + battery are then an *incremental* investment
that displaces gas fuel (and carbon) where it pays. No load is ever shed; no
"value of lost compute" assumption is needed.

**Demand flexibility (optional) = ECONOMIC shedding.** A workload may instead be
declared interruptible (`interruptible_fraction`) with a value of lost compute
(`shed_penalty_mwh`). The model sheds a deficit hour **only when that compute is
worth less than the gas variable cost of serving it** — so premium/AI compute
(worth ≫ gas fuel+carbon) never sheds and collapses to the firm case, while cheap
spot/research compute sheds the expensive hours. This replaces the v5/v5.1
deferral-with-recovery model, which implicitly (and unrealistically) assumed idle
over-provisioned GPUs available to "catch up" during surplus.

**v5 rigour fixes (retained):** (a) honest flexibility accounting — no free
load-shedding counted as renewable; (b) consistent battery cost basis (globally-traded
cells → region-invariant energy $/kWh; only power/BOS $/kW carries a regional premium),
removing v4's EU-2.75×-cheaper artefact; (c) a persistent synoptic factor + mean-reverting
hourly wind producing correlated multi-day "Dunkelflaute" (marginals/CFs preserved).
v5.1 right-sized the optimiser grid (§8.4) and added a boundary-binding guard.

---

## 2. Decision Variables and Optimisation

### Decision variables

| Symbol | Units | Meaning |
|--------|-------|---------|
| $C_{\text{sol}}$ | — | Solar generation overbuild (installed MW ÷ load MW) |
| $C_{\text{win}}$ | — | Wind generation overbuild (installed MW ÷ load MW) |
| $B$ | hours | Battery storage duration (energy capacity MWh ÷ load MW) |

Battery power capacity follows from duration: $P_{\text{batt}} = \min(1,\; 4/B)$ MW per MW-load (see §5.4).

Gas backup capacity is not a decision variable — it is sized dynamically to cover the peak residual hourly deficit (see §7).

### Optimisation objective

$$\min_{C_{\text{sol}},\, C_{\text{win}},\, B} \;\; \text{LCOE}_{\text{system}}$$

$$\text{subject to} \quad f_{\text{RE}}(C_{\text{sol}}, C_{\text{win}}, B) \geq R$$

where $R$ is the user-specified minimum renewable energy fraction and $f_{\text{RE}} = 1 - f_{\text{gas}}$ is the energy-based renewable fraction from dispatch.

### Solution method

*Intuition: re-running an 8,760-hour dispatch for every candidate build would be far too
slow to search over, so we run it **once** on a dense grid of builds, cache the result, and
then let a standard hill-climbing optimiser interpolate within that cached surface to find
the cheapest build that still meets the renewable target. "Multi-start" just means we try
several starting points so a single bad start can't trap us in a local minimum.*

The 3D optimisation is solved by multi-start Nelder-Mead with seven fixed starting points (plus the previous year's optimum as a warm start, when available). The objective function evaluates via trilinear interpolation into a precomputed $21^3 = 9{,}261$-scenario dispatch surface (v5.1; §8.4), making each evaluation near-instantaneous. A **continuity tie-break** (v5.7) then adopts the previous year's build only if it is feasible at the same tolerance *and* within **1%** of the year's fresh optimum — suppressing cosmetic year-to-year mix flips without ever accepting a materially worse build (the fired count and cost deltas are printed as a `[diag]` line). Finally (v5.9) the chosen build is **re-evaluated on exact dispatch** over the stored weather years (`exact_point` — no interpolation), and the reported cost, RE fraction, breakdown and battery cycling all come from that exact pass; the interpolated surface is used only *inside* the optimisation loop. The served-RE feasibility tolerance is **0.2% absolute** (`RE_TOL`, tightened from 0.5% in v5.9).

An exterior penalty method enforces the RE constraint:

$$\text{penalty} = 2000 \cdot v + 10{,}000 \cdot v^2, \qquad v = \max(0,\; R - f_{\text{RE}})$$

The quadratic term prevents the optimiser from accepting small violations cheaply. After Nelder-Mead converges, a post-optimisation 1D scan over $B$ at the optimal $(C_{\text{sol}}, C_{\text{win}})$ repairs any residual feasibility violations.

### Grid bounds

| Region | $C_{\text{sol,max}}$ | $C_{\text{win,max}}$ | $B_{\text{max}}$ |
|--------|---------------------|---------------------|-----------------|
| US | 18× | 18× | 60h |
| EU | 22× | 20× | 60h |

Bounds were right-sized in v5.1 (§8.4) so the 21-node grid resolves the real optima
instead of wasting resolution; a boundary-binding guard warns if any optimum reaches a max.
(Values match `SystemParams` / `SystemParams(EU)` in the code; the firm, no-shed
high-RE optimum is wind-heavy in the EU — the 90% RE build runs ~5× wind, and the unreachable
≥95% region would demand ≈15× — so the EU wind bound is set to 20× to keep the feasible
optima interior. Even at that bound the firm battery-only system tops out at ≈94% RE, §11.)

---

## 3. Cost Learning Curves — Wright's Law

*Intuition: technologies that are manufactured (panels, turbines, battery cells) get reliably
cheaper the more of them the world has ever built — historically by a roughly fixed percentage
for every **doubling** of cumulative production. That empirical regularity is Wright's Law. So to
get a cost for, say, 2035, we project how much total capacity will be installed by then, count
the doublings since today, and apply the per-doubling cost decline. This is what makes the future
cheaper than today in the model — and why solar (a fast-growing, fast-learning technology) falls
furthest.*

### Cumulative capacity trajectory (S-curve, v5.7)

$$Q_t = Q_0 + \sum_{i=1}^{t} \Delta Q_0 \cdot \prod_{j=1}^{i}\big(1 + g_j\big),
\qquad g_j = g_{\text{floor}} + (g_0 - g_{\text{floor}})\,\delta^{\,j-1}$$

where $Q_0$ is cumulative installed capacity in 2025, $\Delta Q_0$ is 2025 annual additions,
$g_0$ is the year-1 growth rate of annual additions, and $\delta=$ `additions_growth_decay`
shrinks that growth toward $g_{\text{floor}}$ each year. **Why the decay (v5.7):** real
technology adoption is an S-curve, not perpetual compounding — the largest markets saturate and
grid-integration limits bite. A *constant* $g$ (the pre-v5.7 model) ran solar to ≈38 TW
cumulative by 2040, ~3–4× mainstream IEA WEO / BNEF NEO projections, which made the
learning-curve cost decline implausibly fast at the long end. With $\delta=1$ the product
collapses to the legacy $\Delta Q_0(1+g_0)^i$, so the formula is backward-compatible; the
shipped technologies set $\delta=0.85$.

**Best-guess central deployment (and its band).** The central path keeps near-term additions
robust — developing-world electrification plus the AI-datacenter clean-power buildout — while
letting growth taper. It lands the cumulatives (and the resulting 2040 doublings vs 2025) at:

| Tech | $g_0$ | $\delta$ | 2030 | 2035 | **2040 central** | low ↔ high (2040) | 2040 doublings | 2040 LCOE |
|------|-------|----------|------|------|------------------|-------------------|----------------|-----------|
| Solar | 6%/yr | 0.85 | 6.7 TW | 11.0 TW | **15.6 TW** | ~11 ↔ ~22 TW | 2.42 | $25.9/MWh |
| Wind  | 3%/yr | 0.85 | — | — | **4.2 TW**  | ~3 ↔ ~6 TW | 1.68 | $44.6/MWh |
| Battery (Li-ion prod.) | 8%/yr | 0.85 | — | — | **39 TWh** | ~36 ↔ ~45 TWh | 2.96 | (×0.54 $/kWh) |

*(Battery basis, v5.8: the driver is global Li-ion **production**, EV + stationary — see the
battery cost-basis note below — so the cumulatives are production TWh, not stationary BESS.)*

The *low* and *high* columns bracket a slower-saturation / AI-supercharged range (vary $g_0$ by
about ∓2 pts / +4 pts); they bound the learning-curve uncertainty. The fig1 *resource/siting*
band (§9.3) and the tornado's **solar-learning** lever (§9) carry the cost-axis sensitivity that
this range implies — a separate fig1 deployment band was deliberately not added (it would
clutter the headline figure).

### Wright's Law

$$\text{LCOE}(t) = \text{LCOE}_0 \cdot \left(\frac{Q_t}{Q_0}\right)^{\log_2(1-\text{LR})}$$

**Verification (v5.8 calibration):** For solar, $Q_{2040}/Q_{2025} = 15{,}557/2{,}900 = 5.36$ — corresponding to $\log_2(5.36) = 2.42$ doublings. With LR = 0.25, cost ratio = $(1-0.25)^{2.42} = 0.498$. So $\text{LCOE}_{2040} = 52 \times 0.498 = \$25.9$/MWh. ✓ (model output: $25.9/MWh)

### Solar LCOE trajectory (US, verified values)

| Year | Cum. capacity (GW) | LCOE ($/MWh) |
|------|--------------------|--------------|
| 2025 | 2,900 | 52.0 |
| 2028 | 5,069 | 41.2 |
| 2030 | 6,660 | 36.8 |
| 2035 | 10,972 | 29.9 |
| 2040 | 15,557 | 25.9 |

*These are the raw Wright's-Law learning-curve LCOEs, quoted at `LEGACY_WACC` — since v5.8 the
**real**-terms equivalent (5.5%) of Lazard's ≈7.7% *nominal* quote basis (the model works in
constant 2025 dollars). The default solar/wind WACC (5.5% real) equals that basis, so
`rewacc_lcoe` (§8.3) is the **identity** at defaults and the delivered generation LCOE equals
the raw curve (solar 2025 = $52 delivered). The pre-v5.8 convention (LEGACY_WACC = 0.07,
treating Lazard's nominal rate as real) granted RE a spurious ≈12% "cheaper capital" discount
— inflation counted twice. The `wacc` lever still re-prices genuinely cheaper
(hyperscaler-backed) or dearer capital.*

### Technology learning parameters

v5.7 deployment growth is $g_0$ (year-1 additions growth) with decay $\delta=0.85$ toward a
floor of 0 (S-curve; see the trajectory section above). The pre-v5.7 constant rates were solar
15% / wind 10% / battery 18% with no decay.

| Technology | LCOE₀ ($/MWh) | LR | Q₀ | ΔQ₀ | g₀ · δ |
|------------|----------------|-----|-----|------|--------|
| Solar PV (US) | 52 | 25% | 2,900 GW | 650 GW/yr | 6%/yr · 0.85 |
| Solar PV (EU) | 60 | 25% | 2,900 GW | 650 GW/yr | 6%/yr · 0.85 |
| Onshore Wind (US) | 61 | 17% | 1,300 GW | 167 GW/yr | 3%/yr · 0.85 |
| Onshore Wind (EU) | 56 | 17% | 1,300 GW | 167 GW/yr | 3%/yr · 0.85 |
| LFP Battery (energy, US) | 90 $/kWh | 19% | 5,000 GWh | 1,600 GWh/yr | 8%/yr · 0.85 |
| LFP Battery (power, US) | 160 $/kW | 19% | (same) | (same) | 8%/yr · 0.85 |
| LFP Battery (energy, EU) | 90 $/kWh | 19% | 5,000 GWh | 1,600 GWh/yr | 8%/yr · 0.85 |
| LFP Battery (power, EU) | 120 $/kW | 19% | (same) | (same) | 8%/yr · 0.85 |

The solar LR (v5.8: 0.30 → **0.25**) is the defensible central of module-price learning
(~20–24%, ITRPV) and system-level LCOE estimates (20–25%; BOS/soft costs learn slower than
modules); 0.30 was the aggressive end (Way et al. 2022, hardware-only). Wind LCOE₀ (v5.8:
50 → **61** US, 48 → **56** EU) is the Lazard LCOE+ 2025 onshore mid (range $37–86/MWh —
US wind *rose* on supply-chain/tariff inflation; the 2024 edition's $27–73 is stale). The
wind Q₀/ΔQ₀ are the **total** wind fleet (on- + offshore, shared supply chain): 1,299 GW
end-2025, 165 GW added 2025 (GWEC) — matched by 1,300/167.

**Battery cost basis (v5, recalibrated v5.8).** Installed cost is split into an **energy**
component ($/kWh, scales with MWh) and a **power/BOS/EPC** component ($/kW, scales with MW).
LFP cells are a globally traded commodity, so the energy component is
**region-invariant** ($90/kWh); only the power/BOS component carries a regional
soft-cost premium — which since v5.8 sits on the **US** ($160/kW vs EU $120/kW): BNEF's 2025
cost survey has US 4h turnkey ≈ $108/kWh vs EU ≈ $101 (Section 301 tariffs on Chinese cells
outweigh EU labour/permitting), reversing the v5–v5.7 EU-premium assumption. A 4h system
therefore costs $4{\times}90 + 160 = \$520$/kW-load (US) ≈ $130/kWh installed, vs
$480/kW-load (EU) ≈ $120/kWh — both a developer soft-cost/interconnection margin above BNEF
turnkey. **Learning driver (v5.8):** the Wright's-Law driver is **global Li-ion cell
production (EV + stationary)** — what actually drives LFP cell cost — ≈5,000 GWh cumulative
through 2025, ≈1,600 GWh added in 2025 (IEA GEVO / BNEF). The pre-v5.8 1,800/600 GWh matched
neither stationary BESS (~600/315) nor all-Li-ion; the restated basis has a nearly identical
additions/base ratio (0.32 vs 0.33), so the *trajectory shape* is essentially unchanged —
this is an honesty fix, not a re-forecast.

**Sources:** Lazard LCOE+ 2025 (June 2025), Way et al. *Joule* (2022), OWID learning curves, ITRPV, NREL ATB 2024, BloombergNEF Energy Storage System Cost Survey (2025), Ember BESS Report (2025), GWEC Global Wind Report (2026), IEA Global EV Outlook (2025).

### SMR cost trajectory

Linear interpolation from FOAK to NOAK, then constant:

$$\text{LCOE}_{\text{SMR}}(t) = \begin{cases} \text{LCOE}_{\text{FOAK}} + (\text{LCOE}_{\text{NOAK}} - \text{LCOE}_{\text{FOAK}}) \cdot t/T_{\text{NOAK}} & t < T_{\text{NOAK}} \\ \text{LCOE}_{\text{NOAK}} & t \geq T_{\text{NOAK}} \end{cases}$$

| Parameter | US | EU |
|-----------|----|----|
| FOAK ($/MWh) | 150 | 175 |
| NOAK ($/MWh) | 85 | 85 |
| Years to NOAK | 10 | 12 |

*(v5.8: FOAK 120→150 US / 140→175 EU — recent FOAK datapoints (NuScale/UAMPS implied ~$120+
in 2023$ before cancellation; GE-Hitachi Darlington capex) sit well above the old values.
NOAK $85 remains the standard industry target; σ = 0.25 acknowledges the spread.)*

SMR is a reference technology only, not included in the optimisation.

### Grid-connected renewable-PPA reference

The off-grid optimisation answers "what does it cost to build my own plant?" — but
the real-world alternative for most datacenters is to stay **on the grid** and sign a
**renewable Power Purchase Agreement (PPA)**. This reference line makes that explicit.
Like SMR, it is *not* part of the optimisation; it is plotted (dotted) on the cost
trajectory and reliability figures, printed in the summary header, and written to the
export files.

Delivered cost is three transparent, adjustable components:

$$\text{LCOE}_{\text{grid+PPA}}(t) = \underbrace{p_{\text{PPA}}^0 \cdot \frac{\text{LCOE}_{\text{sol}}(t)}{\text{LCOE}_{\text{sol}}(0)}}_{\text{contracted RE energy}} + \underbrace{c_{\text{delivery}}}_{\text{network charge}} + \underbrace{c_{\text{firming}}}_{\text{balancing / standby}}$$

The contracted-energy term scales with the region's **solar learning curve** (so it
declines as RE costs fall), while the grid network charge and the firming/balancing
premium — the cost of leaning on the grid to make an intermittent PPA reliable for a
24/7 load — are **flat in real terms**, i.e. a hard floor the all-in price cannot fall
below.

| Component | US ($/MWh) | EU ($/MWh) | Source |
|-----------|-----------|-----------|--------|
| PPA energy (2025) | 55 | 72 | LevelTen PPA Price Index (2025; US prices rose on interconnection/tariff pressure) |
| Grid delivery / network | 22 | 33 | Large-C&I network tariffs (EIA / ENTSO-E) |
| Firming / balancing premium | 8 | 12 | Lazard firming adders |
| **All-in 2025** | **85** | **117** | |
| **All-in 2040** (energy follows solar LR) | **≈57** | **≈81** | |

**What it shows.** Grid+PPA sits well below the off-grid high-RE optimum in both
regions and below carbon-priced EU off-grid gas — i.e. **going off-grid is itself a
cost premium**, paid for siting independence. This context was missing when off-grid RE
was compared only to off-grid gas. The base PPA line represents *annual-volumetric* RE
matching (RECs/PPA netted over the year). A **second reference, Grid + 24/7 CFE**
(`grid_cfe_trajectory`), adds a flat `cfe_premium_mwh` (US $40, EU $55) for hour-by-hour
carbon-free matching — the Google/Microsoft target, which needs firm-clean / storage /
deep overbuild to cover every hour and so costs more (US ≈$125→$97, EU ≈$172→$136 over
2025→2040). Both are stylised, adjustable reference lines, never part of the optimisation.

---

## 4. Weather Generation

*Intuition for the whole section: the model needs a realistic year of hour-by-hour sun and
wind. The hard part isn't the daily rhythm (day/night, summer/winter) — it's getting the
**bad stretches** right: cloudy weeks, calm weeks, and especially the multi-day spells when
sun **and** wind are low at the same time (Dunkelflaute, §4.6), because those are what a
renewable system must store or back up against. So §4 builds the averages first (§4.1–4.2,
4.4–4.5), then layers on realistic clustering and sun↔wind correlation (§4.3, §4.6) **without
disturbing those averages** — the recurring phrase "marginals/CFs preserved" means exactly
that: we change* when *the energy is scarce, not* how much *there is on average.*

### 4.1 Solar — clear-sky profile

The deterministic 8760-hour clear-sky trace:

$$\text{CF}_{\text{cs}}(h) = A \cdot \max\!\left(0,\; \sin\!\left(\tfrac{(h_{\text{od}} - 6)\pi}{12}\right)\right)^{1.1} \cdot \left[1 + 0.35\cos\!\left(\tfrac{2\pi(d-172)}{365}\right)\right]$$

where $h_{\text{od}}$ is hour-of-day (0–23) and $d$ is day-of-year (0–364, solstice at $d=172$). The scalar $A$ normalises the annual mean to $\bar{I}/24$:

$$\overline{\text{CF}}_{\text{cs}} = \frac{\bar{I}}{24}$$

**Irradiance inputs:**

| Region | $\bar{I}$ (kWh/m²/day) | Clear-sky CF | Effective CF (post-cloud) | Source |
|--------|------------------------|--------------|---------------------------|--------|
| US (default) | 5.5 | 0.341 | 0.227 | NREL NSRDB |
| EU (default) | 3.8 | 0.237 | 0.158 | EU JRC PVGIS |

The clear-sky CF is normalised to $\bar I/24/0.667$ so the *effective* (delivered) CF after
the stochastic cloud factor equals $\bar I/24$ — see §4.2 (v5.5 double-count fix).

### 4.2 Solar — stochastic cloud attenuation

Daily cloud cover $\xi_d \in [0,1]$ has a Beta marginal with AR(1) persistence carried in
the **latent normal** (v5.9):

$$h_d = \rho_c\, h_{d-1} + \sqrt{1-\rho_c^2}\;e_d, \qquad \rho_c = 0.35
\qquad (h_d \sim N(0,1) \text{ stationary})$$

$$\xi_d = F^{-1}_{\text{Beta}(3,\,1.5)}\big(\Phi(z_{1,d})\big), \qquad
z_{1,d} = \lambda f_d + \sqrt{1-\lambda^2}\, h_d \qquad \mu = \tfrac{3}{4.5} = 0.667$$

Because $z_{1,d}$ is standard normal *every day*, the Beta(3,1.5) marginal holds **exactly**
— deep-overcast days ($\xi<0.3$) occur at the Beta frequency (5.2% of days). *(v5.9 fix:
through v5.8 the persistence was an AR filter applied AFTER the Beta transform,
$\xi_d = \rho_c \xi_{d-1} + (1-\rho_c)\xi_d^*$, which shrank the cloud variance ~40% —
single-day solar droughts occurred at ~1.3% instead of 5.2%, flattering solar-heavy builds —
and diluted the realized wind-solar correlation, see §4.3.)* Day-to-day cloud persistence is
$\lambda^2\varphi + (1-\lambda^2)\rho_c \approx 0.47$ at the defaults (synoptic + local terms;
measured ≈0.45 in simulation).

**Hourly solar capacity factor:**
$$\text{CF}_{\text{sol}}(h) = \text{CF}_{\text{cs}}(h) \cdot \xi_{\lfloor h/24 \rfloor}$$

**Resulting effective capacity factors (v5.5, verified from simulation):**

| Region | Clear-sky CF | Beta mean | Effective CF |
|--------|--------------|-----------|--------------|
| US | 0.341 | 0.667 | **0.227** |
| EU | 0.237 | 0.667 | **0.158** |

**v5.5 — cloud double-count fixed.** Earlier versions normalised the clear-sky mean to
$\bar I/24$ and *then* multiplied by the cloud factor (mean 0.667), applying cloud losses
twice and depressing effective solar CF ~33% (US 0.229 → 0.152). Since $\bar I$ is an
*actual* (cloud-inclusive) NSRDB/PVGIS average, the clear-sky mean is now pre-divided by
the cloud mean ($\bar I/24/0.667$), so the *effective* CF lands at the physically-correct
$\bar I/24$ (US **0.227**, EU **0.158**). The US value sits inside Lazard v18's utility-solar
CF basis (0.20–0.30) that the imported US solar LCOE ($52/MWh) is levelised at. The EU value
(0.158) is *below* that band — as expected for a lower-irradiance northern-European site —
and is costed not against the US Lazard number but against a EU-specific solar LCOE ($60/MWh,
`SOLAR_EU`) that is itself levelised at the lower European CF, so the cost↔CF consistency
holds region by region. For clear, arid sites (US Southwest, Spain), raise $\bar I$ to
6.5–7.0 for CF ≈ 0.27–0.29.

### 4.3 Solar-wind Gaussian copula correlation

*Intuition: in Northern Europe, overcast days tend to be windier and clear days calmer
(both driven by cyclonic weather). We want to reproduce that **co-movement** between sun and
wind without changing how often clouds or winds of any given strength occur on their own. A
**copula** is the standard statistical tool for precisely this: draw two correlated normal
"dial settings", then map each through its own target distribution (Beta for cloud, Weibull
for wind). The correlation $\rho$ couples them; the individual distributions are untouched.*

The daily cloud draw $\xi_d^*$ and daily wind level are coupled via a Gaussian copula. Two standard normals $(Z_1, Z_2)$ are drawn with correlation $\rho$:

$$Z_1, Z_2 \sim \mathcal{N}\!\left(\begin{pmatrix}0\\0\end{pmatrix}, \begin{pmatrix}1 & \rho \\ \rho & 1\end{pmatrix}\right) \qquad \text{via Cholesky: } Z_2 = \rho Z_1 + \sqrt{1-\rho^2}\,\varepsilon$$

Then:
- $\xi_d^* = F_{\text{Beta}(3,1.5)}^{-1}(\Phi(Z_1))$ — cloud draw with Beta marginal
- $v_{d,\text{level}} = F_{\text{Weibull}}^{-1}(\Phi(Z_2))$ — wind daily level with Weibull marginal

When $\rho < 0$ (Northern Europe): clear-sky days $(Z_1 > 0)$ tend to coincide with lighter winds $(Z_2 < 0)$, and overcast days with stronger wind. The EU default $\rho = -0.35$ is calibrated to ERA5 reanalysis patterns.

| Region | $\rho$ | Physical interpretation |
|--------|--------|------------------------|
| US | 0.0 | No systematic correlation |
| EU | −0.35 | Stronger wind on overcast days (cyclonic patterns) |

**v5 note.** $\rho$ is now the *contemporaneous* (same-day) correlation, realised as the **residual** part of the two-factor model in §4.6: a persistent synoptic factor loads positively on both wind and solar (creating joint multi-day lows), while the residual pair carries the cyclonic $\rho<0$. The net same-day correlation of the latent pair equals $\rho$ exactly.

**v5.9 note — the realized correlation now matches.** Through v5.8 the post-transform cloud
filter (old §4.2) diluted the realized daily *capacity-factor* correlation to ≈half the
configured $\rho$ and biased it positive (EU −0.35 realized ≈−0.18; US 0 realized ≈+0.07).
With persistence moved into z-space the realized correlation is restored: EU −0.35 →
measured ≈**−0.28** (the residual gap is the inherent attenuation of a *Pearson* correlation
through monotone marginal transforms — rank/Spearman correlation is preserved exactly); US
0 → ≈0.00. $\rho$ should be read as the latent (copula) correlation; if calibrating against
an observed ERA5 CF-correlation, target the realized value, ~0.8× the configured one.

### 4.4 Wind — mean-reverting AR(1)-on-quantiles model

*Intuition: wind is "sticky" — a calm hour is likely followed by another calm hour. We need
that stickiness, but a naïve way of adding it would quietly lower the turbine's average output
(because power grows with the cube of wind speed, so the swings matter). The fix: add the
stickiness in a transformed, well-behaved "normal" space, then map back to the real wind-speed
distribution. That keeps the long-run capacity factor **exactly** fixed no matter how sticky we
make it — and lets the stickiness span days, not just hours, so multi-day calm spells actually
appear. "AR(1)" is just "today is a weighted blend of yesterday plus fresh randomness"; doing it
"on quantiles" is the map-to-normal-and-back trick.*

**Key design choice (v4, retained):** The AR(1) process operates in standard-normal space, then transforms back to the Weibull marginal. This preserves the exact Weibull capacity factor regardless of the autocorrelation parameter $\rho_w$ — resolving a v3 bug where AR(1) in speed space reduced wind CF by ~42% via Jensen's inequality on the cubic power curve.

**v5 fix — persistence of multi-day lulls.** v4 reverted the hourly wind toward the *climatological* mean (0 in normal space), so a daily lull decayed within hours ($0.75^{24}\approx 0.001$) and multi-day lulls never persisted in the trace. v5 reverts toward a **persistent daily mean** $m_d$ carried by the synoptic daily draw $z_{2,d}$ (§4.6):

$$z_h = m_{\lfloor h/24\rfloor} + \text{dev}_h, \qquad \text{dev}_h = \rho_w\,\text{dev}_{h-1} + c_{\text{dev}}\,\varepsilon_h$$

with $m_d = a_w\, z_{2,d}$, $a_w^2 = 0.5$ (share of wind variance at the daily/synoptic scale), $\rho_w = 0.75$, and $c_{\text{dev}} = \sqrt{(1-a_w^2)(1-\rho_w^2)}$ chosen so the stationary variance of $\text{dev}$ equals $1-a_w^2$. Hence $z_h \sim \mathcal{N}(0,1)$ **exactly** (sum of independent $\mathcal{N}(0,a_w^2)$ daily mean and $\mathcal{N}(0,1-a_w^2)$ deviation), so the Weibull marginal — and the annual CF — is preserved while the daily/synoptic level now persists across the day.

**Process:**

1. Build the persistent daily mean $m_d = a_w z_{2,d}$ from the synoptic factor (§4.6).

2. Build hourly deviations as a stationary AR(1), add $m_d$, transform to speed:

$$u_h = \Phi(z_h) \qquad v_h = c \cdot (-\ln(1-u_h))^{1/k}, \qquad k=2.1,\; c = \bar{v}/\Gamma(1+1/k)$$

3. Apply seasonal modulation: $v_h \leftarrow v_h \cdot [1 + 0.12\cos(2\pi(d-15)/365)]$

**Verified capacity factors:**

| Region | $\bar{v}$ (m/s) | Simulated CF (v5.5) |
|--------|-----------------|---------------------|
| US | 7.5 | **0.328** |
| EU | 7.0 | **0.289** |

**v5.5 — modern turbine.** The rated speed was lowered 13.0 → 11.0 m/s (cut-in 3.5 → 3.0;
§4.5) to represent a modern **low-specific-power** onshore turbine (large rotor per rated
kW), which is what utility fleets — and the Lazard onshore wind $/MWh — now assume. This
lifts simulated CF from ≈0.22 to **0.33 (US) / 0.29 (EU)**, inside Lazard v18's onshore CF
basis (0.30–0.55) the wind LCOE is levelised at. The old 13 m/s curve (CF ≈0.22) was a
high-specific-power machine, inconsistent with the imported cost.

### 4.5 Wind power curve (IEC Class II)

$$P_{\text{wind}}(v) = \begin{cases} 0 & v < v_{\text{ci}} \text{ or } v > v_{\text{co}} \\ \left(\frac{v - v_{\text{ci}}}{v_r - v_{\text{ci}}}\right)^3 & v_{\text{ci}} \leq v < v_r \\ 1 & v_r \leq v \leq v_{\text{co}} \end{cases}$$

| Parameter | Value (v5.5) | Meaning |
|-----------|--------------|---------|
| $v_{\text{ci}}$ | 3.0 m/s | Cut-in speed |
| $v_r$ | 11.0 m/s | Rated speed |
| $v_{\text{co}}$ | 25.0 m/s | Cut-out speed |

Consistent with a modern **low-specific-power** onshore turbine (large rotor, ~10–11 m/s
rated; e.g. Vestas V162-class, GE 6.1-158). These three speeds are exposed on
`SystemParams` (`wind_v_ci`, `wind_v_rated`, `wind_v_cutout`), so a higher-specific-power
machine or a stronger/weaker resource can be modelled without code changes.

### 4.6 Synoptic "Dunkelflaute" factor (v5)

*Intuition: the thing that actually breaks a high-renewable system isn't one dark hour — it's
a weather system that parks low sun **and** low wind over the whole region for days on end (a
"Dunkelflaute"). To create those, we add a single shared, slow-moving "synoptic" factor $f_d$
that nudges both sun and wind in the same direction together; when it dips for a week, both
sag for a week. Each source then gets its own independent noise on top. The shared factor's
**persistence** ($\varphi$) sets how long a spell lasts; its **loading** ($\lambda$) sets how
tightly sun and wind move together. Crucially the long-run averages are unchanged — we've only
made the scarce periods cluster into realistic multi-day events instead of scattering randomly.*

Persistent multi-day periods of *simultaneously* low wind **and** low sun (Dunkelflaute) are the dominant driver of storage/backup sizing at high RE — and were essentially absent from v4 (daily-independent clouds, ~4h wind memory, and an EU copula that actively paired low-sun with *high* wind). v5 adds a daily-scale latent **two-factor** model:

$$f_d = \varphi\, f_{d-1} + \sqrt{1-\varphi^2}\,\eta_d \quad (\text{common synoptic factor, } f_d\sim\mathcal N(0,1))$$

$$z_{1,d} = \lambda f_d + \sqrt{1-\lambda^2}\,g_{1,d}, \qquad z_{2,d} = \lambda f_d + \sqrt{1-\lambda^2}\,g_{2,d}$$

where $z_1$ drives cloud (Beta marginal) and $z_2$ drives the daily wind level (Weibull). The residual pair $(g_1,g_2)$ is bivariate-normal with correlation

$$\rho_g = \frac{\rho - \lambda^2}{1-\lambda^2}$$

chosen so $\text{corr}(z_1,z_2)=\rho$ exactly (validity requires $\lambda^2 \le (\rho+1)/2$). Because both load **positively** on $f_d$, a multi-day negative excursion of $f_d$ pushes *both* sun and wind down together — a Dunkelflaute episode of mean length $\approx 1/(1-\varphi)$ days. The marginals of $z_1,z_2$ remain standard normal, so the Beta/Weibull marginals — and the annual-mean CFs — are unchanged; only the temporal clustering changes.

| Region | $\lambda$ (loading) | $\varphi$ (persistence) | Episode e-folding |
|--------|--------------------|------------------------|-------------------|
| US | 0.50 | 0.82 | ~5.6 days |
| EU | 0.50 | 0.85 | ~6.7 days |

**Verified effect (8-year EU sample, 5× solar + 4× wind):** the longest spell with 24h-average generation below load grows from ~203h (no synoptic factor) to ~324h mean / 562h max — i.e. realistic week-scale lulls now appear — while $\overline{\text{CF}}_{\text{sol}}$ and $\overline{\text{CF}}_{\text{win}}$ are unchanged to within Monte-Carlo noise.

**Mid-summer seam (v5.9).** Each simulated year's daily latent series $(z_1, z_2)$ is rolled
182 days so the AR-chain start/end seam falls in early **July**: the winter half-year — where
Dunkelflaute risk lives — is one contiguous stretch of the chain, and a lull spanning the
calendar year boundary (Dec→Jan) is fully represented instead of being severed at Jan 1
(pre-v5.9, persistent states restarted at the trace boundary in mid-winter, mildly thinning
extreme multi-week lull statistics). The processes are stationary/seasonless, so the roll
changes no distribution; calendar seasonality (clear-sky, winter wind amplification) stays
calendar-aligned.

### 4.7 Spatial diversification — geographic portfolio (`n_sites`)

*Intuition: a real high-RE operator does not build on one patch of ground; it spreads
generation across sites. Dunkelflaute is synoptic-scale, so sites in a region experience
it largely together — but not perfectly, and the partial decorrelation is exactly what
makes a portfolio more reliable than any single site. The §4.1–4.6 model is single-site
(`n_sites=1`), which §12 flags as the largest **directional** bias in the headline; this
section adds the knob that removes it.*

The portfolio (`weather.generate_weather_portfolio`) builds $N=$ `n_sites` sites that
**share one regional synoptic factor** $f^{\text{common}}_d$ (the §4.6 AR(1) process) and
combine it with an independent per-site part:

$$f^{(i)}_d = \sqrt{c}\;f^{\text{common}}_d + \sqrt{1-c}\;f^{(i),\text{indep}}_d,\qquad
c = \texttt{site\_synoptic\_corr}\in[0,1]$$

so any two sites have synoptic correlation $c$ while each $f^{(i)}$ keeps the same
AR(1)$(\varphi)$ law. Each site then runs the full §4.1–4.6 machinery, and the portfolio CF
is the **average** of the $N$ site traces.

**Shared local weather (v5.9, `site_local_corr`).** Through v5.8 the local cloud/wind
residuals were fully independent per site, so the effective cross-site daily correlation was
only $\lambda^2 c \approx 0.18$ at the defaults — but real intra-region sites (100–300 km
apart) also share local weather (observed daily cross-correlations ~0.3–0.6), so the
portfolio *overstated* diversification. The local residuals are now mixed the same way as
the synoptic factor, with $c_\ell = $ `site_local_corr` (default **0.4**, the literature
mid-range): total cross-site latent daily correlation $= \lambda^2 c + (1-\lambda^2)c_\ell$.
Set `site_local_corr=0` to recover the pre-v5.9 behaviour.

Two properties make this safe and meaningful:

- **Mean CF is preserved exactly** ($E[\text{avg}] = $ single-site mean), so the
  imported-LCOE cost basis (§4.2, the v5.5 CF-consistency invariant) is untouched. Only
  the *variance/clustering* of the lows changes — which is precisely what sizes high-RE
  storage and backup.
- **`n_sites=1` reduces byte-for-byte to §4.1–4.6** (identical RNG draw order), so the
  default model — and every published number — is unchanged. The shipped default is
  `n_sites=1`; diversification is strictly opt-in (`--sites`).

$c\to1$ ⇒ coincident sites ⇒ no smoothing; $c\to0$ ⇒ independent sites ⇒ maximal
(optimistic) smoothing. The default $c=0.7$ is deliberately conservative about how much
diversification helps, since real intra-region sites are strongly coupled.

**Verified effect (EU 90% RE, firm, 2025; reduced-fidelity grid).** The deepest 3-day
generation lull rises from ≈0.46 (single site) to ≈0.73 (3 sites) to ≈0.87 (6 sites) of
load, and the delivered LCOE falls from ≈\$161/MWh (single site) to ≈\$98 (3 sites, $c{=}0.6$)
to ≈\$91 (5 sites, $c{=}0.5$) — a ~40% reduction — with mean solar/wind CF unchanged to
±0.001. **Caveat:** like the synoptic factor itself (§12), $c$ is calibrated, not fitted —
treat the *direction and rough magnitude* as robust and the exact figure as dependent on
the inter-site correlation, which should be set from reanalysis for a specific portfolio.

### 4.8 Real-weather headline & LCOE re-levelling (v6.0)

Through v5.9 the headline ran on the synthetic generator (§4.1–4.6), calibrated so its mean
CFs sat inside Lazard's bands (the v5.5 CF-consistency invariant). v6.0 instead drives the
headline suite with **measured ERA5 reanalysis** at one representative data-center market per
region — **US: ERCOT Texas; EU: France** — loaded through the same `weather_years` hook the
`--weather` flag uses (`tools/fetch_era5.py` → `solar`/`wind` hourly CF, 11 years 2015–2025).
A *single* real site is deliberate: a single off-grid datacenter gets one site's weather, not
the geographic smoothing of a portfolio (§4.7), so the headline must not borrow it.

**Why re-level.** The dispatch's per-MWh generation cost is `C · CF · LCOE` (§6): the imported
Lazard LCOE is levelised at a *reference* CF, and multiplying by the simulated CF only recovers
the right $/MWh when the two agree. Real ERA5 gives the *site's* CF, which generally differs.
A panel or turbine costs the same `$/kW` wherever it stands, so we hold that capital fixed and
re-express the LCOE at the real CF:

$$\text{LCOE}_{\text{real}} = \text{LCOE}_{\text{today}} \cdot \frac{\text{CF}_{\text{ref}}}{\text{CF}_{\text{real}}}$$

where $\text{CF}_{\text{ref}}$ is the synthetic CF for the region's resource (the Lazard-anchored
basis) and $\text{CF}_{\text{real}}$ the mean CF of the supplied years. Substituting back,
`C · CF_real · LCOE_real = C · CF_ref · LCOE_today`: **cost tracks the fixed capital while energy
follows the real weather** — the CF-invariant, at the real site. It is applied once in
`run_simulation` (scaling the `solar`/`wind` `lcoe_today`), so it propagates to every consumer —
the RE-target lines, the gas-free H₂ line, the cost-MC band. Realised: **Texas** solar 0.265 /
wind 0.320 (re-level ×0.86 / ×1.04 — sunny Texas makes solar *cheaper* per MWh); **France** solar
0.183 / wind 0.135 (×0.86 / ×2.1 — poor French wind roughly doubles wind's $/MWh). The synthetic
generator still backs every sensitivity that needs the resource as a free knob (resource band —
auto-skipped under fixed real weather — tornado, `--resource`).

**Caveat — the site carries the result.** A single real cell is only as representative as the
cell. Inland low-wind sites read low (e.g. ERA5 Ashburn, VA: 100 m wind ~4.7 m/s → CF 0.085 via
the §4.5 curve; a 100→150 m hub-height shear lifts it only to ~0.10), so the US anchor is Texas,
whose measured resource matches the cost basis, rather than the #1-by-floorspace but
pathologically-poor-wind Virginia. Re-fetch a precise point (`tools/fetch_era5.py`) to site-tune.

---

## 5. Chronological Dispatch

*Intuition: "dispatch" is just bookkeeping the year hour by hour, in order. Each hour, compare
renewable output to load: if there's a surplus, charge the battery (and spill the rest); if
there's a deficit, drain the battery, and if that's not enough, burn gas. Doing it **in
chronological order** matters — yesterday's clouds leave the battery empty for today's, which a
simple averaging approach would miss. Run all 8,760 hours and you learn the two things the cost
model needs: what fraction of the year's energy came from gas, and how big the worst single-hour
gap was (which sizes the gas plant).*

### 5.1 3D precomputed grid

Unlike a simple 2D grid over $(C, B)$ for a single source, the model runs combined dispatch over all $21^3 = 9{,}261$ combinations of $(C_{\text{sol}}, C_{\text{win}}, B)$ simultaneously (`grid_steps=21`, v5.1 right-sized lattice), vectorised in numpy. This correctly accounts for solar and wind feeding the same load and battery — the critical fix over the independent-surface approximation.

### 5.2 State variables

| Variable | Units | Description |
|----------|-------|-------------|
| $G(t)$ | MW/MW-load | Total renewable generation: $C_{\text{sol}}\cdot\text{CF}_{\text{sol}}(t) + C_{\text{win}}\cdot\text{CF}_{\text{wind}}(t)$ |
| $\text{SoC}(t)$ | MWh/MW-load | Battery state of charge |
| $\text{shed}(t)$ | MW/MW-load | Load shed this hour (interruptible workloads only) |
| $\text{gas}$ | MWh | Cumulative annual gas energy |

### 5.3 Dispatch waterfall (each hour)

$$\text{net}(t) = G(t) - 1 \qquad \text{(load normalised to 1 MW)}$$

**Deficit** ($\text{net} < 0$, $\text{deficit} = -\text{net}$):

**(a) Battery discharge:**
$$d = \min(\text{SoC} \cdot \eta_{\text{dis}},\; P_{\text{batt}},\; \text{deficit})$$
$$\text{SoC} \mathrel{-}= d/\eta_{\text{dis}}, \quad \text{deficit} \mathrel{-}= d$$

**(b) Shed (interruptible workloads only):**
$$\text{shed} = \min(f_{\text{int}},\; \text{deficit}), \quad \text{deficit} \mathrel{-}= \text{shed}$$
where $f_{\text{int}} = $ `interruptible_fraction`. The dispatch records the *max-sheddable*
residual; whether this shed is actually taken is decided **economically** in the
optimiser (§8), which compares the value of lost compute to the gas variable cost. For
the FIRM default ($f_{\text{int}}=0$) nothing is shed. The pre-shed deficit peak is also
recorded, so the firm case can size gas backup to 100% of load.

**(c) Gas backup:** $\text{gas} \mathrel{+}= \text{deficit}$ (peak residual → backup capacity)

**Surplus** ($\text{net} > 0$, $\text{surplus} = \text{net}$):

**(e) Charge battery:**
$$c = \min\!\left(\frac{\text{SoC}_{\max} - \text{SoC}}{\eta_{\text{chg}}},\; P_{\text{batt}},\; \text{surplus}\right)$$
$$\text{SoC} \mathrel{+}= c \cdot \eta_{\text{chg}}$$

**(f) Curtailment:** $\text{surplus} - c$ is curtailed (zero value).

### 5.4 Battery parameters

| Parameter | Symbol | Value | Source |
|-----------|--------|-------|--------|
| Round-trip efficiency | $\eta_{\text{rt}}$ | 0.924 | NREL ATB 2024 (DC-DC, LFP) |
| Charge efficiency | $\eta_{\text{chg}}$ | $\sqrt{0.924} = 0.961$ | Symmetric split |
| Discharge efficiency | $\eta_{\text{dis}}$ | $\sqrt{0.924} = 0.961$ | Symmetric split |
| Max SoC | $\text{SoC}_{\max}$ | $B$ MWh/MW-load | |
| Power rating | $P_{\text{batt}}$ | $\min(1,\; 4/B)$ MW/MW-load | NREL ATB 2024 |

**Power de-rating:** LFP systems with $B > 4$h are typically specified at lower C-rates to reduce inverter cost. At $B = 12$h, $P_{\text{batt}} = 4/12 = 0.33$ MW/MW-load. This is consistent with NREL ATB 2024 long-duration storage specs and is applied consistently in both dispatch and cost calculation.

### 5.5 Workload profiles & flexibility scenarios

The **default is FIRM** (always-on): the datacenter never shuts down and gas backup is
sized to 100% of load. Optionally a workload can be made interruptible via two knobs on
`WorkloadProfile`:

- **`interruptible_fraction`** — *how big a slice of load may be shed* in a deficit hour
  (the rest is must-run). There is **no recovery** — shed compute is lost.
- **`shed_penalty_mwh`** — *the value of that lost compute* ($/MWh). The optimiser sheds
  only when this is **below the gas variable cost** of serving the hour (fuel + carbon +
  VOM). Grounding: an H100-class server (~$25/W, ~4 yr, ~10% WACC) implies ≈ $900/MWh of
  compute-energy in idled hardware alone — so premium training never sheds; only
  genuinely cheap compute does.

| Preset | `interruptible_fraction` | `shed_penalty_mwh` | Represents |
|--------|--------------------------|--------------------|------------|
| `firm`          | 0%  | —      | Always-on; gas backup to 100% (capped opex) |
| `enterprise`    | 5%  | $2500  | User-facing, tight SLA (never sheds) |
| `training`      | 40% | $900   | Premium AI cluster (sheds ~never → ≈ firm) |
| `interruptible` | 60% | $150   | Batch/research, lower-value |
| `best-effort`   | 90% | $40    | Spot/preemptible — sheds eagerly |

Because the shed test is `shed_penalty < gas_var`, **premium presets (`enterprise`,
`training`) collapse to firm** in both regions (compute worth far more than gas
fuel+carbon), and even cheap compute does not shed in the US (gas variable cost ≈ $29/MWh
is below almost any compute value). Shedding mainly matters for low-value compute in
carbon-priced markets.

**Sensitivity.** `--flex-sweep` sweeps `interruptible_fraction × shed_penalty` and emits a
2D heatmap (delivered LCOE + parity year). For EU 90% RE in 2030 it shows the
sharp threshold at the gas variable cost (~$100/MWh in EU 2030, v5.8 combustion-only carbon): below it, more interruptibility →
lower cost and earlier parity (down to ~$34/MWh, parity 2025 at 95% interruptible /
$25 compute); at or above it, the system is firm (~$165/MWh at the sweep's reduced
fidelity). See `figs/eu_flex_heatmap.png`.

### 5.6 What "90% RE" means

The RE target $R$ is an **annual energy fraction of the energy actually served**:

$$f_{\text{RE,served}} = 1 - \frac{f_{\text{gas}}}{1 - f_{\text{drop}}} \;\ge\; R,
\qquad f_{\text{gas}}=\tfrac{\text{gas}_{\text{annual}}}{8760},\quad
f_{\text{drop}}=\tfrac{\text{shed}_{\text{annual}}}{8760}$$

So **"90% RE" = at least 90% of the megawatt-hours the datacenter actually consumes come
from solar + wind + battery, and at most 10% from the gas backup.** Precisely:

*   It is an **energy** fraction over the whole year, **not** an hourly guarantee and **not**
    a capacity share. In any single hour gas may supply 100% (a multi-day Dunkelflaute) or 0%
    (a sunny, windy week); only the annual MWh mix is constrained.
*   For the **firm** default (no shedding) $f_{\text{drop}}=0$, so this is simply
    $1-f_{\text{gas}} \ge R$ over fully-served load — the datacenter is never blacked out
    (gas backup is sized to 100% of load), and "90% RE" is a pure fuel-mix statement.
*   For **interruptible** workloads the denominator is *served* energy: of the power you
    chose to deliver, ≥90% is renewable. The shed fraction (compute you declined to run) is
    reported separately in the summary's `Shed` column so reliability stays explicit.
*   The user picks $R$ (70/80/85/90%); the optimiser finds the least-cost build meeting it.
    Targets above the ~94% battery-only ceiling (§11) are infeasible and flagged with a warning.

This replaces the v4 definition, where load silently dropped during deficits still counted
as "renewable," inflating the RE fraction by ~8 points.

### 5.7 Datacenter load shape (`load_profile`)

The headline normalises load to a **constant** 1 MW each hour. v5.6 allows a non-flat shape
(`weather.load_profile`, `SystemParams.load_profile`, CLI `--load-profile`), kept
**mean-normalised to 1.0** so it is a *shape*, not a level: the model stays "per MW of average
load" and every annual $\big/8760$ denominator (gas fraction, delivered cost) is unchanged.
The hourly balance becomes $\text{net}(t)=G(t)-\ell(t)$ with $\overline{\ell}=1$; `"flat"`
($\ell\equiv1$) is the default and reduces the dispatch **exactly** to §5.3 (all published
numbers unchanged).

`"cooling"` adds a temperature-driven cooling (PUE) overhead on a constant IT base — higher
draw on hot summer afternoons — so **peak load exceeds average** (peak $\approx1.20$). Because
firm gas is sized to *peak* load (§7), a peaky profile needs a modestly larger gas plant; and
because the cooling peak coincides with strong midday solar, renewables cover much of it, so
the annual gas *fraction* barely moves. The effect is opt-in and small; the point is that load
shape is now a first-class, adjustable input rather than a hidden "flat" assumption.

---

## 6. Battery Degradation, Augmentation, and Cost

*Intuition: a battery wears out two ways — just by getting older (calendar fade) and by being
cycled (throughput fade) — so a pack slowly loses usable capacity. Rather than rip out and
replace the whole system mid-life, the operator **augments**: each year it adds just enough new
cells to offset that year's fade, holding capacity at nameplate. That's both standard industry
practice and cheaper, and it's why the delivered battery cost (§6.4–6.5) comes out ~30% below a
full-replacement assumption.*

### 6.1 LFP degradation model

Battery capacity fades through two mechanisms:

$$\text{cap}(t) = 1 - \delta_{\text{cal}} \cdot t - \delta_{\text{cyc}} \cdot N_{\text{FEC,eff}}(t)$$

| Parameter | Symbol | Value | Source |
|-----------|--------|-------|--------|
| Calendar degradation | $\delta_{\text{cal}}$ | 0.020/yr | NREL BTM 2023; Xu et al. (2018) |
| Cycle degradation | $\delta_{\text{cyc}}$ | $5 \times 10^{-5}$/FEC | ~4,000 FEC to 80% at 100% DoD (LFP) |
| DoD exponent | $\beta$ | 0.60 | LFP Wöhler curve approximation |
| Replacement threshold | | 80% | Industry warranty standard |

**Note:** $\delta_{\text{cyc}} = 5\times10^{-5}$/FEC corresponds to 4,000 full cycles to 80% capacity — the standard LFP specification (CATL, EVE Energy datasheets). *(v5.4: the DoD exponent $\beta$ and the 80% replacement threshold are retained in `BatteryParams` but no longer drive the cost — the augmentation model below holds capacity at nameplate, and cycling enters via throughput EFCs, not a Wöhler weighting.)*

### 6.2 Throughput equivalent-full-cycles (v5.4)

Battery cycling is measured as **throughput equivalent full cycles** — the annual
cell-discharge energy divided by the rated energy capacity — accumulated directly in
dispatch:

$$\dot{N}_{\text{FEC}} = \frac{1}{365}\cdot\frac{\sum_t d_t/\eta_{\text{dis}}}{\text{SoC}_{\max}}\ \ [\text{cycles/day}]$$

where $d_t$ is the hour-$t$ discharge delivered to load. This is a standard, robust cycle
count that replaces the v5 $2\sigma_{\text{SoC}}/\text{SoC}_{\max}$ heuristic; it is exact
for symmetric daily cycling. (Full rainflow half-cycle counting with a Wöhler/DoD weight
is the further refinement.)

### 6.3 Augmentation (v5.4 — replaces mid-life full replacement)

Rather than replacing the whole system when capacity hits 80%, the operator **augments**:
each year it adds enough **energy (cell)** capacity to offset that year's fade, holding
usable capacity at nameplate — standard industry practice, and cheaper than full
replacement. The power/BOS (inverter) component is built once (its mid-life replacement is
folded into O&M). With fade rate $\delta = \delta_{\text{cal}} + \delta_{\text{cyc}}\cdot\dot N_{\text{FEC}}\cdot365$:

$$\text{aug}_{\text{yr}} = \delta \cdot C_{\text{energy}}, \qquad C_{\text{energy}} = B\cdot c_{\text{energy}}\cdot10^3$$

Augmentation is priced at today's capex (future cells are cheaper, so this is conservative).

### 6.4 Cost per installation

$$C_{\text{one}} = \underbrace{B \cdot c_{\text{energy}} \cdot 10^3}_{C_{\text{energy}}\ (\text{augmented})} + \underbrace{P_{\text{batt}} \cdot c_{\text{power}} \cdot 10^3}_{\text{power/BOS, built once}} \quad [\$/\text{MW-load}]$$

Power capex scales with $P_{\text{batt}} = \min(1, 4/B)$, not always 1 MW — consistent with the dispatch de-rating.

### 6.5 Augmentation NPV and annualised cost

$$\text{NPV}_{\text{batt}} = C_{\text{one}} + \text{aug}_{\text{yr}} \cdot \sum_{t=1}^{n}(1+r)^{-t}
= C_{\text{one}} + \delta\, C_{\text{energy}}\cdot \frac{1-(1+r)^{-n}}{r}$$

$$C_{\text{batt}}^{\text{ann}} = \text{NPV}_{\text{batt}} \cdot \text{CRF}(r, n) + C_{\text{one}} \cdot \phi_{\text{OM}}, \qquad c_{\text{batt}} = C_{\text{batt}}^{\text{ann}} / 8760$$

where $\phi_{\text{OM}} = 0.015$ (1.5%/yr O&M) and $\text{CRF}(0.07, 20) = 0.0944$.

**Verified delivered battery costs (2025, 0.5 EFC/day, v5.4 augmentation, v5.8 capex basis):**

| Storage duration | Delivered cost (US) | Delivered cost (EU) |
|-----------------|--------------------|--------------------|
| 2h | $4.8/MWh | $4.3/MWh |
| 4h | $7.7/MWh | $7.2/MWh |
| 8h | $12.4/MWh | $12.1/MWh |
| 12h | $17.7/MWh | $17.6/MWh |
| 24h | $34.5/MWh | $34.4/MWh |

The augmentation model (vs full replacement) is worth ~30–35% by itself (v5.4); the v5.8
capex recalibration ($130/kWh US installed 4h, BNEF 2025 anchor) takes the 4h US delivered
cost from $13.1 to $7.7/MWh. US and EU remain nearly identical (region-invariant cells; the
US marginally higher via the tariff-driven power/BOS premium). Consistent with NREL ATB
augmentation methodology, BNEF 2025 system prices, and Ember 2025 LCOS ranges.

---

## 7. Gas Backup Cost and Carbon Trajectory

*Intuition: gas is the backstop that makes the datacenter "firm" (never goes dark). Its cost per
delivered MWh has three parts — the plant's capital (spread over how little it runs), the fuel it
burns, and a carbon price on its emissions. A plant that runs a lot wants to be efficient and is
worth building well (CCGT); one that fires only rarely wants to be cheap to build even if
thirsty to run (OCGT peaker) — so §7.1 picks whichever suits the duty. In Europe a rising carbon
price (§7.3) steadily makes this backstop more expensive, which is the main reason renewables win
there sooner than in the US.*

### 7.1 Technology selection

$$\text{type} = \begin{cases} \text{CCGT} & f_{\text{gas}} \geq 0.20 \\ \text{OCGT} & f_{\text{gas}} < 0.20 \end{cases}$$

At RE > 80%, gas runs less than 20% of hours → OCGT peaker economics apply.

### 7.2 Gas LCOE with Peak Capacity Sizing

$$\text{LCOE}_g = \frac{c_{\text{capex}}^g \cdot \text{CRF}(r, n_g) \cdot K_{\text{gas}}}{8760 \cdot f_{\text{gas}}} + \frac{c_{\text{FOM}}^g \cdot K_{\text{gas}}}{8760 \cdot f_{\text{gas}}} + p_{\text{gas}} \cdot \text{HR}_g + c_{\text{VOM}} + p_{\text{CO}_2}(t) \cdot \varepsilon_g$$

where $K_{\text{gas}}$ is the peak backup capacity factor (maximum hourly residual deficit as a fraction of peak load, i.e., $P_{\text{backup}} / P_{\text{load}}$).

**Delivered gas cost** ($/MWh of total load): $c_{\text{gas}} = \text{LCOE}_g \cdot f_{\text{gas}}$

**Green-hydrogen firming (opt-in, `--firming h2`).** The firming block can instead burn
purchased **green hydrogen** in an H₂-capable turbine — economically "a gas plant with
pricey, zero-carbon fuel", so it reuses the entire formula above with $\varepsilon_g=0$
(no combustion CO₂), a higher fuel price ($p_{\text{gas}}=\$46$/MMBtu ≈ \$5.25/kg LHV,
Lazard LCOH v4.0 unsubsidized PEM), and a modestly higher turbine capex. It trades EU carbon exposure for expensive fuel: a
pure-H₂ plant (the 100%-firming reference) is ≈\$330/MWh (flat, carbon-free) vs natural gas's
\$122→\$163 rising path, but because the firm system burns it only ~10% of hours, an RE+H₂
build still lands well below pure H₂ and decarbonises the residual. Stylised & adjustable
(`GAS_H2`). The breakdown figures (`fig4`/`fig5`) label the firming bands generically
("Firming — capex / fuel + O&M / carbon"), so they are correct under either firming
(green-H₂'s carbon band is ~0 and auto-hidden). `run_firming_comparison` (CLI
`--firming-compare`) re-optimises the same datacenter under gas vs green-H₂ firming and
plots both delivered-cost trajectories: in the EU at 90% RE green-H₂ firming costs
≈ +$27–37/MWh (v5.9: +$37 in 2025 narrowing to +$27 by 2040), a premium that **narrows over
time** as the EU carbon price makes gas firming dearer (the pure-gas reference rises to cross
the RE+H₂ delivered cost ≈ 2039).

**Firm CLEAN baseload firming — geothermal & hydro (opt-in, `--firming geothermal|hydro`, v5.7).**
The same "a power plant with X" trick extends to firm *zero-carbon* resources some sites enjoy:
**geothermal** (e.g. Iceland) and **abundant hydro** (e.g. Norway, the Alps). Both reuse the
gas cost formula with $\varepsilon_g=0$ and **zero fuel**, but with infrastructure-grade capital
(long life, low WACC) and a high baseload CF — so they are firm, dispatchable, and clean.
Standalone delivered cost (via `gas_pure_lcoe(·, cf)`), **sourced to IRENA *Renewable Power
Generation Costs in 2023*** (installed cost) and computed through the model's own per-tech WACC:
**geothermal ≈ \$63/MWh** (\$4,589/kW, CF 0.88, FOM \$130/kW-yr, 30 yr, 6% WACC) and **hydro ≈
\$46/MWh** (\$2,806/kW, CF 0.55, 40 yr, 5% WACC). These land just below IRENA's *published* 2023
LCOEs (\$71 geothermal / \$57 hydro, quoted at IRENA's ~7.5% WACC) — the gap is the lower cost of
capital, the same `rewacc`-style consistency the model applies to the imported Lazard LCOEs.
*Caveat:* the hydro CF (0.55, not an "always-on" 0.85) reflects that a real reservoir is
energy-limited (seasonal inflow); 0.85 would give an over-optimistic ~\$30. Both presets
(`GEOTHERMAL`, `HYDRO`) remain adjustable, and feed the EU-siting comparison (§ below).

### 7.2b Where to site in Europe — clean-power comparison (`tools/build_eu_siting.py`)

A practical question the model can answer: *which EU locations give the cheapest 24/7
carbon-free datacenter power?* `build_eu_siting.py` (`make eu-siting`) scores a curated set of
candidate sites on **one metric — delivered firm zero-carbon \$/MWh** — letting each use its best
clean resource: sun+wind sites build the gas-free solar+wind+LFP+green-H₂ system (§7.6) on **real
ERA5** weather (re-anchoring the imported LCOE to the site CF, as in `build_locations`); geothermal
and hydro sites run on the firm-clean baseload above. It emits a ranked bar chart
(`figs/eu_siting.png`) and **two maps** — one per firming choice (`figs/eu_siting_map_h2.png` and
`figs/eu_siting_map_phs.png`; cartopy coastlines/borders, plain lon/lat-scatter fallback) so the map
never silently picks H₂ vs PHS per site — plus `output/eu_siting_results.json`.

**Result (2030, delivered \$/MWh, real ERA5 2018–2024, v5.9):** firm clean baseload wins decisively —
Nordic/Alpine **hydro ≈ \$46** (Norway, Sweden, the Alps) and Iceland **geothermal ≈ \$63** beat
every build-it-yourself sun+wind site and sit far below gas (EU ~\$131). The sun+wind sites rank by
their **best available firming** (both firmings are shown — see the fairness note): **Gran Canaria
≈\$70, East Crete ≈\$77, Tarifa ≈\$84, SW Sicily ≈\$88, Sines ≈\$93** (pumped-storage-firmed), then
the flat H₂-only sites **Jutland ≈\$118** and **Dover ≈\$121**, and the harder cases **Switzerland
≈\$135** and **Romania ≈\$152** (below). The chart/table also report a **75% RE + gas** build (firm
solar+wind+battery, EU gas on the residual ~25%) — at 75% (not 85%) so it clears the ~80% solar+
battery wall and is feasible at every site with real wind. Candidates are chosen as *promising*
clean-power sites, not typical markets.

**Pumped-storage firming, shown fairly (v5.7).** A sun+wind site can be firmed by green H₂ *or* by
**PHS** (§7.5), and which it can use is itself geographic (off-river PHS needs head). To avoid
conflating site quality with a firming choice, the builder computes **both** firmings for every
site and shows them side by side — two table columns, an open-circle marker on each bar for the
firming *not* chosen, and **two maps** (one per firming, shared colour scale) — with PHS
availability from the **ANU Global Pumped Hydro Atlas**. Where strong sun+wind **co-locates** with
reservoir terrain — the Iberian/Mediterranean sierras and mountainous islands (Tarifa, Sines,
SW Sicily, East Crete, Gran Canaria with its real Chira-Soria scheme) — PHS is markedly cheaper
than H₂ (**Crete \$77 / Tarifa \$84 / Sicily \$88** vs ~\$108–132 via H₂), approaching the firm
hydro (\$46) / geothermal (\$63) leaders, because its 0.80 round-trip wastes far less overbuild
than H₂'s 0.35.

Two honesty points the relocation to real wind resources surfaced. **(1) Wind and PHS terrain are
not always co-located.** Mountainous islands have both; but **Romania**'s wind is on the flat Black
Sea coast (Dobrogea, its windiest region) while its pumped storage is inland in the Carpathians, so
the single-site off-grid model firms Dobrogea with H₂ (≈\$152) — combining the two would need a grid.
**Switzerland** is genuinely wind-poor (best Jura wind CF ≈0.04), so even with world-class PHS its
off-grid RE+PHS cost is high (≈\$135) — its real edge is firm *conventional* hydro generation.
**(2) In carbon-priced EU, partial-gas is not the cheap fallback:** the 75%-RE+gas build undercuts
the fully-clean one only at the flat sites where clean firming (H₂) is itself dear (Jutland, Dover);
where PHS makes clean firming cheap, **going 100% zero-carbon beats 75%-RE-plus-gas**, because
carbon-taxed gas costs more than the extra clean storage. So the verdict is nuanced and defensible:
PHS is transformative **only where good sun/wind *and* pumped-storage terrain coincide**.

**Pure gas reference** (CCGT at 85% CF, verified values, v5.8 capex):

| | US | EU 2025 | EU 2030 | EU 2035 |
|-|----|---------|---------|----|
| Gas price ($/MMBtu) | 4.0 | 10.0 | 10.0 | 10.0 |
| Carbon price ($/tCO₂) | 0 | 70 | 98 | 154 |
| Pure LCOE ($/MWh) | **58.4** | **121.5** | **131.1** | **150.6** |

*(Gas financed at a 9% real WACC (v5.3 per-tech financing). v5.8 recalibration: CCGT capex
$1,100 → $2,000/kW — the post-2024 turbine-shortage order book (GridLab 2025 / EIA AEO2025:
new orders $2,000–2,500/kW with ~2030 delivery; only the already-contracted 2026–27 pipeline
still sees $1,100–1,400) — partially offset on the EU side by the combustion-only carbon
intensity (0.41 → 0.345 tCO₂/MWh, the ETS-consistent scope). Net: US +$12.3, EU 2025 +$7.7.)*

**Gas-baseline asymmetry & the "gas-stress" reference line (v5.7).** The headline holds gas
fuel flat (US $4/MMBtu with **$0 carbon to 2040**; EU $10/MMBtu). That makes US gas a very low,
very *stable* floor — which is *why* US high-RE struggles to cross it — but it is an assumption,
not a fact: Henry Hub has ranged $2–9, AI-datacenter demand is a real upward pressure, and a US
carbon price is a policy possibility. Two transparency features make this visible. (a) The suite
plots a **gas-stress reference line** (`gas_stress_mult`, default ×1.6 → US ≈$74/MWh, EU
≈$160/MWh in 2025, v5.8 capex) — the same plant at a stressed fuel price, a dot-dashed line on fig1; US
70%-RE crosses *it* by ~2030 even though it never crosses the $4 baseline. (b) The **tornado**
(§9) includes a *gas-price ∓25%* lever and a *carbon-introduction* lever (+$40/tCO₂), which for
the US is the single largest mover of the parity gap. The point: the robust US conclusion is
"cheap *flat* gas is hard to beat," and that conclusion is contingent on gas staying cheap.

### 7.3 Carbon price trajectories

Three trajectory modes are available:

**Linear:** $p(t) = p_0 + \dot{p} \cdot t$

**Logistic** (EU default — Fit-for-55 non-linear tightening):
$$p(t) = p_0 + (p_{\text{cap}} - p_0) \cdot \frac{\sigma(t) - \sigma(0)}{1 - \sigma(0)}, \qquad \sigma(t) = \frac{1}{1 + e^{-k(t - t_{\text{mid}}})}$$

This is normalised so $p(0) = p_0$ exactly. The EU default parameters ($p_0 = \$70$, $p_{\text{cap}} = \$200$, $k=0.35$, $t_{\text{mid}}=8$) produce (verified):

| Year | Carbon price ($/tCO₂) |
|------|----------------------|
| 2025 | 70.0 |
| 2027 | 77.1 |
| 2029 | 89.4 |
| 2030 | 97.8 |
| 2033 | 131.0 |
| 2035 | 154.2 |
| 2038 | 179.6 |
| 2040 | 189.0 |

**Step:** jumps from $p_0$ to $p_{\text{cap}}$ at $t_{\text{mid}}$ — for policy shock sensitivity analysis.

### 7.4 Gas parameters

| Parameter | US | EU | Source |
|-----------|----|----|--------|
| CCGT capex ($/kW) | 2,000 | 2,000 | GridLab 2025 / EIA AEO2025 order book (v5.8) |
| OCGT capex ($/kW) | 1,000 | 1,000 | GridLab 2025: CTs in service 2025 $728–1,544/kW (v5.8) |
| CCGT FOM ($/kW-yr) | 15 | 15 | Lazard |
| OCGT FOM ($/kW-yr) | 10 | 10 | Lazard |
| CCGT heat rate (MMBtu/MWh) | 6.5 | 6.5 | EIA; ~52% LHV |
| OCGT heat rate (MMBtu/MWh) | 9.5 | 9.5 | EIA; ~36% LHV |
| VOM ($/MWh) | 3.0 | 3.0 | Lazard |
| Gas price ($/MMBtu) | 4.0 | 10.0 | EIA Henry Hub; TTF forward |
| CCGT CO₂ intensity (tCO₂/MWh) | 0.345 | 0.345 | combustion-only: 53.07 kg/MMBtu × HR (ETS scope, v5.8) |
| OCGT CO₂ intensity (tCO₂/MWh) | 0.50 | 0.50 | combustion-only (v5.8) |
| Carbon price trajectory | Linear ($0) | Logistic ($70→$200) | EU ETS Fit-for-55 |
| Plant lifetime (yr) | 25 | 25 | Industry standard |

*(v5.8 capex note: new turbine orders run $2,000–2,500/kW CCGT with ~2030 delivery slots; the
pre-shortage Lazard-v17-era $1,100/$500 remain available as an explicit override for a
"turbine queue clears" sensitivity. Carbon-intensity note: the model charges the ETS-style
carbon price, whose scope is stack CO₂ only, so the intensity is combustion-only; the old
0.41/0.60 implicitly priced some upstream methane at the ETS rate.)*

### 7.5 Long-duration storage & self-produced-H₂ firming (opt-in overlay, `--ldes`)

Can a high-RE datacenter **make its own H₂** from overcapacity instead of burning gas —
and is that cheaper than **buying** green H₂? The `--ldes` overlay answers this with a
genuine **2-storage chronological dispatch**: at the no-LDES optimal build, LFP keeps the
diurnal cycle while self-produced H₂ charges from otherwise-curtailed surplus and discharges
through multi-day Dunkelflaute.

**Pumped hydro storage (PHS) — the `phs` LDES preset (v5.7).** PHS is the dominant grid
storage worldwide and the cheap multi-day firming option wherever topography + (ideally
existing) reservoirs allow — Switzerland, Italy, Spain, Portugal, Greece, Romania, Norway.
Crucially it is a round-trip **store**, not a generator (pump water up with RE surplus,
regenerate ~80% later through a reversible pump-turbine), so it lives in the LDES tier, *not*
as a firm-clean generation preset. It is **sourced** to NREL ATB (2022–24) and DOE/PNNL
Mongird et al. (2020): round-trip efficiency **0.80** (range 70–87%), all-in CAPEX
**\$1,999–5,505/kW** (the range *is* the site quality; the low end = existing-reservoir sites,
the "untapped" EU case), FOM **\$18/kW-yr**, durations 8–12 h; decomposed (an assumption, from
the all-in \$/kW) into a reversible powerhouse ~\$1,200/kW (pump \$700 + turbine \$500) and a
cheap energy/reservoir component \$60/kWh. Its **~50-yr life** (vs ~20 yr for batteries) is
credited via a new `life_yr` field on `BatteryParams`, so the LDES cost path amortises PHS over
50 yr rather than writing it off over the 20-yr project horizon (~29% cheaper annualised — a
material, correct adjustment). **Lazard does not usefully cover PHS** — its storage analysis is
lithium-ion-centric; the authoritative cost data is NREL ATB + PNNL/Mongird, and for the
*untapped EU potential* specifically the JRC EU-PHS assessment and the ANU Global Pumped Hydro
Atlas. Run via `--ldes phs` / `--ldes-joint phs`; it firms the six PHS-potential siting
candidates (§7.2b). Because PHS's 0.80 round-trip beats green H₂'s ~0.35, far less RE overbuild
is wasted, so RE+PHS is markedly cheaper than RE+H₂ where the topography exists (see §7.2b).

**Tail handling — no blackouts, by assumption.** The firming turbine *always* has fuel: it
burns self-produced H₂ when the store has it and **purchased green H₂ (market) otherwise**.
So the rare deep lull that drains the store is a **cost** (occasional expensive market H₂),
not a loss-of-load event — which is why no reliability/LOLE machinery is needed. Everything
is **zero-carbon** (green H₂ either way): a fully firm, gas-free datacenter. The overlay
sweeps electrolyser power × storage energy and reports the **share of load firmed by
*purchased* H₂** (the rest self-produced) and the delivered LCOE. (Greedy overlay —
LFP/overbuild fixed from the no-LDES optimum → the self-production benefit is a conservative
lower bound; reduced fidelity.)

**Technologies** (`LDES_PRESETS`). Storage and the (mature) H₂ turbine are held flat;
only the electrolyser is assumed to learn, **conservatively** (~15%/doubling, IRENA *Green
Hydrogen Cost Reduction* 2020 cites 16–21%; modest deployment → ≈35% decline by 2035 — not
an aggressive collapse):

| Tech | Energy $/kWh | Electrolyser $/kW | Turbine $/kW | RTE | Source |
|------|-------------|-------------------|--------------|-----|--------|
| Iron-air | 20 | 1,500 | 1,500 | 50% | Form Energy targets / NREL ATB 2024 |
| Self-produced H₂ — **tanks** (default) | 20 (flat) | 1,200→~760 | 1,300 | 35% | DOE/NREL bulk compressed H₂; Lazard LCOH v4.0 |
| Self-produced H₂ — salt cavern (*speculative*) | 0.6 | 1,200→~760 | 1,300 | 35% | Lazard LCOH v4.0 ($20/kg ÷ 33.3 kWh/kg) |

**Salt caverns are *not* assumed** (most sites have none) — above-ground **tanks** are the
default; the cavern preset is kept only to bound the optimistic end (labelled speculative).
Purchased green H₂ (for the residual, and as the buy-everything baseline) is priced from
Lazard LCOH v4.0: ~$5.25/kg ≈ $46/MMBtu, ×CCGT-class turbine heat rate ≈ **$302/MWh-e**,
zero-carbon.

**Finding (EU 90% RE, 2035, tanks, greedy overlay).** Buying *all* firming as green H₂
(turbine + market H₂, no self-production) costs **≈$155/MWh** with ~10% of load served from
the market. **Self-producing most of it is modestly cheaper**: a ~0.5–0.75 MW/MW-load
electrolyser + ~2 days of tank storage cuts the bought share to ~0.6–1% and lands at
**≈$150/MWh** (≈ −$5/MWh) — trading market fuel for electrolyser+storage capex. Driving the
bought share to *exactly* 0 (true self-sufficiency, ~1 MW electrolyser + ~1 week store)
costs **more** (~$190/MWh) than just buying the last sliver, so the economic optimum
**buys the rare deep-lull energy rather than over-building to eliminate it**. The binding
lever is electrolyser power (a small one can't refill between lulls); storage energy and
cavern-vs-tank matter less once the electrolyser is sized. Net: a **fully gas-free,
zero-carbon, firm** EU datacenter is feasible at ~$150/MWh in 2035, self-producing ~99% of
its H₂ and buying the rest — modestly cheaper than buying all H₂, and the deep tail is
handled by the market, not by blackouts or by weeks of storage.

### 7.6 Joint gas-free co-optimisation + market-H₂ spike (`--ldes-joint`)

The overlay above is *greedy* — it bolts H₂ onto the build that was optimal **without**
it. `run_ldes_joint` (CLI `--ldes-joint`) instead **co-optimises the whole gas-free,
zero-carbon system**: multi-start Nelder-Mead over (solar×, wind×, LFP h, electrolyser
MW, H₂-store h) minimising 24/7 delivered LCOE, with the residual bought as green H₂.
No RE target — it is all-green by construction (self-produced + purchased H₂), so it is a
pure cost minimisation on a years-vectorised chronological dispatch. It is **swept over
market-H₂ price multipliers** to stress the deep-lull spike.

**Finding (EU, 2035, self-produced-H₂ tanks, v5.9 inputs).** Co-optimising is **much cheaper
than the greedy overlay**: ~**$120/MWh** for a fully gas-free, zero-carbon, firm datacenter —
vs ~$169 greedy, and **below the gas-backed 90%-RE build of the same year (~$139, §11)**, so
for less than the cost of a 90%-RE-with-gas system you get a fully gas-free, zero-carbon one.
Crucially the optimal *shape* changes: freed from the wind-heavy Dunkelflaute hedge, it goes
**solar-heavy + big electrolyser** (≈6× solar, ≈1.7× wind, ≈6 h LFP, **≈1.0 MW electrolyser**,
≈12 h H₂), self-producing ~92 % of firming and buying ~8 % from the market — turning cheap
surplus solar into H₂ instead of paying for wind overbuild. **Spike hedge:** as market H₂
rises to 2×/4×, the optimum builds more solar + electrolyser + store and buys less
(8.2 %→2.5 %→1.0 %), with LCOE rising only to ~$133/$143 — i.e. self-production caps exposure
to an H₂ price spike. (Caveats: the synthetic weather's sun/wind structure and the
conservative tank/electrolyser learning; multi-start NM on reduced fidelity — treat the
*shape* and *direction* as robust, the absolute as indicative.)

**In the figures.** The headline firm suite draws the **per-year trajectory** of this
optimum (`h2_system_trajectory`, reusing the same `dispatch_h2_vec` + cost model, B_lfp
fixed at 6h, warm-started) as the **"Optimised gas-free H₂ system" line in fig1** and its
capex/opex **breakdown in fig6** (generation / LFP / electrolyser / H₂ storage / turbine /
purchased-H₂ — all zero-carbon), with the **pure-gas reference** overlaid on fig6 so the
zero-carbon system can be read directly against the gas it displaces. The line tracks the
90%-RE-with-gas curve closely in both regions — a fully-optimised gas-free build delivers at
roughly the cost of the constrained 90%-RE-with-gas case (and *below* the cost of forcing RE
past 90% with gas), because it is free to choose a solar-heavy, big-electrolyser mix instead of
the wind-heavy Dunkelflaute hedge. EU trajectory (from `output/eu_firm_results.json`,
`h2_system`): **$154/MWh (2025) → $119 (2035) → $111 (2040)**, crossing below pure gas around
**2031** as EU carbon climbs (US: $146 → $105). (v5.7: these are now evaluated on the full
50-year weather ensemble — the build is optimised on a 20-year subsample — fixing the v5.6
fidelity asymmetry where the line ran on just 6 synthetic years; v5.8/v5.9 lift them a few
$/MWh via the dearer wind/generation inputs and the honest 1-day-drought statistics, partly
offset by cheaper LFP.) These
numbers are exported and regenerable (`tools/regen_doc_tables.py`), not hand-transcribed.

*Robustness (verified).* Fixing B_lfp at 6h is benign — letting `run_ldes_joint` choose it
freely lands at 5.5h (2025) → 6.0h (2040), within 0.5h across the trajectory, and the
fixed-6h per-year optimum reproduces the free 5D joint optimum to <$1/MWh per year. The
electrolyser box ceiling is **4.0 MW/MW-load** (raised from 1.5 in v5.7, where the optimum
approached ~1.7 and would otherwise have bound; kept in sync between `h2system._HI` and
`run_ldes_joint`): under the v5.8 inputs the unconstrained optimum peaks at ~1.2 MW/MW-load,
comfortably interior. All other design variables (overbuild, H₂ store) sit interior to their
bounds throughout.

---

## 8. System LCOE and 3D Optimisation

*Intuition: this section adds up the bill. The delivered cost is just generation + battery +
gas, each in dollars per MWh of load served (§8.1–8.2). Two subtleties make the numbers honest.
First, you pay for **every** MWh a panel or turbine produces, including the surplus you spill on
sunny days (curtailment) — so overbuild shows up as real cost, captured by charging generation at
its full cost over its **simulated** output (§8.1). Second, a dollar of solar and a dollar of gas
aren't financed the same way, so each technology is annualised at its **own** cost of capital and
asset life (§8.3) rather than one blanket rate — cheaper, patient money for renewables, pricier,
riskier money for merchant gas.*

### 8.1 Generation cost

$$c_{\text{gen}} = C_{\text{sol}} \cdot \overline{\text{CF}}_{\text{sol}} \cdot \text{LCOE}_{\text{sol}} + C_{\text{win}} \cdot \overline{\text{CF}}_{\text{win}} \cdot \text{LCOE}_{\text{win}} \quad [\$/\text{MWh-load}]$$

where $\overline{\text{CF}}$ is the **simulated mean capacity factor** across 50 MC weather years. This correctly accounts for curtailed energy — overbuild means paying for generation you don't use, and that cost is fully captured in the generation LCOE.

### 8.2 Total system LCOE

$$\text{LCOE}_{\text{system}} = \underbrace{C_{\text{sol}} \cdot \overline{\text{CF}}_{\text{sol}} \cdot \text{LCOE}_{\text{sol}} + C_{\text{win}} \cdot \overline{\text{CF}}_{\text{win}} \cdot \text{LCOE}_{\text{win}}}_{c_{\text{gen}}} + \underbrace{\frac{C_{\text{batt}}^{\text{ann}}}{8760}}_{c_{\text{batt}}} + \underbrace{\text{LCOE}_g \cdot f_{\text{gas}}}_{c_{\text{gas}}}$$

### 8.3 Capital recovery & per-technology WACC (v5.3)

$$\text{CRF}(r, n) = \frac{r(1+r)^n}{(1+r)^n - 1}$$

**Per-technology cost of capital.** Different assets are financed differently, so v5.3
replaces the single flat WACC with a technology-specific real WACC and asset life. Each
component is levelised over its **own** life (resolving the earlier 20-yr-battery /
25-yr-gas / implicit-gen-life inconsistency): generation and battery costs use their
WACC in their capital-recovery terms; gas plant capex uses the gas WACC over 25 yr.

| Technology | WACC | Life (yr) | CRF | Rationale |
|------------|------|-----------|-----|-----------|
| Solar PV | 5.5% | 30 | 0.0688 | low-risk, long-life infrastructure; contracted revenue |
| Onshore wind | 5.5% | 25 | 0.0745 | same, slightly shorter life |
| LFP battery | 7.0% | 20* | 0.0944 | moderate tech/cycling risk (*replacement horizon, §6) |
| Gas (CCGT/OCGT) | 9.0% | 25 | 0.1018 | merchant + carbon-policy / stranding risk |

These bracket the literature (NREL ATB 4.4–8.0% real; Lazard ~7.7% nominal ≈ 5.5% real for
RE, higher for thermal). **All WACCs in the model are REAL rates** (the model works in
constant 2025 dollars) — battery 7% real ≈ 9.2% nominal, gas 9% real ≈ 11.2% nominal.

**Re-annualising the bundled generation LCOE (`rewacc_lcoe`).** The exogenous solar/wind
LCOE is quoted at `LEGACY_WACC` — since v5.8 **5.5%**, the *real*-terms equivalent of
Lazard's ≈7.7% *nominal* after-tax quote basis at ~2.2% inflation. It splits into a
capital-recovery part (fraction $1-\omega$, with $\omega=$ `om_frac_lcoe`) and a fixed-O&M
part ($\omega$); only the capital part rescales with WACC:

$$\text{LCOE}'_{\text{gen}} = \text{LCOE}_{\text{gen}} \cdot \left[(1-\omega)\,\frac{\text{CRF}(\text{wacc}, L)}{\text{CRF}(0.055, L)} + \omega\right]$$

At the defaults (solar/wind WACC = 5.5% real = the quote basis) this is the **identity** —
delivered generation LCOE equals the imported curve. Pre-v5.8, `LEGACY_WACC` was 0.07
(Lazard's *nominal* rate treated as real), so the transform multiplied solar by 0.876 and
wind by 0.902 — a ~10–12% "cheaper capital" discount that was actually inflation counted
twice; v5.8 removes it. The lever still re-prices genuinely cheaper or dearer capital, and
the factor is year-independent, so cost *trajectories* (and the grid+PPA line, which uses
an LCOE ratio) keep their shape.

### 8.4 Optimiser grid resolution & boundary guard (v5.1)

The objective interpolates trilinearly into a precomputed `grid_steps`³ gas-fraction
surface, so the per-axis node spacing ($\text{max}/(\text{grid\_steps}-1)$) sets the
optimiser's resolution. The gas-fraction surface is convex in $B$, so on a coarse grid
linear interpolation overstates gas (understates RE) between nodes and pins the constrained
optimum onto grid nodes. v5 paired only 15 nodes with oversized bounds (EU $B\!\in\![0,168]$h
→ **12h** spacing; wind to 20× → 1.43×), which produced flat node-pinned trajectories and
overstated EU high-RE cost ~15–30% with ~2× oversized storage.

v5.1 fixes this two ways:

1. **Right-sized bounds** so the grid concentrates where optima live. With the 21 nodes
   and the bounds shipped in the code (§2): battery 0–60h → 3.0h spacing, US wind 0–18× →
   0.9×, EU wind 0–20× → 1.0×, solar 0–18/22× → 0.9/1.1×. (The v5.1 right-sizing was first
   validated against a 41³ fine-grid reference at ~7× lower compute; the EU bounds were then
   widened further for the v5.2 firm model, whose no-shed high-RE optimum is wind-heavy.)
2. **Boundary-binding guard** (`_warn_if_binding` in `optimal_cost_3d`): prints a warning
   whenever an optimum lands within one grid-step of a max bound, so a binding cap (which
   would *understate* cost) can never silently recur. The default suite triggers no warnings.

**No path-regularisation hysteresis (v5.4).** Earlier versions added a small penalty pulling
each year's build toward the previous year's (`0.001·‖x−x_{prev}‖²`) to stabilise the mix in
the flat cost valley. It introduced year-to-year *hysteresis* in the reported optimal mix
(path dependence), so it was removed; `prev_x` is now used only as a warm-start candidate,
which speeds convergence without biasing the objective. Cost impact is sub-$1/MWh; the optimal-mix
trajectory is now path-independent.

### 8.5 Capex vs opex decomposition

`delivered_cost_split` decomposes the delivered cost **per factor** into capex and opex,
which the breakdown figures (`fig4` at 70% RE, `fig5` at 85% RE) display as a stacked area
(solid fill = capex, hatched = opex, within each technology's colour):

- **Generation** — capex (capital recovery) vs O&M.
- **Battery** — capex (capital recovery incl. mid-life replacements) vs O&M.
- **Gas** — plant capacity capex; fuel + VOM + FOM; and carbon (shown separately).
- **Lost compute (shed)** — interruptible workloads only (zero for firm).

Battery and gas split cleanly (capital-recovery term vs FOM/fuel/carbon). Generation uses a
*bundled* learning-curve LCOE, so its capex/opex split uses an assumed O&M fraction
`om_frac_lcoe` (solar 15%, wind 25% of LCOE — solar/wind have no fuel, so LCOE ≈ capital
recovery + fixed O&M). This split affects only the capex-vs-opex attribution, **not** any
total cost. For RE-heavy systems most delivered cost is sunk capex; for gas it is fuel+carbon.

---

## 9. Monte Carlo Uncertainty

### 9.1 Weather uncertainty (50 synthetic years)

The simulator runs 50 independent synthetic weather years with freshly drawn cloud and wind sequences. For each $(C_{\text{sol}}, C_{\text{win}}, B)$ scenario, this produces a distribution of $\{f_{\text{gas}}^{(i)}\}_{i=1}^{50}$.

- **Mean** $\bar{f}_{\text{gas}}$: central estimate (used by default)
- **P90** $f_{\text{gas}}^{(90)}$: worst 1-in-10 weather year — conservative design; available via `use_p90=True`

**Parity-gap tornado (`run_tornado`, CLI `--tornado`, opt-in).** One-at-a-time
sensitivity of the *parity gap* (firm RE delivered LCOE − gas LCOE at a target year;
negative = RE wins) to the key assumptions — gas price, EU carbon ceiling, RE/gas WACC,
wind/solar resource, solar learning rate, battery capex. Emits a ranked tornado figure.
For EU 90% RE @ 2030, the largest movers are **gas price** and **RE WACC**, then **wind
resource**; **solar resource barely matters** (the firm 90% build is wind-dominated).
Reduced fidelity (coarser grid / fewer MC years, bounds widened to avoid cap-binding) —
magnitudes indicative, ranking robust.

**Robustness design (`design_p90`, opt-in).** The headline optimises the build against
the *mean* weather year. A firm, always-on datacenter arguably should instead size for a
bad year. With `design_p90=True` (CLI `--design-p90`), the optimiser is re-run against the
**P90 gas-fraction surface**, so the RE target is met even in a 1-in-10 weather year, and
the result is reported as a parallel `opt_delivered_p90` series (summary line + export
column) alongside the mean-designed headline. Because the P90 surface is pointwise ≥ the
mean and the P90-optimal build is feasible for the mean problem, the P90-designed cost is
**always ≥ the mean-designed cost** — empirically a **≈5–9% robustness premium** at 90% RE.
The mean-designed numbers remain the default headline; this only *adds* a series.

### 9.2 Cost uncertainty (80 lognormal draws)

At the optimal $(C_{\text{sol}}^*, C_{\text{win}}^*, B^*)$ from the central solve, capex parameters are perturbed:

$$\tilde{c} = c \cdot \exp\!\left(\sigma Z - \tfrac{\sigma^2}{2}\right), \qquad Z \sim \mathcal{N}(0,1)$$

The factor $-\sigma^2/2$ makes the draws mean-preserving: $E[\tilde{c}] = c$. The cost function is re-evaluated (no re-optimisation) for each draw, isolating cost uncertainty from the capacity decision.

**P10–P90 cost bands:**

| Technology | $\sigma$ | Implied P10–P90 range |
|------------|----------|-----------------------|
| Solar PV | 0.15 | ±30% of central |
| Onshore wind | 0.15 | ±30% |
| LFP battery | 0.12 | ±25% |

### 9.3 Geographic / siting band (fig1 shading, v5.6)

These §9.2 capex P10–P90 bands are reported in the summary table and the export
(`lcoe_p10`/`lcoe_p90`). The **shading in fig1**, however, is a different and — for an
off-grid *siting* decision — more decision-relevant quantity: the **resource/siting
range**. Each trajectory (every RE target *and* the gas-free H₂ system) is re-optimised at
a **poor** and a **good** site for the region (`RESOURCE_PRESETS[region]["low"]`/`["good"]`;
US 4.5↔6.8 kWh/m²/day & 6.5↔9.0 m/s, EU 3.2↔4.6 & 6.0↔8.5), and fig1 shades the min–max
envelope, with the `default`-resource line drawn as the central case inside it. This is what
gives the H₂ line a band too (it had none before — its trajectory computes a single central
value). It is a **range of choices** (where you build), *not* a probabilistic confidence
interval, and is labelled as such on the figure. Computed only with `resource_band=True`
(the headline suite / CLI `--resource-band`), at reduced MC since it is illustrative; the
central headline numbers are unchanged.

---

## 10. Parameter Tables

### System parameters

| Parameter | Default | Adjustable | Notes |
|-----------|---------|-----------|-------|
| `load_mw` | 100 MW | Yes | Scales absolute costs, not LCOE |
| `discount_rate` | 7% | Yes | Legacy flat WACC — **superseded by per-tech `wacc`** (§8.3); no longer used for costing |
| `wacc` (per tech) | 5.5% sol/wind · 7% batt · 9% gas | Yes | Differentiated real WACC (v5.3, §8.3) |
| `life_yr` (gen) | 30 yr solar · 25 yr wind | Yes | Asset life for the generation re-annualisation (§8.3) |
| `project_lifetime_yr` | 20 yr | Yes | Battery analysis / replacement horizon |
| `n_mc_weather` | 50 | Yes | Synthetic weather years (more than v4's 30: Dunkelflaute widens tails) |
| `grid_steps` | 21 | Yes | Per-axis steps; total = $21^3 = 9{,}261$ (v5.1: was 15) |
| `c_sol_max` | 18× (US), 22× (EU) | Yes | Sized so the grid resolves the real optima and firm high-RE doesn't bind (§8.4) |
| `c_win_max` | 18× (US), 20× (EU) | Yes | Firm (no-shed) high-RE is wind-heavy (EU 90% → ~5×; ≥95% infeasible) |
| `storage_hours_max` | 60h (US & EU) | Yes | Optima ~5–9h; far below the bound |
| `wind_solar_corr` | 0.0 (US), −0.35 (EU) | Yes | Contemporaneous copula ρ |
| `syn_loading` | 0.50 | Yes | Synoptic factor loading λ (§4.6) |
| `syn_persistence` | 0.82 (US), 0.85 (EU) | Yes | Synoptic AR(1) φ; episode length ≈ 1/(1−φ) days |
| `n_sites` | 1 | Yes | Geographic portfolio size (§4.7); 1 = single-site headline |
| `site_synoptic_corr` | 0.70 | Yes | Pairwise cross-site Dunkelflaute correlation $c$ (§4.7); used only if `n_sites`>1 |
| `firm_gas_sizing` | "mean" | Yes | Firm gas-plant sizing: "mean" annual-peak (headline) or "p90" 1-in-10 peak (v5.7, §7) |
| `solar_performance_ratio` | 1.0 | Yes | Solar system PR (v5.7); 1.0 = CF anchored to the imported-LCOE basis; <1 derates (§4.2) |

Per-technology deployment (`TechParams`/`BatteryParams`): `additions_growth_rate` ($g_0$),
`additions_growth_decay` ($\delta$, **0.85** for shipped solar/wind/battery; 1.0 = legacy
constant growth), and `additions_growth_floor` (0) define the S-curve cumulative-capacity path
(§3). The LDES presets leave $\delta=1$ (their conservative learning is documented separately).

**Custom sites & real weather (data, not code).** Two seams let a user re-point the model
without editing source. `load_site_config(path)` (CLI `--site PATH.json`) builds a region
bundle from a small JSON file that inherits a built-in region's tech/battery/SMR/PPA
defaults (`based_on`) and overrides only the location-specific knobs (resource, any
`GasParams` field, any `SystemParams` field above); unknown keys raise. `--weather PATH.npz`
(loader `weather.load_weather_traces`, builder `tools/ingest_weather.py`) drives the dispatch
with real ERA5/NSRDB hourly-CF years instead of the synthetic generator (§12).

---

## 11. Key Results

Headline = **FIRM (always-on)** workload: gas backup sized to 100% of load, nothing shed,
capped opex. All values from `output/*_results.json` (June 2026; 21³ grid, per-tech WACC,
v5.4 battery augmentation, v5.5 CF recalibration, **v5.8 input recalibration** —
turbine-shortage gas capex, BNEF-2025 batteries, Lazard-2025 wind, real-terms WACC; and the
**v6.0 real-weather headline** — see below).
**v6.0 — measured weather (June 2026).** The headline is now driven by **real ERA5 reanalysis**
(11 years, 2015–2025) at a single representative data-center market per region — **US: ERCOT
Texas; EU: France** — instead of the synthetic generator. One real site (no spurious
geographic smoothing a single off-grid datacenter does not get), with its actual hourly cloud /
Dunkelflaute / sun↔wind structure and real interannual spread. The imported Lazard LCOEs are
**re-levelled** from their synthetic reference CF to each site's real CF (holding $/kW capital
fixed; §4.8), so cost and energy still refer to the same plant. Realised CFs: **US solar 0.265 /
wind 0.320** (Texas — close to the prior synthetic 0.23/0.33, so the US story barely moves),
**EU solar 0.183 / wind 0.135** (France — markedly poorer wind than the prior 0.16/0.29, which
makes high-RE Europe materially dearer). The synthetic generator still backs every sensitivity /
siting analysis (resource band, tornado, `--resource`), which need the resource as a free knob.
Premium/AI workloads collapse to firm under the economic shed test, so these are the relevant
numbers for any valuable datacenter. (Tables regenerated from the export via
`tools/regen_doc_tables.py`.)

### US — Firm (always-on)

| RE target | 2025 ($/MWh) | 2030 | 2035 | 2040 | vs gas 2025 | Crossover |
|-----------|-------------|------|------|------|-------------|-----------|
| 70% | 82.9 | 71.6 | 65.1 | 60.4 | +42% | >2040 |
| 80% | 88.9 | 72.7 | 65.1 | 60.4 | +52% | >2040 |
| 85% | 100.3 | 82.6 | 73.8 | 68.4 | +72% | >2040 |
| 90% | 137.3 | 111.4 | 98.6 | 90.6 | +135% | >2040 |
| **Gas** | **58.4** | **58.4** | **58.4** | **58.4** | — | — |
| *Grid+RE PPA (ref.)* | *85* | *69* | *62* | *57* | — | — |

High-RE US never beats cheap untaxed gas within the horizon — $4/MMBtu → ~$58/MWh even at the
v5.8 turbine-shortage capex is a very low, very stable baseline, and high-RE needs heavy
overbuild to ride out multi-day lulls. On **real ERCOT-Texas weather** (v6.0) the picture is
essentially unchanged from the prior synthetic headline — Texas's measured resource (solar
0.265 / wind 0.320) sits right where the Lazard cost basis assumed, so re-levelling barely moves
the lines and **moderate 70–80% RE still does not cross $58 gas by 2040** (the 70% line bottoms
at ≈$60/MWh, a slightly nearer near-miss than the synthetic ≈$62). The crossing happens only
against a stressed gas baseline (the dot-dashed +60%-fuel reference line on fig1, ≈$74/MWh,
which 70–80% RE beats by ~2031–32) — i.e. US RE competitiveness hinges on gas *not* staying at
$4 forever. That this holds on *measured* Texas weather — including its real Dunkelflaute and
interannual spread — is a stronger statement than the synthetic version it replaces.

**95% RE is omitted: it is infeasible for a firm, battery-only off-grid system.** Over the
whole 21³ build grid the maximum achievable annual RE fraction is ≈**0.94 (EU)** / ≈**0.95 (US)**:
in a multi-day Dunkelflaute neither sun nor wind produces, and the $\min(1,4/B)$ battery
power de-rating (§5.4) means a long-duration battery cannot deliver enough *power* to bridge
days, so a few percent of annual energy always falls to gas regardless of overbuild. Pushing
past ~94% needs long-duration storage or H₂ firming — the `--ldes` / `--firming h2` overlays
(§7.5, fig6). The optimiser now emits an explicit `[WARN] … INFEASIBLE` if a requested target
exceeds this ceiling, rather than silently reporting the penalty-saturated point.

### Europe — Firm (always-on)

| RE target | 2025 ($/MWh) | 2030 | 2035 | 2040 | vs gas 2025 | Crossover |
|-----------|-------------|------|------|------|-------------|-----------|
| 70% | 135.2 | 119.1 | 115.8 | 113.7 | +11% | **~2027** |
| 80% | 174.2 | 148.0 | 139.2 | 133.7 | +43% | **~2033** |
| 85% | 225.9 | 187.7 | 171.7 | 161.6 | +86% | **~2040** |
| 90% | 278.0 | 237.4 | 225.0 | 219.3 | +129% | >2040 |
| **Gas** | **121.5** | **131.1** | **150.6** | **162.6** | — | — |
| *Grid+RE PPA (ref.)* | *117* | *96* | *86* | *81* | — | — |

EU gas is expensive and rising (carbon → logistic path toward $200/tCO₂), so even on poor
weather *moderate* RE stays competitive — but **real French weather (v6.0) makes deep
decarbonisation markedly dearer and pushes high-RE parity out of the horizon.** France's
measured wind CF is only **0.135** — far below the prior synthetic EU 0.29 — so wind is both
weaker and, after re-levelling, ~2× costlier per MWh, and riding out multi-day winter lulls now
takes heavy solar+wind overbuild. The result: **70% RE still beats rising EU gas by ~2027**
($135/MWh in 2025 → $114 by 2040, crossing the $122→$163 gas line ~2027), and **80% crosses
~2033**; but **85% reaches parity only ~2040 and 90% not within the horizon** (90% is $278/MWh
in 2025, still $219 in 2040 vs $163 gas). This is a real shift from the prior synthetic headline
(which put 90% parity ~2033): the synthetic 0.29 wind was really a *good* EU site, and France —
a fast-growing, nuclear-heavy data-center hub — is a genuinely poorer-wind one. The high-level
history still holds — v4's "90% parity Q2 2025" was an artefact of free load-shedding and no
multi-day Dunkelflaute; the honesty fixes pushed it to the 2030s; v5.5–v5.9 settled the
*synthetic* 90% line near ~2033 — and v6.0 now shows that on a real poorer-wind hub, 90% off-grid
RE simply does not reach cheap-firming parity by 2040. (95% RE omitted — infeasible for the
battery-only firm system, see the US note above.)

**Optimal EU 90% RE build on real French weather (2025):** ≈ **9.9× solar + 9.0× wind + 6h
storage** — much heavier than the prior synthetic ~6.6× solar + 5.0× wind, because France's poor
real wind (CF 0.135) forces large overbuild of both to cover multi-day winter Dunkelflaute.
Storage stays ~6h: the binding constraint is multi-day lull *energy*, which generation overbuild
covers more cheaply than batteries (a 5-day lull would need ~120h of storage; see below).

### Why so much overbuild but only ~6h of battery?

Generation overbuild and storage solve *different* problems, with very different costs:

*   **~6h battery handles the daily cycle** (store midday solar for the evening). It is cheap
    and high-value, and ~6h is the optimum (a grid-independent 1D scan confirms the true
    optimum is 5.5–6h; the curve is flat to ±$2/MWh around it).
*   **Multi-day Dunkelflaute cannot be solved with batteries.** Covering a 5-day lull would
    need ~120h of storage (~$500+/MWh delivered) — absurd. The cheap way to handle multi-day
    shortfall is to **overbuild generation** (so even a poor day scrapes enough) plus lean on
    the small gas allowance for the deepest hours.
*   So the optimum is: modest battery for diurnal shifting + large overbuild as cheap insurance
    against multi-day lulls + a little gas. The storage bound (60h) never binds; ~6h is a true
    interior optimum, not a cap.

---

## 12. Known Limitations

**v5.8 full-machinery audit (June 2026) — and the v5.9 hardening that closed it.** The
weather generator, dispatch/MC layer, and optimisation/cost-composition layer were
independently audited (analytically and empirically). The core machinery verified clean:
hourly energy balance closes to machine precision with efficiencies applied exactly once; the
battery power de-rating is identical in dispatch and cost; FEC counting is exact against
analytic test cases; the copula/AR(1) algebra is variance-normalised and stationary; the
exterior penalty provably dominates the measured RE shadow prices (so constraints bind, not
leak); per-tech WACC routing has no legacy-rate leak; and the P10/P90 bands measure capex
uncertainty at a frozen build, as documented. Small opt-in-path bugs found and fixed in v5.8
(the gas backup-capacity clip at 1.0× load; `opt_re` omitting the firm shed add-back; the
LDES/H₂ paths ignoring `solar_performance_ratio`). The audit's substantive accuracy findings
were then **implemented in v5.9** (changelog above; all empirically validated and
regression-locked): the realized wind–solar correlation (was ≈half the configured ρ, biased
positive → now ≈ρ up to inherent Pearson attenuation), the narrowed cloud marginal (deep
overcast 1.3% → the exact Beta 5.2%), the ~1%-short US solar-CF anchor (now exact), the
year-boundary lull severing (mid-summer seam), the overstated multi-site diversification
(`site_local_corr`), the interpolated-surface optimum (now confirmed on exact dispatch), and
the 0.5% feasibility tolerance (now 0.2%).

Remaining known accuracy items, with directions of bias (all small):

- **Pearson attenuation of ρ** through the monotone Beta/power-curve transforms: configured
  −0.35 realizes ≈−0.28 (rank correlation is exact). Calibrate against an observed ERA5
  CF-correlation by targeting the realized value (~0.8× the configured).
- **Wind seasonal modulation** (±12% winter amplification) passes through the cubic power
  curve, so it preserves mean *speed* exactly but shifts regional CF by ≈∓1.5% (Jensen) —
  the simulated CFs that anchor the cost basis already include this.
*(The audit's two remaining implementation items — the P90 sizing incoherences and the fig1
H₂ warm-start-only optimisation — were closed in v5.9.1, along with the last duplicated LDES
cost formula and the H₂/LDES paths' single-site-only weather seam.)*

**Optimiser grid resolution (addressed in v5.1).** The optimiser interpolates trilinearly
into a precomputed `grid_steps`³ gas-fraction surface. v5 used 15 nodes over oversized
bounds (EU battery 0–168h → 12h spacing), which pinned the optimum onto coarse nodes and
overstated high-RE cost ~15–30% (the "flat line" artefact). v5.1 right-sizes the bounds
(§8.4) and uses 21 nodes, and a **boundary-binding guard** in `optimal_cost_3d` now prints a
warning whenever an optimum reaches a max bound — so cap-binding can no longer hide. Residual
node-quantisation (~3h on battery, ~0.6× on wind) is within the shallow part of the cost
curve. If the guard fires, raise the corresponding `*_max` in `SystemParams`.

**Shedding is a binary per-year economic decision.** The interruptible model sheds iff
`shed_penalty < gas variable cost` (fuel+carbon+VOM, evaluated at the CCGT heat rate). Because
gas fuel+carbon is flat within a year, this is all-or-nothing per (workload, year): a workload
sheds the full interruptible slice of every gas-residual hour, or none. This (i) creates a
discontinuity at the threshold and (ii) ignores that shedding also saves gas *capacity*, not
just fuel — so it slightly under-credits shedding for workloads near the threshold. Note that
moving the decision *hour-by-hour* would **not** change results here: the marginal serving
cost (gas fuel+carbon+VOM) is constant within a year, so an hourly rule sheds exactly the
same hours as the annual one. Hourly shedding only starts to matter once the marginal cost
varies within the year — e.g. an hourly electricity/gas price or a time-varying carbon signal,
neither of which this off-grid model carries. The genuinely-missing credit is the gas-capacity
saving (ii), not the time resolution. The headline FIRM results are unaffected (they never shed).

**Flexibility sweep fidelity.** `--flex-sweep` re-runs the dispatch per (interruptible × shed
penalty) point at reduced fidelity (coarser grid, fewer MC years) and wider bounds. Treat its
absolute LCOE as indicative; the *shape* of the trade-off surface is the point.

**Synoptic factor is calibrated, not fitted.** The Dunkelflaute structure (§4.6) uses plausible loadings/persistence ($\lambda=0.5$, $\varphi\approx0.82$–0.85) rather than values fitted to multi-decade ERA5 reanalysis at a specific site. It restores realistic multi-day clustering and correct directionality, but the exact frequency/depth of week-scale lulls — which sets high-RE storage/backup — should be validated against site reanalysis before siting decisions. **This is the single largest accuracy gap** and the highest-value next improvement (the single-site assumption is addressed separately by the §4.7 portfolio). `tools/calibrate_synoptic.py` now *fits* λ/φ/ρ (and, for multi-site input, `site_synoptic_corr`) from a real weather `.npz` — a fast moment estimator (monotone, biased toward zero; for ranking and starting values, refine with simulated moments) that converts these from assumptions into measured inputs once a reanalysis feed is wired. The integration seam for closing it is wired end to end: `ChronologicalSimulator(..., weather_years=...)` (CLI `--weather PATH.npz`, loader `weather.load_weather_traces`) dispatches supplied real ERA5/NSRDB hourly CF years instead of the synthetic generator, leaving the optimiser, costing and figures unchanged; and `tools/ingest_weather.py` converts provider data (hourly-CF CSVs, with the documented ERA5/NSRDB → CF recipe) into that `.npz` — so wiring a reanalysis feed is a data step, not a code change.

**Gas CF approximation.** The gas backup LCOE uses $f_{\text{gas}}$ as both the gas plant capacity factor and the energy fraction; capacity capital is separately peak-scaled (firm → 100% of load). Reasonable since dispatch runs gas only when battery (and any shedding) are exhausted.

**Spatial diversification — now modelled, off by default (§4.7).** The headline assumes a
*single* co-located site (worst-case correlation). A real portfolio diversifies across
sites, which softens Dunkelflaute and lowers effective gas fractions — a large effect (≈40%
off EU 90%-RE delivered cost for 3–5 sites; §4.7). `n_sites`/`--sites` adds a multi-site
portfolio that preserves the mean CF exactly; it is **opt-in** (default `n_sites=1`), so the
headline remains single-site. The remaining gap is calibration of the inter-site correlation
`site_synoptic_corr`, which (like the synoptic factor) should be fitted to reanalysis for a
specific portfolio rather than assumed.

**Constant load profile.** Datacenter load is flat at $P_{\text{load}}$ MW; sub-hourly variation and maintenance windows are not modelled.

**Gas supply reliability.** Gas is assumed always available at nameplate capacity. A truly off-grid site needs on-site fuel storage or pipeline access; neither constraint is modelled (this would *raise* the firm backup cost and help the RE case).

**Solar resource is an average-site assumption.** The headline irradiance ($\bar I = 5.5$ kWh/m²/day US, 3.8 EU) gives an effective solar CF of **0.227 US / 0.158 EU** (v5.5; §4.2) — the US value sits inside the Lazard utility-scale band (0.20–0.30) and the EU value just below it, consistent with weaker northern-European irradiance. These represent an *average-cloudiness* site; clear/arid sites (US Southwest, Spain) are sunnier, so set `mean_irr=6.5`–7.0 for CF ≈ 0.27–0.29 (see the `good` resource preset and `--resource-sweep`).

**Wind model is land-based.** IEC Class II power curve and Weibull $k=2.1$ (onshore). Offshore would need a separate parameterisation ($k\approx2.5$, lower cut-in, higher rated wind).

**Generation degradation is embedded, not double-counted.** The exogenous Lazard `lcoe_today` already includes module degradation and inverter replacement, so the model does **not** add them again (that would double-count). A `TechParams.degradation_per_yr` knob exists (default **0**) for use with degradation-free input LCOEs; when >0 it inflates delivered LCOE by $1/(1-\tfrac12\,\text{deg}\cdot\text{life})$. Battery degradation, by contrast, *is* modelled explicitly (§6).

**Capacity factors are CF-basis-consistent (v5.5).** An imported LCOE is capex+FOM
levelised over a *specific* capacity factor, so the dispatch must simulate that same CF or
the cost basis is internally inconsistent. Through v5.4 it was not: the default resource
simulated US solar ≈0.15 / wind ≈0.22, roughly **half** the CF (utility solar 0.20–0.30,
onshore wind 0.30–0.55) that the Lazard v18 generation LCOEs it imports are quoted at —
inflating required overbuild and biasing high-RE cost upward. v5.5 fixes both halves: the
solar cloud **double-count** (effective CF 0.153 → **0.227** US, 0.105 → **0.158** EU) and
the **wind power curve** (rated 13 → 11 m/s; CF 0.22 → **0.33** US, 0.18 → **0.29** EU).
The US CFs and EU wind now sit inside Lazard's bands; EU solar (0.158) sits just below the US
solar band and is matched instead to a EU-specific solar LCOE levelised at that lower CF — so
the imported \$/MWh and the simulated MWh refer to the same plant region by region. Net effect
on the headline: high-RE delivered LCOE falls materially
(≈20–30% at 90% RE) and EU parity moves earlier; the *direction* of the prior "conservative
CF" caveat was right, but the magnitude was large and the inconsistency is now removed.

**Resource-quality sensitivity (`RESOURCE_PRESETS`, `--resource` / `--resource-sweep`).**
Each region has a `default` (CF-basis-consistent average site) and a `good` (modern
well-sited) preset — US `good` = 6.8 kWh/m²/day & 9.0 m/s → simulated CF solar 0.26 / wind
0.45; EU `good` = 4.6 & 8.5 → 0.19 / 0.41. `run_resource_sensitivity` (CLI
`--resource-sweep`) re-runs the firm model at both levels and tabulates LCOE and parity. The
good resource lowers 90%-RE LCOE further and pulls EU parity earlier; whether **US** high-RE
crosses cheap untaxed gas within the horizon is now close and resource-dependent rather than
a hard never — re-run the suite for the current crossover. (`default` exactly equals the
headline resource, so it defines the published numbers.)

### Accuracy summary — what to trust

Treat this as a **stylised techno-economic model: trust directional comparisons, not absolute
numbers to better than ~±20–30%.** Robust conclusions: cheap untaxed US gas is hard to beat —
under the v5.7 deployment recalibration **no RE target crosses flat $4 gas within the horizon**
(the moat *strengthens*), and US RE wins only if gas rises (the stressed-gas line); carbon-priced
EU gas is beatable, with parity moving from ~2025 (70–80% RE) to ~2030 (90% RE); ≥95% RE is
infeasible for the battery-only firm system (~94% ceiling), needing LDES/H₂ to close;
high-RE economics are overbuild-and-gas-dominated, not battery-dominated; and demand
flexibility only helps when compute is worth less than gas — i.e. rarely for premium AI. Not to
be trusted as precise: specific parity *years* and $/MWh (synthetic uncalibrated weather, my own
battery-cost basis, and the **deployment/learning extrapolation to 2040** — v5.7 puts the
central deployment on a defensible S-curve, ~15.6 TW solar, but the low↔high band in §3 still
spans ~±$5–10/MWh at 2040, and the assumed flat US gas price is a comparable swing). The biggest
remaining lever to tighten accuracy is replacing the synthetic weather with real multi-year
ERA5/NSRDB reanalysis.

---

## 13. References

- **[Lazard LCOE+ v18 (2025)](https://www.lazard.com/perspective/levelized-cost-of-energy-levelized-cost-of-storage-and-levelized-cost-of-hydrogen/).** Levelized Cost of Energy Analysis, version 18.
- **[Way, R. et al. (2022)](https://doi.org/10.1016/j.joule.2022.08.009).** "Empirically grounded technology forecasts and the energy transition." *Joule* 6(9), 2057–2082. DOI: 10.1016/j.joule.2022.08.009
- **[NREL ATB 2024](https://atb.nrel.gov/electricity/2024/index).** Annual Technology Baseline. National Renewable Energy Laboratory.
- **[NREL BTM 2023](https://www.nrel.gov/docs/fy23osti/85332.pdf).** Behind-the-Meter Battery Storage Costs. NREL Technical Report.
- **[IEA WEO 2024](https://www.iea.org/reports/world-energy-outlook-2024).** World Energy Outlook. International Energy Agency.
- **[GWEC (2025)](https://gwec.net/global-wind-report-2025/).** Global Wind Report 2025.
- **[IRENA (2025)](https://www.irena.org/publications/2025/Jul/Renewable-power-generation-costs-in-2024).** Renewable Power Generation Costs in 2024.
- **[Ember (2025)](https://ember-climate.org/insights/research/global-electricity-review-2025/).** Global Electricity Review 2025.
- **[OWID](https://ourworldindata.org/learning-curves).** Our World in Data — Energy Learning Curves.
- **[Xu, B. et al. (2018)](https://doi.org/10.1109/TSG.2016.2578950).** "Modeling of Lithium-Ion Battery Degradation for Cell Life Assessment." *IEEE Transactions on Smart Grid* 9(2), 1131–1140. DOI: 10.1109/TSG.2016.2578950
- **[IPCC AR6 (2022)](https://www.ipcc.ch/report/ar6/wg3/).** Working Group III, Table A.III.2 (emissions factors).
- **[EU ETS (2025)](https://climate.ec.europa.eu/eu-action/eu-emissions-trading-system-eu-ets_en).** EUA spot price, May 2025; Fit-for-55 trajectory projections.
- **[EIA (2025)](https://www.eia.gov/naturalgas/weekly/).** Natural Gas Weekly Update. Henry Hub spot price.
- **[Wiser, R. et al. (2021)](https://doi.org/10.1038/s41560-021-00810-z).** "Expert elicitation survey predicts 37%–49% declines in wind energy costs by 2050." *Nature Energy* 6, 555–565. DOI: 10.1038/s41560-021-00810-z
- **[Copernicus ERA5](https://cds.climate.copernicus.eu/cdsapp#!/dataset/reanalysis-era5-single-levels).** ECMWF hourly reanalysis — solar irradiance and wind speed.
- **[NREL NSRDB](https://nsrdb.nrel.gov/).** National Solar Radiation Database.
- **[Epoch AI (2025)](https://epoch.ai/blog/is-almost-everyone-wrong-about-americas-ai-power-problem).** "Is almost everyone wrong about America's AI power problem?" *Gradient Updates.*
- **[BloombergNEF (2025)](https://about.bnef.com/blog/lithium-ion-battery-pack-prices-hit-record-low/).** Battery Price Survey 2025.
