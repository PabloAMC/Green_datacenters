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
    ("Gran Canaria (Chira-Soria)", "gran_canaria", "re", 5.6, 7.5, 27.96, -15.60, None,
     "Canary trade winds + strong sun, AND a real pumped-storage scheme (Chira-Soria, "
     "coast-to-1,950 m relief) — unlike flat Lanzarote"),
    ("Tarifa (Str. of Gibraltar)", "tarifa", "re", 5.2, 8.5, 36.0, -5.6, None,
     "strongest mainland-EU wind (Levante/Poniente) + strong sun"),
    ("Dover Strait (Pas-de-Calais)", "dover_strait", "re", 2.9, 8.2, 50.95, 1.45, None,
     "English Channel: strong, steady wind to complement S-England/N-France solar"),
    ("East Crete (meltemi)", "crete", "re", 5.3, 8.0, 35.2, 26.2, None,
     "strong Aegean meltemi wind + high sun; mountainous island → co-located PHS relief"),
    ("SW Sicily (Mazara)", "sicily", "re", 5.0, 7.0, 37.65, 12.6, None,
     "Sicily-Channel wind + high sun; island relief for PHS"),
    ("Sines (S. Portugal)", "portugal_sines", "re", 5.0, 6.8, 37.9, -8.8, None,
     "Atlantic sun + coastal wind"),
    ("Thisted (NW Jutland)", "jutland", "re", 2.8, 9.0, 56.5, 8.2, None,
     "North Sea wind, weak sun — wind-dominated"),
    ("Jura (Switzerland)", "swiss_jura", "re", 3.2, 5.0, 47.1, 7.0, None,
     "best (modest) Swiss wind + solar; Swiss pumped storage. Switzerland is wind-poor — its "
     "real edge is conventional hydro generation"),
    ("Dobrogea (Romania)", "romania_dobrogea", "re", 3.7, 8.5, 44.6, 28.4, None,
     "Romania's Black Sea wind hub (Fântânele-Cogealac); flat coast → firmed by H₂ "
     "(its pumped storage is separate, inland in the Carpathians)"),
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

