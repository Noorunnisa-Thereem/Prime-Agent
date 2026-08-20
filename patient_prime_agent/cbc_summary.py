from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import ensure_dir, normalize_whitespace

DEFAULT_INPUT_DIR = Path("patient_data") / "CBC"
DEFAULT_OUTPUT_PATH = Path("reports") / "cbc" / "CBC_consolidated_summary.json"

REPORT_LABEL = "Merged Prime Agent CBC Longitudinal Summary"

# Values are extracted by exact keyword match against a clearly labeled report row, not by
# a real per-value ML confidence model. This fixed constant names that methodology; it is
# never a computed probability.
EXTRACTION_CONFIDENCE = 0.98

# (report key, exact label as it appears in the source PDF, group)
# group is used for hematological_assessment / clinical_flags grouping.
CBC_PARAMETER_DEFS: list[tuple[str, str, str]] = [
    ("hemoglobin", "Hemoglobin (Hb)", "red_cell"),
    ("rbc_count", "Total RBC count", "red_cell"),
    ("pcv_hematocrit", "Packed Cell Volume (PCV)", "red_cell"),
    ("mcv", "Mean Corpuscular Volume (MCV)", "red_cell"),
    ("mch", "MCH", "red_cell"),
    ("mchc", "MCHC", "red_cell"),
    ("rdw", "RDW", "red_cell"),
    ("total_wbc_count", "Total WBC count", "white_cell"),
    ("neutrophils", "Neutrophils", "white_cell"),
    ("lymphocytes", "Lymphocytes", "white_cell"),
    ("eosinophils", "Eosinophils", "white_cell"),
    ("monocytes", "Monocytes", "white_cell"),
    ("basophils", "Basophils", "white_cell"),
    ("platelet_count", "Platelet Count", "platelet"),
]
PARAMETER_GROUP = {key: group for key, _label, group in CBC_PARAMETER_DEFS}

REFERENCE_RANGE_PATTERN = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*-\s*([\d,]+(?:\.\d+)?)\s*$")
NUMBER_PATTERN = re.compile(r"^\s*-?[\d,]+(?:\.\d+)?\s*$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a consolidated longitudinal CBC summary from CBC report PDFs")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    manifest, reports = load_reports(args.input_dir)
    report = build_report(reports, manifest, args.input_dir)
    ensure_dir(args.output.parent)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote CBC consolidated summary to {args.output}")
    return 0


# ----------------------------------------------------------------------
# PDF parsing
# ----------------------------------------------------------------------
def load_reports(input_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    detected = sorted(input_dir.glob("*.pdf")) if input_dir.exists() else []
    processed: list[Path] = []
    failed: list[Path] = []
    reports: list[dict[str, Any]] = []

    for path in detected:
        try:
            pages = _read_pdf_pages(path)
        except Exception:
            failed.append(path)
            continue
        report = _parse_cbc_pdf(pages)
        if report is None:
            failed.append(path)
            continue
        report["_source_file"] = str(path)
        reports.append(report)
        processed.append(path)

    reports.sort(key=lambda item: item["report_date"] or "")
    manifest = {
        "source_type": "pdf",
        "detected_count": len(detected),
        "processed_count": len(processed),
        "failed_count": len(failed),
        "skipped_count": 0,
        "status": "complete" if not failed and detected else ("partial" if processed else "no_input"),
        "detected_files": [str(path) for path in detected],
        "processed_files": [str(path) for path in processed],
        "failed_files": [str(path) for path in failed],
        "skipped_files": [],
        "confidence": round(len(processed) / len(detected), 2) if detected else 0.0,
    }
    return manifest, reports


def _read_pdf_pages(path: Path) -> list[str]:
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name, fromlist=["PdfReader"])
        except ImportError:
            continue
        reader = module.PdfReader(str(path))
        return [normalize_whitespace(page.extract_text() or "") for page in reader.pages]
    raise RuntimeError("PDF text extraction requires pypdf or PyPDF2")


