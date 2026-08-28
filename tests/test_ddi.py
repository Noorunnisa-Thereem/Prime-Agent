"""Tests for the additive DDI/therapy-evidence layer (patient_prime_agent.ddi).

These exercise the module against the project's own real, already-generated
category summaries under reports/ (the same convention the report_html.py
build already relies on), plus a couple of synthetic inputs to check
dose-conflict detection and the "no fabricated pair severity" safeguard in
isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patient_prime_agent import ddi_summary
from patient_prime_agent.ddi import aggregation, pairing
from patient_prime_agent.ddi.normalizer import normalize_regimen

REPORTS_ROOT = Path(__file__).resolve().parents[1] / "reports"


def _load(relative_path: str) -> dict:
    path = REPORTS_ROOT / relative_path
    if not path.exists():
        pytest.skip(f"fixture not present: {relative_path}")
    return json.loads(path.read_text(encoding="utf-8"))


def test_normalize_regimen_reads_real_current_regimen():
    clinical_notes = _load("clinical_notes/clinical__notes_summary.json")
    medications, conflicts = normalize_regimen(clinical_notes)

    names = {m.normalized_name for m in medications}
    assert names == {"levetiracetam", "lamotrigine"}
    assert conflicts == []  # this dataset has one dose per drug -- no conflict to find
    for medication in medications:
        assert medication.status == "current"
        assert medication.dose_value is not None


def test_dose_conflict_is_detected_when_present():
    clinical_notes = {
        "clinical_inference": {
            "medication_response": {
                "current_regimen": [
                    {"drug": "Lamotrigine", "dose": "100 mg BID"},
                    {"drug": "Lamotrigine", "dose": "150 mg BID"},
                ]
            }
        }
    }
    medications, conflicts = normalize_regimen(clinical_notes)

    assert len(medications) == 1
    assert medications[0].reconciliation_flags == ["dose_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "dose_conflict"
    assert set(conflicts[0]["values"]) == {"100 mg BID", "150 mg BID"}


def test_pairing_never_labels_a_proposed_drug_as_current():
    clinical_notes = _load("clinical_notes/clinical__notes_summary.json")
    current, _ = normalize_regimen(clinical_notes)
    proposed, _ = normalize_regimen(
        {
            "clinical_inference": {
                "medication_response": {"current_regimen": [{"drug": "Carbamazepine", "dose": "200 mg BID"}]}
            }
        }
    )

    pairs = pairing.generate_pairs(current, proposed)
    proposed_pairs = [p for p in pairs if p["pair_context"] != "current_current"]
    assert proposed_pairs, "expected at least one current-proposed pair"
    for pair in proposed_pairs:
        assessment = aggregation.build_pair_assessment(pair["drug_a"], pair["drug_b"], pair["pair_context"], {})
        assert assessment["status"] == "not_evaluated"


def test_build_report_never_invents_the_missing_planning_drugs():
    report = ddi_summary.build_report(
        clinical_notes=_load("clinical_notes/clinical__notes_summary.json"),
        genetics=_load("genetics/genetics_clinical_summary.json"),
        eeg=_load("eeg/EEG_clinical_summary.json"),
        ecg=_load("ecg/ECG_Clinical_Summary.json"),
        cbc=_load("cbc/CBC_consolidated_summary.json"),
    )

    normalized_names = {
        m["normalized_name"] for m in report["medication_reconciliation"]["normalized_medications"]
    }
    # The integration plan's illustrative regimen (diazepam, sertraline, vitamin D3, folic
    # acid) is not part of this patient's real clinical_notes data -- the engine must not
    # invent them just because the plan mentions them.
    assert normalized_names == {"levetiracetam", "lamotrigine"}
    assert report["medication_reconciliation"]["conflicts"] == []
    assert report["proposed_pair_assessments"] == []


def test_pair_severity_never_escalates_without_an_established_mechanism():
    report = ddi_summary.build_report(
        clinical_notes=_load("clinical_notes/clinical__notes_summary.json"),
        genetics=_load("genetics/genetics_clinical_summary.json"),
        eeg=_load("eeg/EEG_clinical_summary.json"),
        ecg=_load("ecg/ECG_Clinical_Summary.json"),
        cbc=_load("cbc/CBC_consolidated_summary.json"),
    )
    for assessment in report["current_pair_assessments"]:
        if assessment["status"] == "no_interaction_detected":
            assert assessment["severity"] == "minor"


def test_therapy_assessments_flag_unresolved_ddi_coverage():
    report = ddi_summary.build_report(
        clinical_notes=_load("clinical_notes/clinical__notes_summary.json"),
        genetics=_load("genetics/genetics_clinical_summary.json"),
        eeg=_load("eeg/EEG_clinical_summary.json"),
        ecg=_load("ecg/ECG_Clinical_Summary.json"),
        cbc=_load("cbc/CBC_consolidated_summary.json"),
    )
    for therapy in report["therapy_assessments"]:
        unresolved_modalities = {e["modality"] for e in therapy["unresolved_evidence"]}
        assert "pharmacokinetic_ddi" in unresolved_modalities
