#!/usr/bin/env python3
"""
"Where in Europe should you put a zero-carbon datacenter?" — rank a curated set of EU
candidate sites by the **cheapest 24/7 carbon-free delivered power cost**, where each site
uses its *best* clean resource:

  • RE sites (lots of sun and/or wind, e.g. the Canary Islands, Tarifa, Crete): the fully
    gas-free solar+wind+LFP+green-H₂ system (`h2system.h2_system_trajectory`) — the same
    "make/buy H₂" zero-carbon build the headline suite draws, run at the SITE's resource.
  • Geothermal sites (Iceland): firm, zero-carbon geothermal baseload — delivered cost ≈ its
    LCOE (`gas_pure_lcoe(GEOTHERMAL, …)`); RE overbuild is then just a cost premium.
  • Hydro sites (Norway, the Alps): abundant firm-dispatchable hydro baseload — likewise its LCOE.

So every site is scored on the SAME metric — $/MWh of firm, 100%-carbon-free power — and the
three strategies compete head-to-head. Outputs a ranked bar chart (figs/eu_siting.png) and
output/eu_siting_results.json.

Resource data. RE sites use REAL ERA5 (output/era5/<slug>.npz) when present — fetch with
`python tools/build_eu_siting.py --fetch` (needs a CDS key in ~/.cdsapirc) — else fall back to
the illustrative per-site resource below (and say so in the figure). Geothermal/hydro are
weather-independent firm resources, so they need no ERA5.

    python tools/build_eu_siting.py --fetch     # (re)fetch ERA5 for the RE candidates
    python tools/build_eu_siting.py             # build the ranking (real ERA5 if available)
    make eu-siting
"""
import argparse
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

from lcoe.params import REGIONS, GEOTHERMAL, HYDRO, FIRM, _sys_with, MODEL_VERSION  # noqa: E402
from lcoe.costs import gas_pure_lcoe                                          # noqa: E402
from lcoe.h2system import h2_system_trajectory                               # noqa: E402
from lcoe.simulate import run_simulation                                     # noqa: E402
from lcoe.weather import load_weather_traces                                 # noqa: E402
from lcoe.reporting import git_commit                                        # noqa: E402
from tools.build_locations import cf_consistent_techs                        # noqa: E402

YEARS = 15            # 2025–2040
MILESTONE = 2030      # year the bar chart ranks on
ERA5_YEARS = list(range(2018, 2025))   # 7 ERA5 years for the RE candidates

# label, slug, resource ∈ {"re","geothermal","hydro"}, illustrative GHI (kWh/m²/day),
# wind (m/s), lat, lon, baseload-CF (geothermal/hydro only), note.
# CONCRETE named points (a real plant/site, not a country centroid). All priced in the EU
# cost region (EU tech/battery WACC). RE sites are scored on REAL ERA5 at the grid cell of
# these coordinates; firm geothermal/hydro use the resource LCOE (weather-independent).
CANDIDATES = [
    # ── Sun + wind (gas-free RE + H₂; real ERA5) ─────────────────────────────────
    ("Lanzarote (Canary Is.)", "canary_lanzarote", "re", 5.8, 8.0, 29.0, -13.6, None,
     "subtropical sun + steady NE trade winds — the standout sun-and-wind combo"),
    ("Tarifa (Str. of Gibraltar)", "tarifa", "re", 5.2, 8.5, 36.0, -5.6, None,
     "strongest mainland-EU wind (Levante/Poniente) + strong sun"),
    ("Dover Strait (Pas-de-Calais)", "dover_strait", "re", 2.9, 8.2, 50.95, 1.45, None,
     "English Channel: strong, steady wind to complement S-England/N-France solar"),
    ("Heraklion (Crete)", "crete", "re", 5.2, 6.8, 35.3, 25.1, None,
     "high sun + Aegean meltemi wind"),
    ("Gela (Sicily)", "sicily", "re", 5.0, 5.5, 37.3, 14.1, None, "high Mediterranean sun"),
    ("Sines (S. Portugal)", "portugal_sines", "re", 5.0, 6.8, 37.9, -8.8, None,
     "Atlantic sun + coastal wind"),
    ("Thisted (NW Jutland)", "jutland", "re", 2.8, 9.0, 56.5, 8.2, None,
     "North Sea wind, weak sun — wind-dominated"),
    # ── Firm zero-carbon baseload (concrete plants/sites) ────────────────────────
    ("Hellisheiði (Iceland)", "iceland", "geothermal", 2.2, 8.0, 64.04, -21.40, 0.88,
     "Hellisheiði geothermal station — firm high-enthalpy, runs 24/7, no overbuild"),
    ("Aurland (W. Norway)", "norway_hydro", "hydro", 2.5, 6.0, 60.9, 7.19, 0.55,
     "Sognefjord reservoir hydro — cheap firm dispatchable clean power"),
    ("Harsprånget (Lule River, SE)", "sweden_hydro", "hydro", 2.4, 6.5, 66.03, 19.73, 0.55,
     "Sweden's largest hydro plant (Norrland) — abundant firm reservoir hydro"),
    ("Kaprun (Hohe Tauern, AT)", "austria_alps", "hydro", 3.2, 4.5, 47.27, 12.76, 0.55,
     "Alpine reservoir/pumped hydro"),
    ("Buksefjord (Nuuk, Greenland)", "greenland", "hydro", 2.0, 5.5, 64.07, -50.68, 0.55,
     "Greenland is hydro country (~800,000 GWh/yr potential), NOT high-enthalpy "
     "geothermal like Iceland — only marginal low-T prospects (e.g. Tunu)"),
]

