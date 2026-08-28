from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import ensure_dir

REPORT_VERSION = "1.0"

# (category, standalone report path relative to project root, section key used in this
# consolidated report). Each section is the corresponding standalone report's own JSON,
# embedded verbatim -- this module never re-derives or reshapes a value, it only merges
# already-generated, already-verified reports.
SOURCE_REPORTS: tuple[tuple[str, str, str], ...] = (
    ("clinical_notes", "reports/clinical_notes/clinical__notes_summary.json", "clinical__notes_summary"),
    ("cbc", "reports/cbc/CBC_consolidated_summary.json", "CBC_consolidated_summary"),
    ("ct", "reports/ct_scan/CT_scan_clinical_summary.json", "CT_scan_clinical_summary"),
    ("mri", "reports/mri/MRI_clinical_summary.json", "MRI_clinical_summary"),
    ("ecg", "reports/ecg/ECG_Clinical_Summary.json", "ECG_Clinical_Summary"),
    ("eeg", "reports/eeg/EEG_clinical_summary.json", "EEG_clinical_summary"),
    ("questionnaire", "reports/questionnaire/Questionnaire_consolidated_summary.json", "Questionnaire_consolidated_summary"),
    ("genetics", "reports/genetics/genetics_clinical_summary.json", "genetics_clinical_summary"),
    ("ddi", "reports/ddi/DDI_Clinical_Assessment.json", "DDI_Clinical_Assessment"),
)

DEFAULT_OUTPUT_PATH = Path("reports") / "Digital_Twin_Consolidated_Report.json"

# Where to look for a patient identifier in each section, tried in this order until one
# yields a value. Every path is a dotted lookup into that section's own JSON.
PATIENT_ID_LOOKUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("clinical__notes_summary", ("patient_profile", "patient_id")),
    ("CBC_consolidated_summary", ("patient_profile", "patient_id")),
    ("CBC_consolidated_summary", ("patient_id",)),
    ("Questionnaire_consolidated_summary", ("patient_profile", "patient_id")),
    ("EEG_clinical_summary", ("report_metadata", "patient_id")),
    ("MRI_clinical_summary", ("patient_id",)),
    ("CT_scan_clinical_summary", ("patientId",)),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge every category's standalone summary report into one consolidated Digital Twin report")
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    report = build_report(args.project_root)
    ensure_dir(args.output.parent)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote consolidated Digital Twin report to {args.output}")

    return 0


def build_report(project_root: Path) -> dict[str, Any]:
    sections: dict[str, Any] = {}
    included: list[str] = []
    missing: list[str] = []

    for category, relative_path, section_key in SOURCE_REPORTS:
        path = project_root / relative_path
        content = _load_json(path)
        if content is None:
            missing.append(category)
            continue
        sections[section_key] = content
        included.append(category)

    return {
        "digital_twin_report": {
            "version": REPORT_VERSION,
            "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "patient_id": _resolve_patient_id(sections),
            "sections": sections,
        },
        "source_manifest": {
            "sections_included": included,
            "sections_missing": missing,
            "sources": {
                section_key: str(project_root / relative_path)
                for category, relative_path, section_key in SOURCE_REPORTS
                if category in included
            },
        },
    }


def _load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _resolve_patient_id(sections: dict[str, Any]) -> str | None:
    for section_key, lookup in PATIENT_ID_LOOKUPS:
        section = sections.get(section_key)
        if not isinstance(section, dict):
            continue
        value: Any = section
        for key in lookup:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


if __name__ == "__main__":
    raise SystemExit(main())
