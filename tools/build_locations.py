#!/usr/bin/env python3
"""
"fig1 across geographies": the firm off-grid datacenter delivered-cost trajectory at a
fixed renewable target, computed for several large EU countries and US states, on one
two-panel figure (Europe | United States) → figs/locations_fig1.png, plus a small
output/locations_results.json for provenance.

IMPORTANT — illustrative inputs. Each location's resource (annual mean GHI in kWh/m²/day,
mean onshore wind speed in m/s) is an *approximate, representative* value (PVGIS / NSRDB
order-of-magnitude), NOT a fetched site measurement; the gas price, carbon price and
technology costs are inherited from the region (EU vs US) defaults, so within a region the
only thing that differs here is the renewable RESOURCE. Treat the spread as directional —
"sunny/​windy sites beat cloudy/​calm ones, by roughly this much" — not as site-precise. To
make any location exact, feed real ERA5/NSRDB weather via `--weather` (tools/ingest_weather.py).

Run at reduced optimiser fidelity (it is a cross-location comparison, not the headline):
    python tools/build_locations.py
    make locations
"""
import json
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lcoe.params import REGIONS, _sys_with, FIRM, MODEL_VERSION  # noqa: E402
from lcoe.simulate import run_simulation                        # noqa: E402
from lcoe.reporting import git_commit                           # noqa: E402

RE_TARGET = 0.80   # firm renewable share for the comparison (robustly feasible everywhere)

# label, region, mean GHI (kWh/m²/day), mean wind (m/s).  Approximate / representative.
LOCATIONS = [
    # ── Europe (EU gas + EU ETS carbon) ──────────────────────────────────────────
    ("Spain",        "eu", 5.0, 6.2),
    ("France",       "eu", 3.7, 6.8),
    ("United Kingdom", "eu", 2.7, 8.5),
    ("Germany",      "eu", 3.0, 6.8),
    ("Sweden",       "eu", 2.8, 6.8),
    # ── United States (cheap US gas, no federal carbon) ──────────────────────────
    ("Texas",        "us", 5.5, 8.3),
    ("Arizona",      "us", 6.3, 5.8),
    ("Iowa",         "us", 4.3, 8.3),
    ("Virginia",     "us", 4.5, 5.8),
]

# Distinct, print-safe colours (Okabe–Ito + extras), one per location within a panel.
COLS = ["#E69F00", "#56B4E9", "#009E73", "#CC79A7", "#0072B2",
        "#D55E00", "#F0E442", "#7F7F7F", "#117733"]


def run_location(label, region, irr, wind, grid_steps=17, n_mc=22, years=15, seed=42):
    reg = REGIONS[region]
    sysp = _sys_with(reg["sys"], grid_steps=grid_steps, n_mc_weather=n_mc)
    r = run_simulation(solar=reg["solar"], wind=reg["wind"], battery=reg["battery"],
                       gas=reg["gas"], smr=reg["smr"], sys=sysp, workload=FIRM,
                       mean_irr=irr, mean_wind_ms=wind, years=years,
                       reliabilities=[RE_TARGET], seed=seed, grid_ppa=reg.get("grid_ppa"))
    sc = r["scenarios"][RE_TARGET]
    return {"label": label, "region": region, "irr": irr, "wind": wind,
            "years": [int(y) for y in r["years"]],
            "delivered": [round(float(v), 2) for v in sc["opt_delivered"]],
            "gas_pure": [round(float(v), 2) for v in r["gas_pure"]],
            "cf_solar": round(r["sim_cf"]["solar"], 3),
            "cf_wind": round(r["sim_cf"]["wind"], 3)}


def build_figure(results):
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.2), sharey=True)
    panels = {"eu": (axes[0], "Europe", "EU natural-gas baseline"),
              "us": (axes[1], "United States", "US natural-gas baseline")}
    ci = {"eu": 0, "us": 0}
    for r in results:
        ax, _, _ = panels[r["region"]]
        ax.plot(r["years"], r["delivered"], lw=2.2,
                color=COLS[ci[r["region"]] % len(COLS)], label=r["label"])
        ci[r["region"]] += 1
    for reg, (ax, title, gaslbl) in panels.items():
        gas = next(r["gas_pure"] for r in results if r["region"] == reg)
        yrs = next(r["years"] for r in results if r["region"] == reg)
        ax.plot(yrs, gas, color="#444444", lw=2, ls="--", label=gaslbl)
        ax.set(title=title, xlabel="Year", xlim=(yrs[0], yrs[-1]), ylim=(0, None))
        ax.legend(fontsize=8.5, frameon=True, facecolor="white", framealpha=1)
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("Delivered cost ($/MWh of load)")
    fig.suptitle(f"Off-grid datacenter cost at {RE_TARGET:.0%} renewable, by location "
                 f"(firm / always-on)", fontsize=13)
    fig.tight_layout()
    return fig


def main():
    print(f"Computing {len(LOCATIONS)} locations at {RE_TARGET:.0%} renewable "
          f"(reduced fidelity) …")
    results = [run_location(*loc) for loc in LOCATIONS]
    os.makedirs(os.path.join(ROOT, "figs"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
    fig = build_figure(results)
    figpath = os.path.join(ROOT, "figs", "locations_fig1.png")
    fig.savefig(figpath, dpi=200, bbox_inches="tight"); plt.close(fig)
    payload = {"model_version": MODEL_VERSION, "git_commit": git_commit(),
               "re_target": RE_TARGET, "note": "illustrative resource; region-default gas/carbon",
               "locations": results}
    with open(os.path.join(ROOT, "output", "locations_results.json"), "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"Wrote {figpath} and output/locations_results.json")


if __name__ == "__main__":
    main()
