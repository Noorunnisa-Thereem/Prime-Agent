---
name: questionnaire
description: Use when asked to generate, regenerate, update, or validate the questionnaire Digital Twin longitudinal summary — e.g. "generate the questionnaire report", "regenerate the questionnaire summary", "run questionnaire" — or when the questionnaire section of the integrated Digital Twin report needs to be extracted from questionnaire PDFs in patient_data/Questionnaire.
---

# Questionnaire

## Purpose
Turn a series of monthly patient-reported quality-of-life questionnaire PDFs into two outputs:
1. A standalone, longitudinal questionnaire consolidated summary — per-domain score trends plus every recurring patient-reported answer.
2. The `questionnaire` section of the integrated `Digital_Twin_Integrated_Report.json`.

## Inputs
- Source PDFs: `patient_data/Questionnaire/Questionnaire_<NN>_<YYYYMMDD>_<HHMMSS>_printed.pdf` — one PDF per visit. Each has a "Quality-of-Life Domain Summary" score table (7 domains, `<score>/100`) followed by one Question/Response section per domain.
- Standalone generator: `patient_prime_agent/questionnaire_summary.py`
- Integrated extractor: `patient_prime_agent/extractors/questionnaire.py`
- Integrated schema: `schemas/questionnaire.schema.json`
- Standalone output: `reports/questionnaire/Questionnaire_consolidated_summary.json`

## Standalone Summary Workflow
1. Glob `patient_data/Questionnaire/*.pdf`; strip page boilerplate (`SYNTHETIC TEST DATA...`, `Questionnaire NN/12 | Generated 2026`, `Page N`) before parsing — these repeat on every page and must never be treated as content.
2. From the header, extract `Name`, `Patient ID`, `Age/Sex`, `Assessment Date`, `Diagnosis Date`, `Current Meds`, `Seizure History`.
3. Parse the `Domain\nScore` table into an ordered `{domain_name: score}` map.
4. For each domain, locate its Question/Response section by the exact marker sequence `[domain_name, "Question", "Response"]`, then parse Q/A pairs. **Both questions and responses can wrap across a line break in the extracted PDF text** (e.g. a response `"Consistent focal motor\ntwitch"` or `"Borderline\nThrombocytopenia (Low)"`) — a naive one-line-per-answer parser corrupts these into garbled merged entries.
5. **Reconcile across all 12 reports before finalizing** (see `_reconcile_domain_questions`): the same ~9-10 questions recur verbatim in every visit, so first harvest, per domain, the question texts a *majority* of reports parsed identically (those are trustworthy even if a couple of reports hit a wrap ambiguity), then re-derive every report's pairs by anchoring on that known text directly against the joined page text. This sidesteps line-wrap ambiguity entirely instead of guessing per-line. If a known question can't be found in one report's text at all, that report's original per-line parse is kept rather than dropping data.
6. Score `status`: `<60` -> `"low"`, `60`–`<80` -> `"moderate"`, `>=80` -> `"favorable"`. `trend_direction` compares first vs. latest score only (`"increasing"`/`"decreasing"`/`"stable"`), same rule as every other longitudinal category in this project.
7. `latest_domain_summaries[short_key]`: built from the most recent report only. `short_key` comes from the fixed `DOMAIN_SHORT_KEY` mapping (see Canonical Output Shape).
8. `recurring_patient_reported_findings[short_key][]`: **every** question/answer pair from **every** report, grouped by item text, with the unique responses seen, the dates each occurred, and `occurrence_count`. This workflow does not drop any real patient-reported answer to shorten the list (see Rules for how this differs from some reference reports).
9. Write the result to `reports/questionnaire/Questionnaire_consolidated_summary.json`.

Run:

```powershell
python -m patient_prime_agent.questionnaire_summary
```

## Canonical Output Shape
Every standalone report must use exactly these top-level keys:

