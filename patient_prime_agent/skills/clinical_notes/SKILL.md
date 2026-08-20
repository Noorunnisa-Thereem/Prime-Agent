---
name: clinical-notes
description: Use when asked to generate, regenerate, update, or validate the clinical notes Digital Twin summary report — e.g. "generate the clinical notes report", "regenerate the clinical notes summary", "run clinical_notes" — or when the clinical_notes section of the integrated Digital Twin report needs to be extracted from epilepsy visit-note PDFs in patient_data/Clinical_Notes.
---

# Clinical Notes

## Purpose
Turn paired printed + handwritten epilepsy visit-note PDFs into two outputs:
1. A standalone, longitudinal Digital Twin summary — the rich multi-visit narrative report.
2. The seven-field `clinical_notes` section of the integrated `Digital_Twin_Integrated_Report.json`.

## Inputs
- Source PDFs: `patient_data/Clinical_Notes/Report_<NN>_<timestamp>_printed.pdf` paired with `Report_<NN>_<timestamp>_handwritten.pdf`, one pair per visit.
- Standalone generator: `patient_prime_agent/clinical_notes_summary.py`
- Integrated extractor: `patient_prime_agent/extractors/clinical_notes.py`
- Integrated schema: `schemas/clinical_notes.schema.json`
- Standalone output: `reports/clinical_notes/clinical__notes_summary.json`

## Standalone Summary Workflow
1. Group PDFs by visit id parsed from the filename (`Report_(\d+)_(\d{8}_\d{6})_(printed|handwritten)\.pdf`).
2. Extract PDF text with `load_document` (`pypdf`/`PyPDF2` via `file_tools`).
3. From the **printed** report parse: patient name, age, diagnosis date, current medications, seizure history, lab summary, the 7 quality-of-life domain scores (0-100), the 5 seizure-type counts, average/longest seizure duration, clustering flag, post-ictal recovery time, public-seizure worry rating, and the aura / medication-effect / sleep / trigger / cognitive / lab status checklists.
4. From the **handwritten** note parse: visit date, patient name, age, diagnosis text, clinician impression, and plan.
5. Aggregate all visits, sorted by date, into the canonical output shape below.
6. Write the result to `reports/clinical_notes/clinical__notes_summary.json`.

Run:

```powershell
python -m patient_prime_agent.clinical_notes_summary
```

## Canonical Output Shape
Every standalone report must use exactly these top-level keys — this is the reference shape (see the reproduced example at the bottom of this file):

| Key | Contents |
| --- | --- |
| `patient_profile` | `patient_id`, `name`, `age`, `gender`, `diagnosis.{primary, secondary, diagnosis_date, disease_stage}` |
| `observation_window` | `start_date`, `end_date`, `total_visits`, `follow_up_frequency` |
| `longitudinal_summary` | `overall_clinical_status`, `overall_disease_trajectory`, `overall_risk_level`, `clinical_progression[]` — one `{date, status, summary}` per visit |
| `clinical_inference.disease_behavior` | `pattern`, `trend`, `disease_control`, `neurological_decline`, `functional_decline` |
| `clinical_inference.seizure_analysis` | `overall_pattern`, `total_recorded_visits`, `highest_seizure_burden{date, episodes}`, `lowest_seizure_burden{date, episodes}`, `average_duration_trend[]`, `clustering_detected`, `overall_inference` |
| `clinical_inference.aura_analysis` | `persistent_patterns[]`, `clinical_significance`, `digital_twin_use` |
| `clinical_inference.medication_response` | `current_regimen[{drug, dose}]`, `effectiveness`, `tolerability`, `persistent_side_effects[]`, `drug_toxicity`, `overall_inference` |
| `clinical_inference.sleep_analysis` | `overall_trend`, `average_sleep`, `persistent_issues[]`, `clinical_relationship` |
| `clinical_inference.cognitive_analysis` | `overall_status`, `memory`, `attention`, `language`, `mental_clarity`, `overall_inference` |
| `clinical_inference.laboratory_analysis` | `cbc`, `liver_function`, `platelet_count`, `white_cell_count`, `medication_safety`, `overall_inference` |
| `clinical_inference.quality_of_life` | `overall_status`, `daily_function`, `mobility`, `social_function`, `overall_inference` |
| `major_clinical_events[]` | one `{date, event, importance, clinical_impact}` per notable visit |
| `digital_twin_state` | `current_state`, `state_vector.{disease_stability, treatment_response, medication_safety, cognitive_health, sleep_health, quality_of_life, overall_health_score}`, `risk_prediction.{future_seizure_risk, status_epilepticus_risk, hospitalization_risk, cognitive_decline_risk, medication_toxicity_risk, functional_decline_risk}`, `monitoring_priorities[]`, `recommended_actions[]` |
| `executive_summary` | `clinical_conclusion` |

When no visit PDFs are found, `build_report` returns this same shape with every scalar `null`, every list `[]`, and `observation_window.total_visits` / `clinical_inference.seizure_analysis.total_recorded_visits` set to `0` — never a partially fabricated version of it.

## Integrated Report Extraction
Populate only these schema fields for the `clinical_notes` section of the integrated report:
- `chief_complaint`
- `history_of_present_illness`
- `diagnoses`
- `medications`
- `allergies`
- `plan`
- `notable_findings`

