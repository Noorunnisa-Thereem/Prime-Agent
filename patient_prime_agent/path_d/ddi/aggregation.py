"""Evidence aggregation, conflict resolution, and assessment generation.

Builds the two output shapes the report renders: a per-pair
``InteractionAssessment`` and a per-medication ``TherapyAssessment``. Applies
the plan's deterministic conflict-resolution rules rather than free-text
reasoning: patient-specific observed evidence outranks predicted evidence, a
favorable genotype cannot override documented toxicity, "unresolved" is
never collapsed into "no interaction", and a proposed drug is never
described as a current interaction (enforced upstream by ``pair_context``).

``build_therapy_assessment`` also synthesizes evidence from the live
external-evidence layer (``patient_prime_agent.external_evidence_summary`` /
``external_lookup/``, real PubMed/ClinVar/DailyMed/CPIC/ClinicalTrials.gov
results) per the NeuroTwin Multimodal Biomedical Resource Guide's
"Information available" and "How green and red evidence should be stated"
sections: only a source that actually returned data for this exact drug or
gene-drug pair contributes an item -- there is no fixed per-source column,
and a source with nothing to say is never forced into the assessment. Every
synthesized item is ``patient_specific: False`` (population-level
plausibility, per the guide's "database evidence only, not patient-verified"
principle) with a source-appropriate ``evidence_level``, and its
``direction`` follows the guide's own stated limitation for that source --
e.g. ClinVar's own asserted clinical significance can genuinely support or
counter (Benign vs. Pathogenic), while PubMed/DailyMed/ClinicalTrials.gov
findings stay "unresolved" regardless of whether something was found, since
the guide explicitly states each of those requires further review before
being converted into a patient-specific flag.
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
    external_pubmed_pair: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``external_pubmed_pair`` is a single, real PubMed envelope from a query combining
    BOTH drug names (e.g. "Lamotrigine AND Levetiracetam" -- see
    external_evidence_summary.py's ``by_drug_pair``), never two single-drug lookups merged
    together. Folded in via ``_external_pubmed_item``, which always returns
    direction="unresolved" regardless of whether a citation was found -- a found reference
    needs review before it becomes a finding, and a "no_results"/error result is an honest
    coverage gap, never presented as "no interaction" (per the plan's conflict-resolution
    rule #6)."""
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

    pubmed_pair_item = _external_pubmed_item(external_pubmed_pair)
    if pubmed_pair_item is not None:
        evidence.append(pubmed_pair_item)

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


def _external_pubmed_item(pubmed: dict[str, Any] | None) -> dict[str, Any] | None:
    """PubMed ("core literature source"): per the resource guide, "PubMed is an index,
    not an appraisal system... cannot be converted directly into a patient flag without
    study-level review." A found citation is real, useful context -- but is never itself
    supporting or counter evidence, so this always returns "unresolved"."""
    if not isinstance(pubmed, dict):
        return None
    if pubmed.get("status") == "ok" and pubmed.get("records"):
        rec = pubmed["records"][0]
        title = rec.get("title") or "(title not returned by PubMed)"
        statement = f'PubMed candidate reference (PMID {rec.get("pmid")}): "{title}" -- requires study-level review before use as a patient-specific finding.'
    elif pubmed.get("status") == "no_results":
        statement = "PubMed: no citation matched this exact query at the time checked."
    elif pubmed.get("error"):
        statement = f"PubMed: {pubmed.get('note') or 'not retrieved in this session'}."
    else:
        return None
    return {
        "modality": "literature",
        "direction": "unresolved",
        "statement": statement,
        "source_reference": (pubmed.get("records") or [{}])[0].get("url") or "PubMed (NCBI E-utils)",
        "evidence_level": "moderate" if pubmed.get("status") == "ok" else "low",
        "patient_specific": False,
    }


