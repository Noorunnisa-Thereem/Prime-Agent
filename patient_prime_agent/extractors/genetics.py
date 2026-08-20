from __future__ import annotations

import re
from typing import Any

from ..models import Evidence, ValidationIssue
from .base import CategoryExtractorBase
from .common import capture_section, extract_sentences_with_keywords, split_list_items


class GeneticsExtractor(CategoryExtractorBase):
    schema_name = "genetics"

    def extract_document(self, document) -> tuple[dict[str, Any], list[Evidence], list[ValidationIssue]]:
        evidence: list[Evidence] = []
        issues: list[ValidationIssue] = []
        text = document.text

        test_name, test_lines = capture_section(document, ["test name", "test", "assay", "analysis"])
        if test_name:
            evidence.append(self._evidence("test_name", document.path, test_lines, test_name))

        variants = self._extract_variants(text)
        if variants:
            evidence.append(self._evidence("variants", document.path, [], "; ".join(variant["variant"] or "" for variant in variants if variant)))

        overall_interpretation, interpretation_lines = capture_section(
            document,
            ["interpretation", "summary", "result", "conclusion", "overall interpretation"],
        )
        if overall_interpretation:
            evidence.append(self._evidence("overall_interpretation", document.path, interpretation_lines, overall_interpretation))

        recommendations = self._extract_recommendations(text)
        if recommendations:
            evidence.append(self._evidence("recommendations", document.path, [], "; ".join(recommendations)))

        section = {
            "test_name": test_name,
            "variants": variants,
            "overall_interpretation": overall_interpretation,
            "recommendations": recommendations,
        }
        return section, evidence, issues

    def _extract_variants(self, text: str) -> list[dict[str, Any]]:
        variants: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if not any(token in lowered for token in ("pathogenic", "likely pathogenic", "benign", "likely benign", "vus", "variant of uncertain significance", "heterozygous", "homozygous", "compound heterozygous")):
                continue
            gene = self._find_gene_symbol(stripped)
            variant = self._find_variant_notation(stripped)
            zygosity = self._find_zygosity(stripped)
            classification = self._find_classification(stripped)
            inheritance = self._find_inheritance(stripped)
            interpretation = stripped
            variants.append(
                {
                    "gene": gene,
                    "variant": variant,
                    "zygosity": zygosity,
                    "classification": classification,
                    "inheritance": inheritance,
                    "interpretation": interpretation,
                }
            )
        return self._dedupe_variants(variants)

    def _find_gene_symbol(self, text: str) -> str | None:
        match = re.search(r"\b([A-Z0-9]{2,10})\b", text)
        if match:
            return match.group(1)
        return None

    def _find_variant_notation(self, text: str) -> str | None:
        match = re.search(r"\b(c\.[A-Za-z0-9_>+\-]+(?:\([^)]+\))?|p\.[A-Za-z0-9_>+\-]+)\b", text)
        if match:
            return match.group(1)
        return None

    def _find_zygosity(self, text: str) -> str | None:
        lowered = text.lower()
        for term in ("compound heterozygous", "heterozygous", "homozygous", "hemizygous"):
            if term in lowered:
                return term
        return None

    def _find_classification(self, text: str) -> str | None:
        lowered = text.lower()
        for term in ("pathogenic", "likely pathogenic", "benign", "likely benign", "vus", "variant of uncertain significance"):
            if term in lowered:
                return "variant of uncertain significance" if term == "vus" else term
        return None

    def _find_inheritance(self, text: str) -> str | None:
        lowered = text.lower()
        for term in ("autosomal dominant", "autosomal recessive", "x-linked", "mitochondrial"):
            if term in lowered:
                return term
        return None

    def _extract_recommendations(self, text: str) -> list[str]:
        recommendations = extract_sentences_with_keywords(
            text,
            [
                "recommend",
                "genetic counseling",
                "follow-up",
                "family testing",
                "cascade testing",
                "clinical correlation",
                "interpret with caution",
            ],
        )
        return recommendations[:8]

    def _dedupe_variants(self, variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, ...]] = set()
        unique: list[dict[str, Any]] = []
        for variant in variants:
            key = (
                variant.get("gene"),
                variant.get("variant"),
                variant.get("classification"),
                variant.get("zygosity"),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(variant)
        return unique

