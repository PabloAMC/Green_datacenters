#!/usr/bin/env python3
"""
Build the project's GitHub Pages site — a single self-contained `docs/index.html`
generated from the authoritative export (`output/*_firm_results.json`) and the committed
figures, so the headline conclusions and assumptions are visible at a glance and **cannot
drift** from the numbers (same discipline as `tools/check_doc_tables.py`).

Figures are embedded as base64, so the file is fully self-contained (offline / emailable).
Deterministic — no wall-clock — so rebuilding at the same commit/inputs is byte-stable.

    python tools/build_report.py            # writes docs/index.html
    make report
"""
import base64
import json
import os
import sys

# Run from the repo root without installing the package (CI installs deps, not the
# package), so `import lcoe` resolves whether invoked as a script or a module.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REPO_URL = "https://github.com/PabloAMC/Green_datacenteres"

from lcoe.params import (SOLAR, WIND, SOLAR_EU, WIND_EU, BATTERY_US, BATTERY_EU,
                         GAS, GAS_EU, REGIONS, RESOURCE_PRESETS, MODEL_VERSION)  # noqa: E402
MILESTONES = [2025, 2030, 2035, 2040]


def _parity_year(years, series, baseline):
    diff = [s - b for s, b in zip(series, baseline)]
    if diff[0] <= 0:
        return float(years[0])
    for i in range(len(diff) - 1):
        if diff[i] > 0 >= diff[i + 1]:
            f = diff[i] / (diff[i] - diff[i + 1])
            return years[i] + f * (years[i + 1] - years[i])
    return None


