#!/usr/bin/env python3
"""
Europe-wide "green compute zone" scan — score EVERY ~1° land cell of Europe by the
delivered cost of 24/7 carbon-free datacenter power, on real ERA5 weather, and fit a
transparent CF→price surrogate that anyone can check.

For each grid cell (from `tools/fetch_era5_grid.py`):
  1. DISPATCH SCORE — the same fully gas-free solar+wind+LFP+green-H₂ build the siting
     chapter uses (`h2system.h2_system_trajectory`, self-made H₂, tank storage), run at
     the cell's real hourly weather with the imported LCOEs re-anchored to the cell's real
     capacity factors (`cf_consistent_techs`). Scored at the milestone year only
     (`year_subset`) so the whole continent is affordable. EU cost region everywhere —
     the scan isolates GEOGRAPHY (resource quality + weather structure), not national
     cost differences.
  2. FEATURES — plain statistics of the cell's weather: mean solar/wind CF, the depth of
     the worst 5-day and 14-day sun+wind drought (Dunkelflaute), daily sun–wind
     correlation, winter-solar share, worst-year share.
  3. SURROGATE — an ordinary least-squares fit of delivered cost on those features, with
     a held-out validation split. Two nested models are reported: capacity factors only,
     and + temporal structure — the gap between their R² is exactly "what annual averages
     miss". The dispatch value (never the surrogate) is what the map shows.

Outputs: output/eu_scan_results.json, figs/eu_scan_map.png, figs/eu_scan_surrogate.png.

    python tools/scan_eu.py                      # full scan (~1 h on 8 cores)
    python tools/scan_eu.py --sample 40          # quick stratified subsample
    python tools/scan_eu.py --smoke              # machinery test on the curated site npzs
    python tools/scan_eu.py --figs-only          # re-render figures from the JSON
"""
import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lcoe.params import REGIONS, _sys_with, MODEL_VERSION          # noqa: E402
from lcoe.h2system import h2_system_trajectory                     # noqa: E402
from lcoe.reporting import git_commit                              # noqa: E402
from tools.build_locations import cf_consistent_techs              # noqa: E402

GRID_NPZ = os.path.join(ROOT, "output", "era5_grid", "eu_grid_1deg.npz")
OUT_JSON = os.path.join(ROOT, "output", "eu_scan_results.json")
MILESTONE = 2030
HOURS = 8760

# ── features ────────────────────────────────────────────────────────────────────────


def _rolling_min_mean(x, w):
    """Min over all rolling windows of length w of the running mean of x (1D)."""
    c = np.concatenate([[0.0], np.cumsum(x)])
    means = (c[w:] - c[:-w]) / w
    return float(means.min())


def features(sol, win):
    """Plain weather statistics for one cell. sol/win: (n_years, 8760) hourly CF."""
    cf_s, cf_w = float(sol.mean()), float(win.mean())
    blend = sol + win                                   # combined hourly supply, per year
    m = blend.mean() or 1e-9
    worst5 = min(_rolling_min_mean(b, 120) for b in blend) / m
    worst14 = min(_rolling_min_mean(b, 336) for b in blend) / m
    sd = sol.reshape(sol.shape[0], -1, 24).mean(2).ravel()      # daily means
    wd = win.reshape(win.shape[0], -1, 24).mean(2).ravel()
    corr = float(np.corrcoef(sd, wd)[0, 1]) if sd.std() > 0 and wd.std() > 0 else 0.0
    djf = np.r_[0:59 * 24, 334 * 24:HOURS]                       # Dec+Jan+Feb hours
    winter_solar = float(sol[:, djf].mean()) / (cf_s or 1e-9)
    worst_year = float(blend.mean(1).min()) / m
    return {"cf_solar": round(cf_s, 4), "cf_wind": round(cf_w, 4),
            "worst5": round(worst5, 4), "worst14": round(worst14, 4),
            "corr_sw": round(corr, 3), "winter_solar": round(winter_solar, 3),
            "worst_year": round(worst_year, 3)}