COL = {"re": "#56B4E9", "geothermal": "#D55E00", "hydro": "#0072B2"}


def _era5_path(slug):
    return os.path.join(ROOT, "output", "era5", f"{slug}.npz")


def fetch_all(only=None):
    """Fetch ERA5 for the RE candidates (geothermal/hydro need no weather)."""
    import cdsapi
    from tools.fetch_locations import fetch_one
    client = cdsapi.Client()
    out_dir = os.path.join(ROOT, "output", "era5")
    os.makedirs(out_dir, exist_ok=True)
    todo = [(lbl, slug, lat, lon) for (lbl, slug, res, *_rest) in CANDIDATES
            if res == "re" and (only is None or slug in only)
            for lat, lon in [(_rest[2], _rest[3])]]
    print(f"Fetching ERA5 {ERA5_YEARS[0]}–{ERA5_YEARS[-1]} for {len(todo)} RE candidates …")
    for lbl, slug, lat, lon in todo:
        try:
            fetch_one(client, lbl, slug, lat, lon, ERA5_YEARS, out_dir)
        except Exception as e:   # noqa: BLE001
            print(f"  [skip] {slug}: {type(e).__name__}: {e}")


def score_site(cand, grid_steps=15, n_mc=20, seed=42):
    """Cheapest 24/7 carbon-free delivered-cost trajectory for one site (years 2025…)."""
    label, slug, res, irr, wind, lat, lon, cf_base, note = cand
    reg = REGIONS["eu"]
    sysp = _sys_with(reg["sys"], grid_steps=grid_steps, n_mc_weather=n_mc)

    if res in ("geothermal", "hydro"):
        preset = GEOTHERMAL if res == "geothermal" else HYDRO
        lcoe = [round(gas_pure_lcoe(preset, i, preset.wacc, cf=cf_base), 2)
                for i in range(YEARS + 1)]
        return {"label": label, "slug": slug, "resource": res, "lat": lat, "lon": lon,
                "note": note, "years": [2025 + i for i in range(YEARS + 1)],
                "delivered": lcoe, "re85_gas": None, "cf_base": cf_base,
                "weather": "n/a (firm baseload)", "cf_solar": None, "cf_wind": None}

    # RE site: fully gas-free solar+wind+LFP+green-H₂ system, at the site's resource.
    npz = _era5_path(slug)
    weather_years, wsrc, cf_s, cf_w = None, "illustrative", None, None
    solar_t, wind_t = reg["solar"], reg["wind"]
    if os.path.exists(npz):
        weather_years = load_weather_traces(npz)
        cf_s = float(np.mean([s for s, _ in weather_years]))
        cf_w = float(np.mean([w for _, w in weather_years]))
        solar_t, wind_t = cf_consistent_techs(reg, "eu", cf_s, cf_w)
        wsrc = f"ERA5 {ERA5_YEARS[0]}-{ERA5_YEARS[-1]}"
    h2 = h2_system_trajectory(solar_t, wind_t, reg["battery"], irr, wind, sysp,
                              YEARS, seed=seed, n_mc=n_mc, weather_years=weather_years)
    # The cheaper, NOT-fully-clean alternative: a firm 85%-renewable solar+wind+battery
    # build with EU gas covering the residual ~15% (the standard main-model optimisation).
    sim = run_simulation(solar=solar_t, wind=wind_t, battery=reg["battery"], gas=reg["gas"],
                         smr=reg["smr"], sys=sysp, workload=FIRM, mean_irr=irr,
                         mean_wind_ms=wind, years=YEARS, reliabilities=[0.85], seed=seed,
                         weather_years=weather_years)
    sc85 = sim["scenarios"][0.85]
    re85 = [round(float(v), 2) for v in sc85["opt_delivered"]]
    # Achieved renewable fraction — at low-wind sites an 85%-VARIABLE-RE firm build hits the
    # solar+battery wall (infeasible), and the returned cost is a penalty optimum, not a real
    # 85% build. Flag those so the figure/table don't show a misleading number.
    re85_re = [round(float(v), 3) for v in sc85["opt_re"]]
    return {"label": label, "slug": slug, "resource": res, "lat": lat, "lon": lon,
            "note": note, "years": [2025 + i for i in range(YEARS + 1)],
            "delivered": [round(float(v), 2) for v in h2["lcoe"]],
            "re85_gas": re85, "re85_re": re85_re,
            "buy_frac": [round(float(v), 3) for v in h2["buy_frac"]],
            "weather": wsrc, "cf_solar": round(cf_s, 3) if cf_s else None,
            "cf_wind": round(cf_w, 3) if cf_w else None}


