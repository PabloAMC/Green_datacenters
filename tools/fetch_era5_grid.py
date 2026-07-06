#!/usr/bin/env python3
"""
Fetch ERA5 hourly weather for a whole EUROPE GRID (default 1.0°) and convert it to the
scan's grid weather file — the data substrate for `tools/scan_eu.py` (the Europe-wide
"green compute zone" screening map).

Unlike `fetch_era5.py` (one point per request via the timeseries dataset), this asks the
gridded `reanalysis-era5-single-levels` dataset for an AREA, regridded server-side to the
requested resolution — a handful of year-sized requests instead of ~800 point requests.

    python tools/fetch_era5_grid.py --years 2019 2020 2021          # ~fetch + convert
    python tools/fetch_era5_grid.py --years 2019 2020 2021 --convert-only

Output: output/era5_grid/eu_grid_<deg>deg.npz with, for every grid cell that has any
land (ERA5 land-sea mask ≥ --min-lsm, keeping islands like Crete or Gran Canaria):
    lat, lon  (cell centres, 1D, n_cells)
    lsm       (land fraction, n_cells)
    solar     (n_cells, n_years, 8760)  hourly solar CF  = clip(GHI/1000)×--solar-factor
    wind      (n_cells, n_years, 8760)  hourly wind CF   (model's IEC power curve @100 m)
    years     (n_years,)

Conversion is identical to fetch_era5.py / the per-state location tools (GHI×1.25 solar
anchor, same wind power curve), so scan cells and curated sites stay on one basis.
Needs ~/.cdsapirc (see fetch_era5.py header). Raw yearly .nc files are cached in
output/era5_grid/raw/ and requests are skipped if the file already exists (resumable).
"""
import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.fetch_era5 import _wind_cf, _to_8760, _open, HOURS   # noqa: E402

# Europe box (N, W, S, E) — Iceland to the Aegean, Canaries excluded (below 34N; they are
# already covered as a curated point site), Nordics included.
AREA = [72, -25, 34, 32]
OUT_DIR = os.path.join(ROOT, "output", "era5_grid")
VARS = ["surface_solar_radiation_downwards",
        "100m_u_component_of_wind", "100m_v_component_of_wind"]


def _raw_path(year, month, deg):
    return os.path.join(OUT_DIR, "raw", f"eu_{deg}deg_{year}_{month:02d}.nc")


def fetch_month(client, year, month, deg):
    """One area request for a month of hourly ssrd/u100/v100 (a full year exceeds the
    CDS cost limit at hourly resolution, even regridded — 403 'request too large')."""
    fn = _raw_path(year, month, deg)
    if os.path.exists(fn) and os.path.getsize(fn) > 1e5:
        return fn
    os.makedirs(os.path.dirname(fn), exist_ok=True)
    print(f"  [{year}-{month:02d}] requesting ERA5 {deg}° Europe hourly …", flush=True)
    client.retrieve("reanalysis-era5-single-levels", {
        "product_type": ["reanalysis"],
        "variable": VARS,
        "year": [str(year)],
        "month": [f"{month:02d}"],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "time": [f"{h:02d}:00" for h in range(24)],
        "area": AREA,
        "grid": [deg, deg],
        "data_format": "netcdf",
        "download_format": "unarchived",
    }, fn)
    return fn


def fetch_lsm(client, deg):
    """Land-sea mask (time-invariant) — one tiny request."""
    fn = os.path.join(OUT_DIR, "raw", f"eu_{deg}deg_lsm.nc")
    if os.path.exists(fn):
        return fn
    os.makedirs(os.path.dirname(fn), exist_ok=True)
    print("  requesting land-sea mask …")
    client.retrieve("reanalysis-era5-single-levels", {
        "product_type": ["reanalysis"],
        "variable": ["land_sea_mask"],
        "year": ["2020"], "month": ["01"], "day": ["01"], "time": ["00:00"],
        "area": AREA, "grid": [deg, deg],
        "data_format": "netcdf", "download_format": "unarchived",
    }, fn)
    return fn


