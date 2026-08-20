---
name: ecg
description: Use when asked to generate, regenerate, update, or validate the ECG Digital Twin signal-level statistical report — e.g. "generate the ECG report", "regenerate the ECG summary", "run ecg" — or when the ecg section of the integrated Digital Twin report needs to be extracted from ECG recordings in patient_data/ECG.
---

# ECG

## Purpose
Turn per-recording ECG signal statistics into two outputs:
1. A standalone, longitudinal signal-level statistical report — one entry per recording session.
2. The `ecg` section of the integrated `Digital_Twin_Integrated_Report.json`.

## Inputs
- Source file: `patient_data/ECG/ECG_summary.json` — a JSON array with one entry per recording (`file_name`, `patient_id`, `number_of_channels`, `channels[0].{sampling_rate_hz, duration_seconds, samples, physical_dimension, statistics, percentiles, histogram}`).
- Raw waveforms: `patient_data/ECG/ecg_<NN>_<YYYYMMDD>_<HHMMSS>.edf` — not parsed; `.edf` is not a supported extension in this pipeline, so all signal statistics come from `ECG_summary.json`, never from reading the waveform directly.
- Standalone generator: `patient_prime_agent/ecg_summary.py`
- Integrated extractor: `patient_prime_agent/extractors/electrophysiology.py` (`ECGExtractor`)
- Integrated schema: `schemas/ecg.schema.json`
- Standalone output: `reports/ecg/ECG_Clinical_Summary.json`

## Standalone Summary Workflow
1. Load `patient_data/ECG/ECG_summary.json`; parse each entry's `file_name` (`ecg_<NN>_<YYYYMMDD>_<HHMMSS>.edf`) for the recording date and time.
2. Sort recordings by date.
3. Build `dataset_summary` from the recordings: count, `date_range` (min date to max date), `total_recorded_duration` (sum of every `duration_seconds`, formatted `"<H>h <M>m"`), `channel_configuration` (only claim `'single channel ("<name>")  in every recording'` when every recording truly has exactly one channel with the same name — otherwise say configuration varies), `sampling_rate_hz` (only when uniform across all recordings, else `null`), `amplitude_units` (mapped from `physical_dimension`, e.g. `"uV"` -> `"microvolts (uV)"`).
4. Build `recording_log[]`: one `{date, time, duration, samples, sampling_rate_hz}` per recording, `duration` formatted the same way as `total_recorded_duration`.
5. Build `amplitude_statistics_uV[]`: one `{date, min, max, mean, median, std_dev}` per recording, taken directly from `channels[0].statistics` — `min`/`max` rounded to 1 decimal, `mean`/`median`/`std_dev` rounded to 2 decimals.
6. Build `percentile_distribution_uV[]`: one `{date, p5, p25, p75, p95}` per recording, taken from `channels[0].percentiles["5"/"25"/"75"/"95"]`, rounded to the nearest integer.
7. Compute `notable_observations[]` purely from the numbers above — never author a clinical claim:
   - channel/sampling-rate uniformity statement, only when actually uniform;
   - shortest vs. longest recording duration, with dates;
   - any pair of recordings whose `p5`/`p25`/`p75`/`p95` all match within `0.5 uV` — flag as near-identical/possible duplication;
   - lowest vs. highest standard deviation, with dates;
   - lowest vs. highest peak-to-peak amplitude (`max - min`), with dates.
8. `scope_and_limitations` is a fixed disclaimer: this is a statistical summary, not a clinical or diagnostic ECG interpretation.
9. Write the result to `reports/ecg/ECG_Clinical_Summary.json`.

Run:

```powershell
python -m patient_prime_agent.ecg_summary
```

## Canonical Output Shape
Every standalone report must use exactly these top-level keys — this is the reference shape (see the reproduced example at the bottom of this file):

| Key | Contents |
| --- | --- |
| `title` | fixed report title |
| `reporting_period` | `start_date`, `end_date` — min/max recording date |
| `generated` | the date the report was produced |
| `overview[]` | one descriptive paragraph naming the recording count, date span, and the fact that only amplitude-distribution statistics are covered |
| `dataset_summary` | `number_of_recordings`, `date_range`, `total_recorded_duration`, `channel_configuration`, `sampling_rate_hz`, `amplitude_units` |
| `recording_log[]` | one `{date, time, duration, samples, sampling_rate_hz}` per recording |
| `amplitude_statistics_uV[]` | one `{date, min, max, mean, median, std_dev}` per recording |
| `percentile_distribution_uV[]` | one `{date, p5, p25, p75, p95}` per recording |
| `notable_observations[]` | computed statistical observations (see workflow step 7) |
| `scope_and_limitations` | fixed disclaimer that this is not a clinical interpretation |

When `ECG_summary.json` is missing or has no parseable entries, every list is `[]`, every scalar in `dataset_summary`/`reporting_period` is `null` or `0`, and `scope_and_limitations` is still populated — never a partially fabricated version of the shape.