def build_figure(results, mi):
    """Horizontal bar chart ranking sites by delivered $/MWh at year `mi` (cheapest top)."""
    j = mi - 2025
    rows = sorted(results, key=lambda r: r["delivered"][j])
    labels = [r["label"] for r in rows]
    vals = [r["delivered"][j] for r in rows]
    cols = [COL[r["resource"]] for r in rows]
    fig, ax = plt.subplots(figsize=(9.5, 6.2))
    y = np.arange(len(rows))
    ax.barh(y, vals, color=cols, edgecolor="black", lw=0.4)
    ax.set_yticks(y); ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    for yi, v in zip(y, vals):
        ax.text(v + 1.5, yi, f"${v:.0f}", va="center", fontsize=8.5)
    # For sun+wind sites, overlay the cheaper 85%-RE + gas build (not zero-carbon) as a
    # diamond — the gap to the bar end is the premium for going fully carbon-free.
    has85 = False
    for yi, r in zip(y, rows):
        v85 = r.get("re85_gas")
        feasible = r.get("re85_re") and r["re85_re"][j] >= 0.83   # 85% target actually met
        if v85 is not None and feasible:
            has85 = True
            ax.plot(v85[j], yi, marker="D", color="#333", ms=7, zorder=6,
                    markeredgecolor="white", markeredgewidth=0.7)
            ax.text(v85[j], yi - 0.34, f"${v85[j]:.0f}", va="bottom", ha="center",
                    fontsize=7, color="#333")
        elif r["resource"] == "re":   # 85% variable-RE infeasible here (too little wind)
            ax.text(r["delivered"][j] + 9, yi, "85% RE infeasible (low wind)",
                    va="center", fontsize=6.5, color="#999", style="italic")
    # gas reference lines (the dirty alternative)
    eu_gas = REGIONS["eu"]["gas"]; us_gas = REGIONS["us"]["gas"]
    g_eu = gas_pure_lcoe(eu_gas, j, eu_gas.wacc); g_us = gas_pure_lcoe(us_gas, j, us_gas.wacc)
    ax.axvline(g_eu, color="#6B705C", ls="--", lw=1.5, label=f"EU gas {mi} (${g_eu:.0f})")
    ax.axvline(g_us, color="#999999", ls=":", lw=1.5, label=f"US gas {mi} (${g_us:.0f})")
    from matplotlib.patches import Patch
    from matplotlib.lines import Line2D
    handles = [Patch(color=COL["re"], label="Solar+wind+battery+green-H₂ (gas-free)"),
               Patch(color=COL["geothermal"], label="Geothermal (firm)"),
               Patch(color=COL["hydro"], label="Hydro (firm)")]
    if has85:
        handles.append(Line2D([0], [0], marker="D", color="w", markerfacecolor="#333",
                              markeredgecolor="white", markersize=8,
                              label="Same site at 85% RE + gas (15% gas; not zero-carbon)"))
    leg1 = ax.legend(handles=handles, loc="lower right", fontsize=8, frameon=True,
                     facecolor="white", framealpha=1, title="Build option")
    ax.add_artist(leg1)
    ax.legend(loc="upper right", fontsize=8, frameon=True, facecolor="white", framealpha=1)
    real = any(r.get("weather", "").startswith("ERA5") for r in results)
    src = "real ERA5 weather" if real else "illustrative resource"
    ax.set(xlabel=f"Delivered 24/7 carbon-free cost in {mi} ($/MWh of load)", xlim=(0, None),
           title=f"Best zero-carbon power for an EU datacenter, by location ({mi}, firm · {src})")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    return fig


