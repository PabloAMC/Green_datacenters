#!/usr/bin/env python3
"""
Doc-drift guard: fail if the §11 "Key Results" tables in `model_documentation.md`
no longer match the committed `output/*_firm_results.json`.

The model already regenerates those tables from the export (`tools/regen_doc_tables.py`),
but pasting is manual — so a hand-edit, or a stale paste after a model change, can drift.
This script re-derives the milestone LCOEs from the JSON and compares them, cell by cell,
to the numbers actually printed in the doc. Run by `make check-docs` and in CI.

Exit 0 = in sync; exit 1 = drift (prints the offending cells).
"""
import json
import os
import re
import sys

MILESTONES = [2025, 2030, 2035, 2040]
TOL = 0.15  # $/MWh — doc prints 1 decimal; allow rounding slack
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (export prefix, the "### <header> — Firm" subheader it appears under in the doc)
REGIONS = [("us_firm", "US — Firm"), ("eu_firm", "Europe — Firm")]


def expected_from_json(prefix):
    """{'70%': [4 LCOEs], ..., 'Gas': [4]} from the committed export."""
    with open(os.path.join(ROOT, "output", f"{prefix}_results.json")) as fh:
        d = json.load(fh)
    years = d["years"]
    idx = [years.index(y) for y in MILESTONES]
    out = {}
    for R, sc in d["scenarios"].items():
        out[f"{round(float(R) * 100)}%"] = [round(sc["lcoe"][i], 1) for i in idx]
    out["Gas"] = [round(d["gas_pure"][i], 1) for i in idx]
    return out


def parse_doc_table(doc, header):
    """Parse the markdown table under '### <header>' → {row_label: [4 milestone floats]}."""
    start = doc.index(f"### {header}")
    chunk = doc[start:]
    rows = {}
    for line in chunk.splitlines()[1:]:
        s = line.strip()
        if s.startswith("###") or s.startswith("## "):
            break                                   # next section
        if not s.startswith("|"):
            continue
        cells = [c.strip().replace("*", "") for c in s.split("|")[1:-1]]
        if not cells or cells[0] in ("RE target", "") or set(cells[0]) <= set("-:"):
            continue                                # header / separator row
        label = cells[0]
        nums = []
        for c in cells[1:5]:                        # the 4 milestone columns
            try:
                nums.append(float(c))
            except ValueError:
                nums = []
                break
        if len(nums) == 4:
            rows[label] = nums
    return rows


def main():
    with open(os.path.join(ROOT, "model_documentation.md")) as fh:
        doc = fh.read()
    sec = doc[doc.index("## 11. Key Results"):doc.index("## 12.")]

    problems = []
    for prefix, header in REGIONS:
        exp = expected_from_json(prefix)
        got = parse_doc_table(sec, header)
        for label, vals in exp.items():
            if label not in got:
                problems.append(f"[{header}] row '{label}' missing from doc table")
                continue
            for col, (e, g) in enumerate(zip(vals, got[label])):
                if abs(e - g) > TOL:
                    problems.append(
                        f"[{header}] {label} {MILESTONES[col]}: doc {g} vs export {e}")

    if problems:
        print("DOC DRIFT — §11 tables disagree with output/*.json:")
        for p in problems:
            print("  " + p)
        print("\nFix: regenerate with `make tables` (tools/regen_doc_tables.py) and "
              "paste, or re-run `make reproduce` if the model changed.")
        return 1
    print(f"OK — §11 tables match output/ ({sum(len(expected_from_json(p)) for p, _ in REGIONS)} "
          f"rows across {len(REGIONS)} regions).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
