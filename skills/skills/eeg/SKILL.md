---
name: eeg
description: Generate EEG AI Coding Agent clinical summaries from ictal, pre_ictal/preictal, and interictal MAT EEG folders using EEG_clinical_summary.json only as the key/format reference and source-backed values only.
---

# eeg

## Required JSON Schemas

```json
{
    "eeg_record":  {
                       "file_name":  null,
                       "patient_id":  null,
                       "phase":  null,
                       "modality":  null,
                       "signal_variable":  null,
                       "shape":  [

                                 ],
                       "dimensions":  null,
                       "channels":  null,
                       "samples":  null,
                       "global_statistics":  {

                                             },
                       "channel_statistics":  [

                                              ]
                   },
    "eeg_clinical_summary":  {
                                 "report_metadata":  {},
                                 "recording_statistics":  {},
                                 "clinical_findings":  {},
                                 "signal_statistics":  {},
                                 "phase_analysis":  {},
                                 "longitudinal_analysis":  {},
                                 "derived_features":  {},
                                 "key_biomarkers":  {},
                                 "digital_twin_state":  {},
                                 "digital_twin_features":  {},
                                 "recommended_monitoring":  [],
                                 "overall_observation":  {}
                             }
}
```

## Supported File Formats

MAT EEG signal files for ictal, pre_ictal/preictal, and interictal phases. Use `data\raw\DT_Data_2\DT_Data\Clinical_summary_for each_data\EEG_clinical_summary.json` only as the final clinical-summary key/format reference; never copy its values. The raw record shape from `data\raw\DT_Data_2\DT_Data\EEG\EEG_summary.json` may be used only for per-MAT intermediate records.

## Model, OCR, VLM, And Evidence Instructions

Use deterministic MAT parsing for signal arrays, phase labels, channel counts, sample counts, amplitude ranges, standard deviation, variance, and channel statistics. OCR/VLM is not applicable to MAT signal extraction unless future source EEG report images are provided. Use model text only to describe source-backed phase patterns, longitudinal trends, monitoring priorities, and limitations. Do not invent seizure events, localization, diagnosis, or recovery findings beyond the phase labels and calculated signal statistics.

Every EEG clinical value must be traceable to a MAT file, phase folder, preprocessing JSON, deterministic calculation, or explicitly available model output.

## Extraction Workflow

For folder processing, detect every supported EEG MAT file in the `ictal`, `pre_ictal`/`preictal`, and `interictal` folders. Calculate per-file signal statistics from each MAT file, then generate one `eeg_clinical_summary` object with the same top-level keys as `EEG_clinical_summary.json`. Do not hardcode file counts.

## Validation Rules

Use only the schema keys shown in this SKILL.md for this module. Validate each generated EEG record against `eeg_record`, then validate the final report against `eeg_clinical_summary`. Do not add wrapper fields such as `records` or `processing_manifest` to the final EEG clinical summary JSON.

## Consolidation Rules

Group records by interictal, preictal, and ictal phase. Compute recording counts, phase statistics, variance/amplitude trends, dominant discriminative features, digital twin state, monitoring recommendations, and overall observation from calculated phase summaries only. Preserve raw-record evidence separately when needed; do not change the final clinical-summary schema.

## Missing-Value Handling

Use `null` for unsupported scalar values, `{}` for unsupported object sections, and `[]` for unsupported lists. Do not copy values from mentor/reference/example JSON files.

## Summary Generation

Generate factual values from the MAT source files and deterministic calculations. Use model/text generation only for concise supported interpretation based on calculated EEG phase statistics. Folder automation writes `reports\EEG\AI_coding_agent_eeg_consolidated_summary.json` as one consolidated EEG clinical-summary object. Also keep a separate raw-record sidecar JSON in the EEG reports folder when needed for traceability.

## Error Handling

If no EEG MAT files are detected, skip the module. If any detected MAT file cannot be read or validated, fail the module with the source path and error. If a phase is absent, report zero counts and avoid unsupported phase comparisons.