## Integrated Report Extraction
Populate only these schema fields for the `ecg` section of the integrated report:
- `study_date`
- `rate_bpm`
- `rhythm`
- `intervals_ms` (PR, QRS, QT, QTc)
- `axis`
- `interpretation`
- `notable_abnormalities`

`ECG_summary.json` contains only amplitude-distribution statistics, not rhythm, rate, or interval measurements — none of those integrated-report fields can be populated from it. Extract them only from a document that explicitly states them (e.g. a narrative ECG report), and leave them `null` otherwise.

## Rules
- All signal statistics come from `ECG_summary.json`; never parse or estimate values from the raw `.edf` waveform.
- Every number in `amplitude_statistics_uV`, `percentile_distribution_uV`, and `recording_log` must trace to an explicit field in the source entry for that date — never interpolate or estimate.
- `notable_observations` may only state facts computed from the statistics above (ranges, comparisons, near-duplicate detection) — never a rhythm, rate, or diagnostic claim.
- `channel_configuration` and `sampling_rate_hz` must only assert uniformity when it is actually true across every recording.
- Do not claim clinical or diagnostic conclusions anywhere in this report; `scope_and_limitations` must stay intact.

## Output
- Standalone: `reports/ecg/ECG_Clinical_Summary.json`
- Integrated section: written as part of `reports/Digital_Twin_Integrated_Report.json` when the full harness or agent runtime runs.

## Verification
- `python -m json.tool reports/ecg/ECG_Clinical_Summary.json` must succeed.
- Confirm `dataset_summary.number_of_recordings` equals the number of entries in `ECG_summary.json`.
- Confirm `reporting_period` matches the earliest/latest recording dates.
- Spot-check one `amplitude_statistics_uV` entry and one `percentile_distribution_uV` entry against the matching source `statistics`/`percentiles` object.
- Confirm `notable_observations` cites only dates and values that appear elsewhere in the same report.
- Confirm no generated ECG summary is written anywhere under `patient_data/`; outputs only go to `reports/`.

## Reference Example
The shape above matches this reference report (trimmed to two recordings):

```json
{
  "title": "ECG Digital Twin Dataset - Signal-Level Statistical Report",
  "reporting_period": { "start_date": "2026-01-03", "end_date": "2026-06-29" },
  "generated": "2026-07-11",
  "overview": [
    "This report summarizes the signal-level statistical characteristics of the ECG digital twin dataset, comprising 12 recording sessions captured between January and June 2026. Each entry in the source dataset represents a single-channel ECG waveform recorded from the digital twin data stream, described only in terms of amplitude distribution (minimum, maximum, mean, median, standard deviation, and percentiles)."
  ],
  "dataset_summary": {
    "number_of_recordings": 12,
    "date_range": "2026-01-03 to 2026-06-29",
    "total_recorded_duration": "99h 11m",
    "channel_configuration": "single channel (\"ECG SD\") in every recording",
    "sampling_rate_hz": 256,
    "amplitude_units": "microvolts (uV)"
  },
  "recording_log": [
    { "date": "2026-01-03", "time": "10:38:26", "duration": "3h 9m", "samples": 2913280, "sampling_rate_hz": 256.0 }
  ],
  "amplitude_statistics_uV": [
    { "date": "2026-01-03", "min": -1356.0, "max": 1398.0, "mean": -2.83, "median": -20.96, "std_dev": 82.58 }
  ],
  "percentile_distribution_uV": [
    { "date": "2026-01-03", "p5": -96.0, "p25": -34.0, "p75": 15.0, "p95": 128.0 }
  ],
  "notable_observations": [
    "All 12 recordings use a single channel sampled at 256 Hz, so statistics are directly comparable across sessions.",
    "Recording duration varies considerably, from about 1h 11m (2026-04-11) to about 22h 27m (2026-03-25).",
    "The 2026-06-02 and 2026-06-29 recordings have matching percentile values and near-identical histogram shapes, suggesting very similar signal characteristics or a data duplication between the two sessions.",
    "Standard deviation of amplitude ranges from about 82.6 uV (2026-01-03) to about 303.1 uV (2026-05-05), indicating substantially different signal variability/noise levels between sessions."
  ],
  "scope_and_limitations": "This report is derived strictly from the statistical fields present in the source dataset. It does not represent a clinical or diagnostic ECG interpretation. Any assessment of cardiac rhythm, heart rate, or abnormality requires review of the underlying waveform by a qualified clinician or an appropriately validated diagnostic algorithm, neither of which is reflected in this report."
}
```

Note: the peak-to-peak observation in the live-generated report is computed directly from `min`/`max` in `ECG_summary.json` for every recording, and may name a different highest/lowest date than a hand-authored reference if the reference's number does not match its own source statistics exactly — the computed value is authoritative.
