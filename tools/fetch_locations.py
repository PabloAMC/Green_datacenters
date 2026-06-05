#!/usr/bin/env python3
"""
Batch-fetch real ERA5 weather for every location in `build_locations.LOCATIONS` into
output/era5/<slug>.npz, using the single-request TIMESERIES path in tools/fetch_era5.py.

One CDS request per location covers the whole multi-year span (~30-60 s each). Defaults to
2015-2025 (11 years) so the per-location trajectories carry real interannual variability
(Dunkelflaute years, calm years) instead of a 3-year snapshot.

    .venv/bin/python tools/fetch_locations.py                 # 2015-2025, all 9 sites
    .venv/bin/python tools/fetch_locations.py --years 2019 2020 2021
    .venv/bin/python tools/fetch_locations.py --only uk texas # a subset of slugs

Needs a CDS key in ~/.cdsapirc and `pip install cdsapi xarray netcdf4` (see fetch_era5.py).
Solar CF uses --solar-factor 1.25 (tilted/tracked plant ≈ horizontal GHI × 1.25), matching
the documented basis in build_locations.py / model_documentation §4.2.
"""
import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.fetch_era5 import fetch_series, _to_8760, _wind_cf      # noqa: E402
from tools.build_locations import LOCATIONS                        # noqa: E402

SOLAR_FACTOR = 1.25   # horizontal GHI → tilted/tracked utility plant (model_doc §4.2)


def fetch_one(client, label, slug, lat, lon, years, out_dir):
    print(f"[{label}] ({lat}, {lon}) → {slug}.npz")
    yr_all, mo_all, da_all, ghi_all, spd_all = fetch_series(client, lat, lon, years)
    sol, win = [], []
    for yr in years:
        m = yr_all == yr
        if not m.any():
            print(f"  !! no data for {yr}; skipping that year")
            continue
        s = _to_8760(np.clip(ghi_all[m] / 1000.0, 0, 1) * SOLAR_FACTOR, mo_all[m], da_all[m])
        w = _to_8760(_wind_cf(spd_all[m]), mo_all[m], da_all[m])
        sol.append(s); win.append(w)
    solar = np.clip(np.array(sol), 0, 1)
    wind = np.clip(np.array(win), 0, 1)
    got = [int(y) for y in years if (yr_all == y).any()]
    out = os.path.join(out_dir, f"{slug}.npz")
    np.savez_compressed(out, solar=solar, wind=wind, synthetic=False,
                        lat=lat, lon=lon, years=np.array(got))
    print(f"  wrote {out}: {solar.shape[0]} yr × 8760 h "
          f"(solar CF {solar.mean():.3f}, wind CF {wind.mean():.3f})")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--years", type=int, nargs="+",
                   default=list(range(2015, 2026)))
    p.add_argument("--only", nargs="+", help="restrict to these slugs")
    args = p.parse_args(argv)

    try:
        import cdsapi
    except ImportError:
        sys.exit("Need the CDS client:  pip install 'cdsapi>=0.7.2' xarray netcdf4")
    client = cdsapi.Client()

    out_dir = os.path.join(ROOT, "output", "era5")
    os.makedirs(out_dir, exist_ok=True)
    locs = [(lbl, slug, lat, lon) for (lbl, _r, _i, _w, slug, lat, lon) in LOCATIONS
            if not args.only or slug in args.only]
    print(f"Fetching {len(locs)} location(s), years {min(args.years)}-{max(args.years)} …\n")
    for lbl, slug, lat, lon in locs:
        fetch_one(client, lbl, slug, lat, lon, args.years, out_dir)
    print("\nDone.")


if __name__ == "__main__":
    main()