# Sites where pumped storage is AVAILABLE, judged from the ANU Global Pumped Hydro Atlas
# (Blakers et al.) + known real schemes — i.e. sufficient relief/head (off-river PHS needs
# topography, not flat land). For these we compute BOTH firmings (green-H₂ AND PHS) and show
# them side by side, so the chart never conflates "good site" with "was allowed PHS". PHS-
# capable: the Iberian/Mediterranean sierras (Tarifa, Sines, Sicily, Crete), the Alps
# (Switzerland), the Carpathians (Romania), and Gran Canaria (coast-to-1,950 m; real Chira-
# Soria scheme). NOT PHS-capable (atlas: little/no head): flat Jutland (Denmark) and the
# low-relief Dover Strait/Pas-de-Calais — those are firmed by green H₂ only.
PHS_AVAILABLE = {"gran_canaria", "tarifa", "portugal_sines", "sicily", "crete",
                 "swiss_jura"}   # Romania's wind hub (Dobrogea) is flat coast → H₂, not PHS


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
                "delivered": lcoe, "firming": "firm baseload", "re75_gas": None,
                "cf_base": cf_base, "weather": "n/a (firm baseload)",
                "cf_solar": None, "cf_wind": None}

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
    # FAIRNESS: compute the gas-free build with green H₂ always, and ALSO with PHS where the
    # atlas says pumped storage is available — then report both side by side and take the
    # cheaper as the headline. This decouples site quality from the firming choice.
    def _firm(tech):
        return [round(float(v), 2) for v in h2_system_trajectory(
            solar_t, wind_t, reg["battery"], irr, wind, sysp, YEARS, seed=seed,
            n_mc=n_mc, weather_years=weather_years, ldes_tech=tech)["lcoe"]]
    deliv_h2 = _firm("h2")
    deliv_phs = _firm("phs") if slug in PHS_AVAILABLE else None
    if deliv_phs is not None and deliv_phs[MILESTONE - 2025] < deliv_h2[MILESTONE - 2025]:
        delivered, firming = deliv_phs, "PHS"
    else:
        delivered, firming = deliv_h2, "green-H₂"
    h2 = {"lcoe": delivered}   # headline = cheaper firming
    # The cheaper, NOT-fully-clean alternative: a firm 75%-renewable solar+wind+battery
    # build with EU gas covering the residual ~25%. 75% (not 85%) sits BELOW the ~80%
    # solar+battery wall, so it is feasible at every site — including the low-wind ones —
    # giving a clean, universally-comparable "mostly-renewable + gas" reference.
    sim = run_simulation(solar=solar_t, wind=wind_t, battery=reg["battery"], gas=reg["gas"],
                         smr=reg["smr"], sys=sysp, workload=FIRM, mean_irr=irr,
                         mean_wind_ms=wind, years=YEARS, reliabilities=[0.75], seed=seed,
                         weather_years=weather_years)
    sc75 = sim["scenarios"][0.75]
    re75 = [round(float(v), 2) for v in sc75["opt_delivered"]]
    # Achieved renewable fraction (to flag the rare site where even 75% can't be met).
    re75_re = [round(float(v), 3) for v in sc75["opt_re"]]
    return {"label": label, "slug": slug, "resource": res, "lat": lat, "lon": lon,
            "note": note, "years": [2025 + i for i in range(YEARS + 1)],
            "delivered": delivered,           # headline = cheaper firming
            "firming": firming,               # which firming won
            "delivered_h2": deliv_h2,         # green-H₂-firmed (always computed)
            "delivered_phs": deliv_phs,       # PHS-firmed (None where PHS unavailable)
            "re75_gas": re75, "re75_re": re75_re,
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
    # x-axis cap: a wind-poor outlier (Switzerland's 75%-RE+gas needs a huge battery) would
    # otherwise stretch the axis and squash everyone; cap it and tag off-scale markers "▸$X".
    CAP = 165.0
    # FAIRNESS: for sites where both firmings were computed, mark the ALTERNATIVE firming
    # (the one not chosen) as an open circle — so the H₂↔PHS gap is shown side by side and
    # the ranking can't quietly bury the firming choice inside the site's resource quality.
    has_alt = False
    for yi, r in zip(y, rows):
        if r.get("delivered_h2") is None or r.get("delivered_phs") is None:
            continue
        alt = r["delivered_h2"][j] if r["firming"] == "PHS" else r["delivered_phs"][j]
        has_alt = True
        if alt <= CAP:
            ax.plot(alt, yi, marker="o", mfc="none", mec="#1f4e8c", mew=1.5, ms=9, zorder=6)
        else:
            ax.annotate(f"○ ▸${alt:.0f}", (CAP, yi + 0.28), xytext=(-2, 0),
                        textcoords="offset points", va="center", ha="right",
                        fontsize=6.5, color="#1f4e8c")
    # For sun+wind sites, overlay the cheaper 75%-RE + gas build (not zero-carbon) as a
    # diamond — the gap to the bar end is the premium for going fully carbon-free.
    has75 = False
    for yi, r in zip(y, rows):
        v75 = r.get("re75_gas")
        feasible = r.get("re75_re") and r["re75_re"][j] >= 0.73   # 75% target actually met
        if v75 is not None and feasible:
            has75 = True
            if v75[j] <= CAP:
                ax.plot(v75[j], yi, marker="D", color="#333", ms=7, zorder=6,
                        markeredgecolor="white", markeredgewidth=0.7)
                ax.text(v75[j], yi - 0.34, f"${v75[j]:.0f}", va="bottom", ha="center",
                        fontsize=7, color="#333")
            else:
                ax.annotate(f"◆ ▸${v75[j]:.0f}", (CAP, yi - 0.28), xytext=(-2, 0),
                            textcoords="offset points", va="center", ha="right",
                            fontsize=6.5, color="#333")
        elif r["resource"] == "re":   # even 75% RE infeasible here (very little wind)
            ax.text(r["delivered"][j] + 9, yi, "75% RE infeasible (low wind)",
                    va="center", fontsize=6.5, color="#999", style="italic")
    # gas reference lines (the dirty alternative), labelled inline at the top — no 2nd legend.
    eu_gas = REGIONS["eu"]["gas"]; us_gas = REGIONS["us"]["gas"]
    g_eu = gas_pure_lcoe(eu_gas, j, eu_gas.wacc); g_us = gas_pure_lcoe(us_gas, j, us_gas.wacc)
    for gx, gc, gl in [(g_eu, "#6B705C", "EU"), (g_us, "#999999", "US")]:
        ax.axvline(gx, color=gc, ls="--", lw=1.5)
        ax.text(gx, -0.75, f"{gl} gas {mi}\n${gx:.0f}", ha="center", va="bottom",
                fontsize=7, color=gc, fontweight="bold")
    from matplotlib.patches import Patch
    from matplotlib.lines import Line2D
    handles = [Patch(color=COL["re"], label="Sun+wind+battery, gas-free (bar = cheaper firming)"),
               Patch(color=COL["geothermal"], label="Geothermal (firm)"),
               Patch(color=COL["hydro"], label="Hydro (firm)")]
    if has_alt:
        handles.append(Line2D([0], [0], marker="o", mfc="none", mec="#1f4e8c", mew=1.5,
                              ls="none", markersize=9,
                              label="Alternative firming (the other of H₂ / PHS)"))
    if has75:
        handles.append(Line2D([0], [0], marker="D", color="w", markerfacecolor="#333",
                              markeredgecolor="white", markersize=8,
                              label="Same site at 75% RE + gas (25% gas; not zero-carbon)"))
    # "Build option" legend in the UPPER-RIGHT whitespace — above the short, cheap firm-clean
    # bars (hydro $46 / geothermal $63), which have no right-side markers, so it covers nothing.
    ax.legend(handles=handles, loc="upper right", fontsize=8, frameon=True,
              facecolor="white", framealpha=1, title="Build option")
    real = any(r.get("weather", "").startswith("ERA5") for r in results)
    src = "real ERA5 weather" if real else "illustrative resource"
    ax.set(xlabel=f"Delivered 24/7 carbon-free cost in {mi} ($/MWh of load)", xlim=(0, CAP),
           title=f"Best zero-carbon power for an EU datacenter, by location ({mi}, firm · {src})")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    return fig


# Per-site label nudges (offset points dx,dy + horizontal alignment) to avoid collisions —
# e.g. Switzerland (Linth-Limmern) sits just west of Austria (Kaprun), so label it to the LEFT.
LABEL_OFFSET = {"swiss_alps": (-7, 4, "right")}


def _site_cost(r, j, firming):
    """Delivered $/MWh of site `r` at year-index `j` under a single firming choice.
    Firm baseload (geothermal/hydro) is firming-independent → shown on both maps. A sun+wind
    site returns None where that firming isn't available (e.g. PHS at a flat site)."""
    if r["resource"] in ("geothermal", "hydro"):
        return r["delivered"][j]
    v = r.get("delivered_h2" if firming == "h2" else "delivered_phs")
    return v[j] if v is not None else None


def build_map(results, mi, firming, norm=None):
    """Map of EU candidate sites coloured by delivered 24/7 carbon-free $/MWh at `mi` under ONE
    firming choice (`firming` = 'h2' or 'phs'), so the map never silently picks a firming per
    site. Sun+wind sites are firmed by that option (PHS-only sites omitted on the H₂ map? no —
    H₂ works everywhere; on the PHS map, sites without pumped-storage potential are omitted);
    firm geothermal/hydro appear on both as context. cartopy basemap with a scatter fallback."""
    j = mi - 2025
    cmap = plt.get_cmap("RdYlGn_r")
    if norm is None:
        vv = [c for r in results if (c := _site_cost(r, j, firming)) is not None]
        norm = plt.Normalize(min(vv), min(max(vv), 175))
    extent = [-54, 30, 28, 70]   # SW Greenland → E. Mediterranean (incl. Nuuk at −50.7°)
    is_phs = firming == "phs"
    firm_name = "pumped storage" if is_phs else "green hydrogen"

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
    except Exception as e:   # noqa: BLE001  (cartopy/NE data absent → plain scatter)
        print(f"  [map] cartopy unavailable ({type(e).__name__}); plain lon/lat scatter.")
        fig, ax = plt.subplots(figsize=(13, 7.5))
        ax.set_xlim(extent[0], extent[1]); ax.set_ylim(extent[2], extent[3])
        import math as _m
        ax.set_aspect(1.0 / _m.cos(_m.radians(48)))   # latitude aspect correction
        ax.set_facecolor("#EAF2F8")
        tkw = {}

    mk_for = {"geothermal": "^", "hydro": "s", "re": "o"}
    plotted = [(r, c) for r in results if (c := _site_cost(r, j, firming)) is not None]
    for mk in ("o", "^", "s"):
        pts = [(r, c) for (r, c) in plotted if mk_for[r["resource"]] == mk]
        if not pts:
            continue
        ax.scatter([r["lon"] for r, _ in pts], [r["lat"] for r, _ in pts],
                   c=[c for _, c in pts], cmap=cmap, norm=norm, marker=mk, s=190,
                   edgecolor="black", lw=0.7, zorder=5, **tkw)
    for r, c in plotted:                   # labels: site + $value
        dx, dy, ha = LABEL_OFFSET.get(r["slug"], (6, 4, "left"))
        ax.annotate(f"{r['label'].split(' (')[0]}\n${c:.0f}",
                    (r["lon"], r["lat"]), xytext=(dx, dy), textcoords="offset points",
                    fontsize=7.5, fontweight="bold", ha=ha, zorder=6)
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm); sm.set_array([])
    cb = fig.colorbar(sm, ax=ax, shrink=0.6, pad=0.02)
    cb.set_label(f"Delivered 24/7 carbon-free cost in {mi} ($/MWh)")
    from matplotlib.lines import Line2D
    handles = [Line2D([0], [0], marker="o", color="w", markerfacecolor="#999",
                      markeredgecolor="k", markersize=11,
                      label=f"Sun+wind+battery, firmed by {firm_name}"),
               Line2D([0], [0], marker="^", color="w", markerfacecolor="#999",
                      markeredgecolor="k", markersize=12, label="Geothermal (firm)"),
               Line2D([0], [0], marker="s", color="w", markerfacecolor="#999",
                      markeredgecolor="k", markersize=11, label="Hydro (firm)")]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.04), ncol=3,
              fontsize=8.5, frameon=True, facecolor="white", framealpha=1,
              title="Clean resource", title_fontsize=9)
    real = any(r.get("weather", "").startswith("ERA5") for r in results)
    omit = "  ·  flat sites without PHS potential omitted" if is_phs else ""
    ax.set_title(f"EU datacenter — delivered firm clean power if firmed by {firm_name.upper()}, {mi}\n"
                 f"({'real ERA5 weather' if real else 'illustrative resource'}; green = cheaper"
                 f"{omit})", fontsize=12)
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
        extra = (f"  | 75% RE+gas ${r['re75_gas'][j]:5.0f}" if r.get("re75_gas") else "")
        fmg = f" [{r.get('firming')}]" if r.get("firming") else ""
        print(f"  {r['label']:<28} {r['resource']:<10} {args.year}: ${r['delivered'][j]:5.0f}/MWh"
              f"{fmg}{extra}  ({r['weather']})")

    os.makedirs(os.path.join(ROOT, "figs"), exist_ok=True)
    fig = build_figure(results, args.year)
    figpath = os.path.join(ROOT, "figs", "eu_siting.png")
    fig.savefig(figpath, dpi=200, bbox_inches="tight"); plt.close(fig)
    # Two maps — one per firming choice — so the map never silently picks H₂ vs PHS per site.
    # Shared colour scale across both for a like-for-like read.
    j = args.year - 2025
    allc = [c for r in results for fm in ("h2", "phs")
            if (c := _site_cost(r, j, fm)) is not None]
    shared = plt.Normalize(min(allc), min(max(allc), 175))
    for fm in ("h2", "phs"):
        mf = build_map(results, args.year, fm, norm=shared)
        mf.savefig(os.path.join(ROOT, "figs", f"eu_siting_map_{fm}.png"),
                   dpi=200, bbox_inches="tight"); plt.close(mf)

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
    print(f"\nWrote {figpath}, figs/eu_siting_map_h2.png, figs/eu_siting_map_phs.png, "
          "and output/eu_siting_results.json")


if __name__ == "__main__":
    main()
