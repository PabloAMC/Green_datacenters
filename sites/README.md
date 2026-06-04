# Custom sites (`--site`)

Describe a location in a small JSON file and run it without touching code:

```bash
python datacenter_lcoe.py --site sites/example_texas.json --re 0.9
```

A site **inherits** a built-in region's technology, battery, SMR and grid-PPA defaults
via `based_on` (`"us"` or `"eu"`), and overrides only what varies by location.

## Schema

| Key | Meaning |
|-----|---------|
| `label` | Display name for the run / figures. |
| `based_on` | `"us"` or `"eu"` — the region whose tech/battery/SMR/PPA defaults to inherit. |
| `mean_irr` | Site mean GHI, kWh/m²/day (sets solar CF). |
| `mean_wind_ms` | Site mean wind speed, m/s (sets wind CF). |
| *any `GasParams` field* | e.g. `gas_price_mmbtu`, `carbon_price_today`, `carbon_trajectory`, `carbon_price_ceiling`. |
| *any `SystemParams` field* | e.g. `wind_solar_corr`, `n_sites`, `site_synoptic_corr`, `syn_persistence`, `c_sol_max`, `grid_steps`. |

Unknown keys raise an error (so typos surface immediately). Resource quality
(`mean_irr`/`mean_wind_ms`) should sit consistently inside the CF basis of the imported
LCOEs — see `model_documentation.md` §4.2/§4.4.
