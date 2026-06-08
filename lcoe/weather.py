from __future__ import annotations

"""Synthetic 8760-h solar & wind generation with Dunkelflaute structure."""
import math
from typing import List, Optional, Tuple

import numpy as np
from scipy.special import ndtr, betaincinv


def load_weather_traces(path: str) -> "List[Tuple[np.ndarray, np.ndarray]]":
    """
    Load real reanalysis weather years for the dispatch's `weather_years` hook.

    Returns a list of (solar_cf[8760], wind_cf[8760]) pairs — one per year — that
    `ChronologicalSimulator(..., weather_years=...)` consumes INSTEAD of the synthetic
    generator. Each array is an hourly capacity factor in [0,1].

    Expected input: an `.npz` with arrays `solar` and `wind`, each shaped (Y, 8760)
    (Y reanalysis years). This is the single integration point for real data — to wire
    a live feed, materialise such a file from a provider and point the CLI at it:

      • ERA5 (ECMWF / Copernicus CDS): hourly `ssrd` → GHI → PV AC CF (apply a system
        derate / tracking model); `100m wind speed` → hub height → the IEC power curve
        in this module. Needs a free CDS API key.
      • NREL NSRDB (solar) + WIND Toolkit (wind): hourly site CF directly; needs a
        free NREL API key.

    Convert each provider year to a 2×8760 CF pair, stack to (Y,8760), and save. Keeping
    the loader file-based (not network-bound) keeps the model deterministic, offline,
    and provider-agnostic; only this function changes to support a new source.
    """
    with np.load(path) as d:
        solar, wind = np.asarray(d["solar"], float), np.asarray(d["wind"], float)
    if solar.shape != wind.shape or solar.shape[1] != 8760:
        raise ValueError(f"expected solar/wind shaped (Y, 8760); got {solar.shape}, {wind.shape}")
    return [(solar[i], wind[i]) for i in range(solar.shape[0])]


# ─────────────────────────────────────────────────────────────────────────────
# 3. WEATHER GENERATION  (Gaussian copula solar-wind correlation)
# ─────────────────────────────────────────────────────────────────────────────

# Daily cloud-transmission marginal: Beta(α,β). Its mean is the average fraction of
# clear-sky irradiance that reaches the panel. Defined here (one source) so the
# clear-sky normaliser and the cloud draw in generate_weather_year stay consistent.
CLOUD_BETA_A, CLOUD_BETA_B = 3.0, 1.5
CLOUD_MEAN = CLOUD_BETA_A / (CLOUD_BETA_A + CLOUD_BETA_B)   # 0.667


def load_profile(name: str = "flat") -> np.ndarray:
    """An 8760-h datacenter load shape, **normalised to mean 1.0** (so it is a shape, not
    a level — the model stays "per MW of *average* load" and every annual `/8760`
    denominator is unchanged). `"flat"` (default) returns ones, reducing the dispatch
    exactly to the constant-load model and leaving all published numbers untouched.

    `"cooling"` adds a temperature-driven cooling (PUE) overhead on top of a constant IT
    base: the facility draws more on hot summer afternoons, so peak load exceeds average
    and the firm gas backup — sized to *peak* load — must be a little larger. The IT
    compute itself is constant; only the cooling fraction breathes with the weather proxy.
    """
    if name == "flat":
        return np.ones(8760)
    if name == "cooling":
        h = np.arange(8760)
        doy, hod = h // 24, h % 24
        seasonal = 0.5 * (1.0 + np.cos(2 * np.pi * (doy - 200) / 365))   # summer peak, [0,1]
        diurnal = np.clip(np.sin((hod - 7) * np.pi / 14), 0.0, 1.0)      # afternoon peak
        raw = 1.0 + 0.25 * seasonal * diurnal      # up to +25% cooling on a hot afternoon
        return raw / raw.mean()                    # renormalise to mean 1 (a pure shape)
    raise ValueError(f"unknown load_profile {name!r}; expected 'flat' or 'cooling'")


def _ar1_series(phi: float, n: int, rng: np.random.Generator) -> np.ndarray:
    """A length-`n` stationary AR(1) trace ~ N(0,1) with persistence `phi`.

    Factored out of `generate_weather_year` so the synoptic factor can be generated
    once and *shared* across sites (for the spatial-diversification portfolio). The
    draw sequence is identical to the previous inline loop, so single-site results are
    byte-for-byte unchanged.
    """
    x = np.empty(n)
    x[0] = rng.standard_normal()
    sig = math.sqrt(1.0 - phi ** 2)
    for d in range(1, n):
        x[d] = phi * x[d - 1] + sig * rng.standard_normal()
    return x


