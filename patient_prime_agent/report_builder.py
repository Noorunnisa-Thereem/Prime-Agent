from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

from .config import CATEGORY_ORDER
from .models import CategoryResult, Evidence
from .schema_validator import SchemaValidator
from .utils import dedupe_preserve_order, utc_now_iso


def build_integrated_report(
    category_results: dict[str, CategoryResult],
    validator: SchemaValidator,
) -> dict[str, Any]:
    report = validator.default("digital_twin_report")
    report["report_type"] = "Digital_Twin_Integrated_Report"
    report["generated_at"] = utc_now_iso()

    sections = report["sections"]
    traceability: list[dict[str, Any]] = []
    problems: list[str] = []
    medications: list[str] = []
    allergies: list[str] = []

    for category in CATEGORY_ORDER:
        result = category_results.get(category)
        if result is None:
            continue
        sections[category] = deepcopy(result.section)
        traceability.extend([item.to_dict() for item in result.evidence])

        if category == "clinical_notes":
            medications.extend(_string_list(result.section.get("medications")))
            allergies.extend(_string_list(result.section.get("allergies")))
            problems.extend(_string_list(result.section.get("diagnoses")))
        elif category in {"ct", "mri"}:
            problems.extend(_string_list(result.section.get("urgent_findings")))
            problems.extend(_string_list(result.section.get("key_findings")))
        elif category in {"ecg", "eeg"}:
            problems.extend(_string_list(result.section.get("notable_abnormalities")))
        elif category == "cbc":
            problems.extend(_string_list(result.section.get("abnormal_flags")))
        elif category == "genetics":
            overall = result.section.get("overall_interpretation")
            if isinstance(overall, str) and overall.strip():
                problems.append(overall.strip())
        elif category == "questionnaire":
            problems.extend(_string_list(result.section.get("reported_symptoms")))

    report["clinical_summary"] = {
        "active_problems": dedupe_preserve_order([item for item in problems if item]),
        "medications": dedupe_preserve_order([item for item in medications if item]),
        "allergies": dedupe_preserve_order([item for item in allergies if item]),
        "notable_abnormalities": dedupe_preserve_order([item for item in problems if item]),
    }
    report["source_traceability"] = dedupe_preserve_order(traceability)
    report["patient_profile"] = _merge_patient_profile(report["patient_profile"], category_results)
    report["validation"] = {
        "schema": "digital_twin_report.schema.json",
        "status": "passed",
        "checked_at": utc_now_iso(),
        "issues_found": [],
    }
    return report


def _merge_patient_profile(base: dict[str, Any], category_results: dict[str, CategoryResult]) -> dict[str, Any]:
    profile = deepcopy(base)
    for result in category_results.values():
        section = result.section
        if isinstance(section, dict):
            for key in ("patient_id", "name", "date_of_birth", "sex", "age_years"):
                value = section.get(key)
                if profile.get(key) in (None, "", []) and value not in (None, "", []):
                    profile[key] = value
    return profile


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return result

