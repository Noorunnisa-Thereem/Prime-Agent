---
name: genetics
description: Generate genetics and pharmacogenomic clinical summaries when genetics_data.xlsx or genetics workbook files are detected; use clinical-reference keys with values extracted only from source genetics files.
---

# genetics

## Purpose

Generate the pharmacogenomic clinical summary from actual genetics workbook data. The key structure follows `data\raw\DT_Data_2\DT_Data\Clinical_summary_for each_data\genetics_clinical_summary.json`, but values must be generated from `Genetics_data\genetics_data.xlsx` and the project extraction pipeline, not copied from the reference JSON.

## When to Use

Use this skill when the report type is Genetics, Pharmacogenomics, PGx, gene-drug summary, or when a folder contains `genetics_data.xlsx`.

## Required JSON Schemas

```json
{
    "genetics":  {
                     "report_label":  null,
                     "patient_id":  null,
                     "record_count":  null,
                     "pharmacogenomic_records":  [

                                                 ],
                     "overall_summary":  {

                                         },
                     "processing_manifest":  {

                                           }
                 },
    "pharmacogenomic_clinical_summary":  {
                                             "report_type":  null,
                                             "patient":  {

                                                               },
                                             "purpose":  null,
                                             "metabolizer_profile":  [

                                                                     ],
                                             "findings_by_therapeutic_class":  {

                                                                              },
                                             "priority_safety_flags":  [

                                                                       ],
                                             "boxed_warning_note":  null,
                                             "clinical_conclusion":  {

                                                                     }
                                         }
}
```

## Supported File Formats

XLSX genetics/pharmacogenomic workbooks, especially `Genetics_data\genetics_data.xlsx`, and generated genetics records JSON used as an intermediate audit file.

## Model, OCR, VLM, And Evidence Instructions

Use deterministic workbook parsing for gene, variant, genotype, phenotype, drug, disease, response, recommendation, CPIC, FDA, publication, and source-row fields. OCR/VLM is not applicable for `.xlsx` workbooks. Use a model only to group findings by therapeutic class, prioritize safety flags, and write concise source-grounded clinical decision-support language. Never invent gene-drug associations, metabolizer statuses, warnings, current medications, or patient identifiers.

Every summary claim must trace to workbook rows, deduplicated records, deterministic counts, or explicitly available source guidance fields.

## Extraction Workflow

For folder processing, detect every supported genetics/pharmacogenomic workbook, read all rows in each workbook, normalize gene, variant, genotype, phenotype, drug, disease, response, recommendation, CPIC, FDA, and publication fields, deduplicate records, then generate one clinical-summary JSON using the `pharmacogenomic_clinical_summary` keys. Save detailed extracted rows separately as a records report so source evidence is not lost. Do not hardcode workbook or row counts.

## Validation Rules

Use only the schema keys shown in this SKILL.md for the main clinical genetics report. Validate the row-level records first, then validate the clinical summary. The final clinical report must not add `processing_manifest` after `clinical_conclusion`; workbook source details belong in the separate records report. Do not silently exclude workbooks.

## Consolidation Rules

Merge all workbook rows, deduplicate by stable gene/variant/genotype/drug/effect/source fields, group findings by therapeutic class, derive priority safety flags from toxicity/adverse-event/reduced-response evidence, and compute clinical-conclusion counts from the deduplicated source records. Do not copy values from `genetics_clinical_summary.json` or any mentor/reference/example JSON.

## Missing-Value Handling

Use `null` for unsupported scalar values, `{}` for unsupported object sections, and `[]` for unsupported lists. Do not copy values from mentor/reference/example JSON files.

## Summary Generation

Generate factual values from source workbook rows, parsed metadata, and deterministic grouping/counting. Use the model only for supported interpretation, risk prioritization, and concise summaries grounded in extracted gene-drug evidence. Folder automation writes the main clinical summary to `reports\genetics\AI_coding_agent_pharmacogenomic_summary.json` and the detailed audit records to `reports\genetics\*_pharmacogenomic_records_report.json`.

## Error Handling

If no genetics workbook is detected, skip the module. If any workbook cannot be opened, parsed, normalized, or validated, fail the module with the source path and error. If a workbook lacks optional fields, preserve available evidence and use the missing-value policy for unsupported fields.