def solar_clearsky(mean_irr_kwh_m2_day: float, performance_ratio: float = 1.0) -> np.ndarray:
    """
    Deterministic 8760-h clear-sky AC capacity-factor trace.

    `mean_irr_kwh_m2_day` is the site's *actual* (cloud-inclusive) average GHI, as
    reported by NSRDB / PVGIS. The downstream stochastic cloud factor (mean
    `CLOUD_MEAN`) multiplies this trace, so to land the *effective* (delivered)
    annual CF at the physically-correct `mean_irr/24` we normalise the clear-sky
    mean to `mean_irr/24 / CLOUD_MEAN`. (v5.5 fix: previously the clear-sky mean was
    set to `mean_irr/24` and the cloud factor was then applied *again*, double-counting
    cloud losses and depressing the simulated solar CF ~33% below NSRDB/Lazard — e.g.
    US 0.153 vs the ~0.22–0.25 implied by the same Lazard LCOE inputs.)

    `performance_ratio` (default 1.0) is an explicit system performance-ratio knob: the
    effective CF lands at `mean_irr/24 · performance_ratio`. The default of 1.0 keeps the
    CF *anchored to the imported-LCOE cost basis* (the v5.5 invariant — the simulated CF
    sits inside the Lazard utility-solar band the LCOE is levelised at, NOT derived
    bottom-up from a DC→AC system model). Set <1.0 (e.g. ~0.8) to instead derate toward a
    bottom-up specific-yield CF; this is then NO LONGER cost-basis-consistent and the
    imported solar LCOE should be re-levelled to match.
    """
    hours = np.arange(8760)
    doy   = hours // 24
    hod   = hours % 24
    seasonal = 1.0 + 0.35 * np.cos(2 * np.pi * (doy - 172) / 365)
    diurnal  = np.clip(np.sin((hod - 6) * np.pi / 12), 0, 1) ** 1.1
    raw = diurnal * seasonal
    # Pre-divide by the cloud mean so that E[clear-sky × cloud] = mean_irr/24 · PR.
    target_cf = mean_irr_kwh_m2_day / 24.0 * performance_ratio / CLOUD_MEAN
    # Iterate once to absorb the (small) clip at 1.0 of summer-noon hours, so the
    # *clipped* trace still carries the intended mean rather than losing the peaks.
    cs = raw * (target_cf / raw.mean() if raw.mean() > 0 else 1.0)
    for _ in range(3):
        clipped = np.clip(cs, 0.0, 1.0)
        m = clipped.mean()
        if m <= 0:
            break
        cs = cs * (target_cf / m)
    return np.clip(cs, 0.0, 1.0)


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
    wind_v_ci: float = 3.0,
    wind_v_rated: float = 11.0,
    wind_v_cutout: float = 25.0,
    synoptic_f: "Optional[np.ndarray]" = None,
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

    `synoptic_f`, if given, is a precomputed daily synoptic factor (365,) used INSTEAD
    of generating one internally — the seam the spatial-diversification portfolio uses
    to *share* a regional Dunkelflaute factor across sites (see `generate_weather_portfolio`).
    """
    rho = float(np.clip(wind_solar_corr, -0.999, 0.999))
    lam = float(np.clip(syn_loading, 0.0, math.sqrt(max((rho + 1.0) / 2.0, 0.0)) - 1e-6))
    phi = float(np.clip(syn_persistence, 0.0, 0.999))

    # Persistent synoptic common factor f_d ~ N(0,1), AR(1) (φ) — daily scale.
    # Shared across sites when supplied (spatial diversification); else generated here.
    f = _ar1_series(phi, 365, rng) if synoptic_f is None else np.asarray(synoptic_f, float)

    # Residual contemporaneous pair (g1,g2): corr ρ_g so net corr(z1,z2)=ρ.
    one_m = 1.0 - lam ** 2
    rho_g = float(np.clip((rho - lam ** 2) / one_m if one_m > 1e-9 else 0.0,
                          -0.999, 0.999))
    g1 = rng.standard_normal(365)
    g2 = rho_g * g1 + math.sqrt(max(1 - rho_g ** 2, 0)) * rng.standard_normal(365)

    z1 = lam * f + math.sqrt(one_m) * g1   # → cloud,  Var=1
    z2 = lam * f + math.sqrt(one_m) * g2   # → wind,   Var=1

    # Cloud factor: Beta(α, β) marginal via probability integral transform
    u_cloud = np.clip(ndtr(z1), 1e-6, 1 - 1e-6)
    daily_raw = betaincinv(CLOUD_BETA_A, CLOUD_BETA_B, u_cloud)

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

    # IEC power curve. v5.5: rated speed lowered 13.0 → 11.0 m/s (and cut-in 3.5 → 3.0)
    # to represent a modern LOW-SPECIFIC-POWER onshore turbine (large rotor / rated kW),
    # which is what utility fleets and Lazard's $/MWh now assume. The old 13 m/s rated
    # (high-specific-power) curve gave CF ≈ 0.22 at 7.5 m/s — below Lazard's onshore
    # CF basis (0.30–0.55) and inconsistent with the wind LCOE imported from it.
    v_ci, v_r, v_co = wind_v_ci, wind_v_rated, wind_v_cutout
    wind = np.where(speeds < v_ci, 0.0,
           np.where(speeds >= v_co, 0.0,
           np.where(speeds >= v_r, 1.0,
                    ((speeds - v_ci) / (v_r - v_ci)) ** 3)))

    return solar, wind


def generate_weather_portfolio(
    clearsky: np.ndarray,
    mean_wind_ms: float,
    rng: np.random.Generator,
    n_sites: int = 1,
    site_synoptic_corr: float = 0.7,
    syn_persistence: float = 0.82,
    **kwargs,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Solar + wind CF for a PORTFOLIO of `n_sites` geographically-separated sites,
    returned as the portfolio-average hourly CF (one solar, one wind trace).

    Why this is the right shape. A real high-RE operator does not build on one patch
    of ground; it spreads generation across sites. Dunkelflaute is synoptic-scale
    (continental weather systems), so sites in a region experience it *together* — but
    not perfectly: local cloud and wind texture, and the edges of a weather system,
    decorrelate. Averaging partially-correlated sites therefore **preserves the mean CF
    exactly** (so the imported-LCOE cost basis is untouched) while **softening the
    multi-day tails** — which is precisely the quantity that sets high-RE storage/backup.
    The single largest *directional* bias in the headline single-site results (§12) is
    that it has no such smoothing; this knob restores it.

    Model. All sites share one regional synoptic factor `f_common` (AR(1), persistence
    φ); each site's factor is `f_i = √c·f_common + √(1−c)·f_i^indep`, so any two sites
    have synoptic correlation `c = site_synoptic_corr` while each `f_i` keeps the same
    AR(1)(φ) law (hence each site's marginals/CF are individually unchanged). Local cloud
    and wind residuals are drawn independently per site. `n_sites=1` reduces **exactly**
    to `generate_weather_year` (identical draw order), so it leaves the default model and
    all published numbers untouched.

    `c→1` ⇒ fully coincident sites ⇒ no smoothing; `c→0` ⇒ independent sites ⇒ maximal
    smoothing (optimistic — real intra-region sites are strongly coupled, so the default
    c=0.7 is deliberately conservative about how much diversification actually helps).
    """
    n = max(int(n_sites), 1)
    if n == 1:
        return generate_weather_year(clearsky, mean_wind_ms, rng,
                                     syn_persistence=syn_persistence, **kwargs)

    phi = float(np.clip(syn_persistence, 0.0, 0.999))
    c = float(np.clip(site_synoptic_corr, 0.0, 1.0))
    a, b = math.sqrt(c), math.sqrt(1.0 - c)
    f_common = _ar1_series(phi, 365, rng)

    sol_acc = np.zeros(8760)
    win_acc = np.zeros(8760)
    for _ in range(n):
        f_site = a * f_common + b * _ar1_series(phi, 365, rng)
        s, w = generate_weather_year(clearsky, mean_wind_ms, rng,
                                     syn_persistence=phi, synoptic_f=f_site, **kwargs)
        sol_acc += s
        win_acc += w
    return sol_acc / n, win_acc / n


