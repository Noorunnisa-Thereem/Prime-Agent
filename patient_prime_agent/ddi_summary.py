"""Drug-drug interaction (DDI) and therapy-evidence assessment.

Standalone script following the same shape as the other ``*_summary.py``
modules in this package (``genetics_summary.py``, ``cbc_summary.py``, ...):
it reads already-generated category summaries and writes one JSON report,
which ``digital_twin_report.py`` then merges verbatim like every other
section.

This module never invents a medication, a dose, a drug interaction, or a
predicted score -- see ``patient_prime_agent/ddi/`` for the modules that back
each piece (normalization, pairing, curated/predicted CYP reference data,
pharmacodynamic rules, pharmacogenomic modifiers, clinical context, and
evidence aggregation).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .core.utils import ensure_dir
from .path_d.ddi import aggregation, clinical_context, pairing, reference_data
from .path_d.ddi.normalizer import normalize_regimen
from .path_d.ddi.pgx_modifiers import collect_pgx_evidence

REPORT_TYPE = "Drug-Drug Interaction & Therapy Evidence Assessment"

DEFAULT_CLINICAL_NOTES_PATH = Path("reports") / "clinical_notes" / "clinical__notes_summary.json"
DEFAULT_GENETICS_PATH = Path("reports") / "genetics" / "genetics_clinical_summary.json"
DEFAULT_EEG_PATH = Path("reports") / "eeg" / "EEG_clinical_summary.json"
DEFAULT_ECG_PATH = Path("reports") / "ecg" / "ECG_Clinical_Summary.json"
DEFAULT_CBC_PATH = Path("reports") / "cbc" / "CBC_consolidated_summary.json"
DEFAULT_EXTERNAL_EVIDENCE_PATH = Path("reports") / "external_evidence" / "External_Evidence_Report.json"
DEFAULT_OUTPUT_PATH = Path("reports") / "ddi" / "DDI_Clinical_Assessment.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the drug-drug interaction and therapy evidence assessment")
    parser.add_argument("--clinical-notes", type=Path, default=DEFAULT_CLINICAL_NOTES_PATH)
    parser.add_argument("--genetics", type=Path, default=DEFAULT_GENETICS_PATH)
    parser.add_argument("--eeg", type=Path, default=DEFAULT_EEG_PATH)
    parser.add_argument("--ecg", type=Path, default=DEFAULT_ECG_PATH)
    parser.add_argument("--cbc", type=Path, default=DEFAULT_CBC_PATH)
    parser.add_argument(
        "--external-evidence",
        type=Path,
        default=DEFAULT_EXTERNAL_EVIDENCE_PATH,
        help="Live PubMed/ClinVar/DailyMed/CPIC/ClinicalTrials.gov report (external_evidence_summary.py). "
        "Optional -- if not yet generated, therapy assessments are built without it.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    report = build_report(
        clinical_notes=_load_json(args.clinical_notes),
        genetics=_load_json(args.genetics),
        eeg=_load_json(args.eeg),
        ecg=_load_json(args.ecg),
        cbc=_load_json(args.cbc),
        external_evidence=_load_json(args.external_evidence),
    )
    ensure_dir(args.output.parent)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote DDI clinical assessment to {args.output}")
    return 0


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _index_external_evidence(
    external_evidence: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], dict[frozenset[str], dict[str, Any]]]:
    """Index external_evidence_summary.py's report for lookup: ``by_drug`` (drug name ->
    its pubmed/dailymed/clinicaltrials_gov entry), gene-drug pairs grouped by drug name (a
    drug can have more than one relevant gene), and ``by_drug_pair`` (an unordered
    frozenset of the two normalized drug names -> its combined-query pubmed entry) so a
    real "Drug A AND Drug B" DDI literature lookup can be matched to the exact
    current-current pair it was queried for, regardless of which drug ended up as drug_a
    vs drug_b in path_d.ddi.pairing's own sorted combinations. Missing or malformed input
    degrades to empty indexes -- callers then simply have nothing to synthesize, never a
    fabricated entry."""
    by_drug: dict[str, Any] = {}
    for entry in external_evidence.get("by_drug") or []:
        if isinstance(entry, dict) and entry.get("drug_name"):
            by_drug[str(entry["drug_name"]).strip().lower()] = entry

    gene_drug_pairs_by_drug: dict[str, list[dict[str, Any]]] = {}
    for pair in external_evidence.get("by_gene_drug_pair") or []:
        if isinstance(pair, dict) and pair.get("drug_name"):
            gene_drug_pairs_by_drug.setdefault(str(pair["drug_name"]).strip().lower(), []).append(pair)

    by_drug_pair: dict[frozenset[str], dict[str, Any]] = {}
    for pair in external_evidence.get("by_drug_pair") or []:
        if isinstance(pair, dict) and pair.get("drug_a") and pair.get("drug_b"):
            key = frozenset({str(pair["drug_a"]).strip().lower(), str(pair["drug_b"]).strip().lower()})
            by_drug_pair[key] = pair

    return by_drug, gene_drug_pairs_by_drug, by_drug_pair


def build_report(
    clinical_notes: dict[str, Any],
    genetics: dict[str, Any],
    eeg: dict[str, Any],
    ecg: dict[str, Any],
    cbc: dict[str, Any],
    external_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    patient_id = _resolve_patient_id(clinical_notes, genetics, cbc)
    external_by_drug, external_gene_drug_pairs_by_drug, external_by_drug_pair = _index_external_evidence(external_evidence or {})

    current_medications, conflicts = normalize_regimen(clinical_notes)
    proposed_medications: list = []  # no proposed-medication list exists in this dataset

    current_regimen_names = {m.normalized_name for m in current_medications}
    pgx_evidence_by_drug = collect_pgx_evidence(genetics, current_regimen_names)

    context_bundle = clinical_context.build_context(clinical_notes, eeg, ecg, cbc)
    regimen_wide_evidence = context_bundle["regimen_wide_evidence"]

    inference = clinical_notes.get("clinical_inference") if isinstance(clinical_notes, dict) else {}
    medication_response = (inference or {}).get("medication_response") or {}

    def _external_pubmed_for_pair(pair: dict[str, Any]) -> dict[str, Any] | None:
        entry = external_by_drug_pair.get(frozenset({pair["drug_a"].normalized_name, pair["drug_b"].normalized_name}))
        return entry.get("pubmed_combined_query") if entry else None

    pairs = pairing.generate_pairs(current_medications, proposed_medications)
    pair_assessments = [
        aggregation.build_pair_assessment(
            pair["drug_a"],
            pair["drug_b"],
            pair["pair_context"],
            pgx_evidence_by_drug,
            regimen_wide_evidence,
            external_pubmed_pair=_external_pubmed_for_pair(pair),
        )
        for pair in pairs
    ]
    current_pair_assessments = [a for a, p in zip(pair_assessments, pairs) if p["pair_context"] == "current_current"]
    proposed_pair_assessments = [a for a, p in zip(pair_assessments, pairs) if p["pair_context"] != "current_current"]

    therapy_assessments = [
        aggregation.build_therapy_assessment(
            medication=medication,
            pgx_evidence=pgx_evidence_by_drug.get(medication.normalized_name, []),
            regimen_wide_evidence=regimen_wide_evidence,
            medication_response=medication_response,
            external_by_drug=external_by_drug.get(medication.normalized_name),
            external_gene_drug_pairs=external_gene_drug_pairs_by_drug.get(medication.normalized_name),
        )
        for medication in current_medications
    ]

    resolved_drugs = [m.normalized_name for m in current_medications if reference_data.curated_cyp_relationships(m.normalized_name) or reference_data.curated_rationale(m.normalized_name)]
    unresolved_drugs = [m.normalized_name for m in current_medications if m.normalized_name not in resolved_drugs]

    relevant_findings = [
        assessment["patient_specific_interpretation"]
        for assessment in current_pair_assessments
        if assessment["status"] == "interaction_detected"
    ]
    for therapy in therapy_assessments:
        if therapy["position"] in ("high_caution", "reconciliation_required"):
            relevant_findings.append(therapy["clinical_impression"])

    uncertainties = [
        "SuperCYPsPred predicted pharmacokinetic evidence is not available locally for any drug in this regimen.",
    ]
    if not context_bundle["context"]["therapeutic_drug_monitoring"]:
        uncertainties.append("No therapeutic drug concentration data is present in the source reports.")
    if context_bundle["context"]["kidney_function"] is None:
        uncertainties.append("No renal function (eGFR) data is present in the source reports.")

    monitoring = sorted({item for therapy in therapy_assessments for item in therapy["recommended_monitoring"]})

    report = {
        "report_type": REPORT_TYPE,
        "patient_id": patient_id,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "medication_reconciliation": {
            "normalized_medications": [m.to_dict() for m in current_medications + proposed_medications],
            "conflicts": conflicts,
        },
        "source_coverage": {
            "flockhart_source": reference_data.FLOCKHART_SOURCE,
            "flockhart_version": reference_data.FLOCKHART_VERSION,
            "supercypspred_source": reference_data.SUPERCYPSPRED_SOURCE,
            "supercypspred_version": reference_data.SUPERCYPSPRED_VERSION,
            "resolved_drugs": resolved_drugs,
            "unresolved_drugs": unresolved_drugs,
            "not_evaluated_drugs": [],
        },
        "clinical_context": context_bundle["context"],
        "current_pair_assessments": current_pair_assessments,
        "proposed_pair_assessments": proposed_pair_assessments,
        "therapy_assessments": therapy_assessments,
        "overall_interpretation": {
            "clinically_relevant_current_findings": relevant_findings,
            "principal_uncertainties": uncertainties,
            "recommended_monitoring": monitoring,
        },
        "limitations": [
            "This assessment is clinical decision support, not an automated prescribing system; no dose, "
            "start, or stop decision is made or implied here.",
            "No proposed/candidate medication list is present in the source data, so only the current "
            "regimen's pairwise and therapy-level evidence is evaluated.",
            "Curated pharmacokinetic (CYP) coverage is limited to a small bundled reference table; a drug "
            "not listed there is reported as unresolved, not as free of interactions.",
            "Therapy assessments also draw on the live external-evidence layer (see Known Limitations under "
            "Pharmacogenomics, above, for its source-by-source scope and caveats).",
        ],
    }
    return report


def _resolve_patient_id(*sections: dict[str, Any]) -> str | None:
    for section in sections:
        if not isinstance(section, dict):
            continue
        direct = section.get("patient_id")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        profile = section.get("patient_profile")
        if isinstance(profile, dict):
            nested = profile.get("patient_id")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None


if __name__ == "__main__":
    raise SystemExit(main())