def build_map(results, mi):
    """Map of EU candidate sites, coloured by delivered 24/7 carbon-free $/MWh at year `mi`
    (green = cheap), marker shape by clean resource. Uses cartopy for coastlines/borders if
    available; otherwise falls back to a latitude-corrected lon/lat scatter (no basemap)."""
    j = mi - 2025
    vals = [r["delivered"][j] for r in results]
    vmin, vmax = min(vals), min(max(vals), 175)
    cmap = plt.get_cmap("RdYlGn_r")
    norm = plt.Normalize(vmin, vmax)
    marker = {"re": "o", "geothermal": "^", "hydro": "s"}
    extent = [-54, 30, 28, 70]   # SW Greenland → E. Mediterranean (incl. Nuuk at −50.7°)

    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        proj = ccrs.PlateCarree()
        fig = plt.figure(figsize=(13, 7.5))
        ax = plt.axes(projection=proj)
        ax.set_extent(extent, crs=proj)
        ax.add_feature(cfeature.OCEAN, facecolor="#EAF2F8")
        ax.add_feature(cfeature.LAND, facecolor="#F7F7F2")
        ax.add_feature(cfeature.COASTLINE, lw=0.5, edgecolor="#888")
        ax.add_feature(cfeature.BORDERS, lw=0.4, edgecolor="#BBB")
        tkw = dict(transform=proj)
        have_map = True
    except Exception as e:   # noqa: BLE001  (cartopy/NE data absent → plain scatter)
        print(f"  [map] cartopy unavailable ({type(e).__name__}); plain lon/lat scatter.")
        fig, ax = plt.subplots(figsize=(13, 7.5))
        ax.set_xlim(extent[0], extent[1]); ax.set_ylim(extent[2], extent[3])
        import math as _m
        ax.set_aspect(1.0 / _m.cos(_m.radians(48)))   # latitude aspect correction
        ax.set_facecolor("#EAF2F8")
        tkw = {}
        have_map = False

    for res in ("re", "geothermal", "hydro"):
        pts = [r for r in results if r["resource"] == res]
        if not pts:
            continue
        ax.scatter([r["lon"] for r in pts], [r["lat"] for r in pts],
                   c=[r["delivered"][j] for r in pts], cmap=cmap, norm=norm,
                   marker=marker[res], s=190, edgecolor="black", lw=0.7, zorder=5, **tkw)
    for r in results:                      # labels: site + $value
        ax.annotate(f"{r['label'].split(' (')[0]}\n${r['delivered'][j]:.0f}",
                    (r["lon"], r["lat"]), xytext=(6, 4), textcoords="offset points",
                    fontsize=7.5, fontweight="bold", zorder=6)
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm); sm.set_array([])
    cb = fig.colorbar(sm, ax=ax, shrink=0.6, pad=0.02)
    cb.set_label(f"Delivered 24/7 carbon-free cost in {mi} ($/MWh)")
    from matplotlib.lines import Line2D
    handles = [Line2D([0], [0], marker="o", color="w", markerfacecolor="#999",
                      markeredgecolor="k", markersize=11, label="Solar+wind+battery+H₂"),
               Line2D([0], [0], marker="^", color="w", markerfacecolor="#999",
                      markeredgecolor="k", markersize=12, label="Geothermal (firm)"),
               Line2D([0], [0], marker="s", color="w", markerfacecolor="#999",
                      markeredgecolor="k", markersize=11, label="Hydro (firm)")]
    # Legend BELOW the map (a box in the upper-right would occlude the Nordic markers,
    # e.g. Sweden at ~64.5°N/18°E).
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.04), ncol=3,
              fontsize=8.5, frameon=True, facecolor="white", framealpha=1,
              title="Clean resource", title_fontsize=9)
    real = any(r.get("weather", "").startswith("ERA5") for r in results)
    ax.set_title(f"Where to site a zero-carbon EU datacenter — delivered firm clean power, {mi}\n"
                 f"({'real ERA5 weather' if real else 'illustrative resource'}; "
                 f"green = cheaper)", fontsize=12)
    fig.tight_layout()
    return fig


