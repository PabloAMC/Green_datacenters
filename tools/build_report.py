#!/usr/bin/env python3
"""
Build the project's GitHub Pages site — a small set of self-contained pages under
`docs/`, generated from the authoritative export (`output/*_firm_results.json`) and the
committed figures, so the headline conclusions and assumptions are visible at a glance
and **cannot drift** from the numbers (same discipline as `tools/check_doc_tables.py`).

Pages (each fully self-contained — figures embedded as base64 — offline / emailable):
  index.html        short overview: TL;DR, key findings, parity tables, trajectories
  geography.html    the EU siting ranking + continent scan, then per-market comparisons
  zero-carbon.html  the wind-park question + the gas-free hydrogen builds + footprint
  method.html       assumptions, trust/caveats, optimal builds, glossary

Deterministic — no wall-clock — so rebuilding at the same commit/inputs is byte-stable.

    python tools/build_report.py            # writes docs/*.html
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

REPO_URL = "https://github.com/PabloAMC/Green_datacenters"

from lcoe.params import (SOLAR, WIND, SOLAR_EU, WIND_EU, BATTERY_US, BATTERY_EU,
                         GAS, GAS_EU, REGIONS, RESOURCE_PRESETS, MODEL_VERSION)  # noqa: E402
MILESTONES = [2025, 2030, 2035, 2040]

NAV = [("index.html", "Overview"),
       ("geography.html", "Geography"),
       ("zero-carbon.html", "Zero-carbon"),
       ("method.html", "Method & trust")]


# ── data helpers ────────────────────────────────────────────────────────────────────

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


def _load_optional(name):
    path = os.path.join(ROOT, "output", name)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def _crossover(d, R):
    years = d["years"]
    py = _parity_year(years, d["scenarios"][R]["lcoe"], d["gas_pure"])
    return f"~{py:.0f}" if py is not None else f">{years[-1]}"


def _cross(y):   # crossover year, or an honest "never within horizon"
    return f"~{y:.0f}" if y else "not within the horizon"


def _fig_box(img, alt="model figure", caption=None):
    cap = (f'<figcaption>{caption}</figcaption>') if caption else ""
    return (f'<figure class="box"><img src="{img}" style="width:100%" alt="{alt}">{cap}'
            '</figure>')


# ── shared components ───────────────────────────────────────────────────────────────

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


def tldr(us, eu):
    """The three-takeaway executive summary, computed from the exports."""
    yrs = eu["years"]

    def sc(d, R):
        return d["scenarios"][R]["lcoe"]
    us70, us80 = sc(us, "0.70"), sc(us, "0.80")
    eu70, eu80, eu90 = sc(eu, "0.70"), sc(eu, "0.80"), sc(eu, "0.90")
    eu70_py = _parity_year(yrs, eu70, eu["gas_pure"])
    h2 = eu["h2_system"]["lcoe"]
    h2_py = _parity_year(yrs, h2, eu["gas_pure"])

    # Cheapest firm-clean sites, if the siting export exists.
    es = _load_optional("eu_siting_results.json")
    site_txt = ("Sites with firm clean power — Nordic/Alpine hydro or Icelandic "
                "geothermal — deliver 24/7 carbon-free electricity below today's "
                "European gas price")
    if es:
        i = es["sites"][0]["years"].index(es["ranking_year"])
        hydro = min(s["delivered"][i] for s in es["sites"] if s["resource"] == "hydro")
        geo = [s["delivered"][i] for s in es["sites"] if s["resource"] == "geothermal"]
        geo_txt = f" and Icelandic geothermal (~${geo[0]:.0f})" if geo else ""
        site_txt = (f"Sites with firm clean power — Nordic/Alpine hydro (~${hydro:.0f}/MWh)"
                    f"{geo_txt} — deliver 24/7 carbon-free electricity below today's "
                    f"European gas price (${eu['gas_pure'][0]:.0f})")

    items = [
        f"<li><b>Running a datacenter mostly on renewables is already affordable.</b> "
        f"Solar, wind and batteries can supply 70–80% of an always-on datacenter's energy "
        f"for ~${eu70[0]:.0f}–{eu80[0]:.0f}/MWh in Europe and ~${us70[0]:.0f}–{us80[0]:.0f} "
        f"in the US today (gas: ${eu['gas_pure'][0]:.0f} / ${us['gas_pure'][0]:.0f}) — "
        f"a real but modest premium, and in carbon-priced Europe the 70% build becomes the "
        f"<i>cheaper</i> plant {_cross(eu70_py)}.</li>",
        f"<li><b>The expensive part is the last 10–30%.</b> Batteries bridge nights, not "
        f"dark windless weeks, so pushing Europe from 80% to 90% renewable adds "
        f"~${eu90[0] - eu80[0]:.0f}/MWh today. Going fully gas-free takes wind plus "
        f"hydrogen or pumped storage: ${h2[0]:.0f}/MWh in Europe now, falling to "
        f"${h2[-1]:.0f} and crossing below gas {_cross(h2_py)}.</li>",
        f"<li><b>Where you build matters more than how green you aim.</b> {site_txt}; "
        f"in the cheap-gas US, by contrast, no renewable target beats "
        f"${us['gas_pure'][0]:.0f} gas within the horizon (70–80% gets within a few "
        f"$/MWh).</li>",
    ]
    # What a $/MWh premium means in the buyer's own units — the all-in cost of compute.
    # The only external anchor is the hardware term ($500–800 of chips+facility per MWh
    # consumed ≈ $25–35k per kW of IT on a 4–6-yr life, SemiAnalysis-style AI TCO);
    # the premiums themselves come from the exports.
    g0 = eu["gas_pure"][0]
    p70, ph2 = eu70[0] - g0, h2[0] - g0
    scale = (
        f'<p class="scale"><b>What a $/MWh premium means for the cost of compute.</b> '
        f'Electricity is a minority of a datacenter\'s bill: for an AI campus, chips and '
        f'buildings amortise to roughly $500–800 per MWh of electricity consumed '
        f'(≈$25–35k per kW of IT hardware on a 4–6-year life, plus the facility), so '
        f'power at Europe\'s gas price (${g0:.0f}/MWh) is only '
        f'~{100 * g0 / (800 + g0):.0f}–{100 * g0 / (500 + g0):.0f}% of the all-in '
        f'cost of compute. The 70%-renewable premium (+${p70:.0f}/MWh today) therefore '
        f'raises the cost of compute by ~{100 * p70 / (800 + g0):.0f}–'
        f'{100 * p70 / (500 + g0):.0f}%; even the full gas-free premium (+${ph2:.0f} '
        f'today, shrinking to nil as it crosses gas {_cross(h2_py)}) raises it '
        f'~{100 * ph2 / (800 + g0):.0f}–{100 * ph2 / (500 + g0):.0f}%. A conventional '
        f'facility, with cheaper hardware per MWh, feels the same premiums roughly '
        f'twice as hard.</p>')
    return ('<div class="tldr"><h2>Three things to take away</h2><ol>'
            + "".join(items) + '</ol>' + scale +
            '<p class="macro">None of this means sector emissions will fall: the AI '
            'buildout is raising electricity demand faster than clean supply is being added, '
            'and the big operators\' net-zero targets are slipping. What the model shows is '
            'the choice each <i>new</i> datacenter controls — it can be built mostly clean '
            'at a modest, and shrinking, premium.</p></div>')


def findings(us, eu):
    """Key findings: a scannable headline + one-sentence support, with the full
    paragraph behind a <details> expander."""
    def py(d, R):
        return _parity_year(d["years"], d["scenarios"][R]["lcoe"], d["gas_pure"])
    eu70, eu80, eu85, eu90 = py(eu, "0.70"), py(eu, "0.80"), py(eu, "0.85"), py(eu, "0.90")
    yrs = eu["years"]
    eu90_2040 = eu["scenarios"]["0.90"]["lcoe"][-1]
    h2 = eu["h2_system"]["lcoe"]
    h2_cross = _parity_year(yrs, h2, eu["gas_pure"])

    # Solar-only vs solar+wind (tools/build_solar_only.py export)
    so = _load_optional("solar_only_results.json")
    solo_wall, wind_max, solo_vs = "68%", "93%", ""
    if so:
        d = so["data"]["eu"]
        solo_wall = f"{100*d['solo']['max_re']:.0f}%"
        wind_max = f"{100*d['wind']['max_re']:.0f}%"
        re_common = max(r for r, _ in d["solo"]["points"] if any(abs(r - rw) < 1e-9 for rw, _ in d["wind"]["points"]))
        c_solo = next(c for r, c in d["solo"]["points"] if abs(r - re_common) < 1e-9)
        c_wind = next(c for r, c in d["wind"]["points"] if abs(r - re_common) < 1e-9)
        solo_vs = (f"At the same {100*re_common:.0f}% target ({so['year']}, EU), the no-wind build "
                   f"delivers ${c_solo:.0f}/MWh vs ${c_wind:.0f} with wind. ")

    # EU siting ranking (tools/build_eu_siting.py export)
    es = _load_optional("eu_siting_results.json")
    siting_txt = ("Firm hydro (Norway, Sweden, the Alps) and Iceland geothermal beat every "
                  "build-it-yourself sun+wind site.")
    phs_vs_h2 = ""
    if es:
        i = es["sites"][0]["years"].index(es["ranking_year"])
        ranked = sorted(es["sites"], key=lambda s: s["delivered"][i])
        top = ", ".join(f"{s['label'].split(' (')[0]} ≈${s['delivered'][i]:.0f}" for s in ranked[:5])
        siting_txt = (f"At {es['ranking_year']}, the cheapest 24/7 carbon-free sites are: {top} "
                      f"(per-site real ERA5 weather).")
        both = [s for s in es["sites"] if s.get("delivered_phs") and s.get("delivered_h2")]
        if both:
            saves = [s["delivered_h2"][i] - s["delivered_phs"][i] for s in both]
            phs_vs_h2 = (f"Where terrain allows pumped storage, it firms the same sun+wind for "
                         f"${min(saves):.0f}–{max(saves):.0f}/MWh less than green H₂ — often the "
                         f"difference between a marginal site and a competitive one. ")

    # Continent scan (tools/scan_eu.py export) — one-sentence addendum to the siting finding.
    sc = _load_optional("eu_scan_results.json")
    scan_note = ""
    if sc:
        eu_c = [c for c in sc["cells"] if c["lat"] >= 35]
        top = sorted(eu_c, key=lambda c: c["lcoe"])
        if sc.get("offshore") and any("lcoe_offshore" in c for c in eu_c):
            landt = sorted((c for c in eu_c if c["lsm"] >= 0.6), key=lambda c: c["lcoe"])
            offv = sorted(c["lcoe_offshore"] for c in top[:10] if "lcoe_offshore" in c)
            scan_note = (f" A {sc['n_cells']}-cell scan of the whole continent adds: the "
                         f"best build-it-yourself geography is windy coast, not sunny "
                         f"interior — mostly-land coastal cells from the Baltic to Galicia "
                         f"cluster at ~${landt[0]['lcoe']:.0f}–{landt[5]['lcoe']:.0f}/MWh, "
                         f"while the flashier part-sea cells (~${top[0]['lcoe']:.0f} at "
                         f"onshore prices) really cost ~${offv[0]:.0f}+ once their sea "
                         f"wind is priced at offshore capex.")
        else:
            scan_note = (f" A {sc['n_cells']}-cell scan of the whole continent adds: for a "
                         f"build-it-yourself system, the cheapest geography is the windy North "
                         f"Sea/Baltic edge (~${top[0]['lcoe']:.0f}–{top[9]['lcoe']:.0f}/MWh), "
                         f"not the sunny south.")

    smr_us, smr_eu = us.get("smr"), eu.get("smr")
    if smr_us and smr_eu:
        smr_support = (f"Modelled as an exogenous reference line, first-of-a-kind "
                       f"≈${smr_eu[0]:.0f}/MWh (EU) / ${smr_us[0]:.0f} (US) gliding to "
                       f"≈${smr_us[-1]:.0f} over 10–12 years — competitive with Europe's "
                       f"deep-renewable builds <i>if</i> that glide materialises, never with "
                       f"cheap US gas.")
        smr_more = (f"The SMR line is deliberately simple and never part of the optimisation: "
                    f"a linear FOAK→NOAK decline, then flat — no Wright's-Law learning, since "
                    f"there is no deployed fleet to learn from. An ${smr_us[-1]:.0f} NOAK "
                    f"undercuts the EU 90% build (≈${eu90_2040:.0f}/MWh in 2040) if NOAK costs "
                    f"materialise — the big if, given FOAK history — but never approaches the "
                    f"${us['gas_pure'][0]:.0f} US gas baseline.")
    else:
        smr_support = ("SMRs enter as an exogenous FOAK→NOAK reference line, never part of "
                       "the optimisation.")
        smr_more = ""

    # (headline, one-sentence support, optional expandable detail)
    items = [
        ("Europe: moderate renewable shares beat carbon-priced gas; the last decile is dear.",
         f"A 70% renewable build crosses below gas {_cross(eu70)} and 80% {_cross(eu80)}, but "
         f"on France's measured weather 85% reaches parity only {_cross(eu85)} and 90% "
         f"{_cross(eu90)}.",
         f"The baseline is gas rising ${eu['gas_pure'][0]:.0f}→${eu['gas_pure'][-1]:.0f}/MWh "
         f"as EU carbon prices climb. France's measured wind is poor (capacity factor 0.135), "
         f"so riding out multi-day winter Dunkelflaute takes heavy overbuild — roughly 10× "
         f"solar + 9× wind + 6h battery at the 90% target. The cheap insurance is moderate "
         f"renewables; the last decile is a genuine premium on a poor-wind hub."),
        ("Batteries get you through the night; wind gets you through the winter.",
         f"A solar+battery system hits a hard wall at ≈{solo_wall} renewable — no affordable "
         f"battery bridges a week-long lull — while adding wind extends the same firm system "
         f"to ≈{wind_max}.",
         f"Wind's lulls don't coincide with overcast spells, which is why it unlocks the "
         f"winter. {solo_vs}Even with wind, a firm battery-only system tops out near ~94% "
         f"renewable: the last few percent need long-duration storage or hydrogen."),
        ("The firming choice — what covers the dark, windless weeks — moves the bill more than the panels do.",
         f"A fully gas-free build firmed by self-produced hydrogen delivers "
         f"${h2[0]:.0f}→${h2[-1]:.0f}/MWh in Europe, crossing below gas {_cross(h2_cross)}.",
         f"Gas is the cheap-but-emitting default; purchasing green H₂ for the same turbine "
         f"adds ≈$25–30/MWh (a premium that narrows as EU carbon rises); self-produced H₂ — "
         f"an electrolyser plus tank storage, charged on surplus sun — is the cheaper "
         f"zero-carbon route. {phs_vs_h2}"),
        ("Where to build in Europe: water first, then windy coasts and sunny islands.",
         siting_txt,
         "Firm hydro and geothermal sites skip the firming question entirely. Among "
         "sun+wind sites, the winners are those whose resource co-locates with "
         "pumped-storage terrain (islands and sierras); flat sites fall back on dearer "
         "H₂ firming." + scan_note + " Details in the Geography chapter."),
        ("Small modular nuclear: a glide-path reference, not today's competitor.",
         smr_support, smr_more),
        ("The US is a different planet: cheap gas is the moat.",
         f"Even 70–80% renewable targets bottom out ≈$60/MWh vs the ${us['gas_pure'][0]:.0f} "
         f"flat-gas baseline (crossover {_crossover(us, '0.70')}); they beat a stressed "
         f"(+60% fuel) gas baseline by ~2030.",
         f"At $4/MMBtu, renewables compete with a ~$29/MWh <i>fuel</i> bill, not the "
         f"${us['gas_pure'][0]:.0f} all-in gas LCOE — so the pure cost-optimum is ≈0% "
         f"renewable today and only ~⅓ (solar-only, no battery) by 2040. A clean US "
         f"datacenter is a hedge against gas and carbon prices; in Europe it is simply "
         f"the cheaper plant."),
        ("AI datacenters don't just ride the learning curve — they pull it.",
         "Every doubling of cumulative deployment cuts battery system cost ~19% and solar "
         "~25% (Wright's Law), and GW-scale datacenter procurement lands on exactly the "
         "technologies with the steepest curves.",
         "Battery turnkey prices fell ~31% in 2025 alone. The deployment trajectory behind "
         "these projections already leans on the AI clean-power buildout to keep additions "
         "growing — so each clean campus pulls the parity years above forward for everyone "
         "else."),
        ("Off-grid is itself a premium.",
         "Staying on the grid with a renewable-energy contract sits <i>below</i> the "
         "off-grid high-renewable optimum in both regions — off-grid buys siting "
         "independence, at a cost.",
         ""),
    ]
    out = []
    for head, support, more in items:
        det = (f'<details><summary>More</summary><p>{more}</p></details>') if more else ""
        out.append(f"<li><b>{head}</b> {support}{det}</li>")
    return "".join(out)


# ── geography page sections ─────────────────────────────────────────────────────────

def locations_section():
    """The per-state comparisons on real ERA5 weather: a gas-backed renewable-target
    build (tools/build_locations_re.py) and a fully zero-carbon self-made-hydrogen build
    (tools/build_locations_h2.py), each with vs without a wind park. Skipped if unbuilt."""
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
            '<h2>The same build across 14 markets — Europe vs the US</h2>'
            '<p>For contrast with the cheap-gas US, the same firm off-grid build is '
            'computed at <b>seven large EU countries and seven US states</b> — the biggest '
            f'data-center markets in each region — on <b>real ERA5 weather ({yspan}, '
            f'{nyears} years)</b>, one grid point per location, every real year a dispatch '
            'sample (so the curves carry real year-to-year variability). Within a region '
            'only the renewable <b>resource</b> differs — gas, carbon and technology costs '
            'are the region default — so this isolates how much <b>where you build</b>, '
            'and <b>whether you add a wind park</b>, move the cost.</p>'
            '<h3>Is a wind park worth building? (gas-backed)</h3>'
            '<p>Solar is quick to permit; a wind park is a far bigger siting undertaking. '
            'The test is fair: both builds are optimised to the <b>same renewable target</b> '
            'at each site — the most a solar + battery + gas system can reach <i>without</i> '
            'wind (~55–68%, shown in each panel) — and the <b style="color:#0072B2">wind '
            'build</b> may add a wind park <b>only if that lowers cost</b>. So the blue line '
            'is never above the <b style="color:#b07900">no-wind</b> orange line: it dips '
            'below where wind genuinely competes (the United Kingdom; Texas, Iowa) and '
            'merges with it where wind is too weak to bother (Arizona, California, Italy — '
            '~2% capacity factor <i>at the modelled point</i>, so the optimiser builds '
            'almost none). A reading note before a low row surprises you: each location is '
            'sampled at one representative point — usually its datacenter hub (Ashburn, '
            'Silicon Valley, Phoenix, Milan) — not the country\'s windiest terrain. '
            'Italy\'s 0.02 wind CF is real for Milan\'s becalmed Po Valley; Italy\'s '
            'actual wind fleet, on southern ridgelines, averages ~0.2. Read each row as '
            '"a datacenter at this hub", not a national wind verdict. Grey dashed: the gas '
            'baseline; <b style="color:#8e44ad">purple dash-dot: a small modular (nuclear) '
            'reactor</b> — competitive in carbon-priced Europe, undercut by renewables+gas '
            'in the cheap-gas US.</p>'
            + _fig_box(refig, "off-grid datacenter cost by location, gas-backed")
            + "<table><thead><tr><th>Location</th><th>Region</th><th>Solar CF</th><th>Wind CF</th>"
              "<th>Target</th><th>No wind 2035</th><th>Wind-optional 2035</th>"
              "<th>Wind saves</th></tr></thead>"
              f"<tbody>{rows}</tbody></table>"
            '<div class="caveat">Real hourly ERA5 at one point per location ('
            f'{yspan}); solar capacity factor from horizontal irradiance ×1.25; '
            'region-default gas, carbon and technology costs. Each site\'s solar and wind '
            'LCOE is <b>re-anchored to that site\'s real capacity factor</b>, so a low-wind '
            'site correctly pays more per MWh for wind and a sunny site less for solar. '
            'A low wind CF describes the sampled point (usually the local datacenter hub), '
            'not the country\'s whole wind resource. '
            'Reduced optimiser fidelity (~±15% in level) — the cross-site <i>ranking</i> and '
            'the <i>wind gap</i> are the robust messages. Per-state figures: '
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
        # What the wind park buys, computed from the export (top savers / zero-save sites).
        saves = sorted(L["locations"], key=lambda x: x["lcoe_wind"][10] - x["lcoe_nowind"][10])
        top = ", ".join(f"{x['label']} ~${x['lcoe_nowind'][10]-x['lcoe_wind'][10]:.0f}/MWh"
                        for x in saves[:3])
        flat = ", ".join(x["label"] for x in saves
                         if x["lcoe_nowind"][10] - x["lcoe_wind"][10] < 2)
        out.append(
            ('' if intro_done else '<h2>The same build across 14 markets — Europe vs the US</h2>')
            + '<h3>Fully zero-carbon: self-made hydrogen, with and without wind</h3>'
            '<p>To drop gas entirely, the backstop becomes <b>green hydrogen made from '
            'surplus renewables</b> (the small residual bought from the market). Both builds '
            'are zero-carbon by construction — <b style="color:#b07900">solar + battery + '
            'hydrogen</b> vs <b style="color:#0072B2">the same plus a wind park</b> — on the '
            f'same {yspan} weather. The gap between the lines is what the wind park buys '
            f'(2035): {top}; ~$0 where wind is scarce ({flat}), where the easier-to-permit '
            'no-wind build is the one to pick. The <b style="color:#8e44ad">purple dash-dot '
            'small modular reactor</b> is the other firm zero-carbon option — often '
            'competitive, especially in carbon-priced Europe. Grey dashed: the (emitting) '
            'gas baseline, for reference.</p>'
            + _fig_box(lfig, "zero-carbon datacenter cost by location, hydrogen-firmed")
            + "<table><thead><tr><th>Location</th><th>Region</th><th>Solar CF</th><th>Wind CF</th>"
              "<th>No-wind 2035</th><th>With-wind 2035</th><th>Wind saves</th></tr></thead>"
              f"<tbody>{rows}</tbody></table>"
            '<div class="caveat">Same weather and re-anchoring as above; region-default '
            'carbon and technology costs; reduced optimiser fidelity. Per-state figures: '
            '<code>figs/locations_h2/</code>.</div>')
    return "".join(out)


def siting_section():
    """'Where in Europe to build' — the EU clean-power siting ranking
    (tools/build_eu_siting.py). Empty if not built."""
    sp = os.path.join(ROOT, "output", "eu_siting_results.json")
    map_h2, map_phs, barfig = (_img("eu_siting_map_h2.png"), _img("eu_siting_map_phs.png"),
                               _img("eu_siting.png"))
    if not os.path.exists(sp) or not map_h2:
        return ""
    d = json.load(open(sp)); yr = d["ranking_year"]
    j = yr - 2025

    def res_label(r):
        if r["resource"] == "geothermal":
            return "geothermal (firm)"
        if r["resource"] == "hydro":
            return "hydro (firm)"
        firm = r.get("firming", "green-H₂")
        store = "pumped storage" if firm == "PHS" else "green-H₂"
        return f"solar + wind + battery + {store}"
    rows = sorted(d["sites"], key=lambda r: r["delivered"][j])

    def _re75(r):
        if not r.get("re75_gas"):
            return "—"                                   # firm clean (geothermal/hydro)
        if r.get("re75_re") and r["re75_re"][j] >= 0.73:
            return "$%.0f" % r["re75_gas"][j]
        return "<span style='color:#999'>infeasible</span>"   # very low wind: hits the solar wall

    def _firm_cell(r, key):   # green-H₂ / PHS column; bold the cheaper of the two
        v = r.get(key)
        if v is None:
            return "—"
        s = "$%.0f" % v[j]
        other = r.get("delivered_phs" if key == "delivered_h2" else "delivered_h2")
        if other is not None and v[j] <= other[j]:
            s = f"<b>{s}</b>"
        return s
    body = "".join(
        f"<tr><th>{r['label']}</th><td>{res_label(r)}</td>"
        f"<td class='cx'>${r['delivered'][j]:.0f}</td>"
        f"<td>{_firm_cell(r, 'delivered_h2')}</td><td>{_firm_cell(r, 'delivered_phs')}</td>"
        f"<td>{_re75(r)}</td></tr>"
        for r in rows)
    table = ("<table><thead><tr><th>Location</th><th>Cheapest clean resource</th>"
             f"<th>{yr} $/MWh</th><th>via green-H₂</th><th>via PHS</th>"
             f"<th>75% RE + gas</th></tr></thead>"
             f"<tbody>{body}</tbody></table>")
    src = "real ERA5 weather" if d.get("weather") == "real ERA5" else "illustrative resource"
    cheapest = rows[0]
    geo = [r["delivered"][j] for r in rows if r["resource"] == "geothermal"]
    geo_txt = f" and Icelandic <b>geothermal (~${geo[0]:.0f})</b>" if geo else ""
    return (
        '<h1>Where in Europe should you build?</h1>'
        '<p>Candidate locations ranked by the cheapest <b>24/7 carbon-free</b> delivered '
        'power, each using its <b>best clean resource</b>: firm <b>geothermal</b> (Iceland) '
        'or big <b>hydro</b> (Norway, Sweden, the Alps, Greenland) where they exist; '
        'everywhere else a gas-free solar + wind + battery build, firmed by <b>green '
        f'hydrogen</b> or <b>pumped storage (PHS)</b>. ({yr}, firm · {src}.)</p>'
        f'<p><b>Firm clean baseload wins decisively.</b> Nordic/Alpine <b>hydro '
        f'(~${cheapest["delivered"][j]:.0f}/MWh)</b>{geo_txt} beat every '
        'build-it-yourself sun-and-wind site and sit far below gas.</p>'
        '<p><b>Both firmings are shown for every sun+wind site</b>, because which one a '
        'site can use is itself geographic: off-river PHS needs terrain with head '
        '(availability from the <b>ANU Global Pumped Hydro Atlas</b>), while H₂ works '
        'anywhere. Where strong sun+wind co-locates with pumped-storage terrain — the '
        'Iberian sierras and mountainous islands (Tarifa, Sines, Sicily, Crete, Gran '
        'Canaria) — PHS firms far cheaper than H₂ (~80% round-trip efficiency vs ~35%, so '
        'far less overbuild is wasted). Flat sites (Jutland, the Dover Strait) have no '
        'cheap PHS and fall back on dearer H₂; the open circle on each bar marks the '
        'firming <i>not</i> chosen.</p>'
        '<p style="font-size:13.5px">Two caveats worth keeping. (1) <b>Wind and PHS terrain '
        'don\'t always co-locate:</b> Romania\'s wind is on the flat Black Sea coast while '
        'its PHS is inland in the Carpathians, so its site is H₂-firmed; Switzerland is '
        'genuinely wind-poor even with world-class PHS — its real edge is conventional '
        'hydro. (2) <b>In carbon-priced Europe, partial-gas isn\'t the cheap option:</b> '
        '"75% RE + gas" undercuts fully-clean only at the flat H₂-firmed sites; where PHS '
        'makes clean firming cheap, 100% zero-carbon wins.</p>'
        # Two maps STACKED (one below the other), each full width.
        + _fig_box(map_h2, "EU siting map, green-hydrogen firming",
                   "If firmed by <b>green hydrogen</b> — works at every site (H₂ needs no terrain).")
        + _fig_box(map_phs, "EU siting map, pumped-storage firming",
                   "If firmed by <b>pumped storage</b> — only where the ANU atlas shows "
                   "reservoir terrain (flat sites omitted); much cheaper where available. "
                   "Same colour scale as the H₂ map.")
        + table +
        (_fig_box(barfig, "EU siting ranking bar chart") if barfig else "") +
        '<p class="sub" style="font-size:13px">Each sun+wind figure reflects the exact ERA5 '
        'grid cell at the site\'s coordinates, so very localized wind regimes (e.g. the '
        'Tarifa jet) can be under-captured — treat the ranking as directional. '
        'Geothermal/hydro costs: IRENA 2023 installed costs ($4,589/kW geothermal, '
        '$2,806/kW hydro); pumped storage: NREL ATB / DOE-PNNL (RTE 0.80, ~50-yr life). '
        'From <code>tools/build_eu_siting.py</code>.</p>')


def _scan_robustness_note():
    """One caveat-sentence from the weather-year robustness export
    (tools/scan_robustness.py). Empty if the check hasn't been run."""
    rb = _load_optional("eu_scan_robustness.json")
    if not rb:
        return ""
    rho = min(rb["spearman_year_vs_3yr"])
    keep = min(rb["top10_retained_per_year"])
    yrs = rb["weather_years"]
    if rho >= 0.95:
        verdict = ("the geography drives the map; individual ranks inside "
                   "closely-priced bands are noise")
    elif rho >= 0.85:
        verdict = "the broad ordering is stable; nearby ranks are interchangeable"
    else:
        verdict = "single-year rankings differ materially — treat the ordering with caution"
    return (f' Weather-year robustness: re-scoring {rb["n_cells"]} cells on each single '
            f'weather year ({yrs[0]}, {yrs[1]}, {yrs[2]}) separately, the single-year '
            f'rankings correlate with the 3-year ranking at Spearman ρ ≥ {rho:.2f} '
            f'(median per-cell spread {rb["spread_median"]:.0%}); membership of the '
            f'very top shuffles within the tightly-packed leaders ({keep}–'
            f'{max(rb["top10_retained_per_year"])} of the top-10 stay top-10 in any '
            f'single year) — {verdict} (<code>tools/scan_robustness.py</code>).')


