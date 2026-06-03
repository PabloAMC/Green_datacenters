from __future__ import annotations

"""Synthetic 8760-h solar & wind generation with Dunkelflaute structure."""
import math
from typing import Tuple

import numpy as np
from scipy.special import ndtr, betaincinv


# ─────────────────────────────────────────────────────────────────────────────
# 3. WEATHER GENERATION  (Gaussian copula solar-wind correlation)
# ─────────────────────────────────────────────────────────────────────────────

def solar_clearsky(mean_irr_kwh_m2_day: float) -> np.ndarray:
    hours = np.arange(8760)
    doy   = hours // 24
    hod   = hours % 24
    seasonal = 1.0 + 0.35 * np.cos(2 * np.pi * (doy - 172) / 365)
    diurnal  = np.clip(np.sin((hod - 6) * np.pi / 12), 0, 1) ** 1.1
    raw = diurnal * seasonal
    target_cf = mean_irr_kwh_m2_day / 24.0
    mean_raw  = raw.mean()
    return np.clip(raw * (target_cf / mean_raw if mean_raw > 0 else 1), 0, 1)


def generate_weather_year(
    clearsky: np.ndarray,
    mean_wind_ms: float,
    rng: np.random.Generator,
    wind_solar_corr: float = 0.0,
    syn_loading: float = 0.50,
    syn_persistence: float = 0.82,
    cloud_ar1: float = 0.35,
    wind_ar1: float = 0.75,
    wind_daily_share: float = 0.50,
    wind_seasonal_amp: float = 0.12,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Synthetic 8760h solar + wind capacity factors with (v5) a persistent
    synoptic factor that produces correlated multi-day "Dunkelflaute" episodes.

    Daily latent structure — a two-factor Gaussian model at the daily scale:

        f_d         : common synoptic factor, AR(1) with persistence φ
        z1_d = λ·f_d + √(1-λ²)·g1_d   → drives cloud (Beta(3,1.5) marginal)
        z2_d = λ·f_d + √(1-λ²)·g2_d   → drives wind  (Weibull(k=2.1) marginal)

    where (g1_d, g2_d) are contemporaneous bivariate-normal with correlation
        ρ_g = (ρ - λ²) / (1 - λ²)
    chosen so corr(z1_d, z2_d) = ρ (the requested contemporaneous coupling)
    while BOTH variables load positively on the persistent f_d. Hence:

      • f_d ≪ 0 for several days  →  low z1 AND low z2  →  joint multi-day
        low-sun-and-low-wind episode (Dunkelflaute);
      • the residual (g1,g2) carries the cyclonic ρ<0 ("windy when overcast")
        on top of the synoptic mode.

    Because z1_d, z2_d remain *standard normal*, the Beta and Weibull marginals
    — and therefore the annual-mean capacity factors — are preserved exactly;
    only the temporal clustering of lows changes (what storage/backup must cover).
    """
    rho = float(np.clip(wind_solar_corr, -0.999, 0.999))
    lam = float(np.clip(syn_loading, 0.0, math.sqrt(max((rho + 1.0) / 2.0, 0.0)) - 1e-6))
    phi = float(np.clip(syn_persistence, 0.0, 0.999))

    # Persistent synoptic common factor f_d ~ N(0,1), AR(1) (φ) — daily scale.
    f = np.empty(365)
    f[0] = rng.standard_normal()
    sig_f = math.sqrt(1 - phi ** 2)
    for d in range(1, 365):
        f[d] = phi * f[d - 1] + sig_f * rng.standard_normal()

    # Residual contemporaneous pair (g1,g2): corr ρ_g so net corr(z1,z2)=ρ.
    one_m = 1.0 - lam ** 2
    rho_g = float(np.clip((rho - lam ** 2) / one_m if one_m > 1e-9 else 0.0,
                          -0.999, 0.999))
    g1 = rng.standard_normal(365)
    g2 = rho_g * g1 + math.sqrt(max(1 - rho_g ** 2, 0)) * rng.standard_normal(365)

    z1 = lam * f + math.sqrt(one_m) * g1   # → cloud,  Var=1
    z2 = lam * f + math.sqrt(one_m) * g2   # → wind,   Var=1

    # Cloud factor: Beta(3, 1.5) marginal via probability integral transform
    u_cloud = np.clip(ndtr(z1), 1e-6, 1 - 1e-6)
    daily_raw = betaincinv(3.0, 1.5, u_cloud)

    # Wind: Weibull(k=2.1) marginal
    k = 2.1
    c = mean_wind_ms / math.gamma(1 + 1 / k)

    # ── Solar ─────────────────────────────────────────────────────────────────
    # Short-scale cloud autocorrelation on top of the synoptic persistence in z1.
    rho_c = cloud_ar1
    daily_cloud = np.empty(365)
    daily_cloud[0] = daily_raw[0]
    for d in range(1, 365):
        daily_cloud[d] = rho_c * daily_cloud[d - 1] + (1 - rho_c) * daily_raw[d]
    solar = np.clip(clearsky * np.repeat(np.clip(daily_cloud, 0, 1), 24), 0, 1)

    # ── Wind: hourly AR(1) that MEAN-REVERTS to the persistent daily level ──────
    # v4 reverted hourly wind to the *climatological* mean (0 in normal space), so
    # a daily lull decayed within hours (0.75^24 ≈ 0) and multi-day lulls never
    # persisted. v5 reverts to a daily mean m_d carried by the synoptic z2:
    #
    #     z_h = m_day + dev_h ,  dev_h = ρ_w·dev_{h-1} + c_dev·ε_h
    #
    # with Var(m_d)=a_w² and stationary Var(dev)=1-a_w², so z_h ~ N(0,1) exactly
    # (Weibull marginal preserved). a_w² = share of wind variance at the daily/
    # synoptic scale; the rest is intra-day texture (ρ_w=0.75 → ~4h memory).
    rho_w = wind_ar1
    a_w2  = wind_daily_share
    a_w   = math.sqrt(a_w2)
    c_dev = math.sqrt((1 - a_w2) * (1 - rho_w ** 2))   # so stationary Var(dev)=1-a_w²
    m_day = a_w * z2                                    # persistent daily mean (365,)

    z_wind = np.empty(8760)
    eps = rng.standard_normal(8760)
    dev = math.sqrt(1 - a_w2) * rng.standard_normal()  # stationary-variance seed
    z_wind[0] = m_day[0] + dev
    for h in range(1, 8760):
        dev = rho_w * dev + c_dev * eps[h]
        z_wind[h] = m_day[h // 24] + dev

    # Transform normal → uniform → Weibull (preserves exact Weibull marginal)
    u_hourly = np.clip(ndtr(z_wind), 1e-6, 1 - 1e-6)
    speeds = c * (-np.log(1 - u_hourly)) ** (1 / k)

    # Seasonal modulation (NH land: ~12% stronger in winter)
    doy = np.arange(8760) // 24
    speeds *= 1.0 + wind_seasonal_amp * np.cos(2 * np.pi * (doy - 15) / 365)

    v_ci, v_r, v_co = 3.5, 13.0, 25.0
    wind = np.where(speeds < v_ci, 0.0,
           np.where(speeds >= v_co, 0.0,
           np.where(speeds >= v_r, 1.0,
                    ((speeds - v_ci) / (v_r - v_ci)) ** 3)))

    return solar, wind


