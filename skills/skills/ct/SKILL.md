---
name: ct
description: Generate CT scan AI Coding Agent reports from CT_Scan images using the CT clinical-summary key structure. Use when the task mentions CT, CT_Scan images, CT_scan_clinical_summary.json, CT findings, CT impressions, number of CT studies, or CT longitudinal comparison.
---

# CT

## Purpose

Generate CT scan clinical summaries from patient CT image files while using the reference CT JSON only for keys and structure.

## When to Use

Use this skill for CT image-folder summaries, CT clinical summary JSON, CT report validation, CT_Scan folder processing, and CT output filename/key-shape questions. Do not use it for MRI, EEG, ECG, CBC, questionnaire, genetics, or clinical-note summaries.

## Supported Inputs

CT image files from `data\raw\DT_Data_2\DT_Data\CT_Scan`, including PNG, JPG, JPEG, TIF, TIFF, and BMP files. Use `data\raw\DT_Data_2\DT_Data\Clinical_summary_for each_data\CT_scan_clinical_summary.json` only as a key/schema reference.

## Workflow

Detect CT images, extract `studyId` and `studyDate` from filenames, generate findings/impressions from image-derived evidence and configured model output when available, calculate period and study count from detected images, and write the CT clinical-summary JSON shape.

## Model, OCR, VLM, And Evidence Instructions

Use deterministic image metadata extraction for dimensions, grayscale statistics, pixel-intensity fractions, filenames, dates, and study IDs. Use a validated CT-capable VLM/model only when configured and available; record limitations when only fallback image statistics are available. Do not use OCR unless a CT image contains embedded labels needed for evidence. Never ask a model to assert absence or presence of hemorrhage, infarct, mass effect, edema, hydrocephalus, fracture, calcification, lesion, or midline shift unless that assertion is supported by model output or source radiology text.

Each study's `findings` and `impression` must identify whether the evidence came from image statistics, VLM/model output, or source report text.

## Required JSON Schemas

```json
{
  "ct_generated": {
    "reportLabel": null,
    "patientId": null,
    "reportMetadata": {
      "reportType": null,
      "modality": null,
      "bodyPart": null,
      "periodCovered": {
        "startDate": null,
        "endDate": null
      },
      "numberOfStudies": null
    },
    "studies": [],
    "comparison": null
  },
  "ct_digital_twin": {
    "patient_profile": {},
    "study_information": {},
    "brain_structures": {},
    "intracranial_findings": {},
    "findings": null,
    "impression": null,
    "clinical_significance": {},
    "digital_twin_state": {},
    "evidence": {}
  }
}
```

## CT Clinical Summary Key Contract

Use this shape for CT summaries generated from `CT_Scan` folder images:

```json
{
  "reportLabel": "CT Scan Clinical Summary",
  "patientId": "generated patient id",
  "reportMetadata": {
    "reportType": "neuroimaging_longitudinal_summary",
    "modality": "CT",
    "bodyPart": "Head/Brain",
    "periodCovered": {
      "startDate": "YYYY-MM-DD",
      "endDate": "YYYY-MM-DD"
    },
    "numberOfStudies": 0
  },
  "studies": [
    {
      "studyId": "CT_Scan_01",
      "studyDate": "YYYY-MM-DD",
      "findings": "source/model-supported CT findings",
      "impression": "source/model-supported CT impression",
      "verifiedByRadiologist": false
    }
  ],
  "comparison": null
}
```

## Extraction Rules

- Extract `studyId` and `studyDate` from CT image filenames such as `CT_Scan_01_20260112_091530.png`.
- Calculate `periodCovered.startDate`, `periodCovered.endDate`, and `numberOfStudies` from detected CT images.
- Generate `findings`, `impression`, and `comparison` from CT image evidence and model output when available.
- Use `CT_scan_clinical_summary.json` only for keys/shape, never for report values.

## Consolidation Rules

Sort studies by `studyDate`. Generate `comparison` from changes in available source-backed findings and image-derived quality/metadata across studies. If only fallback image evidence exists, state that diagnostic longitudinal comparison is limited.

## Validation Rules

Keep top-level clinical keys exactly as `reportLabel`, `patientId`, `reportMetadata`, `studies`, and `comparison`. Keep each study object exactly as `studyId`, `studyDate`, `findings`, `impression`, and `verifiedByRadiologist`. Do not use `AI_coding_agent_ct_consolidated_summary.json` as the schema/format reference. Do not copy findings, impressions, dates, patient IDs, or comparison text from any reference JSON.

Do not hallucinate hemorrhage, infarct, mass effect, edema, hydrocephalus, fracture, calcification, lesion, or midline shift. Mention limitations when image quality, slice coverage, or model availability limits interpretation. Set unsupported scalar values to `null` only when they cannot be derived from source images or model output.

## Error Handling

If images cannot be decoded or a model is unavailable, state the limitation in `findings`/`impression` and keep unsupported clinical claims out of the result. If no CT images are detected, report the missing source folder or skipped CT module.

## Output

Write CT generated summaries with readable report names like `SYN-100482_generated_ct_summary_2026-01-12_to_2026-06-29.json` or `SYN-100482 generated ct summary 2026-01-12 to 2026-06-29.json`. Use that patient/date CT generated-summary format for CT summaries.
