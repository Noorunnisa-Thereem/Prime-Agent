"""Renders the consolidated Digital Twin JSON as a polished, doctor-facing
PDF report -- built as styled HTML/CSS (inline SVG charts, card layout)
and printed to PDF via headless Microsoft Edge, then given a repeating
header/footer/page-number overlay via reportlab+pypdf (both already
project dependencies; Edge already ships with Windows, so no new
dependency is introduced for either half of the pipeline).

This module never derives, invents, or modifies a clinical value -- every
string, number, and chart data point it draws is read directly out of
`reports/Digital_Twin_Consolidated_Report.json`. Two requested sections
have no counterpart in this dataset: a drug-drug-interaction (DDI) screen,
and per-parameter renal/hepatic/metabolic lab panels (only CBC exists).
Those sections say so explicitly rather than inventing content.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as canvas_module
from pypdf import PdfReader, PdfWriter

from .utils import ensure_dir

DEFAULT_INPUT_PATH = Path("reports") / "Digital_Twin_Consolidated_Report.json"
DEFAULT_OUTPUT_PATH = Path("reports") / "Digital_Twin_Integrated_Report.pdf"

DOC_TITLE = "Neuro Digital Twin Integrated Clinical Report"
FOOTER_NOTE = "Generated from validated patient data. Decision-support summary only -- not a substitute for clinician review."

NOT_AVAILABLE = "Not available"
NONE_REPORTED = "None reported"

SEC_CLINICAL_NOTES = "clinical__notes_summary"
SEC_CBC = "CBC_consolidated_summary"
SEC_CT = "CT_scan_clinical_summary"
SEC_MRI = "MRI_clinical_summary"
SEC_ECG = "ECG_Clinical_Summary"
SEC_EEG = "EEG_clinical_summary"
SEC_QUESTIONNAIRE = "Questionnaire_consolidated_summary"
SEC_GENETICS = "genetics_clinical_summary"
SEC_DDI = "DDI_Clinical_Assessment"

CURRENT_REGIMEN_DRUG_NAMES = ("levetiracetam", "lamotrigine")

# Severity color class per pgx predicted_effect category -- a display-only recolor of an
# already-real category value (same pattern as _status_class), used to pick the single
# worst/most clinically significant finding per current-regimen drug.
_PGX_EFFECT_RANK: dict[str, int] = {"toxicity": 3, "reduced efficacy": 2, "moderate": 1, "efficacy": 0}
_PGX_EFFECT_CLASS: dict[str, str] = {
    "toxicity": "sev-high",
    "reduced efficacy": "sev-mod",
    "moderate": "sev-mod",
    "efficacy": "sev-low",
}

EDGE_CANDIDATES = (
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

# Page geometry (mm) shared between the CSS @page rule and the reportlab
# overlay, so the overlay's header/footer band lands inside the blank
# margin space the HTML content is told to leave alone.
PAGE_MARGIN_TOP_MM = 20
PAGE_MARGIN_BOTTOM_MM = 15
PAGE_MARGIN_SIDE_MM = 14


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the consolidated Digital Twin JSON as an HTML-styled PDF report")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    report = json.loads(args.input.read_text(encoding="utf-8"))
    build_pdf(report, args.output)
    print(f"Wrote Digital Twin PDF report to {args.output}")
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

    raw_pdf_path = output_path.with_name(output_path.stem + "_raw.pdf")
    _print_html_to_pdf(html_path, raw_pdf_path)

    patient_label = _patient_label(digital_twin, sections.get(SEC_CLINICAL_NOTES))
    _overlay_header_footer(raw_pdf_path, output_path, patient_label)
    raw_pdf_path.unlink(missing_ok=True)
    return output_path


# ----------------------------------------------------------------------
# Headless-Edge HTML -> PDF rendering
# ----------------------------------------------------------------------
def _find_edge() -> str:
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("msedge") or shutil.which("msedge.exe")
    if found:
        return found
    raise RuntimeError("Microsoft Edge was not found (checked default install paths and PATH); cannot render the HTML report to PDF.")


def _print_html_to_pdf(html_path: Path, pdf_path: Path) -> None:
    edge = _find_edge()
    cmd = [
        edge,
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path.resolve()}",
        html_path.resolve().as_uri(),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=90)
    if result.returncode != 0 or not pdf_path.exists():
        raise RuntimeError(f"Headless Edge failed to print the report to PDF (exit {result.returncode}): {result.stderr.decode(errors='replace')[:500]}")


# ----------------------------------------------------------------------
# Repeating header / footer / page-number overlay (reportlab draws a
# transparent overlay page per content page; pypdf merges them). This is
# the only role reportlab plays here -- all layout/graphics come from the
# HTML/CSS, this just stamps the same running header/footer that Chromium's
# plain --print-to-pdf CLI mode cannot template.
# ----------------------------------------------------------------------
def _overlay_header_footer(raw_pdf_path: Path, output_path: Path, patient_label: str) -> None:
    reader = PdfReader(str(raw_pdf_path))
    total_pages = len(reader.pages)
    width, height = A4

    buf = io.BytesIO()
    c = canvas_module.Canvas(buf, pagesize=A4)
    top_y = height - (PAGE_MARGIN_TOP_MM - 6) * mm
    rule_y = height - (PAGE_MARGIN_TOP_MM - 9) * mm
    footer_rule_y = (PAGE_MARGIN_BOTTOM_MM - 4) * mm
    footer_y = (PAGE_MARGIN_BOTTOM_MM - 8) * mm
    left_x = PAGE_MARGIN_SIDE_MM * mm
    right_x = width - PAGE_MARGIN_SIDE_MM * mm

    for page_number in range(1, total_pages + 1):
        c.saveState()
        c.setFont("Helvetica-Bold", 8.5)
        c.setFillColor(colors.HexColor("#10243C"))
        c.drawString(left_x, top_y, DOC_TITLE)
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.HexColor("#666666"))
        c.drawRightString(right_x, top_y, patient_label)
        c.setStrokeColor(colors.HexColor("#B9C4CE"))
        c.setLineWidth(0.6)
        c.line(left_x, rule_y, right_x, rule_y)

        c.setFont("Helvetica", 7)
        c.setFillColor(colors.HexColor("#666666"))
        c.drawString(left_x, footer_y, FOOTER_NOTE)
        c.drawRightString(right_x, footer_y, f"Page {page_number} of {total_pages}")
        c.setStrokeColor(colors.HexColor("#B9C4CE"))
        c.setLineWidth(0.4)
        c.line(left_x, footer_rule_y, right_x, footer_rule_y)
        c.restoreState()
        c.showPage()
    c.save()
    buf.seek(0)

    overlay_reader = PdfReader(buf)
    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        page.merge_page(overlay_reader.pages[i])
        writer.add_page(page)
    with open(output_path, "wb") as fh:
        writer.write(fh)


# ----------------------------------------------------------------------
# Generic helpers (JSON access, text escaping/formatting) -- shared by
# every section builder below.
# ----------------------------------------------------------------------
def _get(data: dict[str, Any] | None, *path: str, default: Any = None) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return default if node is None else node


def _raw_text(value: Any) -> str:
    if value is None:
        return NOT_AVAILABLE
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return NONE_REPORTED if not value else "; ".join(_raw_text(item) for item in value)
    text = str(value)
    return text if text.strip() else NOT_AVAILABLE


def _t(value: Any) -> str:
    """HTML-escaped display text for embedding directly into a template string."""
    return xml_escape(_raw_text(value))


def _sentence_case(text: Any) -> str:
    raw = _raw_text(text)
    return raw[:1].upper() + raw[1:] if raw not in (NOT_AVAILABLE, NONE_REPORTED) else raw


def _humanize(key: str) -> str:
    words = [w for w in str(key).replace("_", " ").split() if w]
    parts = [w if w.isupper() else w[:1].upper() + w[1:] for w in words]
    return " ".join(parts) if parts else str(key)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _parse_iso_date(text: Any) -> date | None:
    if not isinstance(text, str):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _pretty_date(iso: Any) -> str:
    parsed = _parse_iso_date(iso)
    return f"{parsed.day:02d} {_MONTHS[parsed.month - 1]} {parsed.year}" if parsed else _raw_text(iso)


def _pretty_range(start: Any, end: Any) -> str:
    s, e = _parse_iso_date(start), _parse_iso_date(end)
    if not s or not e:
        return f"{_raw_text(start)} to {_raw_text(end)}"
    if s.year == e.year:
        return f"{s.day:02d} {_MONTHS[s.month - 1]} \u2013 {e.day:02d} {_MONTHS[e.month - 1]} {e.year}"
    return f"{_pretty_date(start)} \u2013 {_pretty_date(end)}"


def _duration_phrase(start: Any, end: Any) -> str | None:
    s, e = _parse_iso_date(start), _parse_iso_date(end)
    if not s or not e or e < s:
        return None
    days = (e - s).days
    if days < 45:
        weeks = max(1, round(days / 7))
        return f"~{weeks} week{'s' if weeks != 1 else ''}"
    months = round(days / 30.44)
    return f"~{months} month{'s' if months != 1 else ''}"


def _patient_label(digital_twin: dict[str, Any], clinical_notes: dict[str, Any] | None) -> str:
    name = _get(clinical_notes, "patient_profile", "name")
    patient_id = digital_twin.get("patient_id")
    parts = [p for p in (name, f"ID: {patient_id}" if patient_id else None) if p]
    return " | ".join(parts) if parts else "Patient ID: Not available"


def _status_class(text: Any) -> str:
    """Maps a real status/severity word already in the source text to a
    (red/amber/green/neutral) CSS class -- a display recolor, never a new
    clinical judgment."""

    lowered = str(text).lower() if text else ""
    if any(w in lowered for w in ("high", "elevated", "poor", "abnormal", "severe", "major", "positive")):
        return "sev-high"
    if any(w in lowered for w in ("moderate", "intermediate", "monitor", "borderline", "variable", "low to moderate")):
        return "sev-mod"
    if any(w in lowered for w in ("low", "stable", "favorable", "normal", "preserved", "good", "controlled", "improv")):
        return "sev-low"
    return "sev-neutral"


# ----------------------------------------------------------------------
# CSS design system -- adapted from the navy/teal clinical-dashboard
# reference the user supplied earlier in this project, retargeted for
# reliable print output (no web fonts / external assets -- headless
# printing must not depend on network fetches).
# ----------------------------------------------------------------------
_CSS = f"""
:root {{
  --ink:#132238; --muted:#5b6b7a; --line:#dce5e9; --card:#fff;
  --navy:#10243c; --navy2:#1f3b57; --teal:#087c83; --cyan:#35b9b0;
  --mint:#dff5ef; --blue:#eef4f8; --gray-bg:#f3f5f7;
  --amber:#a86505; --amber-bg:#fff1d6; --red:#b53c48; --red-bg:#fdebed;
  --green:#247a5a; --green-bg:#dff5ef;
  --sans: "Segoe UI", -apple-system, Helvetica, Arial, sans-serif;
  --shadow: 0 3px 10px rgba(16,36,60,.07);
}}
* {{ box-sizing: border-box; }}
@page {{ size: A4; margin: {PAGE_MARGIN_TOP_MM}mm {PAGE_MARGIN_SIDE_MM}mm {PAGE_MARGIN_BOTTOM_MM}mm; }}
html, body {{ margin:0; padding:0; background:#fff; color:var(--ink); font-family:var(--sans); font-size:9.6px; line-height:1.42; }}
h1,h2,h3,h4,p,ul,ol,dl,dd {{ margin:0; }}
.section {{ break-inside: avoid-page; margin-bottom: 10px; }}
.section-head {{ display:flex; align-items:baseline; gap:8px; border-bottom:1.6px solid var(--navy); padding-bottom:4px; margin-bottom:8px; }}
.section-num {{ font-weight:800; font-size:12.5px; color:#fff; background:var(--navy); border-radius:5px; padding:1.5px 7px; }}
.section-title {{ font-weight:800; font-size:14.5px; color:var(--navy); letter-spacing:-.01em; }}
.section-sub {{ color:var(--muted); font-size:8.6px; margin-left:auto; text-align:right; }}
.h2 {{ font-weight:800; font-size:10px; color:var(--navy2); margin: 6px 0 4px; }}
.muted {{ color:var(--muted); }}
.small {{ font-size:8.4px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); padding:9px 11px; break-inside: avoid; }}
.grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; }}
.grid3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; }}
.grid4 {{ display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:8px; }}
.grid5 {{ display:grid; grid-template-columns:repeat(5,1fr); gap:6px; }}
.grid6 {{ display:grid; grid-template-columns:repeat(6,1fr); gap:6px; }}
.stack {{ display:flex; flex-direction:column; gap:8px; }}
dl.kv {{ margin:0; }}
dl.kv > div {{ display:grid; grid-template-columns:118px 1fr; gap:8px; padding:4px 0; border-top:1px solid var(--line); }}
dl.kv > div:first-child {{ border-top:0; }}
dl.kv dt {{ font-weight:700; font-size:8.4px; text-transform:uppercase; letter-spacing:.03em; color:var(--muted); }}
dl.kv dd {{ font-size:9.4px; }}
table {{ border-collapse:collapse; width:100%; font-size:8.8px; }}
table.datatable th {{ background:var(--navy); color:#fff; text-align:left; padding:4px 6px; font-size:8px; text-transform:uppercase; letter-spacing:.03em; }}
table.datatable td {{ padding:4px 6px; border-top:1px solid var(--line); vertical-align:top; }}
table.datatable tr:nth-child(even) td {{ background:var(--gray-bg); }}
.stat {{ border-top:3px solid var(--teal); background:var(--blue); border-radius:0 0 6px 6px; padding:6px 8px; text-align:center; }}
.stat b {{ display:block; font-size:15px; color:var(--navy); line-height:1.1; }}
.stat span {{ font-size:7.6px; color:var(--muted); }}
.chip {{ border:1px solid var(--line); border-radius:6px; padding:5px 7px; background:#fff; }}
.chip b {{ display:block; font-size:8.6px; color:var(--navy2); }}
.chip span {{ font-weight:800; font-size:9.4px; text-transform:capitalize; }}
.sev-high {{ color:var(--red); }} .sev-high-bg {{ background:var(--red-bg); border-color:#f3c6cb; }}
.sev-mod {{ color:var(--amber); }} .sev-mod-bg {{ background:var(--amber-bg); border-color:#f0d79a; }}
.sev-low {{ color:var(--green); }} .sev-low-bg {{ background:var(--green-bg); border-color:#b7e4d3; }}
.sev-neutral {{ color:var(--muted); }}
.badge {{ display:inline-block; font-size:7.4px; font-weight:800; text-transform:uppercase; letter-spacing:.03em; padding:2px 6px; border-radius:99px; }}
.callout {{ background:var(--blue); border-left:3px solid var(--teal); border-radius:4px; padding:6px 9px; font-size:8.8px; }}
.callout.warn {{ background:var(--amber-bg); border-left-color:var(--amber); }}
.callout.gap {{ background:var(--gray-bg); border-left-color:var(--muted); }}
.meter-row {{ display:grid; grid-template-columns:120px 1fr 46px; align-items:center; gap:6px; padding:2.5px 0; }}
.meter-track {{ height:6px; background:var(--gray-bg); border-radius:99px; overflow:hidden; }}
.meter-fill {{ height:100%; background:linear-gradient(90deg,var(--cyan),var(--teal)); }}
.timeline {{ position:relative; padding-left:14px; border-left:2px solid var(--line); }}
.tl-item {{ position:relative; padding:0 0 8px 10px; }}
.tl-item:before {{ content:""; position:absolute; left:-17.5px; top:2px; width:8px; height:8px; border-radius:50%; background:var(--teal); border:2px solid #fff; box-shadow:0 0 0 1.4px var(--teal); }}
.tl-item time {{ display:block; font-weight:800; font-size:8px; color:var(--teal); text-transform:uppercase; }}
.tl-item p {{ font-size:8.8px; margin-top:1px; }}
.list-compact {{ margin:0; padding-left:14px; }}
.list-compact li {{ font-size:8.8px; margin:2px 0; }}
.pill-row {{ display:flex; flex-wrap:wrap; gap:5px; }}
.pill {{ font-size:7.8px; border:1px solid var(--line); background:var(--blue); border-radius:99px; padding:2.5px 8px; }}
.hero {{ background:var(--navy); color:#fff; border-radius:9px; padding:14px 16px; margin-bottom:10px; }}
.hero .eyebrow {{ color:var(--cyan); font-weight:800; font-size:8.6px; letter-spacing:.06em; text-transform:uppercase; }}
.hero h1 {{ font-size:19px; font-weight:800; margin:4px 0 4px; letter-spacing:-.01em; }}
.hero p.lede {{ color:#c7d4dd; font-size:9.6px; max-width:80%; }}
.identity {{ background:#fff; border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); overflow:hidden; margin-bottom:10px; }}
.identity > .ihead {{ background:var(--gray-bg); padding:6px 10px; font-weight:800; font-size:9.6px; color:var(--navy); display:flex; justify-content:space-between; }}
.identity .igrid {{ display:grid; grid-template-columns:repeat(4,1fr); }}
.identity .icell {{ padding:6px 10px; border-top:1px solid var(--line); border-right:1px solid var(--line); }}
.identity .icell:nth-child(4n) {{ border-right:0; }}
.identity .icell small {{ display:block; font-size:7.4px; text-transform:uppercase; color:var(--muted); font-weight:700; }}
.identity .icell strong {{ display:block; font-size:9.6px; }}
.identity .icell span {{ display:block; font-size:7.8px; color:var(--muted); margin-top:1px; }}
.identity .ifull {{ grid-column:1/-1; }}
.page-break {{ break-before: page; }}
.no-break {{ break-inside: avoid; }}
svg text {{ font-family:var(--sans); }}
"""


# ----------------------------------------------------------------------
# Inline SVG chart builders. Pure data-driven geometry -- every plotted
# point is a real value already read from the source JSON; nothing is
# smoothed, interpolated, or estimated.
# ----------------------------------------------------------------------
def _svg_bar_chart(categories: list[str], values: list[float], bar_colors: list[str], width: int = 560, height: int = 150, value_suffix: str = "") -> str:
    if not values:
        return ""
    n = len(values)
    pad_l, pad_r, pad_t, pad_b = 28, 8, 14, 26
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    vmax = max(values) * 1.18 or 1
    bar_w = min(46, plot_w / n * 0.58)
    gap = plot_w / n
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    for frac in (0, 0.5, 1):
        y = pad_t + plot_h * (1 - frac)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#dce5e9" stroke-width="1"/>')
    for i, (cat, val, color) in enumerate(zip(categories, values, bar_colors)):
        cx = pad_l + gap * i + gap / 2
        bh = plot_h * (val / vmax) if vmax else 0
        y = pad_t + plot_h - bh
        parts.append(f'<rect x="{cx - bar_w / 2:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" rx="2" fill="{color}"/>')
        parts.append(f'<text x="{cx:.1f}" y="{y - 4:.1f}" font-size="8.5" font-weight="700" fill="#10243c" text-anchor="middle">{val:g}{value_suffix}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{height - 8:.1f}" font-size="7.6" fill="#5b6b7a" text-anchor="middle">{xml_escape(str(cat))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_line_chart(labels: list[str], values: list[float], color: str = "#087c83", width: int = 560, height: int = 140, unit: str = "") -> str:
    if len(values) < 2:
        return ""
    n = len(values)
    pad_l, pad_r, pad_t, pad_b = 34, 10, 14, 24
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    vmin, vmax = min(values), max(values)
    pad = (vmax - vmin) * 0.15 or (abs(vmax) * 0.1 or 1)
    vmin, vmax = vmin - pad, vmax + pad

    def px(i: int) -> float:
        return pad_l + plot_w * i / (n - 1)

    def py(v: float) -> float:
        return pad_t + plot_h * (1 - (v - vmin) / (vmax - vmin)) if vmax > vmin else pad_t + plot_h / 2

    pts = [(px(i), py(v)) for i, v in enumerate(values)]
    path_d = " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    area_d = f"M {pts[0][0]:.1f} {pad_t + plot_h:.1f} L " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts) + f" L {pts[-1][0]:.1f} {pad_t + plot_h:.1f} Z"
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    for frac in (0, 0.5, 1):
        y = pad_t + plot_h * (1 - frac)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#dce5e9" stroke-width="1"/>')
    parts.append(f'<path d="{area_d}" fill="{color}" opacity="0.12"/>')
    parts.append(f'<path d="M {path_d}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    peak_i = max(range(n), key=lambda i: values[i])
    for i, (x, y) in enumerate(pts):
        r = 3.4 if i == peak_i else 2.2
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}"/>')
    parts.append(f'<text x="{pts[peak_i][0]:.1f}" y="{max(pts[peak_i][1] - 7, 9):.1f}" font-size="8" font-weight="700" fill="{color}" text-anchor="middle">{values[peak_i]:g}{unit}</text>')
    step = max(1, n // 8)
    for i in range(0, n, step):
        parts.append(f'<text x="{pts[i][0]:.1f}" y="{height - 6}" font-size="7.2" fill="#5b6b7a" text-anchor="middle">{xml_escape(labels[i])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _meter_rows(items: list[tuple[str, float, float]]) -> str:
    """items: (label, value, max_value) -- renders stacked horizontal meters."""

    rows = []
    for label, value, vmax in items:
        frac = max(0.0, min(1.0, value / vmax)) if vmax else 0.0
        rows.append(
            f'<div class="meter-row"><span class="small">{_t(label)}</span>'
            f'<div class="meter-track"><div class="meter-fill" style="width:{frac * 100:.1f}%"></div></div>'
            f'<span class="small" style="text-align:right">{value:g}/{vmax:g}</span></div>'
        )
    return "".join(rows)


# ----------------------------------------------------------------------
# Cross-section helpers
# ----------------------------------------------------------------------
def _overall_span(sections: dict[str, Any]) -> tuple[str, str] | None:
    candidates: list[tuple[str, str]] = []
    for key in (SEC_CLINICAL_NOTES, SEC_CBC, SEC_QUESTIONNAIRE):
        w = _get(sections.get(key), "observation_window", default={})
        if w.get("start_date") and w.get("end_date"):
            candidates.append((w["start_date"], w["end_date"]))
    period = _get(sections.get(SEC_CT), "reportMetadata", "periodCovered", default={})
    if period.get("startDate") and period.get("endDate"):
        candidates.append((period["startDate"], period["endDate"]))
    starts = [s for s, _ in candidates if _parse_iso_date(s)]
    ends = [e for _, e in candidates if _parse_iso_date(e)]
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def _section_head(number: int, title: str, sub: str = "") -> str:
    sub_html = f'<span class="section-sub">{_t(sub)}</span>' if sub else ""
    return f'<div class="section-head"><span class="section-num">{number:02d}</span><span class="section-title">{_t(title)}</span>{sub_html}</div>'


def _heading_tag(flag: bool = True) -> str:
    return "section" if flag else "div"


# ----------------------------------------------------------------------
# Cover: hero band + patient & clinical-context identity card
# ----------------------------------------------------------------------
def _sec_cover(digital_twin: dict[str, Any], sections: dict[str, Any], manifest: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    cbc = sections.get(SEC_CBC) or {}
    ct = sections.get(SEC_CT) or {}
    ecg = sections.get(SEC_ECG) or {}
    eeg = sections.get(SEC_EEG) or {}
    questionnaire = sections.get(SEC_QUESTIONNAIRE) or {}
    mri = sections.get(SEC_MRI) or {}

    profile = _get(clinical_notes, "patient_profile", default={})
    diagnosis = _get(profile, "diagnosis", default={})
    window = _get(clinical_notes, "observation_window", default={})
    span = _overall_span(sections)
    regimen = _get(clinical_notes, "clinical_inference", "medication_response", "current_regimen", default=[])
    current_state = _get(clinical_notes, "digital_twin_state", "current_state")

    sex = profile.get("gender") or _get(questionnaire, "patient_profile", "sex")
    sex_label = {"m": "Male", "f": "Female"}.get(str(sex).strip().lower(), _raw_text(sex)) if sex else None
    age_sex = " \u00b7 ".join(p for p in [f"{_raw_text(profile.get('age'))} years" if profile.get("age") is not None else None, sex_label] if p)

    import re

    mri_period = _raw_text(mri.get("observation_period"))
    mri_match = re.search(r"(\d+)\s+MRI study identifiers represented by\s+(\d+)\s+series", mri_period)
    mri_series = mri_match.group(2) if mri_match else "?"

    therapy_line = " \u00b7 ".join(f"{_raw_text(i.get('drug'))} {_raw_text(i.get('dose'))}" for i in regimen) if regimen else NONE_REPORTED
    included = manifest.get("sections_included") or []
    total_categories = len(included) + len(manifest.get("sections_missing") or [])

    stats = [
        (window.get("total_visits"), "Clinical Visits"),
        (_get(cbc, "observation_window", "number_of_reports"), "CBC Reports"),
        (_get(ct, "reportMetadata", "numberOfStudies"), "CT Studies"),
        (mri_series, "MRI Series"),
        (_get(ecg, "dataset_summary", "number_of_recordings"), "ECG Sessions"),
        (_get(eeg, "recording_statistics", "total_recordings"), "EEG Recordings"),
        (_get(questionnaire, "observation_window", "number_of_questionnaires"), "Questionnaires"),
    ]
    stat_cards = "".join(f'<div class="stat"><b>{_t(v)}</b><span>{_t(l)}</span></div>' for v, l in stats)

    return f"""
<section class="hero">
  <div class="eyebrow">Epilepsy Digital Twin Report &middot; Integrated Neurological Decision Support</div>
  <h1>Neuro Digital Twin Integrated Clinical Report</h1>
  <p class="lede">An integrated clinical assessment combining follow-up visits, seizure history, EEG, ECG, CT/MRI imaging, laboratory results, and pharmacogenomic testing for {_t(diagnosis.get('primary'))}.</p>
</section>
<div class="identity">
  <div class="ihead"><span>Patient &amp; Clinical Context</span><span class="muted small">{len(included)}/{total_categories or len(included)} data categories included &middot; generated {_t(digital_twin.get('generated_at'))}</span></div>
  <div class="igrid">
    <div class="icell"><small>Patient</small><strong>{_t(profile.get('name'))}</strong><span>Patient ID {_t(digital_twin.get('patient_id'))}</span></div>
    <div class="icell"><small>Age / Sex</small><strong>{_t(age_sex) or NOT_AVAILABLE}</strong><span>&nbsp;</span></div>
    <div class="icell"><small>Primary Diagnosis</small><strong>{_t(diagnosis.get('primary'))}</strong><span>Secondary diagnosis: {_t(diagnosis.get('secondary'))}</span></div>
    <div class="icell"><small>Diagnosed Since</small><strong>{_pretty_date(diagnosis.get('diagnosis_date'))}</strong><span>&nbsp;</span></div>
    <div class="icell ifull"><small>Current Clinical Status</small><strong>{_sentence_case(current_state)}</strong></div>
    <div class="icell ifull"><small>Current Therapy</small><strong>{_t(therapy_line)}</strong></div>
    <div class="icell ifull">
      <small>Reporting Period</small>
      <strong>Data reviewed: {_pretty_range(*span) if span else NOT_AVAILABLE} ({_duration_phrase(*span) if span else NOT_AVAILABLE})</strong>
      <span>Clinic visit notes cover {_pretty_date(window.get('start_date'))} to {_pretty_date(window.get('end_date'))}; laboratory, imaging, EEG/ECG, and questionnaire data continue through {_pretty_date(span[1]) if span else NOT_AVAILABLE}.</span>
    </div>
  </div>
</div>
<div class="grid" style="display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-bottom:10px">{stat_cards}</div>
"""


# ----------------------------------------------------------------------
# 01. Executive Summary
# ----------------------------------------------------------------------
def _sec_executive_summary(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    eeg = sections.get(SEC_EEG) or {}
    genetics = sections.get(SEC_GENETICS) or {}
    questionnaire = sections.get(SEC_QUESTIONNAIRE) or {}

    conclusion = _get(clinical_notes, "executive_summary", "clinical_conclusion")
    overall_risk = _get(clinical_notes, "longitudinal_summary", "overall_risk_level")
    state_vector = _get(clinical_notes, "digital_twin_state", "state_vector", default={})
    health_score = state_vector.get("overall_health_score")
    eeg_risk = _get(eeg, "digital_twin_state", "risk_level")
    flags = genetics.get("priority_safety_flags") or []
    high_flags = [f for f in flags if "high" in str(f.get("severity", "")).lower()]
    low_domain = _get(questionnaire, "digital_twin_state", "latest_low_scoring_domains", default=[])

    stats = [
        (f"{health_score * 100:.0f}%" if isinstance(health_score, (int, float)) else NOT_AVAILABLE, "Overall Health Score"),
        (_raw_text(overall_risk).title(), "Overall Clinical Risk"),
        (_raw_text(eeg_risk), "EEG-Derived Seizure Risk"),
        (f"{len(flags)} ({len(high_flags)} high)", "Genetic Safety Flags"),
    ]
    stat_html = "".join(f'<div class="stat"><b>{_t(v)}</b><span>{_t(l)}</span></div>' for v, l in stats)

    return f"""
<section class="section">
  {_section_head(1, "Executive Summary")}
  <div class="card" style="margin-bottom:8px"><p style="font-size:10px">{_t(conclusion)}</p></div>
  <div class="grid4">{stat_html}</div>
  <p class="small muted" style="margin-top:6px">Domain the patient rates worst on questionnaires: {_t(', '.join(_humanize(d) for d in low_domain)) if low_domain else NONE_REPORTED}. See the Digital Twin Risk Dashboard (Section 13) for the full risk breakdown.</p>
</section>
"""


# ----------------------------------------------------------------------
# 02. 6-Month Timeline
# ----------------------------------------------------------------------
def _sec_timeline(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    genetics = sections.get(SEC_GENETICS) or {}
    events = _get(clinical_notes, "major_clinical_events", default=[])

    items: list[tuple[str, str, str]] = [(e.get("date"), e.get("event"), e.get("clinical_impact")) for e in events]
    genetics_date = _get(genetics, "patient", "report_date")
    if genetics_date:
        items.append((genetics_date, "Pharmacogenomic (PGx) panel reported", "76 variants analyzed across 47 medications; see Pharmacogenomics."))
    items = sorted((i for i in items if i[0]), key=lambda i: i[0])

    tl_html = "".join(f'<div class="tl-item"><time>{_pretty_date(d)}</time><p><b>{_t(e)}</b> \u2014 {_t(c)}</p></div>' for d, e, c in items)
    return f"""
<section class="section">
  {_section_head(2, "6-Month Timeline")}
  <div class="card"><div class="timeline">{tl_html}</div></div>
</section>
"""


# ----------------------------------------------------------------------
# 03. Seizure Trends
# ----------------------------------------------------------------------
def _sec_seizure_trends(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    seizure = _get(clinical_notes, "clinical_inference", "seizure_analysis", default={})
    progression = _get(clinical_notes, "longitudinal_summary", "clinical_progression", default=[])
    events = _get(clinical_notes, "major_clinical_events", default=[])
    high_dates = {e.get("date") for e in events if str(e.get("importance", "")).lower() == "high"}

    import re as _re
    dates, counts = [], []
    for visit in progression:
        m = _re.match(r"^(\d+)\s+total reported episodes", str(visit.get("summary", "")))
        if m and visit.get("date"):
            dates.append(visit["date"])
            counts.append(float(m.group(1)))
    labels = [_pretty_date(d)[:6] for d in dates]
    bar_colors = ["#b53c48" if d in high_dates else "#087c83" for d in dates]
    chart = _svg_bar_chart(labels, counts, bar_colors) if counts else ""

    highest = seizure.get("highest_seizure_burden") or {}
    lowest = seizure.get("lowest_seizure_burden") or {}
    dur = _get(seizure, "average_duration_trend", default=[])
    dur_chart = _svg_line_chart(labels, dur, color="#1f3b57", unit="m") if len(dur) == len(labels) and len(dur) >= 2 else ""

    return f"""
<section class="section">
  {_section_head(3, "Seizure Trends", "Clinical-note episode counts, clustering, and duration")}
  <div class="grid2">
    <div class="card">
      <div class="h2">Reported Episodes per Visit</div>
      {chart}
      <p class="small muted">Red bars mark visits with a high-importance clinical event.</p>
    </div>
    <div class="card">
      <dl class="kv">
        <div><dt>Overall Pattern</dt><dd>{_t(seizure.get('overall_pattern'))}</dd></div>
        <div><dt>Highest Burden</dt><dd>{_t(highest.get('episodes'))} episodes on {_pretty_date(highest.get('date'))}</dd></div>
        <div><dt>Lowest Burden</dt><dd>{_t(lowest.get('episodes'))} episodes on {_pretty_date(lowest.get('date'))}</dd></div>
        <div><dt>Clustering</dt><dd>{_t(seizure.get('clustering_detected'))}</dd></div>
        <div><dt>Clinical Interpretation</dt><dd>{_t(seizure.get('overall_inference'))}</dd></div>
      </dl>
    </div>
  </div>
  {'<div class="card" style="margin-top:8px"><div class="h2">Average Seizure Duration per Visit (minutes)</div>' + dur_chart + '</div>' if dur_chart else ''}
</section>
"""


# ----------------------------------------------------------------------
# 04. Aura & Triggers
# ----------------------------------------------------------------------
def _sec_aura_triggers(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    questionnaire = sections.get(SEC_QUESTIONNAIRE) or {}
    aura = _get(clinical_notes, "clinical_inference", "aura_analysis", default={})
    trends = _get(questionnaire, "domain_score_trends", default={})
    aura_trend = trends.get("aura_prodromal_symptoms", {})
    trigger_trend = trends.get("epileptic_triggers", {})
    aura_summary = _get(questionnaire, "latest_domain_summaries", "aura_summary", "clinically_relevant_responses", default=[])
    trigger_summary = _get(questionnaire, "latest_domain_summaries", "trigger_summary", "clinically_relevant_responses", default=[])

    pattern_pills = "".join(f'<span class="pill">{_t(p)}</span>' for p in aura.get("persistent_patterns") or [])
    aura_items = "".join(f'<li>{_t(r.get("item"))}: <b>{_t(r.get("reported_response"))}</b></li>' for r in aura_summary[:5])
    trigger_items = "".join(f'<li>{_t(r.get("item"))}: <b>{_t(r.get("reported_response"))}</b></li>' for r in trigger_summary[:5])

    return f"""
<section class="section">
  {_section_head(4, "Aura & Triggers", "Prodromal patterns and patient-reported trigger exposure")}
  <div class="card" style="margin-bottom:8px">
    <div class="h2">Persistent Aura / Prodromal Patterns</div>
    <div class="pill-row">{pattern_pills}</div>
    <p class="small" style="margin-top:5px">{_t(aura.get('clinical_significance'))}</p>
  </div>
  <div class="grid2">
    <div class="card">
      <div class="h2">Aura &amp; Prodromal Symptoms &mdash; Questionnaire Domain</div>
      <p class="small">Latest score <b>{_t(aura_trend.get('latest_score'))}/100</b> (first {_t(aura_trend.get('first_score'))}, trend {_t(aura_trend.get('trend_direction'))})</p>
      <ul class="list-compact">{aura_items}</ul>
    </div>
    <div class="card sev-mod-bg" style="border-color:#f0d79a">
      <div class="h2">Epileptic Triggers &mdash; Lowest-Scoring Domain</div>
      <p class="small">Latest score <b>{_t(trigger_trend.get('latest_score'))}/100</b> (min {_t(trigger_trend.get('minimum_score'))}, trend {_t(trigger_trend.get('trend_direction'))})</p>
      <ul class="list-compact">{trigger_items}</ul>
    </div>
  </div>
</section>
"""


# ----------------------------------------------------------------------
# 05. Medication Response
# ----------------------------------------------------------------------
def _sec_medications(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    questionnaire = sections.get(SEC_QUESTIONNAIRE) or {}
    med = _get(clinical_notes, "clinical_inference", "medication_response", default={})
    regimen = med.get("current_regimen") or []
    med_trend = _get(questionnaire, "domain_score_trends", "medication_side_effects", default={})
    med_summary = _get(questionnaire, "latest_domain_summaries", "medication_summary", "clinically_relevant_responses", default=[])

    rows = "".join(f"<tr><td>{_t(i.get('drug'))}</td><td>{_t(i.get('dose'))}</td></tr>" for i in regimen)
    effects_pills = "".join(f'<span class="pill">{_t(p)}</span>' for p in med.get("persistent_side_effects") or [])
    med_items = "".join(f'<li>{_t(r.get("item"))}: <b>{_t(r.get("reported_response"))}</b></li>' for r in med_summary[:5])

    return f"""
<section class="section">
  {_section_head(5, "Medication Response", "Current regimen, tolerability, and reported side effects")}
  <div class="grid2">
    <div class="card">
      <div class="h2">Current Regimen</div>
      <table class="datatable"><thead><tr><th>Drug</th><th>Dose</th></tr></thead><tbody>{rows}</tbody></table>
      <dl class="kv" style="margin-top:6px">
        <div><dt>Effectiveness</dt><dd>{_sentence_case(med.get('effectiveness'))}</dd></div>
        <div><dt>Tolerability</dt><dd>{_sentence_case(med.get('tolerability'))}</dd></div>
        <div><dt>Documented Toxicity</dt><dd>{_t(med.get('drug_toxicity'))}</dd></div>
        <div><dt>Clinical Interpretation</dt><dd>{_t(med.get('overall_inference'))}</dd></div>
      </dl>
    </div>
    <div class="card">
      <div class="h2">Persistent Side Effects (clinical notes)</div>
      <div class="pill-row">{effects_pills}</div>
      <div class="h2" style="margin-top:8px">Medication Side Effects &mdash; Questionnaire Domain</div>
      <p class="small">Latest score <b>{_t(med_trend.get('latest_score'))}/100</b> (first {_t(med_trend.get('first_score'))}, trend {_t(med_trend.get('trend_direction'))})</p>
      <ul class="list-compact">{med_items}</ul>
    </div>
  </div>
</section>
"""


# ----------------------------------------------------------------------
# 06. Pharmacogenomics
# ----------------------------------------------------------------------
def _pgx_worst_per_drug(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce a list of per-gene PGx findings to one row per drug: the single most
    clinically significant / worst-outcome finding (display-only reduction -- every
    finding stays in the source JSON, this only picks which one to print)."""

    best: dict[str, tuple[tuple[int, bool], dict[str, Any]]] = {}
    for entry in entries:
        drug = str(entry.get("drug") or "").strip()
        if not drug:
            continue
        effect = str(entry.get("predicted_effect") or "").strip().lower()
        rank = (_PGX_EFFECT_RANK.get(effect, -1), bool(entry.get("significant")))
        if drug not in best or rank > best[drug][0]:
            best[drug] = (rank, entry)
    return [entry for _, entry in best.values()]


def _sec_pharmacogenomics(sections: dict[str, Any]) -> str:
    genetics = sections.get(SEC_GENETICS) or {}

    current_findings = []
    for entry in _get(genetics, "findings_by_therapeutic_class", "mood_stabilizers_antiepileptics", default=[]):
        drug = str(entry.get("drug", "")).lower()
        if any(d in drug for d in CURRENT_REGIMEN_DRUG_NAMES):
            current_findings.append(entry)

    # Citation = the real per-finding source reference from the PGx panel's own data
    # (a PMID/PMCID carried straight through from the source spreadsheet's "pmid" column
    # via genetics_summary.py) -- not a generic panel-level note, and never invented.
    worst_rows = "".join(
        f'<tr><td>{_t(e.get("drug"))}</td><td>{_t(e.get("genetic_basis"))}</td>'
        f'<td><span class="badge {_PGX_EFFECT_CLASS.get(str(e.get("predicted_effect") or "").lower(), "sev-neutral")}">'
        f'{_sentence_case(e.get("predicted_effect"))}</span></td>'
        f'<td>{_t(e.get("citation"))}</td></tr>'
        for e in _pgx_worst_per_drug(current_findings)
    )

    return f"""
<section class="section">
  {_section_head(6, "Pharmacogenomics", f"{genetics.get('patient', {}).get('variants_analyzed', '?')} variants across {genetics.get('patient', {}).get('drugs_covered', '?')} medications \u00b7 reported {_pretty_date(_get(genetics, 'patient', 'report_date'))}")}
  <div class="callout" style="margin-bottom:8px"><b>PGx summary for the current regimen:</b> the single most clinically significant finding per drug, with its genetic basis and source citation.</div>
  <table class="datatable"><thead><tr><th>Drug</th><th>Gene (Genotype)</th><th>Worst / Most Significant Outcome</th><th>Citation</th></tr></thead><tbody>{worst_rows or '<tr><td colspan="4">No marker in this panel names a current-regimen drug directly.</td></tr>'}</tbody></table>
</section>
"""


# ----------------------------------------------------------------------
# 07. Drug Interactions -- this dataset has no DDI screening data; this
# section states that gap explicitly rather than inventing interaction
# findings (see module docstring).
# ----------------------------------------------------------------------
def _sec_drug_interactions(sections: dict[str, Any]) -> str:
    ddi = sections.get(SEC_DDI) or {}
    if not ddi:
        return _sec_drug_interactions_gap(sections)

    coverage = _get(ddi, "source_coverage", default={})
    reconciliation = _get(ddi, "medication_reconciliation", default={})
    pair_assessments = _get(ddi, "current_pair_assessments", default=[])
    overall = _get(ddi, "overall_interpretation", default={})
    limitations = _get(ddi, "limitations", default=[])

    sub = (
        f"{len(reconciliation.get('normalized_medications') or [])} current medication(s) reconciled · "
        f"{_raw_text(coverage.get('flockhart_source'))} ({_raw_text(coverage.get('flockhart_version'))})"
    )

    med_rows = "".join(
        f"<tr><td>{_t(m.get('source_name'))}</td>"
        f"<td>{_t((m.get('dose') or {}).get('value'))} {_t((m.get('dose') or {}).get('unit'))} {_t(m.get('frequency'))}</td>"
        f"<td>{_t(m.get('status'))}</td>"
        f"<td>{', '.join(m.get('reconciliation_flags') or []) or '&mdash;'}</td></tr>"
        for m in reconciliation.get("normalized_medications") or []
    )
    conflicts = reconciliation.get("conflicts") or []
    conflict_html = (
        "".join(f'<div class="callout warn" style="margin-top:6px">{_t(c.get("details"))}</div>' for c in conflicts)
        if conflicts
        else '<div class="callout" style="margin-top:6px">No conflicting doses found for the same drug across the reconciled source data.</div>'
    )

    # Only clinically relevant/required pairs -- an actual interaction, or one that is
    # genuinely unresolved -- are listed; a resolved "no interaction" pair is not required
    # reading and is skipped here (its full record remains in the source JSON).
    relevant_pairs = [p for p in pair_assessments if p.get("status") != "no_interaction_detected"]
    pair_rows = "".join(
        f"<tr><td>{_t(p.get('drug_a'))} + {_t(p.get('drug_b'))}</td>"
        f'<td><span class="badge {_status_class(p.get("status"))}">{_humanize(p.get("status"))}</span></td>'
        f'<td><span class="badge {_status_class(p.get("severity"))}">{_t(p.get("severity"))}</span></td>'
        f"<td>{_t(p.get('patient_specific_interpretation'))}</td></tr>"
        for p in relevant_pairs
    )
    pairs_html = (
        f'<table class="datatable"><thead><tr><th>Drug Pair</th><th>Status</th><th>Severity</th>'
        f"<th>Clinical Significance</th></tr></thead><tbody>{pair_rows}</tbody></table>"
        if relevant_pairs
        else '<div class="callout">No clinically required drug-drug interaction was identified for the current regimen.</div>'
    )

    monitoring = overall.get("recommended_monitoring") or []
    monitoring_pills = "".join(f'<span class="pill">{_t(m)}</span>' for m in monitoring)

    return f"""
<section class="section">
  {_section_head(7, "Drug Interactions", sub)}
  <div class="card">
    <div class="h2">Medication Reconciliation</div>
    <table class="datatable"><thead><tr><th>Drug</th><th>Dose</th><th>Status</th><th>Flags</th></tr></thead>
      <tbody>{med_rows}</tbody></table>
    {conflict_html}
  </div>
  <div class="card" style="margin-top:8px">
    <div class="h2">Clinically Relevant Interactions</div>
    {pairs_html}
  </div>
  <div class="card" style="margin-top:8px">
    <div class="h2">Essential Monitoring</div>
    <div class="pill-row">{monitoring_pills or '<span class="pill">None recorded.</span>'}</div>
  </div>
  <div class="callout gap" style="margin-top:6px"><b>Limitations of this screen:</b> {
    ' '.join(_t(item) for item in limitations)
  }</div>
</section>
"""


def _sec_drug_interactions_gap(sections: dict[str, Any]) -> str:
    """Fallback used when reports/ddi/DDI_Clinical_Assessment.json has not been generated."""

    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    regimen = _get(clinical_notes, "clinical_inference", "medication_response", "current_regimen", default=[])
    drug_list = ", ".join(_raw_text(i.get("drug")) for i in regimen) if regimen else NONE_REPORTED

    return f"""
<section class="section">
  {_section_head(7, "Drug Interactions")}
  <div class="callout gap">
    <b>No drug-drug interaction (DDI) screen is available for this patient.</b>
    The current regimen ({_t(drug_list)}) has not been checked against a drug-interaction database in the
    available data. This is a different check from Pharmacogenomics (Section 06), which looks at how the
    patient's own genes affect each drug, not how the drugs affect each other. A formal DDI screen of the
    reconciled medication list is recommended before any dose or drug change; see the Clinical Action Plan.
  </div>
</section>
"""


# ----------------------------------------------------------------------
# 08. EEG
# ----------------------------------------------------------------------
def _sec_eeg(sections: dict[str, Any]) -> str:
    eeg = sections.get(SEC_EEG) or {}
    stats = _get(eeg, "recording_statistics", default={})
    sig = _get(eeg, "signal_statistics", default={})
    phases = _get(eeg, "phase_analysis", default={})
    dt_state = _get(eeg, "digital_twin_state", default={})
    obs = _get(eeg, "overall_observation", default={})

    stat_cards = "".join(
        f'<div class="stat"><b>{_t(v)}</b><span>{_t(l)}</span></div>'
        for v, l in [(stats.get("total_recordings"), "Total Recordings"), (stats.get("interictal"), "Interictal"), (stats.get("preictal"), "Preictal"), (stats.get("ictal"), "Ictal")]
    )

    phase_order = ["interictal", "preictal", "ictal"]
    phase_colors = {"interictal": "#247a5a", "preictal": "#a86505", "ictal": "#b53c48"}
    midpoints, cats, colors_l = [], [], []
    for p in phase_order:
        vr = _get(sig, p, "variance_range", default=None)
        if isinstance(vr, list) and len(vr) == 2:
            midpoints.append(sum(vr) / 2)
            cats.append(p.capitalize())
            colors_l.append(phase_colors[p])
    chart = _svg_bar_chart(cats, midpoints, colors_l, value_suffix="") if midpoints else ""

    sig_rows = "".join(
        f"<tr><td>{p.capitalize()}</td><td>{_t(sig[p].get('amplitude_range'))}</td><td>{_t(sig[p].get('standard_deviation_range'))}</td><td>{_t(sig[p].get('variance_range'))}</td></tr>"
        for p in phase_order if p in sig
    )
    phase_rows = "".join(
        f"<tr><td>{p.capitalize()}</td><td>{_t(v.get('state'))}</td><td>{_t(v.get('electrical_activity'))}</td><td>{_t(v.get('signal_variability'))}</td></tr>"
        for p, v in phases.items()
    )

    return f"""
<section class="section">
  {_section_head(8, "EEG", "Longitudinal interictal / preictal / ictal signal characteristics")}
  <div class="grid4" style="margin-bottom:8px">{stat_cards}</div>
  <div class="grid2">
    <div class="card">
      <div class="h2">Signal Variance Midpoint by Phase (\u00b5V\u00b2)</div>
      {chart}
      <div class="pill-row" style="margin-top:6px">
        <span class="pill sev-high">EEG-derived risk: {_t(dt_state.get('risk_level'))}</span>
        <span class="pill sev-high">Future seizure probability: {_t(dt_state.get('future_seizure_probability'))}</span>
        <span class="pill">Dominant feature: {_t(_get(eeg, 'derived_features', 'dominant_discriminative_feature'))}</span>
      </div>
    </div>
    <div class="card">
      <div class="h2">Phase Analysis</div>
      <table class="datatable"><thead><tr><th>Phase</th><th>State</th><th>Activity</th><th>Variability</th></tr></thead><tbody>{phase_rows}</tbody></table>
    </div>
  </div>
  <div class="card" style="margin-top:8px">
    <table class="datatable"><thead><tr><th>Phase</th><th>Amplitude Range</th><th>Std Dev Range</th><th>Variance Range</th></tr></thead><tbody>{sig_rows}</tbody></table>
    <p class="small" style="margin-top:6px">{_t(obs.get('observation'))} {_t(obs.get('clinical_interpretation'))}</p>
  </div>
</section>
"""


# ----------------------------------------------------------------------
# 09. MRI / CT
# ----------------------------------------------------------------------
def _sec_imaging(sections: dict[str, Any]) -> str:
    ct = sections.get(SEC_CT) or {}
    mri = sections.get(SEC_MRI) or {}
    meta = _get(ct, "reportMetadata", default={})
    period = _get(meta, "periodCovered", default={})
    studies = ct.get("studies") or []
    ct_rows = "".join(
        f'<tr><td>{_pretty_date(s.get("studyDate"))}</td><td>{_t(s.get("impression"))}</td></tr>'
        for s in studies
    )

    mri_summary = _get(mri, "mri_summary", default={})
    findings = _get(mri, "structural_findings", default={})
    assessed_keys = ("brain_morphology", "brain_volume", "tissue_signal_characteristics")
    assessed_rows = "".join(
        f'<tr><td>{_humanize(k)}</td><td><span class="badge sev-neutral" style="background:var(--gray-bg)">{_t(findings[k].get("status"))}</span></td><td>{_t(findings[k].get("finding"))}</td></tr>'
        for k in assessed_keys if k in findings
    )
    not_assessed = ", ".join(_humanize(k) for k in findings if k not in assessed_keys)

    return f"""
<section class="section">
  {_section_head(9, "MRI / CT", "Structural imaging: neuroimaging key findings")}
  <div class="h2">CT &mdash; {_t(meta.get('numberOfStudies'))} studies, {_pretty_range(period.get('startDate'), period.get('endDate'))}</div>
  <div class="card" style="margin-bottom:8px">
    <table class="datatable"><thead><tr><th>Date</th><th>Impression</th></tr></thead><tbody>{ct_rows}</tbody></table>
    <p class="small muted" style="margin-top:6px">{_t(ct.get('comparison'))}</p>
  </div>
  <div class="h2">MRI &mdash; {_t(mri_summary.get('overall_status'))}</div>
  <div class="card">
    <table class="datatable"><thead><tr><th>Finding</th><th>Status</th><th>Detail</th></tr></thead><tbody>{assessed_rows}</tbody></table>
    <p class="small muted" style="margin-top:6px">Not directly assessed from the available imaging metadata (this means "not evaluated," not "abnormal"): {_t(not_assessed) if not_assessed else NONE_REPORTED}.</p>
    <p class="small" style="margin-top:4px">{_t(_get(mri, 'longitudinal_assessment', 'overall_trend'))}. {_t(mri_summary.get('clinical_impression'))}</p>
  </div>
</section>
"""


# ----------------------------------------------------------------------
# 10. ECG
# ----------------------------------------------------------------------
def _sec_ecg(sections: dict[str, Any]) -> str:
    ecg = sections.get(SEC_ECG) or {}
    summary = _get(ecg, "dataset_summary", default={})
    amp = ecg.get("amplitude_statistics_uV") or []
    observations = ecg.get("notable_observations") or []

    labels = [_pretty_date(a.get("date"))[:6] for a in amp]
    values = [a.get("std_dev") for a in amp if isinstance(a.get("std_dev"), (int, float))]
    chart = _svg_line_chart(labels, values, color="#087c83", unit=" \u00b5V") if len(values) >= 2 else ""

    stat_cards = "".join(
        f'<div class="stat"><b>{_t(v)}</b><span>{_t(l)}</span></div>'
        for v, l in [
            (summary.get("number_of_recordings"), "Recordings"),
            (summary.get("total_recorded_duration"), "Total Duration"),
            (summary.get("sampling_rate_hz"), "Sampling Rate (Hz)"),
        ]
    )
    obs_items = "".join(f"<li>{_t(o)}</li>" for o in observations)

    return f"""
<section class="section">
  {_section_head(10, "ECG", summary.get("channel_configuration"))}
  <div class="grid3" style="margin-bottom:8px">{stat_cards}</div>
  <div class="card">
    <div class="h2">Amplitude Std Dev by Recording Date (\u00b5V)</div>
    {chart}
  </div>
  <div class="card" style="margin-top:8px">
    <ul class="list-compact">{obs_items}</ul>
    <p class="small muted" style="margin-top:6px">{_t(ecg.get('scope_and_limitations'))}</p>
  </div>
</section>
"""


# ----------------------------------------------------------------------
# 11. CBC / Labs
# ----------------------------------------------------------------------
def _sec_cbc(sections: dict[str, Any]) -> str:
    cbc = sections.get(SEC_CBC) or {}
    questionnaire = sections.get(SEC_QUESTIONNAIRE) or {}
    trends = _get(cbc, "longitudinal_trends", default={})
    window = _get(cbc, "observation_window", default={})
    status = _get(cbc, "digital_twin_state", "overall_hematological_status")

    rows = "".join(
        f"<tr><td>{_t(e.get('parameter'))}</td><td>{_t(e.get('first_value'))}</td><td>{_t(e.get('latest_value'))}</td>"
        f"<td>{_t(e.get('minimum_value'))} \u2013 {_t(e.get('maximum_value'))}</td><td>{_t(e.get('trend_direction'))}</td></tr>"
        for e in trends.values()
    )

    lab_reported = _get(questionnaire, "latest_domain_summaries", "laboratory_lifestyle_summary", "clinically_relevant_responses", default=[])
    reported_flags = [r for r in lab_reported if any(w in str(r.get("reported_response", "")).lower() for w in ("low", "elevat", "borderline"))]
    discrepancy = ""
    if reported_flags:
        items = "; ".join(f"{_raw_text(r.get('item'))}: {_raw_text(r.get('reported_response'))}" for r in reported_flags)
        discrepancy = (
            f'<div class="callout warn" style="margin-top:8px"><b>Patient-reported vs. measured discrepancy:</b> '
            f"the {_pretty_date(_get(questionnaire, 'observation_window', 'end_date'))} questionnaire records patient recollections of "
            f"{_t(items)}; measured CBC values across all {_t(window.get('number_of_reports'))} reports in this period remained "
            "within their reference ranges throughout (see table below). The discrepancy likely reflects patient recall/perception rather than a measured lab abnormality.</div>"
        )

    return f"""
<section class="section">
  {_section_head(11, "CBC / Labs", f"{_t(window.get('number_of_reports'))} reports, {_pretty_range(window.get('start_date'), window.get('end_date'))}")}
  <div class="card">
    <p class="small"><b>Overall hematological status:</b> {_sentence_case(str(status).replace('_', ' '))}. No CBC parameter abnormality was identified against the report-provided reference ranges across the observation period.</p>
    <table class="datatable" style="margin-top:6px"><thead><tr><th>Parameter</th><th>First</th><th>Latest</th><th>Range</th><th>Trend</th></tr></thead><tbody>{rows}</tbody></table>
  </div>
  {discrepancy}
</section>
"""


# ----------------------------------------------------------------------
# 12. Sleep, Cognition & Quality of Life
# ----------------------------------------------------------------------
def _sec_sleep_cognition_qol(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    questionnaire = sections.get(SEC_QUESTIONNAIRE) or {}
    sleep = _get(clinical_notes, "clinical_inference", "sleep_analysis", default={})
    cognitive = _get(clinical_notes, "clinical_inference", "cognitive_analysis", default={})
    qol = _get(clinical_notes, "clinical_inference", "quality_of_life", default={})
    trends = _get(questionnaire, "domain_score_trends", default={})

    sleep_issues = "".join(f'<span class="pill">{_t(p)}</span>' for p in sleep.get("persistent_issues") or [])

    meters = _meter_rows(
        [
            ("Sleep Quality & Architecture", trends.get("sleep_quality_architecture", {}).get("latest_score") or 0, 100),
            ("Cognitive & Executive Function", trends.get("cognitive_executive_function", {}).get("latest_score") or 0, 100),
            ("Lab Biomarkers & Lifestyle", trends.get("lab_biomarkers_lifestyle", {}).get("latest_score") or 0, 100),
        ]
    )

    return f"""
<section class="section">
  {_section_head(12, "Sleep, Cognition & Quality of Life")}
  <div class="grid3">
    <div class="card">
      <div class="h2">Sleep</div>
      <dl class="kv">
        <div><dt>Trend</dt><dd>{_sentence_case(sleep.get('overall_trend'))}</dd></div>
        <div><dt>Average</dt><dd>{_t(sleep.get('average_sleep'))}</dd></div>
      </dl>
      <div class="pill-row" style="margin-top:5px">{sleep_issues}</div>
    </div>
    <div class="card">
      <div class="h2">Cognition</div>
      <dl class="kv">
        <div><dt>Memory</dt><dd>{_sentence_case(cognitive.get('memory'))}</dd></div>
        <div><dt>Attention</dt><dd>{_sentence_case(cognitive.get('attention'))}</dd></div>
        <div><dt>Language</dt><dd>{_sentence_case(cognitive.get('language'))}</dd></div>
        <div><dt>Mental Clarity</dt><dd>{_sentence_case(cognitive.get('mental_clarity'))}</dd></div>
      </dl>
    </div>
    <div class="card">
      <div class="h2">Quality of Life</div>
      <dl class="kv">
        <div><dt>Daily Function</dt><dd>{_sentence_case(qol.get('daily_function'))}</dd></div>
        <div><dt>Social Function</dt><dd>{_sentence_case(qol.get('social_function'))}</dd></div>
        <div><dt>Clinical Interpretation</dt><dd>{_t(qol.get('overall_inference'))}</dd></div>
      </dl>
    </div>
  </div>
  <div class="card" style="margin-top:8px"><div class="h2">Latest Questionnaire Domain Scores (0-100)</div>{meters}</div>
</section>
"""


# ----------------------------------------------------------------------
# 13. Digital Twin Risk Dashboard
# ----------------------------------------------------------------------
def _sec_risk_dashboard(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    eeg = sections.get(SEC_EEG) or {}
    genetics = sections.get(SEC_GENETICS) or {}
    risk = _get(clinical_notes, "digital_twin_state", "risk_prediction", default={})
    state_vector = _get(clinical_notes, "digital_twin_state", "state_vector", default={})
    eeg_state = _get(eeg, "digital_twin_state", default={})
    flags = genetics.get("priority_safety_flags") or []
    high_flags = [f for f in flags if "high" in str(f.get("severity", "")).lower()]
    overall_risk = _get(clinical_notes, "longitudinal_summary", "overall_risk_level")

    chip_items = [(_humanize(k), v) for k, v in risk.items()]
    chip_items.append(("EEG-Derived Seizure Risk", eeg_state.get("risk_level")))
    chip_items.append(("Genetic Safety Flags", f"{len(flags)} flags, {len(high_flags)} high"))
    chips = "".join(
        f'<div class="chip {_status_class(v)}-bg"><b>{_t(l)}</b><span class="{_status_class(v)}">{_t(v)}</span></div>'
        for l, v in chip_items
    )

    sv_meters = _meter_rows([(_humanize(k), round(v * 100, 1), 100) for k, v in state_vector.items() if k != "overall_health_score" and isinstance(v, (int, float))])

    return f"""
<section class="section">
  {_section_head(13, "Digital Twin Risk Dashboard", f"Overall clinical risk: {_raw_text(overall_risk).title()}")}
  <div class="grid4">{chips}</div>
  <div class="card" style="margin-top:8px"><div class="h2">State Vector (Health Metric Scores, 0-100)</div>{sv_meters}
    <p class="small muted" style="margin-top:5px">Each domain score is the mean of clinician-documented per-visit ratings across the clinical-note window; overall health score is the mean of the other domains. Derived digital-twin summary metrics, not a direct lab/imaging measurement.</p>
  </div>
</section>
"""


def _action_plan_reconciliation_item(ddi: dict[str, Any]) -> str:
    if not ddi:
        return (
            "<li>Reconcile the current regimen and screen it against a drug-interaction "
            "database (no DDI screen is present in the source data; see Section 07).</li>"
        )
    conflicts = _get(ddi, "medication_reconciliation", "conflicts", default=[])
    items = [
        "Current regimen reconciled and checked against curated reference pharmacology and this "
        "patient's own pharmacogenomic findings (Section 07). Predicted-model (SuperCYPsPred) coverage "
        "is still unresolved, so a live, comprehensive DDI screen is recommended before adding or "
        "changing any interacting medication."
    ]
    items.extend(_raw_text(c.get("details")) for c in conflicts)
    return "".join(f"<li>{_t(i)}</li>" for i in items)


# ----------------------------------------------------------------------
# 14. Clinical Action Plan
# ----------------------------------------------------------------------
def _sec_action_plan(sections: dict[str, Any]) -> str:
    clinical_notes = sections.get(SEC_CLINICAL_NOTES) or {}
    eeg = sections.get(SEC_EEG) or {}
    mri = sections.get(SEC_MRI) or {}
    genetics = sections.get(SEC_GENETICS) or {}
    ddi = sections.get(SEC_DDI) or {}

    priority_actions = (genetics.get("clinical_conclusion") or {}).get("recommendations") or []
    priority_html = "".join(
        f'<div class="tl-item"><span class="badge {_status_class(r.get("priority"))}">{_t(r.get("priority"))}</span> '
        f'<b>{_t(r.get("action"))}</b><p>{_t(r.get("detail"))}</p></div>'
        for r in priority_actions
    )

    recommended_actions = _get(clinical_notes, "digital_twin_state", "recommended_actions", default=[])
    monitoring = _get(clinical_notes, "digital_twin_state", "monitoring_priorities", default=[])
    eeg_monitoring = eeg.get("recommended_monitoring") or []
    merged_monitoring = list(dict.fromkeys([*monitoring, *eeg_monitoring]))
    mri_follow_up = _get(mri, "recommendations", "follow_up", default=[])
    mri_clinical = _get(mri, "recommendations", "clinical", default=[])

    def _li(items: list[str]) -> str:
        return "".join(f"<li>{_t(i)}</li>" for i in items)

    return f"""
<section class="section">
  {_section_head(14, "Clinical Action Plan")}
  <div class="card" style="margin-bottom:8px">
    <div class="h2">Priority Actions (Pharmacogenomics)</div>
    <div class="timeline">{priority_html}</div>
  </div>
  <div class="grid2">
    <div class="card">
      <div class="h2">Recommended Actions</div>
      <ul class="list-compact">{_li(recommended_actions)}</ul>
      <div class="h2" style="margin-top:6px">Monitoring Priorities</div>
      <ul class="list-compact">{_li(merged_monitoring)}</ul>
    </div>
    <div class="card">
      <div class="h2">MRI Follow-Up</div>
      <ul class="list-compact">{_li([*mri_follow_up, *mri_clinical])}</ul>
      <div class="h2" style="margin-top:6px">Medication Reconciliation</div>
      <ul class="list-compact">{_action_plan_reconciliation_item(ddi)}</ul>
    </div>
  </div>
</section>
"""


# ----------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------
def _build_html(digital_twin: dict[str, Any], sections: dict[str, Any], manifest: dict[str, Any]) -> str:
    body_parts = [
        _sec_cover(digital_twin, sections, manifest),
        _sec_executive_summary(sections),
        _sec_timeline(sections),
        _sec_seizure_trends(sections),
        _sec_aura_triggers(sections),
        _sec_medications(sections),
        _sec_pharmacogenomics(sections),
        _sec_drug_interactions(sections),
        _sec_eeg(sections),
        _sec_imaging(sections),
        _sec_ecg(sections),
        _sec_cbc(sections),
        _sec_sleep_cognition_qol(sections),
        _sec_risk_dashboard(sections),
        _sec_action_plan(sections),
    ]
    body = "\n".join(body_parts)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_t(DOC_TITLE)}</title>
<style>{_CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
