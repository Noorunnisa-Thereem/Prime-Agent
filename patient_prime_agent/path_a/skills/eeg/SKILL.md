---
name: eeg
description: Use when asked to generate, regenerate, update, or validate the EEG Digital Twin longitudinal clinical summary — e.g. "generate the EEG report", "regenerate the EEG summary", "run eeg" — or when the eeg section of the integrated Digital Twin report needs to be extracted from EEG recordings in patient_data/EEG.
---

# EEG

## Purpose
Turn three EEG phase categories (ictal, interictal, pre_ictal) into one consolidated longitudinal summary:
1. A standalone EEG clinical summary combining all three phases into one report.
2. The `eeg` section of the integrated `Digital_Twin_Integrated_Report.json`.

## Inputs
- Source folders (all under `patient_data/EEG/`): `ictal_preprocess_data/`, `interictal_preprocess_data/`, `preictal_preprocess_data/` — one JSON per recording (`file_name`, `patient_id`, `phase`, `channels`, `samples`, `global_statistics.{minimum, maximum, mean, std, variance}`, `channel_statistics[]`).
- Raw segments: `patient_data/EEG/ictal/`, `interictal/`, `pre_ictal/` (`.mat` files) — not parsed; `.mat` is not a supported extension, so every statistic comes from the matching `*_preprocess_data/*.json` file. A raw segment with no matching preprocess JSON is excluded from the report rather than estimated.
- Standalone generator: `patient_prime_agent/eeg_summary.py`
- Integrated extractor: `patient_prime_agent/extractors/electrophysiology.py` (`EEGExtractor`)
- Integrated schema: `schemas/eeg.schema.json`
- Standalone output: `reports/eeg/EEG_clinical_summary.json`

## Standalone Summary Workflow
1. Load every `*.json` file under the three `*_preprocess_data` folders; parse the recording date from the filename (`..._<YYYYMMDD>_<HHMMSS>.json`).
2. Group recordings by phase: `interictal`, `preictal` (source `phase` field is `"pre_ictal"`, report key is `"preictal"`), `ictal`.
3. `recording_statistics`: counts per phase and total; `channels`/`samples_per_recording` only when uniform across every recording, else `null`.
4. `signal_statistics[phase]`: `amplitude_range` = `[min(minimum), max(maximum)]` across that phase's recordings (rounded to the nearest integer); `standard_deviation_range` / `variance_range` = `[min, max]` of `std`/`variance` (rounded to 2 decimals). Never estimate a phase's range from another phase.
5. `phase_analysis[phase].state` is a fixed glossary label (`interictal` -> `"Baseline"`, `preictal` -> `"Transition"`, `ictal` -> `"Seizure"`) — this names the phase category itself, not a claim about the patient.
6. `phase_analysis[phase].electrical_activity` / `signal_variability` are assigned by **ranking** the three phases' mean variance (lowest -> `Stable`/`Low`, middle -> `Increasing Instability`/`Moderate-High`, highest -> `Hypersynchronous`/`Very High`) — computed from the actual numbers, not tied to the phase name.
7. `derived_features`: `variance_trend`/`amplitude_trend` are `"Increasing"` only when mean variance strictly increases interictal -> preictal -> ictal, else `"Variable"`. `maximum_activity_phase`/`minimum_activity_phase` are the phases with the highest/lowest mean variance. `dominant_discriminative_feature` is chosen by comparing the relative inter-phase separation of variance, standard deviation, and amplitude span, and picking whichever separates the phases most.
8. `longitudinal_analysis.pattern_consistency` is `"High"` only when the interictal -> preictal -> ictal variance progression is monotonic across the whole dataset; `baseline_stability` is `"Stable"` only when the interictal phase's own variance has a coefficient of variation <= 0.5.
9. `clinical_findings.{preictal_transition, ictal_events_detected, recovery_pattern_detected}` and `digital_twin_features.{state_transition_detected, longitudinal_consistency}` are booleans derived from actual phase presence and date ordering (a "recovery pattern" is detected only when an interictal recording's date falls after the earliest ictal recording's date) — never assumed true by default.
10. Write the result to `reports/eeg/EEG_clinical_summary.json`.

Run:

```powershell
python -m patient_prime_agent.eeg_summary
```