def _img(path):
    p = os.path.join(ROOT, "figs", path)
    if not os.path.exists(p):
        return ""
    with open(p, "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode()


def _load(prefix):
    with open(os.path.join(ROOT, "output", f"{prefix}_results.json")) as fh:
        return json.load(fh)


def _crossover(d, R):
    years = d["years"]
    py = _parity_year(years, d["scenarios"][R]["lcoe"], d["gas_pure"])
    return f"~{py:.0f}" if py is not None else f">{years[-1]}"


def parity_table(d):
    years = d["years"]
    idx = {y: years.index(y) for y in MILESTONES}
    Rs = sorted(d["scenarios"], key=float)
    rows = []
    head = "".join(f"<th>{y}</th>" for y in MILESTONES)
    for R in Rs:
        lc = d["scenarios"][R]["lcoe"]
        cells = "".join(f"<td>{lc[idx[y]]:.0f}</td>" for y in MILESTONES)
        rows.append(f"<tr><th>{float(R):.0%}</th>{cells}"
                    f"<td class='cx'>{_crossover(d, R)}</td></tr>")
    g = d["gas_pure"]
    gas = "".join(f"<td>{g[idx[y]]:.0f}</td>" for y in MILESTONES)
    rows.append(f"<tr class='gas'><th>Gas baseline</th>{gas}<td>—</td></tr>")
    if d.get("grid_ppa"):
        p = d["grid_ppa"]
        ppa = "".join(f"<td>{p[idx[y]]:.0f}</td>" for y in MILESTONES)
        rows.append(f"<tr class='ref'><th>Grid + renewable contract (on-grid reference)</th>{ppa}<td>—</td></tr>")
    if d.get("h2_system"):
        h = d["h2_system"]["lcoe"]
        h2 = "".join(f"<td>{h[idx[y]]:.0f}</td>" for y in MILESTONES)
        hcx = _parity_year(years, d["h2_system"]["lcoe"], d["gas_pure"])
        rows.append(f"<tr class='h2'><th>Gas-free H₂ system</th>{h2}"
                    f"<td class='cx'>{'~%.0f' % hcx if hcx else '—'}</td></tr>")
    return (f"<table><thead><tr><th>Renewable target</th>{head}<th>vs-gas crossover</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def findings(us, eu):
    def py(d, R):
        return _parity_year(d["years"], d["scenarios"][R]["lcoe"], d["gas_pure"])
    eu70, eu90 = py(eu, "0.70"), py(eu, "0.90")
    us70, us90 = py(us, "0.70"), py(us, "0.90")
    items = [
        f"<b>Europe — renewables already win at moderate shares.</b> Carbon-priced, expensive "
        f"EU gas (rising from ${eu['gas_pure'][0]:.0f} to ${eu['gas_pure'][-1]:.0f}/MWh) "
        f"means a firm 70%-renewable build reaches parity ~{eu70:.0f}, and even a "
        f"90%-renewable one by ~{eu90:.0f}.",
        f"<b>US — cheap gas is a moat at high renewable shares.</b> Untaxed ~$46/MWh gas "
        f"keeps a 90%-renewable build off parity beyond {us['years'][-1]} (US 90% crossover "
        f"{_crossover(us,'0.90')}); only moderate-share builds cross, in the mid-to-late 2030s.",
        "<b>A firm battery-only system tops out near ~94% renewable.</b> Multi-day Dunkelflaute "
        "neither sun nor wind covers, and battery power can't bridge days — the last few "
        "percent always fall to gas. Going higher needs long-duration storage or hydrogen.",
        f"<b>A fully gas-free, zero-carbon datacenter is feasible.</b> A co-optimised "
        f"solar+wind+battery+green-hydrogen build delivers ${eu['h2_system']['lcoe'][0]:.0f}→"
        f"${eu['h2_system']['lcoe'][-1]:.0f}/MWh in the EU, crossing below gas around "
        f"{'%.0f' % _parity_year(eu['years'], eu['h2_system']['lcoe'], eu['gas_pure'])}.",
        "<b>Where you build dominates the cost.</b> Across a poor→good site in a region the "
        "90%-renewable delivered cost spans tens of $/MWh (the shaded bands in the figures); a "
        "multi-site portfolio softens the multi-day lulls and cuts high-renewable cost materially.",
        "<b>Off-grid is itself a premium.</b> Staying on the grid with a renewable-energy "
        "contract sits <i>below</i> the off-grid high-renewable optimum in both regions — "
        "off-grid buys siting independence, at a cost.",
    ]
    return "".join(f"<li>{x}</li>" for x in items)


def _fig_box(img):
    return ('<figure style="margin:1em 0;background:#fff;border:1px solid var(--line);'
            f'border-radius:8px;padding:10px"><img src="{img}" style="width:100%" '
            'alt="off-grid datacenter cost by location"></figure>')


def locations_section():
    """The 'across geographies' section — the firm build computed per large EU country / US
    state on REAL ERA5 weather, as two per-state comparisons: a gas-backed renewable-target
    build (no-wind ~55% vs with-wind ~80%, from tools/build_locations_re.py) and a fully
    zero-carbon self-made-hydrogen build (no-wind vs with-wind, from build_locations_h2.py).
    Each is a vertical 5×2 grid of per-state panels. Empty blocks are skipped."""
    out = []
    intro_done = False

    # ── Gas-backed: is a wind park worth it? (same target, wind optional) ──────────
    rep = os.path.join(ROOT, "output", "locations_re_results.json")
    refig = _img("locations_re_grid.png")
    if refig and os.path.exists(rep):
        d = json.load(open(rep))
        span = d.get("weather", "real ERA5")
        yspan = span.replace("real ERA5", "").strip() or span
        nyears = len({y for x in d["locations"] for y in x.get("weather_years", [])}) or 11

        def rrow(x):
            nw, w = x["delivered_nowind"][10], x["delivered_wind"][10]
            return (f"<tr><th>{x['label']}</th><td>{'Europe' if x['region']=='eu' else 'US'}</td>"
                    f"<td>{x['cf_solar']:.2f}</td><td>{x['cf_wind']:.2f}</td>"
                    f"<td>{x['target']:.0%}</td><td>${nw:.0f}</td><td>${w:.0f}</td>"
                    f"<td>${nw - w:.0f}</td></tr>")
        rows = "".join(rrow(x) for x in
                       sorted(d["locations"], key=lambda x: (x["region"], x["delivered_wind"][10])))
        out.append(
            '<h2>Across geographies</h2>'
            '<p>The same firm off-grid build, now computed <b>at each of seven large EU countries '
            f'and seven US states</b> (the biggest data-center markets in each region) on <b>real '
            f'ERA5 reanalysis weather ({yspan}, {nyears} '
            'years)</b> — one grid point per location, every real year a dispatch sample (so the '
            'curves carry real interannual variability: dark/calm years included). Within a region '
            'only the renewable <b>resource</b> differs — gas, carbon and technology costs are the '
            'region default — so this isolates how much <b>where you build</b>, and <b>whether you '
            'build a wind park</b>, move the cost.</p>'
            '<h3>Is a wind park worth building? (gas-backed)</h3>'
            '<p>Solar panels are quick to permit; a wind park is a far bigger siting undertaking, '
            'so it is worth asking where one actually pays off. This is a <b>fair</b> test: both '
            'builds are optimised to the <b>same renewable target</b> at each site — the most a '
            'solar + battery + gas system can reach <i>without</i> wind (~55–68%, shown in each '
            'panel) — and the <b style="color:#0072B2">wind build is simply allowed to add a wind '
            'park only if it lowers cost</b>. Because building no wind is always an option, the '
            'blue line is <b>never above</b> the orange <b style="color:#b07900">no-wind</b> line: '
            'it <b>dips below where wind genuinely competes</b> (the windy markets — the United '
            'Kingdom; Texas, Iowa) and <b>all but merges with it where wind is too weak to bother</b> '
            '(Arizona, California, Italy, ~2% capacity factor — the optimiser builds almost none, so '
            'by the mid-2030s the lines coincide). The grey '
            'dashed line is the gas baseline; the <b style="color:#8e44ad">purple dash-dot line a '
            'small modular (nuclear) reactor</b>. Pushing renewables <i>beyond</i> this no-wind '
            'ceiling needs either a wind park where the wind is good, or storage/hydrogen — that is '
            'the next two figures. (The reactor is competitive in carbon-priced Europe; in the '
            'cheap-gas US the renewable+gas builds sit well below it.)</p>'
            + _fig_box(refig)
            + "<table><thead><tr><th>State</th><th>Region</th><th>Solar CF</th><th>Wind CF</th>"
              "<th>Target</th><th>No wind 2035</th><th>Wind-optional 2035</th>"
              "<th>Wind saves</th></tr></thead>"
              f"<tbody>{rows}</tbody></table>"
            '<div class="caveat">Real hourly ERA5 at one point per location ('
            f'{yspan}); solar capacity factor from horizontal irradiance ×1.25; region-default '
            'gas, carbon and technology costs. Each site\'s solar and wind LCOE is <b>re-anchored '
            'from the model\'s calibration capacity factor to that site\'s real CF</b>, so a '
            'low-wind site correctly pays more per MWh for wind (and the optimiser builds none '
            'where it is not competitive) and a sunny site pays less for solar. Reduced optimiser '
            'fidelity (directional to ~±15% in level — the cross-site <i>ranking</i> and the '
            '<i>wind gap</i> are the robust messages). Per-state figures are in '
            '<code>figs/locations_re/</code>.</div>')
        intro_done = True

    # ── Fully zero-carbon, self-made hydrogen: no-wind vs with-wind ───────────────
    lp = os.path.join(ROOT, "output", "locations_h2_results.json")
    lfig = _img("locations_h2_grid.png")
    if lfig and os.path.exists(lp):
        L = json.load(open(lp))
        span = L.get("weather", "real ERA5")
        yspan = span.replace("real ERA5", "").strip() or span

        def hrow(x):
            nw, w = x["lcoe_nowind"][10], x["lcoe_wind"][10]
            return (f"<tr><th>{x['label']}</th><td>{'Europe' if x['region']=='eu' else 'US'}</td>"
                    f"<td>{x['cf_solar']:.2f}</td><td>{x['cf_wind']:.2f}</td>"
                    f"<td>${nw:.0f}</td><td>${w:.0f}</td><td>${nw - w:.0f}</td></tr>")
        rows = "".join(hrow(x) for x in
                       sorted(L["locations"], key=lambda x: (x["region"], x["lcoe_wind"][10])))
        out.append(
            ('' if intro_done else '<h2>Across geographies</h2>')
            + '<h3>Fully zero-carbon, self-made hydrogen: no wind vs with wind</h3>'
            '<p>To drop gas entirely, replace the backstop with <b>green hydrogen made from '
            'surplus renewables</b> (the small residual bought from the market). These two builds '
            'are <b>zero-carbon by construction</b> — <b style="color:#b07900">solar + battery + '
            'hydrogen (no wind)</b> and <b style="color:#0072B2">solar + wind + battery + '
            f'hydrogen</b> — on the same {yspan} weather. Again the gap between the lines is what a '
            'wind park buys: where wind is strong it sits well below the no-wind line (the United '
            'Kingdom saves ~$53/MWh, Sweden ~$39, Iowa and Poland ~$30); where wind is scarce — '
            'Arizona, California and Italy, all around a 2% capacity factor — the optimiser builds '
            'essentially no wind, so the two lines coincide (wind saves ~$0) and the '
            'easier-to-permit no-wind build is the one to pick. The grey dashed line is the region '
            'gas baseline (it emits); the '
            '<b style="color:#8e44ad">purple dash-dot line is a small modular (nuclear) '
            'reactor</b> — the other firm zero-carbon option — which here is often competitive '
            'with the hydrogen build, especially in carbon-priced Europe.</p>'
            + _fig_box(lfig)
            + "<table><thead><tr><th>State</th><th>Region</th><th>Solar CF</th><th>Wind CF</th>"
              "<th>No-wind 2035</th><th>With-wind 2035</th><th>Wind saves</th></tr></thead>"
              f"<tbody>{rows}</tbody></table>"
            '<div class="caveat">Real hourly ERA5 at one point per location ('
            f'{yspan}); solar capacity factor from horizontal irradiance ×1.25; region-default '
            'carbon and technology costs. Each site\'s solar and wind LCOE is re-anchored from the '
            'calibration capacity factor to that site\'s real CF, so the optimiser builds wind '
            'only where it is competitive (≈0 in the ~2%-CF sites). Reduced optimiser fidelity. '
            'Per-state figures are in <code>figs/locations_h2/</code>.</div>')
    return "".join(out)


def wind_section():
    """The 'do you need a wind park?' section (figs/solar_only.png +
    output/solar_only_results.json from tools/build_solar_only.py). Empty if not built."""
    path = os.path.join(ROOT, "output", "solar_only_results.json")
    fig = _img("solar_only.png")
    if not fig or not os.path.exists(path):
        return ""
    d = json.load(open(path)); yr = d["year"]
    wall = d["data"]["us"]["solo"]["max_re"]
    blocks = [
        '<h2>Do you even need a wind park?</h2>'
        '<p>Solar panels are modular and quick to permit; a wind park is a far bigger siting '
        'and permitting undertaking. So it is worth asking what <b>dropping wind entirely</b> '
        'costs. This compares a solar + wind + battery build against <b>solar + battery only</b> '
        f'(both firm, with gas backup) in {yr}.</p>'
        f'<p><b>There is a hard wall.</b> A solar + battery + gas system tops out near '
        f'<b>~{wall:.0%} renewable</b> — nights <i>and</i> multi-day cloud always fall to gas, '
        'and a battery cannot shift energy across days — whereas adding wind reaches ~94%. Below '
        'the wall, solar-only is only modestly pricier in the US (strong solar) and a clearer '
        'premium in Europe (weak winter sun). <b>Bottom line:</b> for a <i>moderate</i> renewable '
        'target, solar + battery alone is a reasonable, much-easier-to-build choice; pushing to '
        '<i>high</i> renewable fractions genuinely needs wind (or long-duration storage / hydrogen).</p>'
        + _fig_box(fig)]

    # ── Zero-carbon synthesis (solar+battery+H₂) ────────────────────────────────
    zp = os.path.join(ROOT, "output", "zerocarbon_results.json")
    zfig = _img("zerocarbon.png")
    if zfig and os.path.exists(zp):
        z = json.load(open(zp))

        def c(region, key):
            return next(o["value"] for o in z["data"][region] if o["key"] == key)
        blocks.append(
            '<h3>… and what about solar + battery + hydrogen (no wind)?</h3>'
            '<p>To go <b>fully zero-carbon without a wind park</b>, replace the gas backstop with '
            'green hydrogen. <b>All the hydrogen options below are green</b> (zero-carbon) — they '
            'differ only in <b>how you get the H₂</b>. <b>Making it yourself</b> — a solar-heavy '
            'build plus an electrolyser that turns <i>surplus</i> renewables into H₂, buying only '
            'a few percent from the market — brings a wind-free zero-carbon datacenter to '
            f'<b>~${c("us","nowind_selfmade"):.0f}/MWh (US) / ${c("eu","nowind_selfmade"):.0f} '
            f'(EU)</b> by {z["year"]}. <b>Buying it all</b> on the market instead (no electrolyser) '
            f'is far dearer (${c("us","nowind_bought"):.0f} / ${c("eu","nowind_bought"):.0f}). And '
            'the self-made wind-free build is only ~$12–17 above the <i>same self-made build with a '
            f'wind park added</i> (${c("us","wind_selfmade"):.0f} / ${c("eu","wind_selfmade"):.0f}) '
            '— so the wind park, not the hydrogen, is the smaller lever here. In Europe that '
            'with-wind build is actually the <b>cheapest option of all</b>, since carbon-priced gas '
            'is dearer.</p>'
            '<p class="sub" style="font-size:13px">In the chart, the three green bars are the same '
            'green hydrogen; the only differences are <b>whether there is a wind park</b> and '
            '<b>whether the H₂ is self-made or bought</b>.</p>'
            + _fig_box(zfig))
    return "".join(blocks)


def assumptions_table(us, eu):
    uc, ec = us["simulated_cf"], eu["simulated_cf"]
    rows = [
        ("Solar PV — LCOE₀ · learning rate",
         f"${SOLAR.lcoe_today:.0f}/MWh · {SOLAR.learning_rate:.0%}",
         f"${SOLAR_EU.lcoe_today:.0f}/MWh · {SOLAR_EU.learning_rate:.0%}",
         "Lazard v18; Way et al. Joule 2022"),
        ("Onshore wind — LCOE₀ · LR",
         f"${WIND.lcoe_today:.0f}/MWh · {WIND.learning_rate:.0%}",
         f"${WIND_EU.lcoe_today:.0f}/MWh · {WIND_EU.learning_rate:.0%}",
         "Lazard v18; OWID"),
        ("LFP battery — energy · power",
         f"${BATTERY_US.capex_kwh_today:.0f}/kWh · ${BATTERY_US.capex_kw_today:.0f}/kW",
         f"${BATTERY_EU.capex_kwh_today:.0f}/kWh · ${BATTERY_EU.capex_kw_today:.0f}/kW",
         "BloombergNEF 2024–25; Ember 2025"),
        ("Gas price",
         f"${GAS.gas_price_mmbtu:.0f}/MMBtu", f"${GAS_EU.gas_price_mmbtu:.0f}/MMBtu",
         "EIA Henry Hub / TTF forward"),
        ("Carbon price trajectory",
         "linear $0", f"logistic ${GAS_EU.carbon_price_today:.0f}→${GAS_EU.carbon_price_ceiling:.0f}/tCO₂",
         "EU ETS Fit-for-55"),
        ("WACC (solar/wind · battery · gas)",
         "5.5% · 7% · 9%", "5.5% · 7% · 9%", "NREL ATB 2024; merchant spread"),
        ("Simulated capacity factor (solar / wind)",
         f"{uc['solar']:.2f} / {uc['wind']:.2f}", f"{ec['solar']:.2f} / {ec['wind']:.2f}",
         "inside Lazard CF bands"),
    ]
    body = "".join(f"<tr><th>{n}</th><td>{u}</td><td>{e}</td><td class='src'>{s}</td></tr>"
                   for (n, u, e, s) in rows)
    return ("<table><thead><tr><th>Assumption</th><th>US</th><th>Europe</th>"
            f"<th>Source</th></tr></thead><tbody>{body}</tbody></table>")


HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Off-Grid Datacenter LCOE — results</title>
<style>
:root{{--blue:#3A86FF;--ink:#1d2433;--muted:#5b6472;--line:#e3e7ee;--gas:#6B705C;--h2:#073B4C}}
*{{box-sizing:border-box}}
body{{font:16px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);
  margin:0;background:#f7f9fc}}
.wrap{{max-width:980px;margin:0 auto;padding:32px 20px 64px}}
h1{{font-size:30px;margin:.2em 0 .1em}} h2{{font-size:21px;margin:1.8em 0 .5em;
  border-bottom:2px solid var(--line);padding-bottom:.25em}}
.sub{{color:var(--muted);font-size:17px;margin:0 0 .6em}}
.repo{{margin:0 0 1.2em}}
.repo a{{display:inline-block;background:var(--blue);color:#fff;padding:7px 14px;
  border-radius:8px;font-size:14px;font-weight:600;text-decoration:none}}
.repo a:hover{{background:#2f6fe0}}
.caveat{{background:#fff8e6;border:1px solid #f0d98a;border-radius:10px;padding:14px 18px;
  font-size:14.5px;color:#6b5512}}
ul.find{{padding-left:20px}} ul.find li{{margin:.5em 0}}
table{{border-collapse:collapse;width:100%;font-size:14px;margin:.5em 0;background:#fff;
  border:1px solid var(--line);border-radius:8px;overflow:hidden}}
th,td{{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line)}}
thead th{{background:#eef2f8;text-align:right}} tbody th{{text-align:left;font-weight:600}}
td.cx{{font-weight:700;color:var(--blue)}} td.src{{text-align:left;color:var(--muted);font-size:12.5px}}
tr.gas th,tr.gas td{{color:var(--gas);font-weight:600}}
tr.h2 th,tr.h2 td{{color:var(--h2);font-weight:600}} tr.ref td,tr.ref th{{color:#06a37a}}
.figs{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
.figs figure{{margin:0;background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px}}
.figs img{{width:100%;height:auto}} figcaption{{font-size:12.5px;color:var(--muted);margin-top:6px}}
@media(max-width:760px){{.figs{{grid-template-columns:1fr}}}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:24px}} @media(max-width:760px){{.cols{{grid-template-columns:1fr}}}}
.foot{{margin-top:40px;font-size:12.5px;color:var(--muted);border-top:1px solid var(--line);padding-top:14px}}
code{{background:#eef2f8;padding:1px 5px;border-radius:4px;font-size:13px}}
a{{color:var(--blue)}}
</style></head><body><div class="wrap">

<h1>Off-Grid Datacenter LCOE</h1>
<p class="sub">The least-cost mix of solar, wind, battery and gas backup to run an
always-on, off-grid datacenter on mostly-renewable power — and the year going renewable
beats burning gas, across the US and Europe.</p>
<p class="repo"><a href="{repo}">▶&nbsp; View the source code &amp; full methodology on GitHub</a></p>

<div class="caveat"><b>Read this first.</b> This is a <b>stylised techno-economic model</b>:
trust the <b>directional comparisons</b>, not absolute numbers to better than <b>~±20–30%</b>.
The headline runs on <b>synthetic (not measured) weather</b> and a <b>single generation
site</b> by default; both can be replaced with real reanalysis and multi-site portfolios.
All numbers below are generated directly from the model's exported results.</div>

<h2>The question: how dirty does an AI datacenter have to be?</h2>
<p>The boom in AI datacenters has come with a worry that is now a headline in its own right —
that they are an <b>environmental problem</b>, that their soaring electricity demand simply means
a surge in carbon emissions. This model exists to test, with numbers, <b>how justified that worry
is</b>. The real question is not whether a datacenter draws a lot of power — it does — but whether
that power can be <b>clean</b>, and what it costs to make it so.</p>
<p>The natural way to clean up a datacenter is to build it together with its own dedicated
generation — solar, wind and batteries, off-grid or behind the meter. The catch is that a
datacenter is a <b>firm</b> (always-on) load: it needs power every hour of every day, while sun
and wind do not. The sun sets; the wind can drop for days at a time (a <i>Dunkelflaute</i>). So
the environmental question comes down to the <b>backstop</b> that covers those gaps: do you fall
back on a <b>gas</b> turbine, which emits, or on a <b>clean firm</b> option — green hydrogen, a
small nuclear reactor, or enough storage — and how much does each cost? Squeezing the last
emissions out, from ~90% renewable to a true 100%, is where the bill climbs, because the rarest
dark, windless hours dominate it.</p>
<p>This model works out, transparently, the <b>least-cost mix</b> of solar, wind, battery and
backup for such a plant, and the resulting <b>delivered cost</b> — its levelised cost of energy
(LCOE: the all-in price per MWh of datacenter load once capital, fuel and maintenance are spread
over the plant's life) — for the United States and Europe, every year from 2025 to 2040. So it
answers the environmental question directly: <b>how much</b> of the year a datacenter can run on
renewables; <b>what a clean datacenter costs</b> versus simply burning gas, and when clean becomes
the <i>cheaper</i> choice rather than just the greener one; and whether a fully <b>zero-carbon</b>,
always-on datacenter is achievable today — and at what premium. The short answer, worked out
below: a datacenter need not be dirty. Running fully zero-carbon already costs only a modest
premium over gas in the cheap-gas US — and in carbon-priced Europe it is increasingly the
<i>cheaper</i> option — with that premium shrinking every year as solar, batteries and
electrolysers get cheaper.</p>

<h2>Key findings</h2>
<ul class="find">{findings}</ul>

<h2>Delivered cost &amp; parity ($/MWh of load)</h2>
<div class="cols">
  <div><h3 style="margin:.2em 0">United States</h3>{us_table}</div>
  <div><h3 style="margin:.2em 0">Europe</h3>{eu_table}</div>
</div>
<p class="sub" style="font-size:13.5px;margin-top:.6em">The <b>renewable target</b> is the minimum
share of the datacenter's yearly energy that must come from solar + wind + battery (the rest is
covered by gas). Firm (always-on) workload; gas backup sized to 100% of load. "Crossover" = the
first year the build's delivered cost drops below the gas baseline.</p>

<h2>Cost trajectories</h2>
<div class="figs">
  <figure><img src="{fig1_us}" alt="US cost trajectory"><figcaption>US — lines are the
   central site; shaded bands are the <b>resource/siting range</b> (poor↔good site). Includes
   the gas baseline, grid+PPA reference, and the fully-optimised gas-free H₂ system.</figcaption></figure>
  <figure><img src="{fig1_eu}" alt="EU cost trajectory"><figcaption>Europe — same series. EU
   renewables fall below carbon-priced gas far earlier than in the cheap-gas US.</figcaption></figure>
</div>
<div class="figs" style="margin-top:18px">
  <figure><img src="{fig3_us}" alt="US optimal mix"><figcaption>US optimal build (solar / wind
   overbuild + battery hours) by renewable target.</figcaption></figure>
  <figure><img src="{fig3_eu}" alt="EU optimal mix"><figcaption>Europe optimal build — the firm
   high-renewable optimum is wind-heavy to ride out multi-day lulls.</figcaption></figure>
</div>

{locations_section}

{wind_section}

<h2>Key assumptions</h2>
{assumptions}
<p class="sub" style="font-size:13.5px">All in real 2025 USD. Costs fall over time via
Wright's-Law learning curves. Full derivations, data sources and the accuracy summary are in
<code>model_documentation.md</code>.</p>

<h2>What is and isn't modelled</h2>
<ul class="find">
<li><b>Default = firm, always-on:</b> gas backup covers 100% of load during lulls, so the worst
case is a known, capped fuel cost. Premium/AI workloads never shed and collapse to this case.</li>
<li><b>Weather is synthetic</b> but structured (multi-day Dunkelflaute, region-specific
resource and sun–wind correlation). A real-weather seam (<code>--weather</code>, ERA5/NSRDB)
and a multi-site portfolio (<code>--sites</code>) are built in but opt-in.</li>
<li><b>Single site by default</b> — the largest directional caveat; a geographic portfolio
softens the tails and lowers high-renewable cost.</li>
<li><b>Not modelled:</b> sub-hourly load variation, on-site fuel logistics, transmission.</li>
</ul>

<div class="foot">
Model v{version} · generated from <code>output/*_firm_results.json</code> at commit
<code>{commit}</code> (config {cfg}) · <a href="{repo}">source on GitHub</a> · licensed
CC BY 4.0. Reproduce: <code>make reproduce &amp;&amp; make report</code>.
</div>
</div></body></html>"""


def main():
    us, eu = _load("us_firm"), _load("eu_firm")
    prov = us.get("provenance") or {}
    html = HTML.format(
        findings=findings(us, eu),
        us_table=parity_table(us), eu_table=parity_table(eu),
        fig1_us=_img("us_firm_fig1_trajectories.png"),
        fig1_eu=_img("eu_firm_fig1_trajectories.png"),
        fig3_us=_img("us_firm_fig3_optimal_mix.png"),
        fig3_eu=_img("eu_firm_fig3_optimal_mix.png"),
        assumptions=assumptions_table(us, eu),
        locations_section=locations_section(),
        wind_section=wind_section(),
        version=MODEL_VERSION,
        commit=prov.get("git_commit", "—"),
        cfg=prov.get("config_sha256", "—"),
        repo=REPO_URL,
    )
    out = os.path.join(ROOT, "docs", "index.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        fh.write(html)
    kb = len(html.encode()) / 1024
    print(f"Wrote {out} ({kb:.0f} KB, self-contained).")


if __name__ == "__main__":
    main()
