---
name: integrated-report
description: Generate final integrated AI Coding Agent reports by combining module outputs using the fixed integrated schema and source-backed module values only.
---

# integrated-report

## Required JSON Schemas

```json
{
    "integrated_digital_twin":  {
                                    "ai_coding_agent":  {
                                                            "version":  null,
                                                            "patient_id":  null,
                                                            "generated_at":  null,
                                                            "source_folder":  null,
                                                            "processing_mode":  null,
                                                            "module_output_files":  {

                                                                                    },
                                                            "module_skill_files":  {

                                                                                   },
                                                            "skipped_modules":  {

                                                                                },
                                                            "sections":  {
                                                                             "MRI_clinical_summary":  {

                                                                                                      },
                                                                             "ECG_Clinical_Summary":  {

                                                                                                      },
                                                                             "CT_scan_clinical_summary":  {

                                                                                                          },
                                                                             "EEG_clinical_summary":  {

                                                                                                      },
                                                                             "clinical__notes_summary":  {

                                                                                                         },
                                                                             "Pharmacogenomic_clinical_summary":  {

                                                                                                                  },
                                                                             "CBC_summary":  {

                                                                                             },
                                                                             "questionnaire_summary":  {

                                                                                                       }
                                                                         },
                                                            "scope_and_limitations":  null
                                                        }
                                }
}
```

## Supported File Formats

Generated module JSON files from CBC, CT, MRI, ECG, EEG, clinical notes, genetics, and questionnaires.

## Supported Files

Validated module JSON files from `reports\CBC`, `reports\CT`, `reports\MRI`, `reports\ECG`, `reports\EEG`, `reports\clinical_notes`, `reports\genetics`, and `reports\questionnaires`.

## Extraction Workflow

Read only validated module outputs generated in the current processing run. Record the loaded module skill files in `module_skill_files`, map module payloads into integrated sections, include skipped modules and output paths, do not invent missing sections, validate before writing.

## Validation Rules

Use only the schema keys shown in this SKILL.md for this module. Validate the generated payload before writing. Do not add, remove, or rename keys unless the schema in this file is intentionally updated. Confirm every included section comes from a current module output file, not from `Clinical_summary_for each_data` reference/example files.

## Evidence Requirements

Each integrated section must be copied from the corresponding generated module JSON payload for the same run. `module_output_files` must contain the generated file path for every included module. `module_skill_files` must contain the loaded module `SKILL.md` path for every attempted module.

## Missing-Value Handling

Use `null` for unsupported scalar values, `{}` for unsupported object sections, and `[]` for unsupported lists. Do not copy values from mentor/reference/example JSON files.

## Summary Generation

Generate factual values from source patient files, parsed metadata, OCR/model extraction, and deterministic calculations. Use the model only for supported interpretation, trends, risk assessment, and concise summaries grounded in extracted evidence.

## Error Handling

If a module is missing supported files, include its reason in `skipped_modules`. If a module output fails JSON parsing or schema validation, do not include it in `sections`; report the error in `skipped_modules`. Never copy patient values from mentor/reference/example JSON files to fill missing integrated fields.