# ── per-cell dispatch score (worker) ────────────────────────────────────────────────

_G = {}


def _init_worker(grid_path, milestone):
    d = np.load(grid_path)
    _G["solar"], _G["wind"] = d["solar"], d["wind"]
    _G["lat"], _G["lon"], _G["lsm"] = d["lat"], d["lon"], d["lsm"]
    _G["mi"] = milestone


def score_cell(c):
    """Delivered $/MWh of the gas-free H₂ system at the milestone year, cell c."""
    sol, win = _G["solar"][c].astype(float), _G["wind"][c].astype(float)
    cf_s, cf_w = float(sol.mean()), float(win.mean())
    reg = REGIONS["eu"]
    solar_t, wind_t = cf_consistent_techs(reg, "eu", cf_s, cf_w)
    sysp = _sys_with(reg["sys"], n_mc_weather=sol.shape[0])
    j = _G["mi"] - 2025
    wy = [(sol[k], win[k]) for k in range(sol.shape[0])]
    out = h2_system_trajectory(solar_t, wind_t, reg["battery"], cf_s * 24, 7.0, sysp,
                               j, seed=42, n_mc=sol.shape[0], weather_years=wy,
                               ldes_tech="h2", year_subset=[j])
    f = features(sol, win)
    f.update({"lat": float(_G["lat"][c]), "lon": float(_G["lon"][c]),
              "lsm": round(float(_G["lsm"][c]), 3), "cell": int(c),
              "lcoe": round(float(out["lcoe"][j]), 2),
              "C_sol": round(float(out["C_sol"][j]), 2),
              "C_win": round(float(out["C_win"][j]), 2)})
    return f


# ── surrogate ───────────────────────────────────────────────────────────────────────

CF_FEATS = ["cf_solar", "cf_wind", "cf_sxw", "cf_s2", "cf_w2"]
STRUCT_FEATS = ["worst5", "worst14", "corr_sw", "winter_solar", "worst_year"]


def _design(rows, names):
    X = []
    for r in rows:
        v = dict(r)
        v["cf_sxw"] = r["cf_solar"] * r["cf_wind"]
        v["cf_s2"] = r["cf_solar"] ** 2
        v["cf_w2"] = r["cf_wind"] ** 2
        X.append([1.0] + [v[n] for n in names])
    return np.asarray(X)


def fit_surrogate(rows, seed=7, holdout=0.3):
    """OLS: cost ~ features. Returns fit stats for the CF-only and full models."""
    rng = np.random.default_rng(seed)
    y = np.array([r["lcoe"] for r in rows])
    idx = rng.permutation(len(rows))
    n_te = max(3, int(len(rows) * holdout))
    te, tr = idx[:n_te], idx[n_te:]

    def _fit(names):
        X = _design(rows, names)
        beta, *_ = np.linalg.lstsq(X[tr], y[tr], rcond=None)
        pred = X @ beta
        ss = float(np.sum((y[te] - pred[te]) ** 2))
        st = float(np.sum((y[te] - y[te].mean()) ** 2)) or 1e-9
        # standardized coefficients (on the whole design, for the "what matters" bars)
        sd = X.std(0); sd[sd == 0] = 1.0
        return {"names": names, "beta": [round(float(b), 4) for b in beta],
                "beta_std": [round(float(b * s), 3) for b, s in zip(beta, sd)],
                "r2": round(1.0 - ss / st, 4),
                "mae": round(float(np.abs(y[te] - pred[te]).mean()), 2),
                "pred": [round(float(p), 2) for p in pred]}
    out = {"cf_only": _fit(CF_FEATS), "full": _fit(CF_FEATS + STRUCT_FEATS),
           "holdout_idx": [int(i) for i in te], "n_train": int(len(tr)),
           "n_holdout": int(len(te))}
    return out


# ── figures ─────────────────────────────────────────────────────────────────────────