## Canonical Output Shape
Every standalone report must use exactly these top-level keys — this is the reference shape (see the reproduced example at the bottom of this file):

| Key | Contents |
| --- | --- |
| `report_metadata` | `report_type`, `patient_id`, `modality`, `analysis_type`, `observation_period`, `purpose` |
| `recording_statistics` | `total_recordings`, `interictal`, `preictal`, `ictal`, `channels`, `samples_per_recording` |
| `clinical_findings` | classification labels (see Rules) plus 3 derived booleans |
| `signal_statistics` | `{interictal, preictal, ictal}`, each `{amplitude_range, standard_deviation_range, variance_range}` |
| `phase_analysis` | `{interictal, preictal, ictal}`, each `{state, electrical_activity, signal_variability}` |
| `longitudinal_analysis` | `disease_pattern`, `progression[]`, `pattern_consistency`, `baseline_stability`, `seizure_evolution` |
| `derived_features` | `variance_trend`, `amplitude_trend`, `electrical_instability`, `maximum_activity_phase`, `minimum_activity_phase`, `dominant_discriminative_feature` |
| `key_biomarkers` | `primary[]`, `secondary[]` — fixed labels naming what this report computes |
| `digital_twin_state` | classification labels (see Rules) plus `risk_level`, `future_seizure_probability`, `patient_specific_pattern` |
| `digital_twin_features` | `baseline_state`, `transition_state`, `acute_state`, `recovery_state`, `state_transition_detected`, `longitudinal_consistency` |
| `recommended_monitoring[]` | fixed list naming the metrics this report tracks |
| `overall_observation` | `observation`, `supporting_evidence[]`, `clinical_interpretation`, `digital_twin_interpretation` |

When no preprocessed recordings are found, every count is `0`, `signal_statistics`/`phase_analysis` are `{}`, every derived string is `null` or `"Undetermined"`, and every derived boolean is `false` — never a partially fabricated version of the shape.

## Integrated Report Extraction
Populate only these schema fields for the `eeg` section of the integrated report:
- `study_date`
- `background`
- `epileptiform_activity`
- `events`
- `interpretation`
- `notable_abnormalities`

## Rules
- Every number in `signal_statistics` must trace to an explicit `global_statistics` value in a preprocessed recording for that phase — never interpolate or estimate.
- `phase_analysis` intensity labels (`electrical_activity`, `signal_variability`) must be assigned by ranking the actual computed variance, never hardcoded to a phase name.
- **The source data is organized into ictal/interictal/preictal EEG segments — that folder taxonomy is itself an epilepsy phase-classification structure.** `clinical_findings.{primary_condition, disease_course, baseline_activity}` and `digital_twin_state.{neurological_state, disease_stage}` are fixed labels that describe what this dataset represents by construction, not a clinician-verified diagnosis of this patient. Never present them as a confirmed clinical finding beyond what the folder taxonomy establishes.
- `digital_twin_state.{risk_level, future_seizure_probability}` use `"Elevated"` (not an absolute clinical risk grade like `"High"`) when at least one ictal recording is present, and `"Undetermined"` otherwise — this pipeline has no validated seizure-risk model.
- A raw `.mat` segment with no matching preprocess JSON is silently excluded from every count and statistic — never estimated from a partial read.
- `overall_observation.clinical_interpretation` must state that this is a signal-classification observation, not a clinician-verified diagnosis.

## Output
- Standalone: `reports/eeg/EEG_clinical_summary.json`
- Integrated section: written as part of `reports/Digital_Twin_Integrated_Report.json` when the full harness or agent runtime runs.

## Verification
- `python -m json.tool reports/eeg/EEG_clinical_summary.json` must succeed.
- Confirm `recording_statistics.total_recordings` equals the sum of every `*_preprocess_data` folder's JSON file count.
- Confirm each `signal_statistics[phase].amplitude_range`/`standard_deviation_range`/`variance_range` matches the min/max of the raw `global_statistics` values for that phase.
- Confirm `derived_features.maximum_activity_phase`/`minimum_activity_phase` are the phases with the actual highest/lowest mean variance.
- Validate against `schemas/eeg.schema.json` for the integrated section; confirm event and abnormality text is traceable to the source file.
- Confirm no generated EEG summary is written anywhere under `patient_data/`; outputs only go to `reports/`.

