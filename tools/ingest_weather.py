#!/usr/bin/env python3
"""
Turn real (or demo) reanalysis weather into the `.npz` that the model's reanalysis
hook consumes — the missing half of the v5.5 seam.

The dispatch can run on measured weather instead of the synthetic generator via
`ChronologicalSimulator(..., weather_years=...)` / the CLI `--weather PATH.npz`. That
hook expects an `.npz` with two arrays, `solar` and `wind`, each shaped **(Y, 8760)** —
Y reanalysis years of hourly capacity factor in [0, 1]. This script produces that file
from the common provider formats, so wiring a real feed is a *data* step, not code.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────
  # (a) From two CSVs of hourly capacity factor (one value per hour; length a
  #     multiple of 8760 — i.e. Y stacked years). Header lines starting with '#'
  #     are ignored; a single data column is expected.
  python tools/ingest_weather.py from-csv solar_cf.csv wind_cf.csv out.npz

  # (b) Demo file from the model's own synthetic generator, clearly labelled as
  #     SYNTHETIC so `--weather` can be exercised end to end before you have real
  #     data wired. NOT a substitute for reanalysis — replace it with (a)/(c).
  python tools/ingest_weather.py demo output/sample_weather_us.npz --region us --years 3

  # Then run the model on it:
  python datacenter_lcoe.py --region us --re 0.9 --weather output/sample_weather_us.npz

────────────────────────────────────────────────────────────────────────────────
WIRING REAL DATA (ERA5 / NSRDB) — the conversion this script's `from-csv` expects
────────────────────────────────────────────────────────────────────────────────
The model wants **capacity factor** (delivered AC power ÷ nameplate), not raw
irradiance/wind speed. Produce one hourly CF series per source per year, then feed
them in as CSVs:

  • ERA5 (ECMWF / Copernicus CDS; needs a free CDS API key):
      solar — hourly `ssrd` [J/m²] → GHI [W/m²] (÷3600) → POA via a transposition /
              tracking model → DC → AC with a system derate (~0.85) and inverter clip;
              normalise to the panel nameplate to get CF.
      wind  — hourly `100m` wind speed → shear to hub height → the IEC power curve in
              `lcoe/weather.py` (cut-in 3, rated 11, cut-out 25 m/s) → CF.
      (ERA5 is NetCDF; read it with xarray/netCDF4 — optional deps, deliberately not
      required here so the model stays pure-numpy. Export each year's CF to CSV, then
      use `from-csv`.)

  • NREL NSRDB (solar) + WIND Toolkit (wind; free NREL API key): both can return site
      hourly CF (or power) directly — resample to 8760 and write CSV.

Keeping the loader file-based (not network-bound) keeps the model deterministic,
offline, and provider-agnostic.
"""
import argparse
import os
import sys

import numpy as np

HOURS = 8760


def _read_cf_csv(path: str) -> np.ndarray:
    """Read a single-column hourly-CF CSV → (Y, 8760). Length must be a multiple of 8760."""
    vals = np.loadtxt(path, comments="#", delimiter=",").ravel().astype(float)
    if vals.size == 0 or vals.size % HOURS != 0:
        raise SystemExit(f"{path}: expected a multiple of {HOURS} hourly values, got {vals.size}")
    if np.nanmin(vals) < -1e-6 or np.nanmax(vals) > 1.0 + 1e-6:
        raise SystemExit(f"{path}: values must be capacity factors in [0,1] "
                         f"(got range {np.nanmin(vals):.3f}–{np.nanmax(vals):.3f}). "
                         f"Convert irradiance/wind-speed to CF first — see the header.")
    return vals.reshape(-1, HOURS)


def cmd_from_csv(args) -> None:
    solar = _read_cf_csv(args.solar_csv)
    wind = _read_cf_csv(args.wind_csv)
    if solar.shape != wind.shape:
        raise SystemExit(f"solar/wind year counts differ: {solar.shape} vs {wind.shape}")
    np.savez_compressed(args.out, solar=solar, wind=wind, synthetic=False)
    print(f"Wrote {args.out}: {solar.shape[0]} year(s) × {HOURS} h "
          f"(solar CF mean {solar.mean():.3f}, wind CF mean {wind.mean():.3f}).")


def cmd_demo(args) -> None:
    # Import lazily so `from-csv` has no dependency on the model package.
    from lcoe.params import REGIONS
    from lcoe.weather import solar_clearsky, generate_weather_portfolio

    cfg = REGIONS[args.region]
    sysp = cfg["sys"]
    clearsky = solar_clearsky(cfg["mean_irr"])
    rng = np.random.default_rng(args.seed)
    sol_rows, win_rows = [], []
    for _ in range(args.years):
        s, w = generate_weather_portfolio(
            clearsky, cfg["mean_wind_ms"], rng,
            n_sites=sysp.n_sites, site_synoptic_corr=sysp.site_synoptic_corr,
            wind_solar_corr=sysp.wind_solar_corr, syn_loading=sysp.syn_loading,
            syn_persistence=sysp.syn_persistence)
        sol_rows.append(s); win_rows.append(w)
    solar = np.array(sol_rows); wind = np.array(win_rows)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    # `synthetic=True` flags this as a stand-in, not measured reanalysis.
    np.savez_compressed(args.out, solar=solar, wind=wind, synthetic=True)
    print(f"Wrote SYNTHETIC demo {args.out}: {args.years} year(s) × {HOURS} h "
          f"(region={args.region}, solar CF {solar.mean():.3f}, wind CF {wind.mean():.3f}).")
    print("  NOTE: this is the model's own synthetic weather, for exercising the "
          "--weather path. Replace with real ERA5/NSRDB via `from-csv` before drawing "
          "siting conclusions.")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("from-csv", help="two hourly-CF CSVs → npz")
    c.add_argument("solar_csv"); c.add_argument("wind_csv"); c.add_argument("out")
    c.set_defaults(func=cmd_from_csv)

    d = sub.add_parser("demo", help="synthetic demo npz (labelled synthetic)")
    d.add_argument("out")
    d.add_argument("--region", choices=["us", "eu"], default="us")
    d.add_argument("--years", type=int, default=3)
    d.add_argument("--seed", type=int, default=42)
    d.set_defaults(func=cmd_demo)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    # Allow running from the repo root without installing the package.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main()