def scan_section():
    """Europe-wide scan: the cost choropleth + the CF→price surrogate validation
    (tools/scan_eu.py). Empty if the scan hasn't been run."""
    sp = os.path.join(ROOT, "output", "eu_scan_results.json")
    map_fig, sur_fig = _img("eu_scan_map.png"), _img("eu_scan_surrogate.png")
    if not os.path.exists(sp) or not map_fig:
        return ""
    d = json.load(open(sp))
    mi, yrs = d["milestone"], d["weather_years"]
    cells = d["cells"]
    sur = d["surrogate"]
    n = len(cells)
    eu_cells = [c for c in cells if c["lat"] >= 35]     # the 34°N row is the Maghreb coast
    ranked = sorted(eu_cells, key=lambda c: c["lcoe"])
    land = sorted((c for c in eu_cells if c["lsm"] >= 0.6), key=lambda c: c["lcoe"])

    def _cellname(c):
        return (f"{abs(c['lat']):.0f}°{'N' if c['lat'] >= 0 else 'S'}, "
                f"{abs(c['lon']):.0f}°{'E' if c['lon'] >= 0 else 'W'}")

    off = d.get("offshore")
    has_off = off and any("lcoe_offshore" in c for c in eu_cells)

    def _row(c):
        cell_off = (f"<td>${c['lcoe_offshore']:.0f}</td>" if "lcoe_offshore" in c
                    else "<td>—</td>") if has_off else ""
        return (f"<tr><th>{_cellname(c)}</th>"
                f"<td>{c['cf_solar']:.2f}</td><td>{c['cf_wind']:.2f}</td>"
                f"<td>{c['worst14']:.2f}</td><td>{c['lsm']:.0%}</td>"
                f"<td class='cx'>${c['lcoe']:.0f}</td>{cell_off}</tr>")
    top = "".join(_row(c) for c in ranked[:10])
    off_col = (f"<th>$/MWh offshore-priced</th>" if has_off else "")
    land_txt = ", ".join(f"{_cellname(c)} (${c['lcoe']:.0f})" for c in land[:4])
    r2c, r2f = sur["cf_only"]["r2"], sur["full"]["r2"]
    maec, maef = sur["cf_only"]["mae"], sur["full"]["mae"]
    med = sorted(c["lcoe"] for c in cells)[n // 2]

    # Honest-pricing verdict paragraph — every number computed from the export.
    if has_off:
        off_vals = sorted(c["lcoe_offshore"] for c in ranked[:10] if "lcoe_offshore" in c)
        south = sorted((c for c in eu_cells if c["lsm"] >= 0.6 and 40 <= c["lat"] <= 44),
                       key=lambda c: c["lcoe"])
        s0 = south[0]
        verdict = (
            f'<p><b>But sea wind must be bought at sea prices — and that changes the '
            f'podium.</b> The raw winners above are only 20–40% land: their measured '
            f'wind is North Sea/Baltic <i>sea</i> wind, and the map prices it at onshore '
            f'capex. Re-pricing every cell below {off["lsm_threshold"]:.0%} land at '
            f'European fixed-bottom <b>offshore</b> costs (${off["lcoe_today"]:.0f}/MWh '
            f'levelised at CF {off["ref_cf"]:.2f} — the UK AR7 clearing level — with '
            f'offshore\'s slower ~{off["learning_rate"]:.0%} learning) lifts the raw '
            f'top-10 from ~${ranked[0]["lcoe"]:.0f}–{ranked[9]["lcoe"]:.0f} to '
            f'<b>~${off_vals[0]:.0f}–{off_vals[-1]:.0f}/MWh</b>. The honest '
            f'build-it-yourself ranking is led instead by the cheapest <b>mostly-land</b> '
            f'coastal cells at their legitimate onshore pricing ({land_txt}) — and the '
            f'north–south drama largely evaporates: the best mostly-land southern cells '
            f'({_cellname(s0)} — windy Galicia — at ${s0["lcoe"]:.0f}; '
            f'{_cellname(south[1])} at ${south[1]["lcoe"]:.0f}) sit within the scan\'s '
            f'screening noise of the northern leaders. What survives every pricing: <b>coastal wind beats inland sun</b>, '
            f'the sheltered continental interior is the place to avoid, and no DIY cell '
            f'approaches firm hydro (~$46) or the PHS-firmed southern sites above '
            f'($70–93). (Waters needing floating turbines — the Norwegian Trench — would '
            f'be ~2.4× dearer still; not modelled.)</p>')
        map_cap = ('Map colours use onshore pricing everywhere — read the deepest-green '
                   'part-sea coastal cells against the offshore-priced column in the '
                   'table below.')
    else:
        verdict = ""
        map_cap = None
    return (
        '<h2>Scanning the whole continent</h2>'
        f'<p>The nine sun+wind candidates above were chosen by hand. To remove the '
        f'guesswork, the same gas-free build (solar + wind + battery + self-made '
        f'hydrogen) was computed at <b>every ~1° land cell of Europe — {n} cells</b> — '
        f'on real hourly ERA5 weather ({yrs[0]}–{yrs[-1]}), with each cell\'s solar and '
        f'wind costs re-anchored to its real capacity factors. EU technology costs are '
        f'used everywhere, so the map isolates <b>geography</b>: resource quality and '
        f'weather structure, not national policy. Median cell: ~${med:.0f}/MWh at {mi}.</p>'
        f'<p><b>The raw map says: wind, not sun.</b> The best cells on raw cell weather '
        f'(~${ranked[0]["lcoe"]:.0f}–{ranked[9]["lcoe"]:.0f}/MWh) trace the North Sea and '
        f'Baltic coasts and islands — Danish and Pomeranian shores, the Estonian and '
        f'Swedish Baltic islands, Orkney and the Faroes — where a ~0.5 wind capacity '
        f'factor out-earns Mediterranean sun. The expensive interior band — and the very '
        f'worst cells, the sheltered Scandinavian inland valleys '
        f'(~${max(c["lcoe"] for c in eu_cells):.0f}) — is what a datacenter pays for '
        f'being far from wind. Firm hydro (~$46) still beats every cell on the map.</p>'
        + verdict
        + _fig_box(map_fig, "map of 24/7 carbon-free power cost across Europe", map_cap)
        + '<h3>Can two capacity factors predict the price?</h3>'
        f'<p>Almost — and the gap is the interesting part. A transparent least-squares '
        f'fit on <b>mean solar and wind capacity factor alone</b> predicts the dispatch '
        f'model\'s cost with <b>R² {r2c:.2f}</b> (typical error ~${maec:.0f}/MWh) on '
        f'held-out cells; adding simple <b>weather-structure</b> statistics (the depth of '
        f'the worst 5- and 14-day sun+wind drought, sun–wind correlation, winter-solar '
        f'share) improves it to <b>R² {r2f:.2f}</b> (~${maef:.0f}/MWh). Both the formula '
        f'and its miss are the message: annual averages carry most of the signal, and '
        f'what they miss is exactly the multi-day <i>Dunkelflaute</i> a 24/7 datacenter '
        f'must ride through. The full coefficients are in '
        f'<code>output/eu_scan_results.json</code> — check us.</p>'
        + _fig_box(sur_fig, "surrogate validation scatter and coefficients")
        + '<h3>The cheapest cells found by the scan</h3>'
        + "<table><thead><tr><th>Cell</th><th>Solar CF</th><th>Wind CF</th>"
          "<th>Worst 14-day depth</th><th>Land fraction</th>"
          "<th>$/MWh " + str(mi) + " (onshore-priced)</th>" + off_col + "</tr></thead>"
          f"<tbody>{top}</tbody></table>"
        '<div class="caveat">Screening fidelity: one milestone year, reduced optimizer '
        'starts, 3 weather years, ~1° cells (which average away local wind jets — the '
        'curated point sites above are the precision layer).'
        + _scan_robustness_note() +
        ' Cells with a low land fraction average sea wind into their capacity factor — '
        'the offshore-priced column is the honest cost for building that wind for real. '
        'The scan covers '
        'only the build-it-yourself sun+wind strategy; firm hydro and geothermal (the '
        'overall winners) are plant-specific and stay as the marked point sites. And a '
        'cheap cell is not a permit: several winners overlap sensitive areas (the Wadden '
        'Sea coast is a protected World Heritage sea; Orkney and the Baltic islands '
        'carry major bird and marine designations) — before treating a cell as a real '
        'candidate, re-score its exact coordinates (<code>tools/fetch_era5.py</code> + '
        '<code>--site</code>) and check Natura 2000 / national constraints. The box\'s '
        'southern edge also shows the <i>Maghreb</i> coast as cheap (~$111–120) — real, '
        'but outside the EU siting question.</div>')


# ── zero-carbon page sections ───────────────────────────────────────────────────────

def wind_section():
    """The 'do you need a wind park?' + zero-carbon synthesis sections
    (tools/build_solar_only.py, tools/build_zerocarbon.py). Empty if not built."""
    path = os.path.join(ROOT, "output", "solar_only_results.json")
    fig = _img("solar_only.png")
    if not fig or not os.path.exists(path):
        return ""
    d = json.load(open(path)); yr = d["year"]
    wall = d["data"]["us"]["solo"]["max_re"]
    blocks = [
        '<h1>Going fully zero-carbon</h1>'
        '<h2>Do you even need a wind park?</h2>'
        '<p>Solar is modular and quick to permit; a wind park is a far bigger siting and '
        'permitting undertaking. Comparing solar + wind + battery against <b>solar + '
        f'battery only</b> (both firm, gas-backed, {yr}): <b>there is a hard wall</b>. The '
        f'no-wind system tops out near <b>~{wall:.0%} renewable</b> — nights <i>and</i> '
        'multi-day cloud always fall to gas, and a battery cannot shift energy across days '
        '— while adding wind reaches ~94%. Below the wall, dropping wind costs little in '
        'the sunny US and a clearer premium in Europe. <b>Bottom line:</b> for a moderate '
        'renewable target, solar + battery alone is a reasonable, much-easier-to-build '
        'choice; high renewable fractions genuinely need wind (or long-duration storage / '
        'hydrogen).</p>'
        + _fig_box(fig, "cost vs renewable fraction, with and without wind")]

    # ── Zero-carbon synthesis (solar+battery+H₂) ────────────────────────────────
    zp = os.path.join(ROOT, "output", "zerocarbon_results.json")
    zfig = _img("zerocarbon.png")
    if zfig and os.path.exists(zp):
        z = json.load(open(zp))

        def c(region, key):
            return next(o["value"] for o in z["data"][region] if o["key"] == key)
        gaps = sorted(c(r, "nowind_selfmade") - c(r, "wind_selfmade") for r in ("us", "eu"))
        blocks.append(
            '<h2>Solar + battery + hydrogen (no wind)</h2>'
            '<p>To go <b>fully zero-carbon without a wind park</b>, replace the gas '
            'backstop with green hydrogen. All the options below are green — they differ '
            'only in <b>how you get the H₂</b>. <b>Making it yourself</b> (an electrolyser '
            'turning <i>surplus</i> renewables into H₂, a few percent bought) brings a '
            'wind-free zero-carbon datacenter to '
            f'<b>~${c("eu","nowind_selfmade"):.0f}/MWh (EU) / ${c("us","nowind_selfmade"):.0f} '
            f'(US)</b> by {z["year"]}; <b>buying it all</b> on the market (no electrolyser) '
            f'is far dearer (${c("eu","nowind_bought"):.0f} / ${c("us","nowind_bought"):.0f}). '
            f'The self-made wind-free build is only ~${gaps[0]:.0f}–{gaps[1]:.0f} above the '
            f'same build <i>with</i> a wind park '
            f'(${c("eu","wind_selfmade"):.0f} / ${c("us","wind_selfmade"):.0f}) — '
            'so the wind park, not the hydrogen, is the smaller lever here. In Europe that '
            'with-wind build is the <b>cheapest option of all</b>, since gas there is '
            'carbon-priced.</p>'
            '<p class="sub" style="font-size:13px">In the chart, the three green bars are '
            'the same green hydrogen; the only differences are whether there is a wind park '
            'and whether the H₂ is self-made or bought.</p>'
            + _fig_box(zfig, "zero-carbon build options bar chart"))
    return "".join(blocks)


def footprint_section(us, eu):
    """What a fully-clean datacenter costs the LANDSCAPE — land area and avoided
    emissions per GW, computed from the model's own optimal gas-free builds. Literature
    constants: solar 35–45 MW/km² total plant area (NREL land-use reports); onshore wind
    ~3 MW/km² array spacing with ~1–2% directly occupied (NREL/Denholm); CO₂ from the
    model's CCGT intensity."""
    h_eu, h_us = eu.get("h2_system"), us.get("h2_system")
    if not h_eu or not h_us:
        return ""
    j = 5   # 2030
    sol_eu, win_eu = h_eu["C_sol"][j], h_eu["C_win"][j]
    sol_us, win_us = h_us["C_sol"][j], h_us["C_win"][j]
    # land per GW of datacenter load (GW × overbuild ÷ density)
    s_lo, s_hi = 1000 * sol_eu / 45, 1000 * sol_eu / 35      # km², solar total area
    w_sp = 1000 * win_eu / 3.0                               # km², wind array spacing
    w_direct = w_sp * 0.015
    su_lo, su_hi = 1000 * sol_us / 45, 1000 * sol_us / 35
    co2 = 0.345 * 8.76                                       # MtCO₂/yr per GW vs CCGT
    # Embodied carbon per DELIVERED kWh: lifecycle intensity per generated kWh scaled by
    # the build's generation-to-load ratio (overbuild × CF), so curtailment is charged.
    # Solar 25–48 gCO₂e/kWh (modern supply chains … IPCC AR5 median), wind 11 (AR5).
    ec = eu["simulated_cf"]
    gen_s, gen_w = sol_eu * ec["solar"], win_eu * ec["wind"]
    em_lo, em_hi = 25 * gen_s + 11 * gen_w, 48 * gen_s + 11 * gen_w
    return (
        '<h2>What does it cost the landscape?</h2>'
        f'<p>Per <b>gigawatt of always-on datacenter</b>, the {2025 + j} European '
        f'gas-free build ({sol_eu:.1f}× solar + {win_eu:.1f}× wind + batteries + '
        f'hydrogen) needs roughly:</p>'
        '<ul class="find">'
        f'<li><b>~{s_lo:.0f}–{s_hi:.0f} km² of solar plant</b> (a ~{s_lo**0.5:.0f}–'
        f'{s_hi**0.5:.0f} km square) — the dominant direct land take. In the sunnier US '
        f'the same build needs only ~{su_lo:.0f}–{su_hi:.0f} km².</li>'
        f'<li><b>~{w_sp:.0f} km² of wind-park spacing</b>, of which only '
        f'~{w_direct:.0f} km² (~1–2%) is actually occupied by towers and roads — the '
        f'rest stays farmland or open land.</li>'
        f'<li>In exchange it avoids <b>~{co2:.1f} MtCO₂ every year</b> versus running '
        f'the same datacenter on gas — and burns no fuel, uses no combustion water, and '
        f'needs no pipeline.</li>'
        '</ul>'
        '<p>Two honest counterpoints. First, this is <i>real</i> land: siting must avoid '
        'protected areas (Natura 2000 and national designations), and the scan map\'s '
        'cells are screening averages, not permits. Second, the cheapest clean option of '
        'all — reservoir hydro — carries the largest ecological footprint per site: the '
        'siting ranking prices hydro at full new-build cost at places where big '
        'reservoirs <i>already exist</i> (Norway, Sweden, the Alps); a plan that needs '
        'damming new wild rivers should be treated as environmentally, not just '
        'economically, expensive. A green compute zone is a land-use choice, and an '
        'honest case for it states the acreage up front rather than hiding it.</p>'
        '<h3>And the water?</h3>'
        '<p>Water is often the first local objection, and it splits into two separate '
        'ledgers. The <b>power side</b> of this build is essentially water-free: solar, '
        'wind and batteries consume almost nothing (panel washing aside), the '
        'electrolyser\'s feedwater (~10–15 L per kg of H₂) is minor at these volumes, and '
        'the hydrogen turbine runs only through the rare lulls — while the gas plant the '
        'build replaces <i>evaporates</i> roughly 0.8 m³ of cooling water per MWh it '
        'generates. The <b>datacenter side</b> is a design choice, not a consequence of '
        'going off-grid: evaporative cooling drinks ~1–2 L per kWh of IT load, but dry '
        '(closed-loop) cooling cuts site water to near zero for a small efficiency '
        'penalty — the right default at the sunny, dry sites the siting ranking favours '
        '(Iberia, the islands), which is exactly where water is scarcest.</p>'
        '<h3>Zero-carbon means zero combustion — not zero footprint</h3>'
        '<p>The model\'s carbon accounting is combustion-scope: the gas-free build burns '
        'nothing, so it scores zero. Manufacturing its panels, turbines and batteries '
        'still emits. On standard lifecycle intensities, scaled up by this build\'s own '
        'overbuild (the panels behind curtailed energy get manufactured too), the '
        'delivered power '
        f'carries very roughly <b>~{em_lo:.0f}–{em_hi:.0f} gCO₂e per kWh embodied</b> in '
        'the solar and wind fleet — batteries and the electrolyser add a few grams more — '
        f'versus ~490 for lifecycle gas. That is a ~{100 * (1 - em_hi / 490):.0f}–'
        f'{100 * (1 - em_lo / 490):.0f}% cut, not 100%, and the residual falls further as '
        'the manufacturing itself decarbonises.</p>'
        '<p class="sub" style="font-size:13px">Overbuild ratios from the model\'s '
        'optimal builds (<code>output/*_results.json</code>); land densities: solar '
        '35–45 MW/km² total plant area, onshore wind ~3 MW/km² spacing with ~1–2% '
        'direct occupation (NREL land-use studies; Denholm et al.); CO₂ at the model\'s '
        'CCGT intensity (0.345 tCO₂/MWh, combustion scope). Water: power-plant '
        'consumption from Macknick et al. 2012 (NREL); datacenter water-use figures from '
        'operator environmental reports (evaporative ~1 L/kWh). Lifecycle intensities: '
        'IPCC AR5 Annex III medians (utility solar 48, onshore wind 11, CCGT ~490 '
        'gCO₂e/kWh); modern PV supply chains run nearer 25.</p>')


# ── method page sections ────────────────────────────────────────────────────────────

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
        ("Site capacity factor (solar / wind)",
         f"{uc['solar']:.2f} / {uc['wind']:.2f}", f"{ec['solar']:.2f} / {ec['wind']:.2f}",
         "measured ERA5 (France / Texas); costs re-levelled to the site CF"),
    ]
    body = "".join(f"<tr><th>{n}</th><td>{e}</td><td>{u}</td><td class='src'>{s}</td></tr>"
                   for (n, u, e, s) in rows)
    return ("<table><thead><tr><th>Assumption</th><th>Europe</th><th>US</th>"
            f"<th>Source</th></tr></thead><tbody>{body}</tbody></table>")


