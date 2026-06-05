#!/usr/bin/env python3
"""
"Do you even need a wind park?" — compare the firm off-grid datacenter's delivered cost
vs renewable fraction for two builds: solar + wind + battery, and **solar + battery only**
(no wind). Solar is far easier to permit/site than a wind park, so this quantifies what
dropping wind costs. → figs/solar_only.png + output/solar_only_results.json.

Finding (firm, always-on): solar+battery+gas tops out near ~65% renewable — nights AND
multi-day cloud always fall to gas, and a battery can't shift energy across days — whereas
adding wind reaches ~94%. Below the wall, solar-only is only modestly pricier; above it,
high renewable fractions genuinely need wind (or long-duration storage / H₂).

Reduced optimiser fidelity (a comparison, not the headline).  `make solar-only`
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

from lcoe.params import REGIONS, _sys_with, FIRM, MODEL_VERSION          # noqa: E402
from lcoe.costs import (cumulative_capacity, wright_law, rewacc_lcoe,    # noqa: E402
                        gas_pure_lcoe)
from lcoe.dispatch import ChronologicalSimulator                        # noqa: E402
from lcoe.optimize import optimal_cost_3d                               # noqa: E402
from lcoe.reporting import git_commit                                   # noqa: E402

YEAR = 2035
TARGETS = [0.30, 0.40, 0.50, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
C_WIND = "#3A86FF"   # solar + wind + battery
C_SOLO = "#FF9F1C"   # solar + battery only


def curve(region, no_wind, grid_steps=17, n_mc=15):
    reg = REGIONS[region]
    ov = dict(grid_steps=grid_steps, n_mc_weather=n_mc)
    if no_wind:
        ov["c_win_max"] = 0.0                      # solar + battery only
    sysp = _sys_with(reg["sys"], **ov)
    sim = ChronologicalSimulator(sysp, reg["battery"], FIRM,
                                 reg["mean_irr"], reg["mean_wind_ms"], seed=42)
    max_re = 1.0 - float(sim.gas_mean.min())       # firm: f_RE = 1 - f_gas
    i = YEAR - 2025
    s, w, b, g = reg["solar"], reg["wind"], reg["battery"], reg["gas"]
    lsol = rewacc_lcoe(wright_law(s.lcoe_today, s.cumulative_gw_2025,
                                  cumulative_capacity(s, 15), s.learning_rate), s)[i]
    lwin = rewacc_lcoe(wright_law(w.lcoe_today, w.cumulative_gw_2025,
                                  cumulative_capacity(w, 15), w.learning_rate), w)[i]
    cb = cumulative_capacity(b, 15)
    ckwh = wright_law(b.capex_kwh_today, b.cumulative_gwh_2025, cb, b.learning_rate)[i]
    ckw = wright_law(b.capex_kw_today, b.cumulative_gwh_2025, cb, b.learning_rate)[i]
    pts = []
    for R in TARGETS:
        if R > max_re + 0.005:                     # infeasible for this build → stop the line
            break
        out = optimal_cost_3d(sim, R, lsol, lwin, batt=b, capex_batt_kwh=ckwh,
                              capex_batt_kw=ckw, gas=g, year_index=i, sys=sysp)
        pts.append([R, round(float(out[0]), 2)])
    return {"max_re": round(max_re, 3), "gas": round(gas_pure_lcoe(g, i, g.wacc), 1),
            "points": pts}


def build_figure(data):
    # Independent y-axes: US (cheap gas ~$46) and EU (carbon-priced gas ~$148) live on very
    # different cost levels, so a shared scale would clip the EU curves off the top.
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for ax, region, title in [(axes[0], "us", "United States"), (axes[1], "eu", "Europe")]:
        d = data[region]
        for key, col, lbl in [("wind", C_WIND, "Solar + wind + battery"),
                              ("solo", C_SOLO, "Solar + battery only (no wind)")]:
            p = np.array(d[key]["points"])
            ax.plot(p[:, 0] * 100, p[:, 1], "o-", color=col, lw=2.2, label=lbl)
        # the solar-only wall
        wall = d["solo"]["max_re"] * 100
        ax.axvline(wall, color=C_SOLO, ls=":", lw=1.5)
        ax.text(wall - 1, ax.get_ylim()[1] * 0.96, f"solar-only wall ≈{wall:.0f}%",
                color=C_SOLO, fontsize=8, ha="right", va="top", rotation=90)
        ax.axhline(d["wind"]["gas"], color="#444444", ls="--", lw=1.8,
                   label=f"Natural gas (${d['wind']['gas']:.0f}/MWh)")
        ax.set(title=title, xlabel="Renewable fraction (%)", xlim=(28, 92), ylim=(0, None))
        ax.legend(fontsize=8.5, frameon=True, facecolor="white", framealpha=1, loc="upper left")
        ax.grid(alpha=0.3)
    for ax in axes:
        ax.set_ylabel("Delivered cost ($/MWh of load)")
    fig.suptitle(f"Do you need a wind park?  Firm off-grid cost vs renewable fraction "
                 f"in {YEAR}", fontsize=13)
    fig.tight_layout()
    return fig


def main():
    print(f"Computing solar+wind vs solar-only at {YEAR} (reduced fidelity) …")
    data = {r: {"wind": curve(r, False), "solo": curve(r, True)} for r in ("us", "eu")}
    os.makedirs(os.path.join(ROOT, "figs"), exist_ok=True)
    fig = build_figure(data)
    fp = os.path.join(ROOT, "figs", "solar_only.png")
    fig.savefig(fp, dpi=200, bbox_inches="tight"); plt.close(fig)
    payload = {"model_version": MODEL_VERSION, "git_commit": git_commit(),
               "year": YEAR, "data": data}
    with open(os.path.join(ROOT, "output", "solar_only_results.json"), "w") as fh:
        json.dump(payload, fh, indent=2)
    for r in ("us", "eu"):
        print(f"  {r.upper()}: solar+wind reaches {data[r]['wind']['max_re']:.0%}, "
              f"solar-only wall at {data[r]['solo']['max_re']:.0%}")
    print(f"Wrote {fp} and output/solar_only_results.json")


if __name__ == "__main__":
    main()
