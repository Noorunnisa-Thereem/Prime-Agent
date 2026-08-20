from __future__ import annotations

from typing import Any

from ..models import Evidence, ValidationIssue
from .base import CategoryExtractorBase
from .common import capture_section, extract_sentences_with_keywords, find_date_string, maybe_label_from_filename, split_list_items


IMAGING_URGENCY_KEYWORDS = (
    "hemorrhage",
    "bleed",
    "stroke",
    "infarct",
    "mass",
    "fracture",
    "dissection",
    "embolism",
    "pulmonary embolism",
    "edema",
    "obstruction",
    "perforation",
    "pneumothorax",
    "abscess",
    "hydrocephalus",
    "lesion",
    "tumor",
)


class ImagingExtractorBase(CategoryExtractorBase):
    modality_keywords: tuple[str, ...] = ()

    def extract_document(self, document) -> tuple[dict[str, Any], list[Evidence], list[ValidationIssue]]:
        evidence: list[Evidence] = []
        issues: list[ValidationIssue] = []
        text = document.text

        study_date = find_date_string(text)
        if study_date:
            evidence.append(self._evidence("study_date", document.path, [], study_date))

        body_region = self._detect_body_region(text, document)
        if body_region:
            evidence.append(self._evidence("body_region", document.path, [], body_region))

        indication, indication_lines = capture_section(document, ["indication", "reason for exam", "history", "clinical history"])
        if indication:
            evidence.append(self._evidence("indication", document.path, indication_lines, indication))

        contrast = self._detect_contrast(text)
        if contrast:
            evidence.append(self._evidence("contrast", document.path, [], contrast))

        findings, findings_lines = capture_section(document, ["findings", "results"])
        impression, impression_lines = capture_section(document, ["impression", "conclusion", "interpretation"])
        if not impression:
            impression = findings
            impression_lines = findings_lines
        if impression:
            evidence.append(self._evidence("impression", document.path, impression_lines, impression))

        key_findings = split_list_items(findings or impression or "")
        if not key_findings and impression:
            key_findings = extract_sentences_with_keywords(impression, ["normal", "abnormal", "lesion", "mass", "fracture", "edema", "hemorrhage"])
        key_findings = list(dict.fromkeys(key_findings))
        if key_findings:
            evidence.append(self._evidence("key_findings", document.path, [], "; ".join(key_findings)))

        urgent_findings = [item for item in key_findings if any(keyword in item.lower() for keyword in IMAGING_URGENCY_KEYWORDS)]
        if urgent_findings:
            evidence.append(self._evidence("urgent_findings", document.path, [], "; ".join(urgent_findings)))

        section = {
            "study_date": study_date,
            "body_region": body_region,
            "indication": indication,
            "contrast": contrast,
            "impression": impression,
            "key_findings": key_findings,
            "urgent_findings": urgent_findings,
        }
        return section, evidence, issues

    def _detect_body_region(self, text: str, document) -> str | None:
        filename_hint = maybe_label_from_filename(
            document,
            [
                "brain",
                "head",
                "chest",
                "thorax",
                "abdomen",
                "pelvis",
                "abdomen pelvis",
                "spine",
                "cervical spine",
                "thoracic spine",
                "lumbar spine",
                "knee",
                "shoulder",
                "hip",
                "ankle",
                "wrist",
                "hand",
                "sinus",
                "face",
            ],
        )
        if filename_hint:
            return filename_hint
        lowered = text.lower()
        for keyword in (
            "brain",
            "head",
            "chest",
            "thorax",
            "abdomen",
            "pelvis",
            "spine",
            "cervical spine",
            "thoracic spine",
            "lumbar spine",
            "knee",
            "shoulder",
            "hip",
            "ankle",
            "wrist",
            "hand",
            "sinus",
            "face",
        ):
            if keyword in lowered:
                return keyword
        return None

    def _detect_contrast(self, text: str) -> str | None:
        lowered = text.lower()
        if "without and with contrast" in lowered:
            return "with and without contrast"
        if "with contrast" in lowered or "contrast enhanced" in lowered:
            return "with contrast"
        if "without contrast" in lowered or "noncontrast" in lowered or "non-contrast" in lowered:
            return "without contrast"
        return None


class CTExtractor(ImagingExtractorBase):
    schema_name = "ct"


class MRIExtractor(ImagingExtractorBase):
    schema_name = "mri"

