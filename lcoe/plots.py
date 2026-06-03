from __future__ import annotations

"""All matplotlib figures (trajectories, breakdowns, heatmaps, tornado)."""
from typing import Dict

import matplotlib.pyplot as plt
import numpy as np

try:
    import scienceplots  # noqa: F401
    plt.style.use(["science", "no-latex"])
except ImportError:
    plt.rcParams.update({"font.family": "serif", "axes.grid": True,
                         "grid.alpha": 0.3, "figure.dpi": 150})


# ─────────────────────────────────────────────────────────────────────────────
# 9. PLOTTING
# ─────────────────────────────────────────────────────────────────────────────

C_OPT = "#3A86FF"; C_GAS = "#6B705C"; C_BATT = "#9D4EDD"; C_SMR = "#E71D36"
C_SOL = "#FF9F1C"; C_WIN = "#2EC4B6"; C_PPA = "#06D6A0"; C_CFE = "#118AB2"
C_H2 = "#073B4C"; C_ELEC = "#8ECAE6"; C_STORE = "#0096C7"; C_TURB = "#48CAE4"
REFS  = "Lazard v18 · Way et al. Joule 2022 · NREL ATB 2024 · EU ETS · IPCC AR6"
PALETTE = ["#3A86FF", "#FF9F1C", "#2EC4B6", "#9D4EDD", "#FB5607", "#E71D36"]


def _crossings(ax, years, series, baseline, color, label):
    diff = series - baseline
    idxs = np.where(np.diff(np.sign(diff)))[0]
    for idx in idxs:
        frac = diff[idx] / (diff[idx] - diff[idx+1])
        cx = years[idx] + frac
        cy = baseline[idx] + frac * (baseline[idx+1] - baseline[idx])
        ax.plot(cx, cy, "o", color=color, ms=7, zorder=5)
        ax.annotate(f"{label} {cx:.0f}", xy=(cx, cy), xytext=(10, 8),
                    textcoords="offset points", fontsize=7, color=color,
                    arrowprops=dict(arrowstyle="->", color=color, lw=0.8))


def plot_cost_trajectories(results, region="US"):
    yrs = results["years"]
    Rs  = sorted(results["scenarios"].keys())
    fig, ax = plt.subplots(figsize=(7, 5))
    for R, col in zip(Rs, PALETTE):
        sc = results["scenarios"][R]
        ax.plot(yrs, sc["opt_delivered"], color=col, lw=2, label=f"Optimal ({R:.0%} RE)")
        ax.fill_between(yrs, sc["opt_delivered_low"], sc["opt_delivered_high"],
                        color=col, alpha=0.12, edgecolor="none")
        _crossings(ax, yrs, sc["opt_delivered"], results["gas_pure"], col, f"{R:.0%}")
    ax.plot(yrs, results["gas_pure"], color=C_GAS, lw=2, ls="--", label=results["gas_name"])
    ax.plot(yrs, results["lcoe_smr"], color=C_SMR, lw=2, ls="-.", label=results["smr_name"])
    if "grid_ppa" in results:
        ax.plot(yrs, results["grid_ppa"], color=C_PPA, lw=2, ls=":",
                label=results["grid_ppa_name"])
    if "grid_cfe" in results:
        ax.plot(yrs, results["grid_cfe"], color=C_CFE, lw=2, ls=(0, (1, 1)),
                label=results["grid_cfe_name"])
    if "h2_system" in results:
        ax.plot(yrs, results["h2_system"]["lcoe"], color=C_H2, lw=2.5,
                label="Optimised gas-free H₂ system")
    ax.set(xlabel="Year", ylabel="Delivered cost ($/MWh)",
           title=f"Delivered cost trajectory — {region}",
           xlim=(yrs[0], yrs[-1]), ylim=(0, None))
    ax.legend(fontsize=9, frameon=True, facecolor="white", framealpha=1)
    ax.text(0.01, 0.01, REFS, transform=ax.transAxes, fontsize=6, va="bottom",
            alpha=0.5, family="monospace")
    fig.tight_layout(); return fig


