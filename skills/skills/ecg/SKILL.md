---
name: ecg
description: Generate ECG clinical summaries when the requested report type is ECG or ECG_Clinical_Summary, using ECG EDF/preprocessing files for values and the ECG clinical-summary template only for keys.
---

# ECG

## Purpose

Generate ECG AI Coding Agent clinical summaries in the same JSON structure as `Clinical_summary_for each_data/ECG_Clinical_Summary.json`. Use that file only as the key/format reference. Do not copy its example values.

## When to Use

Use this skill when the user asks to create, update, validate, or debug an ECG summary, ECG clinical summary, ECG generated report, or ECG consolidated summary.

## Supported Inputs

- EDF waveform files from the ECG source folder.
- ECG preprocessing JSON files containing channel metadata, statistics, percentiles, sample counts, sampling rate, duration, and units.
- `ECG_summary.json` lists of preprocessed ECG records.
- Optional ECG report images only when available as source evidence.

## Model, OCR, VLM, And Evidence Instructions

Use deterministic JSON/EDF parsing for sampling rate, channels, samples, durations, amplitude statistics, percentiles, dates, and times. Use OCR/VLM only if ECG report images are provided and a field cannot be extracted from JSON/EDF. Use model text only for concise overview, notable observations, and limitations grounded in extracted signal metadata. Do not infer rhythm diagnosis, intervals, ischemia, arrhythmia, or clinical ECG interpretation unless a source report/model output explicitly supports it.

Every entry in `recording_log`, `amplitude_statistics_uV`, and `percentile_distribution_uV` must map to a source preprocessing JSON or EDF record.

## Required JSON Schema

```json
{
  "ecg_patient_record": {
    "file_name": null,
    "patient_id": null,
    "modality": null,
    "number_of_channels": null,
    "channels": []
  },
  "ecg_generated_report": {
    "title": null,
    "reporting_period": {
      "start_date": null,
      "end_date": null
    },
    "generated": null,
    "overview": [],
    "dataset_summary": {
      "number_of_recordings": null,
      "date_range": null,
      "total_recorded_duration": null,
      "channel_configuration": null,
      "sampling_rate_hz": null,
      "amplitude_units": null
    },
    "recording_log": [
      {
        "date": null,
        "time": null,
        "duration": null,
        "samples": null,
        "sampling_rate_hz": null
      }
    ],
    "amplitude_statistics_uV": [
      {
        "date": null,
        "min": null,
        "max": null,
        "mean": null,
        "median": null,
        "std_dev": null
      }
    ],
    "percentile_distribution_uV": [
      {
        "date": null,
        "p5": null,
        "p25": null,
        "p75": null,
        "p95": null
      }
    ],
    "notable_observations": [],
    "scope_and_limitations": null
  }
}
```

## Workflow

1. Detect ECG report requests from the report type, input folder, or output target.
2. Load `ECG_summary.json` or ECG preprocessing JSON records from the source patient files.
3. Match each record to the corresponding EDF file by file name when EDF data is available.
4. Extract study dates and times from ECG file names.
5. Calculate durations, sample counts, sampling rate, channel configuration, amplitude statistics, and percentile distributions from source ECG/preprocessing data.
6. Generate only supported overview, notable observations, and limitations from extracted values.
7. Validate the payload against `ecg_generated_report` before writing.

## Consolidation Rules

Sort ECG recordings by date and time. Compute `reporting_period`, `dataset_summary.number_of_recordings`, `dataset_summary.date_range`, and `dataset_summary.total_recorded_duration` from detected records. Generate `notable_observations` from observed ranges, missing files, variability, and data-quality limitations only.

## Validation Rules

- Keep the `ecg_generated_report` keys exactly as shown above.
- Do not add `patient_id`, `source_evidence`, ECG interpretation, impression, diagnosis, interval, axis, or rhythm fields to `ecg_generated_report`.
- Do not add any fields after `scope_and_limitations`.
- Do not copy values, sentences, dates, counts, or summaries from `ECG_Clinical_Summary.json`.
- Use source ECG files and preprocessing data for all factual values.
- Use deterministic calculations for counts, date ranges, duration totals, amplitude statistics, and percentiles.
- Use model text only for concise source-supported overview, observations, and limitations.

## Error Handling

- Raise a clear error if the ECG summary source is not a JSON list.
- Raise a clear error if no ECG records are found.
- Raise a clear error if an expected EDF file is missing for a listed ECG record.
- If a scalar value is unsupported by the source files, return `null`.
- If a list has no supported entries, return `[]`.
- If an object section has no supported values, return `{}` only when the schema allows an object placeholder.

## Output

Write valid JSON only. The ECG consolidated summary should use the `ecg_generated_report` structure and should be generated from the patient's ECG source files, not from mentor/reference example values. Folder automation writes `reports\ECG\AI_coding_agent_ecg_consolidated_summary.json`.