def main(argv=None):
    p = argparse.ArgumentParser(description="Rank EU sites by cheapest 24/7 carbon-free power.")
    p.add_argument("--fetch", action="store_true", help="(re)fetch ERA5 for the RE candidates first")
    p.add_argument("--only", nargs="+", help="restrict --fetch to these slugs")
    p.add_argument("--year", type=int, default=MILESTONE, help=f"ranking year (default {MILESTONE})")
    p.add_argument("--grid-steps", type=int, default=15)
    p.add_argument("--mc", type=int, default=20)
    args = p.parse_args(argv)

    if args.fetch:
        fetch_all(only=args.only)

    print(f"Scoring {len(CANDIDATES)} EU candidate sites (firm, zero-carbon) …")
    results = []
    for cand in CANDIDATES:
        r = score_site(cand, grid_steps=args.grid_steps, n_mc=args.mc)
        results.append(r)
        j = args.year - 2025
        extra = (f"  | 85% RE+gas ${r['re85_gas'][j]:5.0f}" if r.get("re85_gas") else "")
        print(f"  {r['label']:<26} {r['resource']:<10} {args.year}: ${r['delivered'][j]:5.0f}/MWh"
              f"{extra}  ({r['weather']})")

    os.makedirs(os.path.join(ROOT, "figs"), exist_ok=True)
    fig = build_figure(results, args.year)
    figpath = os.path.join(ROOT, "figs", "eu_siting.png")
    fig.savefig(figpath, dpi=200, bbox_inches="tight"); plt.close(fig)
    mapfig = build_map(results, args.year)
    mappath = os.path.join(ROOT, "figs", "eu_siting_map.png")
    mapfig.savefig(mappath, dpi=200, bbox_inches="tight"); plt.close(mapfig)

    real = any(r.get("weather", "").startswith("ERA5") for r in results)
    payload = {"model_version": MODEL_VERSION, "git_commit": git_commit(),
               "ranking_year": args.year, "weather": "real ERA5" if real else "illustrative",
               "era5_years": [ERA5_YEARS[0], ERA5_YEARS[-1]],
               "note": ("Cheapest 24/7 carbon-free delivered cost per site: RE sites = gas-free "
                        "solar+wind+LFP+green-H₂; geothermal/hydro = firm-clean baseload LCOE. "
                        "EU cost region; illustrative resource where ERA5 absent."),
               "sites": results}
    with open(os.path.join(ROOT, "output", "eu_siting_results.json"), "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nWrote {figpath}, {mappath}, and output/eu_siting_results.json")


if __name__ == "__main__":
    main()