def plot_reliability_sensitivity(results, target_year=2030, region="US"):
    idx   = target_year - results["years"][0]
    Rs    = sorted(results["scenarios"].keys())
    r_pct = [r * 100 for r in Rs]
    vals  = [results["scenarios"][R]["opt_delivered"][idx] for R in Rs]
    los   = [results["scenarios"][R]["opt_delivered_low"][idx] for R in Rs]
    his   = [results["scenarios"][R]["opt_delivered_high"][idx] for R in Rs]
    fig, ax = plt.subplots(figsize=(6, 4.5))
    ax.plot(r_pct, vals, "o-", color=C_OPT, lw=2, label="Optimal blend")
    ax.fill_between(r_pct, los, his, color=C_OPT, alpha=0.15, edgecolor="none")
    g = results["gas_pure"][idx]; s = results["lcoe_smr"][idx]
    ax.axhline(g, color=C_GAS, ls="--", lw=2, label=f"Gas CCGT (${g:.0f}/MWh)")
    ax.axhline(s, color=C_SMR, ls="-.", lw=2, label=f"SMR (${s:.0f}/MWh)")
    if "grid_ppa" in results:
        gp = results["grid_ppa"][idx]
        ax.axhline(gp, color=C_PPA, ls=":", lw=2, label=f"Grid+RE PPA (${gp:.0f}/MWh)")
    if "grid_cfe" in results:
        gc = results["grid_cfe"][idx]
        ax.axhline(gc, color=C_CFE, ls=(0, (1, 1)), lw=2, label=f"Grid 24/7 CFE (${gc:.0f}/MWh)")
    ax.set(xlabel="Renewable fraction (%)", ylabel="Delivered cost ($/MWh)",
           title=f"Cost vs. RE fraction at {target_year} — {region}",
           ylim=(0, None))
    ax.legend(fontsize=9, frameon=True, facecolor="white", loc="lower right")
    ax.text(0.01, 0.01, REFS, transform=ax.transAxes, fontsize=6, va="bottom",
            alpha=0.5, family="monospace")
    fig.tight_layout(); return fig


def plot_optimal_mix(results, region="US"):
    Rs = sorted(results["scenarios"].keys()); yrs = results["years"]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4))
    for R, col in zip(Rs, PALETTE):
        sc = results["scenarios"][R]; lbl = f"{R:.0%} RE"
        axes[0].plot(yrs, sc["opt_csol"], color=col, lw=2, label=lbl)
        axes[1].plot(yrs, sc["opt_cwin"], color=col, lw=2)
        axes[2].plot(yrs, sc["opt_B"],    color=col, lw=2)
    axes[0].set(xlabel="Year", ylabel="Solar overbuild (×load)", ylim=(0,None), title="Solar")
    axes[1].set(xlabel="Year", ylabel="Wind overbuild (×load)", ylim=(0,None), title="Wind")
    axes[2].set(xlabel="Year", ylabel="Storage duration (h)", ylim=(0,None), title="Battery")
    axes[0].legend(fontsize=9, title="RE target")
    fig.suptitle(f"Optimal capacity mix — {region}", fontsize=12)
    fig.tight_layout(); return fig


