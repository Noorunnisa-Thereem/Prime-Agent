from __future__ import annotations

from typing import Any

from ..models import Evidence, ValidationIssue
from .base import CategoryExtractorBase
from .common import find_metric_in_document, find_date_string, split_list_items


CBC_METRICS = {
    "hemoglobin_g_per_dL": ["hemoglobin", "hgb", "hb"],
    "hematocrit_pct": ["hematocrit", "hct"],
    "rbc_million_uL": ["rbc", "red blood cells", "erythrocytes"],
    "wbc_10e3_uL": ["wbc", "white blood cells", "leukocytes"],
    "platelets_10e3_uL": ["platelets", "plt", "platelet count"],
    "mcv_fL": ["mcv"],
    "mch_pg": ["mch"],
    "mchc_g_dL": ["mchc"],
    "rdw_pct": ["rdw"],
}

CBC_DIFFERENTIAL = {
    "neutrophils_pct": ["neutrophils", "neutrophil"],
    "lymphocytes_pct": ["lymphocytes", "lymphocyte"],
    "monocytes_pct": ["monocytes", "monocyte"],
    "eosinophils_pct": ["eosinophils", "eosinophil"],
    "basophils_pct": ["basophils", "basophil"],
}


class CBCExtractor(CategoryExtractorBase):
    schema_name = "cbc"

    def extract_document(self, document) -> tuple[dict[str, Any], list[Evidence], list[ValidationIssue]]:
        evidence: list[Evidence] = []
        issues: list[ValidationIssue] = []
        text = document.text

        collection_date = find_date_string(text)
        if collection_date:
            evidence.append(self._evidence("collection_date", document.path, [], collection_date))

        section: dict[str, Any] = {
            "collection_date": collection_date,
            "abnormal_flags": [],
            "differential": {
                "neutrophils_pct": None,
                "lymphocytes_pct": None,
                "monocytes_pct": None,
                "eosinophils_pct": None,
                "basophils_pct": None,
            },
        }

        for field, aliases in CBC_METRICS.items():
            value, found_evidence = find_metric_in_document(document, aliases)
            section[field] = value
            if found_evidence is not None:
                found_evidence.field = field
                found_evidence.section = self.category
                evidence.append(found_evidence)

        abnormal_flags = self._extract_abnormal_flags(text)
        section["abnormal_flags"] = abnormal_flags
        if abnormal_flags:
            evidence.append(self._evidence("abnormal_flags", document.path, [], "; ".join(abnormal_flags)))

        for field, aliases in CBC_DIFFERENTIAL.items():
            value, found_evidence = find_metric_in_document(document, aliases)
            section["differential"][field] = value
            if found_evidence is not None:
                found_evidence.field = f"differential.{field}"
                found_evidence.section = self.category
                evidence.append(found_evidence)

        return section, evidence, issues

    def _extract_abnormal_flags(self, text: str) -> list[str]:
        flags: list[str] = []
        for sentence in split_list_items(text):
            lowered = sentence.lower()
            if any(token in lowered for token in (" high", " low", " elevated", " decreased", " abnormal", " critical", " flagged")):
                flags.append(sentence)
        return list(dict.fromkeys(flags))[:10]

