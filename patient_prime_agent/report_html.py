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
import re
import shutil
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as canvas_module
from pypdf import PdfReader, PdfWriter

from .core.utils import ensure_dir
from .ddi_flag_data import ARTIFACT_URL as DDI_FLAG_ARTIFACT_URL
from .ddi_flag_data import GREEN_FLAGS as DDI_GREEN_FLAGS
from .ddi_flag_data import RED_FLAGS as DDI_RED_FLAGS
from .ddi_flag_data import SOURCE_NOTE as DDI_FLAG_SOURCE_NOTE

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
SEC_EXTERNAL_EVIDENCE = "External_Evidence_Report"
SEC_DDI_FLAG_EVIDENCE = "Flag_Sheet_Live_Evidence"

CURRENT_REGIMEN_DRUG_NAMES = ("levetiracetam", "lamotrigine")

# Rank per pgx predicted_effect category, used to pick the single worst/most
# clinically significant finding per current-regimen drug. (Section 07's
# Table 1 renders this as a plain-language sentence and relevance tier via
# _pgx_interpretation_sentence/_pgx_clinical_relevance, not a colored badge --
# see that pair of functions for the corresponding "uncolored" phrasing map.)
_PGX_EFFECT_RANK: dict[str, int] = {"toxicity": 3, "reduced efficacy": 2, "moderate": 1, "efficacy": 0}

# Severity color class per DDI therapy_assessment "position" value (patient_prime_agent.path_d.ddi.
# aggregation.build_therapy_assessment) -- a display-only recolor of an already-real, deterministically
# computed classification, not a new judgment made here. A dedicated map rather than _status_class's
# keyword heuristic, since these are a fixed enum where the keyword match would misfire (e.g.
# "reconciliation_required" and "effectiveness_incomplete" contain no high/moderate/low keyword at all).
_DDI_POSITION_CLASS: dict[str, str] = {
    "continuation_supported": "sev-low",
    "continuation_with_monitoring": "sev-mod",
    "effectiveness_incomplete": "sev-mod",
    "indication_supported_response_inadequate": "sev-mod",
    "evidence_insufficient": "sev-neutral",
    "high_caution": "sev-high",
    "reconciliation_required": "sev-high",
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


def _print_html_to_pdf(html_path: Path, pdf_path: Path, *, max_attempts: int = 2, poll_timeout: float = 12.0) -> None:
    edge = _find_edge()
    last_result: subprocess.CompletedProcess | None = None
    for attempt in range(1, max_attempts + 1):
        # A fresh --user-data-dir per invocation avoids two real failure modes seen with
        # Edge's default shared profile: (a) a launch can fail to print at all if the
        # user's own Edge windows hold that profile's lock, and (b) repeated prints of the
        # same local file path can silently serve a stale disk-cache render instead of the
        # just-written HTML.
        # ignore_cleanup_errors=True: Edge's crashpad/GPU helper processes can hold a lock
        # on a profile-dir file for a moment after the main process exits -- without this,
        # that race turns into an unrelated OSError on the way out.
        with tempfile.TemporaryDirectory(prefix="prime_agent_edge_", ignore_cleanup_errors=True) as profile_dir:
            cmd = [
                edge,
                "--headless",
                "--disable-gpu",
                "--disk-cache-dir=" + str(Path(profile_dir) / "cache"),
                f"--user-data-dir={profile_dir}",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path.resolve()}",
                html_path.resolve().as_uri(),
            ]
            last_result = subprocess.run(cmd, capture_output=True, timeout=90)
            if last_result.returncode == 0:
                # Confirmed by direct observation: this Edge build can hand control back to
                # this process (exit 0, no stdout/stderr) before a detached worker has
                # actually finished writing the PDF -- the file has been seen to appear up
                # to ~3s later. Poll here, inside the `with` block, so the profile/cache
                # directory that worker may still be using stays alive while we wait,
                # instead of racing its own cleanup against a background write.
                deadline = time.monotonic() + poll_timeout
                while time.monotonic() < deadline:
                    if pdf_path.exists():
                        return
                    time.sleep(0.2)
        if pdf_path.exists():
            pdf_path.unlink(missing_ok=True)
    raise RuntimeError(
        f"Headless Edge failed to print the report to PDF after {max_attempts} attempt(s) "
        f"(exit {last_result.returncode}): {last_result.stderr.decode(errors='replace')[:500]}"
    )


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
.flagpair-table td {{ vertical-align:top; width:50%; }}
.flagpair-block {{ padding:6px 0; border-top:1px solid var(--line); }}
.flagpair-block:first-child {{ border-top:0; padding-top:0; }}
.flagpair-title {{ font-weight:800; color:var(--navy2); margin-bottom:2px; }}
.flagpair-line {{ margin:1px 0; }}
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
a {{ color:var(--teal); text-decoration:none; }}
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
# Live external-evidence rendering, shared by Sections 06 and 07.
#
# Every cell below reads directly out of reports/external_evidence/
# External_Evidence_Report.json (patient_prime_agent.external_evidence_summary),
# which itself is the on-disk record of real HTTP calls this pipeline made to
# PubMed, ClinVar, DailyMed, CPIC, and ClinicalTrials.gov -- see that module's
# docstring for the request/cache contract. Nothing here re-queries an API or
# invents a record; a missing section, a "no_results" status, or any of
# http_client's failure statuses (rate_limited/network_error/http_error/
# invalid_response -- see external_lookup/http_client.py) is displayed as
# such via _error_message_cell, never silently upgraded to a finding.
# ----------------------------------------------------------------------
_GENE_TOKEN_RE = re.compile(r"^\s*([A-Za-z0-9]+)")
_CITATION_PMID_RE = re.compile(r"(\d{6,9})")


