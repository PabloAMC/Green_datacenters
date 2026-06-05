#!/usr/bin/env python3
"""
"fig1, per state — the self-made-hydrogen, zero-carbon builds." For each large EU country and
US state, one figure showing the firm off-grid datacenter's delivered-cost trajectory
(2025→2040) for TWO zero-carbon builds that make their own green hydrogen from surplus
renewables (residual bought from the market), against the region's gas baseline:

  • Solar + battery + hydrogen           — NO wind park (solar is easy to permit/site).
  • Solar + wind + battery + hydrogen    — WITH a wind park.

Both are zero-carbon by construction (no gas → no renewable target to set); the only
difference is whether a wind park is allowed. The gap between the two lines is what the wind
park buys you at that location.

Outputs (one panel per state, as you asked):
  • figs/locations_h2/<slug>.png   — an individual figure per location.
  • figs/locations_h2_grid.png     — all locations on one sheet (EU row(s) | US row(s)).
  • output/locations_h2_results.json

Weather: REAL ERA5 reanalysis years from output/era5/<slug>.npz (tools/fetch_locations.py;
defaults to 2015-2025). Each real year is one dispatch sample, so the spread reflects real
interannual variability (Dunkelflaute years, calm years). Region-default carbon/tech costs.

Reduced optimiser fidelity (a cross-location comparison, not the headline) — directional to
roughly ±15% in level; the *ranking* and the *wind gap* are the robust messages.
    make locations-h2
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

import lcoe.h2system as h2s                                       # noqa: E402
from lcoe.params import REGIONS, _sys_with, MODEL_VERSION        # noqa: E402
from lcoe.costs import gas_pure_lcoe, smr_trajectory             # noqa: E402
from lcoe.reporting import git_commit                            # noqa: E402
from lcoe.weather import load_weather_traces                     # noqa: E402
from tools.build_locations import LOCATIONS                      # noqa: E402

YEARS = 15            # 2025 → 2040
# Default ceilings allow a wind park; NO_WIND forces the second entry (wind cap) to 0.
WIND_HI = np.array([24.0, 22.0, 4.0, 720.0])     # C_sol, C_win, electrolyser MW, H₂-store h
NO_WIND_HI = np.array([24.0, 0.0, 4.0, 720.0])

C_NOWIND = "#E69F00"   # solar + battery + H₂ (no wind)
C_WIND = "#0072B2"     # solar + wind + battery + H₂
C_GAS = "#666666"      # region gas baseline (reference)
C_SMR = "#8e44ad"      # small modular reactor (firm reference, not part of the optimisation)


def _trajectory(reg, irr, wind, traces, hi, grid_steps=15, n_mc=10, seed=42):
    sysp = _sys_with(reg["sys"], grid_steps=grid_steps, n_mc_weather=n_mc)
    saved = h2s._HI.copy(); h2s._HI = hi
    try:
        out = h2s.h2_system_trajectory(reg["solar"], reg["wind"], reg["battery"],
                                       irr, wind, sysp, years=YEARS, seed=seed,
                                       weather_years=traces)
    finally:
        h2s._HI = saved
    return ([round(float(v), 2) for v in out["lcoe"]],
            [round(float(v), 4) for v in out["buy_frac"]],
            [round(float(v), 3) for v in out["C_win"]])


def run_location(label, region, irr, wind, slug, lat, lon):
    reg = REGIONS[region]
    npz = os.path.join(ROOT, "output", "era5", f"{slug}.npz")
    traces = load_weather_traces(npz)
    with np.load(npz) as d:
        wyears = [int(y) for y in d["years"]]
    g = reg["gas"]
    lcoe_nw, buy_nw, _ = _trajectory(reg, irr, wind, traces, NO_WIND_HI)
    lcoe_w, buy_w, cwin = _trajectory(reg, irr, wind, traces, WIND_HI)
    return {"label": label, "region": region, "slug": slug, "lat": lat, "lon": lon,
            "weather_years": wyears,
            "cf_solar": round(float(np.mean([s for s, _ in traces])), 3),
            "cf_wind": round(float(np.mean([w for _, w in traces])), 3),
            "years": [2025 + i for i in range(YEARS + 1)],
            "lcoe_nowind": lcoe_nw, "buy_nowind": buy_nw,
            "lcoe_wind": lcoe_w, "buy_wind": buy_w, "wind_build": cwin,
            "gas": [round(float(gas_pure_lcoe(g, i, g.wacc)), 2) for i in range(YEARS + 1)],
            "smr": [round(float(v), 2) for v in smr_trajectory(reg["smr"], YEARS)]}


def _panel(ax, r, top_ylim=None):
    yrs = r["years"]
    ax.plot(yrs, r["gas"], color=C_GAS, lw=1.6, ls="--", label="Gas baseline (emits)")
    ax.plot(yrs, r["smr"], color=C_SMR, lw=1.6, ls="-.", label="Small modular reactor")
    ax.plot(yrs, r["lcoe_nowind"], color=C_NOWIND, lw=2.4,
            label="Solar + battery + hydrogen (no wind)")
    ax.plot(yrs, r["lcoe_wind"], color=C_WIND, lw=2.4,
            label="Solar + wind + battery + hydrogen")
    reg = "Europe" if r["region"] == "eu" else "US"
    ax.set_title(f"{r['label']} ({reg}) · solar CF {r['cf_solar']:.0%}, "
                 f"wind CF {r['cf_wind']:.0%}", fontsize=10)
    ax.set(xlim=(2025, 2040), ylim=(0, top_ylim), xticks=[2025, 2030, 2035, 2040])
    ax.grid(alpha=0.3)


def build_individual(results, span, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for r in results:
        fig, ax = plt.subplots(figsize=(6.2, 4.4))
        _panel(ax, r)
        ax.set_xlabel("Year"); ax.set_ylabel("Delivered cost ($/MWh of load)")
        ax.legend(fontsize=8.5, frameon=True, facecolor="white", framealpha=1, loc="upper right")
        fig.suptitle("Firm zero-carbon datacenter — self-made hydrogen\n"
                     f"(always-on · real ERA5 weather {span})", fontsize=10.5)
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, f"{r['slug']}.png"), dpi=200, bbox_inches="tight")
        plt.close(fig)


def build_grid(results, span):
    # Vertical 5×2 sheet (Europe in the left column, US in the right) — tall and narrow so
    # each panel is large and legible when scrolling the HTML page.
    eu = [r for r in results if r["region"] == "eu"]
    us = [r for r in results if r["region"] == "us"]
    nrow = max(len(eu), len(us))
    fig, axes = plt.subplots(nrow, 2, figsize=(11, 3.3 * nrow), squeeze=False)
    # shared y per region-column over every plotted series so no line is clipped
    def _top(group):
        return max(max(r["lcoe_nowind"] + r["lcoe_wind"] + r["gas"] + r["smr"])
                   for r in group) * 1.05
    eu_top, us_top = _top(eu), _top(us)
    for col, (group, top) in enumerate([(eu, eu_top), (us, us_top)]):
        for row in range(nrow):
            ax = axes[row][col]
            if row < len(group):
                _panel(ax, group[row], top_ylim=top)
                if col == 0:
                    ax.set_ylabel("Delivered cost ($/MWh)")
                if row == len(group) - 1:
                    ax.set_xlabel("Year")
            else:
                ax.axis("off")
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2, fontsize=10, frameon=False)
    fig.suptitle("Firm zero-carbon datacenter, self-made hydrogen — by state: with vs without "
                 f"a wind park\n(always-on · real ERA5 weather {span} · Europe left, US right)",
                 fontsize=13)
    fig.tight_layout(rect=(0, 0.05, 1, 0.97))
    return fig


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out_json = os.path.join(ROOT, "output", "locations_h2_results.json")
    if "--replot" in argv:                       # re-render figures from saved results (no recompute)
        results = json.load(open(out_json))["locations"]
    else:
        missing = [loc[4] for loc in LOCATIONS
                   if not os.path.exists(os.path.join(ROOT, "output", "era5", f"{loc[4]}.npz"))]
        if missing:
            sys.exit(f"Missing ERA5 files for {missing}. Run: "
                     f".venv/bin/python tools/fetch_locations.py --only {' '.join(missing)}")
        print(f"Computing {len(LOCATIONS)} locations × 2 builds (no-wind / with-wind), "
              f"self-made-H₂, real ERA5 weather, reduced fidelity …")
        results = [run_location(*loc) for loc in LOCATIONS]
    allyrs = sorted({y for r in results for y in r["weather_years"]})
    span = f"{allyrs[0]}–{allyrs[-1]}" if allyrs else "?"

    os.makedirs(os.path.join(ROOT, "figs"), exist_ok=True)
    build_individual(results, span, os.path.join(ROOT, "figs", "locations_h2"))
    fig = build_grid(results, span)
    fp = os.path.join(ROOT, "figs", "locations_h2_grid.png")
    fig.savefig(fp, dpi=200, bbox_inches="tight"); plt.close(fig)
    if "--replot" not in argv:
        with open(out_json, "w") as fh:
            json.dump({"model_version": MODEL_VERSION, "git_commit": git_commit(),
                       "builds": ["solar+battery+self-made-H2 (no wind)",
                                  "solar+wind+battery+self-made-H2"],
                       "weather": f"real ERA5 {span}", "locations": results}, fh, indent=2)
    for r in results:
        save = r["lcoe_nowind"][10] - r["lcoe_wind"][10]
        print(f"  {r['label']:<15} 2035  no-wind ${r['lcoe_nowind'][10]:.0f}  "
              f"with-wind ${r['lcoe_wind'][10]:.0f}  (wind saves ${save:.0f})")
    print(f"Wrote {fp}, figs/locations_h2/<slug>.png, and output/locations_h2_results.json")


if __name__ == "__main__":
    main()