def _style():
    try:
        import scienceplots  # noqa: F401
        plt.style.use(["science", "no-latex"])
    except Exception:      # noqa: BLE001
        pass


def map_figure(rows, milestone, weather_note, out_png):
    """Choropleth of delivered 24/7 clean cost; curated firm-clean sites overlaid."""
    _style()
    lats = sorted({r["lat"] for r in rows}); lons = sorted({r["lon"] for r in rows})
    li = {v: i for i, v in enumerate(lats)}; lj = {v: i for i, v in enumerate(lons)}
    Z = np.full((len(lats), len(lons)), np.nan)
    for r in rows:
        Z[li[r["lat"]], lj[r["lon"]]] = r["lcoe"]
    vmin = np.nanpercentile(Z, 2); vmax = np.nanpercentile(Z, 98)
    cmap = plt.get_cmap("viridis_r").copy()

    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        proj = ccrs.PlateCarree()
        fig = plt.figure(figsize=(9.8, 8.6))
        ax = plt.axes(projection=ccrs.LambertConformal(central_longitude=5,
                                                       central_latitude=52))
        ax.set_extent([-24, 32, 34, 71], crs=proj)
        ax.add_feature(cfeature.OCEAN, facecolor="#eef2f8", zorder=0)
        ax.add_feature(cfeature.COASTLINE, lw=0.5, edgecolor="#666")
        ax.add_feature(cfeature.BORDERS, lw=0.3, edgecolor="#999")
        lon_e = np.array(lons + [lons[-1] + 1.0]) - 0.5
        lat_e = np.array(lats + [lats[-1] + 1.0]) - 0.5
        pm = ax.pcolormesh(lon_e, lat_e, Z, cmap=cmap, vmin=vmin, vmax=vmax,
                           transform=proj, zorder=2, alpha=0.92)
        tkw = dict(transform=proj)
    except Exception:      # noqa: BLE001  (cartopy absent → plain scatter fallback)
        fig, ax = plt.subplots(figsize=(9.8, 8.6))
        pm = ax.scatter([r["lon"] for r in rows], [r["lat"] for r in rows],
                        c=[r["lcoe"] for r in rows], cmap=cmap, vmin=vmin, vmax=vmax,
                        marker="s", s=52)
        ax.set(xlabel="lon", ylabel="lat")
        tkw = {}

    # Curated firm-clean sites (hydro/geothermal — the strategies a weather scan can't see)
    sp = os.path.join(ROOT, "output", "eu_siting_results.json")
    if os.path.exists(sp):
        es = json.load(open(sp))
        jj = es["ranking_year"] - 2025
        for s in es["sites"]:
            if s["resource"] not in ("hydro", "geothermal"):
                continue
            if not (-24 < s["lon"] < 32 and 34 < s["lat"] < 71):
                continue
            mk = "^" if s["resource"] == "hydro" else "s"
            ax.plot(s["lon"], s["lat"], marker=mk, ms=11, mfc="white", mec="#1d2433",
                    mew=1.4, zorder=6, **tkw)
            ax.annotate(f"${s['delivered'][jj]:.0f}", (s["lon"], s["lat"]),
                        xytext=(7, 4), textcoords="offset points", fontsize=7.5,
                        fontweight="bold", color="#1d2433", zorder=7,
                        **({"xycoords": tkw["transform"]._as_mpl_transform(ax)}
                           if tkw else {}))
    cb = fig.colorbar(pm, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label(f"Delivered 24/7 carbon-free cost, {milestone} ($/MWh of load)")
    from matplotlib.lines import Line2D
    ax.legend(handles=[
        Line2D([], [], marker="^", ls="none", mfc="white", mec="#1d2433", ms=10,
               label="Firm hydro site (curated)"),
        Line2D([], [], marker="s", ls="none", mfc="white", mec="#1d2433", ms=9,
               label="Firm geothermal site (curated)")],
        loc="lower right", fontsize=8, framealpha=0.9)
    ax.set_title(f"What 24/7 carbon-free datacenter power costs across Europe — {milestone}\n"
                 f"(build-it-yourself solar + wind + battery + self-made H₂ · {weather_note} · "
                 "EU costs everywhere)", fontsize=10.5)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out_png}")


