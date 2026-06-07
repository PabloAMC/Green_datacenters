# Off-Grid Datacenter LCOE Model

[![CI](https://github.com/PabloAMC/Green_datacenteres/actions/workflows/ci.yml/badge.svg)](https://github.com/PabloAMC/Green_datacenteres/actions/workflows/ci.yml)
[![Live results](https://img.shields.io/badge/live%20results-page-3A86FF)](https://pabloamc.github.io/Green_datacenteres/)
[![License: CC BY 4.0](https://img.shields.io/badge/license-CC%20BY%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by/4.0/)

An optimization model that finds the least-cost combination of solar PV, onshore wind, LFP battery storage, and natural gas backup to power an off-grid datacenter at a target renewable energy fraction, across the **US** and **Europe**.

📊 **[Live results page →](https://pabloamc.github.io/Green_datacenteres/)** — the headline conclusions, figures, and assumptions at a glance (generated from `output/`, so it never drifts from the numbers).

**The question it answers:** *if you build a large datacenter that is not connected to the grid, what is the cheapest mix of clean generation + storage + gas backup that keeps it running 24/7 — and in what year does going (mostly) renewable become cheaper than just burning gas?* The headline output is the **LCOE — Levelized Cost of Energy**, the all-in cost of a delivered megawatt-hour (\$/MWh) once capital, fuel, carbon, and storage are amortised over the project's life. New to the terminology? Jump to the **[Glossary](#-glossary)** first.

The **default model is a FIRM, always-on datacenter**: gas turbines are sized to cover **100% of load** whenever sun/wind are absent and batteries are exhausted, so the datacenter never shuts down and the worst case is a known, **capped opex** — a bounded operating (fuel) cost, since the fallback is simply to run on gas. Solar + wind + battery are an incremental investment that displaces gas fuel and carbon where it pays. Optionally, a workload can be made **interruptible** (cheap/spot compute), in which case the model sheds load only when the lost compute is worth less than the gas needed to serve it.

**Design philosophy.** The model aims for *honest accounting* rather than a thumb on the scale in either direction. It charges every generated MWh — including curtailed surplus — at its true cost, never counts shed load as renewable, sizes firm gas backup to 100% of load, and rides out **multi-day wind+solar lulls** (Dunkelflaute) rather than merely hourly ones. Some assumptions cut against renewables (the headline assumes a *single* generation site with no geographic smoothing — though `--sites` now models a multi-site portfolio, which softens the multi-day lulls and can cut high-RE cost substantially; and no credit for free load-shedding) and some cut for them (low per-technology cost of capital for solar/wind; learning-curve cost declines to 2040), so the net bias is not one-directional. Results are therefore reported as **central estimates with explicit uncertainty** — P10–P90 cost bands, an optional P90 bad-weather design premium, and a sensitivity tornado — and should be read as **directional, accurate to roughly ±20–30%**, not precise to the dollar. The full methodology, derivations, data sources, and accuracy caveats live in **[`model_documentation.md`](model_documentation.md)**; the section below summarises the governing equations and parameters.

---

## 📂 Repository File Structure

* **`lcoe/`**: The model, split into focused modules — `params` (dataclasses, tech/region presets), `costs` (learning curves, per-tech WACC, battery & gas costs), `weather` (synthetic solar/wind + Dunkelflaute), `dispatch` (vectorized 8760-h chronological dispatch over the 3D grid), `optimize` (multi-start Nelder-Mead + capex/opex split), `reporting` (summary table + CSV/JSON export), `plots`, `simulate` (run orchestration), `analysis` (flexibility / resource / tornado sweeps), and `cli`.
* **`datacenter_lcoe.py`**: Thin backward-compatible entry point — re-exports the `lcoe` package API and runs the CLI, so `import datacenter_lcoe` and `python datacenter_lcoe.py` keep working unchanged.
* **`model_documentation.md`**: The technical reference — all mathematical formulations (Wright's Law learning curves, Gaussian-copula wind-solar coupling, synoptic Dunkelflaute factor, battery Wöhler degradation, CCGT/OCGT sizing, per-technology WACC, carbon trajectories, the economic-shed rule) with data sources and an accuracy summary.
* **`tools/regen_doc_tables.py`**: Regenerates the documentation result tables from the `output/` JSON export (so the doc numbers are never hand-transcribed).
* **`tools/ingest_weather.py`**: Converts real reanalysis weather (ERA5 / NSRDB, via hourly-CF CSVs) into the `.npz` the model's `--weather` hook consumes — the missing half of the v5.5 reanalysis seam; also has a `demo` mode that writes a synthetic stand-in so the path is exercisable out of the box.
* **`tools/fetch_era5.py`**: One-command ERA5 download + conversion for a lat/lon point → the weather `.npz` (needs a free [CDS](https://cds.climate.copernicus.eu/) API key and `pip install cdsapi xarray netcdf4`). E.g. `python tools/fetch_era5.py --lat 39.0 --lon -77.5 --years 2019 2020 2021 --out output/virginia.npz`.
* **`tools/fetch_locations.py`**: Batch wrapper around `fetch_era5.py` — (re)downloads real ERA5 for every built-in comparison location (large EU countries / US states) into `output/era5/<slug>.npz` in one go (defaults to 2015–2025). `make fetch-locations`.
* **`tools/build_locations.py` / `tools/build_locations_h2.py`**: "fig1 across geographies" — the firm delivered-cost trajectory by location, on illustrative *or* real ERA5 weather (`make locations` / `locations-real`). The **per-state** variants draw **one figure per state** (7 EU countries + 7 US states — the biggest data-center markets in each region — real ERA5 2015–2025) comparing a build *with* vs *without* a wind park: `build_locations_re.py` does the gas-backed renewable-target build (~55% no-wind vs ~80% with-wind; `make locations-re`, panels in `figs/locations_re/`) and `build_locations_h2.py` the fully zero-carbon self-made-hydrogen build (`make locations-h2`, panels in `figs/locations_h2/`). In both, the gap between the two lines is what a wind park buys at that location.
* **`tools/build_solar_only.py` / `tools/build_zerocarbon.py`**: "Do you even need a wind park?" (cost vs renewable fraction, solar+battery-only vs +wind) and the zero-carbon build-option synthesis bar chart (`make solar-only` / `zerocarbon`).
* **`tools/calibrate_synoptic.py`**: Fits the Dunkelflaute parameters (`syn_persistence`, `syn_loading`, `wind_solar_corr`, and multi-site `site_synoptic_corr`) from a real-weather `.npz` — turning the model's biggest "calibrated, not fitted" caveat into a measured input.
* **`tools/check_doc_tables.py`**: Doc-drift guard — fails if the §11 result tables in `model_documentation.md` no longer match `output/*.json`. Run by `make check-docs` and in CI.
* **`tools/build_report.py` / `docs/`**: Generates the self-contained GitHub Pages site (`docs/index.html`) from `output/` + the figures (`make report`); published by `.github/workflows/pages.yml`.
* **`Makefile` / `pyproject.toml` / `requirements-lock.txt`**: `pip install -e .` (console command `datacenter-lcoe`); `make test` / `reproduce` / `check`; pinned versions for byte-for-byte reproduction. CI (`.github/workflows/ci.yml`) runs the tests + doc-drift guard on every push.
* **`sites/`**: Custom-site configs (`--site`). A JSON file inherits a built-in region's defaults and overrides only what varies by location (resource, gas price/carbon, weather structure), so a new geography is a *data* file, not a code change — see `sites/README.md` and `sites/example_texas.json`.
* **`scratch/plot_comparison.py`**: Regenerates the US vs. Europe 70%-RE firm trajectory against each region's gas baseline and annotates any EU/gas parity crossover.
* **`tests/test_model.py`**: Regression + unit tests (LCOE formulas, weather-CF marginals, dispatch energy balance, firm/shed consistency, RE feasibility). Runs standalone (`python tests/test_model.py`) or under `pytest` — no extra dependency required.
* **`output/`**: Machine-readable results written on every run — one tidy CSV (`<prefix>_results.csv`, a row per RE-target × year) and one structured JSON per region, so the figures and documentation tables can be regenerated programmatically instead of hand-transcribed.
* **`figs/`**: Generated plots per region — `fig1` cost trajectory (incl. the gas baseline, SMR, grid+PPA / 24/7-CFE references, and the **fully-optimised gas-free green-H₂ system**; every trajectory, including the H₂ line, is **shaded over a poor↔good site for the region** — the geographic/siting range), `fig2` cost vs RE fraction, `fig3` optimal solar/wind/battery mix, `fig4`/`fig5` cost breakdown by factor split into **capex vs opex** (at 70% / 85% RE), **`fig6` the gas-free green-H₂ system breakdown** (generation / LFP / electrolyser / H₂ storage / turbine / purchased-H₂, all zero-carbon, with the pure-gas reference overlaid for comparison) — plus the US-vs-EU comparison (at 70% RE) and (via `--flex-sweep`) the flexibility trade-off heatmap.

---

## 🚀 Quick Start & Replication

### 1. Prerequisites

Ensure you have Python 3.10+ installed, then install the dependencies (numpy, scipy, matplotlib — pinned floors in [`requirements.txt`](requirements.txt)):

```bash
pip install -r requirements.txt          # floors; or `pip install -e .` for the package
# Exact reproduction of the committed figures/tables: pip install -r requirements-lock.txt
```

For the common tasks there is a `Makefile`: `make test` (regression suite), `make reproduce` (regenerate every figure + `output/` export), `make check` (tests + doc-table drift guard — what CI runs).

*Notes:* No other setup is needed — the model is pure-Python and runs fully offline (no API keys, no network). `scienceplots` is an optional nicety for prettier plots; the code falls back to a plain matplotlib style if it's absent. Likewise, if you do not have LaTeX installed the scripts automatically use the `no-latex` style sheet, so figures still render.

### 2. Run the Main Model

Runs the headline FIRM (always-on) suite for the US and Europe and writes all figures to `figs/`:

```bash
python datacenter_lcoe.py
```

### 3. Choose your own scenario (CLI)

No args reproduces the firm suite. Pick a region, a workload preset, or set the two flexibility knobs directly:

```bash
# EU, cheap interruptible (spot) compute, only the 90% renewable (RE) target
python datacenter_lcoe.py --region eu --workload best-effort --re 0.9

# US, custom flexibility: 30% of load interruptible, compute worth $200/MWh
python datacenter_lcoe.py --region us --interruptible 0.30 --shed-penalty 200

# Flexibility sensitivity sweep → interruptible × compute-value heatmap (opt-in; a few minutes)
python datacenter_lcoe.py --flex-sweep --region eu

# Robustness design: also report a build sized for the 1-in-10 (P90) weather year (~5–9% premium)
python datacenter_lcoe.py --region eu --re 0.9 --design-p90

# Resource-quality sensitivity: conservative default site vs a modern, well-sited 'good' resource
python datacenter_lcoe.py --resource-sweep --region us --re 0.9     # comparison table
python datacenter_lcoe.py --region us --re 0.9 --resource good      # single run on a good site

# Tornado: which assumptions most move RE-vs-gas competitiveness (parity gap) → figure
python datacenter_lcoe.py --tornado --region eu --re 0.9

# Green-hydrogen firming instead of natural gas (zero combustion carbon, pricey fuel)
python datacenter_lcoe.py --region eu --re 0.9 --firming h2

# Gas-backed vs green-H2-firmed delivered cost, same RE build → comparison figure
python datacenter_lcoe.py --firming-compare --region eu --re 0.9

# Long-duration storage overlay: can iron-air or self-produced H2 (made from RE
# overcapacity) displace the residual gas?  Tanks (default), salt cavern, or iron-air.
python datacenter_lcoe.py --ldes h2 --region eu --re 0.9          # self-produced H2, tanks
python datacenter_lcoe.py --ldes h2-cavern --region eu --re 0.9   # + salt-cavern storage
python datacenter_lcoe.py --ldes iron-air --region eu --re 0.9

# JOINT co-optimise a gas-free zero-carbon datacenter (solar+wind+LFP+self/bought-H2),
# swept over the market-H2 price (deep-lull spike) → figure. Slow (~minutes).
python datacenter_lcoe.py --ldes-joint h2 --region eu

# GEOGRAPHIC DIVERSIFICATION: portfolio-average over N separated sites (softens the
# multi-day Dunkelflaute that drives high-RE cost; mean CF is preserved exactly).
python datacenter_lcoe.py --region eu --re 0.9 --sites 4 --site-corr 0.6

# REAL WEATHER: drive the dispatch with measured ERA5/NSRDB years instead of the
# synthetic generator (build the .npz with tools/ingest_weather.py; `demo` for a try-out).
python tools/ingest_weather.py demo output/sample_eu.npz --region eu --years 3
python datacenter_lcoe.py --region eu --re 0.9 --weather output/sample_eu.npz

# CUSTOM SITE: describe a location in a JSON file (inherits a region's defaults).
python datacenter_lcoe.py --site sites/example_texas.json --re 0.9

# NON-FLAT LOAD: add a temperature-driven cooling (PUE) overhead so peak load > average
# (firm gas sizes to the peak). Default is a flat, constant load.
python datacenter_lcoe.py --region us --re 0.9 --load-profile cooling

# FIT the Dunkelflaute parameters (incl. site-corr) from a real-weather .npz.
python tools/calibrate_synoptic.py output/sample_eu.npz
```

Workload presets (`--workload`): `firm` (always-on, 0% shed) · `enterprise` (5% / $2500) · `training` (40% / $900) · `interruptible` (60% / $150) · `best-effort` (90% / $40). `--interruptible` = *fraction of load you may shed*; `--shed-penalty` = *value of the lost compute, $/MWh* (high = firm; the model only sheds when this is below the gas variable cost). Advanced: `--grid-steps`, `--mc`, `--years`, `--seed`.

### 4. Run the US vs. EU Comparison Plot

Regenerates the firm US-vs-Europe **70%-RE** trajectory against each region's gas baseline (and annotates any EU/gas crossover). EU 70% RE is already below gas from 2025; the US 70% line, under the v5.7 deployment recalibration, **never crosses the flat $4/MMBtu US gas baseline within the horizon** (it bottoms at ≈$50/MWh) — it would cross only a stressed-gas baseline:

```bash
PYTHONPATH=. python scratch/plot_comparison.py
```

---

## 🔬 Scientific Reference

A condensed statement of the governing equations and inputs. Full derivations, verification tables, and accuracy caveats are in [`model_documentation.md`](model_documentation.md).

> *Prefer words to symbols? You can skip straight to the [results](#-summary-of-crossover-results-firm--always-on) and [Glossary](#-glossary) — nothing below is needed to read those. This section is here for readers who want the actual equations; each block has a one-line plain-English caption, and the linked documentation opens every section with an "Intuition" line before any math.*

### Optimisation problem

For each region and year, minimise the system LCOE over the build $(C_{\text{sol}}, C_{\text{win}}, B)$ — solar and wind overbuild (installed MW ÷ load MW) and battery duration (hours) — subject to a renewable-energy-fraction target $R$:

$$
\min_{C_{\text{sol}},\,C_{\text{win}},\,B}\; \text{LCOE}_{\text{sys}} \qquad \text{s.t.}\quad f_{\text{RE}}(C_{\text{sol}},C_{\text{win}},B)\;\ge\;R
$$

Gas backup is **not** a decision variable — it is sized dynamically to the peak residual hourly deficit (firm: 100% of load). Solved by multi-start Nelder–Mead with an exterior quadratic penalty, evaluating against a precomputed $21^3 = 9{,}261$-point chronological-dispatch surface (trilinear-interpolated, so each objective call is ~µs). $f_{\text{RE}}$ is an **annual energy** fraction of *served* load, not an hourly guarantee.

### Delivered cost (\$/MWh of load)

$$
\text{LCOE}_{\text{sys}} = \underbrace{C_{\text{sol}}\,\overline{\text{CF}}_{\text{sol}}\,\text{L}_{\text{sol}} + C_{\text{win}}\,\overline{\text{CF}}_{\text{win}}\,\text{L}_{\text{win}}}_{\text{generation}} \;+\; c_{\text{storage}} \;+\; \underbrace{\text{LCOE}_{g}\cdot f_{\text{gas}}}_{\text{firming}} \;+\; \underbrace{v_{\text{shed}}\cdot f_{\text{drop}}}_{\text{lost compute}}
$$

Generation is charged at the imported per-MWh LCOE on **every** MWh produced (including curtailed), which keeps the cost basis consistent with the simulated capacity factor.

### Cost trajectories — Wright's Law + per-technology WACC

Technology costs fall with cumulative deployment $Q_t$ (a learning rate $\text{LR}$ per doubling); the capital part is then re-annualised from the quoted 7% basis to each technology's own WACC and asset life:

$$
\text{L}(t) = \text{L}_0\left(\frac{Q_t}{Q_0}\right)^{\log_2(1-\text{LR})}, \qquad \text{CRF}(r,n)=\frac{r(1+r)^n}{(1+r)^n-1}
$$

WACC / life: solar **5.5% / 30 yr**, wind **5.5% / 25 yr**, battery **7% / 20 yr**, gas **9% / 25 yr**.

### Weather (synthetic 8760-h, or real ERA5/NSRDB via the reanalysis hook)

- **Solar:** a deterministic clear-sky shape × a daily cloud factor $\xi_d\sim\text{Beta}(3,1.5)$ (mean 0.667) with AR(1) day-to-day persistence. The clear-sky mean is normalised so the *effective* (post-cloud) annual CF equals $\bar I/24$ — no double-counting of cloud loss.
- **Wind:** a Weibull($k{=}2.1$) speed run through an IEC power curve (cut-in 3, rated 11, cut-out 25 m/s — a modern low-specific-power turbine), with within-day AR(1) persistence and a winter seasonal lift.
- **Dunkelflaute:** a persistent synoptic common factor (loading $\lambda$, daily persistence $\varphi\approx0.82$–0.85, e-folding ≈5–6 days) jointly suppresses wind and solar for days; a Gaussian copula sets the wind–solar correlation ($\rho=-0.35$ in N. Europe). Marginals/CFs are preserved — only the multi-day *clustering* that storage/backup must cover changes.

### Battery — throughput cycling + augmentation

Capacity fades by calendar + cycle aging, where cycling is counted as **throughput equivalent-full-cycles** $\dot N_{\text{FEC}}=\tfrac{1}{365}\sum_t (d_t/\eta_{\text{dis}})/\text{SoC}_{\max}$. Rather than a mid-life full replacement, the operator **augments** — tops up only the faded cells each year — and pays power/BOS once. Power is coupled to duration as $P_{\text{batt}}=\min(1,\,4/B)$ MW per MW-load.

### Gas backup + carbon

$$
\text{LCOE}_g = \frac{c^g_{\text{capex}}\,\text{CRF}(r,n_g)\,K_{\text{gas}}}{8760\,f_{\text{gas}}} + \frac{c^g_{\text{FOM}}\,K_{\text{gas}}}{8760\,f_{\text{gas}}} + p_{\text{gas}}\,\text{HR}_g + c_{\text{VOM}} + p_{\text{CO}_2}(t)\,\varepsilon_g
$$

CCGT is selected above a 20% gas fraction, else an OCGT peaker; $K_{\text{gas}}$ is the peak-deficit capacity factor. The EU carbon price follows a logistic Fit-for-55 path ($\$70\!\to\!\$200$/tCO₂). Green-H₂ firming (`--firming h2`) reuses this with $\varepsilon_g=0$ and a higher fuel price.

### Economic shedding

A deficit hour is shed only if the value of the lost compute is **below** the gas variable cost of serving it:

$$
\text{shed} \iff v_{\text{shed}} < p_{\text{gas}}\text{HR} + c_{\text{VOM}} + p_{\text{CO}_2}\varepsilon
$$

So premium/AI workloads ($v_{\text{shed}}$ high) never shed and collapse to the firm/capped-opex case; only genuinely cheap compute sheds.

### Key parameters & sources (2025)

| Parameter                                       | US                | EU                  | Source                                                 |
| ----------------------------------------------- | ----------------- | ------------------- | ------------------------------------------------------ |
| Solar PV LCOE₀ (\$/MWh) · LR                  | 52 · 30%         | 60 · 30%           | Lazard LCOE+ v18; Way et al.*Joule* (2022)           |
| Onshore wind LCOE₀ (\$/MWh) · LR              | 50 · 17%         | 48 · 17%           | Lazard v18; OWID learning curves                       |
| LFP battery (energy\$/kWh · power \$/kW) · LR | 180 · 140 · 19% | 180 · 175 · 19%   | BloombergNEF 2024–25; Ember BESS 2025                 |
| Simulated capacity factor (solar / wind)        | 0.23 / 0.33       | 0.16 / 0.29         | inside Lazard CF bands (EU solar below, by irradiance) |
| Gas price (\$/MMBtu) · CO₂ (tCO₂/MWh CCGT)   | 4.0 · 0.41       | 10.0 · 0.41        | EIA Henry Hub / TTF forward; IPCC AR6                  |
| Carbon price (\$/tCO₂) trajectory              | linear\$0         | logistic\$70→\$200 | EU ETS Fit-for-55                                      |
| WACC (solar/wind · battery · gas)             | 5.5% · 7% · 9%  | 5.5% · 7% · 9%    | NREL ATB 2024; merchant-risk spread                    |

### References

- Lazard, *Levelized Cost of Energy+ (LCOE+) v18* (2025) and *LCOH v4.0* (2024).
- Way, Ives, Mealy & Doyne Farmer, "Empirically grounded technology forecasts and the energy transition," *Joule* 6 (2022).
- NREL, *Annual Technology Baseline (ATB) 2024*; NREL *NSRDB* (solar) and *WIND Toolkit*.
- ECMWF, *ERA5 reanalysis* (Copernicus CDS).
- BloombergNEF, *Battery / Energy Storage Outlook* (2024–25); Ember, *Battery Storage / LCOS Report* (2025).
- U.S. EIA (Henry Hub, heat rates); IPCC *AR6* (combustion CO₂ intensities); EU ETS *Fit-for-55*.

---

## 📊 Summary of Crossover Results (FIRM / always-on)

Tables regenerable via `tools/regen_doc_tables.py` from `output/*_results.json`. These are the relevant numbers for any valuable datacenter (premium/AI workloads never shed and collapse to firm). Gas baseline: US flat ~$46/MWh; EU rising from $114 (2025) to $163 (2040) as carbon prices climb. Simulated capacity factors: US solar 0.23 / wind 0.33, EU 0.16 / 0.29 — consistent with the Lazard CF basis of the imported LCOEs.

> **v5.7 deployment recalibration.** Learning curves are now driven by an **S-curve** deployment trajectory (additions growth *decays*), landing solar ≈15.6 TW / wind ≈4.2 TW / batteries ≈14.5 TWh cumulative by 2040 — vs the old constant-growth 38 TW / 7 TW / 45 TWh that was ~3–4× mainstream IEA/BNEF and made deep-future RE too cheap. **2025 numbers and builds are unchanged** (only future unit costs rise). Net: deep-future RE costs lift ~25–40% at 2040, so **US high-RE no longer crosses cheap flat gas within the horizon** (the moat strengthens; RE wins only against the stressed-gas reference line), and **EU 90% parity shifts ~2029→~2030**. Directional conclusions unchanged.

> **A firm, battery-only off-grid system tops out at ~94% RE** (≈0.94 EU / ≈0.95 US over the whole build grid): during a multi-day Dunkelflaute neither sun nor wind produces and a long battery can't deliver enough power to bridge days, so a few percent of annual energy always falls to gas. The suite therefore reports up to 90% RE; pushing higher needs long-duration storage or H₂ firming (the `--ldes` / `--firming h2` overlays and `fig6`). Requesting a target above the ceiling triggers an explicit infeasibility warning.

> **On-grid reference.** Every trajectory/reliability figure and the summary also plot a **Grid + renewable-PPA** line — the realistic alternative of staying on the grid and signing a renewable PPA (all-in ≈ \$75/MWh US, \$117/MWh EU in 2025, declining with the solar learning curve). It sits *below* the off-grid high-RE optimum in both regions, making explicit that **going off-grid is itself a cost premium**. A second line, **Grid + 24/7 CFE**, adds a premium for hour-by-hour carbon-free matching (≈\$115/MWh US, \$172/MWh EU in 2025). Both are annual-vs-hourly reference lines — not part of the optimisation.

### US — 90% RE

* **2025 LCOE:** $126.4/MWh; **2040:** $77.6/MWh. **Parity: >2040 at every RE target** (70% bottoms at ≈$50/MWh — above the $46 flat-gas baseline).
* *Why?* Extremely cheap, untaxed US gas (~$46/MWh even at a 9% WACC) is a moat clean energy can't cross within the horizon — and the v5.7 deployment recalibration (less-aggressive late-horizon learning) *widens* it, removing the moderate-RE mid-2030s crossings of earlier versions. US RE wins only against the **stressed-gas** reference (×1.6 fuel ≈$62/MWh), which 70% RE beats by ~2030 — i.e. competitiveness hinges on gas not staying at $4.

### Europe — 90% RE

* **2025 LCOE:** $156.1/MWh; **2040:** $106.5/MWh. **Parity: ~2030** (70–80% RE reach parity ~2025; **85% ~2026**).
* *Why?* Expensive, carbon-taxed EU gas makes RE competitive — and with a CF-consistent resource the always-on build that rides out week-long Dunkelflaute reaches 90% parity by ~2030 (v5.7 nudges this a year later than the pre-recalibration ~2029, but rising EU carbon still does most of the work).

### If the compute is cheap (interruptible)

For low-value/spot compute, shedding the most expensive hours helps a lot: in the `--flex-sweep` (EU 90% RE, 2030), a 95%-interruptible workload valued at $25/MWh sees delivered cost fall to ~$32/MWh (parity by 2025) versus ~$174/MWh fully firm. (The flex-sweep runs at reduced fidelity — coarser grid, wider bounds — so its firm corner reads above the §11 headline of ~$120; treat the *shape* of the trade-off, not the absolute, as the point.) Premium AI ($900/MWh) sheds nothing and stays firm.

---

## 📖 Glossary

The model lives at the intersection of power-systems and finance jargon. Quick definitions of every term used above (the full mathematical treatment and data sources are in `model_documentation.md`):

| Term                                           | Plain-language meaning                                                                                                                                                                                        |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **LCOE** (Levelized Cost of Energy)      | The all-in cost of one delivered MWh (\$/MWh) once capital, fuel, carbon, and O&M are spread over the project's life. The model's headline output. Lower = cheaper power.                                     |
| **MWh / MW**                             | Megawatt-hour (energy) / megawatt (power). A 1 MW datacenter running flat for a year uses 8,760 MWh.                                                                                                          |
| **Capacity factor (CF)**                 | Average output ÷ nameplate rating. A solar farm with CF 0.23 produces 23% of its peak rating averaged over the year. Sets how much energy a given amount of capacity actually delivers.                      |
| **RE fraction / "90% RE"**               | Share of the datacenter's*served* energy that comes from renewables + storage (the rest is gas). The target the optimiser must meet.                                                                        |
| **Firm / always-on**                     | A datacenter that never shuts down: gas backup is sized to cover 100% of load during lulls, so the worst case is a known, capped cost. The model's default.                                                   |
| **Interruptible / shedding**             | Optionally pausing cheap workloads during deficits instead of burning gas. Only done when the lost compute is worth less than the gas it would take to serve it.                                              |
| **Overbuild**                            | Installing more solar/wind nameplate than peak load (e.g. "6× solar") so enough energy is still made on poor-weather days. Excess on good days is**curtailed** (spilled).                              |
| **Dunkelflaute**                         | German for "dark doldrums" — a multi-day, wide-area spell of low sun*and* low wind. The hardest thing for a renewable system to ride through; it sets how much storage/backup is needed.                   |
| **WACC**                                 | Weighted Average Cost of Capital — the financing/discount rate. The model uses a different WACC per technology (solar/wind 5.5%, battery 7%, gas 9%) to reflect differing risk.                              |
| **Wright's Law / learning rate**         | Empirical rule that a technology's cost falls by a fixed % for every doubling of cumulative production. A 30% solar learning rate means each doubling cuts cost ~30%. Drives the cost-over-time trajectories. |
| **CCGT / OCGT**                          | Combined-Cycle / Open-Cycle Gas Turbine. CCGT is efficient baseload; OCGT is a cheap-to-build, expensive-to-run peaker. The model picks whichever is cheaper for the gas duty.                                |
| **SMR**                                  | Small Modular (nuclear) Reactor — plotted as a firm-clean reference alternative, not optimised.                                                                                                              |
| **PPA**                                  | Power Purchase Agreement — a long-term contract to buy renewable energy. "Grid + RE PPA" is the realistic*on-grid* alternative to building off-grid (see reference-lines note in the results).             |
| **24/7 CFE**                             | Carbon-Free Energy matched in*every hour* (the Google/Microsoft standard), vs. cheaper annual-average matching. Plotted as an on-grid reference line.                                                       |
| **LDES**                                 | Long-Duration Energy Storage (iron-air, hydrogen) — multi-day storage explored via the `--ldes` overlay as a possible substitute for residual gas.                                                         |
| **Green H₂ firming**                    | Burning purchased/self-made green hydrogen in a turbine instead of gas — zero combustion carbon, but pricey fuel (`--firming h2`).                                                                         |
| **P90 / design-P90**                     | The 1-in-10 bad weather year.`--design-p90` sizes the build to survive it (a robustness premium over the average year).                                                                                     |
| **Crossover / parity**                   | The year a renewable build's LCOE drops below the gas baseline's — i.e. when going (mostly) renewable becomes the cheaper choice.                                                                            |
| **Lazard v18 / NREL ATB / ERA5 / NSRDB** | External data sources: Lazard's*Levelized Cost of Energy+* and NREL's *Annual Technology Baseline* for costs; ERA5 (ECMWF) and NSRDB (NREL) for real weather (via the reanalysis hook).                   |

---

## 📜 License

This work is licensed under a [**Creative Commons Attribution 4.0 International (CC BY 4.0)**](https://creativecommons.org/licenses/by/4.0/) license — see [`LICENSE`](LICENSE). You may share and adapt the model and its outputs, including commercially, provided you give appropriate credit. The cost and weather *inputs* are derived from the third-party sources cited in `model_documentation.md`, which carry their own terms.
