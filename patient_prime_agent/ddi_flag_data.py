"""Canonical Green-Flags / Red-Flags drug-drug-interaction pair data.

Single source of truth for the curated 16-pair list drawn from the NeuroTwin
drug-extraction report (reports/genetics/genetics_clinical_summary.json, 44
drugs) and cross-checked against the NeuroPrecisionDx PGx report (Sample ID
1325006529) plus open pharmacology sources (drug labels, peer-reviewed
literature) -- see the interactive sheet for the full write-up:
https://claude.ai/artifact/CF1sD3LZn2kQAT7rsjX9uN

Consumers:
  - reports/drug_interaction_flags/build_flags_table.py (the standalone
    Excel deliverable) -- still uses the curated bullet text in full.
  - patient_prime_agent/report_html.py's _ddi_flag_sheet_card -- uses this
    module only for the pair list (drug_a/drug_b) and which list a pair is
    drawn from (Status column). The curated bullet text is retired as a
    report source: the PDF's Finding/Evidence Source columns come from
    patient_prime_agent.ddi_flag_evidence's live PubMed/DailyMed lookups
    instead (or, for the one pair that is also the patient's current
    regimen, from the real computed DDI_Clinical_Assessment.json pipeline)
    -- never this module's bullets.
  - reports/drug_interaction_flags/preview.html mirrors this data as a
    JS literal (a published, already-shared artifact -- if this list
    changes, update that file's GREEN/RED arrays to match).

Not exhaustive: 44 drugs allow 946 possible pairs; this is a curated
clinically-notable subset, not a full screen.
"""

from __future__ import annotations

SOURCE_NOTE = (
    "Source: reports/genetics/genetics_clinical_summary.json (44 drugs, NeuroTwin PGx extraction), "
    "cross-checked against the NeuroPrecisionDx PGx report (Sample ID 1325006529, pp. 2–3). "
    "Each pair confirmed against open pharmacology sources (drug labels, peer-reviewed literature). "
    "Decision-support reference only — not a prescribing instruction. Not exhaustive across all "
    "possible pairs among the 44 extracted drugs."
)

ARTIFACT_URL = "https://claude.ai/artifact/CF1sD3LZn2kQAT7rsjX9uN"

# Each entry: (drug_a, drug_b, [bullet, bullet])
GREEN_FLAGS: list[tuple[str, str, list[str]]] = [
    ("Lamotrigine", "Levetiracetam", [
        "Patient's actual current regimen. No shared CYP pathway; levetiracetam is barely liver-metabolized.",
        "This patient's own DDI screen found no resolved pharmacokinetic/pharmacodynamic interaction between them.",
    ]),
    ("Levetiracetam", "Sertraline", [
        "Levetiracetam does not inhibit or induce any CYP enzyme sertraline depends on.",
        "No clinically significant interaction reported between the two.",
    ]),
    ("Levetiracetam", "Escitalopram", [
        "Same clean metabolic profile applies; escitalopram's CYP2C19/3A4 clearance is unaffected.",
        "No dose adjustment typically needed for either drug.",
    ]),
    ("Levetiracetam", "Atorvastatin", [
        "Levetiracetam has no effect on CYP3A4, atorvastatin's main clearance route.",
        "Statin exposure and effectiveness are unaffected by adding levetiracetam.",
    ]),
    ("Levetiracetam", "Aripiprazole", [
        "No shared metabolic pathway; levetiracetam doesn't touch CYP2D6/CYP3A4.",
        "Aripiprazole dosing is unaffected by concurrent levetiracetam use.",
    ]),
    ("Levetiracetam", "Atomoxetine", [
        "Different clearance routes (renal vs. CYP2D6); no meaningful metabolic overlap.",
        "No significant interaction documented between the two.",
    ]),
    ("Levetiracetam", "Methylphenidate", [
        "Different mechanisms of action and metabolism; often co-prescribed in pediatric epilepsy+ADHD care.",
        "No significant pharmacokinetic interaction reported.",
    ]),
]

RED_FLAGS: list[tuple[str, str, list[str]]] = [
    ("Lamotrigine", "Valproic Acid", [
        "Valproate blocks lamotrigine's UGT1A4 clearance, roughly doubling lamotrigine blood levels.",
        "Raises toxicity and serious skin-reaction risk (SJS/TEN); lamotrigine dose must be cut ~50%.",
    ]),
    ("Lamotrigine", "Carbamazepine", [
        "Carbamazepine speeds up lamotrigine breakdown, lowering its level and effectiveness.",
        "Combination also causes added CNS toxicity (diplopia, dizziness) even at “normal” drug levels.",
    ]),
    ("Carbamazepine", "Phenytoin", [
        "Each drug induces the other's metabolism, making both levels unpredictable over 2-4 weeks.",
        "Risk of breakthrough seizures from sub-therapeutic levels; needs frequent level checks.",
    ]),
    ("Valproic Acid", "Phenytoin", [
        "Valproate displaces phenytoin from plasma proteins and blocks its metabolism at the same time.",
        "Free (active) phenytoin can rise sharply while total level looks normal — toxicity is easy to miss.",
    ]),
    ("Fluoxetine", "Aripiprazole", [
        "Fluoxetine strongly blocks CYP2D6, the main enzyme clearing aripiprazole.",
        "Levels can rise 2-3x, increasing tremor/akathisia/sedation risk; FDA label calls for dose reduction.",
    ]),
    ("Bupropion", "Nortriptyline", [
        "Bupropion inhibits CYP2D6, raising nortriptyline blood levels.",
        "Both drugs lower seizure threshold, so the combination compounds seizure risk too.",
    ]),
    ("Lithium", "Haloperidol", [
        "Case reports describe a neurotoxicity syndrome (confusion, rigidity, fever) with this combination.",
        "Rare but severe outcomes (irreversible dyskinesia) reported even at standard doses — monitor closely.",
    ]),
    ("Carbamazepine", "Atorvastatin", [
        "Carbamazepine strongly induces CYP3A4, which atorvastatin depends on for clearance.",
        "Statin exposure can drop 53-82%, meaningfully reducing cholesterol-lowering effect.",
    ]),
    ("Duloxetine", "Paroxetine", [
        "Combining two serotonergic antidepressants raises serotonin-syndrome risk.",
        "Paroxetine also inhibits the enzyme that clears duloxetine, pushing duloxetine levels higher.",
    ]),
]
