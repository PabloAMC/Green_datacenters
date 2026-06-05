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
from dataclasses import replace

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lcoe.params import REGIONS, _sys_with, FIRM, MODEL_VERSION  # noqa: E402
from lcoe.simulate import run_simulation                        # noqa: E402
from lcoe.reporting import git_commit                           # noqa: E402
from lcoe.weather import load_weather_traces                    # noqa: E402

_CF_REF_CACHE = {}


def cf_reference(region):
    """The model's calibration capacity factors — the synthetic-weather CFs the Lazard
    `lcoe_today` values are anchored to (see params.py "CF-CONSISTENCY"). Read once from
    the headline firm-results export (US ≈ solar 0.23 / wind 0.33, EU ≈ 0.16 / 0.29)."""
    if region not in _CF_REF_CACHE:
        fj = json.load(open(os.path.join(ROOT, "output", f"{region}_firm_results.json")))
        cf = fj["simulated_cf"]
        _CF_REF_CACHE[region] = (float(cf["solar"]), float(cf["wind"]))
    return _CF_REF_CACHE[region]


def cf_consistent_techs(reg, region, cf_solar_site, cf_wind_site):
    """Re-anchor solar & wind `lcoe_today` from the calibration CF to THIS site's real CF.

    An LCOE is capex+FOM spread over a specific capacity factor, so feeding real weather at
    a different CF without rescaling under-prices low-CF generation (e.g. wind at a 2%-CF
    site looks as cheap per-MWh as wind at a 30%-CF site). Scaling `lcoe_today` by
    `ref_CF / site_CF` holds the cost per unit *capacity* fixed, so a calm site correctly
    pays a higher $/MWh for wind (and a sunny site a lower $/MWh for solar). LCOE is linear
    in `lcoe_today` (wright_law × rewacc_lcoe), so this scales the whole trajectory.
    Returns (solar, wind) TechParams."""
    ref_s, ref_w = cf_reference(region)
    solar = replace(reg["solar"],
                    lcoe_today=reg["solar"].lcoe_today * ref_s / max(cf_solar_site, 1e-6))
    wind = replace(reg["wind"],
                   lcoe_today=reg["wind"].lcoe_today * ref_w / max(cf_wind_site, 1e-6))
    return solar, wind

RE_TARGET = 0.80   # firm renewable share for the comparison (robustly feasible everywhere)

# label, region, illustrative GHI (kWh/m²/day), illustrative wind (m/s), slug, lat, lon.
# The illustrative resource drives the synthetic figure; lat/lon drive the --real figure
# (real ERA5 from output/era5/<slug>.npz, fetched by tools/fetch_era5.py).
# The seven EU countries and seven US states are the largest data-center markets in each
# region (US states ranked by capacity/count — Virginia, Texas, Ohio, Georgia, California
# lead — plus Arizona and Iowa; EU adds Italy and Poland to the big-five economies). The
# lat/lon is a representative point in each (the real ERA5 figure samples one grid cell there);
# the illustrative irr/wind are only used by the non-real `make locations` figure.
LOCATIONS = [
    # ── Europe (EU gas + EU ETS carbon) ──────────────────────────────────────────
    ("Spain",          "eu", 5.0, 6.2, "spain",    40.0,  -3.7),
    ("France",         "eu", 3.7, 6.8, "france",   47.0,   2.5),
    ("United Kingdom", "eu", 2.7, 8.5, "uk",       53.0,  -1.5),
    ("Germany",        "eu", 3.0, 6.8, "germany",  51.0,  10.0),
    ("Sweden",         "eu", 2.8, 6.8, "sweden",   59.0,  15.0),
    ("Italy",          "eu", 4.0, 3.8, "italy",    45.5,   9.2),  # Milan / Po valley
    ("Poland",         "eu", 2.9, 5.5, "poland",   52.2,  21.0),  # Warsaw
    # ── United States (cheap US gas, no federal carbon) ──────────────────────────
    ("Virginia",       "us", 4.5, 5.8, "virginia", 39.0,  -77.5),  # Ashburn — Data Center Alley
    ("Texas",          "us", 5.5, 8.3, "texas",    32.5, -100.0),
    ("Ohio",           "us", 3.9, 5.5, "ohio",     40.0,  -83.0),  # Columbus / New Albany
    ("Georgia",        "us", 4.6, 3.8, "georgia",  33.7,  -84.4),  # Atlanta
    ("California",     "us", 5.0, 3.5, "california",37.4, -121.9),  # Santa Clara / Silicon Valley
    ("Arizona",        "us", 6.3, 5.8, "arizona",  33.4, -112.0),  # Phoenix
    ("Iowa",           "us", 4.3, 8.3, "iowa",     42.0,  -93.5),
]

