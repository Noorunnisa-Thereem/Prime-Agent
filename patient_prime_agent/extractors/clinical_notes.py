from __future__ import annotations

from typing import Any

from ..models import Evidence, ValidationIssue
from .base import CategoryExtractorBase
from .common import capture_section, extract_sentences_with_keywords, split_list_items, textify


class ClinicalNotesExtractor(CategoryExtractorBase):
    schema_name = "clinical_notes"

    def extract_document(self, document) -> tuple[dict[str, Any], list[Evidence], list[ValidationIssue]]:
        evidence: list[Evidence] = []
        issues: list[ValidationIssue] = []
        text = document.text

        chief_complaint, chief_lines = capture_section(document, ["chief complaint", "cc"])
        if not chief_complaint:
            chief_complaint = self._infer_chief_complaint(text)
        if chief_complaint:
            evidence.append(self._evidence("chief_complaint", document.path, chief_lines, chief_complaint))

        hpi, hpi_lines = capture_section(document, ["history of present illness", "hpi"])
        if not hpi:
            hpi = self._infer_hpi(text)
        if hpi:
            evidence.append(self._evidence("history_of_present_illness", document.path, hpi_lines, hpi))

        diagnoses = self._merge_lists(
            capture_section(document, ["assessment", "assessment and plan", "diagnoses", "diagnosis", "problem list"])[0],
            extract_sentences_with_keywords(text, ["diagnosis", "assessment", "impression", "problem list"]),
        )
        if diagnoses:
            evidence.append(self._evidence("diagnoses", document.path, [], "; ".join(diagnoses)))

        medications = self._merge_lists(
            capture_section(document, ["medications", "meds", "home medications", "current medications"])[0],
            extract_sentences_with_keywords(text, ["medication", "meds", "prescribed", "takes"]),
        )
        medications = self._normalize_allergy_medication_list(medications)
        if medications:
            evidence.append(self._evidence("medications", document.path, [], "; ".join(medications)))

        allergies = self._merge_lists(
            capture_section(document, ["allergies"])[0],
            extract_sentences_with_keywords(text, ["allergy", "allergic", "nkda", "no known drug allergies"]),
        )
        allergies = self._normalize_allergy_medication_list(allergies)
        if allergies:
            evidence.append(self._evidence("allergies", document.path, [], "; ".join(allergies)))

        plan, plan_lines = capture_section(document, ["plan", "assessment and plan"])
        if plan:
            evidence.append(self._evidence("plan", document.path, plan_lines, plan))

        notable_findings = self._extract_notable_findings(text)
        if notable_findings:
            evidence.append(self._evidence("notable_findings", document.path, [], "; ".join(notable_findings)))

        section = {
            "chief_complaint": chief_complaint,
            "history_of_present_illness": hpi,
            "diagnoses": diagnoses,
            "medications": medications,
            "allergies": allergies,
            "plan": plan,
            "notable_findings": notable_findings,
        }
        return section, evidence, issues

    def _infer_chief_complaint(self, text: str) -> str | None:
        for sentence in extract_sentences_with_keywords(
            text,
            ["presents with", "complains of", "chief complaint", "c/o", "reports", "presenting"],
        ):
            if len(sentence) > 10:
                return sentence
        return None

    def _infer_hpi(self, text: str) -> str | None:
        for sentence in extract_sentences_with_keywords(text, ["history", "onset", "course", "duration", "worsen", "improve"]):
            if len(sentence) > 10:
                return sentence
        return None

    def _extract_notable_findings(self, text: str) -> list[str]:
        findings = extract_sentences_with_keywords(
            text,
            [
                "fever",
                "pain",
                "shortness of breath",
                "dyspnea",
                "chest pain",
                "weakness",
                "numbness",
                "seizure",
                "bleeding",
                "infection",
                "mass",
                "fracture",
                "edema",
                "stroke",
                "headache",
                "nausea",
                "vomiting",
            ],
        )
        return findings[:8]

    def _merge_lists(self, *values: str | list[str] | None) -> list[str]:
        items: list[str] = []
        for value in values:
            if value is None:
                continue
            if isinstance(value, str):
                items.extend(split_list_items(value))
            else:
                items.extend(value)
        return self._normalize_allergy_medication_list(items)

    def _normalize_allergy_medication_list(self, items: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in items:
            candidate = item.strip()
            if not candidate:
                continue
            if candidate.lower() in {"nkda", "no known drug allergies"}:
                candidate = "No known drug allergies"
            normalized.append(candidate)
        return list(dict.fromkeys(normalized))

