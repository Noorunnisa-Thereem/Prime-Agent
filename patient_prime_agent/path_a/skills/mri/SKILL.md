---
name: mri
description: Use when asked to generate, regenerate, update, or validate the MRI Structural Assessment Digital Twin report — e.g. "generate the MRI report", "regenerate the MRI summary", "run mri" — or when the mri section of the integrated Digital Twin report needs to be extracted from MRI files in patient_data/MRI.
---

# MRI

## Purpose
Turn preprocessed MRI series metadata into two outputs:
1. A standalone MRI Structural Assessment — one report aggregating every available T1w/FLAIR series.
2. The seven-field `mri` section of the integrated `Digital_Twin_Integrated_Report.json`.

## Inputs
- Source file: `patient_data/MRI/MRI_summary.json` — a JSON array with one entry per series (`file_name`, `patient_id`, `scan_type`, `modality`, `shape`, `voxel_spacing_mm`, `datatype`, `statistics`, `intensity_percentiles`, `brain_volume.volume_mm3`, `histogram`).
- Raw volumes: `patient_data/MRI/sub-<NN>_acq-<...>.nii` — presence/size only checked (`nifti_exists`, `nifti_size_bytes`); voxel data itself is never parsed by this pipeline.
- Standalone generator: `patient_prime_agent/mri_summary.py`
- Integrated extractor: `patient_prime_agent/extractors/imaging.py` (`MRIExtractor`)
- Integrated schema: `schemas/mri.schema.json`
- Standalone output: `reports/mri/MRI_clinical_summary.json`

