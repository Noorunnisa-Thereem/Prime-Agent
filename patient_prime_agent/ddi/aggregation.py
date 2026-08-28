"""Evidence aggregation, conflict resolution, and assessment generation.

Builds the two output shapes the report renders: a per-pair
``InteractionAssessment`` and a per-medication ``TherapyAssessment``. Applies
the plan's deterministic conflict-resolution rules rather than free-text
reasoning: patient-specific observed evidence outranks predicted evidence, a
favorable genotype cannot override documented toxicity, "unresolved" is
never collapsed into "no interaction", and a proposed drug is never
described as a current interaction (enforced upstream by ``pair_context``).
"""

from __future__ import annotations

from typing import Any

from . import pharmacodynamic_rules, reference_data
from .normalizer import Medication
from .severity import score_pair

_MONITORING_BY_MODALITY = {
    "clinical": "Continue seizure frequency, duration, and clustering tracking.",
    "eeg": "Continue EEG-derived seizure-risk monitoring.",
    "laboratory": "Continue periodic CBC and safety-laboratory monitoring.",
    "pharmacogenomic": "Review the priority safety flags in the Pharmacogenomics section before adding or changing an interacting medication.",
}

_UNRESOLVED_DDI_NOTE = (
    "{drug} was not resolved against a live drug-drug interaction knowledge base in the "
    "available sources; only curated reference pharmacology and this patient's own "
    "pharmacogenomic findings were evaluated."
)


