#!/usr/bin/env python3
"""
"fig1, per state — the renewable-target builds." Companion to build_locations_h2.py, but for
the conventional **gas-backed** firm build rather than the zero-carbon hydrogen one. For each
large EU country and US state, one figure with two trajectories (2025→2040):

  • Solar + battery + gas, NO wind, at a ~55% renewable target — comfortably reachable without
    a wind park (solar + a battery walls out around ~60-68% for a firm load).
  • Solar + wind + battery + gas, WITH wind, at a ~80% renewable target — a wind park lets you
    push much higher.

against the region's gas baseline. The two targets sit either side of the no-wind feasibility
wall (see build_solar_only.py): ~55% is the easy no-wind reach, ~80% needs wind. Where a site
cannot reach a target (e.g. calm Arizona tops out near 77% even with wind, so its with-wind
line is clamped to ~75%), the realised target is shown in the panel.

Outputs (one panel per state):
  • figs/locations_re/<slug>.png   — an individual figure per location.
  • figs/locations_re_grid.png     — vertical 5×2 sheet (Europe left, US right).
  • output/locations_re_results.json

Weather: REAL ERA5 reanalysis years from output/era5/<slug>.npz (tools/fetch_locations.py;
defaults 2015-2025). Region-default gas/carbon/tech costs. Reduced optimiser fidelity — a
cross-location comparison, directional to ~±15% in level.  make locations-re
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
from tools.build_locations import LOCATIONS                      # noqa: E402

YEARS = 15            # 2025 → 2040
DESIRED_NW = 0.55     # no-wind renewable target (comfortably below the solar+battery wall)
DESIRED_W = 0.80      # with-wind renewable target (needs a wind park)
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


def _target(reg, irr, wind, traces, no_wind, desired):
    mx = _max_re(reg, irr, wind, traces, no_wind)
    return round(min(desired, mx - 0.02), 2), round(mx, 3)


def run_location(label, region, irr, wind, slug, lat, lon):
    reg = REGIONS[region]
    npz = os.path.join(ROOT, "output", "era5", f"{slug}.npz")
    traces = load_weather_traces(npz)
    with np.load(npz) as d:
        wyears = [int(y) for y in d["years"]]
    t_nw, mx_nw = _target(reg, irr, wind, traces, True, DESIRED_NW)
    t_w, mx_w = _target(reg, irr, wind, traces, False, DESIRED_W)
    d_nw, _ = _run(reg, irr, wind, traces, True, t_nw)
    d_w, gas = _run(reg, irr, wind, traces, False, t_w)
    smr = [round(float(v), 2) for v in smr_trajectory(reg["smr"], YEARS)]
    return {"label": label, "region": region, "slug": slug, "lat": lat, "lon": lon,
            "weather_years": wyears,
            "cf_solar": round(float(np.mean([s for s, _ in traces])), 3),
            "cf_wind": round(float(np.mean([w for _, w in traces])), 3),
            "years": [2025 + i for i in range(YEARS + 1)],
            "target_nowind": t_nw, "maxre_nowind": mx_nw, "delivered_nowind": d_nw,
            "target_wind": t_w, "maxre_wind": mx_w, "delivered_wind": d_w,
            "gas": gas, "smr": smr}


def _panel(ax, r, top_ylim=None):
    yrs = r["years"]
    ax.plot(yrs, r["gas"], color=C_GAS, lw=1.6, ls="--", label="Gas baseline (emits)")
    ax.plot(yrs, r["smr"], color=C_SMR, lw=1.6, ls="-.", label="Small modular reactor")
    ax.plot(yrs, r["delivered_nowind"], color=C_NOWIND, lw=2.4,
            label="Solar + battery + gas (no wind)")
    ax.plot(yrs, r["delivered_wind"], color=C_WIND, lw=2.4,
            label="Solar + wind + battery + gas")
    reg = "Europe" if r["region"] == "eu" else "US"
    ax.set_title(f"{r['label']} ({reg}) · wind CF {r['cf_wind']:.0%}", fontsize=10)
    ax.text(0.035, 0.05, f"no-wind {r['target_nowind']:.0%} renewable\n"
            f"with-wind {r['target_wind']:.0%} renewable",
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
        fig.suptitle("Firm datacenter — renewable target, with vs without a wind park\n"
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
    fig.suptitle("Firm datacenter, renewable target — by state: ~55% without wind vs ~80% with "
                 f"a wind park\n(always-on · real ERA5 weather {span} · Europe left, US right)",
                 fontsize=13)
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
        print(f"Computing {len(LOCATIONS)} locations × 2 builds (no-wind ~{DESIRED_NW:.0%} / "
              f"with-wind ~{DESIRED_W:.0%}), gas-backed, real ERA5 weather, reduced fidelity …")
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
                       "desired_targets": {"no_wind": DESIRED_NW, "with_wind": DESIRED_W},
                       "weather": f"real ERA5 {span}", "locations": results}, fh, indent=2)
    for r in results:
        print(f"  {r['label']:<15} 2035  no-wind@{r['target_nowind']:.0%} "
              f"${r['delivered_nowind'][10]:.0f}  with-wind@{r['target_wind']:.0%} "
              f"${r['delivered_wind'][10]:.0f}")
    print(f"Wrote {fp}, figs/locations_re/<slug>.png, and {os.path.basename(out_json)}")


if __name__ == "__main__":
    main()
