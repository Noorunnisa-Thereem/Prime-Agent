"""Pharmacodynamic interaction rules, independent of CYP metabolism.

Rules fire on drug *class* pairs (see ``reference_data.DRUG_CLASS_MAP``), not
on specific brand names, so the table is reusable if the regimen changes.
None of these rules fire for the current two-drug regimen (levetiracetam +
lamotrigine, both antiepileptics) -- there is no benzodiazepine or
serotonergic agent in it today. The table is still implemented in full so it
is exercised by unit tests and ready the moment a rescue or psychiatric
medication is added to the source data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import reference_data


@dataclass(slots=True, frozen=True)
class PharmacodynamicRule:
    rule_id: str
    category: str
    drug_classes_a: tuple[str, ...]
    drug_classes_b: tuple[str, ...]
    mechanism: str
    expected_consequence: str
    base_severity: str
    monitoring: tuple[str, ...]
    evidence_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "mechanism": self.mechanism,
            "expected_consequence": self.expected_consequence,
            "base_severity": self.base_severity,
            "monitoring": list(self.monitoring),
            "evidence_source": self.evidence_source,
        }


RULES: tuple[PharmacodynamicRule, ...] = (
    PharmacodynamicRule(
        rule_id="pd-cns-depression-01",
        category="cns_depression",
        drug_classes_a=("benzodiazepine_sedative",),
        drug_classes_b=("antiepileptic", "benzodiazepine_sedative"),
        mechanism="Additive central nervous system depression from combined GABAergic/sedative agents.",
        expected_consequence="Increased sedation, dizziness, impaired coordination, cognitive slowing.",
        base_severity="moderate",
        monitoring=("Sedation level", "Coordination/gait", "Cognitive slowing"),
        evidence_source="General pharmacodynamic reference (class-level).",
    ),
    PharmacodynamicRule(
        rule_id="pd-serotonergic-01",
        category="serotonergic_burden",
        drug_classes_a=("ssri",),
        drug_classes_b=("ssri",),
        mechanism="Additive serotonergic activity from combined serotonergic agents.",
        expected_consequence="Increased serotonin-syndrome risk, activation, sleep disturbance, agitation.",
        base_severity="moderate",
        monitoring=("Serotonin-syndrome signs", "Sleep quality", "Agitation"),
        evidence_source="General pharmacodynamic reference (class-level).",
    ),
    PharmacodynamicRule(
        rule_id="pd-seizure-threshold-01",
        category="seizure_threshold",
        drug_classes_a=("*",),
        drug_classes_b=("*",),
        mechanism="Some non-antiepileptic agents can lower seizure threshold.",
        expected_consequence="Potential worsening of seizure susceptibility in a patient with active epilepsy.",
        base_severity="major",
        monitoring=("Seizure frequency", "EEG-derived future seizure risk"),
        evidence_source="General pharmacodynamic reference (class-level); applies only to proposed, non-antiepileptic candidates.",
    ),
)


def evaluate_pair(drug_a_normalized: str, drug_b_normalized: str) -> list[dict[str, Any]]:
    """Return every rule whose class pattern matches this pair's drug classes."""

    class_a = reference_data.drug_class(drug_a_normalized)
    class_b = reference_data.drug_class(drug_b_normalized)
    if class_a is None or class_b is None:
        return []

    fired: list[dict[str, Any]] = []
    for rule in RULES:
        if rule.category == "seizure_threshold":
            continue  # only meaningful for current-proposed pairs; handled by callers explicitly
        forward = class_a in rule.drug_classes_a and class_b in rule.drug_classes_b
        backward = class_b in rule.drug_classes_a and class_a in rule.drug_classes_b
        if forward or backward:
            fired.append(rule.to_dict())
    return fired