def build_pair_assessment(
    drug_a: Medication,
    drug_b: Medication,
    pair_context: str,
    pgx_evidence_by_drug: dict[str, list[dict[str, Any]]],
    regimen_wide_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    regimen_wide_evidence = regimen_wide_evidence or []
    pk_summary = reference_data.pharmacokinetic_pair_summary(drug_a.normalized_name, drug_b.normalized_name)
    pd_rules = pharmacodynamic_rules.evaluate_pair(drug_a.normalized_name, drug_b.normalized_name)

    pgx_a = pgx_evidence_by_drug.get(drug_a.normalized_name, [])
    pgx_b = pgx_evidence_by_drug.get(drug_b.normalized_name, [])
    # Per the plan's safeguard, a PGx finding only *amplifies* a pair's severity when an
    # established pharmacokinetic/pharmacodynamic mechanism already links the two drugs --
    # it must never manufacture pair-level severity on its own (that belongs to the
    # therapy-level assessment, which already reflects it independently).
    mechanism_established = pk_summary["mechanism_found"] or bool(pd_rules)
    pgx_amplification_present = mechanism_established and any(
        item["direction"] == "counter" for item in pgx_a + pgx_b
    )

    evidence: list[dict[str, Any]] = []
    mechanisms: list[dict[str, Any]] = []

    evidence.append(
        {
            "modality": "pharmacokinetic_ddi",
            "direction": "supporting" if not pk_summary["mechanism_found"] else "counter",
            "statement": pk_summary["statement"],
            "source_reference": f"{reference_data.FLOCKHART_SOURCE} ({reference_data.FLOCKHART_VERSION})",
            "evidence_level": "high" if pk_summary["evidence"] else "moderate",
            "patient_specific": False,
        }
    )
    if pk_summary["mechanism_found"]:
        mechanisms.append(
            {"type": "pharmacokinetic", "description": pk_summary["statement"], "enzymes": [o["enzyme"] for o in pk_summary["evidence"]]}
        )

    evidence.append(
        {
            "modality": "pharmacokinetic_ddi",
            "direction": "unresolved",
            "statement": f"SuperCYPsPred predicted-interaction evidence is not available locally for "
            f"{drug_a.normalized_name} and {drug_b.normalized_name}.",
            "source_reference": "SuperCYPsPred (no local snapshot)",
            "evidence_level": "predicted",
            "patient_specific": False,
        }
    )

    for rule in pd_rules:
        mechanisms.append({"type": "pharmacodynamic", "description": rule["mechanism"], "enzymes": []})
        evidence.append(
            {
                "modality": "pharmacodynamic_ddi",
                "direction": "counter",
                "statement": f"{rule['mechanism']} Expected consequence: {rule['expected_consequence']}",
                "source_reference": rule["evidence_source"],
                "evidence_level": "moderate",
                "patient_specific": False,
            }
        )

    seen_statements: set[tuple[str, str]] = set()
    for item in pgx_a + pgx_b:
        key = (item["modality"], item["statement"])
        if key in seen_statements:
            continue
        seen_statements.add(key)
        evidence.append(item)

    # Same rule as above: patient vulnerability can raise the severity of an *established*
    # interaction, but must not by itself turn a "no mechanism found" pair into a
    # moderate/major one -- that would contradict the pair's own status below.
    clinical_vulnerability_present = mechanism_established and any(
        item.get("modality") == "clinical" and item.get("direction") == "counter" for item in regimen_wide_evidence
    )

    severity = score_pair(
        pk_mechanism_found=pk_summary["mechanism_found"],
        pd_rules_fired=pd_rules,
        pgx_amplification_present=pgx_amplification_present,
        clinical_vulnerability_present=clinical_vulnerability_present,
    )

    if pair_context != "current_current":
        status = "not_evaluated"
        interpretation = (
            f"{drug_b.source_name if pair_context == 'current_proposed' else drug_a.source_name} is a "
            "candidate medicine, not part of the current regimen; this pair is not reported as an active interaction."
        )
    elif pk_summary["mechanism_found"] or pd_rules:
        status = "interaction_detected"
        interpretation = (
            f"Current evidence supports a mechanism between {drug_a.source_name} and {drug_b.source_name}: "
            + pk_summary["statement"]
        )
    else:
        status = "no_interaction_detected"
        interpretation = (
            f"No resolved pharmacokinetic or pharmacodynamic interaction mechanism was identified between "
            f"{drug_a.source_name} and {drug_b.source_name} in the available curated and rule-based sources. "
            "Predicted-model coverage (SuperCYPsPred) remains unresolved; absence of data does not establish "
            "absence of risk."
        )

    monitoring = sorted({_MONITORING_BY_MODALITY[e["modality"]] for e in evidence if e["modality"] in _MONITORING_BY_MODALITY})

    return {
        "drug_a": drug_a.source_name,
        "drug_b": drug_b.source_name,
        "pair_context": pair_context,
        "status": status,
        "mechanisms": mechanisms,
        "evidence": evidence,
        "severity": severity["category"],
        "severity_rationale": severity["rationale"],
        "patient_specific_interpretation": interpretation,
        "monitoring": monitoring,
        "limitations": [
            "Predicted (SuperCYPsPred) pharmacokinetic evidence is not available locally for this pair.",
        ],
    }


def build_therapy_assessment(
    medication: Medication,
    pgx_evidence: list[dict[str, Any]],
    regimen_wide_evidence: list[dict[str, Any]],
    medication_response: dict[str, Any],
) -> dict[str, Any]:
    all_evidence = list(pgx_evidence) + list(regimen_wide_evidence)
    all_evidence.append(
        {
            "modality": "pharmacokinetic_ddi",
            "direction": "unresolved",
            "statement": _UNRESOLVED_DDI_NOTE.format(drug=medication.source_name),
            "source_reference": "curated reference pharmacology only (see reference_data)",
            "evidence_level": "low",
            "patient_specific": False,
        }
    )

    drug_toxicity = bool(medication_response.get("drug_toxicity")) if isinstance(medication_response, dict) else False
    if not drug_toxicity:
        all_evidence.append(
            {
                "modality": "clinical",
                "direction": "supporting",
                "statement": "No documented drug toxicity for this agent in the clinical-notes source.",
                "source_reference": "clinical__notes_summary.json:clinical_inference.medication_response",
                "evidence_level": "high",
                "patient_specific": True,
            }
        )

    supporting = [e for e in all_evidence if e["direction"] == "supporting"]
    counter = [e for e in all_evidence if e["direction"] == "counter"]
    unresolved = [e for e in all_evidence if e["direction"] == "unresolved"]

    if medication.reconciliation_flags:
        position = "reconciliation_required"
    elif any(e["evidence_level"] == "high" and e["direction"] == "counter" and e["modality"] == "pharmacogenomic" for e in counter):
        position = "high_caution"
    elif "incomplete" in str(medication_response.get("overall_inference", "")).lower():
        position = "effectiveness_incomplete"
    elif counter and supporting:
        position = "continuation_with_monitoring"
    elif supporting and not counter:
        position = "continuation_supported"
    else:
        position = "evidence_insufficient"

    clinical_impression = (
        f"{len(supporting)} supporting, {len(counter)} counter, and {len(unresolved)} unresolved evidence "
        f"item(s) were identified for {medication.source_name}."
    )
    inference_text = medication_response.get("overall_inference") if isinstance(medication_response, dict) else None
    if inference_text:
        clinical_impression += f" Clinical-notes inference: {inference_text}"

    monitoring = sorted({_MONITORING_BY_MODALITY[e["modality"]] for e in all_evidence if e["modality"] in _MONITORING_BY_MODALITY})

    return {
        "medication": medication.to_dict(),
        "supporting_evidence": supporting,
        "counter_evidence": counter,
        "unresolved_evidence": unresolved,
        "clinical_impression": clinical_impression,
        "position": position,
        "recommended_monitoring": monitoring,
    }
