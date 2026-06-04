#!/usr/bin/env python3
"""
Fetch ERA5 reanalysis for a point and convert it to the model's weather .npz
(`solar`,`wind` hourly capacity factor, shaped (Y, 8760)) for use with `--weather`.

This is the one network-bound step, so it lives in its own script with its own optional
dependencies — it is NOT imported by the model.

────────────────────────────────────────────────────────────────────────────────
ONE-TIME SETUP
────────────────────────────────────────────────────────────────────────────────
 1. Create the Climate Data Store (CDS) API key file. Log in at
    https://cds.climate.copernicus.eu/ , open your profile, copy your Personal Access
    Token, and write ~/.cdsapirc:

        url: https://cds.climate.copernicus.eu/api
        key: <YOUR-PERSONAL-ACCESS-TOKEN>

 2. Accept the licence for "ERA5 hourly data on single levels from 1940 to present"
    (the dataset's Download tab → Terms of use → Accept). Without this, retrievals 403.

 3. Install the client + NetCDF readers (extra deps, not in requirements.txt):

        pip install "cdsapi>=0.7.2" xarray netcdf4

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────
    # Northern Virginia (datacenter alley), 3 recent full years:
    python tools/fetch_era5.py --lat 39.0 --lon -77.5 --years 2019 2020 2021 \
        --out output/virginia.npz

    # then run the model on real weather, and/or fit the Dunkelflaute parameters:
    python datacenter_lcoe.py --region us --re 0.9 --weather output/virginia.npz
    python tools/calibrate_synoptic.py output/virginia.npz

CONVERSION (documented, deliberately simple — refine if you need site precision):
  • Solar PV CF  = clip(GHI / 1000 W/m², 0, 1) × `--solar-factor`, where GHI = ssrd / 3600.
        This uses *horizontal* irradiance with no tilt / tracking / temperature model, so it
        is conservative (a tilted/tracked plant yields more). The script prints the resulting
        annual CF; if it lands below the site's known utility-scale CF (US ~0.20–0.28), raise
        `--solar-factor` to ~1.1–1.3 so the cost basis stays consistent (see model_doc §4.2).
  • Wind CF = the model's own IEC low-specific-power power curve applied to the 100 m wind
        speed √(u100²+v100²) — same curve as lcoe/weather.py, so it matches the synthetic case.
"""
import argparse
import os
import sys

import numpy as np

HOURS = 8760
V_CI, V_R, V_CO = 3.0, 11.0, 25.0   # model's IEC low-specific-power curve (lcoe/weather.py)


def _wind_cf(speed):
    s = np.asarray(speed, float)
    return np.where(s < V_CI, 0.0,
           np.where(s >= V_CO, 0.0,
           np.where(s >= V_R, 1.0, ((s - V_CI) / (V_R - V_CI)) ** 3)))


def _to_8760(arr, months, days):
    """Drop a Feb-29 if present so every year is exactly 8760 hours."""
    arr = np.asarray(arr, float)
    if arr.shape[0] == HOURS:
        return arr
    keep = ~((months == 2) & (days == 29))
    arr = arr[keep]
    if arr.shape[0] != HOURS:                 # fallback: clip/pad defensively
        arr = arr[:HOURS] if arr.shape[0] > HOURS else np.pad(arr, (0, HOURS - arr.shape[0]))
    return arr


def fetch_year(client, lat, lon, year, pad):
    import xarray as xr
    fn = f"/tmp/era5_{lat}_{lon}_{year}.nc"
    if not os.path.exists(fn):
        print(f"  [{year}] requesting ERA5 (this can queue for minutes on the CDS) …")
        client.retrieve("reanalysis-era5-single-levels", {
            "product_type": "reanalysis",
            "variable": ["surface_solar_radiation_downwards",
                         "100m_u_component_of_wind", "100m_v_component_of_wind"],
            "year": str(year),
            "month": [f"{m:02d}" for m in range(1, 13)],
            "day": [f"{d:02d}" for d in range(1, 32)],
            "time": [f"{h:02d}:00" for h in range(24)],
            "area": [lat + pad, lon - pad, lat - pad, lon + pad],   # N, W, S, E
            "data_format": "netcdf",
            "download_format": "unarchived",
        }, fn)
    ds = xr.open_dataset(fn)
    # nearest grid point to the requested coordinate
    latname = "latitude" if "latitude" in ds.coords else "lat"
    lonname = "longitude" if "longitude" in ds.coords else "lon"
    ds = ds.sel({latname: lat, lonname: lon}, method="nearest")
    tname = "valid_time" if "valid_time" in ds.coords else "time"
    t = ds[tname].dt
    months, days = t.month.values, t.day.values
    ssrd = ds["ssrd"].values
    u = ds[("u100" if "u100" in ds else "100m_u_component_of_wind")].values
    v = ds[("v100" if "v100" in ds else "100m_v_component_of_wind")].values
    ds.close()
    ghi = np.clip(ssrd, 0, None) / 3600.0                      # J/m² → W/m²
    return months, days, ghi, np.sqrt(u ** 2 + v ** 2)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--years", type=int, nargs="+", required=True)
    p.add_argument("--out", required=True, help="output .npz")
    p.add_argument("--solar-factor", type=float, default=1.0,
                   help="multiply GHI-based solar CF (raise to ~1.1-1.3 to match a "
                        "tilted/tracked plant's known CF; default 1.0 = conservative horizontal)")
    p.add_argument("--area-pad", type=float, default=0.25, help="lat/lon box half-width (deg)")
    args = p.parse_args(argv)

    try:
        import cdsapi
    except ImportError:
        sys.exit("Need the CDS client:  pip install 'cdsapi>=0.7.2' xarray netcdf4")
    client = cdsapi.Client()

    sol, win = [], []
    for yr in args.years:
        months, days, ghi, spd = fetch_year(client, args.lat, args.lon, yr, args.area_pad)
        s = _to_8760(np.clip(ghi / 1000.0, 0, 1) * args.solar_factor, months, days)
        w = _to_8760(_wind_cf(spd), months, days)
        sol.append(s); win.append(w)
        print(f"  [{yr}] annual CF — solar {s.mean():.3f}  wind {w.mean():.3f}")
    solar = np.clip(np.array(sol), 0, 1)
    wind = np.clip(np.array(win), 0, 1)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    np.savez_compressed(args.out, solar=solar, wind=wind, synthetic=False,
                        lat=args.lat, lon=args.lon, years=np.array(args.years))
    print(f"\nWrote {args.out}: {solar.shape[0]} year(s) × {HOURS} h "
          f"(mean solar CF {solar.mean():.3f}, wind CF {wind.mean():.3f}).")
    print("Sanity-check the CFs against the site's known utility-scale values; if solar is "
          "low, the horizontal-irradiance estimate is conservative — raise --solar-factor.")
    print(f"Next:  python datacenter_lcoe.py --region <us|eu> --re 0.9 --weather {args.out}")


if __name__ == "__main__":
    main()
