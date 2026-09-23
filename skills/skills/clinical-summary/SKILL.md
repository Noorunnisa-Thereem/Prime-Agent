---
name: clinical-summary
description: Generate clinical-note AI Coding Agent summaries from patient clinical files using fixed clinical summary schemas and source-backed values only.
---

# clinical-summary

## Required JSON Schemas

```json
{
    "clinical_notes":  {
                           "patient_profile":  {

                                               },
                           "observation_window":  {

                                                  },
                           "longitudinal_summary":  {

                                                    },
                           "clinical_inference":  {

                                                  },
                           "major_clinical_events":  [

                                                     ],
                           "digital_twin_state":  {

                                                  },
                           "executive_summary":  {

                                                 }
                       },
    "clinical_file":  {
                          "patient_profile":  {

                                              },
                          "observation_window":  {

                                                 },
                          "longitudinal_summary":  {

                                                   },
                          "clinical_inference":  {

                                                 },
                          "major_clinical_events":  [

                                                    ],
                          "digital_twin_state":  {

                                                 },
                          "executive_summary":  {

                                                }
                      }
}
```

## Module Identification

Select this module when a folder name, file name, CLI report type, or request contains `Clinical_Notes`, `clinical notes`, `clinical-summary`, visit notes, handwritten notes, or printed clinical PDFs. Folder automation must load this `SKILL.md` before extracting clinical files.

## Supported File Formats

Clinical note PDFs and supported clinical summary JSON files.

## Extraction Workflow

For folder processing, detect every supported clinical note PDF, parse each visit, preserve patient ID, visit/report date, extracted diagnoses, medications, seizure counts, seizure durations, clustering, aura symptoms, side effects, sleep, cognition, labs, quality-of-life fields, source filename, and confidence internally during extraction, then merge all visits into one chronological `clinical_notes` payload. Do not hardcode visit counts.

## Model, OCR, And Evidence Instructions

Use deterministic PDF text extraction for printed notes. Use OCR/VLM only for handwritten, scanned, or low-text pages, and require the model output to cite the source file/page or extracted text span used for each clinical field. Use a language model only to normalize visit text, classify clinical events, and write concise source-grounded summaries. Never ask a model to invent diagnoses, seizure counts, medications, dates, lab values, or patient identifiers.

Clinical interpretation must be supported by extracted visit-level evidence, CBC/questionnaire support only when those module outputs are explicitly available, or deterministic calculations from visits.

## Validation Rules

Use only the schema keys shown in this SKILL.md for this module. Validate the generated payload before writing. Do not add fields after `executive_summary.clinical_conclusion` in the final clinical-notes JSON. Confirm every detected clinical note PDF is parsed into a visit or recorded as a failed file by the caller.

## Consolidation Rules

Sort visits by `visit_date`. Derive `observation_window.total_visits`, disease trajectory, seizure analysis, major clinical events, state vector, risk prediction, monitoring priorities, and recommended actions from visit records only. Do not copy clinical values from `Clinical_summary_for each_data` reference/example JSON.

## Missing-Value Handling

Use `null` for unsupported scalar values, `{}` for unsupported object sections, and `[]` for unsupported lists. Do not copy values from mentor/reference/example JSON files.

## Summary Generation

Generate factual values from source patient files, parsed metadata, OCR/model extraction, and deterministic calculations. Use the model only for supported interpretation, trends, risk assessment, and concise summaries grounded in extracted evidence. Folder automation writes `reports\clinical_notes\AI_coding_agent_clinical_notes_consolidated_summary.json`.

## Error Handling

If no clinical-note files are detected, skip the module with `No supported input files detected.` If any detected file cannot be parsed or validated, fail the module with the failed path and error. If a clinical concept is not present in source notes, use the missing-value policy instead of inferred clinical claims.
