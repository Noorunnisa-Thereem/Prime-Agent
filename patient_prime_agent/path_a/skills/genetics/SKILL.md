---
name: genetics
description: Use when asked to generate, regenerate, update, or validate the pharmacogenomic (PGx) clinical interpretation summary — e.g. "generate the genetics report", "regenerate the genetics summary", "run genetics" — or when the genetics section of the integrated Digital Twin report needs to be extracted from patient_data/Genetics/genetics_data.xlsx.
---

# Genetics

## Purpose
Turn a pharmacogenomic gene-drug annotation spreadsheet into two outputs:
1. A standalone PGx clinical interpretation summary.
2. The `genetics` section of the integrated `Digital_Twin_Integrated_Report.json`.

## Inputs
- Source file: `patient_data/Genetics/genetics_data.xlsx` — one row per gene-drug annotation (164 rows, 63 columns: `Gene`, `Drug`, `Drug Class`, `Disease`, `Medical Conditions`, `Patient_genotype`, `Diplotype`, `Phenotype`, `PGx response`, `Significance`, `Recommendation Statement`, `Therapeutic Interaction`, `FDA`, `FDA Boxed Warning`, and duplicate `_01`-suffixed columns).
- Standalone generator: `patient_prime_agent/genetics_summary.py`
- Integrated extractor: `patient_prime_agent/extractors/genetics.py` (`GeneticsExtractor`)
- Integrated schema: `schemas/genetics.schema.json`
- Standalone output: `reports/genetics/genetics_clinical_summary.json`

**This source is structurally different from every other category's data.** CBC/ECG/EEG/MRI/CT are clean per-visit panels; this is a 164-row literature-annotation spreadsheet with duplicate/near-duplicate columns and inconsistent capitalization (`cyp3a4` vs `CYP3A5`). Always normalize gene names to uppercase before matching, and always read the base-named column (`PGx response`, `Recommendation Statement`), never its `_01`-suffixed duplicate — the two can disagree on the same row, and using the base column is what reproduces this report's reference values.

## Standalone Summary Workflow
1. Load `genetics_data.xlsx` with `openpyxl`; treat row 1 as headers.
2. **`metabolizer_profile`**: for each of the 9 headline pharmacogenes (`CYP2D6`, `CYP2C19`, `CYP2C9`, `CYP2B6`, `CYP3A5`, `CYP1A2`, `CYP3A4`, `UGT1A4`, `POR`), collect that gene's rows:
   - `status` = `"<Diplotype> - <Phenotype>"` when both are present on the row (e.g. `CYP2C9`: `"*1/*3 - Intermediate metabolizer"`); otherwise the raw `Patient_genotype` value (e.g. `CYP1A2`: `"AA"`). Never invent a phenotype label when the sheet has none — `CYP2D6`/`CYP2C19`/`CYP1A2`/`CYP3A4` in this dataset only have a raw genotype, not a diplotype-derived phenotype.
   - `impact` = the gene's drug(s) grouped by their `PGx response` value, each re-worded through the fixed vocabulary in `RESPONSE_PHRASES` (`efficacy` -> "favorable efficacy signal", `reduced efficacy` -> "reduced efficacy signal", `toxicity` -> "increased adverse-reaction risk", `moderate` -> "moderate response"). This is a fixed re-wording of the sheet's own category value, never a new clinical claim.
3. **`findings_by_therapeutic_class`**: bucket every row by its `Drug Class` value via `THERAPEUTIC_CLASS_MAP` (`Antidepressant` -> `antidepressants`; `Atypical antipsychotic`/`Typical antipsychotic` -> `antipsychotics`; `Antiepileptic`/`Antiepileptics`/`Anticonvulsants`/`Mood stabilizer` -> `mood_stabilizers_antiepileptics`; `Stimulant`/`Non-stimulant ADHD medication` -> `adhd_stimulants`; everything else -> `other_panel_entries`). Every row with a gene and a drug appears somewhere — this workflow does not trim the panel down to a curated subset.
4. **`priority_safety_flags`**: a *software-detected candidate list*, not an exhaustive clinical review (see Rules). Search only `Recommendation Statement` and `Therapeutic Interaction` (never `FDA Boxed Warning`, which is generic drug-level labeling repeated across many unrelated rows) for `"suicid"` -> `severity: "high"`; a `toxicity` row for one of the 9 headline genes with `Significance == "yes"` -> `severity: "moderate"`. Group by gene, cap at 10, sort high severity first.
5. **`boxed_warning_note`**: count and name the distinct drugs that actually have non-empty `FDA Boxed Warning` text in this patient's rows — never restate specific FDA warning language as if it were verified beyond what the sheet contains.
6. **`clinical_conclusion`**: assembled only from what was computed in steps 2 and 4 (which genes are reduced-function, which flags are high severity) — never an independently-authored clinical narrative.
7. Write the result to `reports/genetics/genetics_clinical_summary.json`.