def _parse_cbc_pdf(pages: list[str]) -> dict[str, Any] | None:
    full_text = "\n".join(pages)
    if not full_text.strip():
        return None

    report_date = _first_date(_regex(full_text, r"Report Date:\s*([0-9]{2}-[A-Za-z]{3}-[0-9]{4})"))
    patient_name = _regex(full_text, r"Patient Name:\s*(.+)")
    patient_id = _regex(full_text, r"Patient ID:\s*(.+)")
    age_sex = re.search(r"Age/Sex:\s*(\d+)\s*/\s*(\w+)", full_text)
    age = int(age_sex.group(1)) if age_sex else None
    sex = age_sex.group(2) if age_sex else None
    sample_type = _regex(full_text, r"Primary Sample Type:\s*(.+)")
    laboratory = next((line.strip() for line in full_text.splitlines() if "laboratory" in line.lower()), None)

    parameters: dict[str, dict[str, Any]] = {}
    for key, label, _group in CBC_PARAMETER_DEFS:
        found = _find_parameter(pages, label)
        if found is not None:
            parameters[key] = {"parameter": label, **found}

    return {
        "report_date": report_date,
        "patient_name": patient_name,
        "patient_id": patient_id,
        "age": age,
        "sex": sex,
        "sample_type": sample_type,
        "laboratory": laboratory,
        "parameters": parameters,
    }


def _find_parameter(pages: list[str], label: str) -> dict[str, Any] | None:
    for page_number, page_text in enumerate(pages, start=1):
        lines = page_text.splitlines()
        for index, line in enumerate(lines[:-2]):
            if line.strip() != label:
                continue
            value_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
            range_line = lines[index + 2].strip() if index + 2 < len(lines) else ""
            unit_line = lines[index + 3].strip() if index + 3 < len(lines) else ""
            if not NUMBER_PATTERN.match(value_line):
                continue
            range_match = REFERENCE_RANGE_PATTERN.match(range_line)
            if not range_match:
                continue
            return {
                "value": _to_number(value_line),
                "unit": unit_line or None,
                "reference_range": range_line,
                "reference_low": _to_number(range_match.group(1)),
                "reference_high": _to_number(range_match.group(2)),
                "source_page": page_number,
            }
    return None


# ----------------------------------------------------------------------
# per-report derivation
# ----------------------------------------------------------------------
def build_report(reports: list[dict[str, Any]], manifest: dict[str, Any], input_dir: Path) -> dict[str, Any]:
    if not reports:
        return _empty_report(manifest, input_dir)

    cbc_reports = [_build_cbc_report_entry(report) for report in reports]
    dates = [entry["report_date"] for entry in cbc_reports if entry["report_date"]]

    first = reports[0]
    patient_profile = {
        "patient_name": first.get("patient_name"),
        "patient_id": first.get("patient_id"),
        "age": first.get("age"),
        "sex": first.get("sex"),
    }

    longitudinal_trends = _build_longitudinal_trends(reports)
    abnormal_findings_over_time = [
        {"date": entry["report_date"], "finding": finding}
        for entry in cbc_reports
        for finding in entry["abnormal_findings"]
    ]
    any_abnormal = any(cbc_reports) and any(entry["abnormal_findings"] for entry in cbc_reports)

    return {
        "report_label": REPORT_LABEL,
        "patient_profile": patient_profile,
        "patient_id": patient_profile["patient_id"],
        "observation_window": {
            "start_date": dates[0] if dates else None,
            "end_date": dates[-1] if dates else None,
            "number_of_reports": len(reports),
            "source_folder": str(input_dir),
        },
        "cbc_reports": cbc_reports,
        "longitudinal_trends": longitudinal_trends,
        "abnormal_findings_over_time": abnormal_findings_over_time,
        "digital_twin_state": {
            "overall_hematological_status": "abnormality_detected" if any_abnormal else "stable_within_reference_ranges",
            "active_flags_by_date": {
                entry["report_date"]: [name for name, active in entry["clinical_flags"].items() if active is True]
                for entry in cbc_reports
            },
            "unsupported_clinical_risk": (
                "Overall patient risk is not determined from CBC values alone; this summary only reports "
                "CBC-specific abnormalities against report-provided reference ranges."
            ),
        },
        "executive_summary": {
            "clinical_conclusion": (
                f"{len(reports)} CBC report(s) from {dates[0]} to {dates[-1]} were processed. "
                + (
                    "No CBC parameter abnormality was identified against the report-provided reference ranges."
                    if not any_abnormal
                    else "One or more CBC parameters fell outside the report-provided reference ranges; see abnormal_findings_over_time."
                )
            ),
            "basis": (
                "Generated from extracted CBC PDF values, units, source pages, and report-provided reference "
                "ranges. No reference report values were copied."
            ),
        },
        "processing_manifest": manifest,
    }


