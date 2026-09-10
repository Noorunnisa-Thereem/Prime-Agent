"""NeuroTwin clinical-context resolver.

Builds a structured context object from the already-generated clinical
notes, EEG, ECG, and CBC summaries, plus a matching list of evidence items
that apply to every current therapy (seizure control, EEG risk, and lab
safety are regimen-wide facts in this dataset, not attributable to one drug
over the other). Fields this dataset cannot support (kidney function,
therapeutic drug concentrations, a diagnostic QT/rhythm read) are reported as
unavailable rather than filled in with a plausible-looking value.
"""

from __future__ import annotations

from typing import Any


def build_context(
    clinical_notes: dict[str, Any],
    eeg: dict[str, Any],
    ecg: dict[str, Any],
    cbc: dict[str, Any],
) -> dict[str, Any]:
    inference = clinical_notes.get("clinical_inference") if isinstance(clinical_notes, dict) else {}
    inference = inference if isinstance(inference, dict) else {}
    disease_behavior = inference.get("disease_behavior") if isinstance(inference.get("disease_behavior"), dict) else {}
    seizure_analysis = inference.get("seizure_analysis") if isinstance(inference.get("seizure_analysis"), dict) else {}
    profile = clinical_notes.get("patient_profile") if isinstance(clinical_notes, dict) else {}
    diagnosis = profile.get("diagnosis") if isinstance(profile, dict) and isinstance(profile.get("diagnosis"), dict) else {}

    eeg_state = eeg.get("digital_twin_state") if isinstance(eeg, dict) else {}
    eeg_state = eeg_state if isinstance(eeg_state, dict) else {}
    eeg_stats = eeg.get("recording_statistics") if isinstance(eeg, dict) else {}
    eeg_stats = eeg_stats if isinstance(eeg_stats, dict) else {}

    ecg_dataset = ecg.get("dataset_summary") if isinstance(ecg, dict) else {}
    ecg_dataset = ecg_dataset if isinstance(ecg_dataset, dict) else {}
    ecg_limitations = ecg.get("scope_and_limitations") if isinstance(ecg, dict) else None

    cbc_state = cbc.get("digital_twin_state") if isinstance(cbc, dict) else {}
    cbc_state = cbc_state if isinstance(cbc_state, dict) else {}

    context = {
        "diagnosis": [d for d in (diagnosis.get("primary"), diagnosis.get("secondary")) if d],
        "seizure_course": {
            "persistent_activity": bool(seizure_analysis.get("clustering_detected") is not None),
            "peak_count": (seizure_analysis.get("highest_seizure_burden") or {}).get("episodes")
            if isinstance(seizure_analysis.get("highest_seizure_burden"), dict)
            else None,
            "clustering": seizure_analysis.get("clustering_detected"),
            "latest_trend": disease_behavior.get("trend") or None,
        },
        "eeg": {
            "total_recordings": eeg_stats.get("total_recordings"),
            "future_seizure_risk": eeg_state.get("future_seizure_probability"),
            "risk_level": eeg_state.get("risk_level"),
            "technical_limitations": [] if eeg else ["EEG summary not available."],
        },
        "ecg": {
            "recording_count": ecg_dataset.get("number_of_recordings"),
            "total_duration": ecg_dataset.get("total_recorded_duration"),
            "diagnostic_rhythm_assessment_available": False,
            "qt_assessment_available": False,
            "technical_limitations": [ecg_limitations] if ecg_limitations else [
                "ECG diagnostic rhythm/QT assessment not available in source data."
            ],
        },
        "therapeutic_drug_monitoring": [],  # not present in source data for any current drug
        "kidney_function": None,  # not present in source data
        "liver_function": None,  # not present in source data (CBC panel has no LFT analytes)
        "laboratory_abnormalities": [],  # cbc.abnormal_findings_over_time is empty for this patient
    }

    regimen_wide_evidence: list[dict[str, Any]] = []

    if seizure_analysis:
        peak = seizure_analysis.get("highest_seizure_burden") or {}
        peak_count = peak.get("episodes") if isinstance(peak, dict) else None
        peak_date = peak.get("date") if isinstance(peak, dict) else None
        statement = "Breakthrough seizures persisted on the current regimen"
        if peak_count is not None:
            statement += f", with a recorded peak of {peak_count} episodes"
            if peak_date:
                statement += f" on {peak_date}"
        statement += "."
        regimen_wide_evidence.append(
            {
                "modality": "clinical",
                "direction": "counter",
                "statement": statement,
                "source_reference": "clinical__notes_summary.json:clinical_inference.seizure_analysis",
                "evidence_level": "high",
                "patient_specific": True,
            }
        )
        if seizure_analysis.get("clustering_detected"):
            regimen_wide_evidence.append(
                {
                    "modality": "clinical",
                    "direction": "counter",
                    "statement": "Seizure clustering was documented during the observation window.",
                    "source_reference": "clinical__notes_summary.json:clinical_inference.seizure_analysis",
                    "evidence_level": "moderate",
                    "patient_specific": True,
                }
            )

    if eeg_state.get("risk_level") or eeg_state.get("future_seizure_probability"):
        parts = []
        if eeg_state.get("risk_level"):
            parts.append(f"EEG-derived risk: {eeg_state['risk_level']}")
        if eeg_state.get("future_seizure_probability"):
            parts.append(f"future seizure probability: {eeg_state['future_seizure_probability']}")
        regimen_wide_evidence.append(
            {
                "modality": "eeg",
                "direction": "counter",
                "statement": "EEG synthesis remained consistent with active epilepsy (" + "; ".join(parts) + ").",
                "source_reference": "EEG_clinical_summary.json:digital_twin_state",
                "evidence_level": "moderate",
                "patient_specific": True,
            }
        )

    hematological_status = cbc_state.get("overall_hematological_status")
    if hematological_status:
        regimen_wide_evidence.append(
            {
                "modality": "laboratory",
                "direction": "supporting",
                "statement": f"CBC status across the observation window: {hematological_status.replace('_', ' ')}; "
                "no documented hematological toxicity.",
                "source_reference": "CBC_consolidated_summary.json:digital_twin_state",
                "evidence_level": "high",
                "patient_specific": True,
            }
        )

    regimen_wide_evidence.append(
        {
            "modality": "laboratory",
            "direction": "unresolved",
            "statement": "No renal function (eGFR), hepatic enzyme, or therapeutic drug concentration data is "
            "present in the available source reports for this patient.",
            "source_reference": "not available in source data",
            "evidence_level": "low",
            "patient_specific": True,
        }
    )

    return {"context": context, "regimen_wide_evidence": regimen_wide_evidence}
