from __future__ import annotations

"""Command-line interface."""
import os

import matplotlib.pyplot as plt

from .params import (REGIONS, WORKLOAD_PRESETS, WorkloadProfile,
                     AI_TRAINING, RESOURCE_PRESETS, FIRMING_PRESETS, LDES_PRESETS)
from .simulate import run_region_key, run_full_suite
from .analysis import (run_flex_sensitivity, run_resource_sensitivity, run_tornado,
                       run_ldes_overlay, run_ldes_joint, run_firming_comparison)
from .plots import (plot_flex_heatmap, plot_tornado, plot_ldes_joint,
                    plot_firming_comparison)


def build_arg_parser():
    import argparse
    p = argparse.ArgumentParser(
        description="Off-grid datacenter LCOE model (v5.5). No args → firm US+EU suite.")
    p.add_argument("--region", choices=list(REGIONS), help="us | eu")
    p.add_argument("--workload", choices=list(WORKLOAD_PRESETS),
                   help="flexibility preset for a single-scenario run "
                        "(default: training). With no scenario args at all, the "
                        "model instead runs the firm US+EU suite.")
    p.add_argument("--interruptible", type=float,
                   help="override: interruptible (sheddable) fraction of load [0–1]")
    p.add_argument("--shed-penalty", type=float,
                   help="override: value of lost compute, $/MWh shed (deep = firm)")
    p.add_argument("--re", type=float, nargs="+",
                   help="RE targets, e.g. --re 0.8 0.9 0.95")
    p.add_argument("--years", type=int, default=15, help="projection horizon (default 15)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--flex-sweep", action="store_true",
                   help="run the flexibility sensitivity (interruptible × compute-value heatmap)")
    p.add_argument("--design-p90", action="store_true",
                   help="also report a robustness-design series sized against the "
                        "1-in-10 (P90) weather year (single-scenario runs)")
    p.add_argument("--resource", choices=["default", "good"],
                   help="resource quality for a single-scenario run: conservative "
                        "default site, or a modern well-sited 'good' resource")
    p.add_argument("--firming", choices=list(FIRMING_PRESETS), default="gas",
                   help="firming resource: 'gas' (default) or 'h2' (zero-carbon "
                        "green-hydrogen turbine, pricey fuel)")
    p.add_argument("--resource-sweep", action="store_true",
                   help="compare default vs good-site resource (LCOE + parity table)")
    p.add_argument("--tornado", action="store_true",
                   help="parity-gap tornado: sensitivity of RE-vs-gas competitiveness "
                        "to key assumptions → figure")
    p.add_argument("--ldes", choices=list(LDES_PRESETS),
                   help="long-duration storage overlay: can iron-air or self-produced "
                        "H2 (charged from RE overcapacity) displace the residual gas?")
    p.add_argument("--ldes-joint", choices=list(LDES_PRESETS),
                   help="JOINT co-optimise a gas-free zero-carbon datacenter "
                        "(solar+wind+LFP+self/bought-H2), swept over market-H2 price → figure")
    p.add_argument("--firming-compare", action="store_true",
                   help="compare gas-backed vs green-H2-firmed delivered cost for a "
                        "region/RE target → figure")
    p.add_argument("--grid-steps", type=int, help="advanced: optimiser grid resolution")
    p.add_argument("--mc", type=int, help="advanced: Monte-Carlo weather years")
    return p