def _external_clinvar_item(clinvar: dict[str, Any] | None) -> dict[str, Any] | None:
    """ClinVar ("patient-specific variant interpretation context"): its own asserted
    clinical_significance is a real, specific classification -- per the guide's "How
    green and red evidence should be stated," a Benign/Likely benign assertion is a
    genuine narrow-supported finding (green) and a Pathogenic/Likely pathogenic or
    Conflicting assertion is a genuine specific conflict (red). Anything else
    (Uncertain significance, not classified, no match) stays unresolved -- it is not
    evidence either way."""
    if not isinstance(clinvar, dict):
        return None
    if clinvar.get("status") == "ok" and clinvar.get("records"):
        rec = clinvar["records"][0]
        significance = str(rec.get("clinical_significance") or "").lower()
        if "pathogenic" in significance or "conflicting" in significance:
            direction = "counter"
        elif "benign" in significance:
            direction = "supporting"
        else:
            direction = "unresolved"
        total = clinvar.get("total_matches_in_clinvar")
        statement = (
            f'ClinVar {rec.get("accession")}: {rec.get("clinical_significance") or "not classified"} '
            f"({total} total gene-wide record(s) for this gene, not variant-specific)."
        )
        return {
            "modality": "genomic_evidence",
            "direction": direction,
            "statement": statement,
            "source_reference": rec.get("url") or "ClinVar (NCBI E-utils)",
            "evidence_level": "moderate",
            "patient_specific": False,
        }
    if clinvar.get("status") == "no_results":
        statement = "ClinVar: no record matched this exact gene query at the time checked."
    elif clinvar.get("error"):
        statement = f"ClinVar: {clinvar.get('note') or 'not retrieved in this session'}."
    else:
        return None
    return {
        "modality": "genomic_evidence",
        "direction": "unresolved",
        "statement": statement,
        "source_reference": "ClinVar (NCBI E-utils)",
        "evidence_level": "low",
        "patient_specific": False,
    }


def _external_cpic_item(cpic: dict[str, Any] | None) -> dict[str, Any] | None:
    """CPIC ("the strongest bridge from a validated pharmacogenomic result to a
    medication-specific action... explains how to use a result IF IT EXISTS"): an active,
    versioned dosing guideline for this exact gene-drug pair is itself a real, narrow,
    supported fact -- an established, actionable relationship exists -- regardless of what
    the guideline recommends, so this counts as supporting. No guideline (or no pair on
    CPIC at all) is unresolved, never a reassuring "no interaction.\""""
    if not isinstance(cpic, dict):
        return None
    if cpic.get("status") == "ok" and cpic.get("records"):
        rec = cpic["records"][0]
        guideline = rec.get("guideline")
        if guideline and guideline.get("url"):
            return {
                "modality": "pharmacogenomic",
                "direction": "supporting",
                "statement": (
                    f'CPIC has an active dosing guideline for this gene-drug pair (evidence level '
                    f'{rec.get("cpic_level")}): {guideline.get("name")}.'
                ),
                "source_reference": guideline.get("url"),
                "evidence_level": "high",
                "patient_specific": False,
            }
        return {
            "modality": "pharmacogenomic",
            "direction": "unresolved",
            "statement": f'CPIC has assessed this gene-drug pair (evidence level {rec.get("cpic_level")}) but has not published an active dosing guideline.',
            "source_reference": "CPIC (api.cpicpgx.org)",
            "evidence_level": "moderate",
            "patient_specific": False,
        }
    if cpic.get("status") == "no_results":
        statement = cpic.get("note") or "CPIC has no record for this gene-drug pair."
    elif cpic.get("error"):
        statement = cpic.get("note") or "not retrieved in this session"
    else:
        return None
    return {
        "modality": "pharmacogenomic",
        "direction": "unresolved",
        "statement": f"CPIC: {statement}",
        "source_reference": "CPIC (api.cpicpgx.org)",
        "evidence_level": "low",
        "patient_specific": False,
    }


