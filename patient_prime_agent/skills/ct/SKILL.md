---
name: ct
description: Use when asked to generate, regenerate, update, or validate the CT scan Digital Twin summary report — e.g. "generate the CT report", "regenerate the CT scan summary", "run ct" — or when the ct section of the integrated Digital Twin report needs to be extracted from CT image files in patient_data/CT.
---

# CT

## Purpose
Turn CT head/brain PNG image files into two outputs:
1. A standalone, longitudinal CT Scan Clinical Summary — one entry per study, in acquisition order.
2. The seven-field `ct` section of the integrated `Digital_Twin_Integrated_Report.json`.

## Inputs
- Source files: `patient_data/CT/CT_Scan_<NN>_<YYYYMMDD>_<HHMMSS>.png`
- Standalone generator: `patient_prime_agent/ct_scan_summary.py`
- Template (report label / patient id / report type only): `patient_data/Clinical_summary_for each_data/CT_scan_clinical_summary.json`
- Extractor: `patient_prime_agent/extractors/imaging.py` (`CTExtractor`)
- Schema: `schemas/ct.schema.json`
- Standalone output: `reports/ct_scan/CT_scan_clinical_summary.json`

## Standalone Summary Workflow
1. Glob `patient_data/CT/*.png` and parse `studyId` (`CT_Scan_<NN>`) and `studyDate` from the filename `CT_Scan_<NN>_<YYYYMMDD>_<HHMMSS>.png`.
2. Read `reportLabel`, `patientId`, and `reportMetadata.reportType` from the template JSON if it exists; otherwise fall back to `"CT Scan Clinical Summary"`, `"PATIENT-01"`, `"neuroimaging_longitudinal_summary"`.
3. For each image, look up its `findings`/`impression` text by exact `studyId` in the `VISUAL_FINDINGS` table in `ct_scan_summary.py`. Files with no entry get the generic fallback: `"CT image reviewed; no structured radiology report text was available for this file."`
4. Append the source PNG's pixel resolution to `findings` (`"Source image resolution: <w> x <h> pixels."`) when it can be read.
5. Set `verifiedByRadiologist` to `false` for every study — this pipeline never marks an image-only read as clinician-verified.
6. Set `reportMetadata.periodCovered` to the min/max `studyDate` across all studies and `numberOfStudies` to the study count.
7. Build `comparison`: no studies -> "no files available"; one study -> "comparison not possible"; two or more -> the fixed image-only limitation statement (never a fabricated trend).
8. Write the result to `reports/ct_scan/CT_scan_clinical_summary.json`.

Run:

```powershell
python -m patient_prime_agent.ct_scan_summary
```

## Canonical Output Shape
Every standalone report must use exactly these top-level keys — this is the reference shape (see the reproduced example at the bottom of this file):

| Key | Contents |
| --- | --- |
| `reportLabel` | fixed report title, from the template or the default |
| `patientId` | from the template or the default |
| `reportMetadata` | `reportType`, `modality` (`"CT"`), `bodyPart` (`"Head/Brain"`), `periodCovered.{startDate, endDate}`, `numberOfStudies` |
| `studies[]` | one entry per PNG, in filename order: `{studyId, studyDate, findings, impression, verifiedByRadiologist}` |
| `comparison` | one fixed longitudinal-limitation statement, chosen by study count (see step 7 above) |

When no CT PNGs are found, `studies` is `[]`, `periodCovered.startDate`/`endDate` are `null`, `numberOfStudies` is `0`, and `comparison` states no files were available — never a partially fabricated version of the shape.

## Integrated Report Extraction
Populate only these schema fields for the `ct` section of the integrated report:
- `study_date`
- `body_region`
- `indication`
- `contrast`
- `impression`
- `key_findings`
- `urgent_findings`

## Rules
- Favor the radiology impression over broad findings.
- Use `null` for absent metadata and `[]` for absent finding lists.
- Do not diagnose beyond the report wording; the `VISUAL_FINDINGS` lookup text is curated, synthetic-demo content, not a real radiologist read — never rephrase it into a stronger clinical claim.
- `verifiedByRadiologist` is always `false` for this pipeline; never set it `true`.
- A filename with no `VISUAL_FINDINGS` entry gets the generic fallback text — never invent findings for it.
- Mark image-only standalone summaries as not radiologist verified.

## Output
- Standalone: `reports/ct_scan/CT_scan_clinical_summary.json`
- Integrated section: written as part of `reports/Digital_Twin_Integrated_Report.json` when the full harness or agent runtime runs.

## Verification
- `python -m json.tool reports/ct_scan/CT_scan_clinical_summary.json` must succeed.
- Confirm `reportMetadata.numberOfStudies` equals the number of `CT_Scan_*.png` files in `patient_data/CT`.
- Confirm `reportMetadata.periodCovered` matches the earliest/latest filename dates.
- Confirm every `studies[].studyId` is unique and every `verifiedByRadiologist` is `false`.
- Validate against `schemas/ct.schema.json` for the integrated section; check that urgent findings are a subset of source-supported key findings.
- Confirm no generated CT summary is written anywhere under `patient_data/`; outputs only go to `reports/`.

## Reference Example
The shape above matches this reference report (trimmed to two studies):

```json
{
  "reportLabel": "CT Scan Clinical Summary",
  "patientId": "PATIENT-01",
  "reportMetadata": {
    "reportType": "neuroimaging_longitudinal_summary",
    "modality": "CT",
    "bodyPart": "Head/Brain",
    "periodCovered": { "startDate": "2026-01-12", "endDate": "2026-06-29" },
    "numberOfStudies": 6
  },
  "studies": [
    {
      "studyId": "CT_Scan_01",
      "studyDate": "2026-01-12",
      "findings": "The lateral ventricles are visualized at the level of the frontal horns and bodies and appear grossly symmetric. No midline shift. No hyperdense collection to suggest acute intracranial hemorrhage.",
      "impression": "No acute intracranial abnormality on this synthetic demo read.",
      "verifiedByRadiologist": false
    },
    {
      "studyId": "CT_Scan_06",
      "studyDate": "2026-06-29",
      "findings": "The frontal horns of the lateral ventricles are visualized. Small symmetric hyperdense foci are noted near the ventricular atria/trigones bilaterally, in a location and appearance typical of benign choroid plexus calcification. No midline shift.",
      "impression": "No acute abnormality identified. Incidental bilateral choroid plexus calcification (common benign finding) on this synthetic demo read.",
      "verifiedByRadiologist": false
    }
  ],
  "comparison": "Direct interval comparison across the six studies is limited by differences in image resolution, windowing, and slice positioning between files; a quantitative trend/progression assessment is not rendered in this synthetic demo report."
}
```