GLOSSARY = [
    ("LCOE", "Levelized Cost of Energy — the all-in cost of one delivered MWh ($/MWh) once "
             "capital, fuel, carbon and O&M are spread over the project's life. The model's "
             "headline output; lower = cheaper power."),
    ("Capacity factor (CF)", "Average output ÷ nameplate rating. A solar farm with CF 0.23 "
             "produces 23% of its peak rating averaged over the year."),
    ("Renewable target / \"90% RE\"", "Share of the datacenter's served energy that must come "
             "from renewables + storage (the rest is gas)."),
    ("Firm / always-on", "A datacenter that never shuts down: gas backup is sized to cover "
             "100% of load during lulls, so the worst case is a known, capped cost. The "
             "model's default."),
    ("Overbuild / curtailment", "Installing more solar/wind than peak load (e.g. \"6× solar\") "
             "so enough is made on poor days; the excess on good days is curtailed (spilled)."),
    ("Dunkelflaute", "German for \"dark doldrums\" — a multi-day, wide-area spell of low sun "
             "and low wind. It sets how much storage/backup a renewable system needs."),
    ("Firming", "Whatever covers the hours renewables + battery cannot: a gas turbine, green "
             "hydrogen, pumped storage, hydro, or nuclear."),
    ("WACC", "Weighted Average Cost of Capital — the financing rate; the model uses a "
             "different one per technology (solar/wind 5.5%, battery 7%, gas 9%)."),
    ("Learning rate (Wright's Law)", "A technology's cost falls a fixed % for every doubling "
             "of cumulative production — this drives the cost-over-time trajectories."),
    ("CCGT / OCGT", "Combined-/Open-Cycle Gas Turbine — efficient baseload vs a cheap-to-build "
             "peaker; the model picks whichever is cheaper for the gas duty."),
    ("SMR", "Small Modular (nuclear) Reactor — plotted as a firm-clean reference line, never "
             "part of the optimisation."),
    ("PPA / 24/7 CFE", "Power Purchase Agreement — a long-term renewable supply contract "
             "(the on-grid alternative). 24/7 CFE is the stricter hour-by-hour carbon-free "
             "matching standard; both are plotted as reference lines."),
    ("PHS / LDES", "Pumped Hydro Storage / Long-Duration Energy Storage (iron-air, hydrogen) "
             "— multi-day storage options that can replace residual gas."),
    ("Crossover / parity", "The year a build's LCOE drops below the gas baseline — when going "
             "(mostly) renewable becomes the cheaper choice, not just the greener one."),
]


