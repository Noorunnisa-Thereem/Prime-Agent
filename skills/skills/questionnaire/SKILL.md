---
name: questionnaire
description: Generate questionnaire AI Coding Agent reports from questionnaire PDFs using fixed questionnaire schemas and source-backed values only.
---

# questionnaire

## Required JSON Schemas

```json
{
    "questionnaire":  {
                          "patient_profile":  {

                                              },
                          "domain_scores":  {

                                            },
                          "seizure_summary":  {

                                              },
                          "aura_summary":  {

                                           },
                          "medication_summary":  {

                                                 },
                          "sleep_summary":  {

                                            },
                          "trigger_summary":  {

                                              },
                          "cognitive_summary":  {

                                                },
                          "laboratory_lifestyle_summary":  {

                                                           },
                          "clinical_assessment":  {

                                                  },
                          "digital_twin_state":  {

                                                 }
                      },
    "questionnaire_folder":  {
                                 "report_label":  null,
                                 "patient_profile":  {

                                                     },
                                 "observation_window":  {

                                                        },
                                 "domain_score_trends":  {

                                                         },
                                 "latest_domain_summaries":  {

                                                             },
                                 "recurring_patient_reported_findings":  {

                                                                         },
                                 "digital_twin_state":  {

                                                        },
                                 "executive_summary":  {

                                                       },
                                 "processing_manifest":  {

                                                       }
                             }
}
```

## Module Identification

Select this module when a folder name, file name, CLI report type, or request contains `Questionnaire`, `Questionnaires`, patient-reported outcomes, survey forms, or questionnaire PDFs. Folder automation must load this `SKILL.md` before extracting questionnaire files.

## Supported File Formats

Questionnaire PDF files.

## Extraction Workflow

For folder processing, detect every supported questionnaire PDF, extract each file with text extraction first and OCR fallback when text is absent or incomplete, preserve patient ID, assessment/report date, source filename, page evidence where available, extracted response values, domain scores, status labels, and confidence, then merge all questionnaires into one chronological `questionnaire_folder` payload. Do not hardcode file counts.

## Model, OCR, And Evidence Instructions

Use deterministic PDF text extraction before OCR. Use OCR only for scanned pages or missing form/table text. Use a model only to normalize extracted answers, group them into supported questionnaire domains, and generate concise source-grounded summaries. Never ask a model to invent questionnaire responses, dates, scores, patient medications, or seizure-history values.

Each domain score and clinically relevant response must be traceable to a source questionnaire or a deterministic calculation from extracted answers.

## Validation Rules

Use only the schema keys shown in this SKILL.md for this module. Validate the generated payload before writing. Confirm every detected questionnaire file appears in `processing_manifest.processed_files` or `processing_manifest.failed_files`; do not silently exclude files. Reject or mark failed any questionnaire whose date or answer set cannot be extracted with evidence.

## Consolidation Rules

Sort questionnaire reports by assessment date. Compute `observation_window`, `domain_score_trends`, `latest_domain_summaries`, `recurring_patient_reported_findings`, and `digital_twin_state` from extracted questionnaires only. Do not copy questionnaire values from reference/example JSON.

## Missing-Value Handling

Use `null` for unsupported scalar values, `{}` for unsupported object sections, and `[]` for unsupported lists. Do not copy values from mentor/reference/example JSON files.

## Summary Generation

Generate factual values from source patient files, parsed metadata, OCR/model extraction, and deterministic calculations. Use the model only for supported interpretation, trends, risk assessment, and concise summaries grounded in extracted evidence. Folder automation writes `reports\questionnaires\AI_coding_agent_questionnaire_consolidated_summary.json`.

## Error Handling

If no questionnaire PDFs are detected, skip the module with `No supported input files detected.` If any detected questionnaire fails extraction or validation, fail the questionnaire module with the failed file path and error. Do not silently drop questionnaires from consolidation.