def plot_component_breakdown(results, reliability=0.90, region="US"):
    """Delivered-cost breakdown by factor (generation / battery / firming), each split
    into capex vs opex. Solid fill = capex, hatched = opex, within a colour family. The
    firming bands are labelled generically so the figure is correct whether the firming
    resource is natural gas or green H₂ (`--firming h2`, whose carbon band is ~0)."""
    sc = results["scenarios"][reliability]; yrs = results["years"]
    z = np.zeros_like(sc["gen_capex"])
    firm = results.get("gas_name", "Firming")        # "Gas Backup (US)" | "Green H2 firming…"
    # (values, label, colour, hatch)  — capex solid, opex hatched, per factor
    bands = [
        (sc["gen_capex"],          "Generation — capex", C_SOL,  None),
        (sc["gen_om"],             "Generation — O&M",   C_SOL,  "////"),
        (sc["batt_capex"],         "Battery — capex",    C_BATT, None),
        (sc["batt_om"],            "Battery — O&M",      C_BATT, "////"),
        (sc["gas_capex"],          "Firming — capex",        C_GAS,  None),
        (sc["gas_opex"],           "Firming — fuel + O&M",   C_GAS,  "////"),
        (sc["gas_carbon"],         "Firming — carbon",       "#2B2D42", "xx"),
        (sc.get("opt_cp", z),      "Lost compute (shed)", C_SMR, ".."),
    ]
    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    bottom = np.zeros_like(yrs, dtype=float)
    for vals, lbl, col, hatch in bands:
        if np.allclose(vals, 0):   # skip empty bands (e.g. carbon for green H₂, shed for firm)
            continue
        ax.fill_between(yrs, bottom, bottom + vals, label=lbl, facecolor=col,
                        alpha=0.85, hatch=hatch, edgecolor="white", linewidth=0.3)
        bottom = bottom + vals
    ax.plot(yrs, results["gas_pure"], color="#E07A5F", lw=2, ls="--",
            label=f"{firm} (pure)")
    ax.set(xlabel="Year", ylabel="Delivered cost ($/MWh)",
           title=f"Cost breakdown (capex/opex by factor) at {reliability:.0%} RE — {region}",
           xlim=(yrs[0], yrs[-1]), ylim=(0, None))
    ax.legend(fontsize=8, frameon=True, facecolor="white",
              loc="upper left", bbox_to_anchor=(1.01, 1.0),
              title="solid = capex · hatched = opex")
    ax.text(0.01, 0.01, REFS, transform=ax.transAxes, fontsize=6, va="bottom",
            alpha=0.5, family="monospace")
    fig.tight_layout(); return fig


def plot_h2_breakdown(results, region="US"):
    """fig6 — cost breakdown of the FULLY-OPTIMISED gas-free green-H₂ system (no RE
    target: minimise LCOE over solar+wind+LFP + self-produced H₂, residual bought as
    green H₂). Analogue of fig4 but with H₂ components instead of gas; all zero-carbon."""
    h = results["h2_system"]; yrs = results["years"]
    bands = [
        (h["gen_capex"],     "Generation — capex",   C_SOL,  None),
        (h["gen_om"],        "Generation — O&M",     C_SOL,  "////"),
        (h["lfp_capex"],     "LFP battery — capex",  C_BATT, None),
        (h["lfp_om"],        "LFP battery — O&M",    C_BATT, "////"),
        (h["elec_capex"],    "Electrolyser",         C_ELEC, None),
        (h["store_capex"],   "H₂ storage (tanks)",   C_STORE, None),
        (h["turbine_capex"], "H₂ turbine",           C_TURB, None),
        (h["buy_h2"],        "Purchased green H₂",   C_H2,   ".."),
    ]
    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    bottom = np.zeros_like(yrs, dtype=float)
    for vals, lbl, col, hatch in bands:
        if np.allclose(vals, 0):
            continue
        ax.fill_between(yrs, bottom, bottom + vals, label=lbl, facecolor=col,
                        alpha=0.85, hatch=hatch, edgecolor="white", linewidth=0.3)
        bottom = bottom + vals
    ax.plot(yrs, h["lcoe"], color="black", lw=1.5, ls="--", label="Total (optimised)")
    if "gas_pure" in results:
        ax.plot(yrs, results["gas_pure"], color=C_GAS, lw=2, ls="--",
                label=f"{results.get('gas_name', 'Gas CCGT')} (pure, ref)")
    ax.set(xlabel="Year", ylabel="Delivered cost ($/MWh)",
           title=f"Optimised gas-free green-H₂ system — {region}",
           xlim=(yrs[0], yrs[-1]), ylim=(0, None))
    ax.legend(fontsize=8, frameon=True, facecolor="white", loc="upper left",
              bbox_to_anchor=(1.01, 1.0), title="hatched = opex/fuel")
    ax.text(0.01, 0.01, REFS + " · Lazard LCOH v4.0", transform=ax.transAxes,
            fontsize=6, va="bottom", alpha=0.5, family="monospace")
    fig.tight_layout(); return fig


