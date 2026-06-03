from __future__ import annotations

"""Console summary table and machine-readable CSV/JSON export."""
import csv
import json
import os
from typing import Dict, Tuple

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# 10. SUMMARY TABLE
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(results, region="US"):
    yrs = results["years"]
    milestones = [y for y in [2025, 2028, 2030, 2035, 2040] if y <= yrs[-1]]
    cf = results["sim_cf"]
    print(f"\n{'═'*102}")
    print(f"  DATACENTER POWER COST — {region}  |  ρ(sol,wind)={results['wind_solar_corr']:.2f}")
    print(f"  Simulated CF:  Solar={cf['solar']:.3f}   Wind={cf['wind']:.3f}")
    if "grid_ppa" in results:
        gp = results["grid_ppa"]
        ref = "  ".join(f"{y}:${gp[y-yrs[0]]:.0f}" for y in milestones)
        print(f"  Grid+RE PPA reference (on-grid alt., $/MWh):  {ref}")
    if "grid_cfe" in results:
        gc = results["grid_cfe"]
        ref = "  ".join(f"{y}:${gc[y-yrs[0]]:.0f}" for y in milestones)
        print(f"  Grid 24/7 CFE reference (hourly clean, $/MWh): {ref}")
    print(f"{'═'*102}")
    for R in sorted(results["scenarios"].keys()):
        sc = results["scenarios"][R]
        print(f"\n  RE target: {R:.0%} of served energy  |  Gas ≤{1-R:.0%} of served")
        print(f"  {'Year':<6}{'Optimal':>10}{'P10':>8}{'P90':>8}"
              f"{'Gas':>9}{'SMR':>7}{'vs Gas':>8}  {'C_sol':>6}{'C_win':>6}{'B(h)':>6}{'Shed':>6}")
        print(f"  {'─'*94}")
        for yr in milestones:
            i = yr - yrs[0]
            t = sc["opt_delivered"][i]; lo = sc["opt_delivered_low"][i]
            hi = sc["opt_delivered_high"][i]
            g  = results["gas_pure"][i]; s = results["lcoe_smr"][i]
            vs = (t / g - 1) * 100
            cs = sc["opt_csol"][i]; cw = sc["opt_cwin"][i]; b = sc["opt_B"][i]
            shed = sc.get("opt_shed", np.zeros_like(sc["opt_csol"]))[i] * 100
            print(f"  {yr:<6}${t:>7.1f}   ${lo:>5.1f}  ${hi:>5.1f}"
                  f"   ${g:>6.1f}  ${s:>4.1f}  {vs:>+7.1f}%"
                  f"  {cs:>5.1f}× {cw:>5.1f}× {b:>4.0f}h {shed:>4.1f}%")
        if "opt_delivered_p90" in sc:
            # Robustness-design series: cost of sizing for a 1-in-10 bad weather year.
            p90 = sc["opt_delivered_p90"]; mean = sc["opt_delivered"]
            cells = "  ".join(
                f"{y}:${p90[y-yrs[0]]:.0f}(+{(p90[y-yrs[0]]/mean[y-yrs[0]]-1)*100:.0f}%)"
                for y in milestones)
            print(f"  P90-designed (size for 1-in-10 weather, premium vs mean):  {cells}")
    print(f"\n{'═'*102}\n")


# ─────────────────────────────────────────────────────────────────────────────
# 10b. MACHINE-READABLE RESULTS EXPORT
# ─────────────────────────────────────────────────────────────────────────────

# Per-year, per-factor fields written to CSV/JSON. Keys are the `results`
# scenario arrays; the CSV column order follows this list.
_EXPORT_FIELDS = [
    ("lcoe",       "opt_delivered"),
    ("lcoe_p10",   "opt_delivered_low"),
    ("lcoe_p90",   "opt_delivered_high"),
    ("c_solar",    "opt_csol"),
    ("c_wind",     "opt_cwin"),
    ("battery_h",  "opt_B"),
    ("shed_frac",  "opt_shed"),
    ("gen_capex",  "gen_capex"),
    ("gen_om",     "gen_om"),
    ("batt_capex", "batt_capex"),
    ("batt_om",    "batt_om"),
    ("gas_capex",  "gas_capex"),
    ("gas_opex",   "gas_opex"),
    ("gas_carbon", "gas_carbon"),
    ("lcoe_p90design", "opt_delivered_p90"),   # present only when design_p90=True
]


def export_results(results: Dict, region: str, prefix: str,
                   outdir: str = "output") -> Tuple[str, str]:
    """
    Write the scenario results to a tidy CSV (one row per RE-target × year) and a
    structured JSON (with run metadata). Purely additive — emits the exact numbers
    already computed so downstream analysis and the documentation tables can be
    regenerated programmatically instead of hand-transcribed.

    Returns (csv_path, json_path).
    """
    os.makedirs(outdir, exist_ok=True)
    yrs = [int(y) for y in results["years"]]
    gas = [float(v) for v in results["gas_pure"]]
    smr = [float(v) for v in results["lcoe_smr"]]
    ppa = ([float(v) for v in results["grid_ppa"]] if "grid_ppa" in results
           else [None] * len(yrs))
    cfe = ([float(v) for v in results["grid_cfe"]] if "grid_cfe" in results
           else [None] * len(yrs))
    Rs  = sorted(results["scenarios"].keys())

    csv_path  = os.path.join(outdir, f"{prefix}_results.csv")
    json_path = os.path.join(outdir, f"{prefix}_results.json")

    # ── CSV (long / tidy format) ────────────────────────────────────────────────
    cols = (["region", "re_target", "year"]
            + [name for name, _ in _EXPORT_FIELDS]
            + ["gas_pure", "smr", "grid_ppa", "grid_cfe"])
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for R in Rs:
            sc = results["scenarios"][R]
            for i, yr in enumerate(yrs):
                row = [region, f"{R:.2f}", yr]
                for _, key in _EXPORT_FIELDS:
                    arr = sc.get(key)
                    row.append(f"{float(arr[i]):.4f}" if arr is not None else "")
                row += [f"{gas[i]:.4f}", f"{smr[i]:.4f}",
                        f"{ppa[i]:.4f}" if ppa[i] is not None else "",
                        f"{cfe[i]:.4f}" if cfe[i] is not None else ""]
                w.writerow(row)

    # ── JSON (structured, with metadata) ────────────────────────────────────────
    payload = {
        "region": region,
        "workload": results.get("workload_name"),
        "wind_solar_corr": float(results.get("wind_solar_corr", 0.0)),
        "simulated_cf": {k: float(v) for k, v in results["sim_cf"].items()},
        "years": yrs,
        "gas_pure": gas,
        "smr": smr,
        "grid_ppa": ppa if "grid_ppa" in results else None,
        "grid_cfe": cfe if "grid_cfe" in results else None,
        "scenarios": {
            f"{R:.2f}": {name: [round(float(v), 4) for v in results["scenarios"][R][key]]
                         for name, key in _EXPORT_FIELDS
                         if key in results["scenarios"][R]}
            for R in Rs
        },
    }
    with open(json_path, "w") as fh:
        json.dump(payload, fh, indent=2)

    print(f"  [Export] wrote {csv_path} and {json_path}")
    return csv_path, json_path


