#!/usr/bin/env python3
"""
Fetch ERA5 reanalysis for a point and convert it to the model's weather .npz
(`solar`,`wind` hourly capacity factor, shaped (Y, 8760)) for use with `--weather`.

Uses the **ERA5 single-levels TIMESERIES** dataset (`reanalysis-era5-single-levels-
timeseries`, ARCO/Zarr), which is purpose-built for "a few variables at a single point
over many years": the whole multi-year series comes back in ONE request in ~30 s, instead
of chunking the gridded dataset month-by-month (which the new CDS rejects as too large).

This is the one network-bound step, so it lives in its own script with its own optional
dependencies — it is NOT imported by the model.

────────────────────────────────────────────────────────────────────────────────
ONE-TIME SETUP
────────────────────────────────────────────────────────────────────────────────
 1. Save your CDS API key. Log in at https://cds.climate.copernicus.eu/ , open the
    "How to API" page, copy the shown block into ~/.cdsapirc:

        url: https://cds.climate.copernicus.eu/api
        key: <YOUR-PERSONAL-ACCESS-TOKEN>

 2. Accept the licence on the dataset's Download tab (once) — otherwise retrievals 403:
        https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels-timeseries

 3. Install the client + NetCDF readers (extra deps, not in requirements.txt):

        pip install "cdsapi>=0.7.2" xarray netcdf4

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────
    # Northern Virginia (datacenter alley), 3 recent full years, one ~30 s request:
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
    arr = arr[~((months == 2) & (days == 29))]
    if arr.shape[0] != HOURS:                 # fallback: clip/pad defensively
        arr = arr[:HOURS] if arr.shape[0] > HOURS else np.pad(arr, (0, HOURS - arr.shape[0]))
    return arr


def _open(fn):
    """Open a CDS download (the result is delivered inside a .zip) as one dataset."""
    import xarray as xr
    import zipfile
    import tempfile
    if not zipfile.is_zipfile(fn):
        return xr.open_dataset(fn)
    with zipfile.ZipFile(fn) as zf:
        d = tempfile.mkdtemp()
        ncs = [zf.extract(n, d) for n in zf.namelist() if n.endswith(".nc")]
    return xr.merge([xr.open_dataset(p) for p in ncs], compat="override", join="override")


def _read_series(fn):
    """Read a point timeseries NetCDF → (year, month, day, GHI[W/m²], wind-speed[m/s])."""
    ds = _open(fn)
    tname = "valid_time" if "valid_time" in ds.coords else "time"
    t = ds[tname].dt
    years, months, days = t.year.values, t.month.values, t.day.values
    ghi = np.clip(np.asarray(ds["ssrd"].values, float).ravel(), 0, None) / 3600.0
    u = np.asarray(ds[("u100" if "u100" in ds else "100m_u_component_of_wind")].values, float).ravel()
    v = np.asarray(ds[("v100" if "v100" in ds else "100m_v_component_of_wind")].values, float).ravel()
    ds.close()
    return years, months, days, ghi, np.sqrt(u ** 2 + v ** 2)


def fetch_series(client, lat, lon, years):
    """One CDS timeseries request covering all requested years (efficient point retrieval)."""
    y0, y1 = min(years), max(years)
    fn = f"/tmp/era5ts_{lat}_{lon}_{y0}_{y1}.nc"
    if not os.path.exists(fn):
        print(f"  requesting ERA5 timeseries {y0}-{y1} at ({lat}, {lon}) — one request …")
        client.retrieve("reanalysis-era5-single-levels-timeseries", {
            "variable": ["surface_solar_radiation_downwards",
                         "100m_u_component_of_wind", "100m_v_component_of_wind"],
            "location": {"latitude": lat, "longitude": lon},
            "date": [f"{y0}-01-01/{y1}-12-31"],
            "data_format": "netcdf",
        }, fn)
    return _read_series(fn)


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
    args = p.parse_args(argv)

    try:
        import cdsapi
    except ImportError:
        sys.exit("Need the CDS client:  pip install 'cdsapi>=0.7.2' xarray netcdf4")
    client = cdsapi.Client()

    yr_all, mo_all, da_all, ghi_all, spd_all = fetch_series(client, args.lat, args.lon, args.years)
    sol, win = [], []
    for yr in args.years:
        m = (yr_all == yr)
        if not m.any():
            sys.exit(f"no data returned for {yr}")
        s = _to_8760(np.clip(ghi_all[m] / 1000.0, 0, 1) * args.solar_factor, mo_all[m], da_all[m])
        w = _to_8760(_wind_cf(spd_all[m]), mo_all[m], da_all[m])
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


if __name__ == "__main__":
    main()