## Reference Example
The shape above matches this reference report (trimmed):

```json
{
  "report_metadata": {
    "report_type": "EEG",
    "patient_id": "Patient_001",
    "modality": "Electroencephalography",
    "analysis_type": "Longitudinal",
    "observation_period": "6 Months",
    "purpose": "Digital Twin Feature Extraction"
  },
  "recording_statistics": {
    "total_recordings": 33, "interictal": 13, "preictal": 10, "ictal": 10,
    "channels": 1, "samples_per_recording": 1024
  },
  "clinical_findings": {
    "primary_condition": "Epileptic Seizure Disorder", "disease_course": "Chronic", "baseline_activity": "Preserved",
    "preictal_transition": true, "ictal_events_detected": true, "recovery_pattern_detected": true
  },
  "signal_statistics": {
    "interictal": { "amplitude_range": [-89, 99], "standard_deviation_range": [11.68, 23.50], "variance_range": [136.44, 552.46] },
    "preictal": { "amplitude_range": [-345, 440], "standard_deviation_range": [14.48, 92.07], "variance_range": [209.71, 8477.48] },
    "ictal": { "amplitude_range": [-504, 784], "standard_deviation_range": [52.50, 286.42], "variance_range": [2756.47, 82033.96] }
  },
  "phase_analysis": {
    "interictal": { "state": "Baseline", "electrical_activity": "Stable", "signal_variability": "Low" },
    "preictal": { "state": "Transition", "electrical_activity": "Increasing Instability", "signal_variability": "Moderate-High" },
    "ictal": { "state": "Seizure", "electrical_activity": "Hypersynchronous", "signal_variability": "Very High" }
  },
  "longitudinal_analysis": {
    "disease_pattern": "Recurrent",
    "progression": ["Interictal", "Preictal", "Ictal", "Recovery"],
    "pattern_consistency": "High", "baseline_stability": "Stable",
    "seizure_evolution": "Consistent across observation period"
  },
  "derived_features": {
    "variance_trend": "Increasing", "amplitude_trend": "Increasing",
    "electrical_instability": "Increasing before seizure onset",
    "maximum_activity_phase": "Ictal", "minimum_activity_phase": "Interictal",
    "dominant_discriminative_feature": "Signal Variance"
  },
  "key_biomarkers": {
    "primary": ["Signal Variance", "Standard Deviation", "Signal Amplitude", "Electrical Stability"],
    "secondary": ["Phase Transition", "Temporal Evolution", "Recovery Pattern"]
  },
  "digital_twin_state": {
    "neurological_state": "Epileptic", "disease_stage": "Chronic Active",
    "risk_level": "High", "future_seizure_probability": "High",
    "patient_specific_pattern": "Stable Longitudinal Seizure Evolution"
  },
  "digital_twin_features": {
    "baseline_state": "Normal Interictal Activity", "transition_state": "Preictal Electrical Instability",
    "acute_state": "Ictal Seizure Activity", "recovery_state": "Return to Baseline",
    "state_transition_detected": true, "longitudinal_consistency": true
  },
  "recommended_monitoring": [
    "Signal Variance", "Amplitude Dynamics", "Electrical Stability",
    "Preictal Detection", "Seizure Frequency", "Temporal Disease Progression"
  ],
  "overall_observation": {
    "observation": "Longitudinal EEG recordings demonstrate a consistent transition from stable interictal activity to preictal electrical instability followed by high-amplitude ictal seizure activity.",
    "supporting_evidence": [
      "Signal variance progressively increases before seizure onset.",
      "Amplitude increases significantly during ictal recordings."
    ],
    "clinical_interpretation": "Findings are consistent with chronic recurrent epilepsy exhibiting reproducible seizure evolution.",
    "digital_twin_interpretation": "EEG features provide sufficient longitudinal information to model baseline, transition, seizure, and recovery states for personalized neurological Digital Twin generation."
  }
}
```

Note: the live-generated report uses `"Elevated"` for `risk_level`/`future_seizure_probability` instead of the reference's `"High"` — this pipeline has no validated seizure-risk model, so it signals that ictal events were captured without asserting an absolute clinical risk grade. See Rules above.
