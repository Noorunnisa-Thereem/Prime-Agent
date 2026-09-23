---
name: cbc
description: Generate CBC AI Coding Agent reports from CBC PDFs using fixed CBC schemas and source-backed values only.
---

# cbc

## Required JSON Schemas

```json
{
    "cbc":  {
                "report_label":  null,
                "patient_profile":  {

                                    },
                "report_metadata":  {

                                    },
                "cbc_parameters":  {
                                       "__preserve_extra__":  true
                                   },
                "abnormal_findings":  [

                                      ],
                "hematological_assessment":  {

                                             },
                "overall_cbc_impression":  null,
                "clinical_flags":  {

                                   },
                "clinical_flag_basis":  {

                                        },
                "risk_assessment":  {

                                    },
                "digital_twin_health_state":  {

                                              },
                "monitoring_recommendations":  [

                                               ],
                "longitudinal_trend":  {

                                       },
                "confidence":  {

                               }
            },
    "cbc_folder":  {
                       "report_label":  null,
                       "patient_profile":  {

                                           },
                       "patient_id":  null,
                       "observation_window":  {

                                              },
                       "cbc_reports":  [

                                       ],
                       "longitudinal_trends":  {

                                               },
                       "abnormal_findings_over_time":  [

                                                       ],
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

Select this module when a folder name, file name, CLI report type, or user request contains `CBC`, `CBC_Synthesis`, `complete blood count`, or CBC PDF reports. Folder automation must load this `SKILL.md` before extracting CBC files.

## Supported File Formats

PDF CBC reports.

## Extraction Workflow

For folder processing, detect every supported CBC PDF in the module folder, extract each file with text extraction first and OCR fallback when text is absent or incomplete, preserve patient ID, report date, source filename, page evidence, parameter confidence, extracted CBC values, units, reference ranges, and status, then merge all reports into one chronological `cbc_folder` payload. Do not hardcode report counts.

## Model, OCR, And Evidence Instructions

Use deterministic PDF text extraction before OCR. Use OCR only for scanned pages or missing table text, and include page/source evidence when available. Use a model only to normalize messy text into fields, summarize trends, or write concise impressions grounded in extracted values. Never ask a model to invent CBC values, reference ranges, dates, or patient identifiers.

Every parameter value in `cbc_parameters` must be traceable to one detected PDF, source page/text evidence, or deterministic calculation. Every longitudinal trend must be derived from the extracted records.

## Validation Rules

Use only the schema keys shown in this SKILL.md for this module. Validate the generated payload before writing. Confirm every detected CBC PDF either appears in `processing_manifest.processed_files` or in `processing_manifest.failed_files`; do not silently exclude files. Reject or mark failed any PDF whose report date or required table values cannot be extracted with evidence.

## Consolidation Rules

Sort reports by `report_metadata.report_date`. Compute `observation_window`, `longitudinal_trends`, `abnormal_findings_over_time`, and `digital_twin_state` from the extracted report list. Do not copy CBC conclusions or patient values from reference/example JSON.

## Missing-Value Handling

Use `null` for unsupported scalar values, `{}` for unsupported object sections, and `[]` for unsupported lists. Do not copy values from mentor/reference/example JSON files.

## Summary Generation

Generate factual values from source patient files, parsed metadata, OCR/model extraction, and deterministic calculations. Use the model only for supported interpretation, trends, risk assessment, and concise summaries grounded in extracted evidence. Folder automation writes `reports\CBC\AI_coding_agent_cbc_consolidated_summary.json`.

## Error Handling

If no CBC PDFs are detected, skip the module with `No supported input files detected.` If any detected PDF fails extraction or validation, fail the CBC module with the failed file path and error. Do not write a partial consolidated CBC report unless failed files are explicitly recorded in `processing_manifest.failed_files`.