def _validate_args(parser, args) -> None:
    """Reject out-of-range CLI inputs with a clean error (not a deep stack trace)."""
    if args.interruptible is not None and not (0.0 <= args.interruptible <= 1.0):
        parser.error("--interruptible must be a fraction in [0, 1]")
    if args.shed_penalty is not None and args.shed_penalty < 0.0:
        parser.error("--shed-penalty must be ≥ 0 ($/MWh of lost compute)")
    if args.re is not None and any(not (0.0 < r < 1.0) for r in args.re):
        parser.error("--re targets must each be strictly between 0 and 1")
    if args.years is not None and args.years < 1:
        parser.error("--years must be ≥ 1")
    if args.grid_steps is not None and args.grid_steps < 2:
        parser.error("--grid-steps must be ≥ 2 (need ≥2 nodes per axis to interpolate)")
    if args.mc is not None and args.mc < 1:
        parser.error("--mc must be ≥ 1")


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    os.makedirs("figs", exist_ok=True)

    # Flexibility sensitivity sweep
    if args.flex_sweep:
        region = args.region or "eu"
        re_t = (args.re or [0.90])[0]
        sweep = run_flex_sensitivity(
            region_key=region, re_target=re_t, target_year=2030,
            grid_steps=args.grid_steps or 15, n_mc=args.mc or 15, seed=args.seed)
        name = f"{region}_flex_heatmap"
        fig = plot_flex_heatmap(sweep)
        fig.savefig(f"figs/{name}.png", dpi=200, bbox_inches="tight"); plt.close(fig)
        print(f"\nDone — flexibility figure saved: figs/{name}.png")
        return

    # Resource-quality sensitivity (default vs good site)
    if args.resource_sweep:
        region = args.region or "us"
        re_t = (args.re or [0.90])[0]
        run_resource_sensitivity(region_key=region, re_target=re_t,
                                 years=args.years, seed=args.seed,
                                 grid_steps=args.grid_steps, n_mc=args.mc)
        return

    # Gas-backed vs green-H2-firmed delivered-cost comparison
    if args.firming_compare:
        region = args.region or "eu"
        re_t = (args.re or [0.90])[0]
        r = run_firming_comparison(region_key=region, re_target=re_t,
                                   grid_steps=args.grid_steps, n_mc=args.mc, seed=args.seed)
        name = f"{region}_firming_compare"
        fig = plot_firming_comparison(r)
        fig.savefig(f"figs/{name}.png", dpi=200, bbox_inches="tight"); plt.close(fig)
        print(f"\nDone — firming comparison figure saved: figs/{name}.png")
        return

    # Joint gas-free zero-carbon co-optimisation + market-H2 price spike sweep
    if args.ldes_joint:
        region = args.region or "eu"
        r = run_ldes_joint(region_key=region, target_year=2035, ldes_tech=args.ldes_joint,
                           n_mc=args.mc or 8, seed=args.seed)
        name = f"{region}_ldes_joint"
        fig = plot_ldes_joint(r)
        fig.savefig(f"figs/{name}.png", dpi=200, bbox_inches="tight"); plt.close(fig)
        print(f"\nDone — joint co-opt figure saved: figs/{name}.png")
        return

    # LDES overlay (iron-air / self-produced H2)
    if args.ldes:
        region = args.region or "eu"
        re_t = (args.re or [0.90])[0]
        run_ldes_overlay(region_key=region, re_target=re_t, target_year=2035,
                         ldes_tech=args.ldes, grid_steps=args.grid_steps or 13,
                         n_mc=args.mc or 12, seed=args.seed)
        return

    # Parity-gap tornado sensitivity
    if args.tornado:
        region = args.region or "eu"
        re_t = (args.re or [0.90])[0]
        t = run_tornado(region_key=region, re_target=re_t, target_year=2030,
                        grid_steps=args.grid_steps or 11, n_mc=args.mc or 12,
                        seed=args.seed)
        name = f"{region}_tornado"
        fig = plot_tornado(t)
        fig.savefig(f"figs/{name}.png", dpi=200, bbox_inches="tight"); plt.close(fig)
        print(f"\nDone — tornado figure saved: figs/{name}.png")
        return

    # Single custom scenario
    if args.region or args.workload or args.interruptible is not None \
            or args.shed_penalty is not None or args.re or args.design_p90 \
            or args.resource or args.firming != "gas":
        region = args.region or "us"
        wl = WORKLOAD_PRESETS[args.workload] if args.workload else AI_TRAINING
        if args.interruptible is not None or args.shed_penalty is not None:
            ifrac = args.interruptible if args.interruptible is not None else wl.interruptible_fraction
            pen = args.shed_penalty if args.shed_penalty is not None else wl.shed_penalty_mwh
            wl = WorkloadProfile(f"custom {ifrac:.0%} @ ${pen:.0f}",
                                 interruptible_fraction=ifrac, shed_penalty_mwh=pen)
        reliabilities = args.re or [0.70, 0.80, 0.90, 0.95]
        sys_ov = {}
        if args.grid_steps:
            sys_ov["grid_steps"] = args.grid_steps
        if args.mc:
            sys_ov["n_mc_weather"] = args.mc
        mi = mw = None
        if args.resource:
            mi, mw = RESOURCE_PRESETS[region][args.resource]
        gas_override = FIRMING_PRESETS[args.firming]   # None → region default gas
        prefix = f"cli_{region}_{args.workload or 'training'}"
        run_region_key(region, wl, reliabilities, prefix=prefix,
                       sys_overrides=sys_ov or None, seed=args.seed,
                       design_p90=args.design_p90, mean_irr=mi, mean_wind_ms=mw,
                       gas=gas_override)
        print(f"\nDone — figures saved with prefix figs/{prefix}_*.png")
        return

    # Default: full suite
    run_full_suite()


if __name__ == "__main__":
    main()
