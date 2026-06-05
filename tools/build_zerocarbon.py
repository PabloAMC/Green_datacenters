#!/usr/bin/env python3
"""
"How to build a (nearly) zero-carbon firm datacenter" — delivered cost in 2035 across build
options, US & EU, synthesising the wind-vs-no-wind and gas-vs-hydrogen trade-offs.
→ figs/zerocarbon.png + output/zerocarbon_results.json.  make zerocarbon

Options (firm, always-on):
  1. Solar + battery + gas        — caps at ~66% renewable; the rest is gas (emits).
  2. Solar + wind + battery       — 90% renewable; ~10% gas (emits a little).
  3. Solar + wind + battery + H₂  — fully zero-carbon, WITH a wind park.
  4. Solar + battery + self-made H₂ — fully zero-carbon, NO wind (make H₂ from surplus solar).
  5. Solar + battery + bought H₂  — fully zero-carbon, NO wind (buy all the H₂; expensive).
Reduced optimiser fidelity (a comparison, not the headline).
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

import lcoe.h2system as h2s                                              # noqa: E402
from lcoe.params import REGIONS, _sys_with, FIRM, GAS_H2, MODEL_VERSION  # noqa: E402
from lcoe.costs import cumulative_capacity, wright_law, rewacc_lcoe      # noqa: E402
from lcoe.dispatch import ChronologicalSimulator                        # noqa: E402
from lcoe.optimize import optimal_cost_3d                               # noqa: E402
from lcoe.reporting import git_commit                                   # noqa: E402

YEAR = 2035
I = YEAR - 2025
C_EMIT = "#9a8c7a"    # options that still burn some gas
C_ZERO = "#2a9d5c"    # fully zero-carbon options


def _yr_costs(reg):
    s, w, b = reg["solar"], reg["wind"], reg["battery"]
    lsol = rewacc_lcoe(wright_law(s.lcoe_today, s.cumulative_gw_2025,
                                  cumulative_capacity(s, 15), s.learning_rate), s)[I]
    lwin = rewacc_lcoe(wright_law(w.lcoe_today, w.cumulative_gw_2025,
                                  cumulative_capacity(w, 15), w.learning_rate), w)[I]
    cb = cumulative_capacity(b, 15)
    ckwh = wright_law(b.capex_kwh_today, b.cumulative_gwh_2025, cb, b.learning_rate)[I]
    ckw = wright_law(b.capex_kw_today, b.cumulative_gwh_2025, cb, b.learning_rate)[I]
    return lsol, lwin, ckwh, ckw


def options(region):
    reg = REGIONS[region]
    fj = json.load(open(os.path.join(ROOT, "output", f"{region}_firm_results.json")))
    k = fj["years"].index(YEAR)
    sw90 = fj["scenarios"]["0.90"]["lcoe"][k]        # solar+wind+battery, 90% renewable
    h2wind = fj["h2_system"]["lcoe"][k]              # solar+wind+battery+H₂, zero-carbon

    # solar + battery only (no wind): one sim, two firming options at its RE ceiling
    sysp = _sys_with(reg["sys"], grid_steps=17, n_mc_weather=15, c_win_max=0.0)
    sim = ChronologicalSimulator(sysp, reg["battery"], FIRM, reg["mean_irr"],
                                 reg["mean_wind_ms"], seed=42)
    R = round(1.0 - float(sim.gas_mean.min()) - 0.02, 2)
    lsol, lwin, ckwh, ckw = _yr_costs(reg)
    kw = dict(batt=reg["battery"], capex_batt_kwh=ckwh, capex_batt_kw=ckw,
              year_index=I, sys=sysp)
    sb_gas = optimal_cost_3d(sim, R, lsol, lwin, gas=reg["gas"], **kw)[0]
    sb_buy = optimal_cost_3d(sim, R, lsol, lwin, gas=GAS_H2, **kw)[0]

    # solar + battery + self-made H₂ (no wind): force wind to 0 in the gas-free co-opt
    saved = h2s._HI.copy(); h2s._HI = np.array([24.0, 0.0, 4.0, 720.0])
    try:
        out = h2s.h2_system_trajectory(reg["solar"], reg["wind"], reg["battery"],
                                       reg["mean_irr"], reg["mean_wind_ms"],
                                       _sys_with(reg["sys"], grid_steps=15, n_mc_weather=10),
                                       years=15, seed=42, n_mc=6)
        sb_self = out["lcoe"][I]
    finally:
        h2s._HI = saved

    return [
        (f"Solar + battery + gas (~{R:.0%} renewable, emits)", round(sb_gas, 0), C_EMIT),
        ("Solar + wind + battery (90% renewable, ~10% gas)", round(sw90, 0), C_EMIT),
        ("Solar + wind + battery + green H₂ (zero-carbon)", round(h2wind, 0), C_ZERO),
        ("Solar + battery + self-made H₂ (zero-carbon, no wind)", round(sb_self, 0), C_ZERO),
        ("Solar + battery + bought H₂ (zero-carbon, no wind)", round(sb_buy, 0), C_ZERO),
    ]


def build_figure(data):
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, region, title in [(axes[0], "us", "United States"), (axes[1], "eu", "Europe")]:
        opts = sorted(data[region], key=lambda o: o[1])         # cheapest at top
        labels = [o[0] for o in opts]; vals = [o[1] for o in opts]; cols = [o[2] for o in opts]
        y = np.arange(len(opts))
        ax.barh(y, vals, color=cols, edgecolor="white")
        for yi, v in zip(y, vals):
            ax.text(v + max(vals) * 0.01, yi, f"${v:.0f}", va="center", fontsize=9)
        ax.set_yticks(y); ax.set_yticklabels(labels, fontsize=8.5)
        ax.invert_yaxis()
        ax.set(title=title, xlabel="Delivered cost in 2035 ($/MWh of load)",
               xlim=(0, max(vals) * 1.12))
        ax.grid(axis="x", alpha=0.3)
    from matplotlib.patches import Patch
    fig.legend(handles=[Patch(facecolor=C_ZERO, label="fully zero-carbon"),
                        Patch(facecolor=C_EMIT, label="still burns some gas")],
               loc="lower center", ncol=2, fontsize=9, frameon=False)
    fig.suptitle("How to build a firm zero-carbon datacenter — cost by build option (2035)",
                 fontsize=13)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    return fig


def main():
    print(f"Computing decarbonisation options at {YEAR} (reduced fidelity) …")
    data = {r: options(r) for r in ("us", "eu")}
    os.makedirs(os.path.join(ROOT, "figs"), exist_ok=True)
    fig = build_figure(data)
    fp = os.path.join(ROOT, "figs", "zerocarbon.png")
    fig.savefig(fp, dpi=200, bbox_inches="tight"); plt.close(fig)
    with open(os.path.join(ROOT, "output", "zerocarbon_results.json"), "w") as fh:
        json.dump({"model_version": MODEL_VERSION, "git_commit": git_commit(),
                   "year": YEAR, "data": data}, fh, indent=2)
    for r in ("us", "eu"):
        print(f"  {r.upper()}: " + " | ".join(f"{o[0].split('(')[0].strip()} ${o[1]:.0f}"
              for o in data[r]))
    print(f"Wrote {fp} and output/zerocarbon_results.json")


if __name__ == "__main__":
    main()