# Distinct, print-safe colours (Okabe–Ito + extras), one per location within a panel.
COLS = ["#E69F00", "#56B4E9", "#009E73", "#CC79A7", "#0072B2",
        "#D55E00", "#F0E442", "#7F7F7F", "#117733"]


def run_location(label, region, irr, wind, slug, lat, lon,
                 real=False, grid_steps=17, n_mc=22, years=15, seed=42):
    reg = REGIONS[region]
    sysp = _sys_with(reg["sys"], grid_steps=grid_steps, n_mc_weather=n_mc)
    weather_years = None
    wyears = []
    solar_t, wind_t = reg["solar"], reg["wind"]
    if real:                                   # drive the dispatch with real ERA5 years
        npz = os.path.join(ROOT, "output", "era5", f"{slug}.npz")
        weather_years = load_weather_traces(npz)
        with np.load(npz) as d:
            wyears = [int(y) for y in d["years"]]
        cf_s = float(np.mean([s for s, _ in weather_years]))
        cf_w = float(np.mean([w for _, w in weather_years]))
        solar_t, wind_t = cf_consistent_techs(reg, region, cf_s, cf_w)  # re-anchor LCOE to site CF
    r = run_simulation(solar=solar_t, wind=wind_t, battery=reg["battery"],
                       gas=reg["gas"], smr=reg["smr"], sys=sysp, workload=FIRM,
                       mean_irr=irr, mean_wind_ms=wind, years=years,
                       reliabilities=[RE_TARGET], seed=seed, grid_ppa=reg.get("grid_ppa"),
                       weather_years=weather_years)
    sc = r["scenarios"][RE_TARGET]
    return {"label": label, "region": region, "irr": irr, "wind": wind,
            "lat": lat, "lon": lon, "weather_years": wyears,
            "years": [int(y) for y in r["years"]],
            "delivered": [round(float(v), 2) for v in sc["opt_delivered"]],
            "gas_pure": [round(float(v), 2) for v in r["gas_pure"]],
            "cf_solar": round(r["sim_cf"]["solar"], 3),
            "cf_wind": round(r["sim_cf"]["wind"], 3)}


def build_figure(results, real=False):
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
    yrs = sorted({y for r in results for y in r.get("weather_years", [])})
    span = f"{yrs[0]}–{yrs[-1]}" if yrs else "?"
    src = (f"real ERA5 weather, {span}" if real else "illustrative resource")
    fig.suptitle(f"Off-grid datacenter cost at {RE_TARGET:.0%} renewable, by location "
                 f"(firm / always-on · {src})", fontsize=13)
    fig.tight_layout()
    return fig


def main(argv=None):
    real = "--real" in (argv if argv is not None else sys.argv[1:])
    tag = "real" if real else "illustrative"
    print(f"Computing {len(LOCATIONS)} locations at {RE_TARGET:.0%} renewable "
          f"({tag} weather, reduced fidelity) …")
    results = [run_location(*loc, real=real) for loc in LOCATIONS]
    os.makedirs(os.path.join(ROOT, "figs"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
    fig = build_figure(results, real=real)
    suffix = "_real" if real else ""
    figpath = os.path.join(ROOT, "figs", f"locations_fig1{suffix}.png")
    fig.savefig(figpath, dpi=200, bbox_inches="tight"); plt.close(fig)
    yrs = sorted({y for r in results for y in r.get("weather_years", [])})
    span = f"{yrs[0]}-{yrs[-1]}" if yrs else "?"
    payload = {"model_version": MODEL_VERSION, "git_commit": git_commit(),
               "re_target": RE_TARGET,
               "weather_span": span if real else None,
               "note": (f"real ERA5 {span} (solar from horizontal GHI×1.25); region-default "
                        "gas/carbon" if real else
                        "illustrative resource; region-default gas/carbon"),
               "locations": results}
    with open(os.path.join(ROOT, "output", f"locations{suffix}_results.json"), "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"Wrote {figpath} and output/locations{suffix}_results.json")


if __name__ == "__main__":
    main()
