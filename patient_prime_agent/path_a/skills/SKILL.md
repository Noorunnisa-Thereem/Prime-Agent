---
name: patient-prime-agent
description: Use when generating, updating, validating, or explaining the Prime Agent patient digital twin report across clinical_notes, cbc, ct, mri, ecg, eeg, genetics, and questionnaire data folders.
---

# Prime Agent Harness

## Inputs
- Source data root: `patient_data/`
- Category skills: `patient_prime_agent/skills/<category>/SKILL.md`
- Category schemas: `schemas/<category>.schema.json`
- Integrated schema: `schemas/digital_twin_report.schema.json`
- Output folder: `reports/`

## Workflow
1. Discover files with `patient_prime_agent.file_tools.collect_files`.
2. Map files to categories using folder or filename aliases from `config.CATEGORY_ALIASES`.
3. Load the matching category skill before extracting that category.
4. Run each category extractor from `patient_prime_agent.extractors`.
5. Use `null` for unknown scalar values and `[]` for unknown lists.
6. Preserve source traceability with file paths, source lines where available, and short excerpts.
7. Repair and validate each category against its schema before merging.
8. Merge category outputs into `reports/Digital_Twin_Integrated_Report.json`.

## Category Order
Process categories in this order: clinical_notes, cbc, ct, mri, ecg, eeg, genetics, questionnaire.

## Validation
- Run `python -m patient_prime_agent` for the integrated report.
- Run `python -m json.tool <output-json>` on generated JSON files.
- If a validation issue repeats, update the narrowest category `SKILL.md` with the reusable lesson.