def convert(years, deg, min_lsm, solar_factor):
    """Raw yearly .nc → one compact all-cells npz (land cells only)."""
    import xarray as xr
    ds0 = _open(_raw_path(years[0], 1, deg))
    lats = np.asarray(ds0["latitude"].values, float)
    lons = np.asarray(ds0["longitude"].values, float)
    ds0.close()
    lsm_ds = _open(os.path.join(OUT_DIR, "raw", f"eu_{deg}deg_lsm.nc"))
    lsm = np.asarray(lsm_ds["lsm"].values, float).reshape(len(lats), len(lons))
    lsm_ds.close()

    keep = lsm >= min_lsm                     # (nlat, nlon) — keeps islands
    ii, jj = np.where(keep)
    n = len(ii)
    print(f"  {n} land cells (of {keep.size}) at lsm ≥ {min_lsm}")
    sol = np.empty((n, len(years), HOURS), np.float32)
    win = np.empty((n, len(years), HOURS), np.float32)
    for k, yr in enumerate(years):
        ds = xr.concat([_open(_raw_path(yr, m, deg)) for m in range(1, 13)], dim="valid_time")
        tname = "valid_time" if "valid_time" in ds.coords else "time"
        t = ds[tname].dt
        months, days = t.month.values, t.day.values
        ghi = np.clip(np.asarray(ds["ssrd"].values, float), 0, None) / 3600.0   # (T, lat, lon)
        u = np.asarray(ds[("u100" if "u100" in ds else "100m_u_component_of_wind")].values, float)
        v = np.asarray(ds[("v100" if "v100" in ds else "100m_v_component_of_wind")].values, float)
        spd = np.sqrt(u ** 2 + v ** 2)
        ds.close()
        scf = np.clip(ghi / 1000.0, 0, 1) * solar_factor
        wcf = _wind_cf(spd)
        for c in range(n):
            sol[c, k] = _to_8760(scf[:, ii[c], jj[c]], months, days)
            win[c, k] = _to_8760(wcf[:, ii[c], jj[c]], months, days)
        print(f"  [{yr}] converted — grid-mean CF: solar {sol[:, k].mean():.3f} "
              f"wind {win[:, k].mean():.3f}")
    out = os.path.join(OUT_DIR, f"eu_grid_{deg}deg.npz")
    np.savez_compressed(out, lat=lats[ii], lon=lons[jj], lsm=lsm[ii, jj],
                        solar=np.clip(sol, 0, 1), wind=np.clip(win, 0, 1),
                        years=np.array(years), solar_factor=solar_factor)
    print(f"Wrote {out} ({os.path.getsize(out)/1e6:.0f} MB): {n} cells × {len(years)} years.")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--years", type=int, nargs="+", default=[2019, 2020, 2021])
    p.add_argument("--deg", type=float, default=1.0, help="grid resolution (default 1.0°)")
    p.add_argument("--min-lsm", type=float, default=0.15,
                   help="keep cells with at least this land fraction (default 0.15)")
    p.add_argument("--solar-factor", type=float, default=1.25,
                   help="GHI→plant-CF anchor, as in the per-state location tools")
    p.add_argument("--convert-only", action="store_true",
                   help="skip fetching; convert cached raw .nc files")
    args = p.parse_args(argv)

    deg = args.deg if args.deg % 1 else int(args.deg)
    if not args.convert_only:
        try:
            import cdsapi
        except ImportError:
            sys.exit("Need the CDS client:  pip install 'cdsapi>=0.7.2' xarray netcdf4")
        client = cdsapi.Client()
        fetch_lsm(client, deg)
        for yr in args.years:
            for mo in range(1, 13):
                fetch_month(client, yr, mo, deg)
    convert(args.years, deg, args.min_lsm, args.solar_factor)


if __name__ == "__main__":
    main()
