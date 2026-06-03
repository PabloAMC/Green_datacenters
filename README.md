# Off-Grid Datacenter LCOE Model (v5.4)

An optimization model that finds the least-cost combination of solar PV, onshore wind, LFP battery storage, and natural gas backup to power an off-grid datacenter at a target renewable energy fraction, across the **US** and **Europe**.

The **default model is a FIRM, always-on datacenter**: gas turbines are sized to cover **100% of load** whenever sun/wind are absent and batteries are exhausted, so the datacenter never shuts down and the worst case is a known, **capped opex** (run on gas). Solar + wind + battery are an incremental investment that displaces gas fuel and carbon where it pays. Optionally, a workload can be made **interruptible** (cheap/spot compute), in which case the model sheds load only when the lost compute is worth less than the gas needed to serve it.

> **v5.4 — battery augmentation + throughput cycle counting; no optimiser hysteresis.** Storage cost moves from lumpy full-replacement to **capacity augmentation** (top up faded cells yearly — standard practice, ~30–35% cheaper); cycling is counted as **throughput equivalent-full-cycles**; and the optimiser's path-regularisation penalty (a source of year-to-year hysteresis) is removed. Net: RE LCOE falls a further ~8–15% → **EU 90% RE parity ~2033, 95% ~2035; US now reaches parity at 70–80% RE (~2038–39)** (90%+ still never beats cheap US gas).
>
> **v5.3 — per-technology cost of capital.** A single flat WACC is replaced by differentiated financing: solar/wind **5.5%** (low-risk, long-life infrastructure), LFP battery **7%**, gas **9%** (merchant + carbon-policy risk). Legacy flat-7% is recoverable by setting each `wacc` to 0.07. See *Key Upgrades*.

> **v5.2 — demand flexibility as economic shedding.** Earlier versions let paused load be "recovered later," which secretly assumed idle over-provisioned GPUs (economically irrational, since hardware capex ≫ energy). v5.2 drops that: premium compute never sheds and collapses to the firm/capped-opex case; only genuinely cheap compute sheds.

---

## 📂 Repository File Structure

