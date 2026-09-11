"""Alternate consolidated-report renderer (v2).

Reads the exact same three JSON reports as `report_html.py`
(`Digital_Twin_Consolidated_Report.json`, which already embeds
`DDI_Clinical_Assessment.json` and `External_Evidence_Report.json` under
`digital_twin_report.sections`) and produces a differently designed PDF --
a navy/teal, card-based, web-report visual language adapted from a
reference HTML mockup the user supplied, instead of `report_html.py`'s
dense clinical-document layout.

This module never derives, invents, or modifies a clinical value. It
reuses `report_html.py`'s own JSON-access and text-formatting helpers (via
import, not copy-paste) so both renderers read every field the same way;
only the HTML/CSS presentation differs. It does not import or execute any
of `report_html.py`'s own section-builder functions, and it does not
modify that module.

Per DDI_Integration_Plan_v02 section 13/15, therapy positions are stated in
plain clinical language (never a raw enum) and evidence gaps are stated as
"unresolved" / "not evaluated" -- never presented as an absence of risk.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .core.utils import ensure_dir
from . import report_html as _rh

DEFAULT_INPUT_PATH = Path("reports") / "Digital_Twin_Consolidated_Report.json"
DEFAULT_OUTPUT_PATH = Path("reports") / "Digital_Twin_Integrated_Report_v2.pdf"

DOC_TITLE = "Neuro Digital Twin Intelligence — Integrated Epilepsy & DDI Report"

# ---------------------------------------------------------------------------
# Reused infrastructure and constants from report_html.py -- generic JSON
# access / text formatting / PDF-printing helpers that do not depend on
# either renderer's visual design.
# ---------------------------------------------------------------------------
_get = _rh._get
_t = _rh._t
_raw_text = _rh._raw_text
_sentence_case = _rh._sentence_case
_humanize = _rh._humanize
_pretty_date = _rh._pretty_date
_pretty_range = _rh._pretty_range
_duration_phrase = _rh._duration_phrase
_overall_span = _rh._overall_span
_patient_label = _rh._patient_label
_gene_from_basis = _rh._gene_from_basis
_citation_pmid = _rh._citation_pmid
_pgx_worst_per_drug = _rh._pgx_worst_per_drug
_is_cached = _rh._is_cached
_find_edge = _rh._find_edge
_print_html_to_pdf = _rh._print_html_to_pdf

NOT_AVAILABLE = _rh.NOT_AVAILABLE
NONE_REPORTED = _rh.NONE_REPORTED
CURRENT_REGIMEN_DRUG_NAMES = _rh.CURRENT_REGIMEN_DRUG_NAMES

SEC_CLINICAL_NOTES = _rh.SEC_CLINICAL_NOTES
SEC_CBC = _rh.SEC_CBC
SEC_CT = _rh.SEC_CT
SEC_MRI = _rh.SEC_MRI
SEC_ECG = _rh.SEC_ECG
SEC_EEG = _rh.SEC_EEG
SEC_QUESTIONNAIRE = _rh.SEC_QUESTIONNAIRE
SEC_GENETICS = _rh.SEC_GENETICS
SEC_DDI = _rh.SEC_DDI
SEC_EXTERNAL_EVIDENCE = _rh.SEC_EXTERNAL_EVIDENCE

# Plain-clinical-language statement per therapy_assessment "position" enum
# (patient_prime_agent.path_d.ddi.aggregation.build_therapy_assessment) --
# reused verbatim from report_html.py so both renderers describe the same
# computed position with the same wording (DDI_Integration_Plan_v02 section
# 13: never display the raw enum).
_POSITION_STATEMENT = _rh._POSITION_KEY_FINDING


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the consolidated Digital Twin JSON as the v2 (navy/teal card) HTML-styled PDF report")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    report = json.loads(args.input.read_text(encoding="utf-8"))
    build_pdf(report, args.output)
    print(f"Wrote Digital Twin v2 PDF report to {args.output}")
    return 0


def build_pdf(report: dict[str, Any], output_path: Path) -> Path:
    """Render `report` (the consolidated JSON, already loaded) to `output_path`."""
    ensure_dir(output_path.parent)
    digital_twin = report.get("digital_twin_report") or {}
    sections = digital_twin.get("sections") or {}
    manifest = report.get("source_manifest") or {}

    html = _build_html(digital_twin, sections, manifest)
    html_path = output_path.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")

    # Unlike report_html.py, this renderer relies on the reference design's
    # own built-in `@media print` rules (hides the sticky nav, forces the
    # dark hero background, avoids breaking cards across pages) rather than
    # a reportlab running header/footer overlay -- there is no analogous
    # requirement here, and Chromium's headless --print-to-pdf already
    # respects the page's own @page/@media print CSS.
    _print_html_to_pdf(html_path, output_path)
    return output_path


# ---------------------------------------------------------------------------
# Small local helpers not already covered by report_html.py's infra.
# ---------------------------------------------------------------------------
def _short_source(ref: Any) -> str:
    """Compact, human-readable provenance label for an evidence item's
    `source_reference` -- a URL is shown as host + trailing path, a JSON
    field path is shown as its trailing dotted segment, anything else is
    shown as-is (truncated only if very long). Never a fabricated label."""
    text = _raw_text(ref)
    if text in (NOT_AVAILABLE, NONE_REPORTED):
        return text
    match = re.match(r"https?://([^/]+)(/.*)?", text)
    if match:
        host, path = match.group(1), match.group(2) or ""
        return host + (path if len(path) <= 42 else "…" + path[-38:])
    if ".json:" in text:
        return text.split(".json:", 1)[1]
    return text if len(text) <= 90 else text[:60] + "…" + text[-25:]


def _evidence_list_html(items: list[dict[str, Any]] | None, *, empty_label: str) -> str:
    items = items or []
    if not items:
        return f'<li class="muted">{_t(empty_label)}</li>'
    return "".join(
        f'<li>{_t(item.get("statement"))} <span class="src">— {_t(_short_source(item.get("source_reference")))}</span></li>'
        for item in items
    )


def _pct(numerator: int, denominator: int) -> float:
    return (numerator / denominator * 100.0) if denominator else 0.0


def _first_sentence(text: Any) -> str:
    """First sentence of a real (possibly long) narrative field, for a
    compact summary card -- the full text remains available verbatim
    elsewhere (e.g. the Signals section's CT table or ECG chart note), so
    nothing is lost, only not duplicated in extenso in a cramped layout."""
    raw = _raw_text(text)
    if raw in (NOT_AVAILABLE, NONE_REPORTED):
        return raw
    match = re.search(r"^(.*?[.!?])(\s|$)", raw)
    return match.group(1) if match else raw


# ---------------------------------------------------------------------------
# Inline SVG chart builders -- generic, real-data-driven geometry styled
# with the reference design's own `.chart-svg` class vocabulary
# (gridline / axisline / axis-title / line-a / point-a / bar-mark / area).
# Every plotted value is a real number already read from the source JSON;
# nothing here is smoothed, interpolated, or estimated.
# ---------------------------------------------------------------------------
def _svg_bar_series(labels: list[str], values: list[float], *, width: int = 620, height: int = 250, value_fmt: str = "{:g}") -> str:
    if not values or not any(isinstance(v, (int, float)) for v in values):
        return ""
    pad_left, pad_right, pad_top, pad_bottom = 54, 20, 34, 40
    plot_w, plot_h = width - pad_left - pad_right, height - pad_top - pad_bottom
    max_v = max((v for v in values if isinstance(v, (int, float))), default=0) or 1
    n = len(values)
    step = plot_w / n
    bar_w = min(58.0, step * 0.62)

    grid = []
    for i in range(5):
        y = pad_top + plot_h - (plot_h * i / 4)
        grid.append(f'<line class="gridline" x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}"/>')
        grid.append(f'<text x="{pad_left - 8}" y="{y + 4:.1f}" text-anchor="end">{max_v * i / 4:.0f}</text>')

    bars = []
    for i, (label, v) in enumerate(zip(labels, values)):
        v = v if isinstance(v, (int, float)) else 0
        x = pad_left + step * i + (step - bar_w) / 2
        h = (v / max_v) * plot_h if max_v else 0
        y = pad_top + plot_h - h
        bars.append(f'<rect class="bar-mark" x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="3"/>')
        bars.append(f'<text class="axis-title" x="{x + bar_w / 2:.1f}" y="{y - 7:.1f}" text-anchor="middle">{value_fmt.format(v)}</text>')
        bars.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - pad_bottom + 18:.1f}" text-anchor="middle">{_t(label)}</text>')

    return f'<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img">{"".join(grid)}{"".join(bars)}</svg>'


def _svg_line_series(labels: list[str], values: list[float], *, width: int = 620, height: int = 250, area: bool = True) -> str:
    numeric = [v for v in values if isinstance(v, (int, float))]
    if len(numeric) < 2:
        return ""
    pad_left, pad_right, pad_top, pad_bottom = 54, 20, 30, 40
    plot_w, plot_h = width - pad_left - pad_right, height - pad_top - pad_bottom
    min_v, max_v = min(numeric), max(numeric)
    span = (max_v - min_v) or 1
    n = len(values)
    step = plot_w / (n - 1) if n > 1 else 0

    points = []
    for i, v in enumerate(values):
        v = v if isinstance(v, (int, float)) else min_v
        x = pad_left + step * i
        y = pad_top + plot_h - ((v - min_v) / span) * plot_h
        points.append((x, y, v))

    grid = []
    for i in range(5):
        y = pad_top + plot_h - (plot_h * i / 4)
        val = min_v + span * i / 4
        grid.append(f'<line class="gridline" x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}"/>')
        grid.append(f'<text x="{pad_left - 8}" y="{y + 4:.1f}" text-anchor="end">{val:.0f}</text>')

    path_d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y, _v) in enumerate(points))
    area_html = ""
    if area:
        area_d = path_d + f" L{points[-1][0]:.1f} {pad_top + plot_h:.1f} L{points[0][0]:.1f} {pad_top + plot_h:.1f} Z"
        area_html = f'<path class="area" d="{area_d}"/>'
    circles = "".join(f'<circle class="point-a" cx="{x:.1f}" cy="{y:.1f}" r="4.5"/>' for x, y, _v in points)
    x_labels = "".join(
        f'<text x="{x:.1f}" y="{height - pad_bottom + 18:.1f}" text-anchor="middle">{_t(l)}</text>' for (x, _y, _v), l in zip(points, labels)
    )
    return (
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img">{"".join(grid)}'
        f'{area_html}<path class="line-a" d="{path_d}"/>{circles}{x_labels}</svg>'
    )


# ---------------------------------------------------------------------------
# CSS -- adapted from the navy/teal reference design the user supplied
# (NeuroTwin_Integrated_DDI_Report.html): same variable names, card
# vocabulary, therapy-stack / donut / pgx-chip / risk-strip / identity-card
# classes, sticky nav, and print rules. Extended with a `.src` class (inline
# evidence-item provenance) and a `.donut-label` element (a real HTML node
# instead of a static `::after` string, so the percentage can be a live
# computed value rather than a fixed demo number).
# ---------------------------------------------------------------------------
_CSS = """
:root{--ink:#132238;--muted:#627286;--paper:#f2f6f8;--card:#fff;--line:#dce5e9;--navy:#10243c;--teal:#087c83;--cyan:#35b9b0;--mint:#dff5ef;--blue:#e8f1f7;--amber:#a86505;--amber-bg:#fff1d6;--red:#b53c48;--red-bg:#fdebed;--green:#247a5a;--shadow:0 14px 38px rgba(21,43,63,.08);--mono:"Cascadia Code",Consolas,monospace;--sans:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
[data-theme=dark]{--ink:#e9f2f6;--muted:#a8b9c5;--paper:#09141e;--card:#10202d;--line:#263948;--navy:#07111a;--teal:#61d1d2;--cyan:#65d7ca;--mint:#13362f;--blue:#162c3b;--amber:#ffc66a;--amber-bg:#3a2a13;--red:#ff8d98;--red-bg:#3c1c24;--green:#71d4aa;--shadow:0 18px 46px rgba(0,0,0,.25)}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:74px}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 var(--sans)}a{color:var(--teal)}.shell{max-width:1180px;margin:auto;padding:0 26px}
@page{size:A4;margin:12mm}
.top{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--paper) 88%,transparent);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}.top .shell{height:62px;display:flex;align-items:center;gap:18px}.brand{font-weight:850;letter-spacing:-.02em;display:flex;align-items:center;gap:10px;white-space:nowrap}.mark{width:29px;height:29px;border:1px solid var(--teal);border-radius:9px;position:relative}.mark:before{content:"";position:absolute;inset:8px 5px;background:linear-gradient(150deg,transparent 40%,var(--teal) 41% 48%,transparent 49%),radial-gradient(circle,var(--cyan) 0 2px,transparent 3px);background-size:100%,9px 9px}.nav{margin-left:auto;display:flex;gap:3px}.nav a{font-size:11px;font-weight:750;text-decoration:none;color:var(--muted);padding:7px;border-radius:8px}.nav a:hover{background:var(--card);color:var(--ink)}button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:9px;width:34px;height:34px;cursor:pointer}
.notice{background:var(--blue);border-bottom:1px solid var(--line);font-size:13px}.notice .shell{display:flex;gap:10px;padding-top:9px;padding-bottom:9px}.notice b{color:var(--teal);white-space:nowrap}
.hero{background:var(--navy);color:#f8fbfd;padding:68px 0 52px;position:relative;overflow:hidden}.hero:before{content:"";position:absolute;width:650px;height:650px;border-radius:50%;right:-130px;top:-330px;background:radial-gradient(circle,rgba(53,185,176,.35),transparent 65%)}.hero-grid{position:relative;display:grid;grid-template-columns:1.35fr .65fr;gap:44px;align-items:end}.eyebrow,.kicker{font:800 11px/1 var(--mono);text-transform:uppercase;letter-spacing:.15em;color:var(--teal)}.hero .eyebrow{color:#80e2d8}h1{font-size:clamp(33px,5vw,54px);line-height:1.05;letter-spacing:-.045em;margin:14px 0 18px}.hero p{font-size:18px;color:#bdd0db;max-width:720px}.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:25px}.chips span{font-size:11px;padding:6px 9px;border:1px solid rgba(255,255,255,.18);border-radius:99px;background:rgba(255,255,255,.06)}.identity-card{position:relative;width:calc(100% - 52px);max-width:1128px;margin:34px auto 0;border:1px solid rgba(255,255,255,.18);border-radius:18px;background:rgba(255,255,255,.075);backdrop-filter:blur(12px);overflow:hidden}.identity-head{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 17px;border-bottom:1px solid rgba(255,255,255,.13);background:rgba(255,255,255,.035)}.identity-head b{font-size:13px}.source-badge{font:750 9px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:#80e2d8}.identity-grid{display:grid;grid-template-columns:1.15fr .65fr 1.5fr .8fr}.identity-item{padding:15px 17px;border-right:1px solid rgba(255,255,255,.12);border-bottom:1px solid rgba(255,255,255,.12)}.identity-item:nth-child(4n){border-right:0}.identity-item small{display:block;color:#8fa9b8;font:750 9px var(--mono);text-transform:uppercase;letter-spacing:.09em;margin-bottom:5px}.identity-item strong{display:block;font-size:13px;line-height:1.4}.identity-item span{display:block;color:#bed0da;font-size:11px;line-height:1.4;margin-top:3px}.identity-wide{grid-column:span 2}.identity-full{grid-column:1/-1;border-right:0}.identity-full strong{font-size:12px}.identity-note{color:#ffcf7c!important}
.orb{height:270px;display:grid;place-items:center;position:relative}.core{width:142px;height:142px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#7ae2d6,#0d7b83 58%,#123f57);display:grid;place-items:center;text-align:center;font-weight:850;line-height:1.2;box-shadow:0 0 60px rgba(53,185,176,.4);color:#f8fbfd;padding:10px;font-size:13px}.node{position:absolute;font:700 10px var(--mono);border:1px solid rgba(127,226,216,.4);background:rgba(7,20,31,.8);padding:6px 8px;border-radius:8px;color:#eaf7f5}.n1{top:10px}.n2{right:0;top:74px}.n3{right:8px;bottom:30px}.n4{left:0;bottom:35px}.n5{left:0;top:72px}
.section{padding:56px 0;border-bottom:1px solid var(--line)}.head{display:flex;justify-content:space-between;gap:26px;margin-bottom:25px}.head p{max-width:560px;color:var(--muted);margin:4px 0}.kicker{margin-bottom:10px}h2{font-size:clamp(26px,3.6vw,38px);line-height:1.1;letter-spacing:-.035em;margin:0}h3{font-size:18px;line-height:1.3;margin:0}.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:13px}.metric,.panel,.med,.signal,.phase{background:var(--card);border:1px solid var(--line);border-radius:17px;box-shadow:var(--shadow)}.metric{padding:19px}.metric strong{display:block;font-size:32px;line-height:1;letter-spacing:-.05em}.metric span{display:block;color:var(--muted);font-size:12px;margin-top:8px}.teal{color:var(--teal)}.red{color:var(--red)}.green{color:var(--green)}.amber{color:var(--amber)}
.two{display:grid;grid-template-columns:1.25fr .75fr;gap:16px;margin-top:16px}.panel{padding:24px}.lead{font-size:19px;line-height:1.45;color:var(--ink)!important;margin-top:0}.panel p{color:var(--muted)}.highlight{background:linear-gradient(120deg,var(--mint),var(--blue))}.priority{display:grid;grid-template-columns:32px 1fr;gap:10px;border-top:1px solid var(--line);padding:12px 0}.priority:first-of-type{border-top:0}.num{width:29px;height:29px;border-radius:8px;background:var(--navy);color:white;display:grid;place-items:center;font:700 11px var(--mono)}.priority p{font-size:12px;margin:2px 0}
.events{display:grid;gap:12px;margin-top:18px}.event{display:grid;grid-template-columns:80px 10px 1fr;gap:10px}.event time{font:750 10px var(--mono);color:var(--teal)}.event i{width:9px;height:9px;border-radius:50%;background:var(--cyan);box-shadow:0 0 0 4px var(--mint);margin-top:3px}.event p{font-size:12px;margin:0}
.clinical-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:13px}.clinical-card{background:var(--card);border:1px solid var(--line);border-radius:17px;padding:19px;box-shadow:var(--shadow)}.clinical-card h3{margin-bottom:12px}.clinical-card dl{margin:0}.clinical-card dl div{display:grid;grid-template-columns:105px 1fr;gap:10px;padding:8px 0;border-top:1px solid var(--line)}.clinical-card dl div:first-child{border-top:0}.clinical-card dt{font:750 9px var(--mono);text-transform:uppercase;color:var(--muted)}.clinical-card dd{margin:0;font-size:12px}.clinical-card.wide{grid-column:span 2}.risk-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-top:14px}.risk-chip{border:1px solid var(--line);border-radius:12px;background:var(--card);padding:11px}.risk-chip b{display:block;font-size:11px}.risk-chip span{font:800 9px var(--mono);text-transform:uppercase}.risk-high span{color:var(--red)}.risk-mod span{color:var(--amber)}.risk-low span{color:var(--green)}.evidence-note{margin-top:14px;padding:12px 14px;border-radius:12px;background:var(--blue);border-left:4px solid var(--teal);font-size:12px;color:var(--muted)}.pgx-summary{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:15px 0}.pgx-gene{border:1px solid var(--line);border-radius:12px;padding:10px;background:var(--blue)}.pgx-gene b{display:block;font-size:13px}.pgx-gene span{font-size:10px;color:var(--muted)}.subhead{display:flex;align-items:end;justify-content:space-between;gap:20px;margin:30px 0 12px}.subhead h3{font-size:22px}.subhead p{max-width:620px;margin:0;color:var(--muted);font-size:12px}
.therapy-stack{display:grid;gap:14px}.therapy-card{background:var(--card);border:1px solid var(--line);border-radius:17px;box-shadow:var(--shadow);overflow:hidden}.therapy-card>header{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;padding:18px 20px;background:var(--blue);border-bottom:1px solid var(--line)}.therapy-card>header small{display:block;font:800 9px var(--mono);text-transform:uppercase;color:var(--teal);margin-bottom:4px}.therapy-card>header p{margin:3px 0 0;color:var(--muted);font-size:12px}.therapy-position{max-width:330px;text-align:right;font-size:12px;font-weight:800;color:var(--ink)}.therapy-columns{display:grid;grid-template-columns:repeat(3,1fr)}.evidence-block{padding:17px 19px;border-right:1px solid var(--line)}.evidence-block:last-child{border-right:0}.evidence-block h4{font:800 10px var(--mono);text-transform:uppercase;letter-spacing:.06em;margin:0 0 9px}.evidence-block ul{margin:0;padding-left:17px}.evidence-block li{font-size:12px;color:var(--muted);margin:6px 0}.evidence-block li .src{font-size:10px;color:var(--muted);opacity:.8}.evidence-block.support h4{color:var(--green)}.evidence-block.counter h4{color:var(--red)}.evidence-block.unknown h4{color:var(--amber)}.therapy-impression{padding:13px 19px;border-top:1px solid var(--line);font-size:12px}.therapy-impression b{color:var(--teal)}.pill-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.pill{font-size:11px;border:1px solid var(--line);background:var(--blue);border-radius:99px;padding:4px 10px;color:var(--ink)}.alt-assessment td:nth-child(1){min-width:135px}
.ddi-grid{display:grid;grid-template-columns:.8fr 1.2fr;gap:16px}.coverage{display:flex;align-items:center;gap:20px;margin-top:18px}.donut{width:145px;height:145px;border-radius:50%;position:relative;flex:none;display:grid;place-items:center}.donut-label{width:107px;height:107px;border-radius:50%;background:var(--card);display:grid;place-items:center;font-size:23px;font-weight:850}.legend{font-size:12px;color:var(--muted)}.legend div{margin:7px 0}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:18px}.mini{border:1px solid var(--line);border-radius:12px;padding:12px;background:var(--blue)}.mini b{display:block;font-size:24px;line-height:1}.mini span{font-size:10px;color:var(--muted)}.infer{margin-top:16px;border-left:4px solid var(--amber);background:var(--amber-bg)}.infer h3{color:var(--amber)}.infer .big{font-size:19px;line-height:1.4;color:var(--ink)}
.tablewrap{overflow:auto;border:1px solid var(--line);border-radius:14px;background:var(--card);box-shadow:var(--shadow)}table{border-collapse:collapse;width:100%;font-size:12px}th{text-align:left;background:var(--navy);color:#dce9ef;padding:12px;font:750 9px var(--mono);text-transform:uppercase;letter-spacing:.07em}td{padding:13px;border-top:1px solid var(--line);vertical-align:top}.tag{display:inline-flex;padding:5px 7px;border-radius:99px;font:800 9px var(--mono);text-transform:uppercase;white-space:nowrap}.major{color:var(--red);background:var(--red-bg)}.mod,.gap{color:var(--amber);background:var(--amber-bg)}.ok{color:var(--green);background:var(--mint)}
.chart-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.chart-card{background:var(--card);border:1px solid var(--line);border-radius:17px;padding:22px;box-shadow:var(--shadow);overflow:hidden}.chart-card.wide{grid-column:1/-1}.chart-head{display:flex;align-items:start;justify-content:space-between;gap:18px;margin-bottom:12px}.chart-head p,.chart-note{font-size:11px;color:var(--muted);margin:3px 0 0}.chart-meta{font:800 9px var(--mono);text-transform:uppercase;color:var(--teal);white-space:nowrap}.chart-svg{display:block;width:100%;height:auto;overflow:visible}.chart-svg .gridline{stroke:var(--line);stroke-width:1}.chart-svg text{fill:var(--muted);font:10px var(--mono)}.chart-svg .axis-title{fill:var(--ink);font-weight:800}.chart-svg .area{fill:color-mix(in srgb,var(--teal) 18%,transparent)}.chart-svg .line-a{fill:none;stroke:var(--teal);stroke-width:3;stroke-linecap:round;stroke-linejoin:round}.chart-svg .point-a{fill:var(--teal);stroke:var(--card);stroke-width:2}.chart-svg .bar-mark{fill:color-mix(in srgb,var(--cyan) 42%,var(--blue))}
.phase{overflow:hidden}.phase header{background:var(--navy);color:#fff;padding:15px 17px}.phase header span{color:#7fe1d6;font:750 9px var(--mono);text-transform:uppercase}.phase header h3{margin-top:5px}.phase ul{margin:0;padding:17px 17px 17px 35px}.phase li{font-size:12px;color:var(--muted);margin-bottom:9px}.phase li::marker{color:var(--teal)}.road{display:grid;grid-template-columns:repeat(3,1fr);gap:13px}
.notes{font-size:12px;color:var(--muted)}.notes li{margin:7px 0}.disclaimer{margin-top:18px;padding:15px;border:1px solid var(--line);border-radius:12px;background:var(--blue);font-size:11px;color:var(--muted)}footer{padding:25px 0 42px;color:var(--muted);font-size:11px}
@media screen and (max-width:900px){.hero-grid,.two,.ddi-grid,.chart-grid{grid-template-columns:1fr}.chart-card.wide{grid-column:span 1}.orb,.nav{display:none}.identity-grid{grid-template-columns:1fr 1fr}.identity-item:nth-child(4n){border-right:1px solid rgba(255,255,255,.12)}.identity-item:nth-child(2n){border-right:0}.identity-wide{grid-column:span 1}.identity-full{grid-column:1/-1}.grid4{grid-template-columns:repeat(2,1fr)}.clinical-grid,.road{grid-template-columns:1fr 1fr}.clinical-card.wide{grid-column:span 2}.risk-strip{grid-template-columns:repeat(3,1fr)}.pgx-summary{grid-template-columns:repeat(3,1fr)}.therapy-columns{grid-template-columns:1fr}.evidence-block{border-right:0;border-bottom:1px solid var(--line)}.evidence-block:last-child{border-bottom:0}.head{display:block}.head p{margin-top:10px}}
@media print{.top{display:none}.hero{background:#10243c!important;print-color-adjust:exact}.identity-card{break-inside:avoid}.section{padding:28px 0}.panel,.metric,.med,.signal,.phase,.tablewrap,.clinical-card,.chart-card,.therapy-card,.disclaimer{box-shadow:none;break-inside:avoid}}
"""


def _section_head(kicker: str, title: str, sub: str = "") -> str:
    sub_html = f"<p>{_t(sub)}</p>" if sub else ""
    return f"""<div class="head"><div><div class="kicker">{_t(kicker)}</div><h2>{_t(title)}</h2></div>{sub_html}</div>"""


def _fmt_num(value: Any) -> str:
    """Formats a real numeric dose/value without a spurious trailing '.0'
    (e.g. JSON's 150.0 -> "150", but 12.5 stays "12.5")."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _raw_text(value)


# ---------------------------------------------------------------------------
# Hero + identity card
# ---------------------------------------------------------------------------
def _sec_hero(digital_twin: dict[str, Any], sections: dict[str, Any], manifest: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    cbc = sections.get(SEC_CBC) or {}
    ct = sections.get(SEC_CT) or {}
    ecg = sections.get(SEC_ECG) or {}
    eeg = sections.get(SEC_EEG) or {}
    genetics = sections.get(SEC_GENETICS) or {}
    ddi = sections.get(SEC_DDI) or {}

    profile = _get(clinical_notes, "patient_profile", default={})
    diagnosis = _get(profile, "diagnosis", default={})
    window = _get(clinical_notes, "observation_window", default={})
    span = _overall_span(sections)
    current_state = _get(clinical_notes, "digital_twin_state", "current_state")

    reconciled = _get(ddi, "medication_reconciliation", "normalized_medications", default=[])
    therapy_line = (
        " · ".join(
            f"{_raw_text(m.get('source_name'))} {_fmt_num((m.get('dose') or {}).get('value'))}{_raw_text((m.get('dose') or {}).get('unit'))} {_raw_text(m.get('frequency'))}"
            for m in reconciled
        )
        if reconciled
        else _raw_text(_get(clinical_notes, "clinical_inference", "medication_response", "current_regimen", default=None)) or NONE_REPORTED
    )
    conflicts = _get(ddi, "medication_reconciliation", "conflicts", default=[])

    sex = profile.get("gender")
    sex_label = {"m": "Male", "f": "Female"}.get(str(sex).strip().lower(), _raw_text(sex)) if sex else None
    age_sex = " · ".join(p for p in [f"{_raw_text(profile.get('age'))} years" if profile.get("age") is not None else None, sex_label] if p)

    included = manifest.get("sections_included") or []
    total_categories = len(included) + len(manifest.get("sections_missing") or [])

    ddi_generated = ddi.get("generated_at")
    pgx_reported = _get(genetics, "patient", "report_date")

    chips = [
        f"Observation · {_pretty_range(window.get('start_date'), window.get('end_date'))}" if window.get("start_date") else None,
        f"Duration · {_duration_phrase(*span)}" if span else None,
        f"PGx panel · {_pretty_date(pgx_reported)}" if pgx_reported else None,
        f"DDI analysis · {_pretty_date(ddi_generated)}" if ddi_generated else None,
        "Decision support · not a prescription",
    ]
    chips_html = "".join(f"<span>{_t(c)}</span>" for c in chips if c)

    node_stats = [
        (_get(eeg, "recording_statistics", "total_recordings"), "EEG"),
        (_get(cbc, "observation_window", "number_of_reports"), "labs"),
        (_get(ct, "reportMetadata", "numberOfStudies"), "CT"),
        (_get(ecg, "dataset_summary", "number_of_recordings"), "ECG"),
        (window.get("total_visits"), "visits"),
    ]
    node_html = "".join(f'<span class="node n{i + 1}">{_t(v)} {_t(l)}</span>' for i, (v, l) in enumerate(node_stats) if v is not None)

    conflict_note = (
        f'<span class="identity-note">Medication reconciliation flags {len(conflicts)} dose conflict(s): {"; ".join(_raw_text(c.get("details")) for c in conflicts)}</span>'
        if conflicts
        else '<span>No dose or frequency conflict was found across the reconciled source records for this regimen.</span>'
    )

    return f"""
<header class="top"><div class="shell"><div class="brand"><span class="mark"></span><span>Neuro Digital Twin Intelligence</span></div><nav class="nav"><a href="#snapshot">Snapshot</a><a href="#clinical">Clinical profile</a><a href="#trajectory">Trajectory</a><a href="#therapy">Therapy assessment</a><a href="#ddi">DDI inference</a><a href="#pgx">Pharmacogenomics</a><a href="#signals">Signals</a><a href="#roadmap">Roadmap</a></nav><button id="theme" aria-label="Toggle theme">&#9680;</button></div></header>
<div class="notice"><div class="shell"><b>Decision support, not a prescription.</b><span>This report is a source-preserving synthesis of the patient's own clinical, laboratory, imaging, EEG/ECG, genetic, and medication-reconciliation records. It does not replace clinician review, and no dose, start, or stop decision is made or implied here.</span></div></div>
<main>
<section class="hero"><div class="shell"><div class="hero-grid"><div><div class="eyebrow">Epilepsy Digital Twin Report &middot; Integrated neurological decision support</div><h1>Integrated epilepsy assessment &amp; drug&ndash;drug interaction evidence.</h1><p>A single integrated picture drawn from clinical visits, seizure history, EEG, ECG, CT/MRI, blood work, pharmacogenomics, and drug&ndash;drug interaction analysis.</p><div class="chips">{chips_html}</div></div><div class="orb"><div class="core">{_t(_sentence_case(current_state))}</div>{node_html}</div></div></div>
<div class="identity-card"><div class="identity-head"><b>Patient &amp; clinical context</b><span class="source-badge">{len(included)}/{total_categories or len(included)} data categories included</span></div><div class="identity-grid">
<div class="identity-item"><small>Patient</small><strong>{_t(profile.get('name'))}</strong><span>Patient ID {_t(digital_twin.get('patient_id')) if digital_twin.get('patient_id') else _t(profile.get('patient_id'))}</span></div>
<div class="identity-item"><small>Age / Sex</small><strong>{_t(age_sex) or NOT_AVAILABLE}</strong><span>&nbsp;</span></div>
<div class="identity-item"><small>Primary diagnosis</small><strong>{_t(diagnosis.get('primary'))}</strong><span>Secondary: {_t(diagnosis.get('secondary'))}</span></div>
<div class="identity-item"><small>Diagnosed since</small><strong>{_pretty_date(diagnosis.get('diagnosis_date'))}</strong><span>&nbsp;</span></div>
<div class="identity-item identity-full"><small>Current clinical status</small><strong>{_t(_sentence_case(current_state))}</strong></div>
<div class="identity-item identity-full"><small>Current therapy (reconciled)</small><strong>{_t(therapy_line)}</strong>{conflict_note}</div>
<div class="identity-item identity-full"><small>Reporting period</small><strong>Data reviewed: {_pretty_range(*span) if span else NOT_AVAILABLE} ({_duration_phrase(*span) if span else NOT_AVAILABLE})</strong><span>Clinic visit notes cover {_pretty_date(window.get('start_date'))} to {_pretty_date(window.get('end_date'))}; laboratory, imaging, EEG/ECG, and genetics data continue through {_pretty_date(span[1]) if span else NOT_AVAILABLE}.</span></div>
</div></div></section>
"""


# ---------------------------------------------------------------------------
# 01 · Snapshot (executive summary)
# ---------------------------------------------------------------------------
def _sec_snapshot(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    eeg = sections.get(SEC_EEG) or {}
    genetics = sections.get(SEC_GENETICS) or {}
    ddi = sections.get(SEC_DDI) or {}

    conclusion = _get(clinical_notes, "executive_summary", "clinical_conclusion")
    overall_risk = _get(clinical_notes, "longitudinal_summary", "overall_risk_level")
    state_vector = _get(clinical_notes, "digital_twin_state", "state_vector", default={})
    health_score = state_vector.get("overall_health_score")
    eeg_risk = _get(eeg, "digital_twin_state", "risk_level")
    flags = genetics.get("priority_safety_flags") or []
    high_flags = [f for f in flags if "high" in str(f.get("severity", "")).lower()]

    metrics = [
        (f"{health_score * 100:.0f}%" if isinstance(health_score, (int, float)) else NOT_AVAILABLE, "Overall health score", "teal"),
        (_raw_text(overall_risk).title(), "Overall clinical risk", "amber"),
        (_raw_text(eeg_risk), "EEG-derived seizure risk", "red"),
        (f"{len(flags)} ({len(high_flags)} high)", "Genetic safety flags", ""),
    ]
    metric_html = "".join(f'<article class="metric"><strong class="{cls}">{_t(v)}</strong><span>{_t(l)}</span></article>' for v, l, cls in metrics)

    monitoring = _get(ddi, "overall_interpretation", "recommended_monitoring", default=[])
    priorities_html = "".join(
        f'<div class="priority"><span class="num">{i}</span><div><p>{_t(m)}</p></div></div>' for i, m in enumerate(monitoring[:4], start=1)
    ) or '<p class="muted small">No recommended-monitoring items are present in the source reports.</p>'

    return f"""
<section class="section" id="snapshot"><div class="shell">
{_section_head("01 · executive summary", "Integrated status and immediate monitoring priorities.")}
<div class="grid4">{metric_html}</div>
<div class="two"><article class="panel"><div class="kicker">Integrated impression</div><p class="lead">{_t(conclusion)}</p></article>
<aside class="panel highlight"><div class="kicker">Recommended monitoring (current DDI assessment)</div>{priorities_html}</aside></div>
</div></section>
"""


# ---------------------------------------------------------------------------
# 02 · Comprehensive clinical profile
# ---------------------------------------------------------------------------
def _sec_clinical_profile(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    eeg = sections.get(SEC_EEG) or {}
    ecg = sections.get(SEC_ECG) or {}
    mri = sections.get(SEC_MRI) or {}
    ct = sections.get(SEC_CT) or {}
    cbc = sections.get(SEC_CBC) or {}
    genetics = sections.get(SEC_GENETICS) or {}
    ddi = sections.get(SEC_DDI) or {}

    seizure = _get(clinical_notes, "clinical_inference", "seizure_analysis", default={})
    sleep = _get(clinical_notes, "clinical_inference", "sleep_analysis", default={})
    cognitive = _get(clinical_notes, "clinical_inference", "cognitive_analysis", default={})
    qol = _get(clinical_notes, "clinical_inference", "quality_of_life", default={})
    med = _get(clinical_notes, "clinical_inference", "medication_response", default={})
    diagnosis = _get(clinical_notes, "patient_profile", "diagnosis", default={})

    tdm = _get(ddi, "clinical_context", "therapeutic_drug_monitoring", default=[])
    kidney = _get(ddi, "clinical_context", "kidney_function")
    liver = _get(ddi, "clinical_context", "liver_function")
    cbc_status = _get(cbc, "digital_twin_state", "overall_hematological_status")

    eeg_state = _get(eeg, "digital_twin_state", default={})
    ecg_summary = _get(ecg, "dataset_summary", default={})
    mri_summary = _get(mri, "mri_summary", default={})
    ct_meta = _get(ct, "reportMetadata", default={})

    flags = genetics.get("priority_safety_flags") or []
    high_flags = [f for f in flags if "high" in str(f.get("severity", "")).lower()]

    return f"""
<section class="section" id="clinical"><div class="shell">
{_section_head("02 · comprehensive clinical profile", "The full clinical picture behind the summary.", "Separates documented observations from interpretation across seizure phenotype, function, EEG/ECG/imaging, therapeutic monitoring, and organ safety.")}
<div class="clinical-grid">
<article class="clinical-card wide"><h3>Neurological phenotype &amp; course</h3><dl>
<div><dt>Diagnosis</dt><dd>{_t(diagnosis.get('primary'))}{'; ' + _t(diagnosis.get('secondary')) if diagnosis.get('secondary') else ''}; onset recorded {_pretty_date(diagnosis.get('diagnosis_date'))}.</dd></div>
<div><dt>Activity</dt><dd>{_t(seizure.get('overall_pattern'))}</dd></div>
<div><dt>Clustering</dt><dd>{_t(seizure.get('clustering_detected'))}</dd></div>
<div><dt>Control</dt><dd>{_t(seizure.get('overall_inference'))}</dd></div>
</dl></article>
<article class="clinical-card"><h3>Function, cognition &amp; symptoms</h3><dl>
<div><dt>Sleep</dt><dd>{_sentence_case(sleep.get('overall_trend'))}; average {_t(sleep.get('average_sleep'))}.</dd></div>
<div><dt>Memory / Attention</dt><dd>{_sentence_case(cognitive.get('memory'))}; {_raw_text(cognitive.get('attention')).lower() if cognitive.get('attention') else NOT_AVAILABLE}.</dd></div>
<div><dt>Daily function</dt><dd>{_sentence_case(qol.get('daily_function'))}</dd></div>
<div><dt>Documented toxicity</dt><dd>{_t(med.get('drug_toxicity'))}</dd></div>
</dl></article>
<article class="clinical-card"><h3>Therapeutic drug monitoring</h3><dl>
{"".join(f"<div><dt>{_t(d.get('drug'))}</dt><dd>{_t(d.get('minimum'))}–{_t(d.get('maximum'))} {_t(d.get('unit'))}; {_t(d.get('interpretation'))}</dd></div>" for d in tdm) if tdm else '<div><dt>Coverage</dt><dd>No renal function (eGFR), hepatic enzyme, or therapeutic drug concentration data is present in the available source reports for this patient.</dd></div>'}
</dl></article>
<article class="clinical-card"><h3>Laboratory &amp; organ safety</h3><dl>
<div><dt>Hematology</dt><dd>{_sentence_case(str(cbc_status).replace('_', ' ')) if cbc_status else NOT_AVAILABLE}.</dd></div>
<div><dt>Kidney</dt><dd>{_t(kidney) if kidney else 'Not evaluated in the available sources.'}</dd></div>
<div><dt>Liver</dt><dd>{_t(liver) if liver else 'Not evaluated in the available sources.'}</dd></div>
</dl></article>
<article class="clinical-card"><h3>EEG, ECG &amp; imaging</h3><dl>
<div><dt>EEG</dt><dd>{_t(_get(eeg, 'recording_statistics', 'total_recordings'))} recordings; EEG-derived risk {_t(eeg_state.get('risk_level'))}, future seizure probability {_t(eeg_state.get('future_seizure_probability'))}.</dd></div>
<div><dt>ECG</dt><dd>{_t(ecg_summary.get('number_of_recordings'))} sessions totaling {_t(ecg_summary.get('total_recorded_duration'))}. {_t(_first_sentence(ecg.get('scope_and_limitations')))} See Signals for the full scope note.</dd></div>
<div><dt>CT</dt><dd>{_t(ct_meta.get('numberOfStudies'))} studies. {_t(_first_sentence(ct.get('comparison')))} See Signals for per-study impressions.</dd></div>
<div><dt>MRI</dt><dd>{_t(mri_summary.get('overall_status'))}. {_t(_first_sentence(mri_summary.get('clinical_impression')))}</dd></div>
</dl></article>
<article class="clinical-card wide"><h3>Integrated risk and protective indicators</h3><div class="risk-strip">
<div class="risk-chip risk-high"><b>Future seizure</b><span>{_t(eeg_state.get('risk_level'))} &middot; EEG synthesis</span></div>
<div class="risk-chip risk-mod"><b>Genetic safety flags</b><span>{len(flags)} total, {len(high_flags)} high</span></div>
<div class="risk-chip risk-low"><b>Hematological status</b><span>{_t(_sentence_case(str(cbc_status).replace('_',' '))) if cbc_status else NOT_AVAILABLE}</span></div>
</div><div class="evidence-note"><b>Clinical interpretation:</b> {_t(seizure.get('overall_inference'))} {_t(qol.get('overall_inference'))}</div></article>
</div>
</div></section>
"""


# ---------------------------------------------------------------------------
# 03 · Disease trajectory
# ---------------------------------------------------------------------------
def _sec_trajectory(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    genetics = sections.get(SEC_GENETICS) or {}
    ddi = sections.get(SEC_DDI) or {}
    progression = _get(clinical_notes, "longitudinal_summary", "clinical_progression", default=[])
    events = _get(clinical_notes, "major_clinical_events", default=[])

    dates, counts = [], []
    for visit in progression:
        m = re.match(r"^(\d+)\s+total reported episodes", str(visit.get("summary", "")))
        if m and visit.get("date"):
            dates.append(visit["date"])
            counts.append(float(m.group(1)))
    labels = [_pretty_date(d)[:6] for d in dates]
    chart = _svg_bar_series(labels, counts) if counts else ""

    items: list[tuple[str, str]] = [(e.get("date"), e.get("event")) for e in events]
    genetics_date = _get(genetics, "patient", "report_date")
    if genetics_date:
        items.append((genetics_date, "Pharmacogenomic (PGx) panel reported."))
    ddi_date = ddi.get("generated_at")
    if ddi_date:
        items.append((ddi_date, "Drug–drug interaction and therapy-evidence assessment generated."))
    items = sorted((i for i in items if i[0]), key=lambda i: i[0])
    events_html = "".join(f'<div class="event"><time>{_pretty_date(d)[:6].upper()}</time><i></i><p>{_t(e)}</p></div>' for d, e in items)

    return f"""
<section class="section" id="trajectory"><div class="shell">
{_section_head("03 · disease trajectory", "Recorded seizure burden and clinical milestones.", "Episode counts from clinical-note visits, shown with the events that mark the observation window.")}
<div class="two"><article class="panel"><h3>Recorded seizure burden</h3>{chart or '<p class="muted small">No per-visit episode counts were present in the clinical-note progression data.</p>'}</article>
<article class="panel"><h3>Milestones</h3><div class="events">{events_html or '<p class="muted small">No major clinical events recorded.</p>'}</div></article></div>
</div></section>
"""


# ---------------------------------------------------------------------------
# 04 · Therapy-level multimodal assessment (DDI_Integration_Plan_v02 core)
# ---------------------------------------------------------------------------
def _pgx_chip_strip(genetics: dict[str, Any]) -> str:
    profile = _get(genetics, "metabolizer_profile", default=[])
    if not isinstance(profile, list) or not profile:
        return ""
    chips = "".join(f'<div class="pgx-gene"><b>{_t(g.get("gene"))}</b><span>{_t(g.get("status"))}</span></div>' for g in profile if isinstance(g, dict))
    return f'<div class="pgx-summary">{chips}</div>'


def _therapy_card(therapy: dict[str, Any]) -> str:
    medication = therapy.get("medication") or {}
    dose = medication.get("dose") or {}
    position = therapy.get("position")
    position_text = _POSITION_STATEMENT.get(position, _humanize(position))

    dose_line = " · ".join(
        p for p in [f"{_fmt_num(dose.get('value'))} {_raw_text(dose.get('unit'))}".strip(), _raw_text(medication.get("frequency")) if medication.get("frequency") else None] if p and p != NOT_AVAILABLE
    )
    monitoring = therapy.get("recommended_monitoring") or []
    monitoring_html = "".join(f'<span class="pill">{_t(m)}</span>' for m in monitoring) if monitoring else '<span class="pill">None recorded.</span>'

    return f"""
<article class="therapy-card"><header><div><small>Current therapy</small><h3>{_t(medication.get('source_name'))}{' · ' + dose_line if dose_line else ''}</h3><p>Status: {_t(medication.get('status'))}</p></div><div class="therapy-position">{_t(position_text)}</div></header>
<div class="therapy-columns">
<div class="evidence-block support"><h4>Supporting evidence</h4><ul>{_evidence_list_html(therapy.get('supporting_evidence'), empty_label='None identified in the available sources.')}</ul></div>
<div class="evidence-block counter"><h4>Counter-evidence</h4><ul>{_evidence_list_html(therapy.get('counter_evidence'), empty_label='None identified in the available sources.')}</ul></div>
<div class="evidence-block unknown"><h4>Unresolved evidence</h4><ul>{_evidence_list_html(therapy.get('unresolved_evidence'), empty_label='None identified in the available sources.')}</ul></div>
</div>
<div class="therapy-impression"><b>Clinical impression:</b> {_t(therapy.get('clinical_impression'))}</div>
<div class="therapy-impression" style="border-top:0;padding-top:0"><b>Recommended monitoring:</b><div class="pill-row">{monitoring_html}</div></div>
</article>
"""


def _sec_therapy_assessment(sections: dict[str, Any]) -> str:
    genetics = sections.get(SEC_GENETICS) or {}
    ddi = sections.get(SEC_DDI) or {}
    therapy_assessments = _get(ddi, "therapy_assessments", default=[])
    proposed = _get(ddi, "proposed_pair_assessments", default=[])

    cards = "".join(_therapy_card(t) for t in therapy_assessments)

    # Per DDI_Integration_Plan_v02, a comparative table for proposed/candidate
    # therapies is rendered only when that data actually exists in the
    # source JSON -- this dataset's normalizer never produces a "proposed"
    # medication (see path_d/ddi/normalizer.py and ddi_summary.py's own
    # `proposed_medications: list = []` comment), so it is omitted entirely
    # rather than rendered empty or invented.
    comparative_html = ""
    if proposed:
        rows = "".join(
            f"<tr><td><b>{_t(p.get('drug_a'))} + {_t(p.get('drug_b'))}</b></td><td>{_t(_humanize(p.get('status')))}</td>"
            f"<td>{_t(p.get('patient_specific_interpretation'))}</td></tr>"
            for p in proposed
        )
        comparative_html = f"""
<div class="subhead"><h3>Comparative assessment of proposed therapies</h3><p>These medicines have not been administered to this patient; their position reflects candidate-pair evidence only.</p></div>
<div class="tablewrap"><table class="alt-assessment"><thead><tr><th>Candidate pair</th><th>Status</th><th>Patient-specific interpretation</th></tr></thead><tbody>{rows}</tbody></table></div>
"""

    return f"""
<section class="section" id="therapy"><div class="shell">
{_section_head("04 · therapy-level multimodal assessment", "Evidence supporting treatment, evidence against it, and unresolved findings.", "Each current medication is assessed against clinical course, EEG physiology, pharmacogenomics, laboratory safety, and drug-interaction evidence.")}
{_pgx_chip_strip(genetics)}
<div class="evidence-note"><b>Interpretive method.</b> A pharmacogenomic association is weighed against this patient's own observed clinical response and laboratory evidence; it is never used as an isolated prescribing instruction, and a favorable genotype cannot override documented toxicity or persistent symptoms.</div>
<div class="subhead"><h3>Current therapy</h3><p>Diagnostic-style adjudication of every medication recorded in the reconciled current regimen.</p></div>
<div class="therapy-stack">{cards or '<p class="muted small">No therapy-level assessment is present in the source DDI report.</p>'}</div>
{comparative_html}
</div></section>
"""


# ---------------------------------------------------------------------------
# 05 · Drug–drug interaction inference
# ---------------------------------------------------------------------------
def _coverage_donut(resolved: int, unresolved: int, not_evaluated: int) -> str:
    total = resolved + unresolved + not_evaluated
    if total == 0:
        return '<div class="donut" style="background:var(--line)"><span class="donut-label">N/A</span></div>'
    pct_resolved = _pct(resolved, total)
    pct_unresolved_end = pct_resolved + _pct(unresolved, total)
    gradient = f"conic-gradient(var(--teal) 0 {pct_resolved:.1f}%, var(--amber) {pct_resolved:.1f}% {pct_unresolved_end:.1f}%, var(--muted) {pct_unresolved_end:.1f}% 100%)"
    return f'<div class="donut" style="background:{gradient}"><span class="donut-label">{pct_resolved:.0f}%</span></div>'


def _sec_ddi(sections: dict[str, Any]) -> str:
    ddi = sections.get(SEC_DDI) or {}
    if not ddi:
        return f"""
<section class="section" id="ddi"><div class="shell">{_section_head("05 · drug–drug interaction inference", "No drug–drug interaction assessment is available for this patient.")}
<div class="panel"><p>A formal DDI screen of the reconciled medication list is recommended before any dose or drug change.</p></div></div></section>
"""

    coverage = _get(ddi, "source_coverage", default={})
    reconciliation = _get(ddi, "medication_reconciliation", default={})
    pair_assessments = _get(ddi, "current_pair_assessments", default=[])
    principal_uncertainties = _get(ddi, "overall_interpretation", "principal_uncertainties", default=[])
    limitations = ddi.get("limitations") or []

    resolved = coverage.get("resolved_drugs") or []
    unresolved = coverage.get("unresolved_drugs") or []
    not_evaluated = coverage.get("not_evaluated_drugs") or []
    donut = _coverage_donut(len(resolved), len(unresolved), len(not_evaluated))

    interactions_detected = sum(1 for p in pair_assessments if p.get("status") == "interaction_detected")
    evidence_items = sum(len(p.get("evidence") or []) for p in pair_assessments)
    unresolved_evidence = sum(1 for p in pair_assessments for e in (p.get("evidence") or []) if e.get("direction") == "unresolved")
    stats = [
        (str(len(pair_assessments)), "current-current pair(s) evaluated"),
        (str(interactions_detected), "interaction(s) detected"),
        (str(evidence_items), "evidence item(s) reviewed"),
        (str(unresolved_evidence), "unresolved evidence item(s)"),
    ]
    stats_html = "".join(f'<div class="mini"><b>{_t(v)}</b><span>{_t(l)}</span></div>' for v, l in stats)

    pair_html = "".join(
        f'<p style="font-size:12px;margin:6px 0"><b>{_t(p.get("drug_a"))} + {_t(p.get("drug_b"))}:</b> {_t(p.get("patient_specific_interpretation"))}</p>'
        for p in pair_assessments
    )

    name_by_source = {m.get("source_name"): m.get("normalized_name") for m in reconciliation.get("normalized_medications") or []}
    therapy_by_normalized = {(t.get("medication") or {}).get("normalized_name"): t for t in ddi.get("therapy_assessments") or []}

    def _status_row(source_name: str) -> str:
        normalized = name_by_source.get(source_name)
        if normalized in resolved:
            tag, meaning = "ok", f"Curated CYP/pharmacokinetic reference data exists for this drug ({_raw_text(coverage.get('flockhart_source'))})."
        elif normalized in unresolved:
            tag, meaning = "gap", "No curated CYP/pharmacokinetic reference entry for this drug; screening relies on pharmacodynamic rules and pharmacogenomic findings only."
        elif normalized in not_evaluated:
            tag, meaning = "gap", "Not present in the curated reference or pharmacogenomic findings; no automated interaction evidence is available."
        else:
            tag, meaning = "gap", "Coverage status not determined by this assessment."
        therapy = therapy_by_normalized.get(normalized)
        action = (therapy.get("recommended_monitoring") or [None])[0] if therapy else "Review current-current pair evidence below before adding an interacting medication."
        label = {"ok": "Resolved", "gap": "Unresolved"}[tag]
        return f'<tr><td><b>{_t(source_name)}</b></td><td><span class="tag {tag}">{label}</span></td><td>{_t(meaning)}</td><td>{_t(action)}</td></tr>'

    coverage_rows = "".join(_status_row(m.get("source_name")) for m in reconciliation.get("normalized_medications") or [])

    uncertainty_html = "".join(f"<li>{_t(u)}</li>" for u in principal_uncertainties)

    return f"""
<section class="section" id="ddi"><div class="shell">
{_section_head("05 · drug–drug interaction inference", "Curated pharmacokinetic, pharmacodynamic, and pharmacogenomic screening of the current regimen.", f"{len(reconciliation.get('normalized_medications') or [])} current medication(s) reconciled · {_raw_text(coverage.get('flockhart_source'))} ({_raw_text(coverage.get('flockhart_version'))}).")}
<div class="ddi-grid">
<article class="panel"><h3>Curated-source coverage</h3><div class="coverage">{donut}<div class="legend"><div><b>{len(resolved)} resolved</b> in the curated reference source</div><div><b>{len(unresolved) + len(not_evaluated)} unresolved / not evaluated</b></div><p>Predicted-model (SuperCYPsPred) coverage is not available locally for any drug in this regimen.</p></div></div></article>
<article class="panel"><h3>Current-current pair output</h3><div class="stats">{stats_html}</div>{pair_html}</article>
</div>
<article class="panel infer"><h3>Clinical inference</h3><p class="big"><strong>Absence of a resolved current-current interaction does not establish absence of interaction.</strong> Unresolved predicted-model coverage limits interpretation; the active medication list should be reassessed against a complete interaction source before any treatment change.</p>
<ul class="notes">{uncertainty_html}</ul></article>
<h3 style="margin:28px 0 11px">Current regimen &mdash; curated-source coverage</h3>
<div class="tablewrap"><table><thead><tr><th>Medicine</th><th>Status</th><th>Meaning</th><th>Action</th></tr></thead><tbody>{coverage_rows}</tbody></table></div>
<div class="evidence-note" style="margin-top:14px"><b>Limitations of this screen:</b> {' '.join(_t(item) for item in limitations)}</div>
</div></section>
"""


# ---------------------------------------------------------------------------
# 06 · Pharmacogenomics (external evidence)
# ---------------------------------------------------------------------------
def _sec_pharmacogenomics(sections: dict[str, Any]) -> str:
    genetics = sections.get(SEC_GENETICS) or {}
    external = sections.get(SEC_EXTERNAL_EVIDENCE) or {}

    current_findings = []
    for entry in _get(genetics, "findings_by_therapeutic_class", "mood_stabilizers_antiepileptics", default=[]):
        drug = str(entry.get("drug", "")).lower()
        if any(d in drug for d in CURRENT_REGIMEN_DRUG_NAMES):
            current_findings.append(entry)
    worst_findings = _pgx_worst_per_drug(current_findings)

    pairs_by_key = {
        (str(p.get("drug_name", "")).strip().lower(), str(p.get("gene_symbol", "")).strip().upper()): p
        for p in (external.get("by_gene_drug_pair") or [])
    }

    rows = []
    for finding in worst_findings:
        drug = str(finding.get("drug") or "").strip()
        gene = _gene_from_basis(finding.get("genetic_basis"))
        pair = pairs_by_key.get((drug.lower(), gene))
        if not pair:
            continue
        cpic, clinvar, pubmed = pair.get("cpic"), pair.get("clinvar"), pair.get("pubmed_combined_query")
        rows.append(
            f"<tr><td><b>{_t(gene)}</b> &rarr; {_t(_sentence_case(drug))}<br><span class=\"src\">{_t(finding.get('genetic_basis'))}: {_t(_sentence_case(finding.get('predicted_effect')))}</span></td>"
            f"<td>{_v2_cpic_cell(cpic)}</td><td>{_v2_clinvar_cell(clinvar)}</td><td>{_v2_pubmed_cell(pubmed)}</td></tr>"
        )
    table_html = (
        f'<div class="tablewrap"><table><thead><tr><th>Gene &rarr; Drug (patient-specific pair)</th><th>CPIC</th><th>ClinVar</th><th>PubMed</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        if rows
        else '<div class="panel"><p>No current-regimen gene-drug pair from the patient panel matches an entry in the live external-evidence report.</p></div>'
    )

    limitations = external.get("limitations") or []
    limitations_html = "".join(f"<li>{_t(item)}</li>" for item in limitations)

    return f"""
<section class="section" id="pgx"><div class="shell">
{_section_head("06 · pharmacogenomic external evidence", f"{_raw_text(_get(genetics, 'patient', 'variants_analyzed'))} variants across {_raw_text(_get(genetics, 'patient', 'drugs_covered'))} medications · reported {_pretty_date(_get(genetics, 'patient', 'report_date'))}.", "Live-verified CPIC / ClinVar / PubMed evidence for the gene-drug pairs relevant to the current regimen.")}
{_pgx_chip_strip(genetics)}
<div class="evidence-note" style="margin-bottom:14px"><b>Database evidence only, not patient-verified:</b> each row below is a live public-database lookup for the same gene/drug pair as the patient-specific finding in the panel above &mdash; it establishes general scientific plausibility, not a fact confirmed for this individual patient. An unresolved result means this exact query returned nothing, or failed, at the time checked; it is not evidence that no relevant data exists.</div>
{table_html}
<div class="evidence-note" style="margin-top:14px"><b>Known limitations (external evidence):</b><ul class="notes">{limitations_html}</ul></div>
</div></section>
"""


def _v2_provenance(envelope: dict[str, Any], *, id_label: str, record_id: Any) -> str:
    retrieved = _pretty_date(envelope["retrieved_at"]) if envelope.get("retrieved_at") else NOT_AVAILABLE
    return f'<div class="src">{id_label} {_t(record_id)} &middot; Retrieved {retrieved}</div>'


def _v2_cpic_cell(cpic: dict[str, Any] | None) -> str:
    if not cpic:
        return NOT_AVAILABLE
    status = cpic.get("status")
    if status == "ok" and cpic.get("records"):
        rec = cpic["records"][0]
        guideline = rec.get("guideline")
        has_guideline = bool(guideline and guideline.get("url"))
        detail = f'<a href="{_raw_text(guideline.get("url"))}">{_t(guideline.get("name"))}</a>' if has_guideline else "No active CPIC dosing guideline for this pair"
        tag = '<span class="tag ok">Active guideline</span>' if has_guideline else '<span class="tag gap">No guideline</span>'
        return f"{tag} Level {_t(rec.get('cpic_level'))}<br>{detail}{_v2_provenance(cpic, id_label='DrugID', record_id=rec.get('drugid'))}"
    if status == "no_results":
        return f'<span class="tag gap">Unresolved</span> <span class="src">{_t(cpic.get("note")) or "No CPIC gene-drug pair on record."}</span>'
    return f'<span class="tag gap">Unresolved</span> <span class="src">{_t(cpic.get("note") or cpic.get("error"))}</span>' if cpic.get("error") else NOT_AVAILABLE


def _v2_clinvar_cell(clinvar: dict[str, Any] | None) -> str:
    if not clinvar:
        return NOT_AVAILABLE
    status = clinvar.get("status")
    if status == "ok" and clinvar.get("records"):
        rec = clinvar["records"][0]
        total = clinvar.get("total_matches_in_clinvar")
        return (
            f'<a href="{_raw_text(rec.get("url"))}">{_t(rec.get("accession"))}</a>: {_t(rec.get("clinical_significance")) or "Not classified"}'
            f'<br><span class="src">{_t(total)} total gene-wide ClinVar record(s), not variant-specific</span>'
            f'{_v2_provenance(clinvar, id_label="Accession", record_id=rec.get("accession") or rec.get("uid"))}'
        )
    if status == "no_results":
        return '<span class="tag gap">Unresolved</span> <span class="src">No ClinVar record matched this exact gene query at the time checked.</span>'
    return NOT_AVAILABLE


def _v2_pubmed_cell(pubmed: dict[str, Any] | None) -> str:
    if not pubmed:
        return NOT_AVAILABLE
    status = pubmed.get("status")
    if status == "ok" and pubmed.get("records"):
        rec = pubmed["records"][0]
        title = _t(rec.get("title")) or "(title not returned by PubMed)"
        return f'<a href="{_raw_text(rec.get("url"))}">PMID {_t(rec.get("pmid"))}</a>: {title}{_v2_provenance(pubmed, id_label="PMID", record_id=rec.get("pmid"))}'
    if status == "no_results":
        return '<span class="tag gap">Unresolved</span> <span class="src">No PubMed citation matched this exact query at the time checked.</span>'
    return NOT_AVAILABLE


# ---------------------------------------------------------------------------
# 07 · Multimodal clinical graphs (EEG / ECG / CBC / MRI-CT)
# ---------------------------------------------------------------------------
def _sec_signals(sections: dict[str, Any]) -> str:
    eeg = sections.get(SEC_EEG) or {}
    ecg = sections.get(SEC_ECG) or {}
    cbc = sections.get(SEC_CBC) or {}
    ct = sections.get(SEC_CT) or {}
    mri = sections.get(SEC_MRI) or {}

    sig = _get(eeg, "signal_statistics", default={})
    phase_order = ["interictal", "preictal", "ictal"]
    eeg_labels, eeg_values = [], []
    for p in phase_order:
        vr = _get(sig, p, "variance_range", default=None)
        if isinstance(vr, list) and len(vr) == 2:
            eeg_labels.append(p.capitalize())
            eeg_values.append(sum(vr) / 2)
    eeg_chart = _svg_bar_series(eeg_labels, eeg_values, value_fmt="{:.0f}") if eeg_values else ""

    amp = ecg.get("amplitude_statistics_uV") or []
    ecg_labels = [_pretty_date(a.get("date"))[:6] for a in amp]
    ecg_values = [a.get("std_dev") for a in amp if isinstance(a.get("std_dev"), (int, float))]
    ecg_chart = _svg_line_series(ecg_labels, ecg_values) if len(ecg_values) >= 2 else ""

    trends = _get(cbc, "longitudinal_trends", default={})
    cbc_rows = "".join(
        f"<tr><td>{_t(e.get('parameter'))}</td><td>{_t(_fmt_num(e.get('first_value')))}</td><td>{_t(_fmt_num(e.get('latest_value')))}</td>"
        f"<td>{_t(_fmt_num(e.get('minimum_value')))}–{_t(_fmt_num(e.get('maximum_value')))}</td><td>{_t(e.get('trend_direction'))}</td></tr>"
        for e in trends.values()
    )

    ct_studies = ct.get("studies") or []
    ct_rows = "".join(f"<tr><td>{_pretty_date(s.get('studyDate'))}</td><td>{_t(s.get('impression'))}</td></tr>" for s in ct_studies)
    mri_summary = _get(mri, "mri_summary", default={})
    mri_findings = _get(mri, "structural_findings", default={})
    mri_rows = "".join(
        f"<tr><td>{_humanize(k)}</td><td>{_t(v.get('status'))}</td><td>{_t(v.get('finding'))}</td></tr>"
        for k, v in mri_findings.items()
        if isinstance(v, dict)
    )

    return f"""
<section class="section" id="signals"><div class="shell">
{_section_head("07 · multimodal clinical graphs", "EEG, ECG, and blood trends.", "Graphical views are derived directly from the source JSON and show signal evolution without implying diagnostic conclusions the underlying single-channel/single-lead data cannot support.")}
<div class="chart-grid">
<article class="chart-card"><div class="chart-head"><div><h3>EEG variance midpoint by seizure state</h3><p>Signal-variance midpoint per recorded phase.</p></div><span class="chart-meta">{_t(_get(eeg, 'recording_statistics', 'total_recordings'))} recordings</span></div>{eeg_chart or '<p class="chart-note">Insufficient phase-level variance data to chart.</p>'}<p class="chart-note">{_t(_get(eeg, 'overall_observation', 'clinical_interpretation'))}</p></article>
<article class="chart-card"><div class="chart-head"><div><h3>ECG amplitude variability</h3><p>Amplitude standard deviation by recording date.</p></div><span class="chart-meta">{_t(_get(ecg, 'dataset_summary', 'number_of_recordings'))} sessions</span></div>{ecg_chart or '<p class="chart-note">Insufficient recordings to chart a trend.</p>'}<p class="chart-note">{_t(ecg.get('scope_and_limitations'))}</p></article>
<article class="chart-card wide"><div class="chart-head"><div><h3>Blood-test trends</h3><p>Longitudinal CBC parameters across the observation window.</p></div><span class="chart-meta">{_t(_get(cbc, 'observation_window', 'number_of_reports'))} reports</span></div>
<div class="tablewrap"><table><thead><tr><th>Parameter</th><th>First</th><th>Latest</th><th>Range</th><th>Trend</th></tr></thead><tbody>{cbc_rows}</tbody></table></div></article>
<article class="chart-card wide"><div class="chart-head"><div><h3>CT studies</h3></div></div><div class="tablewrap"><table><thead><tr><th>Date</th><th>Impression</th></tr></thead><tbody>{ct_rows or '<tr><td colspan="2">No CT studies recorded.</td></tr>'}</table></div><p class="chart-note">{_t(ct.get('comparison'))}</p></article>
<article class="chart-card wide"><div class="chart-head"><div><h3>MRI structural findings</h3></div></div><div class="tablewrap"><table><thead><tr><th>Finding</th><th>Status</th><th>Detail</th></tr></thead><tbody>{mri_rows or '<tr><td colspan="3">No structural findings recorded.</td></tr>'}</table></div><p class="chart-note">{_t(mri_summary.get('clinical_impression'))}</p></article>
</div>
</div></section>
"""


# ---------------------------------------------------------------------------
# 08 · Roadmap (clinical action plan)
# ---------------------------------------------------------------------------
def _sec_roadmap(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    mri = sections.get(SEC_MRI) or {}
    genetics = sections.get(SEC_GENETICS) or {}
    ddi = sections.get(SEC_DDI) or {}
    eeg = sections.get(SEC_EEG) or {}

    priority_actions = (genetics.get("clinical_conclusion") or {}).get("recommendations") or []
    recommended_actions = _get(clinical_notes, "digital_twin_state", "recommended_actions", default=[])
    monitoring = _get(clinical_notes, "digital_twin_state", "monitoring_priorities", default=[])
    eeg_monitoring = eeg.get("recommended_monitoring") or []
    merged_monitoring = list(dict.fromkeys([*monitoring, *eeg_monitoring]))
    mri_follow_up = _get(mri, "recommendations", "follow_up", default=[])
    conflicts = _get(ddi, "medication_reconciliation", "conflicts", default=[])

    def _li(items: list[str]) -> str:
        return "".join(f"<li>{_t(i)}</li>" for i in items) or '<li class="muted">None recorded.</li>'

    reconciliation_items = [_raw_text(c.get("details")) for c in conflicts] or [
        "Current regimen reconciled against curated reference pharmacology and this patient's own pharmacogenomic findings; no dose or frequency conflict was found across the reconciled source records."
    ]
    priority_items = [f"{_raw_text(r.get('action'))} ({_raw_text(r.get('priority'))} priority): {_raw_text(r.get('detail'))}" for r in priority_actions]

    return f"""
<section class="section" id="roadmap"><div class="shell">
{_section_head("08 · clinical action plan", "Sequence the next review around safety and evidence.", "Medication changes remain the responsibility of the treating clinician.")}
<div class="road">
<article class="phase"><header><span>Now &middot; reconcile</span><h3>Verify before changing</h3></header><ul>{_li(reconciliation_items)}</ul></article>
<article class="phase"><header><span>Near term &middot; optimize</span><h3>Monitor the right signals</h3></header><ul>{_li([*recommended_actions, *merged_monitoring])}</ul></article>
<article class="phase"><header><span>Pharmacogenomics</span><h3>Priority actions</h3></header><ul>{_li(priority_items)}</ul></article>
</div>
<div class="evidence-note" style="margin-top:14px"><b>MRI follow-up:</b> {" ".join(_t(i) for i in mri_follow_up) if mri_follow_up else "No MRI follow-up items were recommended in the source report."}</div>
</div></section>
"""


# ---------------------------------------------------------------------------
# 09 · Provenance & limitations
# ---------------------------------------------------------------------------
def _sec_notes(sections: dict[str, Any], manifest: dict[str, Any]) -> str:
    external = sections.get(SEC_EXTERNAL_EVIDENCE) or {}
    ddi = sections.get(SEC_DDI) or {}
    sources = manifest.get("sources") or {}
    source_items = "".join(f"<li><code>{_t(v)}</code></li>" for v in sources.values())
    ddi_limitations = "".join(f"<li>{_t(i)}</li>" for i in (ddi.get("limitations") or []))
    external_limitations = "".join(f"<li>{_t(i)}</li>" for i in (external.get("limitations") or []))

    return f"""
<section class="section" id="notes"><div class="shell">
{_section_head("09 · provenance & limitations", "Read the evidence with its boundaries.")}
<div class="two">
<article class="panel"><h3>Supplied sources</h3><ul class="notes">{source_items}</ul></article>
<article class="panel"><h3>Assessment limitations</h3><ul class="notes">{ddi_limitations}{external_limitations}</ul></article>
</div>
<div class="disclaimer"><b>Clinical-use disclaimer.</b> This is decision support, not a prescription. It does not replace medication reconciliation, a validated interaction database, diagnostic EEG/ECG interpretation, radiology review, or clinician judgment. Do not start, stop, or change medication based on this report alone.</div>
</div></section>
"""


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def _build_html(digital_twin: dict[str, Any], sections: dict[str, Any], manifest: dict[str, Any]) -> str:
    body_parts = [
        _sec_hero(digital_twin, sections, manifest),
        _sec_snapshot(sections),
        _sec_clinical_profile(sections),
        _sec_trajectory(sections),
        _sec_therapy_assessment(sections),
        _sec_ddi(sections),
        _sec_pharmacogenomics(sections),
        _sec_signals(sections),
        _sec_roadmap(sections),
        _sec_notes(sections, manifest),
    ]
    body = "\n".join(body_parts)
    generated_at = _t(digital_twin.get("generated_at"))
    return f"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">
<title>{_t(DOC_TITLE)}</title>
<style>{_CSS}</style>
</head>
<body>
{body}
</main><footer><div class="shell">Neuro Digital Twin integrated epilepsy and DDI report &middot; generated {generated_at} &middot; source-preserving synthesis with explicit uncertainty.</div></footer>
<script>
const b=document.getElementById("theme"),s=localStorage.getItem("ndt-theme");if(s)document.documentElement.dataset.theme=s;b.onclick=()=>{{const n=document.documentElement.dataset.theme==="dark"?"light":"dark";document.documentElement.dataset.theme=n;localStorage.setItem("ndt-theme",n)}}
</script>
</body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