Run:

```powershell
python -m patient_prime_agent.genetics_summary
```

## Canonical Output Shape
Every standalone report must use exactly these top-level keys:

| Key | Contents |
| --- | --- |
| `report_type` | fixed report title |
| `patient` | `patient_id`, `report_date`, `specimen`, `ordering_context`, `variants_analyzed`, `drugs_covered` |
| `purpose` | templated disclosure of what the report is and is not |
| `metabolizer_profile[]` | one `{gene, status, impact}` per headline pharmacogene actually present |
| `findings_by_therapeutic_class` | `{antidepressants, antipsychotics, mood_stabilizers_antiepileptics, adhd_stimulants, other_panel_entries}`, each a list of `{drug, genetic_basis, predicted_effect, significant}` |
| `priority_safety_flags[]` | `{flag, risk, severity}` (see Rules — a candidate list, not exhaustive) |
| `boxed_warning_note` | derived from real `FDA Boxed Warning` presence in this patient's rows |
| `clinical_conclusion` | `{impression, recommendations[]}`, built only from computed facts |

When the spreadsheet is missing or empty, every list/dict is `[]`/`{}`, every scalar is `null` or `0`, and `clinical_conclusion.impression` states that no source data was available — never a partially fabricated version of the shape.

## Integrated Report Extraction
Populate only these schema fields for the `genetics` section of the integrated report:
- `test_name`
- `variants[]` (`gene`, `variant`, `zygosity`, `classification`, `inheritance`, `interpretation`)
- `overall_interpretation`
- `recommendations[]`

The integrated extractor works on text/JSON documents with explicit classification/zygosity wording; it does not parse `.xlsx` directly (see Rules).

## Rules
- **`priority_safety_flags` is a keyword- and significance-driven candidate list, not a substitute for clinical review.** It will not necessarily match every flag a human curator would choose (or exclude every one they would drop) — never present it as an exhaustive or clinically validated ranking.
- Never search `FDA Boxed Warning` text for gene-specific risk signals — it is drug-level labeling (e.g. the standard antidepressant suicidality class warning) that repeats across many rows regardless of which gene the row is actually about, and including it produces false gene-specific flags.
- `metabolizer_profile.status` must only combine `Diplotype`/`Phenotype` when the sheet provides both for that gene; a bare genotype (e.g. `"AA"`) must never be upgraded into an invented phenotype label.
- Always read the base-named column (`PGx response`, `Recommendation Statement`), never the `_01`-suffixed duplicate — they can disagree on the same row, and this project's reports must be reproducible from one documented rule, not an undocumented choice between two near-duplicate columns.
- `findings_by_therapeutic_class` includes every row with a gene and a drug; do not silently drop rows to make the list shorter or tidier.
- Never assert a specific FDA warning's content beyond what is literally present in that row's `FDA Boxed Warning` text.
- Do not infer pathogenicity, inheritance, or treatment action from context alone; never invent a diplotype, phenotype, or drug-class grouping not derivable from the sheet.

## Output
- Standalone: `reports/genetics/genetics_clinical_summary.json`
- Integrated section: written as part of `reports/Digital_Twin_Integrated_Report.json` when the full harness or agent runtime runs.

## Verification
- `python -m json.tool reports/genetics/genetics_clinical_summary.json` must succeed.
- Confirm every `metabolizer_profile` entry's `status` traces to that gene's actual `Diplotype`/`Phenotype`/`Patient_genotype` cells.
- Confirm `findings_by_therapeutic_class` totals equal the number of rows with both a gene and a drug.
- Confirm every `priority_safety_flags` entry's underlying text is present in that gene's `Recommendation Statement` or `Therapeutic Interaction` cell.
- Validate against `schemas/genetics.schema.json` for the integrated section; check that every variant has source support in the original file.
- Confirm no generated genetics summary is written anywhere under `patient_data/`; outputs only go to `reports/`.
