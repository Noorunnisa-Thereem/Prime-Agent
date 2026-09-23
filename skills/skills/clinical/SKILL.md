---
name: clinical
description: Generate clinical-note Digital Twin summaries from Clinical_Notes folders, printed PDFs, handwritten PDFs, and visit-note files using source-backed values only.
---

# clinical

## Required JSON Schemas

```json
{
  "clinical_notes": {
    "patient_profile": {},
    "observation_window": {},
    "longitudinal_summary": {},
    "clinical_inference": {},
    "major_clinical_events": [],
    "digital_twin_state": {},
    "executive_summary": {}
  },
  "clinical_file": {
    "patient_profile": {},
    "observation_window": {},
    "longitudinal_summary": {},
    "clinical_inference": {},
    "major_clinical_events": [],
    "digital_twin_state": {},
    "executive_summary": {}
  }
}
```

## Module Identification

Select this module when the folder or request refers to `Clinical_Notes`, `Clinical`, clinical notes, visit notes, printed clinical PDFs, or handwritten clinical PDFs. Folder automation must load this `SKILL.md` before extracting clinical files.

## Supported Files

Clinical note PDFs, printed visit-note PDFs, handwritten visit-note PDFs, and source clinical-summary JSON only when used as input data rather than a mentor/example value source.

## Extraction Workflow

Detect every clinical note file, extract printed text first, run OCR/VLM for handwritten or scanned pages, parse one visit record per visit date, preserve source file/page evidence, normalize diagnoses, medications, seizure burden, aura, side effects, sleep, cognition, labs, and quality-of-life fields, then consolidate visits chronologically into one `clinical_notes` JSON.

## Model, OCR, And VLM Rules

Use OCR/VLM only to read source files that deterministic text extraction cannot read. Require model output to be grounded in extracted text or visible page evidence. Use model reasoning only for normalization, event classification, longitudinal interpretation, and concise summaries. Never copy patient values from reference/example JSON files and never invent absent clinical facts.

## Validation Rules

Validate the final report against `clinical_notes`. Keep only the required schema keys. Confirm all detected files are processed or reported as failures. Keep unsupported scalar values as `null`, unsupported objects as `{}`, and unsupported lists as `[]`.

## Consolidation Rules

Sort visits by date. Derive counts, trends, highest/lowest seizure burden, monitoring priorities, and risk language from parsed visits and available module outputs only. The consolidated output path is `reports\clinical_notes\AI_coding_agent_clinical_notes_consolidated_summary.json`.

## Error Handling

Skip the module when no clinical files are found. Fail the module when a detected file cannot be read, parsed, or validated, and include the source path and error. Do not write a partial clinical report unless failed files are explicitly represented by the caller.
