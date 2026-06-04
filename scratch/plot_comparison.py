import os
import math
import numpy as np
import matplotlib.pyplot as plt
import datacenter_lcoe

# Try to use scienceplots for formatting, fallback if not available
try:
    import scienceplots
    plt.style.use(["science", "no-latex"])
except ImportError:
    plt.rcParams.update({
        "font.family": "serif",
        "axes.grid": True,
        "grid.alpha": 0.3,
        "figure.dpi": 150,
        "font.size": 10
    })

def main():
    print("Running simulations for Europe and US at 70% RE...")
    
    # 1. Run Europe (Firm) at 70% RE
    results_eu = datacenter_lcoe.run_simulation(
        solar=datacenter_lcoe.SOLAR_EU,
        wind=datacenter_lcoe.WIND_EU,
        battery=datacenter_lcoe.BATTERY_EU,
        gas=datacenter_lcoe.GAS_EU,
        smr=datacenter_lcoe.SMR_EU,
        sys=datacenter_lcoe.SYSTEM_EU,
        workload=datacenter_lcoe.FIRM,
        mean_irr=3.8,
        mean_wind_ms=7.0,
        reliabilities=[0.70],
        seed=42
    )
    
    # 2. Run US (Firm) at 70% RE
    results_us = datacenter_lcoe.run_simulation(
        solar=datacenter_lcoe.SOLAR,
        wind=datacenter_lcoe.WIND,
        battery=datacenter_lcoe.BATTERY_US,
        gas=datacenter_lcoe.GAS,
        smr=datacenter_lcoe.SMR,
        sys=datacenter_lcoe.SYSTEM,
        workload=datacenter_lcoe.FIRM,
        mean_irr=5.5,
        mean_wind_ms=7.5,
        reliabilities=[0.70],
        seed=42
    )

    yrs = results_eu["years"]
    sc_eu = results_eu["scenarios"][0.70]
    sc_us = results_us["scenarios"][0.70]
    
    fig, ax = plt.subplots(figsize=(8, 6))
    
    # Plot Europe
    line_eu, = ax.plot(yrs, sc_eu["opt_delivered"], color="#3A86FF", lw=2.5, label="Europe: 70% RE (Optimal)")
    ax.fill_between(yrs, sc_eu["opt_delivered_low"], sc_eu["opt_delivered_high"],
                    color="#3A86FF", alpha=0.12, edgecolor="none")
    
    # Plot US
    line_us, = ax.plot(yrs, sc_us["opt_delivered"], color="#FF9F1C", lw=2.5, label="US: 70% RE (Optimal)")
    ax.fill_between(yrs, sc_us["opt_delivered_low"], sc_us["opt_delivered_high"],
                    color="#FF9F1C", alpha=0.12, edgecolor="none")
    
    # Plot Gas References
    ax.plot(yrs, results_eu["gas_pure"], color="black", lw=2, ls="--", label="Europe: Gas CCGT + Carbon Tax")
    ax.plot(yrs, results_us["gas_pure"], color="#E71D36", lw=2, ls="-.", label="US: Gas CCGT Baseline")
    
    # Find Crossing for Europe
    diff = sc_eu["opt_delivered"] - results_eu["gas_pure"]
    idxs = np.where(np.diff(np.sign(diff)))[0]
    
    for idx in idxs:
        frac = diff[idx] / (diff[idx] - diff[idx+1])
        cx = yrs[idx] + frac
        cy = results_eu["gas_pure"][idx] + frac * (results_eu["gas_pure"][idx+1] - results_eu["gas_pure"][idx])
        
        # Calculate Quarter
        q = int(frac * 4) + 1
        label_text = f"Europe Parity: Q{q} {int(cx)}"
        
        ax.plot(cx, cy, "o", color="#3A86FF", ms=8, zorder=5)
        # Position annotation slightly higher and with a white background bbox to prevent occlusion
        ax.annotate(label_text, xy=(cx, cy), xytext=(20, 15),
                    textcoords="offset points", fontsize=8.5, color="#0056b3", fontweight="bold",
                    arrowprops=dict(arrowstyle="->", color="#3A86FF", lw=1.2),
                    bbox=dict(facecolor='white', edgecolor='none', alpha=0.85, boxstyle='round,pad=0.2'))
        print(f"Annotated crossover: {label_text} at year={cx:.4f}, cost=${cy:.2f}/MWh")

    ax.set_xlabel("Year", fontsize=11, fontweight="bold")
    ax.set_ylabel(r"Delivered LCOE (\$/MWh)", fontsize=11, fontweight="bold")
    ax.set_title("70% RE Green Datacenter vs. Gas CCGT: Europe vs. US", fontsize=12, fontweight="bold", pad=15)
    
    ax.set_xlim(yrs[0], yrs[-1])
    ax.set_ylim(0, 180)
    from matplotlib.ticker import MaxNLocator
    ax.xaxis.set_major_locator(MaxNLocator(integer=True, steps=[1, 2, 5, 10]))
    
    ax.legend(fontsize=9, frameon=True, facecolor="white", framealpha=1, loc="upper right")
    
    # References text at the bottom left
    refs_text = "Sources: Lazard v18 · Way et al. Joule (2022) · NREL ATB 2024 · Ember 2025 · EU ETS\nNote: Firm (always-on) workload — gas backup sized to 100% of load, no shedding. EU includes logistic carbon pricing."
    ax.text(0.01, 0.02, refs_text, transform=ax.transAxes, fontsize=7.5, va="bottom",
            alpha=0.6, family="sans-serif")
            
    os.makedirs("figs", exist_ok=True)
    fig.savefig("figs/eu_vs_us_comparison.png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    print("Successfully generated figs/eu_vs_us_comparison.png")

if __name__ == "__main__":
    main()