## Standalone Summary Workflow
1. Load `patient_data/MRI/MRI_summary.json` (a list of per-series metadata objects).
2. For each entry, resolve the matching `.nii` file in the same folder and record whether it exists and its size — never fabricate presence.
3. Group series by `patient_id` (study identifier) and by `scan_type` (`T1w`, `FLAIR`).
4. Compute `imaging_details`: `modalities` (labelled from the scan types actually present: `T1w` -> `"3D T1-weighted MRI"`, `FLAIR` -> `"T2-FLAIR MRI"`), `brain_coverage` (series count + how many study identifiers have both T1w and FLAIR), `spatial_resolution` (voxel spacing per scan type, flagged as `"variable spacing"` when it isn't uniform), `image_consistency` (whether shape and spacing are identical across series of the same scan type).
5. Compute `structural_findings.brain_volume` from `brain_volume.volume_mm3` per series: min/max/mean nonzero-voxel volume per scan type, and a range-percent — status is only `"Stable within available metadata"` when the T1w range is <=5% and the FLAIR range is <=12%, otherwise `"Variable across series"`.
6. Compute `structural_findings.tissue_signal_characteristics` from `statistics.mean`/`statistics.std` per scan type.
7. Every other `structural_findings` entry (`brain_morphology`, `white_matter`, `hippocampus`, `cortical_structure`, `mass_lesion`, `hemorrhage`, `infarction`) is fixed as **not assessed / not determined** — this pipeline has no segmentation, lesion detection, or radiologist review, so these can never be populated from metadata alone.
8. `longitudinal_assessment` and `conclusion` are derived text built only from the computed volume/spacing/consistency values above — never a diagnostic claim.
9. Write the result to `reports/mri/MRI_clinical_summary.json`.

Run:

```powershell
python -m patient_prime_agent.mri_summary
```

## Canonical Output Shape
Every standalone report must use exactly these top-level keys — this is the reference shape (see the reproduced example at the bottom of this file):

| Key | Contents |
| --- | --- |
| `report_type` | fixed `"MRI Structural Assessment"` |
| `patient_id` | comma-joined sorted study identifiers |
| `observation_period` | study/series counts (scan dates are not in the metadata, so this never claims a date range) |
| `mri_summary` | `overall_status`, `clinical_impression` |
| `imaging_details` | `modalities[]`, `brain_coverage`, `image_quality`, `spatial_resolution`, `image_consistency` |
| `structural_findings` | `{brain_morphology, brain_volume, tissue_signal_characteristics, white_matter, hippocampus, cortical_structure, mass_lesion, hemorrhage, infarction}`, each `{status, finding}` |
| `longitudinal_assessment` | `overall_trend`, `comparison_with_previous_scans`, `evidence_of_progression`, `structural_stability` |
| `clinical_inference` | `positive_observations[]`, `overall_interpretation` |
| `recommendations` | `follow_up[]`, `clinical[]` |
| `conclusion` | `summary` |

When `MRI_summary.json` is missing or empty, every scalar is `null`, every list is `[]`, and the eight `structural_findings` entries are all `{status: null, finding: null}` — never a partially fabricated version of the shape.

## Integrated Report Extraction
Populate only these schema fields for the `mri` section of the integrated report:
- `study_date`
- `body_region`
- `indication`
- `contrast`
- `impression`
- `key_findings`
- `urgent_findings`

`MRI_summary.json` has no study date, indication, or radiology impression text — those integrated-report fields can only be populated from a document that states them explicitly (e.g. a narrative MRI report), and stay `null` otherwise.

## Rules
- Keep modality-specific wording from the source.
- Use `null` for absent metadata and `[]` for absent finding lists.
- Do not infer lesion type, acuity, morphology, or diagnosis unless stated — `brain_morphology`, `white_matter`, `hippocampus`, `cortical_structure`, `mass_lesion`, `hemorrhage`, and `infarction` stay "not assessed / not determined" because no segmentation, lesion detection, or radiologist review is available.
- `brain_volume` figures are preprocessing-derived nonzero-voxel volumes, not formal brain volumetry — state that explicitly in the finding text.
- Every recommendation and conclusion sentence must be built only from the counts/ranges computed in this workflow, never an invented clinical judgment.

## Output
- Standalone: `reports/mri/MRI_clinical_summary.json`
- Integrated section: written as part of `reports/Digital_Twin_Integrated_Report.json` when the full harness or agent runtime runs.

## Verification
- `python -m json.tool reports/mri/MRI_clinical_summary.json` must succeed.
- Confirm `patient_id` lists exactly the study identifiers present in `MRI_summary.json`.
- Confirm `imaging_details.modalities` matches the distinct `scan_type` values actually present.
- Confirm every `structural_findings` entry other than `brain_volume` and `tissue_signal_characteristics` stays "not assessed / not determined".
- Validate against `schemas/mri.schema.json` for the integrated section; check that key findings and urgent findings are traceable to the source.
- Confirm no generated MRI summary is written anywhere under `patient_data/`; outputs only go to `reports/`.

## Reference Example
The shape above matches this reference report (trimmed):

```json
{
  "report_type": "MRI Structural Assessment",
  "patient_id": "Patient_001",
  "observation_period": "Past 6 Months",
  "mri_summary": {
    "overall_status": "Structurally Stable",
    "clinical_impression": "Serial MRI examinations demonstrate consistent anatomical coverage and high-quality image acquisition throughout the six-month observation period. No measurable structural progression can be inferred from the available imaging metadata."
  },
  "imaging_details": {
    "modalities": ["3D T1-weighted MRI", "T2-FLAIR MRI"],
    "brain_coverage": "Complete",
    "image_quality": "Excellent",
    "spatial_resolution": "High",
    "image_consistency": "Maintained across follow-up examinations"
  },
  "structural_findings": {
    "brain_morphology": { "status": "Stable", "finding": "No measurable global structural variation identified from available MRI metadata." },
    "brain_volume": { "status": "Preserved", "finding": "Brain volume measurements remain consistent across available examinations without evidence of significant global volume loss." },
    "tissue_signal_characteristics": { "status": "Stable", "finding": "Signal intensity distribution remains consistent between examinations, indicating reliable image acquisition." },
    "white_matter": { "status": "Not Assessed", "finding": "White matter lesion analysis was not available." },
    "hippocampus": { "status": "Not Assessed", "finding": "Hippocampal volumetric evaluation was not performed." },
    "cortical_structure": { "status": "Not Assessed", "finding": "Cortical thickness and cortical morphology were not evaluated." },
    "mass_lesion": { "status": "Not Determined", "finding": "No lesion analysis was available from the provided data." },
    "hemorrhage": { "status": "Not Determined", "finding": "Cannot be evaluated without image interpretation." },
    "infarction": { "status": "Not Determined", "finding": "No assessment available from acquisition metadata." }
  },
  "longitudinal_assessment": {
    "overall_trend": "Stable",
    "comparison_with_previous_scans": "MRI acquisition characteristics remained consistent over the six-month follow-up period with no measurable changes in global structural measurements.",
    "evidence_of_progression": "No structural progression identified from available quantitative metadata.",
    "structural_stability": "Maintained"
  },
  "clinical_inference": {
    "positive_observations": [
      "Consistent MRI acquisition quality across all examinations.",
      "High-resolution anatomical imaging available.",
      "Complete brain coverage achieved."
    ],
    "overall_interpretation": "Based on the available MRI metadata, there is no measurable evidence of structural instability during the six-month observation period."
  },
  "recommendations": {
    "follow_up": ["Perform quantitative brain volumetric analysis.", "Assess hippocampal volume for epilepsy-related structural abnormalities."],
    "clinical": ["Correlate MRI findings with seizure history and neurological examination."]
  },
  "conclusion": {
    "summary": "MRI examinations obtained over the past six months demonstrate stable structural imaging with preserved brain morphology and consistently high image quality."
  }
}
```
