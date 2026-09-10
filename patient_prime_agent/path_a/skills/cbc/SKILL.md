---
name: cbc
description: Use when asked to generate, regenerate, update, or validate the CBC Digital Twin longitudinal summary — e.g. "generate the CBC report", "regenerate the CBC summary", "run cbc" — or when the cbc section of the integrated Digital Twin report needs to be extracted from CBC report PDFs in patient_data/CBC.
---

# CBC

## Purpose
Turn a series of single-visit CBC lab report PDFs into two outputs:
1. A standalone, longitudinal CBC consolidated summary — one entry per report plus dataset-wide trends.
2. The `cbc` section of the integrated `Digital_Twin_Integrated_Report.json`.

## Inputs
- Source PDFs: `patient_data/CBC/CBC_<NN>_<YYYYMMDD>_<HHMMSS>.pdf` — one PDF per visit, each a single-page tabular CBC report (`Investigation` / `Result` / `Reference Value` / `Unit` rows).
- Standalone generator: `patient_prime_agent/cbc_summary.py`
- Integrated extractor: `patient_prime_agent/extractors/cbc.py` (`CBCExtractor`)
- Integrated schema: `schemas/cbc.schema.json`
- Standalone output: `reports/cbc/CBC_consolidated_summary.json`

## Standalone Summary Workflow
1. Glob `patient_data/CBC/*.pdf`; read each PDF page by page with `pypdf`/`PyPDF2` (needed to record which page a value came from).
2. From the header, extract `Patient Name`, `Patient ID`, `Age/Sex`, and `Report Date`.
3. For each of the 14 known parameter labels (Hemoglobin (Hb), Total RBC count, Packed Cell Volume (PCV), Mean Corpuscular Volume (MCV), MCH, MCHC, RDW, Total WBC count, Neutrophils, Lymphocytes, Eosinophils, Monocytes, Basophils, Platelet Count), find the exact label line and read the three lines that follow it: numeric result, reference range, unit. Keep the reference range as the **raw text from the row** (e.g. `"0 - 6"` vs `"13.0 - 17.0"`) — never reformat it, since the source PDF itself is not consistently formatted.
4. `status` per parameter is `"low"`/`"high"`/`"normal"`, computed only by comparing the extracted value against that same row's extracted reference bounds — never an external clinical threshold.
5. `clinical_flags` (`anemia`, `polycythemia`, `leukocytosis`, `leukopenia`, `thrombocytopenia`, `thrombocytosis`) are derived the same way, from hemoglobin/WBC/platelet vs. their own in-report reference bounds. `neutropenia` always stays `null` (see Rules).
6. `hematological_assessment` groups parameters into red cell / white cell / platelet lineages and reports `within_reference_ranges` unless a grouped parameter's status is not `"normal"`.
7. Aggregate across all reports into `longitudinal_trends[parameter]`: full value history, `first_value`, `latest_value`, `minimum_value`, `maximum_value`, `trend_direction` (`"increasing"`/`"decreasing"`/`"stable"` from first vs. latest value only), `abnormal_dates`.
8. Build `processing_manifest` from the real glob result: detected/processed/failed file counts and paths — a PDF that fails to parse is recorded as failed, never silently dropped or guessed.
9. Write the result to `reports/cbc/CBC_consolidated_summary.json`.

Run:

```powershell
python -m patient_prime_agent.cbc_summary
```

## Canonical Output Shape
Every standalone report must use exactly these top-level keys — this is the reference shape (see the reproduced example at the bottom of this file):

| Key | Contents |
| --- | --- |
| `report_label` | fixed report title |
| `patient_profile` | `patient_name`, `patient_id`, `age`, `sex` |
| `patient_id` | top-level duplicate of `patient_profile.patient_id` |
| `observation_window` | `start_date`, `end_date`, `number_of_reports`, `source_folder` |
| `cbc_reports[]` | one entry per PDF: `report_date`, `report_metadata`, `cbc_parameters` (14 keys), `abnormal_findings[]`, `hematological_assessment`, `overall_cbc_impression`, `clinical_flags`, `risk_assessment`, `digital_twin_health_state` |
| `longitudinal_trends` | one entry per parameter (14 keys) with the full value history and derived trend |
| `abnormal_findings_over_time[]` | flattened `{date, finding}` list across every report |
| `digital_twin_state` | `overall_hematological_status`, `active_flags_by_date`, `unsupported_clinical_risk` |
| `executive_summary` | `clinical_conclusion`, `basis` |
| `processing_manifest` | `source_type`, counts, `status`, and the detected/processed/failed/skipped file lists |

Each `cbc_parameters` entry: `{parameter, value, unit, reference_range, reference_low, reference_high, status, confidence, source_page}`.

When no PDFs are found or none parse, every list is `[]`/`{}`, every scalar is `null`, and `processing_manifest.status` is `"no_input"` — never a partially fabricated version of the shape.

## Integrated Report Extraction
Populate only these schema fields for the `cbc` section of the integrated report:
- `collection_date`
- `hemoglobin_g_per_dL`, `hematocrit_pct`, `rbc_million_uL`, `wbc_10e3_uL`, `platelets_10e3_uL`, `mcv_fL`, `mch_pg`, `mchc_g_dL`, `rdw_pct`
- `differential.{neutrophils_pct, lymphocytes_pct, monocytes_pct, eosinophils_pct, basophils_pct}`
- `abnormal_flags`