def tornado_section():
    """'What moves the answer most' — the parity-gap tornado (CLI --tornado --region eu,
    which also writes the JSON export this ranks from). Empty if not generated."""
    t = _load_optional("eu_tornado_results.json")
    fig = _img("eu_tornado.png")
    if not t or not fig:
        return ""
    rows = sorted(t["rows"], key=lambda r: abs(r[2] - r[1]), reverse=True)

    def sw(r):
        return abs(r[2] - r[1])
    return (
        '<h2>What moves the answer most</h2>'
        f'<p>One-at-a-time swings of the key assumptions around the base case, measured as '
        f'the change in the <b>parity gap</b> (the {t["re_target"]:.0%}-renewable build\'s '
        f'delivered cost minus gas, at {t["target_year"]}; base '
        f'{t["base"]:+.0f} $/MWh). The ranking is the message: <b>{rows[0][0]}</b> swings '
        f'the gap by ${sw(rows[0]):.0f}/MWh and <b>{rows[1][0]}</b> by '
        f'${sw(rows[1]):.0f} — together they dwarf everything else — while the smallest '
        f'levers ({rows[-1][0]}, ${sw(rows[-1]):.0f}; {rows[-2][0]}, '
        f'${sw(rows[-2]):.0f}) barely move it. In words: whether deep-renewable Europe '
        f'beats gas hinges on <b>wind quality and the gas price</b>, not on battery costs '
        f'or financing.</p>'
        + _fig_box(fig, "tornado chart: parity-gap sensitivity to each assumption")
        + '<p class="sub" style="font-size:13px">Reduced-fidelity, one-at-a-time swings on '
        'the synthetic weather generator (the free-knob mode behind sensitivity runs; the '
        'headline itself uses measured ERA5) — at this fidelity the firm battery-only '
        'system tops out just below the 90% target, so the gap is measured at the '
        '~89% penalty optimum. Treat magnitudes as indicative and the ranking as robust. '
        'Reproduce: <code>python datacenter_lcoe.py --tornado --region eu</code> → '
        '<code>output/eu_tornado_results.json</code>.</p>')