def _build_cbc_report_entry(report: dict[str, Any]) -> dict[str, Any]:
    parameters = report["parameters"]
    cbc_parameters: dict[str, Any] = {}
    abnormal_findings: list[str] = []

    for key, label, _group in CBC_PARAMETER_DEFS:
        found = parameters.get(key)
        if found is None:
            continue
        status = _status_for(found["value"], found["reference_low"], found["reference_high"])
        entry = {
            "parameter": label,
            "value": found["value"],
            "unit": found["unit"],
            "reference_range": found["reference_range"],
            "reference_low": found["reference_low"],
            "reference_high": found["reference_high"],
            "status": status,
            "confidence": EXTRACTION_CONFIDENCE,
            "source_page": found["source_page"],
        }
        cbc_parameters[key] = entry
        if status != "normal":
            abnormal_findings.append(f"{label} is {status} ({found['value']} {found['unit']}, reference {entry['reference_range']})")

    hematological_assessment = _hematological_assessment(cbc_parameters)
    clinical_flags = _clinical_flags(cbc_parameters)

    return {
        "report_date": report["report_date"],
        "report_metadata": {
            "report_date": report["report_date"],
            "sample_type": report.get("sample_type"),
            "laboratory": report.get("laboratory"),
        },
        "cbc_parameters": cbc_parameters,
        "abnormal_findings": abnormal_findings,
        "hematological_assessment": hematological_assessment,
        "overall_cbc_impression": (
            "No CBC parameter abnormality identified based on extracted values and report reference ranges."
            if not abnormal_findings
            else f"Abnormality identified in: {'; '.join(abnormal_findings)}."
        ),
        "clinical_flags": clinical_flags,
        "risk_assessment": {
            "overall_risk": "no_cbc_abnormality_detected" if not abnormal_findings else "cbc_abnormality_detected",
            "basis": (
                "CBC-specific assessment based only on extracted values and report-provided reference ranges; "
                "overall patient risk is not determined from CBC alone."
            ),
            "supporting_parameters": [
                {
                    "parameter": entry["parameter"],
                    "value": entry["value"],
                    "unit": entry["unit"],
                    "reference_range": entry["reference_range"],
                    "status": entry["status"],
                    "source_page": entry["source_page"],
                    "confidence": entry["confidence"],
                }
                for entry in cbc_parameters.values()
            ],
        },
        "digital_twin_health_state": {
            "red_cell_status": hematological_assessment["red_cell_assessment"]["status"],
            "white_cell_status": hematological_assessment["white_cell_assessment"]["status"],
            "platelet_status": hematological_assessment["platelet_assessment"]["status"],
            "overall_hematological_status": (
                "stable_within_reference_ranges" if not abnormal_findings else "abnormality_detected"
            ),
            "active_flags": [name for name, active in clinical_flags.items() if active is True],
        },
    }


def _hematological_assessment(cbc_parameters: dict[str, Any]) -> dict[str, Any]:
    groups = {"red_cell": [], "white_cell": [], "platelet": []}
    for key, entry in cbc_parameters.items():
        groups[PARAMETER_GROUP[key]].append(entry)

    def group_assessment(entries: list[dict[str, Any]]) -> dict[str, Any]:
        abnormal = [entry["parameter"] for entry in entries if entry["status"] != "normal"]
        return {
            "status": "within_reference_ranges" if not abnormal else "abnormal",
            "abnormal_parameters": abnormal,
            "basis": (
                "All extracted lineage parameters are within report-provided reference ranges."
                if not abnormal
                else f"The following parameters fall outside the report-provided reference ranges: {', '.join(abnormal)}."
            ),
        }

    red_cell = group_assessment(groups["red_cell"])
    white_cell = group_assessment(groups["white_cell"])
    platelet = group_assessment(groups["platelet"])
    all_abnormal = red_cell["abnormal_parameters"] + white_cell["abnormal_parameters"] + platelet["abnormal_parameters"]

    return {
        "overall_status": "within_reference_ranges" if not all_abnormal else "abnormal",
        "red_cell_assessment": red_cell,
        "white_cell_assessment": white_cell,
        "platelet_assessment": platelet,
        # Grading true clinical severity (mild/moderate/severe) requires domain thresholds this
        # pipeline does not have; only a binary none/abnormal distinction is derived here.
        "severity": "none" if not all_abnormal else "abnormal",
        "summary": (
            "Red cell, white cell, and platelet parameters are within the report-provided reference ranges."
            if not all_abnormal
            else f"Parameters outside reference range: {', '.join(all_abnormal)}."
        ),
    }


