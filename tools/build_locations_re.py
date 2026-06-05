#!/usr/bin/env python3
"""
"Is a wind park worth building?" — per-state, gas-backed firm build. Companion to
build_locations_h2.py (the zero-carbon hydrogen version). For each large EU country and US
state, a FAIR with-vs-without-wind comparison: both builds are optimised to the **same
renewable target** (the most a solar + battery + gas system can reach WITHOUT wind at that
site, ~55-68%), and the with-wind build is simply allowed to add a wind park **only if it
lowers cost**. Two trajectories (2025→2040) per panel, against gas and SMR references:

  • Solar + battery + gas (no wind).
  • Solar + wind + battery + gas — wind optional (the optimiser may build 0).

Because wind=0 is always available to the second build, its line is **never above** the
no-wind line: it dips below where wind is genuinely competitive (windy sites) and coincides
where it is not (calm sites). The complementary "you need wind/storage to push past this
ceiling" story lives in build_solar_only.py (the wall) and build_locations_h2.py.

Outputs (one panel per state):
  • figs/locations_re/<slug>.png   — an individual figure per location.
  • figs/locations_re_grid.png     — vertical 7×2 sheet (Europe left, US right).
  • output/locations_re_results.json

Weather: REAL ERA5 reanalysis years from output/era5/<slug>.npz (tools/fetch_locations.py;
defaults 2015-2025). Region-default gas/carbon/tech costs; per-site CF-consistent LCOE (see
build_locations.cf_consistent_techs). Reduced optimiser fidelity — a cross-location
comparison, directional to ~±15% in level.  make locations-re
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

from lcoe.params import REGIONS, _sys_with, FIRM, MODEL_VERSION   # noqa: E402
from lcoe.simulate import run_simulation                         # noqa: E402
from lcoe.costs import smr_trajectory                            # noqa: E402
from lcoe.dispatch import ChronologicalSimulator                 # noqa: E402
from lcoe.reporting import git_commit                            # noqa: E402
from lcoe.weather import load_weather_traces                     # noqa: E402
from tools.build_locations import LOCATIONS, cf_consistent_techs  # noqa: E402

YEARS = 15            # 2025 → 2040
MARGIN = 0.02         # target = (no-wind feasibility ceiling − MARGIN), shared by both builds
GRID_STEPS, N_MC = 15, 10

C_NOWIND = "#E69F00"   # solar + battery + gas (no wind)
C_WIND = "#0072B2"     # solar + wind + battery + gas
C_GAS = "#666666"      # region gas baseline
C_SMR = "#8e44ad"      # small modular reactor (firm reference, not part of the optimisation)


def _sysp(reg, no_wind):
    ov = dict(grid_steps=GRID_STEPS, n_mc_weather=N_MC)
    if no_wind:
        ov["c_win_max"] = 0.0
    return _sys_with(reg["sys"], **ov)


def _max_re(reg, irr, wind, traces, no_wind):
    sim = ChronologicalSimulator(_sysp(reg, no_wind), reg["battery"], FIRM, irr, wind,
                                 seed=42, weather_years=traces)
    return 1.0 - float(sim.gas_mean.min())


def _run(reg, irr, wind, traces, no_wind, target):
    r = run_simulation(solar=reg["solar"], wind=reg["wind"], battery=reg["battery"],
                       gas=reg["gas"], smr=reg["smr"], sys=_sysp(reg, no_wind), workload=FIRM,
                       mean_irr=irr, mean_wind_ms=wind, years=YEARS, reliabilities=[target],
                       seed=42, grid_ppa=reg.get("grid_ppa"), weather_years=traces)
    sc = r["scenarios"][target]
    return ([round(float(v), 2) for v in sc["opt_delivered"]],
            [round(float(v), 2) for v in r["gas_pure"]])


def run_location(label, region, irr, wind, slug, lat, lon):
    reg = REGIONS[region]
    npz = os.path.join(ROOT, "output", "era5", f"{slug}.npz")
    traces = load_weather_traces(npz)
    with np.load(npz) as d:
        wyears = [int(y) for y in d["years"]]
    cf_s = float(np.mean([s for s, _ in traces]))
    cf_w = float(np.mean([w for _, w in traces]))
    solar_t, wind_t = cf_consistent_techs(reg, region, cf_s, cf_w)   # re-anchor LCOE to site CF
    reg = {**reg, "solar": solar_t, "wind": wind_t}
    # Common target = the greenest a no-wind (solar+battery+gas) build can reach here. Both
    # builds aim for it; the with-wind build may add wind only if it lowers cost.
    mx_nw = _max_re(reg, irr, wind, traces, True)
    target = round(mx_nw - MARGIN, 2)
    d_nw, _ = _run(reg, irr, wind, traces, True, target)    # wind banned
    d_w, gas = _run(reg, irr, wind, traces, False, target)  # wind allowed (optional)
    # wind=0 is always available to the wind-allowed build, so its cost must be ≤ no-wind;
    # clamp away any residual optimiser noise so the invariant is exact in the figure.
    d_w = [round(min(w, n), 2) for w, n in zip(d_w, d_nw)]
    smr = [round(float(v), 2) for v in smr_trajectory(reg["smr"], YEARS)]
    return {"label": label, "region": region, "slug": slug, "lat": lat, "lon": lon,
            "weather_years": wyears,
            "cf_solar": round(cf_s, 3), "cf_wind": round(cf_w, 3),
            "years": [2025 + i for i in range(YEARS + 1)],
            "target": target, "maxre_nowind": round(mx_nw, 3),
            "delivered_nowind": d_nw, "delivered_wind": d_w,
            "gas": gas, "smr": smr}


def _panel(ax, r, top_ylim=None):
    yrs = r["years"]
    ax.plot(yrs, r["gas"], color=C_GAS, lw=1.6, ls="--", label="Gas baseline (emits)")
    ax.plot(yrs, r["smr"], color=C_SMR, lw=1.6, ls="-.", label="Small modular reactor")
    ax.plot(yrs, r["delivered_nowind"], color=C_NOWIND, lw=2.4,
            label="Solar + battery + gas (no wind)")
    ax.plot(yrs, r["delivered_wind"], color=C_WIND, lw=2.4,
            label="Solar + wind + battery + gas (wind optional)")
    reg = "Europe" if r["region"] == "eu" else "US"
    ax.set_title(f"{r['label']} ({reg}) · solar CF {r['cf_solar']:.0%}, "
                 f"wind CF {r['cf_wind']:.0%}", fontsize=10)
    ax.text(0.035, 0.05, f"both at {r['target']:.0%} renewable\n(max without wind)",
            transform=ax.transAxes, ha="left", va="bottom", fontsize=7.5, color="#333333")
    ax.set(xlim=(2025, 2040), ylim=(0, top_ylim), xticks=[2025, 2030, 2035, 2040])
    ax.grid(alpha=0.3)


def build_individual(results, span, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for r in results:
        fig, ax = plt.subplots(figsize=(6.2, 4.4))
        _panel(ax, r)
        ax.set_xlabel("Year"); ax.set_ylabel("Delivered cost ($/MWh of load)")
        ax.legend(fontsize=8.5, frameon=True, facecolor="white", framealpha=1, loc="upper right")
        fig.suptitle("Is a wind park worth it? Same renewable target, with vs without wind\n"
                     f"(always-on · real ERA5 weather {span})", fontsize=10.5)
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, f"{r['slug']}.png"), dpi=200, bbox_inches="tight")
        plt.close(fig)


def build_grid(results, span):
    eu = [r for r in results if r["region"] == "eu"]
    us = [r for r in results if r["region"] == "us"]
    nrow = max(len(eu), len(us))
    fig, axes = plt.subplots(nrow, 2, figsize=(11, 3.3 * nrow), squeeze=False)
    # y-max per region-column over EVERY plotted series (so the highest line — often the
    # with-wind build in the cheap-gas US — is never clipped).
    def _top(group):
        return max(max(r["delivered_nowind"] + r["delivered_wind"] + r["gas"] + r["smr"])
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
    fig.suptitle("Is a wind park worth building? Firm cost at the same renewable target, with "
                 f"vs without wind\n(always-on · real ERA5 weather {span} · target = most "
                 "renewables reachable without wind · Europe left, US right)", fontsize=12.5)
    fig.tight_layout(rect=(0, 0.05, 1, 0.97))
    return fig


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out_json = os.path.join(ROOT, "output", "locations_re_results.json")
    if "--replot" in argv:
        results = json.load(open(out_json))["locations"]
    else:
        missing = [loc[4] for loc in LOCATIONS
                   if not os.path.exists(os.path.join(ROOT, "output", "era5", f"{loc[4]}.npz"))]
        if missing:
            sys.exit(f"Missing ERA5 files for {missing}. Run: "
                     f".venv/bin/python tools/fetch_locations.py --only {' '.join(missing)}")
        print(f"Computing {len(LOCATIONS)} locations (gas-backed, same target both builds, wind "
              "optional), real ERA5 weather, reduced fidelity …")
        results = [run_location(*loc) for loc in LOCATIONS]
    allyrs = sorted({y for r in results for y in r["weather_years"]})
    span = f"{allyrs[0]}–{allyrs[-1]}" if allyrs else "?"

    os.makedirs(os.path.join(ROOT, "figs"), exist_ok=True)
    build_individual(results, span, os.path.join(ROOT, "figs", "locations_re"))
    fig = build_grid(results, span)
    fp = os.path.join(ROOT, "figs", "locations_re_grid.png")
    fig.savefig(fp, dpi=200, bbox_inches="tight"); plt.close(fig)
    if "--replot" not in argv:
        with open(out_json, "w") as fh:
            json.dump({"model_version": MODEL_VERSION, "git_commit": git_commit(),
                       "framing": "same target both builds (no-wind feasibility ceiling); "
                                  "wind optional", "weather": f"real ERA5 {span}",
                       "locations": results}, fh, indent=2)
    for r in results:
        save = r["delivered_nowind"][10] - r["delivered_wind"][10]
        print(f"  {r['label']:<15} 2035 @{r['target']:.0%}  no-wind ${r['delivered_nowind'][10]:.0f}"
              f"  wind-optional ${r['delivered_wind'][10]:.0f}  (wind saves ${save:.0f})")
    print(f"Wrote {fp}, figs/locations_re/<slug>.png, and {os.path.basename(out_json)}")


if __name__ == "__main__":
    main()