## Rules
- Capture numeric values only when explicitly stated on that parameter's row; keep missing values `null` and missing abnormal flags `[]`.
- `reference_range` is the raw text from the source row, not a reconstructed string — the source PDF itself is inconsistently formatted (`"0 - 6"` next to `"13.0 - 17.0"`), and reformatting it would silently change what was actually printed on the report.
- `confidence` is a fixed constant (`0.98`) naming the extraction methodology (exact keyword match against a clearly labeled row) — it is never a real per-value ML confidence score, and must not be presented as one.
- `neutropenia` always stays `null`: it requires an absolute neutrophil count (ANC) compared against an external clinical threshold; a neutrophil *percentage* alone cannot determine it, and no ANC reference range appears in the source report.
- `hematological_assessment.severity` only distinguishes `"none"` vs `"abnormal"` — grading true clinical severity (mild/moderate/severe) requires domain thresholds this pipeline does not have, so it is never invented.
- Do not infer normality from the absence of a flag, and do not carry a value or status from one visit's report into another's.

## Output
- Standalone: `reports/cbc/CBC_consolidated_summary.json`
- Integrated section: written as part of `reports/Digital_Twin_Integrated_Report.json` when the full harness or agent runtime runs.

## Verification
- `python -m json.tool reports/cbc/CBC_consolidated_summary.json` must succeed.
- Confirm `observation_window.number_of_reports` equals `processing_manifest.processed_count`.
- Confirm every `cbc_parameters[key].reference_range` matches the raw text on that PDF's row exactly.
- Confirm `longitudinal_trends[key].first_value`/`latest_value` match the earliest/latest dated report's value for that parameter.
- Validate against `schemas/cbc.schema.json` for the integrated section; check units before merging: g/dL, percent, million/uL, 10e3/uL, fL, pg.
- Confirm no generated CBC summary is written anywhere under `patient_data/`; outputs only go to `reports/`.

## Reference Example
The shape above matches this reference report (trimmed to one report and one trend):

```json
{
  "report_label": "Merged Prime Agent CBC Longitudinal Summary",
  "patient_profile": { "patient_name": "Jordan A. Sample (SYNTHETIC PATIENT)", "patient_id": "SYN-100482", "age": 34, "sex": "M" },
  "patient_id": "SYN-100482",
  "observation_window": { "start_date": "2026-01-01", "end_date": "2026-06-30", "number_of_reports": 12, "source_folder": "patient_data/CBC" },
  "cbc_reports": [
    {
      "report_date": "2026-01-01",
      "report_metadata": { "report_date": "2026-01-01", "sample_type": "Blood", "laboratory": "Fictional Reference Pathology Laboratory (Synthetic Dataset)" },
      "cbc_parameters": {
        "hemoglobin": { "parameter": "Hemoglobin (Hb)", "value": 14.4, "unit": "g/dL", "reference_range": "13.0 - 17.0", "reference_low": 13.0, "reference_high": 17.0, "status": "normal", "confidence": 0.98, "source_page": 1 }
      },
      "abnormal_findings": [],
      "hematological_assessment": {
        "overall_status": "within_reference_ranges",
        "red_cell_assessment": { "status": "within_reference_ranges", "abnormal_parameters": [], "basis": "All extracted lineage parameters are within report-provided reference ranges." },
        "severity": "none",
        "summary": "Red cell, white cell, and platelet parameters are within the report-provided reference ranges."
      },
      "overall_cbc_impression": "No CBC parameter abnormality identified based on extracted values and report reference ranges.",
      "clinical_flags": { "anemia": false, "polycythemia": false, "leukocytosis": false, "leukopenia": false, "neutropenia": null, "thrombocytopenia": false, "thrombocytosis": false },
      "risk_assessment": {
        "overall_risk": "no_cbc_abnormality_detected",
        "basis": "CBC-specific assessment based only on extracted values and report-provided reference ranges; overall patient risk is not determined from CBC alone.",
        "supporting_parameters": [{ "parameter": "Hemoglobin (Hb)", "value": 14.4, "unit": "g/dL", "reference_range": "13.0 - 17.0", "status": "normal", "source_page": 1, "confidence": 0.98 }]
      },
      "digital_twin_health_state": { "red_cell_status": "within_reference_ranges", "white_cell_status": "within_reference_ranges", "platelet_status": "within_reference_ranges", "overall_hematological_status": "stable_within_reference_ranges", "active_flags": [] }
    }
  ],
  "longitudinal_trends": {
    "hemoglobin": {
      "parameter": "Hemoglobin (Hb)",
      "first_value": 14.4, "latest_value": 15.1, "minimum_value": 13.5, "maximum_value": 15.9,
      "trend_direction": "increasing", "abnormal_dates": []
    }
  },
  "abnormal_findings_over_time": [],
  "digital_twin_state": {
    "overall_hematological_status": "stable_within_reference_ranges",
    "active_flags_by_date": { "2026-01-01": [] },
    "unsupported_clinical_risk": "Overall patient risk is not determined from CBC values alone; this summary only reports CBC-specific abnormalities against report-provided reference ranges."
  },
  "executive_summary": {
    "clinical_conclusion": "12 CBC report(s) from 2026-01-01 to 2026-06-30 were processed. No CBC parameter abnormality was identified against the report-provided reference ranges.",
    "basis": "Generated from extracted CBC PDF values, units, source pages, and report-provided reference ranges. No reference report values were copied."
  },
  "processing_manifest": { "source_type": "pdf", "detected_count": 12, "processed_count": 12, "failed_count": 0, "skipped_count": 0, "status": "complete", "confidence": 1.0 }
}
```