| Key | Contents |
| --- | --- |
| `report_label` | fixed report title |
| `patient_profile` | `patient_name`, `patient_id`, `age`, `sex`, `diagnosis_date`, `current_medications[]`, `seizure_history[]` |
| `observation_window` | `start_date`, `end_date`, `number_of_questionnaires`, `source_folder` |
| `domain_score_trends` | one entry per domain (slugified name, e.g. `aura_prodromal_symptoms`), each `{domain, values[], first_score, latest_score, minimum_score, maximum_score, trend_direction}` |
| `latest_domain_summaries` | one entry per domain using the short key (`DOMAIN_SHORT_KEY`), each `{score, status, clinically_relevant_responses[], summary}` |
| `recurring_patient_reported_findings` | one entry per domain (short key), each a list of `{item, reported_responses[], dates[], occurrence_count}` |
| `digital_twin_state` | `latest_low_scoring_domains[]`, `domains_with_score_change[]`, `monitoring_value` |
| `executive_summary` | `summary`, `limitations` |
| `processing_manifest` | `source_type`, counts, `status`, and the detected/processed/failed/skipped file lists |

`DOMAIN_SHORT_KEY` (fixed, matches this questionnaire's 7 known domains): `Seizure Frequency & Types` -> `seizure_summary`, `Aura & Prodromal Symptoms` -> `aura_summary`, `Medication Side Effects` -> `medication_summary`, `Sleep Quality & Architecture` -> `sleep_summary`, `Epileptic Triggers` -> `trigger_summary`, `Cognitive & Executive Function` -> `cognitive_summary`, `Lab Biomarkers & Lifestyle` -> `laboratory_lifestyle_summary`. A domain not in this map falls back to its slugified full name.

When no PDFs are found or none parse, every list/dict is `[]`/`{}`, every scalar is `null`, and `processing_manifest.status` is `"no_input"` — never a partially fabricated version of the shape.

## Integrated Report Extraction
Populate only these schema fields for the `questionnaire` section of the integrated report:
- `reported_symptoms[]`, `symptom_onset`, `duration`, `severity`, `relevant_history[]`, `patient_concerns[]`

## Rules
- Both questions and responses can wrap across a line break in the extracted text; never assume one line == one answer.
- `recurring_patient_reported_findings` includes **every** occurrence of every item across every report, including responses like `"No"`/`"Never"`/`"None"`/`"Absent"`/`"No change"`. Some hand-authored reference reports for this dataset exclude those negative/normal-finding occurrences to shorten the list — this workflow deliberately does not, because dropping a real patient-reported "No" would mean silently discarding real data to make the output shorter, which conflicts with this project's never-invent-and-never-discard rule. If a "findings only, no negatives" view is wanted, filter `recurring_patient_reported_findings` downstream rather than losing the data at generation time.
- `latest_domain_summaries[key].clinically_relevant_responses` is **every** Q&A pair captured for that domain in the latest report, not an arbitrarily trimmed subset — for the same reason as above.
- Do not infer diagnoses from questionnaire symptoms; keep patient-stated facts and clearly labeled responses only.
- Use `null` for absent scalar responses and `[]` for absent lists.

## Output
- Standalone: `reports/questionnaire/Questionnaire_consolidated_summary.json`
- Integrated section: written as part of `reports/Digital_Twin_Integrated_Report.json` when the full harness or agent runtime runs.

## Verification
- `python -m json.tool reports/questionnaire/Questionnaire_consolidated_summary.json` must succeed.
- Confirm `observation_window.number_of_questionnaires` equals `processing_manifest.processed_count`.
- Confirm every `domain_score_trends[key].values[]` entry matches that visit's PDF score table exactly.
- Spot-check at least one multi-line-wrapped response (search the source PDFs for an answer that spans two lines) and confirm it appears as one clean joined string, not split into two entries.
- Validate against `schemas/questionnaire.schema.json` for the integrated section.
- Confirm no generated questionnaire summary is written anywhere under `patient_data/`; outputs only go to `reports/`.