def surrogate_figure(rows, sur, out_png):
    """Left: predicted-vs-dispatched scatter (holdout highlighted). Right: what drives
    cost — standardized OLS coefficients of the full model."""
    _style()
    y = np.array([r["lcoe"] for r in rows])
    pf = np.array(sur["full"]["pred"]); pc = np.array(sur["cf_only"]["pred"])
    te = set(sur["holdout_idx"])
    is_te = np.array([i in te for i in range(len(rows))])

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11.5, 5.0),
                                  gridspec_kw={"width_ratios": [1.05, 1]})
    lo = min(y.min(), pf.min()) - 5; hi = max(y.max(), pf.max()) + 5
    ax.plot([lo, hi], [lo, hi], color="#999", lw=1, ls="--", zorder=1)
    ax.scatter(y[~is_te], pf[~is_te], s=14, color="#56B4E9", alpha=0.55, lw=0,
               zorder=2, label=f"training cells (n={sur['n_train']})")
    ax.scatter(y[is_te], pf[is_te], s=26, color="#D55E00", alpha=0.9, lw=0,
               zorder=3, label=f"held-out cells (n={sur['n_holdout']})")
    ax.set(xlabel="Dispatch-model cost ($/MWh)", ylabel="Surrogate prediction ($/MWh)",
           xlim=(lo, hi), ylim=(lo, hi))
    ax.legend(loc="upper left", fontsize=8.5, framealpha=0.9)
    ax.text(0.97, 0.05,
            f"capacity factors only:  R² {sur['cf_only']['r2']:.2f} · "
            f"MAE ${sur['cf_only']['mae']:.0f}\n"
            f"+ weather structure:    R² {sur['full']['r2']:.2f} · "
            f"MAE ${sur['full']['mae']:.0f}",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=9,
            family="monospace",
            bbox=dict(boxstyle="round,pad=0.4", fc="#f7f9fc", ec="#e3e7ee"))
    ax.set_title("Can two capacity factors predict the price?", fontsize=10.5)

    names = ["(intercept)"] + sur["full"]["names"]
    vals = sur["full"]["beta_std"]
    keep = [(n, v) for n, v in zip(names, vals) if n != "(intercept)"]
    labels = {"cf_solar": "solar CF", "cf_wind": "wind CF", "cf_sxw": "solar×wind CF",
              "cf_s2": "solar CF²", "cf_w2": "wind CF²",
              "worst5": "worst 5-day drought", "worst14": "worst 14-day drought",
              "corr_sw": "sun–wind correlation", "winter_solar": "winter-solar share",
              "worst_year": "worst-year share"}
    keep.sort(key=lambda t: abs(t[1]))
    ypos = np.arange(len(keep))
    cols = ["#0072B2" if n in dict(zip(CF_FEATS, CF_FEATS)) else "#009E73"
            for n, _ in keep]
    ax2.barh(ypos, [v for _, v in keep], color=cols, height=0.62)
    ax2.set_yticks(ypos)
    ax2.set_yticklabels([labels.get(n, n) for n, _ in keep], fontsize=8.5)
    ax2.axvline(0, color="#666", lw=0.8)
    ax2.set_xlabel("Standardized effect on cost ($/MWh per SD)", fontsize=9)
    from matplotlib.patches import Patch
    ax2.legend(handles=[Patch(color="#0072B2", label="capacity-factor terms"),
                        Patch(color="#009E73", label="weather-structure terms")],
               loc="lower right", fontsize=8, framealpha=0.9)
    ax2.set_title("What actually moves the price", fontsize=10.5)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out_png}")


# ── main ────────────────────────────────────────────────────────────────────────────