def plot_flex_heatmap(sweep: Dict) -> "plt.Figure":
    """
    2D flexibility surface: delivered LCOE (left) and parity year vs gas (right)
    over interruptible-fraction × shed-penalty (value of lost compute).
    """
    it = sweep["interruptibles"]; pen = sweep["shed_penalties"]
    L = sweep["lcoe"]; P = sweep["parity"]; yr = sweep["target_year"]; region = sweep["region"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))

    def _heat(ax, M, title, cmap_name, fmt):
        cmap = plt.get_cmap(cmap_name)
        finite = M[np.isfinite(M)]
        vmin, vmax = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
        rng = vmax - vmin or 1.0
        im = ax.imshow(M, origin="lower", aspect="auto", cmap=cmap, vmin=vmin, vmax=vmax,
                       extent=[-0.5, len(pen) - 0.5, -0.5, len(it) - 0.5])
        ax.set_xticks(range(len(pen))); ax.set_xticklabels([f"{p:.0f}" for p in pen])
        ax.set_yticks(range(len(it))); ax.set_yticklabels([f"{f:.0%}" for f in it])
        ax.set(xlabel="Value of lost compute (\\$/MWh)",
               ylabel="Interruptible fraction", title=title)
        for i in range(len(it)):
            for j in range(len(pen)):
                v = M[i, j]
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    ax.text(j, i, "—", ha="center", va="center", fontsize=8, color="0.3")
                    continue
                # adaptive text colour: dark text on light cells, white on dark
                r, g, b, _ = cmap((v - vmin) / rng)
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                ax.text(j, i, fmt(v), ha="center", va="center", fontsize=8,
                        color="black" if lum > 0.55 else "white")
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)

    _heat(axes[0], L, f"Delivered LCOE {yr} (\\$/MWh) — {region}", "viridis_r",
          lambda v: f"{v:.0f}")
    _heat(axes[1], P, f"Parity year vs gas — {region}", "RdYlGn_r",
          lambda v: f"{v:.0f}")
    fig.suptitle(f"Flexibility trade-off surface — {region} — {sweep['re_target']:.0%} RE",
                 fontsize=12)
    fig.tight_layout(); return fig


def plot_tornado(t: Dict) -> "plt.Figure":
    """Horizontal tornado of the parity-gap sensitivity (negative = RE beats gas)."""
    rows = t["rows"]; base = t["base"]
    labels = [r[0] for r in rows]
    fig, ax = plt.subplots(figsize=(8, 0.5 * len(rows) + 1.6))
    for i, (name, lo, hi) in enumerate(rows):
        left, right = min(lo, hi), max(lo, hi)
        ax.barh(i, right - base, left=base, color=C_GAS, alpha=0.85)
        ax.barh(i, base - left, left=left, color=C_OPT, alpha=0.85)
        ax.text(left, i, f"{lo:+.0f}", va="center", ha="right", fontsize=7)
        ax.text(right, i, f"{hi:+.0f}", va="center", ha="left", fontsize=7)
    ax.axvline(base, color="black", lw=1.2, ls="--",
               label=f"base {base:+.0f} $/MWh")
    ax.axvline(0, color="#888", lw=1, ls=":")
    ax.set_yticks(range(len(rows))); ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("Parity gap: firm RE LCOE − gas LCOE ($/MWh) — negative = RE wins")
    ax.set_title(f"Tornado — {t['region']} — {t['re_target']:.0%} RE @ {t['target_year']}")
    ax.legend(fontsize=8, loc="lower right")
    ax.text(0.01, 0.01, REFS, transform=ax.transAxes, fontsize=6, va="bottom",
            alpha=0.5, family="monospace")
    fig.tight_layout(); return fig




