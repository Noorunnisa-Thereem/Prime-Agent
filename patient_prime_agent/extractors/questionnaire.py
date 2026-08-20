from __future__ import annotations

from typing import Any

from ..models import Evidence, ValidationIssue
from .base import CategoryExtractorBase
from .common import capture_section, extract_sentences_with_keywords, split_list_items


class QuestionnaireExtractor(CategoryExtractorBase):
    schema_name = "questionnaire"

    def extract_document(self, document) -> tuple[dict[str, Any], list[Evidence], list[ValidationIssue]]:
        evidence: list[Evidence] = []
        issues: list[ValidationIssue] = []
        text = document.text

        symptoms, symptom_lines = capture_section(document, ["symptoms", "presenting symptoms", "complaints", "concerns"])
        if not symptoms:
            symptoms = self._extract_symptoms(text)
        symptom_list = split_list_items(symptoms) if symptoms else []
        if symptom_list:
            evidence.append(self._evidence("reported_symptoms", document.path, symptom_lines, "; ".join(symptom_list)))

        symptom_onset = self._extract_onset(text)
        if symptom_onset:
            evidence.append(self._evidence("symptom_onset", document.path, [], symptom_onset))

        duration = self._extract_duration(text)
        if duration:
            evidence.append(self._evidence("duration", document.path, [], duration))

        severity = self._extract_severity(text)
        if severity:
            evidence.append(self._evidence("severity", document.path, [], severity))

        relevant_history = self._extract_history(text)
        if relevant_history:
            evidence.append(self._evidence("relevant_history", document.path, [], "; ".join(relevant_history)))

        patient_concerns = self._extract_concerns(text)
        if patient_concerns:
            evidence.append(self._evidence("patient_concerns", document.path, [], "; ".join(patient_concerns)))

        section = {
            "reported_symptoms": symptom_list,
            "symptom_onset": symptom_onset,
            "duration": duration,
            "severity": severity,
            "relevant_history": relevant_history,
            "patient_concerns": patient_concerns,
        }
        return section, evidence, issues

    def _extract_symptoms(self, text: str) -> str | None:
        for sentence in extract_sentences_with_keywords(
            text,
            [
                "pain",
                "fever",
                "cough",
                "shortness of breath",
                "dyspnea",
                "headache",
                "fatigue",
                "nausea",
                "vomiting",
                "diarrhea",
                "dizziness",
                "weakness",
                "numbness",
                "rash",
                "seizure",
            ],
        ):
            return sentence
        return None

    def _extract_onset(self, text: str) -> str | None:
        for sentence in extract_sentences_with_keywords(text, ["onset", "began", "started", "since", "following"]):
            if len(sentence) > 10:
                return sentence
        return None

    def _extract_duration(self, text: str) -> str | None:
        for sentence in extract_sentences_with_keywords(text, ["for ", "duration", "weeks", "months", "days", "years"]):
            if len(sentence) > 10:
                return sentence
        return None

    def _extract_severity(self, text: str) -> str | None:
        for sentence in extract_sentences_with_keywords(text, ["mild", "moderate", "severe", "10/10", "pain scale"]):
            if len(sentence) > 5:
                return sentence
        return None

    def _extract_history(self, text: str) -> list[str]:
        findings = extract_sentences_with_keywords(text, ["history", "family history", "social history", "medication", "allergy"])
        return findings[:8]

    def _extract_concerns(self, text: str) -> list[str]:
        concerns = extract_sentences_with_keywords(text, ["concern", "worried", "questions", "asks", "would like"])
        return concerns[:8]

