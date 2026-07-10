#!/usr/bin/env python3
"""
Weather-year robustness of the Europe scan ranking (`tools/scan_eu.py`).

The scan scores every cell on 3 weather years (2019–2021). This tool asks: is the
RANKING an artefact of those particular years? It re-scores a subset of cells — the
top of the ranking (the cells the report names) plus a stratified sample across the
whole distribution — on each single weather year, and reports how stable the ordering
is: Spearman rank correlation of each single-year ranking against the 3-year ranking
(and pairwise between years), top-10 retention, and the per-cell spread.

A high rank correlation across years says the map is driven by geography, not by the
weather draw; the residual per-cell spread is the honest interannual noise floor.
(The full 2015–2025 check needs an 11-year grid refetch — `tools/fetch_era5_grid.py
--years 2015 2025` — after which this tool runs unchanged on the wider file.)

Outputs: output/eu_scan_robustness.json (summary + per-cell records).

    python tools/scan_robustness.py                  # ~10-15 min on 8 cores
    python tools/scan_robustness.py --top 10 --sample 20   # quicker
"""
import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lcoe.params import REGIONS, _sys_with                          # noqa: E402
from lcoe.h2system import h2_system_trajectory                      # noqa: E402
from tools.build_locations import cf_consistent_techs               # noqa: E402
from tools import scan_eu                                           # noqa: E402

OUT_JSON = os.path.join(ROOT, "output", "eu_scan_robustness.json")


def score_cell_year(job):
    """Delivered $/MWh of the same gas-free H₂ build as scan_eu.score_cell, but on a
    single weather year k (scan_eu._G is set by scan_eu._init_worker in this process)."""
    c, k = job
    G = scan_eu._G
    sol = G["solar"][c, k].astype(float)
    win = G["wind"][c, k].astype(float)
    cf_s, cf_w = float(sol.mean()), float(win.mean())
    reg = REGIONS["eu"]
    solar_t, wind_t = cf_consistent_techs(reg, "eu", cf_s, cf_w)
    sysp = _sys_with(reg["sys"], n_mc_weather=1)
    j = G["mi"] - 2025
    out = h2_system_trajectory(solar_t, wind_t, reg["battery"], cf_s * 24, 7.0, sysp,
                               j, seed=42, n_mc=1, weather_years=[(sol, win)],
                               ldes_tech="h2", year_subset=[j])
    return {"cell": int(c), "year_idx": int(k),
            "lcoe": round(float(out["lcoe"][j]), 2)}


def _spearman(a, b):
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    return float(np.corrcoef(ra, rb)[0, 1])


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--top", type=int, default=25,
                   help="cheapest-N cells of the 3-year ranking to include (default 25)")
    p.add_argument("--sample", type=int, default=60,
                   help="stratified sample size across the rest of the ranking (default 60)")
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    args = p.parse_args()

    base = json.load(open(scan_eu.OUT_JSON))
    years = base["weather_years"]
    eu = sorted((c for c in base["cells"] if c["lat"] >= 35), key=lambda c: c["lcoe"])
    picks = list(eu[:args.top])
    rest = eu[args.top:]
    step = max(1, len(rest) // args.sample)
    picks += rest[::step][:args.sample]
    cells = [c["cell"] for c in picks]
    base_lcoe = {c["cell"]: c["lcoe"] for c in picks}

    jobs = [(c, k) for c in cells for k in range(len(years))]
    print(f"Re-scoring {len(cells)} cells × {len(years)} single years "
          f"({len(jobs)} dispatches, {args.workers} workers) …")
    per = {c: {} for c in cells}
    with ProcessPoolExecutor(max_workers=args.workers,
                             initializer=scan_eu._init_worker,
                             initargs=(scan_eu.GRID_NPZ, base["milestone"])) as ex:
        for i, r in enumerate(ex.map(score_cell_year, jobs, chunksize=1), 1):
            per[r["cell"]][r["year_idx"]] = r["lcoe"]
            if i % 25 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)}")

    # ── rank-stability statistics over the subset ─────────────────────────────────
    b = np.array([base_lcoe[c] for c in cells])
    Y = np.array([[per[c][k] for c in cells] for k in range(len(years))])
    rho_vs_base = [round(_spearman(Y[k], b), 3) for k in range(len(years))]
    rho_pairs = {f"{years[i]}v{years[j]}": round(_spearman(Y[i], Y[j]), 3)
                 for i in range(len(years)) for j in range(i + 1, len(years))}
    top10 = set(np.array(cells)[np.argsort(b)][:10].tolist())
    keep10 = [len(top10 & set(np.array(cells)[np.argsort(Y[k])][:10].tolist()))
              for k in range(len(years))]
    spread = (Y.max(0) - Y.min(0)) / Y.mean(0)
    out = {
        "weather_years": years, "milestone": base["milestone"],
        "n_cells": len(cells), "top_n": args.top,
        "spearman_year_vs_3yr": rho_vs_base,
        "spearman_year_pairs": rho_pairs,
        "top10_retained_per_year": keep10,
        "spread_median": round(float(np.median(spread)), 3),
        "spread_p90": round(float(np.percentile(spread, 90)), 3),
        "cells": [{"cell": int(c), "lat": next(x["lat"] for x in picks if x["cell"] == c),
                   "lon": next(x["lon"] for x in picks if x["cell"] == c),
                   "lcoe_3yr": base_lcoe[c],
                   "lcoe_by_year": [per[c][k] for k in range(len(years))]}
                  for c in cells],
    }
    with open(OUT_JSON, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"\nSpearman rank corr, single year vs 3-year ranking: {rho_vs_base}")
    print(f"Pairwise between years: {rho_pairs}")
    print(f"Top-10 cells retained in each single year's top-10: {keep10}")
    print(f"Per-cell spread across years (max−min)/mean: median "
          f"{out['spread_median']:.1%}, p90 {out['spread_p90']:.1%}")
    print(f"Wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