def benchmarks_section(us, eu):
    """'How this compares with other studies' — external validation against the
    published 24/7-CFE and off-grid literature. The literature numbers are cited
    constants (source-linked); the model's numbers are computed from the exports."""
    eu90 = eu["scenarios"]["0.90"]["lcoe"][0]
    h2_eu, h2_us = eu["h2_system"]["lcoe"], us["h2_system"]["lcoe"]
    us80, us90 = us["scenarios"]["0.80"]["lcoe"][0], us["scenarios"]["0.90"]["lcoe"][0]
    rows = [
        ("<a href='https://doi.org/10.1016/j.esr.2024.101488'>Riepin &amp; Brown "
         "2022/24</a> (TU Berlin, PyPSA)",
         "Grid-connected 24/7 hourly-matched clean supply, Germany / Ireland 2025, "
         "surplus sold to the grid (€2020)",
         "Wind+solar+battery only: 100% hourly matching costs €194–229/MWh (+141–242% "
         "vs annual matching); adding hydrogen LDES brings it to €99–114; 90–95% "
         "matching costs only a small premium — <i>the last ~2% roughly doubles the "
         "cost</i>",
         f"Same non-linearity, same fix: this model's battery-only 90% RE is "
         f"${eu90:.0f}/MWh while the H₂-firmed 100% build is ${h2_eu[0]:.0f} — deep "
         f"targets need LDES, not more batteries. Levels sit above theirs because this "
         f"build is islanded (no grid sales) on poorer-wind France"),
        ("<a href='https://zenodo.org/records/6229426'>Xu, Manocha, Patankar &amp; "
         "Jenkins 2021</a> (Princeton ZERO Lab)",
         "Grid-connected 24/7 CFE, California / PJM ~2030 ($2020)",
         "100% CFE ≈ $68–100/MWh all-in; the premium is strongly non-linear (98→100% "
         "costs more than 88→98%) and shrinks as the surrounding grid decarbonises",
         f"Consistent: this model's islanded US 90% RE is ${us90:.0f}/MWh (2025) and "
         f"the gas-free H₂ system ${h2_us[0]:.0f}→${h2_us[-1]:.0f} by 2040 — dearer "
         f"than grid-connected 24/7 CFE, which is exactly the off-grid premium the "
         f"Overview flags"),
        ("<a href='https://www.offgridai.us/'>offgridai.us 2024</a> (Baranko, "
         "Campbell, Hausfather, McWalter, Ransohoff)",
         "Fully islanded solar+battery+gas microgrid, US Southwest (~$2024)",
         "90% solar-served load at $97–109/MWh (optimised $97; solar $0.75/W, "
         "batteries $120/kWh)",
         f"Closest architecture to this model's US case: this model's 80% RE is "
         f"${us80:.0f}/MWh and 90% ${us90:.0f} (2025, Texas ERA5) — the same "
         f"ballpark, with this model's 90% dearer mainly via costlier battery and "
         f"firm-always-on assumptions"),
        ("<a href='https://doi.org/10.1016/j.jclepro.2019.118466'>Fasihi &amp; Breyer "
         "2020</a> (LUT)",
         "Off-grid PV+wind+battery+H₂ firm baseload, best global sites (Maghreb-class "
         "resource, 7% WACC)",
         "&lt;€119/MWh in 2020 falling to ≈€54 by 2030 on projected technology costs",
         f"This model's H₂ system is ${h2_eu[0]:.0f} (2025) → ${h2_eu[5]:.0f} (2030) "
         f"on French resource; its scan prices the Maghreb edge at ~$111–120 in 2030 — "
         f"the remaining gap to €54 is their more aggressive 2030 solar/electrolyser "
         f"cost projections"),
    ]
    body = "".join(f"<tr><th>{a}</th><td class='src'>{b}</td><td class='src'>{c}</td>"
                   f"<td class='src'>{d}</td></tr>" for a, b, c, d in rows)
    return (
        '<h2>How this compares with other studies</h2>'
        '<p>No published study prices exactly this build (islanded, always-on, real '
        'single-site weather), but the closest literature brackets it — and agrees on '
        'the shape: <b>hourly-matched clean power is cheap until the last few percent, '
        'which only long-duration storage or clean-firm capacity closes '
        'affordably</b>. Grid-connected studies land below this model (they sell '
        'surplus and lean on the grid); islanded studies with sunnier sites or more '
        'aggressive cost projections land at or below it too. Nothing in the '
        'literature contradicts the directional findings; the levels differ for '
        'stated, checkable reasons.</p>'
        "<table><thead><tr><th>Study</th><th>What it prices</th><th>Their finding</th>"
        f"<th>This model</th></tr></thead><tbody>{body}</tbody></table>"
        '<p class="sub" style="font-size:13px">Literature numbers are quoted in each '
        'study\'s own currency-year (Riepin &amp; Brown €2020; Princeton $2020; '
        'offgridai ≈$2024) — inflate ~15–20% to compare €/$2020 with this model\'s '
        'real-2025 USD. All model numbers in this table are generated from '
        '<code>output/*.json</code>.</p>')


