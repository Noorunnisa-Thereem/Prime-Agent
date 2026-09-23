---
name: mri
description: Generate MRI clinical summaries when MRI_summary.json, MRI preprocessing JSON, or NIfTI image files are detected; use the MRI clinical-summary keys and source-backed values only.
---

# mri

## Purpose

Generate the MRI Digital Twin clinical summary from actual MRI source files. The clinical-summary key structure follows `data\raw\DT_Data_2\DT_Data\Clinical_summary_for each_data\MRI_clinical_summary.json`, but values must be generated from MRI files and calculations, not copied from that reference.

## When to Use

Use this skill when the report type is MRI, when an MRI folder is detected, or when the user asks to generate an MRI clinical summary, MRI twin report, MRI consolidated summary, or MRI NIfTI report.

## Required JSON Schemas

```json
{
    "mri_patient_record":  {
                               "file_name":  null,
                               "patient_id":  null,
                               "scan_type":  null,
                               "modality":  null,
                               "shape":  [

                                         ],
                               "number_of_dimensions":  null,
                               "voxel_spacing_mm":  [

                                                    ],
                               "datatype":  null,
                               "affine_matrix":  [

                                                 ],
                               "statistics":  {

                                              },
                               "intensity_percentiles":  {

                                                         },
                               "brain_volume":  {

                                                },
                               "histogram":  {

                                             }
                           },
    "mri_clinical_summary":  {
                                 "report_type":  null,
                                 "patient_id":  null,
                                 "observation_period":  null,
                                 "mri_summary":  {

                                                 },
                                 "imaging_details":  {

                                                     },
                                 "structural_findings":  {

                                                         },
                                 "longitudinal_assessment":  {

                                                             },
                                 "clinical_inference":  {

                                                        },
                                 "recommendations":  {

                                                     },
                                 "conclusion":  {

                                                }
                             }
}
```

## Supported File Formats

NIfTI MRI files (`.nii`, `.nii.gz`), `MRI_summary.json`, and MRI preprocessing JSON files.

## Model, OCR, VLM, And Evidence Instructions

Use deterministic NIfTI/preprocessing extraction for shape, affine matrix, voxel spacing, image dimensions, datatype, voxel intensity statistics, percentiles, nonzero-voxel volume, and histograms. Use a validated MRI-capable model/VLM only when configured and available; otherwise report that lesion-level or diagnostic radiology findings are not inferred. OCR is not applicable unless source MRI report PDFs/images are later added as evidence. Never copy MRI patient values, dates, findings, or conclusions from reference/example JSON files.

Every clinical-summary field must be grounded in detailed MRI records, preprocessing JSON, NIfTI-derived calculations, or explicit model output.

## Extraction Workflow

1. Detect `MRI_summary.json` and the matching MRI image folder.
2. For every supported MRI entry, find the matching NIfTI source file.
3. Calculate image-derived values from the NIfTI file, including shape, dimensions, voxel spacing, datatype, affine matrix, intensity statistics, percentiles, nonzero-voxel volume, and histogram.
4. Validate each detailed MRI record against `mri_patient_record`.
5. Build one clinical-summary object using only the `mri_clinical_summary` keys shown above.
6. Save the clinical summary as `reports\MRI\AI_coding_agent_mri_consolidated_summary.json`.
7. Save the detailed per-file MRI measurements separately as `reports\MRI\AI_coding_agent_mri_twin_records_report.json` so individual file evidence is preserved without changing the final clinical-summary schema.

## Validation Rules

Use only the schema keys shown in this SKILL.md for the final MRI clinical summary. The final report must not add `records`, `evidence`, or `processing_manifest` after `conclusion`. Validate every detailed MRI record and then validate the final clinical summary before writing. Do not silently exclude MRI entries; a missing matching NIfTI file must raise an error.

## Consolidation Rules

Sort MRI studies by source record order, acquisition date when available, or filename. Summarize modalities, coverage, spatial resolution, acquisition consistency, volume ranges, intensity ranges, longitudinal stability, limitations, recommendations, and conclusion from the detailed source-backed MRI records only. Unsupported clinical findings must be explicitly marked as not assessed or not determined.

## Missing-Value Handling

Use `null` for unsupported scalar values, `{}` for unsupported object sections, and `[]` for unsupported lists. Do not copy values from mentor/reference/example JSON files.

## Summary Generation

Generate factual values from source MRI files, parsed metadata, and deterministic calculations. Use the model only for supported interpretation, trends, risk assessment, and concise summaries grounded in extracted evidence.

## Error Handling

Return an error if `MRI_summary.json` is not a JSON list, if no MRI studies are found, or if any listed MRI study cannot be matched to a source NIfTI file. Missing unsupported clinical findings must be `null`, not invented.

## Output

Final MRI clinical summary: `reports\MRI\AI_coding_agent_mri_consolidated_summary.json`.

Detailed MRI twin records report: `reports\MRI\AI_coding_agent_mri_twin_records_report.json`.