def _external_dailymed_item(dailymed: dict[str, Any] | None) -> dict[str, Any] | None:
    """DailyMed ("authoritative label language for known interactions, contraindications...
    should anchor many red-flag rules"): this lookup only retrieves the Structured Product
    Label's identity/metadata, not its warnings/interactions section text, so a found label
    cannot yet be classified as supporting or counter -- it stays unresolved, flagging that
    the actual label content still needs direct review."""
    if not isinstance(dailymed, dict):
        return None
    if dailymed.get("status") == "ok" and dailymed.get("records"):
        rec = dailymed["records"][0]
        statement = (
            f'DailyMed label on file: "{rec.get("title")}" (published {rec.get("published_date")}); '
            "label section text (warnings/interactions/contraindications) was not extracted by this "
            "lookup and requires direct review."
        )
        evidence_level = "moderate"
        source_reference = rec.get("url") or "DailyMed (NLM REST API v2)"
    elif dailymed.get("status") == "no_results":
        statement = "DailyMed: no label matched this exact drug name at the time checked."
        evidence_level = "low"
        source_reference = "DailyMed (NLM REST API v2)"
    elif dailymed.get("error"):
        statement = f"DailyMed: {dailymed.get('note') or 'not retrieved in this session'}."
        evidence_level = "low"
        source_reference = "DailyMed (NLM REST API v2)"
    else:
        return None
    return {
        "modality": "labeling",
        "direction": "unresolved",
        "statement": statement,
        "source_reference": source_reference,
        "evidence_level": evidence_level,
        "patient_specific": False,
    }


def _external_trials_item(trials: dict[str, Any] | None) -> dict[str, Any] | None:
    """ClinicalTrials.gov ("shows whether a therapy... has been studied... registration
    does not imply positive results or high study quality"): a matching trial is context
    about what has been studied, never itself a supporting or counter finding."""
    if not isinstance(trials, dict):
        return None
    if trials.get("status") == "skipped":
        return None  # no diagnosis available to search on -- nothing meaningful to add
    if trials.get("status") == "ok" and trials.get("records"):
        rec = trials["records"][0]
        statement = (
            f'ClinicalTrials.gov {rec.get("nct_id")} ({rec.get("overall_status")}): "{rec.get("brief_title")}" '
            "-- registration does not establish study quality or results."
        )
        source_reference = rec.get("url") or "ClinicalTrials.gov (API v2)"
    elif trials.get("status") == "no_results":
        statement = "ClinicalTrials.gov: no trial matched this exact condition/drug query at the time checked."
        source_reference = "ClinicalTrials.gov (API v2)"
    elif trials.get("error"):
        statement = f"ClinicalTrials.gov: {trials.get('note') or 'not retrieved in this session'}."
        source_reference = "ClinicalTrials.gov (API v2)"
    else:
        return None
    return {
        "modality": "clinical_trial",
        "direction": "unresolved",
        "statement": statement,
        "source_reference": source_reference,
        "evidence_level": "low",
        "patient_specific": False,
    }


def _synthesize_external_evidence(
    external_by_drug: dict[str, Any] | None,
    external_gene_drug_pairs: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Per the resource guide's "How green and red evidence should be stated" and
    "Minimum provenance to retain": synthesize one evidence item per source that
    actually returned something for this exact drug (PubMed/DailyMed/ClinicalTrials.gov,
    keyed by drug name) or this exact gene-drug pair (CPIC/ClinVar, keyed by gene+drug) --
    never a fixed column layout, and a source with nothing real to contribute is skipped
    rather than padded with an empty entry."""
    items: list[dict[str, Any]] = []
    external_by_drug = external_by_drug or {}
    for builder, key in (
        (_external_pubmed_item, "pubmed"),
        (_external_dailymed_item, "dailymed"),
        (_external_trials_item, "clinicaltrials_gov"),
    ):
        item = builder(external_by_drug.get(key))
        if item is not None:
            items.append(item)

    for pair in external_gene_drug_pairs or []:
        for item in (_external_cpic_item(pair.get("cpic")), _external_clinvar_item(pair.get("clinvar"))):
            if item is not None:
                items.append(item)

    return items


def build_therapy_assessment(
    medication: Medication,
    pgx_evidence: list[dict[str, Any]],
    regimen_wide_evidence: list[dict[str, Any]],
    medication_response: dict[str, Any],
    external_by_drug: dict[str, Any] | None = None,
    external_gene_drug_pairs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    all_evidence = list(pgx_evidence) + list(regimen_wide_evidence)
    all_evidence.extend(_synthesize_external_evidence(external_by_drug, external_gene_drug_pairs))
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