## Rules
- Keep clinically important statements only.
- Treat `NKDA` as `No known drug allergies`.
- Do not invent missing diagnoses, medications, allergies, plans, dates, seizure metrics, domain scores, or risk levels — every populated field must trace back to text in a source PDF (printed or handwritten) for that visit.
- Preserve exact source wording when it helps traceability.
- Clean PDF encoding artifacts (e.g. `â€”`, stray em/en dashes) before writing report text.
- If a visit's printed or handwritten file is missing, only that visit's corresponding fields go unset — never fabricate the missing side from the other file.

## Output
- Standalone: `reports/clinical_notes/clinical__notes_summary.json`
- Integrated section: written as part of `reports/Digital_Twin_Integrated_Report.json` when the full harness or agent runtime runs.

## Verification
- `python -m json.tool reports/clinical_notes/clinical__notes_summary.json` must succeed.
- Confirm `observation_window.total_visits` equals the number of printed/handwritten PDF pairs found in `patient_data/Clinical_Notes`.
- Confirm every date in `clinical_progression` and `major_clinical_events` traces to a real visit PDF.
- Confirm `medication_response.current_regimen` matches "Current Meds:" from the first printed report.
- Confirm no generated clinical-notes summary is written anywhere under `patient_data/`; outputs only go to `reports/`.

## Reference Example
The shape above matches this reference report (trimmed to structure-defining content):

```json
{
  "patient_profile": {
    "patient_id": "Patient_001",
    "name": "John Doe",
    "age": 34,
    "gender": "Unknown",
    "diagnosis": {
      "primary": "Focal Impaired Awareness Epilepsy",
      "secondary": "Secondary Generalized Tonic-Clonic Seizures",
      "diagnosis_date": "2021-05-12",
      "disease_stage": "Chronic"
    }
  },
  "observation_window": {
    "start_date": "2026-01-01",
    "end_date": "2026-03-02",
    "total_visits": 5,
    "follow_up_frequency": "Bi-weekly"
  },
  "longitudinal_summary": {
    "overall_clinical_status": "Stable with intermittent exacerbations followed by recovery",
    "overall_disease_trajectory": "Moderately Controlled Epilepsy",
    "overall_risk_level": "Moderate",
    "clinical_progression": [
      { "date": "2026-01-01", "status": "Baseline Stable", "summary": "..." }
    ]
  },
  "clinical_inference": {
    "disease_behavior": { "pattern": "...", "trend": "...", "disease_control": "Partial", "neurological_decline": false, "functional_decline": false },
    "seizure_analysis": {
      "overall_pattern": "...",
      "total_recorded_visits": 5,
      "highest_seizure_burden": { "date": "2026-02-15", "episodes": 11 },
      "lowest_seizure_burden": { "date": "2026-01-16", "episodes": 4 },
      "average_duration_trend": [3.4, 9.0, 6.7, 8.2, 4.5],
      "clustering_detected": true,
      "overall_inference": "..."
    },
    "aura_analysis": { "persistent_patterns": ["Visual Aura", "Epigastric Rising Sensation"], "clinical_significance": "...", "digital_twin_use": "..." },
    "medication_response": {
      "current_regimen": [{ "drug": "Levetiracetam", "dose": "1000 mg BID" }, { "drug": "Lamotrigine", "dose": "150 mg BID" }],
      "effectiveness": "Moderately Effective",
      "tolerability": "Good",
      "persistent_side_effects": ["Morning dizziness", "Occasional fatigue"],
      "drug_toxicity": false,
      "overall_inference": "..."
    },
    "sleep_analysis": { "overall_trend": "Gradual improvement", "average_sleep": "6-7 hours", "persistent_issues": ["Sleep fragmentation"], "clinical_relationship": "..." },
    "cognitive_analysis": { "overall_status": "Preserved", "memory": "Stable", "attention": "Stable", "language": "...", "mental_clarity": "...", "overall_inference": "..." },
    "laboratory_analysis": { "cbc": "Normal", "liver_function": "Normal", "platelet_count": "Stable", "white_cell_count": "Stable", "medication_safety": "...", "overall_inference": "..." },
    "quality_of_life": { "overall_status": "Stable", "daily_function": "Independent", "mobility": "Preserved", "social_function": "Maintained", "overall_inference": "..." }
  },
  "major_clinical_events": [
    { "date": "2026-01-16", "event": "Seizure clustering detected", "importance": "High", "clinical_impact": "..." }
  ],
  "digital_twin_state": {
    "current_state": "Moderately Controlled Chronic Epilepsy",
    "state_vector": {
      "disease_stability": 0.76, "treatment_response": 0.79, "medication_safety": 0.93,
      "cognitive_health": 0.91, "sleep_health": 0.71, "quality_of_life": 0.81, "overall_health_score": 0.82
    },
    "risk_prediction": {
      "future_seizure_risk": "Moderate", "status_epilepticus_risk": "Low", "hospitalization_risk": "Low",
      "cognitive_decline_risk": "Low", "medication_toxicity_risk": "Low", "functional_decline_risk": "Low"
    },
    "monitoring_priorities": ["Seizure frequency", "Seizure duration", "Medication adherence"],
    "recommended_actions": ["Continue current anti-epileptic regimen.", "Maintain regular sleep schedule."]
  },
  "executive_summary": {
    "clinical_conclusion": "Across five follow-up visits, the patient demonstrated a fluctuating but overall stable epilepsy course. ..."
  }
}
```