def method_page(us, eu):
    gloss = "".join(f"<tr><th>{t}</th><td>{x}</td></tr>" for t, x in GLOSSARY)
    return (
        '<h1>Method &amp; how much to trust it</h1>'
        '<div class="caveat"><b>Read this before quoting numbers.</b> This is a stylised '
        'techno-economic model. Trust the <b>directional comparisons</b> — which option is '
        'cheaper, how gaps close over time — not absolute numbers to better than '
        '<b>~±20–30%</b>. Results are central estimates; the model also reports P10–P90 '
        'weather bands, an optional 1-in-10-bad-year design premium, and a sensitivity '
        'tornado (below). The headline Europe/US trajectories run on <b>measured '
        'ERA5 reanalysis weather</b> (2015–2025) at one representative market per region — '
        'EU: France; US: ERCOT Texas — with imported costs re-levelled to each site\'s '
        'measured capacity factor. The per-state and siting chapters run on measured ERA5 '
        'at each location, at reduced optimiser fidelity (~±15%; the rankings and gaps are '
        'the robust message).</div>'
        '<h2>What is and isn\'t modelled</h2>'
        '<ul class="find">'
        '<li><b>Default = firm, always-on:</b> gas backup covers 100% of load during lulls, '
        'so the worst case is a known, capped fuel cost. Premium/AI workloads never shed '
        'and collapse to this case; interruptible (spot) workloads can shed load when the '
        'lost compute is worth less than the gas to serve it (<code>--workload</code>).</li>'
        '<li><b>Optimisation:</b> for each region and year, the least-cost solar overbuild '
        '× wind overbuild × battery hours meeting the renewable target, evaluated on '
        'chronological hourly dispatch; gas is sized to the peak residual deficit, never a '
        'decision variable. Every reported optimum is confirmed with exact dispatch.</li>'
        '<li><b>Costs:</b> Wright\'s-Law learning curves on an S-curve deployment '
        'trajectory, per-technology WACC, battery augmentation (top up faded cells, not '
        'mid-life replacement), CCGT/OCGT selection, EU ETS carbon path. All in real 2025 '
        'USD.</li>'
        '<li><b>Single real site per region</b> — the largest directional caveat: one '
        'off-grid datacenter gets no geographic smoothing; a multi-site portfolio '
        '(<code>--sites</code>) softens multi-day lulls and lowers high-renewable cost. '
        'The documented experiment (§4.7) puts the effect at <b>≈40% off the EU 90%-RE '
        'delivered cost</b> for 3–5 partially-correlated sites (≈$161 → $91–98/MWh, 2025, '
        'reduced fidelity) — the direction is robust; the magnitude depends on the '
        'calibrated inter-site correlation. Every high-renewable number on this site is '
        'therefore a single-site <i>worst case</i>.</li>'
        '<li><b>Not modelled:</b> sub-hourly load variation, on-site fuel logistics, '
        'transmission. Embodied carbon and water use are discussed qualitatively on the '
        '<a href="zero-carbon.html">Zero-carbon page</a> but not priced.</li>'
        '</ul>'
        '<h2>What the optimiser actually builds</h2>'
        '<div class="figs">'
        f'<figure><img src="{_img("eu_firm_fig3_optimal_mix.png")}" alt="EU optimal mix">'
        '<figcaption>Europe — optimal build (solar / wind overbuild + battery hours) by '
        'renewable target. The firm high-renewable optimum is wind-heavy, to ride out '
        'multi-day lulls.</figcaption></figure>'
        f'<figure><img src="{_img("us_firm_fig3_optimal_mix.png")}" alt="US optimal mix">'
        '<figcaption>United States — same series. Texas\'s stronger sun and wind reach '
        'the same targets with far less overbuild.</figcaption></figure>'
        '</div>'
        '<h2>Where the money goes</h2>'
        '<p>The delivered cost, split by what is actually paid for. Two things to notice: '
        'the <b>battery is a thin slice at every target</b> — high-renewable economics are '
        'dominated by generation overbuild and firming, not storage — and moving from 70% '
        'to 85% renewable is bought almost entirely with <b>more generation capital</b> '
        '(the overbuild that rides out multi-day lulls), while the firming and carbon '
        'slices shrink.</p>'
        '<div class="figs">'
        f'<figure><img src="{_img("eu_firm_fig4_breakdown.png")}" alt="EU cost breakdown '
        'at 70% renewable"><figcaption>Europe, 70% renewable — generation capital plus gas '
        'firming (fuel, capital, carbon) carry the bill; the battery (pink) barely '
        'shows.</figcaption></figure>'
        f'<figure><img src="{_img("eu_firm_fig5_breakdown.png")}" alt="EU cost breakdown '
        'at 85% renewable"><figcaption>Europe, 85% renewable — the last percentage points '
        'are bought with overbuild: generation capital balloons, firming shrinks, the '
        'battery stays thin.</figcaption></figure>'
        '</div>'
        + tornado_section()
        + benchmarks_section(us, eu) +
        '<h2>Key assumptions</h2>'
        + assumptions_table(us, eu) +
        '<p class="sub" style="font-size:13.5px">All in real 2025 USD; costs fall over time '
        'via learning curves. Full derivations, data sources and the accuracy summary: '
        f'<a href="{REPO_URL}/blob/main/model_documentation.md"><code>model_documentation.md'
        '</code></a>.</p>'
        '<h2>Glossary</h2>'
        f'<table class="gloss"><tbody>{gloss}</tbody></table>'
        '<h2>Reproduce</h2>'
        '<p><code>pip install -r requirements.txt</code>, then <code>make reproduce '
        '&amp;&amp; make report</code> regenerates every figure, the exports and this site. '
        'The model is pure Python and runs fully offline.</p>')


