#!/usr/bin/env python3
"""
Fit the synoptic-factor parameters (`syn_persistence` φ, `syn_loading` λ,
`wind_solar_corr` ρ, and — for multi-site input — `site_synoptic_corr` c) from real
weather, turning §12's "single largest accuracy gap" (the Dunkelflaute structure is
*calibrated, not fitted*) into a measured input.

Input: an `.npz` of hourly capacity factor, either
  • single-site  `solar`,`wind` shaped (Y, 8760)         → estimates φ, λ, ρ
  • multi-site   `solar`,`wind` shaped (S, Y, 8760)      → also estimates c
(build it with tools/ingest_weather.py from ERA5/NSRDB).

Identifiability. After removing the day-of-year climatology (to isolate the stochastic
cloud / wind-level component) and mapping each daily series to a standard normal (so the
Beta/Weibull marginal is undone), the model implies, for the daily latent z₁ (cloud) and
z₂ (wind):
    autocorr₁(z) = λ²φ ,   autocorr₂(z) = λ²φ²   ⟹   φ = ac₂/ac₁ ,  λ = √(ac₁/φ)
and ρ = corr(z₁, z₂). For multi-site data, c = mean pairwise corr of the per-site daily
synoptic proxy.

**Accuracy.** This is a fast *moment* estimator. Against the model's full structure (which
also carries short-scale cloud AR(1), within-day wind texture and seasonal modulation) the
estimates are **monotone but biased toward zero** — i.e. they rank/compare datasets
correctly and recover the *sign* of ρ, but the true |φ|, λ and c are typically somewhat
*larger* than reported (e.g. an est. φ≈0.72 corresponds to a true φ≈0.85; an est. site
corr≈0.36 to a true c≈0.6). Use them to compare portfolios and as starting values; for
precise fitting, refine with method-of-simulated-moments against the generator.

Usage:
    python tools/calibrate_synoptic.py weather.npz
    python tools/calibrate_synoptic.py weather.npz --json   # machine-readable
"""
import argparse
import json
import sys

import numpy as np
from scipy.special import ndtri   # inverse standard-normal CDF


def _to_normal(x):
    """Probability-integral transform a 1-D sample to ~N(0,1) via ranks."""
    r = np.argsort(np.argsort(x)) + 1.0
    return ndtri(r / (len(x) + 1.0))


def _deseasonalise_daily(series_2d):
    """(Y,8760) hourly CF → (Y,365) daily mean with the day-of-year climatology divided
    out (isolates the stochastic component that carries the synoptic structure)."""
    Y = series_2d.shape[0]
    daily = series_2d.reshape(Y, 365, 24).mean(2)          # (Y,365)
    clim = daily.mean(0)                                    # per-doy climatology
    clim = np.where(clim > 1e-6, clim, 1e-6)
    return daily / clim                                    # ~1-centred residual


def _autocorr(z, lag):
    a, b = z[:-lag], z[lag:]
    return float(np.corrcoef(a, b)[0, 1])


def estimate(solar, wind):
    """solar/wind shaped (Y,8760) [single site] or (S,Y,8760) [multi-site] → params dict."""
    solar = np.asarray(solar, float); wind = np.asarray(wind, float)
    multi = (solar.ndim == 3)
    sites_s = solar if multi else solar[None]
    sites_w = wind if multi else wind[None]

    ac1, ac2, rhos, f_by_site = [], [], [], []
    for s_site, w_site in zip(sites_s, sites_w):
        rs = _deseasonalise_daily(s_site); rw = _deseasonalise_daily(w_site)
        f_years = []
        for ys, yw in zip(rs, rw):
            z1, z2 = _to_normal(ys), _to_normal(yw)
            ac1.append((_autocorr(z1, 1) + _autocorr(z2, 1)) / 2)
            ac2.append((_autocorr(z1, 2) + _autocorr(z2, 2)) / 2)
            rhos.append(float(np.corrcoef(z1, z2)[0, 1]))
            f_years.append((z1 + z2) / 2.0)               # synoptic proxy (common mode)
        f_by_site.append(np.concatenate(f_years))

    a1, a2 = np.mean(ac1), np.mean(ac2)
    phi = float(np.clip(a2 / a1, 0.0, 0.999)) if a1 > 1e-6 else float("nan")
    lam = float(np.clip(np.sqrt(max(a1 / phi, 0.0)), 0.0, 0.999)) if phi > 1e-6 else float("nan")
    rho = float(np.mean(rhos))

    out = {"syn_persistence": round(phi, 3), "syn_loading": round(lam, 3),
           "wind_solar_corr": round(rho, 3), "n_years": int(sites_s.shape[1] if multi else solar.shape[0]),
           "n_sites": int(len(f_by_site))}
    if multi and len(f_by_site) > 1:
        S = len(f_by_site)
        cc = [np.corrcoef(f_by_site[i], f_by_site[j])[0, 1]
              for i in range(S) for j in range(i + 1, S)]
        out["site_synoptic_corr"] = round(float(np.mean(cc)), 3)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("npz", help="weather .npz (solar/wind (Y,8760) or (S,Y,8760))")
    p.add_argument("--json", action="store_true", help="emit JSON only")
    args = p.parse_args(argv)
    with np.load(args.npz) as d:
        params = estimate(d["solar"], d["wind"])
    if args.json:
        print(json.dumps(params))
        return
    print(f"Fitted synoptic parameters from {args.npz} "
          f"({params['n_years']} years × {params['n_sites']} site(s)):")
    for k in ("syn_persistence", "syn_loading", "wind_solar_corr", "site_synoptic_corr"):
        if k in params:
            print(f"  {k:20s} = {params[k]}")
    print("\nPaste these into a site JSON (sites/), or a SystemParams override. "
          "NOTE: moment estimates are monotone but biased toward zero — true magnitudes "
          "are typically somewhat larger (see the module header). Use for ranking / "
          "starting values; more years tighten them.")


if __name__ == "__main__":
    sys.exit(main())
