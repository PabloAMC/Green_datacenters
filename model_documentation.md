# Off-grid Datacenter LCOE Model — Technical Documentation

**Model version:** v5.3  
**Code file:** `datacenter_lcoe.py`  
**Last verified:** June 2026  
**All numerical values cross-checked against model output (`scratch/v53_run.log`)**

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

## Table of Contents

1. [Overview and scope](#1-overview-and-scope)
2. [Decision variables and optimisation](#2-decision-variables-and-optimisation)
3. [Cost learning curves — Wright's Law](#3-cost-learning-curves--wrights-law)
4. [Weather generation](#4-weather-generation)
5. [Chronological dispatch](#5-chronological-dispatch)
6. [Battery degradation and replacement NPV](#6-battery-degradation-and-replacement-npv)
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
- Accounts for battery degradation, mid-life replacement NPV, and depth-of-discharge effects.
- Defaults to a **firm (always-on)** datacenter with gas backup sized to 100% of load, and optionally models interruptible workloads that shed load only when the value of lost compute is below the gas variable cost (§5.5).
- Covers two regions (US, EU) with region-specific resource, gas price/carbon, and battery soft-costs.
- Uses dynamic backup generator capacity sizing (firm → 100% of load).

**Normalisation:** All quantities are per MW of constant datacenter load. Delivered costs are in real 2025 USD per MWh of load served.

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

The 3D optimisation is solved by multi-start Nelder-Mead with six starting points. The objective function evaluates via trilinear interpolation into a precomputed $21^3 = 9{,}261$-scenario dispatch surface (v5.1; §8.4), making each evaluation near-instantaneous.

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
high-RE optimum is wind-heavy in the EU — 95% RE reaches ≈15× wind — so the EU wind
bound is set to 20× to keep the optimum interior.)

---

## 3. Cost Learning Curves — Wright's Law

### Cumulative capacity trajectory

$$Q_t = Q_0 + \sum_{i=1}^{t} \Delta Q_0 \cdot (1 + g)^i$$

where $Q_0$ is cumulative installed capacity in 2025, $\Delta Q_0$ is 2025 annual additions, and $g$ is the growth rate of annual additions.

### Wright's Law

$$\text{LCOE}(t) = \text{LCOE}_0 \cdot \left(\frac{Q_t}{Q_0}\right)^{\log_2(1-\text{LR})}$$

**Verification:** For solar, $Q_{2040}/Q_{2025} = 38{,}466/2{,}900 = 13.3$ — corresponding to $\log_2(13.3) = 3.74$ doublings. With LR = 0.30, cost ratio = $(1-0.30)^{3.74} = 0.263$. So $\text{LCOE}_{2040} = 52 \times 0.263 = \$13.7$/MWh. ✓ (model output: $13.7/MWh)

### Solar LCOE trajectory (US, verified values)

| Year | Cum. capacity (GW) | LCOE ($/MWh) |
|------|--------------------|--------------|
| 2025 | 2,900 | 52.0 |
| 2028 | 5,496 | 37.4 |
| 2030 | 7,940 | 31.0 |
| 2035 | 18,077 | 20.3 |
| 2040 | 38,466 | 13.7 |

*These are the raw Wright's-Law learning-curve LCOEs (quoted at the legacy 7% WACC). v5.3
re-annualises them at the solar/wind WACC of 5.5% via `rewacc_lcoe` (§8.3), multiplying the
**delivered** generation LCOE by ≈0.876 (solar) / 0.902 (wind) — e.g. solar 2025 $52 → $45.5
delivered. The learning shape is unchanged (constant multiplier).*

### Technology learning parameters

| Technology | LCOE₀ ($/MWh) | LR | Q₀ | ΔQ₀ | g |
|------------|----------------|-----|-----|------|---|
| Solar PV (US) | 52 | 30% | 2,900 GW | 650 GW/yr | 15%/yr |
| Solar PV (EU) | 60 | 30% | 2,900 GW | 650 GW/yr | 15%/yr |
| Onshore Wind (US) | 50 | 17% | 1,300 GW | 167 GW/yr | 10%/yr |
| Onshore Wind (EU) | 48 | 17% | 1,300 GW | 167 GW/yr | 10%/yr |
| LFP Battery (energy, US) | 180 $/kWh | 19% | 1,800 GWh | 600 GWh/yr | 18%/yr |
| LFP Battery (power, US) | 140 $/kW | 19% | (same) | (same) | 18%/yr |
| LFP Battery (energy, EU) | 180 $/kWh | 19% | 1,800 GWh | 600 GWh/yr | 18%/yr |
| LFP Battery (power, EU) | 175 $/kW | 19% | (same) | (same) | 18%/yr |

**Battery cost basis (v5).** Installed cost is split into an **energy** component
($/kWh, scales with MWh) and a **power/BOS/EPC** component ($/kW, scales with MW).
LFP cells are a globally traded commodity, so the energy component is
**region-invariant** ($180/kWh); only the power/BOS component carries a regional
soft-cost premium (US $140/kW; EU $175/kW, reflecting higher labour/permitting and
no IRA-equivalent credit). A 4h system therefore costs $4{\times}180 + 140 = \$860$/kW-load
(US) ≈ $215/kWh installed, vs $895/kW-load (EU). This replaces v4's energy costs
of $220/kWh (US) and $80/kWh (EU), which were on inconsistent bases (a system price
vs. a near-cell price) and made EU storage implausibly ~2.75× cheaper.

**Sources:** Lazard LCOE+ v18 (2025), Way et al. *Joule* (2022), OWID learning curves, NREL ATB 2024, BloombergNEF (2024–25), Ember BESS Report (2025).

### SMR cost trajectory

Linear interpolation from FOAK to NOAK, then constant:

$$\text{LCOE}_{\text{SMR}}(t) = \begin{cases} \text{LCOE}_{\text{FOAK}} + (\text{LCOE}_{\text{NOAK}} - \text{LCOE}_{\text{FOAK}}) \cdot t/T_{\text{NOAK}} & t < T_{\text{NOAK}} \\ \text{LCOE}_{\text{NOAK}} & t \geq T_{\text{NOAK}} \end{cases}$$

| Parameter | US | EU |
|-----------|----|----|
| FOAK ($/MWh) | 120 | 140 |
| NOAK ($/MWh) | 85 | 85 |
| Years to NOAK | 10 | 12 |

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
| PPA energy (2025) | 45 | 72 | LevelTen PPA Price Index (2024–25) |
| Grid delivery / network | 22 | 33 | Large-C&I network tariffs (EIA / ENTSO-E) |
| Firming / balancing premium | 8 | 12 | Lazard v18 firming adders |
| **All-in 2025** | **75** | **117** | |
| **All-in 2040** (energy follows solar LR) | **≈42** | **≈64** | |

**What it shows.** Grid+PPA sits well below the off-grid high-RE optimum in both
regions and below carbon-priced EU off-grid gas — i.e. **going off-grid is itself a
cost premium**, paid for siting independence. This context was missing when off-grid RE
was compared only to off-grid gas. **Caveat:** this represents *annual-volumetric* RE
matching (RECs/PPA netted over the year), **not** hour-by-hour 24/7 carbon-free energy;
true 100% 24/7 CFE on the grid would cost more and is not modelled here.

---

## 4. Weather Generation

### 4.1 Solar — clear-sky profile

The deterministic 8760-hour clear-sky trace:

$$\text{CF}_{\text{cs}}(h) = A \cdot \max\!\left(0,\; \sin\!\left(\tfrac{(h_{\text{od}} - 6)\pi}{12}\right)\right)^{1.1} \cdot \left[1 + 0.35\cos\!\left(\tfrac{2\pi(d-172)}{365}\right)\right]$$

where $h_{\text{od}}$ is hour-of-day (0–23) and $d$ is day-of-year (0–364, solstice at $d=172$). The scalar $A$ normalises the annual mean to $\bar{I}/24$:

$$\overline{\text{CF}}_{\text{cs}} = \frac{\bar{I}}{24}$$

**Irradiance inputs:**

| Region | $\bar{I}$ (kWh/m²/day) | Clear-sky CF | Source |
|--------|------------------------|--------------|--------|
| US (default) | 5.5 | 0.229 | NREL NSRDB |
| EU (default) | 3.8 | 0.158 | EU JRC PVGIS |

### 4.2 Solar — stochastic cloud attenuation

Daily cloud cover $\xi_d \in [0,1]$ drawn from a Beta distribution with AR(1) persistence:

$$\xi_d^* \sim \text{Beta}(\alpha=3,\; \beta=1.5) \qquad \mu = \frac{3}{4.5} = 0.667$$

$$\xi_d = \rho_c \cdot \xi_{d-1} + (1-\rho_c) \cdot \xi_d^*, \qquad \rho_c = 0.45$$

**Hourly solar capacity factor:**
$$\text{CF}_{\text{sol}}(h) = \text{CF}_{\text{cs}}(h) \cdot \xi_{\lfloor h/24 \rfloor}$$

**Resulting effective capacity factors (verified from simulation):**

| Region | Clear-sky CF | Beta mean | Effective CF |
|--------|--------------|-----------|--------------|
| US | 0.229 | 0.667 | **0.152** |
| EU | 0.158 | 0.667 | **0.105** |

These are consistent with moderately cloudy mid-latitude sites. For clear, arid sites (US Southwest, Spain), increase $\bar{I}$ to 6.5–7.0 to raise CF toward 0.22–0.28.

### 4.3 Solar-wind Gaussian copula correlation

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

**v5 note.** $\rho$ is now the *contemporaneous* (same-day) correlation, realised as the **residual** part of the two-factor model in §4.6: a persistent synoptic factor loads positively on both wind and solar (creating joint multi-day lows), while the residual pair carries the cyclonic $\rho<0$. The net same-day correlation still equals $\rho$.

### 4.4 Wind — mean-reverting AR(1)-on-quantiles model

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

| Region | $\bar{v}$ (m/s) | Simulated CF | Analytic Weibull CF |
|--------|-----------------|--------------|---------------------|
| US | 7.5 | **0.216** | 0.221 |
| EU | 7.0 | **0.176** | 0.180 |

The small residual gap (2%) comes from the seasonal modulation — winter amplification is partly offset by summer reduction, with asymmetric power curve response.

### 4.5 Wind power curve (IEC Class II)

$$P_{\text{wind}}(v) = \begin{cases} 0 & v < v_{\text{ci}} \text{ or } v > v_{\text{co}} \\ \left(\frac{v - v_{\text{ci}}}{v_r - v_{\text{ci}}}\right)^3 & v_{\text{ci}} \leq v < v_r \\ 1 & v_r \leq v \leq v_{\text{co}} \end{cases}$$

| Parameter | Value | Meaning |
|-----------|-------|---------|
| $v_{\text{ci}}$ | 3.5 m/s | Cut-in speed |
| $v_r$ | 13.0 m/s | Rated speed |
| $v_{\text{co}}$ | 25.0 m/s | Cut-out speed |

Consistent with a modern 3–5 MW turbine (Vestas V150, Siemens SG 5.0).

### 4.6 Synoptic "Dunkelflaute" factor (v5)

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

---

## 5. Chronological Dispatch

### 5.1 3D precomputed grid

Unlike a simple 2D grid over $(C, B)$ for a single source, v4 runs combined dispatch over all $15^3 = 3{,}375$ combinations of $(C_{\text{sol}}, C_{\text{win}}, B)$ simultaneously, vectorised in numpy. This correctly accounts for solar and wind feeding the same load and battery — the critical fix over the independent-surface approximation.

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
sharp threshold at the gas variable cost (~$120/MWh): below it, more interruptibility →
lower cost and earlier parity (down to ~$32/MWh, parity 2025 at 95% interruptible /
$25 compute); at or above it, the system is firm ($174/MWh, parity 2035). See
`figs/eu_flex_heatmap.png`.

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
*   The user picks $R$ (70/80/90/95%); the optimiser finds the least-cost build meeting it.

This replaces the v4 definition, where load silently dropped during deficits still counted
as "renewable," inflating the RE fraction by ~8 points.

---

## 6. Battery Degradation and Replacement NPV

### 6.1 LFP degradation model

Battery capacity fades through two mechanisms:

$$\text{cap}(t) = 1 - \delta_{\text{cal}} \cdot t - \delta_{\text{cyc}} \cdot N_{\text{FEC,eff}}(t)$$

| Parameter | Symbol | Value | Source |
|-----------|--------|-------|--------|
| Calendar degradation | $\delta_{\text{cal}}$ | 0.020/yr | NREL BTM 2023; Xu et al. (2018) |
| Cycle degradation | $\delta_{\text{cyc}}$ | $5 \times 10^{-5}$/FEC | ~4,000 FEC to 80% at 100% DoD (LFP) |
| DoD exponent | $\beta$ | 0.60 | LFP Wöhler curve approximation |
| Replacement threshold | | 80% | Industry warranty standard |

**Note:** $\delta_{\text{cyc}} = 5\times10^{-5}$/FEC corresponds to 4,000 full cycles to 80% capacity — the standard LFP specification (CATL, EVE Energy datasheets).

### 6.2 DoD-weighted effective FEC

A cycle at depth-of-discharge $d$ does $d^\beta$ times the damage of a reference 100% DoD cycle (Wöhler curve approximation):

$$\text{FEC}_{\text{eff}} = d^\beta \qquad \text{(per actual cycle)}$$

The effective daily DoD is estimated from the hourly SoC variance accumulated during dispatch:

$$d_{\text{eff}} = \text{clip}\!\left(\frac{2 \cdot \sigma_{\text{SoC}}}{\text{SoC}_{\max}},\; 0,\; 1\right)$$

where $\sigma_{\text{SoC}}$ is the hourly standard deviation of SoC over the year. This is computed during dispatch and passed to the cost function, replacing the fixed 1.5 FEC/day assumption of v3.

### 6.3 Replacement interval

$$T_{\text{replace}} = \max\!\left(1,\; \frac{1 - \theta_{\text{replace}}}{\delta_{\text{cal}} + \delta_{\text{cyc}} \cdot \dot{N}_{\text{FEC}} \cdot 365}\right)$$

**At default values** ($\dot{N}_{\text{FEC}} = 0.5$/day, which is the dispatch-average FEC assuming moderate DoD):

$$T_{\text{replace}} = \frac{0.20}{0.020 + 5\times10^{-5} \times 182.5} = \frac{0.20}{0.0291} = 6.9 \text{ yr}$$

This implies **two replacements** in a 20-year project (at ~yr 7 and ~yr 14), consistent with industry expectations for stationary LFP.

### 6.4 Cost per installation

$$C_{\text{one}} = B \cdot c_{\text{energy}} \cdot 10^3 + P_{\text{batt}} \cdot c_{\text{power}} \cdot 10^3 \quad [\$/\text{MW-load}]$$

Power capex scales with $P_{\text{batt}} = \min(1, 4/B)$, not always 1 MW — consistent with the dispatch de-rating.

### 6.5 Replacement NPV and annualised cost

$$\text{NPV}_{\text{batt}} = C_{\text{one}} \cdot \left[1 + \sum_{k=1}^{\lfloor n / T_{\text{replace}} \rfloor} (1+r)^{-k \cdot T_{\text{replace}}}\right]$$

$$C_{\text{batt}}^{\text{ann}} = \text{NPV}_{\text{batt}} \cdot \text{CRF}(r, n) + C_{\text{one}} \cdot \phi_{\text{OM}}$$

$$c_{\text{batt}} = C_{\text{batt}}^{\text{ann}} / 8760 \quad [\$/\text{MWh-load}]$$

where $\phi_{\text{OM}} = 0.015$ (1.5%/yr O&M) and $\text{CRF}(0.07, 20) = 0.0944$.

**Verified delivered battery costs (2025, 0.5 FEC/day, v5 consistent basis):**

| Storage duration | Delivered cost (US) | Delivered cost (EU) |
|-----------------|--------------------|--------------------|
| 2h | $11.8/MWh | $12.6/MWh |
| 4h | $20.2/MWh | $21.0/MWh |
| 8h | $35.5/MWh | $35.9/MWh |
| 12h | $51.9/MWh | $52.2/MWh |
| 24h | $102.1/MWh | $102.3/MWh |

With the v5 region-invariant energy component, US and EU storage costs are now nearly identical (EU marginally higher via the power/BOS premium), converging at long durations where energy dominates — versus v4, where the EU column was ~2× cheaper. Consistent with NREL ATB 2024 and Ember 2025 LCOS ranges.

---

## 7. Gas Backup Cost and Carbon Trajectory

### 7.1 Technology selection

$$\text{type} = \begin{cases} \text{CCGT} & f_{\text{gas}} \geq 0.20 \\ \text{OCGT} & f_{\text{gas}} < 0.20 \end{cases}$$

At RE > 80%, gas runs less than 20% of hours → OCGT peaker economics apply.

### 7.2 Gas LCOE with Peak Capacity Sizing

$$\text{LCOE}_g = \frac{c_{\text{capex}}^g \cdot \text{CRF}(r, n_g) \cdot K_{\text{gas}}}{8760 \cdot f_{\text{gas}}} + \frac{c_{\text{FOM}}^g \cdot K_{\text{gas}}}{8760 \cdot f_{\text{gas}}} + p_{\text{gas}} \cdot \text{HR}_g + c_{\text{VOM}} + p_{\text{CO}_2}(t) \cdot \varepsilon_g$$

where $K_{\text{gas}}$ is the peak backup capacity factor (maximum hourly residual deficit as a fraction of peak load, i.e., $P_{\text{backup}} / P_{\text{load}}$).

**Delivered gas cost** ($/MWh of total load): $c_{\text{gas}} = \text{LCOE}_g \cdot f_{\text{gas}}$

**Pure gas reference** (CCGT at 85% CF, verified values):

| | US | EU 2025 | EU 2030 | EU 2035 |
|-|----|---------|---------|----|
| Gas price ($/MMBtu) | 4.0 | 10.0 | 10.0 | 10.0 |
| Carbon price ($/tCO₂) | 0 | 70 | 98 | 154 |
| Pure LCOE ($/MWh) | **46.1** | **113.8** | **125.2** | **148.3** |

*(v5.3: gas financed at a 9% WACC — slightly higher capex recovery than the legacy
flat 7%, which raised the pure-gas reference by ≈$2.4/MWh US and ≈$2.4–3/MWh EU.)*

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
| CCGT capex ($/kW) | 1,100 | 1,100 | Lazard v18 |
| OCGT capex ($/kW) | 500 | 500 | Lazard v18 |
| CCGT FOM ($/kW-yr) | 15 | 15 | Lazard v18 |
| OCGT FOM ($/kW-yr) | 10 | 10 | Lazard v18 |
| CCGT heat rate (MMBtu/MWh) | 6.5 | 6.5 | EIA; ~52% LHV |
| OCGT heat rate (MMBtu/MWh) | 9.5 | 9.5 | EIA; ~36% LHV |
| VOM ($/MWh) | 3.0 | 3.0 | Lazard v18 |
| Gas price ($/MMBtu) | 4.0 | 10.0 | EIA Henry Hub; TTF forward |
| CCGT CO₂ intensity (tCO₂/MWh) | 0.41 | 0.41 | IPCC AR6 |
| OCGT CO₂ intensity (tCO₂/MWh) | 0.60 | 0.60 | IPCC AR6 |
| Carbon price trajectory | Linear ($0) | Logistic ($70→$200) | EU ETS Fit-for-55 |
| Plant lifetime (yr) | 25 | 25 | Industry standard |

---

## 8. System LCOE and 3D Optimisation

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
| Onshore wind | 5.5% | 25 | 0.0719 | same, slightly shorter life |
| LFP battery | 7.0% | 20* | 0.0944 | moderate tech/cycling risk (*replacement horizon, §6) |
| Gas (CCGT/OCGT) | 9.0% | 25 | 0.1018 | merchant + carbon-policy / stranding risk |

These bracket the literature (NREL ATB 4.4–8.0% real; Lazard ~8% nominal ≈ 5.5% real for
RE, higher for thermal). The legacy flat **7%** is recovered by setting every `wacc` to 0.07.

**Re-annualising the bundled generation LCOE (`rewacc_lcoe`).** The exogenous solar/wind
LCOE is quoted at the legacy 7% (`LEGACY_WACC`). It splits into a capital-recovery part
(fraction $1-\omega$, with $\omega=$ `om_frac_lcoe`) and a fixed-O&M part ($\omega$); only
the capital part rescales with WACC:

$$\text{LCOE}'_{\text{gen}} = \text{LCOE}_{\text{gen}} \cdot \left[(1-\omega)\,\frac{\text{CRF}(\text{wacc}, L)}{\text{CRF}(0.07, L)} + \omega\right]$$

At the defaults this multiplies solar by **0.876** and wind by **0.902** (cheaper capital →
lower delivered LCOE); it is the identity when wacc = 0.07, so the legacy results are
recovered exactly. The factor is year-independent, so cost *trajectories* (and the
grid+PPA line, which uses an LCOE ratio) keep their shape.

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
| `c_win_max` | 18× (US), 20× (EU) | Yes | Firm (no-shed) high-RE is wind-heavy (EU 95% → ~15×) |
| `storage_hours_max` | 60h (US & EU) | Yes | Optima ~5–9h; far below the bound |
| `wind_solar_corr` | 0.0 (US), −0.35 (EU) | Yes | Contemporaneous copula ρ |
| `syn_loading` | 0.50 | Yes | Synoptic factor loading λ (§4.6) |
| `syn_persistence` | 0.82 (US), 0.85 (EU) | Yes | Synoptic AR(1) φ; episode length ≈ 1/(1−φ) days |

---

## 11. Key Results

Headline = **FIRM (always-on)** workload: gas backup sized to 100% of load, nothing shed,
capped opex. All values from `scratch/v53_run.log` (June 2026; 50 MC weather years, 21³ grid,
Dunkelflaute weather, consistent battery basis, **per-tech WACC v5.3**). Premium/AI workloads
collapse to firm under the economic shed test, so these are the relevant numbers for any
valuable datacenter. (Tables are regenerated from the export via `tools/regen_doc_tables.py`.)

### US — Firm (always-on)

| RE target | 2025 ($/MWh) | 2030 | 2035 | 2040 | vs gas 2025 | Crossover |
|-----------|-------------|------|------|------|-------------|-----------|
| 70% | 88.5 | 68.3 | 57.0 | 49.1 | +92% | >2040 |
| 80% | 105.5 | 76.1 | 60.0 | 49.5 | +129% | >2040 |
| 85% | 124.2 | 90.3 | 70.9 | 57.6 | +170% | >2040 |
| 90% | 176.9 | 137.8 | 109.9 | 90.3 | +284% | >2040 |
| 95% | 192.9 | 154.9 | 133.0 | 117.6 | +319% | >2040 |
| **Gas** | **46.1** | **46.1** | **46.1** | **46.1** | — | — |

US RE never beats gas within the horizon — cheap untaxed gas ($4/MMBtu → ~$46/MWh even at a
9% WACC) is a very low baseline, and high-RE needs heavy wind overbuild (~7–11×) to ride out
multi-day lulls. v5.3's cheaper RE financing narrows the gap (90% RE: +316% → +284% vs gas)
but does not close it.

### Europe — Firm (always-on)

| RE target | 2025 ($/MWh) | 2030 | 2035 | 2040 | vs gas 2025 | Crossover |
|-----------|-------------|------|------|------|-------------|-----------|
| 70% | 114.2 | 95.7 | 87.9 | 79.8 | +0% | **~2025** |
| 80% | 130.5 | 101.6 | 88.8 | 80.0 | +15% | **~2027** |
| 85% | 148.2 | 114.0 | 98.0 | 86.9 | +30% | **~2029** |
| 90% | 196.6 | 159.6 | 138.9 | 119.0 | +73% | **~2034** |
| 95% | 215.0 | 174.1 | 156.6 | 140.5 | +89% | **~2036** |
| **Gas** | **113.8** | **125.2** | **148.3** | **162.6** | — | — |

EU gas is expensive and rising (carbon → logistic path toward $200/tCO₂). An always-on RE
datacenter beats gas from ~2025 at 70% RE; **90% RE reaches parity ~2034, 95% ~2036.**
Note how much later these still are than early versions: v4 claimed "90% parity Q2 2025." The
move out to the mid-2030s is the cumulative effect of every honesty fix — no free load-shedding,
realistic batteries, multi-day Dunkelflaute, a properly-resolved optimiser, and (largest
here) **no free demand-deferral**: an always-on datacenter must build enough firm capacity to
ride out week-long lulls. v5.3's per-tech WACC (cheaper RE, dearer gas) then pulls parity ~1 yr
earlier than v5.2 (90%: 2035 → 2034; 95%: 2040 → 2036).

**Optimal EU 90% RE build (2025):** ≈ 11.0× solar + 10.0× wind + 6h storage. Note the huge
generation overbuild but only ~6h battery — see the next subsection.

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
just fuel — so it slightly under-credits shedding for workloads near the threshold. A
finer model would shed hour-by-hour on marginal serving cost. The headline FIRM results are
unaffected (they never shed).

**Flexibility sweep fidelity.** `--flex-sweep` re-runs the dispatch per (interruptible × shed
penalty) point at reduced fidelity (coarser grid, fewer MC years) and wider bounds. Treat its
absolute LCOE as indicative; the *shape* of the trade-off surface is the point.

**Synoptic factor is calibrated, not fitted.** The Dunkelflaute structure (§4.6) uses plausible loadings/persistence ($\lambda=0.5$, $\varphi\approx0.82$–0.85) rather than values fitted to multi-decade ERA5 reanalysis at a specific site. It restores realistic multi-day clustering and correct directionality, but the exact frequency/depth of week-scale lulls — which sets high-RE storage/backup — should be validated against site reanalysis before siting decisions. It is also single-site; geographic aggregation would soften the tails. **This is the single largest accuracy gap** and the highest-value next improvement.

**Gas CF approximation.** The gas backup LCOE uses $f_{\text{gas}}$ as both the gas plant capacity factor and the energy fraction; capacity capital is separately peak-scaled (firm → 100% of load). Reasonable since dispatch runs gas only when battery (and any shedding) are exhausted.

**Spatial correlation not modelled.** A real portfolio diversifies across multiple wind/solar sites. The model assumes co-located generation (worst-case correlation); diversification would lower effective gas fractions and soften Dunkelflaute.

**Constant load profile.** Datacenter load is flat at $P_{\text{load}}$ MW; sub-hourly variation and maintenance windows are not modelled.

**Gas supply reliability.** Gas is assumed always available at nameplate capacity. A truly off-grid site needs on-site fuel storage or pipeline access; neither constraint is modelled (this would *raise* the firm backup cost and help the RE case).

**Solar CF is conservative.** Beta(3, 1.5) cloud attenuation (mean 0.667) produces US solar CF ≈ 0.15, below typical US utility-scale (0.22–0.28). Appropriate for average-cloudiness sites; for clear/arid sites set `mean_irr=6.5` or higher.

**Wind model is land-based.** IEC Class II power curve and Weibull $k=2.1$ (onshore). Offshore would need a separate parameterisation ($k\approx2.5$, lower cut-in, higher rated wind).

**Generation degradation is embedded, not double-counted.** The exogenous Lazard `lcoe_today` already includes module degradation and inverter replacement, so the model does **not** add them again (that would double-count). A `TechParams.degradation_per_yr` knob exists (default **0**) for use with degradation-free input LCOEs; when >0 it inflates delivered LCOE by $1/(1-\tfrac12\,\text{deg}\cdot\text{life})$. Battery degradation, by contrast, *is* modelled explicitly (§6).

**Wind CF is conservative.** The default mean speeds (US 7.5 m/s, EU 7.0 m/s) with the IEC Class II curve give simulated CFs of ≈0.22 (US) / ≈0.19 (EU) — below modern, well-sited, high-hub-height, low-specific-power onshore fleets (US ≈0.33–0.40, EU ≈0.24–0.30). Like the conservative solar CF, this biases the model *against* renewables (more overbuild needed), so the headline "RE is hard/expensive off-grid" conclusion is if anything a lower bound on RE competitiveness. Raise `mean_wind_ms` (or lower the rated/cut-in speeds for a modern low-specific-power turbine) for good wind sites.

**Resource-quality sensitivity (`RESOURCE_PRESETS`, `--resource` / `--resource-sweep`).** To quantify the bias above, each region has a `default` (conservative, headline) and a `good` (modern well-sited) resource preset — US `good` = 6.8 kWh/m²/day & 9.0 m/s → simulated CF solar 0.18 / wind 0.34; EU `good` = 4.6 & 8.5 → 0.13 / 0.30. `run_resource_sensitivity` (CLI `--resource-sweep`) re-runs the firm model at both levels and tabulates LCOE and parity. The good resource lowers 90%-RE LCOE by **~15–20%**, but the qualitative conclusions hold: **US still never reaches gas parity** within the horizon (cheap untaxed gas is a moat even on a great site), while EU parity moves **earlier**. The solar CF stays moderate even at `good` because the Beta(3,1.5) cloud model is conservative; the larger mover is wind. (`default` exactly equals the headline resource, so it does not change any published number.)

### Accuracy summary — what to trust

Treat this as a **stylised techno-economic model: trust directional comparisons, not absolute
numbers to better than ~±20–30%.** Robust conclusions: cheap untaxed US gas is very hard to
beat; carbon-priced EU gas is beatable, with parity moving from ~2025–2027 (70–80% RE) to ~2034–2036
(90–95%); high-RE economics are overbuild-and-gas-dominated, not battery-dominated; and demand
flexibility only helps when compute is worth less than gas — i.e. rarely for premium AI. Not to
be trusted as precise: specific parity *years* and $/MWh (synthetic uncalibrated weather, my own
battery-cost basis, learning-rate extrapolation to 2040, a single site). The biggest lever to
tighten accuracy is replacing the synthetic weather with real multi-year ERA5/NSRDB reanalysis.

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