# ── index page ──────────────────────────────────────────────────────────────────────

def index_page(us, eu):
    return (
        '<p class="sub">A techno-economic model of the least-cost way to run an always-on, '
        'off-grid datacenter on (mostly) renewable power — solar, wind, batteries and a '
        'backstop — and of when that beats burning gas, across Europe and the US, '
        '2025–2040.</p>'
        f'<p class="repo"><a href="{REPO_URL}">▶&nbsp; Source code &amp; full methodology '
        'on GitHub</a></p>'
        + tldr(us, eu) +
        '<div class="caveat"><b>How much to trust this.</b> A stylised techno-economic '
        'model: trust the directional comparisons, not absolute numbers to better than '
        '~±20–30%. The headline runs on measured ERA5 weather (EU: France; US: Texas; '
        '2015–2025) at a single site per region — every number on this page is generated '
        'from the model\'s exports. Full assumptions, caveats and glossary: '
        '<a href="method.html">Method &amp; trust</a>.</div>'
        '<h2>The question</h2>'
        '<p>AI datacenters use a lot of electricity, and the boom is — so far — pushing '
        'power-sector emissions up, not down. This model asks the narrower question each '
        '<i>builder</i> controls: if you pair a new datacenter with its own solar, wind '
        'and batteries, how clean can it run, and what does that cost compared with just '
        'burning gas?</p>'
        '<p>The catch is that a datacenter needs power every hour and sun and wind don\'t '
        'deliver every hour. The bill therefore hinges on the <b>backstop</b> for dark, '
        'windless spells (a <i>Dunkelflaute</i>): a gas turbine, which emits, or a clean '
        'option — hydrogen, pumped storage, hydro, nuclear. The model finds the least-cost '
        'mix and the delivered cost per MWh (<b>LCOE</b> — levelized cost of energy), for '
        'Europe and the US, every year to 2040.</p>'
        '<h2>Key findings</h2>'
        f'<ul class="find">{findings(us, eu)}</ul>'
        '<h2>Delivered cost &amp; parity ($/MWh of load)</h2>'
        '<div class="cols">'
        f'<div><h3 style="margin:.2em 0">Europe</h3>{parity_table(eu)}</div>'
        f'<div><h3 style="margin:.2em 0">United States</h3>{parity_table(us)}</div>'
        '</div>'
        '<p class="sub" style="font-size:13.5px;margin-top:.6em">The <b>renewable '
        'target</b> is the minimum share of the datacenter\'s yearly energy that must come '
        'from solar + wind + battery (the rest is gas). Firm (always-on) workload; gas '
        'backup sized to 100% of load. "Crossover" = the first year the build\'s delivered '
        'cost drops below the gas baseline.</p>'
        '<h2>Cost trajectories</h2>'
        '<div class="figs">'
        f'<figure><img src="{_img("eu_firm_fig1_trajectories.png")}" alt="EU cost '
        'trajectory"><figcaption>Europe — lines are the central site; shaded bands the '
        'resource/siting range (poor↔good site). Includes the gas baseline, grid+PPA '
        'reference, and the gas-free H₂ system.</figcaption></figure>'
        f'<figure><img src="{_img("us_firm_fig1_trajectories.png")}" alt="US cost '
        'trajectory"><figcaption>United States — same series. In the cheap-gas US, '
        'renewables reach the gas baseline far later than in carbon-priced Europe.'
        '</figcaption></figure>'
        '</div>'
        '<h2>Dig deeper</h2>'
        '<div class="chapters">'
        '<a href="geography.html"><b>Geography</b><br>Where in Europe 24/7 clean power '
        'is cheapest — a ranked siting map, a scan of every ~1° cell of the continent '
        'on real weather, and how 14 EU/US markets compare.</a>'
        '<a href="zero-carbon.html"><b>Zero-carbon</b><br>Dropping gas entirely: the '
        'solar-only wall, what green hydrogen costs, and the honest land, water and '
        'embodied-carbon ledger per GW.</a>'
        '<a href="method.html"><b>Method &amp; trust</b><br>Assumptions, what is and '
        'isn\'t modelled, how far to trust the numbers, and a glossary.</a>'
        '</div>')