*   **`lcoe/`**: The model, split into focused modules — `params` (dataclasses, tech/region presets), `costs` (learning curves, per-tech WACC, battery & gas costs), `weather` (synthetic solar/wind + Dunkelflaute), `dispatch` (vectorized 8760-h chronological dispatch over the 3D grid), `optimize` (multi-start Nelder-Mead + capex/opex split), `reporting` (summary table + CSV/JSON export), `plots`, `simulate` (run orchestration), `analysis` (flexibility / resource / tornado sweeps), and `cli`.
*   **`datacenter_lcoe.py`**: Thin backward-compatible entry point — re-exports the `lcoe` package API and runs the CLI, so `import datacenter_lcoe` and `python datacenter_lcoe.py` keep working unchanged.
*   **`model_documentation.md`**: The technical reference — all mathematical formulations (Wright's Law learning curves, Gaussian-copula wind-solar coupling, synoptic Dunkelflaute factor, battery Wöhler degradation, CCGT/OCGT sizing, per-technology WACC, carbon trajectories, the economic-shed rule) with data sources and an accuracy summary.
*   **`tools/regen_doc_tables.py`**: Regenerates the documentation result tables from the `output/` JSON export (so the doc numbers are never hand-transcribed).
*   **`scratch/plot_comparison.py`**: Regenerates the US vs. Europe 90%-RE firm trajectory and annotates the parity crossover.
*   **`tests/test_model.py`**: Regression + unit tests (LCOE formulas, weather-CF marginals, dispatch energy balance, firm/shed consistency, RE feasibility). Runs standalone (`python tests/test_model.py`) or under `pytest` — no extra dependency required.
*   **`output/`**: Machine-readable results written on every run — one tidy CSV (`<prefix>_results.csv`, a row per RE-target × year) and one structured JSON per region, so the figures and documentation tables can be regenerated programmatically instead of hand-transcribed.
*   **`figs/`**: Generated plots per region — `fig1` cost trajectory, `fig2` cost vs RE fraction, `fig3` optimal solar/wind/battery mix, `fig4`/`fig5` cost breakdown by factor split into **capex vs opex** (at 70% / 85% RE; solid = capex, hatched = opex) — plus the US-vs-EU comparison (at 70% RE) and (via `--flex-sweep`) the flexibility trade-off heatmap.

---

## 🚀 Quick Start & Replication

### 1. Prerequisites
Ensure you have Python 3.10+ installed. Install the required libraries:
```bash
pip install numpy scipy matplotlib scienceplots
```
*Note: If you do not have LaTeX installed on your system, the scripts are configured to automatically fall back to standard matplotlib fonts using the `no-latex` style sheet to prevent compilation errors.*

### 2. Run the Main Model
Runs the headline FIRM (always-on) suite for the US and Europe and writes all figures to `figs/`:
```bash
python datacenter_lcoe.py
```

### 3. Choose your own scenario (CLI)
No args reproduces the firm suite. Pick a region, a workload preset, or set the two flexibility knobs directly:
```bash
# EU, cheap interruptible (spot) compute, only the 90% RE target
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
```
Workload presets (`--workload`): `firm` (always-on, 0% shed) · `enterprise` (5% / $2500) · `training` (40% / $900) · `interruptible` (60% / $150) · `best-effort` (90% / $40). `--interruptible` = *fraction of load you may shed*; `--shed-penalty` = *value of the lost compute, $/MWh* (high = firm; the model only sheds when this is below the gas variable cost). Advanced: `--grid-steps`, `--mc`, `--years`, `--seed`.

### 4. Run the US vs. EU Comparison Plot
Regenerates the firm US-vs-Europe 90%-RE trajectory and annotates the parity crossover (~Q4 2034 in EU):
```bash
PYTHONPATH=. python scratch/plot_comparison.py
```

---

## 💡 Key Upgrades (v4 → v5.4)

Each version removed an assumption that made high renewable fractions look cheaper/easier than they are. The cumulative effect is large: EU 90%-RE parity moved from v4's "Q2 2025" to **~2033** for an always-on datacenter.

-1. **Battery augmentation + throughput cycle counting; no hysteresis (v5.4, this release).** Storage cost moves from full-system replacement to yearly **capacity augmentation** (top up only the faded cells — ~30–35% cheaper); degradation is driven by **throughput equivalent-full-cycles** rather than a 2σ(SoC) proxy; and the optimiser's path-regularisation penalty (year-to-year hysteresis) is removed. Cheaper storage pulls parity earlier across the board.

0.  **Per-technology cost of capital (v5.3).** A single flat WACC is replaced by differentiated real WACC + asset life: solar/wind **5.5%** (30/25 yr), battery **7%** (20 yr), gas **9%** (25 yr). The bundled generation LCOE is re-annualised at the new WACC (`rewacc_lcoe`); each component is levelised over its own life (fixing the prior mixed-horizon treatment). Cheaper RE and dearer gas pull EU parity ~1 yr earlier; the US moat holds.

1.  **No free load-shedding (v5).** v4 silently dropped up to 20–40% of load during deficits yet counted it as "renewable" (~8% of annual load vanished). Fixed.
2.  **Consistent battery cost basis (v5).** LFP cells are globally traded, so the **energy** component is region-invariant ($180/kWh); only the **power/BOS** component carries a regional premium (US $140/kW, EU $175/kW). EU storage is modestly *more* expensive than the US — opposite of v4's 2.75×-cheaper artefact.
3.  **Multi-day "Dunkelflaute" weather (v5).** A persistent synoptic factor jointly suppresses wind and solar for days, and hourly wind mean-reverts to the persistent daily level (v4 lulls decayed within hours). Marginals/CFs preserved; only the temporal clustering — what storage/backup must cover — changes.
4.  **Right-sized optimiser grid + boundary guard (v5.1).** v5's coarse 15³ grid over oversized bounds pinned the optimum onto nodes (flat "capped" lines) and overstated high-RE cost ~15–30%. A 21³ grid over right-sized bounds fixes it; a guard now warns if any optimum hits a bound.
5.  **Flexibility = economic shedding; FIRM default (v5.2).** Replaces the v5/v5.1 "defer-and-recover" model (which assumed idle over-provisioned GPUs). The headline is now a **firm, always-on** datacenter with gas backup sized to 100% of load (capped opex). An optional **interruptible** mode sheds only when the value of lost compute is below the gas variable cost — so premium/AI compute never sheds and reverts to firm; only cheap spot/research compute sheds. Two CLI knobs (`--interruptible`, `--shed-penalty`) and a `--flex-sweep` sensitivity surface.

Inherited from v4: 3D optimisation over (solar, wind, battery); Gaussian-copula solar-wind correlation (EU $\rho=-0.35$); DoD-weighted battery degradation; dynamic gas sizing.

---

## 📊 Summary of Crossover Results (FIRM / always-on, v5.4)

From `scratch/v54_run.log` (tables regenerable via `tools/regen_doc_tables.py`). These are the relevant numbers for any valuable datacenter (premium/AI workloads never shed and collapse to firm). Gas baseline: US flat ~$46/MWh; EU rising from $114 (2025) to $163 (2040) as carbon prices climb.

> **On-grid reference (new).** Every trajectory/reliability figure and the summary now also plot a **Grid + renewable-PPA** line — the realistic alternative of staying on the grid and signing a renewable PPA (all-in ≈ \$75/MWh US, \$117/MWh EU in 2025, declining with the solar learning curve). It sits *below* the off-grid high-RE optimum in both regions, making explicit that **going off-grid is itself a cost premium**. A second line, **Grid + 24/7 CFE**, adds a premium for hour-by-hour carbon-free matching (≈\$115/MWh US, \$172/MWh EU in 2025). Both are annual-vs-hourly reference lines — not part of the optimisation.

### US — 90% RE
*   **2025 LCOE:** $161.8/MWh; **2040:** $85.0/MWh. **Parity (90% RE): >2040.** But **70–80% RE now reach parity ~2038–39** as cheaper (augmented) storage carries moderate-RE builds below $46.
*   *Why?* Extremely cheap, untaxed US gas (~$46/MWh even at a 9% WACC) is a moat clean energy can't cross within the horizon at *high* RE fractions, where heavy wind overbuild (~7–11×) is needed for multi-day lulls.

### Europe — 90% RE
*   **2025 LCOE:** $181.7/MWh; **2040:** $113.6/MWh. **Parity: ~2033** (70–80% RE reach parity ~2025; **85% ~2027**; 95% ~2035).
*   *Why?* Expensive, carbon-taxed EU gas makes RE competitive — but an *always-on* datacenter must build enough firm capacity (≈11× solar + 10× wind + 6h battery at 90%) to ride out week-long Dunkelflaute, which keeps 90%+ parity in the mid-2030s.

### If the compute is cheap (interruptible)
For low-value/spot compute, shedding the most expensive hours helps a lot: at EU 90% RE in 2030, a 95%-interruptible workload valued at $25/MWh reaches roughly the gas variable cost (parity by 2025) versus the firm ~$150/MWh. See the `--flex-sweep` figures. Premium AI ($900/MWh) sheds nothing and stays firm.
