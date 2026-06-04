"""
Regenerate the documentation result tables directly from the machine-readable
export (`output/<prefix>_results.json`), so the numbers in README / model docs are
never hand-transcribed (closing the last doc-drift risk).

Usage:
    python tools/regen_doc_tables.py                      # default firm suite (us_firm, eu_firm)
    python tools/regen_doc_tables.py us_firm eu_firm      # explicit prefixes

Emits, per region, the §11 "Key Results" markdown table (delivered LCOE at
milestone years, % vs gas in the base year, and the interpolated parity year),
plus a one-line per-RE 2025/2040 summary for the README. It only *prints* the
tables — paste them into the docs after eyeballing.
"""
import json
import os
import sys

MILESTONES = [2025, 2030, 2035, 2040]


def _parity_year(years, series, baseline):
    """First (interpolated) year `series` drops to/below `baseline`; None if never."""
    diff = [s - b for s, b in zip(series, baseline)]
    if diff[0] <= 0:
        return float(years[0])
    for i in range(len(diff) - 1):
        if diff[i] > 0 >= diff[i + 1]:
            frac = diff[i] / (diff[i] - diff[i + 1])
            return years[i] + frac * (years[i + 1] - years[i])
    return None


def table_for(payload: dict) -> str:
    years = payload["years"]
    gas = payload["gas_pure"]
    idx = {y: years.index(y) for y in MILESTONES if y in years}
    Rs = sorted(payload["scenarios"], key=float)

    lines = [f"### {payload['region']} — Firm (always-on)", ""]
    head = "| RE target | " + " | ".join(f"{y}" for y in idx) + " | vs gas 2025 | Crossover |"
    sep = "|" + "----|" * (len(idx) + 3)
    lines += [head, sep]
    base_y = years[0]
    for R in Rs:
        lcoe = payload["scenarios"][R]["lcoe"]
        cells = " | ".join(f"{lcoe[idx[y]]:.1f}" for y in idx)
        vs = (lcoe[idx[base_y]] / gas[idx[base_y]] - 1) * 100 if base_y in idx else float("nan")
        py = _parity_year(years, lcoe, gas)
        cross = f"~{py:.0f}" if py is not None else f">{years[-1]}"
        lines.append(f"| {float(R):.0%} | {cells} | {vs:+.0f}% | {cross} |")
    gas_cells = " | ".join(f"{gas[idx[y]]:.1f}" for y in idx)
    lines.append(f"| **Gas** | {gas_cells} | — | — |")

    if payload.get("grid_ppa"):
        gp = payload["grid_ppa"]
        gp_cells = " | ".join(f"{gp[idx[y]]:.0f}" for y in idx)
        lines.append(f"| *Grid+RE PPA (ref.)* | {gp_cells} | — | — |")
    return "\n".join(lines)


def main(argv):
    prefixes = argv or ["us_firm", "eu_firm"]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for prefix in prefixes:
        path = os.path.join(root, "output", f"{prefix}_results.json")
        if not os.path.exists(path):
            print(f"  [skip] {path} not found — run `python datacenter_lcoe.py` first.")
            continue
        with open(path) as fh:
            payload = json.load(fh)
        print("\n" + table_for(payload) + "\n")

        # Fully-optimised gas-free H₂ system trajectory (fig1 line / §7.6), if exported.
        h2 = payload.get("h2_system")
        if h2 and "lcoe" in h2:
            years = payload["years"]
            idx = {y: years.index(y) for y in MILESTONES if y in years}
            cells = "  ".join(f"{y}:${h2['lcoe'][idx[y]]:.0f}" for y in idx)
            buy = "  ".join(f"{y}:{h2['buy_frac'][idx[y]]*100:.1f}%" for y in idx)
            print(f"  Gas-free H₂ system ({h2.get('ldes_name','')}) delivered LCOE: {cells}")
            print(f"    purchased-H₂ share: {buy}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