def plot_ldes_joint(result: Dict) -> "plt.Figure":
    """Joint gas-free zero-carbon optimum vs market-H2 price spike: delivered LCOE
    and how the optimal design self-produces more as bought H2 gets dearer."""
    by = result["by_mult"]
    mults = sorted(by)
    price = [by[m]["h2_price"] for m in mults]
    lcoe = [by[m]["total"] for m in mults]
    buy = [by[m]["buy_frac"] * 100 for m in mults]
    elec = [by[m]["elec"] for m in mults]
    fig, (a0, a1) = plt.subplots(1, 2, figsize=(11, 4.4))
    a0.plot(price, lcoe, "o-", color=C_OPT, lw=2)
    for x, y in zip(price, lcoe):
        a0.annotate(f"${y:.0f}", (x, y), textcoords="offset points", xytext=(0, 7),
                    fontsize=8, ha="center")
    a0.set(xlabel="Market green-H₂ price ($/MWh-e)",
           ylabel="Cheapest 24/7 gas-free LCOE ($/MWh)",
           title=f"Gas-free zero-carbon optimum — {result['region']} — {result['target_year']}",
           ylim=(0, None))
    a1.plot(price, buy, "s-", color=C_GAS, lw=2, label="bought H₂ (% of load)")
    a1b = a1.twinx()
    a1b.plot(price, elec, "^--", color=C_PPA, lw=2, label="electrolyser (MW/MW-load)")
    a1.set(xlabel="Market green-H₂ price ($/MWh-e)", ylabel="Bought H₂ (% of load)",
           title="Self-production hedges the H₂ spike", ylim=(0, None))
    a1b.set_ylabel("Electrolyser size (MW per MW-load)")
    l0, lab0 = a1.get_legend_handles_labels(); l1, lab1 = a1b.get_legend_handles_labels()
    a1.legend(l0 + l1, lab0 + lab1, fontsize=8, loc="upper left")
    a0.text(0.01, 0.01, REFS, transform=a0.transAxes, fontsize=6, va="bottom",
            alpha=0.5, family="monospace")
    fig.tight_layout(); return fig


def plot_firming_comparison(result: Dict) -> "plt.Figure":
    """Gas-backed vs green-H₂-firmed delivered cost for the same firm RE datacenter:
    the cost (and convergence as carbon rises) of zero-carbon firming."""
    s = result["series"]; reg = result["region"]; R = result["re_target"]
    yrs = s["Gas-backed"]["years"]
    fig, ax = plt.subplots(figsize=(7.5, 5))
    ax.plot(yrs, s["Gas-backed"]["lcoe"], "o-", color=C_GAS, lw=2,
            label="RE + gas firming (delivered)")
    ax.plot(yrs, s["Green-H₂-firmed"]["lcoe"], "s-", color=C_PPA, lw=2,
            label="RE + green-H₂ firming (delivered, zero-C)")
    ax.plot(yrs, s["Gas-backed"]["firm_ref"], ls="--", color=C_GAS, lw=1.5, alpha=0.7,
            label="pure gas (ref)")
    ax.plot(yrs, s["Green-H₂-firmed"]["firm_ref"], ls=":", color=C_PPA, lw=1.5, alpha=0.8,
            label="pure green-H₂ (ref)")
    ax.fill_between(yrs, s["Gas-backed"]["lcoe"], s["Green-H₂-firmed"]["lcoe"],
                    color=C_PPA, alpha=0.10, edgecolor="none")
    ax.set(xlabel="Year", ylabel="Delivered cost ($/MWh)",
           title=f"Firming choice: gas vs green H₂ — {reg} — {R:.0%} RE",
           xlim=(yrs[0], yrs[-1]), ylim=(0, None))
    ax.legend(fontsize=8, frameon=True, facecolor="white", loc="upper right")
    ax.text(0.01, 0.01, REFS, transform=ax.transAxes, fontsize=6, va="bottom",
            alpha=0.5, family="monospace")
    fig.tight_layout(); return fig