def run_scan(grid_path, milestone, sample, workers):
    d = np.load(grid_path)
    n = d["solar"].shape[0]
    years = [int(y) for y in d["years"]]
    cells = list(range(n))
    if sample and sample < n:                       # stratified-by-latitude subsample
        order = np.argsort(d["lat"])
        cells = [int(order[i]) for i in
                 np.linspace(0, n - 1, sample).round().astype(int)]
    print(f"Scoring {len(cells)}/{n} cells at {milestone} "
          f"(weather {years[0]}–{years[-1]}, {workers} workers) …")
    rows = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(grid_path, milestone)) as ex:
        for k, r in enumerate(ex.map(score_cell, cells, chunksize=4)):
            rows.append(r)
            if (k + 1) % 25 == 0 or k + 1 == len(cells):
                print(f"  {k + 1}/{len(cells)}  (last: {r['lat']:.0f}N {r['lon']:.0f}E "
                      f"→ ${r['lcoe']:.0f})", flush=True)
    rows.sort(key=lambda r: r["cell"])
    return rows, years


def smoke(milestone):
    """Machinery test: score the curated RE-site npzs as if they were scan cells."""
    from lcoe.weather import load_weather_traces
    era5 = os.path.join(ROOT, "output", "era5")
    slugs = [f[:-4] for f in sorted(os.listdir(era5)) if f.endswith(".npz")][:4]
    rows = []
    for k, slug in enumerate(slugs):
        wy = load_weather_traces(os.path.join(era5, f"{slug}.npz"))[:3]
        sol = np.array([s for s, _ in wy]); win = np.array([w for _, w in wy])
        _G.update({"solar": sol[None], "wind": win[None], "lat": np.array([0.0]),
                   "lon": np.array([0.0]), "lsm": np.array([1.0]), "mi": milestone})
        r = score_cell(0); r["cell"] = k; r["slug"] = slug
        rows.append(r)
        print(f"  {slug}: cf_s {r['cf_solar']:.3f} cf_w {r['cf_wind']:.3f} "
              f"worst14 {r['worst14']:.2f} → ${r['lcoe']:.0f}")
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--grid", default=GRID_NPZ)
    p.add_argument("--milestone", type=int, default=MILESTONE)
    p.add_argument("--sample", type=int, default=0, help="score only N cells (0 = all)")
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--figs-only", action="store_true")
    args = p.parse_args(argv)

    if args.smoke:
        rows = smoke(args.milestone)
        print(f"smoke OK ({len(rows)} sites scored)")
        return

    if args.figs_only:
        d = json.load(open(OUT_JSON))
        rows, sur, years = d["cells"], d["surrogate"], d["weather_years"]
    else:
        if not os.path.exists(args.grid):
            sys.exit(f"grid weather not found: {args.grid} — run tools/fetch_era5_grid.py")
        rows, years = run_scan(args.grid, args.milestone, args.sample, args.workers)
        sur = fit_surrogate(rows)
        payload = {
            "milestone": args.milestone, "weather_years": years,
            "n_cells": len(rows), "surrogate": sur, "cells": rows,
            "note": ("gas-free solar+wind+LFP+self-made-H2 system, EU cost region at "
                     "every cell; LCOEs re-anchored to each cell's real ERA5 CF; "
                     "reduced fidelity (single milestone year, 2 optimizer starts)"),
            "provenance": {"model_version": MODEL_VERSION, "git_commit": git_commit()},
        }
        os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
        with open(OUT_JSON, "w") as fh:
            json.dump(payload, fh)
        print(f"Wrote {OUT_JSON} ({len(rows)} cells)")

    wnote = f"real ERA5 {years[0]}–{years[-1]}"
    map_figure(rows, args.milestone, wnote, os.path.join(ROOT, "figs", "eu_scan_map.png"))
    surrogate_figure(rows, sur, os.path.join(ROOT, "figs", "eu_scan_surrogate.png"))


if __name__ == "__main__":
    main()