def _gene_from_basis(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    match = _GENE_TOKEN_RE.match(text)
    return match.group(1).upper() if match else ""


def _citation_pmid(citation: Any) -> str | None:
    """Pull the bare PMID digits out of the patient genetics panel's own
    citation field (e.g. "PMID:28343093") so it can be compared against a
    live PubMed result's pmid -- a display comparison only, never a change
    to either source's own data."""
    if not isinstance(citation, str):
        return None
    match = _CITATION_PMID_RE.search(citation)
    return match.group(1) if match else None


def _live_badge(from_cache: bool) -> str:
    cls = "sev-neutral" if from_cache else "sev-low"
    label = "CACHED" if from_cache else "LIVE"
    return f'<span class="badge {cls}">{label}</span>'


def _is_cached(envelope: dict[str, Any]) -> bool:
    """Whether ``envelope`` was recalled from external_lookup's long-term
    memory store (memory_store.py, 30-day TTL) rather than freshly
    verified this run. Prefers the explicit "verification" field
    memory_store.recall_or_compute stamps on every envelope; falls back to
    the older "from_cache" flag (set by http_client's own low-level,
    no-TTL, per-URL disk cache) for any envelope that predates it."""
    verification = envelope.get("verification")
    if verification is not None:
        return verification == "cached"
    return bool(envelope.get("from_cache"))


def _unresolved_badge() -> str:
    """Missing or failed coverage is always labeled UNRESOLVED, never left
    to read as a quiet, reassuring negative. A "no_results" status or any
    of http_client's failure statuses means this exact query was not
    resolved -- it says nothing about whether the underlying question has
    a clean answer, and must never be presented as if it did."""
    return '<span class="badge sev-neutral">UNRESOLVED</span>'


def _unresolved_cell(message: str) -> str:
    return f'{_unresolved_badge()} <span class="muted small">{_t(message)}</span>'


def _error_message_cell(envelope: dict[str, Any]) -> str:
    """Render a failed lookup's explanation. external_lookup's http_client
    classifies *why* a call failed (rate-limited / network error / http
    error / invalid response -- see http_client.classify_error) rather than
    collapsing every failure into one generic status, so this prefers that
    classification's "Not retrieved in this session" note and only falls
    back to the raw error string when no note was attached. Always shown
    with the same UNRESOLVED badge as a "no_results" cell -- a failed
    lookup is missing coverage too, not a distinct, worse-sounding state."""
    message = envelope.get("note") or f"Lookup error: {_raw_text(envelope.get('error'))}"
    return _unresolved_cell(message)


def _provenance_line(envelope: dict[str, Any], *, id_label: str, record_id: Any, version: Any = None, id_already_shown: bool = False) -> str:
    """Compact provenance footer for a rendered finding: the record's own ID
    and retrieval date, plus a version only when the source actually
    returns one (DailyMed's spl_version -- every other source here has no
    such field, so it's simply omitted rather than shown as a placeholder).
    The resource name is deliberately not repeated here -- the surrounding
    table's own column header (PubMed / ClinVar / CPIC / DailyMed /
    ClinicalTrials.gov) already identifies it. The LIVE/CACHED badge is
    deliberately omitted too, per user feedback that it added clutter
    without changing what a reader does with the finding; the retrieval
    date itself is kept as the one freshness signal.

    ``id_already_shown=True`` (PubMed and ClinVar both link the record's own
    ID as their main line's clickable text, e.g. "PMID 123456: <title>") omits
    the id_label/record_id repeat here -- printing it again right below was a
    literal duplicate of the ID just shown one line above, not a second fact.
    CPIC's main line links the *guideline name*, not its DrugID, so CPIC
    still passes id_already_shown=False and keeps the ID here -- that's the
    only place it's shown."""
    retrieved = _pretty_date(envelope["retrieved_at"]) if envelope.get("retrieved_at") else NOT_AVAILABLE
    version_part = f" &middot; v{_t(version)}" if version not in (None, "") else ""
    if id_already_shown:
        return f'<div class="small muted" style="margin-top:3px">Retrieved {retrieved}</div>'
    return f'<div class="small muted" style="margin-top:3px">{id_label} {_t(record_id)}{version_part} &middot; Retrieved {retrieved}</div>'


def _evidence_scope_note() -> str:
    """Fixed scope statement repeated on every live-evidence card: exactly
    which sources this pipeline actually queries live, so absence of a
    resource from this table is never mistaken for that resource having
    nothing relevant -- see _known_limitations_block for the full,
    already-generated limitations text this note points to."""
    return (
        '<p class="small muted" style="margin-top:6px"><b>Evidence Scope:</b> live-verified via ClinVar, CPIC, '
        "DailyMed, PubMed, and ClinicalTrials.gov only. The rest of the NeuroTwin resource catalog "
        "(PharmGKB, DrugBank, Reactome, STRING, GTEx, and others) is out of scope for this live-lookup layer "
        "and is not reflected anywhere in this table &mdash; see Known Limitations below.</p>"
    )


def _known_limitations_block(external: dict[str, Any]) -> str:
    """Renders external_evidence_summary's own recorded ``limitations`` list
    verbatim -- real text already generated by that module, never invented
    here -- so "see Known Limitations" above always points at something
    concrete rather than a dangling reference."""
    items = external.get("limitations") or []
    if not items:
        return ""
    return (
        '<div class="callout gap" style="margin-top:4px"><b>Known Limitations (External Evidence):</b>'
        f'<ul class="list-compact">{"".join(f"<li>{_t(i)}</li>" for i in items)}</ul></div>'
    )


def _retrieved_cell(envelopes: list[dict[str, Any]]) -> str:
    valid = [e for e in envelopes if isinstance(e, dict) and e.get("retrieved_at")]
    if not valid:
        return NOT_AVAILABLE
    newest = max(valid, key=lambda e: e["retrieved_at"])
    all_cached = all(_is_cached(e) for e in valid)
    return f"{_pretty_date(newest['retrieved_at'])}<br>{_live_badge(all_cached)}"


def _pubmed_cell(pubmed: dict[str, Any] | None) -> str:
    if not pubmed:
        return NOT_AVAILABLE
    status = pubmed.get("status")
    if status == "ok" and pubmed.get("records"):
        rec = pubmed["records"][0]
        # Full title, no hard character-count truncation: the table cell already wraps
        # (no white-space:nowrap on .datatable td), and slicing an HTML-escaped string
        # by character count risked both a mid-word cut and a mid-entity cut (e.g.
        # "...Epileptic&amp" with the closing ";" sliced off).
        title = _t(rec.get("title")) or "(title not returned by PubMed)"
        return (
            f'<a href="{xml_escape(_raw_text(rec.get("url")))}">PMID {_t(rec.get("pmid"))}</a>: {title}'
            f'{_provenance_line(pubmed, id_label="PMID", record_id=rec.get("pmid"), id_already_shown=True)}'
        )
    if status == "no_results":
        return _unresolved_cell("No PubMed citation matched this exact query at the time checked.")
    if pubmed.get("error"):
        return _error_message_cell(pubmed)
    return NOT_AVAILABLE


def _clinvar_significance_badge(text: Any) -> str:
    """Colors ClinVar's OWN clinical_significance text -- a display recolor
    of a real, already-returned classification, never a new judgment
    computed here. Green is reserved for a narrow, specific SUPPORTED
    classification (Benign/Likely benign); red for a specific reported
    CONFLICT (Pathogenic/Likely pathogenic, or ClinVar's own "Conflicting
    interpretations" label); anything else (Uncertain significance, not
    provided, ...) stays neutral -- it is not evidence either way and must
    not be colored as if it were."""
    lowered = str(text or "").lower()
    if "conflicting" in lowered or "pathogenic" in lowered:
        cls = "sev-high"
    elif "benign" in lowered:
        cls = "sev-low"
    else:
        cls = "sev-neutral"
    return f'<span class="badge {cls}">{_t(text) or "Not classified"}</span>'


def _clinvar_cell(clinvar: dict[str, Any] | None) -> str:
    if not clinvar:
        return NOT_AVAILABLE
    status = clinvar.get("status")
    if status == "ok" and clinvar.get("records"):
        rec = clinvar["records"][0]
        total = clinvar.get("total_matches_in_clinvar")
        record_id = rec.get("accession") or rec.get("uid")
        return (
            f'<a href="{xml_escape(_raw_text(rec.get("url")))}">{_t(rec.get("accession"))}</a>: '
            f'{_clinvar_significance_badge(rec.get("clinical_significance"))}'
            f'<br><span class="small muted">{_t(total)} total ClinVar record(s) for this gene '
            f"(gene-wide count, not variant-specific)</span>"
            f'{_provenance_line(clinvar, id_label="Accession", record_id=record_id, id_already_shown=True)}'
        )
    if status == "no_results":
        return _unresolved_cell("No ClinVar record matched this exact gene query at the time checked.")
    if clinvar.get("error"):
        return _error_message_cell(clinvar)
    return NOT_AVAILABLE


def _cpic_guideline_badge(has_guideline: bool) -> str:
    """Green is reserved for an actual active CPIC dosing guideline on this
    exact gene-drug pair -- a narrow, specific, actionable proposition
    CPIC itself published. No guideline is shown neutral, never green:
    CPIC not having written a guideline for this pair is not the same as
    CPIC having cleared it, and must not read as a reassuring result."""
    return (
        '<span class="badge sev-low">ACTIVE GUIDELINE</span>'
        if has_guideline
        else '<span class="badge sev-neutral">NO ACTIVE GUIDELINE</span>'
    )


def _cpic_cell(cpic: dict[str, Any] | None) -> str:
    if not cpic:
        return NOT_AVAILABLE
    status = cpic.get("status")
    if status == "ok" and cpic.get("records"):
        rec = cpic["records"][0]
        level = _t(rec.get("cpic_level"))
        guideline = rec.get("guideline")
        has_guideline = bool(guideline and guideline.get("url"))
        detail = (
            f'<a href="{xml_escape(_raw_text(guideline.get("url")))}">{_t(guideline.get("name"))}</a>'
            if has_guideline
            else "No active CPIC dosing guideline for this exact pair"
        )
        return (
            f"{_cpic_guideline_badge(has_guideline)} Level {level}<br>{detail}"
            f'{_provenance_line(cpic, id_label="DrugID", record_id=rec.get("drugid"))}'
        )
    if status == "no_results":
        return _unresolved_cell(_t(cpic.get("note")) or "No CPIC gene-drug pair on record.")
    if cpic.get("error"):
        return _error_message_cell(cpic)
    return NOT_AVAILABLE


def _dailymed_cell(dailymed: dict[str, Any] | None) -> str:
    if not dailymed:
        return NOT_AVAILABLE
    status = dailymed.get("status")
    if status == "ok" and dailymed.get("records"):
        rec = dailymed["records"][0]
        title = _t(rec.get("title"))  # full title -- the cell wraps; see _pubmed_cell for why not sliced
        return (
            f'<a href="{xml_escape(_raw_text(rec.get("url")))}">{title}</a>'
            f'<br><span class="small muted">Published {_t(rec.get("published_date"))}</span>'
            f'{_provenance_line(dailymed, id_label="SetID", record_id=rec.get("setid"), version=rec.get("spl_version"))}'
        )
    if status == "no_results":
        return _unresolved_cell("No DailyMed label matched this exact drug name at the time checked.")
    if dailymed.get("error"):
        return _error_message_cell(dailymed)
    return NOT_AVAILABLE


def _trials_cell(trials: dict[str, Any] | None) -> str:
    if not trials:
        return NOT_AVAILABLE
    status = trials.get("status")
    if status == "skipped":
        return _unresolved_cell(_raw_text(trials.get("note")))
    if status == "ok" and trials.get("records"):
        rec = trials["records"][0]
        total = trials.get("total_matches_on_clinicaltrials_gov")
        total_note = f" &middot; {_t(total)} total match(es)" if total is not None else ""
        title = _t(rec.get("brief_title"))  # full title -- the cell wraps; see _pubmed_cell for why not sliced
        return (
            f'<a href="{xml_escape(_raw_text(rec.get("url")))}">{_t(rec.get("nct_id"))}</a>: {title}'
            f'<br><span class="small muted">{_t(rec.get("overall_status"))}{total_note}</span>'
            f'{_provenance_line(trials, id_label="NCT", record_id=rec.get("nct_id"))}'
        )
    if status == "no_results":
        return _unresolved_cell("No trial matched this exact condition/drug query at the time checked.")
    if trials.get("error"):
        return _error_message_cell(trials)
    return NOT_AVAILABLE


def _pgx_cpic_status(cpic: dict[str, Any] | None) -> tuple[bool, str]:
    """Short, plain (no badge, no color) status text for one CPIC lookup, plus
    whether it actually resolved. Detailed provenance (DrugID, guideline name,
    retrieval date) is intentionally NOT built here -- see
    _pgx_provenance_item, which renders that below the table instead."""
    if not cpic:
        return False, NOT_AVAILABLE
    status = cpic.get("status")
    if status == "ok" and cpic.get("records"):
        rec = cpic["records"][0]
        has_guideline = bool(rec.get("guideline") and rec["guideline"].get("url"))
        return True, f"Level {_t(rec.get('cpic_level'))}, {'active guideline' if has_guideline else 'no active guideline'}"
    if status == "no_results" or cpic.get("error"):
        return False, "Unresolved"
    return False, NOT_AVAILABLE


def _pgx_clinvar_status(clinvar: dict[str, Any] | None) -> tuple[bool, str]:
    """Short, plain status text for one ClinVar lookup. ClinVar's OWN
    clinical_significance text is shown as-is, uncolored -- see
    _clinvar_significance_badge (used elsewhere) for the colored version this
    table deliberately does not use."""
    if not clinvar:
        return False, NOT_AVAILABLE
    status = clinvar.get("status")
    if status == "ok" and clinvar.get("records"):
        rec = clinvar["records"][0]
        return True, _t(rec.get("clinical_significance")) or "Not classified"
    if status == "no_results" or clinvar.get("error"):
        return False, "Unresolved"
    return False, NOT_AVAILABLE


def _pgx_pubmed_status(pubmed: dict[str, Any] | None) -> tuple[bool, str]:
    """Short, plain status text for one PubMed lookup."""
    if not pubmed:
        return False, NOT_AVAILABLE
    status = pubmed.get("status")
    if status == "ok" and pubmed.get("records"):
        rec = pubmed["records"][0]
        return True, f"PMID {_t(rec.get('pmid'))} found"
    if status == "no_results" or pubmed.get("error"):
        return False, "Unresolved"
    return False, NOT_AVAILABLE


def _pgx_overall_status(resolved_flags: list[bool]) -> str:
    """One combined status word per row -- plain text, no color -- summarizing
    how many of the 3 sources (CPIC/ClinVar/PubMed) actually resolved."""
    total = len(resolved_flags)
    resolved = sum(resolved_flags)
    if resolved == total:
        return f"Resolved across all {total} sources"
    if resolved == 0:
        return f"Unresolved across all {total} sources"
    return f"Partially resolved ({resolved} of {total} sources)"


def _evidence_flag(resolved_flags: list[bool]) -> tuple[str, str]:
    """Green/amber/red flag classification for one drug/pair's external-evidence
    verification, driven by how many of its real per-source lookups actually
    resolved. Reuses the report's existing plain text-color severity classes
    (.sev-low/.sev-mod/.sev-high -- see the CSS block above, each just a
    `color:` rule, never a background or border) so the flag reads as colored
    text next to the heading, not as a filled badge box."""
    total = len(resolved_flags)
    resolved = sum(resolved_flags)
    if total and resolved == total:
        return "sev-low", "VERIFIED"
    if resolved == 0:
        return "sev-high", "UNVERIFIED"
    return "sev-mod", "PARTIALLY VERIFIED"


def _flag_block(subject: str, flag_css: str, flag_label: str, bullets: list[str]) -> str:
    """One drug/pair's own flagged block: a bold subject heading, a colored
    plain-text flag label (see _evidence_flag), and up to 2 short explanation
    bullets -- reused wherever computed evidence should be grouped per
    drug/pair under its own heading instead of merged into one shared table
    (e.g. _pgx_live_evidence_card, _ddi_candidate_pair_blocks)."""
    bullet_html = "".join(f"<li>{b}</li>" for b in bullets if b)
    return (
        f'<li style="margin-top:6px"><b>{subject}</b> &nbsp;<span class="{flag_css}">{flag_label}</span>'
        f'<ul class="list-compact">{bullet_html}</ul></li>'
    )


def _pgx_provenance_item(pair_label: str, gene: str, drug: str, cpic: Any, clinvar: Any, pubmed: Any, citation: Any) -> str:
    """Detailed provenance (record ID, retrieval date, cache status) for one
    gene-drug pair's 3 lookups -- rendered as a single list item below the
    table, not inline in a cell, per the table's own plain/uniform styling."""
    parts = []
    if isinstance(cpic, dict) and cpic.get("status") == "ok" and cpic.get("records"):
        rec = cpic["records"][0]
        guideline = rec.get("guideline")
        g_name = f', guideline &ldquo;{_t(guideline.get("name"))}&rdquo;' if guideline and guideline.get("url") else ""
        parts.append(f'CPIC &mdash; DrugID {_t(rec.get("drugid"))}{g_name} &middot; {_live_badge(_is_cached(cpic))} &middot; Retrieved {_pretty_date(cpic.get("retrieved_at"))}')
    if isinstance(clinvar, dict) and clinvar.get("status") == "ok" and clinvar.get("records"):
        rec = clinvar["records"][0]
        record_id = rec.get("accession") or rec.get("uid")
        total = clinvar.get("total_matches_in_clinvar")
        parts.append(f'ClinVar &mdash; Accession {_t(record_id)} ({_t(total)} total gene-wide records) &middot; {_live_badge(_is_cached(clinvar))} &middot; Retrieved {_pretty_date(clinvar.get("retrieved_at"))}')
    live_pmid = None
    if isinstance(pubmed, dict) and pubmed.get("status") == "ok" and pubmed.get("records"):
        rec = pubmed["records"][0]
        live_pmid = rec.get("pmid")
        parts.append(f'PubMed &mdash; PMID {_t(live_pmid)} &middot; {_live_badge(_is_cached(pubmed))} &middot; Retrieved {_pretty_date(pubmed.get("retrieved_at"))}')
    if not parts:
        return ""
    # The patient panel's own citation (Table 1) and this row's independent live
    # PubMed search are two different sources that can legitimately land on two
    # different papers for the same pair -- flag it explicitly rather than let a
    # reader assume they were supposed to match.
    patient_pmid = _citation_pmid(citation)
    discrepancy = ""
    if live_pmid and patient_pmid and str(live_pmid) != patient_pmid:
        discrepancy = " Note: Table 1's citation is from the patient's own PGx report; this is an independent live PubMed search, may differ."
    return f"<li><b>{pair_label}:</b> {'; '.join(parts)}.{discrepancy}</li>"


def _pgx_live_evidence_card(external: dict[str, Any], worst_findings: list[dict[str, Any]]) -> str:
    """External Evidence Verification: live PubMed/ClinVar/CPIC status for
    exactly the gene-drug findings already shown in Table 1 above -- not
    every gene the panel found, only the ones the clinician just read, so
    this stays in lockstep with the visible summary. Per request, this is no
    longer one shared table across every pair -- each gene-drug pair gets its
    own flagged block (colored VERIFIED/PARTIALLY VERIFIED/UNVERIFIED label,
    plain text color only -- see _evidence_flag -- plus exactly 2 short
    explanation bullets), the same visual pattern as the curated
    Green-Flags/Red-Flags sheet further down the report. Detailed provenance
    for each pair is still collected separately and rendered as a plain list
    below the blocks, not inline."""

    pairs_by_key = {
        (str(p.get("drug_name", "")).strip().lower(), str(p.get("gene_symbol", "")).strip().upper()): p
        for p in (external.get("by_gene_drug_pair") or [])
    }
    blocks = []
    provenance_items = []
    for finding in worst_findings:
        drug = str(finding.get("drug") or "").strip()
        gene = _gene_from_basis(finding.get("genetic_basis"))
        pair = pairs_by_key.get((drug.lower(), gene))
        if not pair:
            continue
        cpic, clinvar, pubmed = pair.get("cpic"), pair.get("clinvar"), pair.get("pubmed_combined_query")
        cpic_ok, cpic_text = _pgx_cpic_status(cpic)
        clinvar_ok, clinvar_text = _pgx_clinvar_status(clinvar)
        pubmed_ok, pubmed_text = _pgx_pubmed_status(pubmed)
        overall = _pgx_overall_status([cpic_ok, clinvar_ok, pubmed_ok])
        flag_css, flag_label = _evidence_flag([cpic_ok, clinvar_ok, pubmed_ok])
        pair_label = f"{_t(gene)} &rarr; {_t(_sentence_case(drug))}"
        blocks.append(
            _flag_block(
                pair_label,
                flag_css,
                flag_label,
                [f"CPIC: {cpic_text}. ClinVar: {clinvar_text}.", f"PubMed: {pubmed_text}. Overall: {overall}."],
            )
        )
        item = _pgx_provenance_item(pair_label, gene, drug, cpic, clinvar, pubmed, finding.get("citation"))
        if item:
            provenance_items.append(item)
    if not blocks:
        return ""
    provenance_block = (
        f'<div class="callout" style="margin-top:8px"><b>Provenance</b><ul class="list-compact">{"".join(provenance_items)}</ul></div>'
        if provenance_items
        else ""
    )
    return f"""
  <div class="h2" style="margin-top:10px">External Evidence Verification</div>
  <p class="small muted" style="margin-bottom:6px"><b>Database evidence only, not patient-verified:</b> each block
  below is a live public-database lookup for the same gene/drug pair as the patient-specific finding in Table 1
  above -- it establishes general scientific plausibility for that pair, not a fact confirmed for this
  individual patient. An unresolved result means this exact query returned nothing, or failed, at the time
  checked -- it is not evidence that no relevant data exists, and must not be read as a clean or reassuring
  negative.</p>
  <ul class="list-compact" style="padding-left:12px">{''.join(blocks)}</ul>
  {provenance_block}
  {_evidence_scope_note()}
  {_known_limitations_block(external)}
"""


# _ddi_live_evidence_card (the old "External Database Evidence -- DailyMed /
# ClinicalTrials.gov / PubMed" table for the current-regimen drugs) was
# removed: confirmed via a PDF-text identifier scan (matching PMIDs, NCT
# numbers, and DailyMed SetIDs) that every fact it showed was already
# present, in fuller form, inside each drug's own Unresolved Evidence
# bullets in _therapy_assessment_card below (which additionally cover
# CPIC/ClinVar, absent from that table) -- and its Evidence Scope /
# Known Limitations blocks duplicated the identical text already rendered
# once in Section 07 via _pgx_live_evidence_card. Single source of truth
# for both now: the therapy cards for per-drug external evidence, Section 07
# for the scope/limitations disclaimer.


# ----------------------------------------------------------------------
# 07. Pharmacogenomics
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


def _pgx_summary_chips(genetics: dict[str, Any]) -> str:
    """Real per-gene chips from the patient's own metabolizer_profile (genetics_summary.py)
    -- the panel-wide genotype/phenotype overview, shown before narrowing to the findings
    that actually name a current-regimen drug. Every gene the panel tested is shown; none
    is picked or omitted based on what would look tidier."""
    profile = _get(genetics, "metabolizer_profile", default=[])
    if not isinstance(profile, list) or not profile:
        return ""
    chips = "".join(
        f'<div class="chip"><b>{_t(g.get("gene"))}</b><span>{_t(g.get("status"))}</span></div>'
        for g in profile
        if isinstance(g, dict)
    )
    return f'<div class="grid5" style="margin-bottom:8px">{chips}</div>'


_PGX_INTERPRETATION_SENTENCE: dict[str, str] = {
    "efficacy": "Genotype is associated with a favorable efficacy response to this therapy.",
    "reduced efficacy": "Genotype is associated with a reduced-efficacy response to this therapy.",
    "toxicity": "Genotype is associated with an increased toxicity/adverse-reaction risk on this therapy.",
    "moderate": "Genotype is associated with a moderate or mixed response to this therapy.",
}

_PGX_RELEVANCE_TIER: dict[str, str] = {
    "toxicity": "High",
    "reduced efficacy": "Moderate",
    "moderate": "Moderate",
    "efficacy": "Supportive",
}


def _pgx_interpretation_sentence(entry: dict[str, Any]) -> str:
    """Plain-language sentence for this finding's real predicted_effect enum
    (efficacy / reduced efficacy / toxicity / moderate) -- never displays the
    raw enum value directly."""
    effect = str(entry.get("predicted_effect") or "").strip().lower()
    sentence = _PGX_INTERPRETATION_SENTENCE.get(effect)
    return _t(sentence) if sentence else _t(_sentence_case(entry.get("predicted_effect")))


def _pgx_clinical_relevance(entry: dict[str, Any]) -> str:
    """Whether this finding is flagged as clinically significant in the
    source panel, plus its real citation -- a distinct judgment from
    _pgx_interpretation_sentence's "what the genotype means," so the two
    table columns state different facts rather than repeating one."""
    effect = str(entry.get("predicted_effect") or "").strip().lower()
    tier = _PGX_RELEVANCE_TIER.get(effect, "Uncertain")
    flag_text = "flagged as clinically significant" if entry.get("significant") else "not flagged as clinically significant"
    citation = entry.get("citation")
    citation_part = f" (citation: {_t(citation)})" if citation else ""
    return f"{_t(tier)} relevance; {flag_text} in the source panel{citation_part}."


def _sec_pharmacogenomics(sections: dict[str, Any]) -> str:
    genetics = sections.get(SEC_GENETICS) or {}
    external = sections.get(SEC_EXTERNAL_EVIDENCE) or {}

    current_findings = []
    for entry in _get(genetics, "findings_by_therapeutic_class", "mood_stabilizers_antiepileptics", default=[]):
        drug = str(entry.get("drug", "")).lower()
        if any(d in drug for d in CURRENT_REGIMEN_DRUG_NAMES):
            current_findings.append(entry)

    # Citation = the real per-finding source reference from the PGx panel's own data
    # (a PMID/PMCID carried straight through from the source spreadsheet's "pmid" column
    # via genetics_summary.py) -- not a generic panel-level note, and never invented.
    worst_findings = _pgx_worst_per_drug(current_findings)
    worst_rows = "".join(
        f'<tr><td>{_t(e.get("drug"))}</td><td>{_t(e.get("genetic_basis"))}</td>'
        f'<td>{_pgx_interpretation_sentence(e)}</td>'
        f'<td>{_pgx_clinical_relevance(e)}</td></tr>'
        for e in worst_findings
    )

    return f"""
<section class="section">
  {_section_head(7, "Pharmacogenomics", f"{genetics.get('patient', {}).get('variants_analyzed', '?')} variants across {genetics.get('patient', {}).get('drugs_covered', '?')} medications \u00b7 reported {_pretty_date(_get(genetics, 'patient', 'report_date'))}")}
  <div class="h2">Pharmacogenomic Panel Summary</div>
  {_pgx_summary_chips(genetics)}
  <div class="callout" style="margin-bottom:8px"><b>Interpretive method:</b> a pharmacogenomic association is weighed against this patient's own observed clinical response, laboratory, and EEG evidence -- it is never used as an isolated prescribing instruction, and a favorable genotype cannot override documented toxicity or persistent symptoms. The table below shows the single most clinically significant finding per current-regimen drug; the full therapy-level assessment -- this finding combined with clinical, EEG, laboratory, and external-database evidence -- is presented in Section 06, Drug Interactions.</div>
  <div class="h2">Patient-Specific PGx Findings</div>
  <table class="datatable"><thead><tr><th>Therapy</th><th>Gene/Genotype</th><th>Patient-Specific Interpretation</th><th>Clinical Relevance</th></tr></thead><tbody>{worst_rows or '<tr><td colspan="4">No marker in this panel names a current-regimen drug directly.</td></tr>'}</tbody></table>
  {_pgx_live_evidence_card(external, worst_findings)}
</section>
"""


def _short_source(ref: Any) -> str:
    """Compact, human-readable provenance label for an evidence item's
    ``source_reference`` -- a URL is shown as host + trailing path, a JSON
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


def _ddi_evidence_list(items: list[dict[str, Any]] | None) -> str:
    """One <li> per real evidence item (patient_prime_agent.path_d.ddi.aggregation's
    EvidenceItem shape: modality/direction/statement/source_reference/evidence_level).
    An empty list is stated as such -- per DDI_Integration_Plan_v02 section 15, "unresolved"
    is never collapsed into "no evidence," so a genuinely empty bucket says so explicitly
    rather than rendering a blank list. Each item's real source_reference is shown inline
    (per DDI_Integration_Plan_v02 section 14, every displayed finding must carry its
    provenance) rather than only in the live-evidence tables further down the section."""
    items = items or []
    if not items:
        return '<li class="muted">None identified in the available sources.</li>'
    return "".join(
        f'<li>{_t(item.get("statement"))} <span class="muted small">&mdash; {_t(_short_source(item.get("source_reference")))}</span></li>'
        for item in items
    )


_EVIDENCE_LEVEL_RANK: dict[str, int] = {"high": 3, "moderate": 2, "low": 1, "predicted": 0}


def _top_evidence_items(items: list[dict[str, Any]] | None, limit: int = 3) -> list[dict[str, Any]]:
    """Reduces a full evidence-item list to the ``limit`` most load-bearing
    entries, per DDI_Integration_Plan_v02's own conflict-resolution rules --
    rule 1 ("patient-specific observed evidence outranks predicted evidence")
    and rule 3 ("clinical response outranks a favorable response
    association") -- by sorting on each item's own real ``evidence_level``
    (high/moderate/low/predicted) rather than truncating in whatever order
    aggregation.py happened to emit them. A stable sort preserves that
    original order among items tied on evidence_level. This never drops the
    underlying data (the full lists still exist in DDI_Clinical_Assessment.json
    and DDI_Clinical_Assessment's own JSON); it only narrows what this
    specific card displays."""
    items = items or []
    ranked = sorted(items, key=lambda item: _EVIDENCE_LEVEL_RANK.get(str(item.get("evidence_level") or "").lower(), -1), reverse=True)
    return ranked[:limit]


def _therapy_assessment_card(therapy: dict[str, Any]) -> str:
    """Renders one patient_prime_agent.path_d.ddi.aggregation.build_therapy_assessment()
    record: therapy name, current clinical position, the 3 most load-bearing supporting
    and counter-evidence points (see _top_evidence_items), clinical impression, and
    recommended monitoring. Unresolved evidence is intentionally not shown here (by
    request, to match the reference report's 2-column current-therapy layout) -- it
    remains fully available in DDI_Clinical_Assessment.json, just not rendered in this
    card. Dose is deliberately not repeated here -- the Medication Reconciliation table
    directly above is this section's single source for it."""
    medication = therapy.get("medication") or {}
    position = therapy.get("position")
    position_badge = f'<span class="badge {_DDI_POSITION_CLASS.get(str(position or "").lower(), "sev-neutral")}">{_humanize(position)}</span>'
    # Plain-clinical-language statement for the same computed position, per
    # DDI_Integration_Plan_v02 section 13 ("never display the raw enum") --
    # _POSITION_KEY_FINDING already exists below for the section-level summary
    # table; shown here too so the individual therapy card carries it directly
    # rather than only the enum badge.
    position_statement = _POSITION_KEY_FINDING.get(position, _humanize(position))

    monitoring = therapy.get("recommended_monitoring") or []
    monitoring_pills = "".join(f'<span class="pill">{_t(m)}</span>' for m in monitoring) or '<span class="pill">None recorded.</span>'

    return f"""
  <div class="card" style="margin-top:8px">
    <div class="h2">{_t(medication.get('source_name'))} &nbsp; {position_badge}</div>
    <p class="small" style="margin-top:-4px;margin-bottom:6px">{_t(position_statement)}</p>
    <div class="grid2">
      <div>
        <div class="h2" style="font-size:8.6px">Supporting Evidence</div>
        <ul class="list-compact">{_ddi_evidence_list(_top_evidence_items(therapy.get('supporting_evidence')))}</ul>
      </div>
      <div>
        <div class="h2" style="font-size:8.6px">Counter-Evidence</div>
        <ul class="list-compact">{_ddi_evidence_list(_top_evidence_items(therapy.get('counter_evidence')))}</ul>
      </div>
    </div>
    <div class="h2" style="margin-top:6px">Clinical Impression</div>
    <p class="small">{_t(therapy.get('clinical_impression'))}</p>
    <div class="h2" style="margin-top:6px">Recommended Monitoring</div>
    <div class="pill-row">{monitoring_pills}</div>
  </div>
"""


# ----------------------------------------------------------------------
# 06. Drug Interactions -- top-of-section summary overview
#
# Combines current_pair_assessments (pair-level DDI screening) and
# therapy_assessments (per-drug evidence assessment) into one at-a-glance
# table, computed entirely from real, already-generated fields -- never a
# hardcoded per-drug row. The detailed per-therapy evidence cards below
# remain the full record; this is an additive index into them, not a
# replacement.
# ----------------------------------------------------------------------

# Evidence-item modality -> which real layer produced it, for the summary
# table's "Evidence Source" column (a display label, not a new computation --
# every modality here is one already assigned by path_d.ddi.aggregation).
_MODALITY_LAYER_LABEL: dict[str, str] = {
    "pharmacokinetic_ddi": "Curated DDI (Flockhart)",
    "pharmacodynamic_ddi": "Pharmacodynamic rule",
    "pharmacogenomic": "Patient pharmacogenomic findings",
    "clinical": "Clinical record",
    "eeg": "EEG findings",
    "laboratory": "Laboratory findings",
    "literature": "External evidence (PubMed)",
    "genomic_evidence": "External evidence (ClinVar)",
    "labeling": "External evidence (DailyMed)",
    "clinical_trial": "External evidence (ClinicalTrials.gov)",
}

# One short clinical sentence per therapy_assessment "position" value -- keyed by the
# computed enum value, never by drug name, so this generalizes to any medication that
# reaches that position.
_POSITION_KEY_FINDING: dict[str, str] = {
    "continuation_supported": "Supporting evidence outweighs counter-evidence for continuation.",
    "continuation_with_monitoring": "Mixed evidence; continuation supported with monitoring.",
    "effectiveness_incomplete": "Therapeutic evidence present, but clinical effectiveness remains incomplete.",
    "indication_supported_response_inadequate": "Indication is supported, but clinical response remains inadequate.",
    "evidence_insufficient": "Evidence is insufficient to support a therapeutic conclusion.",
    "high_caution": "High-caution counter-evidence identified for this therapy.",
    "reconciliation_required": "Medication reconciliation is required before further interpretation.",
}

# Traffic-light label per severity color class, reused for both pair and therapy rows --
# green means "no resolved major finding," never "confirmed safe."
_FLAG_LABEL_BY_CLASS: dict[str, str] = {
    "sev-low": "NO MAJOR CONCERN",
    "sev-mod": "UNRESOLVED",
    "sev-high": "RISK IDENTIFIED",
    "sev-neutral": "UNRESOLVED",
}
_POSITION_RANK: dict[str, int] = {"sev-high": 3, "sev-mod": 2, "sev-neutral": 1, "sev-low": 0}


def _evidence_source_labels(items: list[dict[str, Any]] | None) -> str:
    labels: list[str] = []
    for item in items or []:
        label = _MODALITY_LAYER_LABEL.get(item.get("modality"))
        if label and label not in labels:
            labels.append(label)
    return "; ".join(labels) if labels else "No resolved evidence source identified"


def _patient_impact_from_impression(clinical_impression: Any) -> str:
    """clinical_impression already carries a real, computed clinical-notes inference
    sentence (patient_prime_agent.path_d.ddi.aggregation.build_therapy_assessment) --
    this reuses that exact text rather than generating new prose."""
    text = _raw_text(clinical_impression)
    marker = "Clinical-notes inference:"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return text


def _pair_evidence_source_items(pair: dict[str, Any]) -> list[dict[str, Any]]:
    """Evidence Source for a pair row: the curated/rule-based items that actually drove
    the pair's status (never "unresolved" ones like the generic SuperCYPsPred-unavailable
    note), PLUS the real pair-level PubMed query (external_evidence_summary.py's
    by_drug_pair, e.g. "Lamotrigine AND Levetiracetam") even though its own direction is
    always "unresolved" -- a genuine combined DDI literature search was performed for this
    exact pair, and that provenance must be visible, not hidden by the same "unresolved
    items don't count as a source" rule that correctly excludes tangential noise."""
    return [e for e in (pair.get("evidence") or []) if e.get("direction") != "unresolved" or e.get("modality") == "literature"]


def _ddi_summary_pair_row(
    pair: dict[str, Any],
    coverage: dict[str, Any],
    name_map: dict[str, str],
    therapy_by_source_name: dict[str, dict[str, Any]],
) -> dict[str, str]:
    status = pair.get("status")
    if status == "interaction_detected":
        css_class = "sev-high"
        mechanisms = pair.get("mechanisms") or []
        key_finding = _raw_text((mechanisms[0] if mechanisms else {}).get("description")) if mechanisms else "Clinically relevant interaction mechanism identified."
        evidence_items = _pair_evidence_source_items(pair)
    elif status == "no_interaction_detected":
        drug_a_norm = name_map.get(pair.get("drug_a"))
        drug_b_norm = name_map.get(pair.get("drug_b"))
        unresolved = set(coverage.get("unresolved_drugs") or [])
        css_class = "sev-mod" if (drug_a_norm in unresolved or drug_b_norm in unresolved) else "sev-low"
        key_finding = "No resolved major DDI identified between these two current medications."
        evidence_items = _pair_evidence_source_items(pair)
    else:  # not_evaluated -- e.g. a proposed-drug pair, not applicable to the current regimen
        css_class = "sev-mod"
        key_finding = "This pair was not evaluated as part of the current regimen."
        evidence_items = []

    worse_therapy: dict[str, Any] | None = None
    for name in (pair.get("drug_a"), pair.get("drug_b")):
        candidate = therapy_by_source_name.get(name)
        if candidate is None:
            continue
        candidate_rank = _POSITION_RANK.get(_DDI_POSITION_CLASS.get(str(candidate.get("position") or "").lower(), "sev-neutral"), 0)
        worse_rank = _POSITION_RANK.get(_DDI_POSITION_CLASS.get(str((worse_therapy or {}).get("position") or "").lower(), "sev-neutral"), -1)
        if worse_therapy is None or candidate_rank > worse_rank:
            worse_therapy = candidate
    patient_impact = (
        f"{_patient_impact_from_impression(worse_therapy.get('clinical_impression'))} This reflects therapy-level "
        "effectiveness, not a confirmed drug interaction."
        if worse_therapy is not None
        else "No therapy-level effectiveness concern is connected to this pair."
    )

    return {
        "subject": f"{_t(pair.get('drug_a'))} + {_t(pair.get('drug_b'))}",
        "flag_css": css_class,
        "flag_label": _FLAG_LABEL_BY_CLASS[css_class],
        "key_finding": _t(key_finding),
        "patient_impact": _t(patient_impact),
        "evidence_source": _t(_evidence_source_labels(evidence_items)),
    }


def _ddi_summary_therapy_row(therapy: dict[str, Any]) -> dict[str, str]:
    medication = therapy.get("medication") or {}
    position = therapy.get("position")
    css_class = _DDI_POSITION_CLASS.get(str(position or "").lower(), "sev-neutral")
    resolved_evidence = (therapy.get("supporting_evidence") or []) + (therapy.get("counter_evidence") or [])
    return {
        "subject": _t(medication.get("source_name")),
        "flag_css": css_class,
        "flag_label": _FLAG_LABEL_BY_CLASS.get(css_class, "UNRESOLVED"),
        "key_finding": _t(_POSITION_KEY_FINDING.get(position, _humanize(position))),
        "patient_impact": _t(_patient_impact_from_impression(therapy.get("clinical_impression"))),
        "evidence_source": _t(_evidence_source_labels(resolved_evidence)),
    }


_FINDING_BUCKETS: tuple[str, ...] = ("clinically_significant", "resolved_no_concern", "unresolved")
_FINDING_BUCKET_LABEL: dict[str, str] = {
    "clinically_significant": "Clinically Significant",
    "resolved_no_concern": "Resolved — No Concern",
    "unresolved": "Unresolved",
}


def _flag_css_to_bucket(flag_css: str) -> str:
    if flag_css == "sev-high":
        return "clinically_significant"
    if flag_css == "sev-low":
        return "resolved_no_concern"
    return "unresolved"  # sev-mod / sev-neutral


def _ddi_finding_counts(ddi: dict[str, Any]) -> dict[str, int]:
    """Single source of truth for every clinically-significant /
    resolved-no-concern / unresolved count shown anywhere in Section 06's
    summary overview card -- both the headline sentence and the stat tiles
    below it are built from this same dict, so they can never diverge
    again. (This fixes a real bug: the headline used to count
    therapy_assessments + current_pair_assessments, while the tiles counted
    an entirely different thing -- curated-source drug coverage from
    source_coverage -- so the two answered different questions and could
    show contradictory numbers, e.g. "2 unresolved" in the headline next to
    "0" in the coverage tile.)

    Counts across two inputs that never overlap: one bucket per
    current-regimen medication's own therapy assessment
    (_ddi_summary_therapy_row's flag_css), and one bucket per real
    current-current pair in current_pair_assessments (_ddi_summary_pair_row's
    flag_css).

    This is deliberately scoped to the patient's real current regimen only.
    The Drug Interaction Flag Sheet's ~325-pair screening panel
    (_ddi_flag_sheet_card, drawn from path_d.ddi.candidate_drugs -- mostly
    drugs the patient is not actually taking) is NOT folded into this
    count: its own Positive/Negative/Needs-Review buckets answer a
    different question ("what does the literature say about this
    screening pair") than this headline's "how is the patient's actual
    regimen doing" -- mixing them would inflate this section's real-patient
    headline with screening-panel noise. The screening panel reports its
    own coverage stats separately, in its own intro."""
    counts = {b: 0 for b in _FINDING_BUCKETS}

    reconciliation = _get(ddi, "medication_reconciliation", default={})
    coverage = _get(ddi, "source_coverage", default={})
    therapy_assessments = _get(ddi, "therapy_assessments", default=[])
    name_map = {m.get("source_name"): m.get("normalized_name") for m in reconciliation.get("normalized_medications") or []}
    therapy_by_source_name = {(t.get("medication") or {}).get("source_name"): t for t in therapy_assessments}

    for therapy in therapy_assessments:
        row = _ddi_summary_therapy_row(therapy)
        counts[_flag_css_to_bucket(row["flag_css"])] += 1

    current_pairs = [p for p in _get(ddi, "current_pair_assessments", default=[]) if p.get("pair_context") == "current_current"]
    for pair in current_pairs:
        row = _ddi_summary_pair_row(pair, coverage, name_map, therapy_by_source_name)
        counts[_flag_css_to_bucket(row["flag_css"])] += 1

    return counts


def _ddi_summary_overview_card(ddi: dict[str, Any]) -> str:
    therapy_assessments = _get(ddi, "therapy_assessments", default=[])
    therapy_rows = [_ddi_summary_therapy_row(t) for t in therapy_assessments]

    counts = _ddi_finding_counts(ddi)
    total_findings = sum(counts.values())
    if total_findings == 0:
        return ""
    clinically_relevant = counts["clinically_significant"] + counts["resolved_no_concern"]
    unresolved = counts["unresolved"]

    # Every stat tile comes from the exact same `counts` dict the headline sentence above
    # uses -- see _ddi_finding_counts -- so the two can never show contradictory numbers.
    stat_tiles = [
        (str(total_findings), "Total Findings Assessed"),
        (str(counts["clinically_significant"]), _FINDING_BUCKET_LABEL["clinically_significant"]),
        (str(counts["resolved_no_concern"]), _FINDING_BUCKET_LABEL["resolved_no_concern"]),
        (str(counts["unresolved"]), _FINDING_BUCKET_LABEL["unresolved"]),
    ]
    stat_html = "".join(f'<div class="stat"><b>{_t(v)}</b><span>{_t(l)}</span></div>' for v, l in stat_tiles)

    # Per-drug rows only -- current-regimen pair rows moved out of this shared table into
    # their own tables in _ddi_flag_sheet_card, so "Drug Pair" would no longer be an
    # accurate header for what remains here.
    table_rows = "".join(
        f'<tr><td>{r["subject"]}</td>'
        f'<td><span class="badge {r["flag_css"]}">{r["flag_label"]}</span></td>'
        f'<td>{r["key_finding"]}</td>'
        f'<td>{r["patient_impact"]}</td>'
        f'<td class="small muted">{r["evidence_source"]}</td></tr>'
        for r in therapy_rows
    )

    return f"""
  <div class="card" style="margin-bottom:8px">
    <div class="h2">DDI &amp; Patient-Specific Therapy Assessment &mdash; Summary</div>
    <p class="small" style="margin:2px 0 6px"><b>Overall: {clinically_relevant} clinically relevant findings | {unresolved} unresolved finding(s).</b></p>
    <div class="grid4" style="margin-bottom:8px">{stat_html}</div>
    <table class="datatable"><thead><tr><th>Medication</th><th>Flag</th><th>Key Finding</th><th>Patient Impact</th><th>Evidence Source</th></tr></thead>
    <tbody>{table_rows}</tbody></table>
    <div class="callout" style="margin-top:8px"><b>Key clinical interpretation:</b> These flags identify findings that require ongoing clinical monitoring, not an automatic medication change. Each finding should be interpreted alongside the patient's actual treatment response, adverse effects, and the clinical/laboratory findings documented elsewhere in this report -- a resolved or unresolved flag is not itself a prescribing instruction.</div>
    <div class="h2" style="margin-top:8px">Monitoring / Clinician Review Checklist</div>
    <ul class="list-compact">
      <li>Medication reconciliation (dose, frequency, and route consistent across all source records)</li>
      <li>Actual treatment response (seizure/symptom control on the current regimen)</li>
      <li>Relevant adverse effects reported since the last review</li>
      <li>Therapeutic drug levels, when indicated and available</li>
      <li>Relevant laboratory and clinical parameters (e.g. renal, hepatic, hematological)</li>
    </ul>
  </div>
"""


def _ddi_candidate_pair_blocks(ddi: dict[str, Any]) -> str:
    """Candidate / Proposed Medication-Pair Screening: one flagged block per
    proposed_pair_assessments entry (a candidate drug paired against a
    current-regimen drug), each under its own heading -- same "own flagged
    block per pair" principle as _ddi_flag_sheet_card, applied here since a
    proposed pair's "not_evaluated" status doesn't fit that sheet's
    Clinically-Significant/No-Significant-Concern taxonomy. This dataset has
    no proposed-medication list
    (see ddi_summary.py's module docstring), so proposed_pair_assessments is
    always empty here; this states that gap explicitly rather than inventing
    a candidate list to fill the section."""
    proposed_pairs = _get(ddi, "proposed_pair_assessments", default=[])
    if not proposed_pairs:
        return """
  <div class="card" style="margin-top:8px">
    <div class="h2">Candidate / Proposed Medication-Pair Screening</div>
    <p class="small muted">No candidate or proposed medications are present in this patient's regimen; no candidate-pair screening applies.</p>
  </div>
"""
    items = "".join(
        _flag_block(
            f'{_t(p.get("drug_a"))} + {_t(p.get("drug_b"))}',
            "sev-mod",
            "NOT EVALUATED",
            [_t(p.get("patient_specific_interpretation")), f'Severity if added: {_t(p.get("severity"))}.'],
        )
        for p in proposed_pairs
    )
    return f"""
  <div class="card" style="margin-top:8px">
    <div class="h2">Candidate / Proposed Medication-Pair Screening</div>
    <ul class="list-compact" style="padding-left:12px">{items}</ul>
  </div>
"""


def _ddi_coverage_action_table(ddi: dict[str, Any]) -> str:
    """Medicine / Status / Meaning / Action table: for each reconciled
    current-regimen drug, whether curated pharmacokinetic (Flockhart)
    reference data actually exists for it (source_coverage's own
    resolved/unresolved/not_evaluated_drugs lists -- never a new
    classification computed here), what that status means in plain
    language, and a coverage-status-specific next action. This is additive
    to the Medication Reconciliation table above (which never varies its
    "current" status) and to the DDI & Therapy Assessment summary card
    (which is pair/therapy-outcome-oriented, not coverage-oriented). The
    Action column is deliberately not a drug's recommended_monitoring text
    -- that already appears once, per drug, in its therapy card below."""
    coverage = _get(ddi, "source_coverage", default={})
    reconciliation = _get(ddi, "medication_reconciliation", default={})
    resolved = coverage.get("resolved_drugs") or []
    unresolved = coverage.get("unresolved_drugs") or []
    not_evaluated = coverage.get("not_evaluated_drugs") or []
    name_by_source = {m.get("source_name"): m.get("normalized_name") for m in reconciliation.get("normalized_medications") or []}
    flockhart_source = _raw_text(coverage.get("flockhart_source"))

    def _row(source_name: Any) -> str:
        normalized = name_by_source.get(source_name)
        if normalized in resolved:
            badge_class, label = "sev-low", "Resolved"
            meaning = f"Curated CYP/pharmacokinetic reference data exists for this drug ({flockhart_source})."
        elif normalized in unresolved:
            badge_class, label = "sev-mod", "Unresolved"
            meaning = "No curated CYP/pharmacokinetic reference entry for this drug; screening relies on pharmacodynamic rules and pharmacogenomic findings only."
        elif normalized in not_evaluated:
            badge_class, label = "sev-mod", "Not Evaluated"
            meaning = "Not present in the curated reference or pharmacogenomic findings; no automated interaction evidence is available."
        else:
            badge_class, label = "sev-neutral", "Not Determined"
            meaning = "Coverage status not determined by this assessment."
        if badge_class == "sev-low":
            action = "No additional action required beyond the current-current pair evidence below."
        elif label == "Not Evaluated":
            action = "Verify coverage manually before adding an interacting medication."
        else:
            action = "Review current-current pair evidence and pharmacogenomic findings below before adding an interacting medication."
        return (
            f"<tr><td>{_t(source_name)}</td><td><span class=\"badge {badge_class}\">{label}</span></td>"
            f"<td>{_t(meaning)}</td><td>{_t(action)}</td></tr>"
        )

    rows = "".join(_row(m.get("source_name")) for m in reconciliation.get("normalized_medications") or [])
    if not rows:
        return ""
    return f"""
  <div class="card" style="margin-top:8px">
    <div class="h2">Current Regimen &mdash; Curated-Source Coverage</div>
    <table class="datatable"><thead><tr><th>Medicine</th><th>Status</th><th>Meaning</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table>
  </div>
"""


def _ddi_clinical_inference_callout(ddi: dict[str, Any]) -> str:
    """Single, visually prominent statement of DDI_Integration_Plan_v02's
    conflict-resolution rule 6 ("unresolved does not mean no interaction")
    and rule 7 ("absence of data does not mean absence of risk") -- this is
    the section's one load-bearing caution. It also introduces what the
    therapy-level evidence cards below actually contain, so that framing is
    stated once here rather than repeated in a second, separately-worded
    callout further down the section."""
    uncertainties = _get(ddi, "overall_interpretation", "principal_uncertainties", default=[])
    items = "".join(f"<li>{_t(u)}</li>" for u in uncertainties)
    return f"""
  <div class="callout warn" style="margin-top:8px">
    <b>Clinical inference:</b> Absence of a resolved current-current interaction does not establish absence of
    interaction. Unresolved predicted-model coverage limits interpretation; the active medication list should be
    reassessed against a complete interaction source before any treatment change. The therapy-level cards below
    give the supporting, counter, and unresolved evidence behind that conclusion for each current medication,
    across pharmacokinetic, pharmacodynamic, pharmacogenomic, clinical, EEG, and laboratory sources -- not a
    pass/fail interaction check.
    {f'<ul class="list-compact" style="margin-top:4px">{items}</ul>' if items else ''}
  </div>
"""


_IMPACT_COLUMN_LABEL: dict[str, str] = {
    "positive": "Positive Impact",
    "negative": "Negative Impact",
}
_IMPACT_ORDER: tuple[str, ...] = ("positive", "negative")


def _ddi_flag_sheet_pairs_by_drug(flag_evidence: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """Every screening-panel pair with real evidence
    (patient_prime_agent.ddi_flag_evidence's output -- already dropped every
    pair with no finding and every pair that didn't resolve clearly to
    Positive or Negative; there is no third "needs review" bucket, see that
    module's own docstring), indexed by EACH of its two drugs
    (lowercased) -- so a pair like "Lamotrigine + Levetiracetam" appears
    once under "lamotrigine" (partner "Levetiracetam") and once under
    "levetiracetam" (partner "Lamotrigine"), letting each drug's own
    section show every pair it is actually part of, from that drug's own
    point of view -- matching the reference layout's per-drug sections."""
    by_drug: dict[str, list[dict[str, Any]]] = {}
    for pair in _get(flag_evidence or {}, "pairs", default=[]):
        drug_a, drug_b = pair.get("drug_a"), pair.get("drug_b")
        if not drug_a or not drug_b:
            continue
        by_drug.setdefault(str(drug_a).strip().lower(), []).append({**pair, "_self": drug_a, "_partner": drug_b})
        by_drug.setdefault(str(drug_b).strip().lower(), []).append({**pair, "_self": drug_b, "_partner": drug_a})
    return by_drug


def _ddi_flag_sheet_hyperlink_anchor_text(primary_evidence_label: str | None) -> str:
    """A short, human-readable link-anchor name for the pair's primary
    evidence source -- ``entry["evidence_labels"][0]``, the same source
    ``ddi_flag_evidence._pair_finding`` drew ``hyperlink_url`` from (its
    ``candidates[0]``; text and url always come from the same candidate).

    An earlier version of this renderer hard-coded the anchor text
    "PubMed" for every pair regardless of source -- correct only while
    PubMed was the sole live source that ever supplied a real URL. Now that
    DailyMed, ClinicalTrials.gov, and Tavily also supply ``hyperlink_url``
    (a live 325-pair run: PubMed supplies only 100 of the 315 linked
    pairs' urls, DailyMed 59, ClinicalTrials.gov 97, Tavily 59), that
    hard-coded text misnamed the other 215 links' actual destination --
    e.g. a real clinicaltrials.gov URL captioned "PubMed"."""
    label = primary_evidence_label or ""
    if "PubMed" in label:
        return "PubMed"
    if "ClinicalTrials.gov" in label:
        return "ClinicalTrials.gov"
    if "DailyMed" in label:
        return "DailyMed"
    if "Tavily" in label or "General web search" in label:
        return "General web search (Tavily)"
    if "Curated DDI" in label:
        return "Flockhart"
    return label or "Source"


def _ddi_flag_sheet_entry_block(entry: dict[str, Any]) -> str:
    """One pair's own Finding/Evidence/Hyperlink block, from the section
    drug's own point of view ("{self} + {partner}"). Evidence lists which
    real source(s) contributed (Flockhart / pharmacodynamic rules / PubMed
    / DailyMed / ClinicalTrials.gov / Tavily -- see ddi_flag_evidence.
    _pair_finding); Hyperlink links the clickable resource NAME (the
    source that actually supplied ``hyperlink_url``, via
    ``entry["primary_evidence_label"]`` and ``_ddi_flag_sheet_hyperlink_
    anchor_text`` -- never a hard-coded "PubMed", and never
    ``evidence_labels[0]`` either: since ddi_flag_evidence.py's
    priority-order fall-through, the source that resolves a pair's Finding/
    url is not always the first one listed in ``evidence_labels`` -- a real,
    observed bug this caused: a link whose href genuinely pointed to a
    ClinicalTrials.gov study, or a general web page, was captioned with a
    higher-priority source's name instead, because that source's own text
    didn't classify and got skipped but still led evidence_labels), never
    the raw URL, per the reference layout -- and is included only when a
    real URL exists ("if there"), never a placeholder link."""
    hyperlink_line = ""
    url = entry.get("hyperlink_url")
    if url:
        anchor_text = _ddi_flag_sheet_hyperlink_anchor_text(entry.get("primary_evidence_label"))
        hyperlink_line = f'<div class="flagpair-line"><b>Hyperlink:</b> <a href="{xml_escape(str(url))}">{xml_escape(anchor_text)}</a></div>'
    evidence_text = "; ".join(entry.get("evidence_labels") or []) or "—"
    return f"""
    <div class="flagpair-block">
      <div class="flagpair-title">{_t(entry["_self"])} + {_t(entry["_partner"])}</div>
      <div class="flagpair-line"><b>Findings:</b> {_t(entry.get("finding"))}</div>
      <div class="flagpair-line"><b>Evidence:</b> {_t(evidence_text)}</div>
      {hyperlink_line}
    </div>"""


def _ddi_flag_sheet_drug_section(drug_display_name: str, entries: list[dict[str, Any]]) -> str:
    """One drug's own 2-column Positive/Negative table -- every pair this
    drug is part of (from the screening panel's surviving, evidence-backed
    pairs only, each already resolved to exactly one of the two outcomes --
    see ddi_flag_evidence._pair_finding) is sorted into exactly one column
    by its own ``impact``; a pair never appears in more than one column for
    the same drug."""
    by_impact: dict[str, list[str]] = {impact: [] for impact in _IMPACT_ORDER}
    for entry in sorted(entries, key=lambda e: str(e.get("_partner") or "")):
        impact = entry.get("impact")
        if impact in by_impact:
            by_impact[impact].append(_ddi_flag_sheet_entry_block(entry))

    no_pairs_note = '<p class="small muted">No pairs currently fall in this category.</p>'
    header_cells = "".join(f"<th>{_t(_IMPACT_COLUMN_LABEL[impact])}</th>" for impact in _IMPACT_ORDER)
    body_cells = "".join(f"<td>{''.join(by_impact[impact]) or no_pairs_note}</td>" for impact in _IMPACT_ORDER)

    return f"""
  <div class="h2" style="margin-top:10px">{_t(drug_display_name.upper())}</div>
  <table class="datatable flagpair-table three-col"><thead><tr>{header_cells}</tr></thead>
    <tbody><tr>{body_cells}</tr></tbody>
  </table>
"""


def _ddi_flag_sheet_known_limitations_block(flag_evidence: dict[str, Any] | None) -> str:
    """Renders ddi_flag_evidence.py's own recorded ``limitations`` list
    verbatim -- real text already generated by that module, never invented
    here -- so the Flag Sheet's own "(see Known Limitations)" reference
    (in its intro paragraph, just above the per-drug tables) points at
    something concrete. Same pattern as _known_limitations_block, which
    does the equivalent job for external_evidence_summary's report -- kept
    as a separate block because these are two different reports' own
    limitations, never merged into one list that would blur which module
    each caveat actually describes."""
    items = _get(flag_evidence or {}, "limitations", default=[])
    if not items:
        return ""
    return (
        '<div class="callout gap" style="margin-top:8px"><b>Known Limitations (Drug Interaction Flag Sheet):</b>'
        f'<ul class="list-compact">{"".join(f"<li>{_t(i)}</li>" for i in items)}</ul></div>'
    )


def _ddi_flag_sheet_card(flag_evidence: dict[str, Any] | None) -> str:
    """Renders the Drug Interaction Flag Sheet's screening panel (see
    patient_prime_agent.ddi_flag_evidence and, upstream,
    path_d.ddi.candidate_drugs) as one section per screened drug, each with
    its own Positive Impact / Negative Impact 2-column table -- matching
    the reference "Extracted Drug List" layout exactly. A drug with zero
    surviving (evidence-backed and resolved) pairs gets no section at all
    -- nothing to show, and an all-empty 2-column table would add noise,
    not information.

    Every pair shown here already passed through ddi_flag_evidence.py's own
    multi-source check (PGx/Flockhart/pharmacodynamic-rule/DailyMed/PubMed/
    ClinicalTrials.gov/Tavily) and its own drop-if-unresolved rule -- a pair
    with no finding, or whose evidence never resolved clearly to Positive
    or Negative, never reaches this function at all, so "not listed under
    this drug" always means "no resolved evidence was found for this
    pair," never "confirmed compatible." This function's only job is to
    lay out whatever ``flag_evidence`` already decided, once per drug, from
    that drug's own point of view (see _ddi_flag_sheet_pairs_by_drug)."""
    pairs = _get(flag_evidence or {}, "pairs", default=[])
    if not pairs:
        return """
  <div class="h2" style="margin-top:10px">Drug Interaction Flag Sheet &mdash; Extracted Drug List</div>
  <div class="callout gap">No live multi-source evidence is available for the drug-interaction screening panel yet.</div>
"""

    by_drug = _ddi_flag_sheet_pairs_by_drug(flag_evidence)
    drug_display_by_normalized: dict[str, str] = {}
    for pair in pairs:
        for name in (pair.get("drug_a"), pair.get("drug_b")):
            if name:
                drug_display_by_normalized.setdefault(str(name).strip().lower(), str(name).strip())

    sections = "".join(
        _ddi_flag_sheet_drug_section(drug_display_by_normalized[normalized], by_drug.get(normalized, []))
        for normalized in sorted(drug_display_by_normalized)
    )

    checked = _get(flag_evidence or {}, "pairs_checked", default=0)
    with_evidence = _get(flag_evidence or {}, "pairs_with_evidence", default=0)
    dropped = _get(flag_evidence or {}, "pairs_dropped_unresolved", default=0)

    return f"""
  <div class="h2" style="margin-top:10px">Drug Interaction Flag Sheet &mdash; Extracted Drug List</div>
  <p class="small muted" style="margin-bottom:2px"><b>Positive Impact</b> = curated/literature evidence explicitly describes a favorable or well-tolerated combination; <b>Negative Impact</b> = a real curated pharmacokinetic mechanism, a pharmacodynamic-risk rule, or literature language explicitly describing a risk. A pair with no evidence, or whose evidence never clearly resolved either way, is not listed here at all -- it is never guessed into either column (see Known Limitations).</p>
  <p class="small muted" style="margin-bottom:2px">Screened {_t(len(drug_display_by_normalized))} drug(s) across {_t(checked)} unique pairs; {_t(with_evidence)} pair(s) resolved clearly to Positive or Negative and are shown below. {_t(dropped)} pair(s) had no evidence, or evidence that did not clearly resolve either way, and are not listed here -- never shown as a negative/no-concern finding (see Known Limitations).</p>
  {sections}
  {_ddi_flag_sheet_known_limitations_block(flag_evidence)}
"""


# ----------------------------------------------------------------------
# 06. Drug Interactions -- this dataset has no DDI screening data; this
# section states that gap explicitly rather than inventing interaction
# findings (see module docstring).
# ----------------------------------------------------------------------
def _sec_drug_interactions(sections: dict[str, Any]) -> str:
    ddi = sections.get(SEC_DDI) or {}
    if not ddi:
        return _sec_drug_interactions_gap(sections)

    flag_evidence = sections.get(SEC_DDI_FLAG_EVIDENCE) or {}
    coverage = _get(ddi, "source_coverage", default={})
    reconciliation = _get(ddi, "medication_reconciliation", default={})
    therapy_assessments = _get(ddi, "therapy_assessments", default=[])
    limitations = _get(ddi, "limitations", default=[])

    sub = (
        f"{len(reconciliation.get('normalized_medications') or [])} current medication(s) reconciled · "
        f"{_raw_text(coverage.get('flockhart_source'))} ({_raw_text(coverage.get('flockhart_version'))})"
    )

    # No "Status" column: this reconciliation table only ever lists current-regimen
    # medications (patient_prime_agent.path_d.ddi.normalizer only ever produces "current" --
    # see that module's docstring), so the value is constant and adds no information here;
    # the section subtitle above already states "N current medication(s) reconciled."
    med_rows = "".join(
        f"<tr><td>{_t(m.get('source_name'))}</td>"
        f"<td>{_t((m.get('dose') or {}).get('value'))} {_t((m.get('dose') or {}).get('unit'))} {_t(m.get('frequency'))}</td>"
        f"<td>{', '.join(m.get('reconciliation_flags') or []) or '&mdash;'}</td></tr>"
        for m in reconciliation.get("normalized_medications") or []
    )
    conflicts = reconciliation.get("conflicts") or []
    conflict_html = (
        "".join(f'<div class="callout warn" style="margin-top:6px">{_t(c.get("details"))}</div>' for c in conflicts)
        if conflicts
        else '<div class="callout" style="margin-top:6px">No conflicting doses found for the same drug across the reconciled source data.</div>'
    )

    # Per-pair detail (status, finding, evidence source) for every known drug pair --
    # both the current-regimen pair and the wider curated panel -- now lives entirely in
    # _ddi_flag_sheet_card below -- one Status/Finding/Evidence-Source table per pair, so
    # this fact is never split across a second "Clinically Relevant Interactions" table
    # (removed) that only listed a subset of it.
    therapy_cards = "".join(_therapy_assessment_card(t) for t in therapy_assessments)

    return f"""
<section class="section">
  {_section_head(6, "Drug Interactions", sub)}
  {_ddi_summary_overview_card(ddi)}
  <div class="card">
    <div class="h2">Medication Reconciliation</div>
    <table class="datatable"><thead><tr><th>Drug</th><th>Dose</th><th>Flags</th></tr></thead>
      <tbody>{med_rows}</tbody></table>
    {conflict_html}
  </div>
  {_ddi_flag_sheet_card(flag_evidence)}
  {_ddi_coverage_action_table(ddi)}
  {_ddi_clinical_inference_callout(ddi)}
  {_ddi_candidate_pair_blocks(ddi)}
  {therapy_cards}
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
  {_section_head(6, "Drug Interactions")}
  <div class="callout gap">
    <b>No drug-drug interaction (DDI) screen is available for this patient.</b>
    The current regimen ({_t(drug_list)}) has not been checked against a drug-interaction database in the
    available data. This is a different check from Pharmacogenomics (Section 07), which looks at how the
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
            "database (no DDI screen is present in the source data; see Section 06).</li>"
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
        _sec_drug_interactions(sections),
        _sec_pharmacogenomics(sections),
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