def _clinical_flags(cbc_parameters: dict[str, Any]) -> dict[str, bool | None]:
    hemoglobin = cbc_parameters.get("hemoglobin")
    wbc = cbc_parameters.get("total_wbc_count")
    platelets = cbc_parameters.get("platelet_count")

    return {
        "anemia": _below_low(hemoglobin),
        "polycythemia": _above_high(hemoglobin),
        "leukocytosis": _above_high(wbc),
        "leukopenia": _below_low(wbc),
        # Neutropenia requires an absolute neutrophil count against an external clinical
        # threshold (e.g. ANC < 1500/uL); a neutrophil PERCENTAGE alone cannot determine it,
        # and no such threshold appears in the source report, so this always stays null.
        "neutropenia": None,
        "thrombocytopenia": _below_low(platelets),
        "thrombocytosis": _above_high(platelets),
    }


def _below_low(entry: dict[str, Any] | None) -> bool | None:
    if entry is None:
        return None
    return entry["value"] < entry["reference_low"]


def _above_high(entry: dict[str, Any] | None) -> bool | None:
    if entry is None:
        return None
    return entry["value"] > entry["reference_high"]


def _status_for(value: float, low: float, high: float) -> str:
    if value < low:
        return "low"
    if value > high:
        return "high"
    return "normal"


# ----------------------------------------------------------------------
# longitudinal aggregation
# ----------------------------------------------------------------------
def _build_longitudinal_trends(reports: list[dict[str, Any]]) -> dict[str, Any]:
    trends: dict[str, Any] = {}
    for key, label, _group in CBC_PARAMETER_DEFS:
        values: list[dict[str, Any]] = []
        for report in reports:
            found = report["parameters"].get(key)
            if found is None:
                continue
            status = _status_for(found["value"], found["reference_low"], found["reference_high"])
            values.append(
                {
                    "date": report["report_date"],
                    "value": found["value"],
                    "unit": found["unit"],
                    "reference_range": found["reference_range"],
                    "status": status,
                    "confidence": EXTRACTION_CONFIDENCE,
                    "source_page": found["source_page"],
                }
            )
        if not values:
            continue
        numeric_values = [item["value"] for item in values]
        trends[key] = {
            "parameter": label,
            "values": values,
            "first_value": numeric_values[0],
            "latest_value": numeric_values[-1],
            "minimum_value": min(numeric_values),
            "maximum_value": max(numeric_values),
            "trend_direction": _trend_direction(numeric_values),
            "abnormal_dates": [item["date"] for item in values if item["status"] != "normal"],
        }
    return trends


def _trend_direction(values: list[float]) -> str:
    if len(values) < 2:
        return "insufficient_data"
    first, last = values[0], values[-1]
    if last == first:
        return "stable"
    return "increasing" if last > first else "decreasing"


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _regex(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None


def _first_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return value


def _to_number(text: str) -> float:
    return float(text.replace(",", ""))


def _empty_report(manifest: dict[str, Any], input_dir: Path) -> dict[str, Any]:
    return {
        "report_label": REPORT_LABEL,
        "patient_profile": {"patient_name": None, "patient_id": None, "age": None, "sex": None},
        "patient_id": None,
        "observation_window": {
            "start_date": None,
            "end_date": None,
            "number_of_reports": 0,
            "source_folder": str(input_dir),
        },
        "cbc_reports": [],
        "longitudinal_trends": {},
        "abnormal_findings_over_time": [],
        "digital_twin_state": {
            "overall_hematological_status": None,
            "active_flags_by_date": {},
            "unsupported_clinical_risk": (
                "Overall patient risk is not determined from CBC values alone; this summary only reports "
                "CBC-specific abnormalities against report-provided reference ranges."
            ),
        },
        "executive_summary": {"clinical_conclusion": "No CBC reports were available to process.", "basis": None},
        "processing_manifest": manifest,
    }


if __name__ == "__main__":
    raise SystemExit(main())
