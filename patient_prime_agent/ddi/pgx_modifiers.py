"""Pharmacogenomic modifier engine.

Reads the patient's *actual* genetics summary (``reports/genetics/
genetics_clinical_summary.json``) and surfaces only the findings that name a
drug already in the current regimen. This mirrors the filter
``report_html.py`` already applies for its Pharmacogenomics section, but
derives the current-regimen name list dynamically from the normalized
regimen instead of a hard-coded tuple, so it stays correct if the regimen
changes.

Per the integration plan's safeguard: an altered CYP/PGx phenotype is
reported as *evidence about the therapy* (supporting or counter), never used
here to invent a drug-drug interaction on its own -- that would require an
actual second drug sharing the same pathway, which belongs to
:mod:`.reference_data`.
"""

from __future__ import annotations

from typing import Any

_TOXICITY_TERMS = ("toxicity", "adverse")
_REDUCED_TERMS = ("reduced",)
_FAVORABLE_TERMS = ("efficacy",)


def _direction_for_predicted_effect(predicted_effect: str) -> str:
    text = predicted_effect.lower()
    if any(term in text for term in _TOXICITY_TERMS):
        return "counter"
    if any(term in text for term in _REDUCED_TERMS):
        return "counter"
    if any(term in text for term in _FAVORABLE_TERMS):
        return "supporting"
    return "unresolved"


def collect_pgx_evidence(
    genetics: dict[str, Any],
    current_regimen_names: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Return PGx evidence items grouped by the normalized drug name they apply to."""

    evidence_by_drug: dict[str, list[dict[str, Any]]] = {name: [] for name in current_regimen_names}
    if not isinstance(genetics, dict):
        return evidence_by_drug

    findings_by_class = genetics.get("findings_by_therapeutic_class")
    if isinstance(findings_by_class, dict):
        for entries in findings_by_class.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                drug = str(entry.get("drug") or "")
                matched = _match_regimen_drug(drug, current_regimen_names)
                if matched is None:
                    continue
                predicted_effect = str(entry.get("predicted_effect") or "")
                genetic_basis = str(entry.get("genetic_basis") or "")
                significance = str(entry.get("significance") or "")
                evidence_by_drug[matched].append(
                    {
                        "modality": "pharmacogenomic",
                        "direction": _direction_for_predicted_effect(predicted_effect),
                        "statement": (
                            f"{genetic_basis}: {predicted_effect}".strip(": ")
                            + (f" ({significance})" if significance else "")
                        ),
                        "source_reference": "genetics_clinical_summary.json:findings_by_therapeutic_class",
                        "evidence_level": "high" if significance.lower() == "significant" else "moderate",
                        "patient_specific": True,
                    }
                )

    safety_flags = genetics.get("priority_safety_flags")
    if isinstance(safety_flags, list):
        for flag in safety_flags:
            if not isinstance(flag, dict):
                continue
            risk_text = str(flag.get("risk") or "")
            matched = _match_regimen_drug(risk_text, current_regimen_names)
            if matched is None:
                continue
            evidence_by_drug[matched].append(
                {
                    "modality": "pharmacogenomic",
                    "direction": "counter",
                    "statement": f"{flag.get('flag', '')}: {risk_text}".strip(": "),
                    "source_reference": "genetics_clinical_summary.json:priority_safety_flags",
                    "evidence_level": "high" if str(flag.get("severity", "")).lower() == "high" else "moderate",
                    "patient_specific": True,
                }
            )

    return evidence_by_drug


def _match_regimen_drug(text: str, current_regimen_names: set[str]) -> str | None:
    lowered = text.lower()
    for name in current_regimen_names:
        if name and name in lowered:
            return name
    return None
