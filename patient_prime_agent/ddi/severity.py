"""Deterministic severity scoring for a drug pair.

Combines multiple factors into one score, then buckets the score into a
clinical category. The score is kept internally for traceability; the report
only ever shows the category and its rationale, per the plan's instruction
not to expose a raw number as a clinical conclusion.
"""

from __future__ import annotations

from typing import Any

_SEVERITY_WEIGHT = {"minor": 1, "moderate": 2, "major": 3, "critical": 4}


def score_pair(
    pk_mechanism_found: bool,
    pd_rules_fired: list[dict[str, Any]],
    pgx_amplification_present: bool,
    clinical_vulnerability_present: bool,
) -> dict[str, Any]:
    score = 0
    rationale: list[str] = []

    if pk_mechanism_found:
        score += 2
        rationale.append("Curated pharmacokinetic (CYP) mechanism identified between the two drugs.")

    for rule in pd_rules_fired:
        score += _SEVERITY_WEIGHT.get(rule.get("base_severity", "minor"), 1)
        rationale.append(f"Pharmacodynamic rule fired: {rule.get('mechanism')}")

    if pgx_amplification_present:
        score += 1
        rationale.append("Patient pharmacogenomic findings may amplify exposure or risk for one of the two drugs.")

    if clinical_vulnerability_present:
        score += 1
        rationale.append("Patient-specific clinical vulnerability (e.g. active/breakthrough seizures) noted.")

    if score == 0:
        category = "minor"
    elif score <= 2:
        category = "moderate"
    elif score <= 4:
        category = "major"
    else:
        category = "critical"

    return {"score": score, "category": category, "rationale": rationale}
