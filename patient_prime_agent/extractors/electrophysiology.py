from __future__ import annotations

import re
from typing import Any

from ..models import Evidence, ValidationIssue
from .base import CategoryExtractorBase
from .common import capture_section, extract_sentences_with_keywords, find_date_string, split_list_items


class ECGExtractorBase(CategoryExtractorBase):
    rhythm_terms = (
        "sinus rhythm",
        "normal sinus rhythm",
        "atrial fibrillation",
        "atrial flutter",
        "tachycardia",
        "bradycardia",
        "paced rhythm",
        "junctional rhythm",
        "ventricular tachycardia",
    )

    def extract_document(self, document) -> tuple[dict[str, Any], list[Evidence], list[ValidationIssue]]:
        evidence: list[Evidence] = []
        issues: list[ValidationIssue] = []
        text = document.text

        study_date = find_date_string(text)
        if study_date:
            evidence.append(self._evidence("study_date", document.path, [], study_date))

        rate_bpm = self._extract_rate(text)
        if rate_bpm is not None:
            evidence.append(self._evidence("rate_bpm", document.path, [], str(rate_bpm)))

        rhythm = self._detect_rhythm(text)
        if rhythm:
            evidence.append(self._evidence("rhythm", document.path, [], rhythm))

        intervals = {
            "pr_ms": self._extract_interval(text, ["pr interval", "pr"], "ms"),
            "qrs_ms": self._extract_interval(text, ["qrs duration", "qrs"], "ms"),
            "qt_ms": self._extract_interval(text, ["qt interval", "qt"], "ms"),
            "qtc_ms": self._extract_interval(text, ["qtc", "qtc interval"], "ms"),
        }
        for field, value in intervals.items():
            if value is not None:
                evidence.append(self._evidence(f"intervals.{field}", document.path, [], str(value)))

        axis = self._detect_axis(text)
        if axis:
            evidence.append(self._evidence("axis", document.path, [], axis))

        impression, impression_lines = capture_section(document, ["impression", "interpretation", "conclusion"])
        if impression:
            evidence.append(self._evidence("impression", document.path, impression_lines, impression))

        notable_abnormalities = self._extract_abnormalities(text, impression or "")
        if notable_abnormalities:
            evidence.append(self._evidence("notable_abnormalities", document.path, [], "; ".join(notable_abnormalities)))

        section = {
            "study_date": study_date,
            "rate_bpm": rate_bpm,
            "rhythm": rhythm,
            "intervals": intervals,
            "axis": axis,
            "impression": impression,
            "notable_abnormalities": notable_abnormalities,
        }
        return section, evidence, issues

    def _extract_rate(self, text: str) -> int | None:
        match = re.search(r"(?i)(?:ventricular\s+)?rate\s*[:=]?\s*(\d{2,3})\s*bpm?", text)
        if match:
            return int(match.group(1))
        match = re.search(r"(?i)\b(\d{2,3})\s*bpm\b", text)
        if match:
            return int(match.group(1))
        return None

    def _detect_rhythm(self, text: str) -> str | None:
        lowered = text.lower()
        for term in self.rhythm_terms:
            if term in lowered:
                return term
        return None

    def _extract_interval(self, text: str, aliases: list[str], unit: str) -> int | None:
        alias_pattern = "|".join(re.escape(alias) for alias in aliases)
        match = re.search(rf"(?i)\b(?:{alias_pattern})\b\s*[:=]?\s*(\d{{2,4}})\s*{re.escape(unit)}\b", text)
        if match:
            return int(match.group(1))
        match = re.search(rf"(?i)\b(?:{alias_pattern})\b\s*[:=]?\s*(\d{{2,4}})", text)
        if match:
            return int(match.group(1))
        return None

    def _detect_axis(self, text: str) -> str | None:
        lowered = text.lower()
        for term in (
            "normal axis",
            "left axis deviation",
            "right axis deviation",
            "extreme axis deviation",
            "axis normal",
            "axis left",
            "axis right",
        ):
            if term in lowered:
                return term
        return None

    def _extract_abnormalities(self, text: str, impression: str) -> list[str]:
        findings = extract_sentences_with_keywords(
            f"{text}\n{impression}",
            [
                "st depression",
                "st elevation",
                "t wave inversion",
                "bundle branch block",
                "left ventricular hypertrophy",
                "right ventricular hypertrophy",
                "q waves",
                "arrhythmia",
                "prolonged qt",
                "ischemia",
                "infarct",
                "pacemaker",
            ],
        )
        return findings[:8]


class EEGExtractorBase(CategoryExtractorBase):
    def extract_document(self, document) -> tuple[dict[str, Any], list[Evidence], list[ValidationIssue]]:
        evidence: list[Evidence] = []
        issues: list[ValidationIssue] = []
        text = document.text

        study_date = find_date_string(text)
        if study_date:
            evidence.append(self._evidence("study_date", document.path, [], study_date))

        background, background_lines = capture_section(document, ["background", "background activity", "background rhythm"])
        if background:
            evidence.append(self._evidence("background", document.path, background_lines, background))

        epileptiform_activity = self._detect_epileptiform_activity(text)
        if epileptiform_activity:
            evidence.append(self._evidence("epileptiform_activity", document.path, [], epileptiform_activity))

        events = self._extract_events(text)
        if events:
            evidence.append(self._evidence("events", document.path, [], "; ".join(events)))

        impression, impression_lines = capture_section(document, ["impression", "interpretation", "conclusion"])
        if impression:
            evidence.append(self._evidence("impression", document.path, impression_lines, impression))

        notable_abnormalities = self._extract_abnormalities(text, impression or "")
        if notable_abnormalities:
            evidence.append(self._evidence("notable_abnormalities", document.path, [], "; ".join(notable_abnormalities)))

        section = {
            "study_date": study_date,
            "background": background,
            "epileptiform_activity": epileptiform_activity,
            "events": events,
            "impression": impression,
            "notable_abnormalities": notable_abnormalities,
        }
        return section, evidence, issues

    def _detect_epileptiform_activity(self, text: str) -> str | None:
        lowered = text.lower()
        for term in (
            "epileptiform",
            "spike-wave",
            "sharp waves",
            "spikes",
            "seizure",
            "ictal",
            "interictal",
            "slowing",
            "focal slowing",
        ):
            if term in lowered:
                return term
        return None

    def _extract_events(self, text: str) -> list[str]:
        events = extract_sentences_with_keywords(text, ["event", "seizure", "episode", "captured", "clinical correlate"])
        return events[:8]

    def _extract_abnormalities(self, text: str, impression: str) -> list[str]:
        findings = extract_sentences_with_keywords(
            f"{text}\n{impression}",
            ["abnormal", "seizure", "spike", "sharp", "slowing", "epileptiform", "focal", "diffuse"],
        )
        return findings[:8]


class ECGExtractor(ECGExtractorBase):
    schema_name = "ecg"


class EEGExtractor(EEGExtractorBase):
    schema_name = "eeg"