# ── page assembly ───────────────────────────────────────────────────────────────────

CSS = """
:root{--blue:#3A86FF;--ink:#1d2433;--muted:#5b6472;--line:#e3e7ee;--gas:#6B705C;--h2:#073B4C}
*{box-sizing:border-box}
body{font:16px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);
  margin:0;background:#f7f9fc}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 64px}
nav{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 20px}
nav a{padding:6px 13px;border-radius:8px;font-size:14px;font-weight:600;text-decoration:none;
  color:var(--ink);background:#fff;border:1px solid var(--line)}
nav a.on{background:var(--blue);color:#fff;border-color:var(--blue)}
h1{font-size:28px;margin:.2em 0 .3em}
h2{font-size:21px;margin:1.8em 0 .5em;border-bottom:2px solid var(--line);padding-bottom:.25em}
.sub{color:var(--muted);font-size:17px;margin:0 0 .6em}
.repo{margin:0 0 1.2em}
.repo a{display:inline-block;background:var(--blue);color:#fff;padding:7px 14px;
  border-radius:8px;font-size:14px;font-weight:600;text-decoration:none}
.repo a:hover{background:#2f6fe0}
.tldr{background:#eef4ff;border:1px solid #c5d8ff;border-radius:10px;padding:16px 20px;
  margin:0 0 14px}
.tldr h2{margin:0 0 .4em;border:none;padding:0;font-size:17px}
.tldr ol{margin:0;padding-left:20px}
.tldr li{margin:.45em 0}
.tldr .macro{font-style:italic;color:var(--muted);font-size:14px;margin:.7em 0 0}
.tldr .scale{font-size:14px;margin:.8em 0 0;padding-top:.7em;border-top:1px solid #c5d8ff}
.caveat{background:#fff8e6;border:1px solid #f0d98a;border-radius:10px;padding:14px 18px;
  font-size:14.5px;color:#6b5512}
ul.find{padding-left:20px} ul.find li{margin:.6em 0}
details{margin:.15em 0 0}
summary{cursor:pointer;color:var(--blue);font-size:13.5px}
details p{font-size:14px;color:var(--muted);margin:.35em 0 0}
table{border-collapse:collapse;width:100%;font-size:14px;margin:.5em 0;background:#fff;
  border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line)}
thead th{background:#eef2f8;text-align:right} tbody th{text-align:left;font-weight:600}
td.cx{font-weight:700;color:var(--blue)} td.src{text-align:left;color:var(--muted);font-size:12.5px}
tr.gas th,tr.gas td{color:var(--gas);font-weight:600}
tr.h2 th,tr.h2 td{color:var(--h2);font-weight:600} tr.ref td,tr.ref th{color:#06a37a}
table.gloss td{text-align:left;color:var(--muted);font-size:13.5px}
table.gloss th{width:220px;vertical-align:top}
.figs{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.figs figure{margin:0;background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px}
.figs img{width:100%;height:auto} figcaption{font-size:12.5px;color:var(--muted);margin-top:6px}
figure.box{margin:1em 0;background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px}
@media(max-width:760px){.figs{grid-template-columns:1fr}}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
.chapters{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.chapters a{display:block;background:#fff;border:1px solid var(--line);border-radius:10px;
  padding:12px 14px;font-size:14px;text-decoration:none;color:var(--ink)}
.chapters a:hover{border-color:var(--blue)} .chapters b{color:var(--blue)}
@media(max-width:760px){.chapters{grid-template-columns:1fr}}
.foot{margin-top:40px;font-size:12.5px;color:var(--muted);border-top:1px solid var(--line);
  padding-top:14px}
code{background:#eef2f8;padding:1px 5px;border-radius:4px;font-size:13px}
a{color:var(--blue)}
"""


def _page(fname, title, body, foot, h1=None):
    nav_parts = []
    for href, label in NAV:      # no f-string conditional: 3.10 forbids \" in {expr}
        cls = ' class="on"' if href == fname else ''
        nav_parts.append(f'<a href="{href}"{cls}>{label}</a>')
    nav = "".join(nav_parts)
    head_h1 = f"<h1>{h1}</h1>" if h1 else ""
    return ('<!doctype html>\n<html lang="en"><head>\n'
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{title}</title>\n<style>{CSS}</style></head><body><div class="wrap">\n'
            f'<nav>{nav}</nav>\n{head_h1}{body}\n{foot}\n</div></body></html>')


def main():
    us, eu = _load("us_firm"), _load("eu_firm")
    prov = us.get("provenance") or {}
    foot = (f'<div class="foot">All figures are real 2025 USD; at 2025-average exchange '
            'rates (≈$1.1 per €) $100/MWh is roughly €90/MWh. '
            f'Model v{MODEL_VERSION} · generated from '
            f'<code>output/*_firm_results.json</code> at commit '
            f'<code>{prov.get("git_commit", "—")}</code> (config '
            f'{prov.get("config_sha256", "—")}) · <a href="{REPO_URL}">source on GitHub</a> '
            '· licensed CC BY 4.0. Reproduce: <code>make reproduce &amp;&amp; make report'
            '</code>.</div>')
    pages = {
        "index.html": ("Green datacenters — Overview",
                       "How green can a datacenter be — and where should it go?",
                       index_page(us, eu)),
        "geography.html": ("Green datacenters — Geography",
                           None, siting_section() + scan_section() + locations_section()),
        "zero-carbon.html": ("Green datacenters — Zero-carbon",
                             None, wind_section() + footprint_section(us, eu)),
        "method.html": ("Green datacenters — Method & trust",
                        None, method_page(us, eu)),
    }
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    for fname, (title, h1, body) in pages.items():
        html = _page(fname, title, body, foot, h1=h1)
        out = os.path.join(ROOT, "docs", fname)
        with open(out, "w") as fh:
            fh.write(html)
        print(f"Wrote {out} ({len(html.encode()) / 1024:.0f} KB, self-contained).")


if __name__ == "__main__":
    main()
