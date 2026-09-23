"""The curated drug-screening candidate list for the expanded Drug
Interaction Flag Sheet (report_html.py's per-drug Positive/Negative Impact
tables).

This is NOT the full ~102-drug NeuroPrecisionDx PGx panel documented in
``skills/patient_prime_agent/ddi-therapeutic-response/SKILL.md`` -- that
full panel spans therapeutic areas (antivirals, statins, thyroid
replacement, ...) with no real relevance to this patient's actual
neurological/psychiatric care, and screening all of it would produce
~5,000 pairs dominated by clinically irrelevant combinations. Instead, every
name below is drawn from ``reports/genetics/genetics_clinical_summary.json``
-- the patient's own real 44-drug pharmacogenomic panel (confirmed by
reading that file directly: it names exactly 44 drugs under
``findings_by_therapeutic_class``) -- so every candidate here has genuine,
already-computed per-patient genetic findings available to it, never a
drug pulled in with no real patient-specific data behind it at all.

The 18 starting names are exactly ``ddi_flag_data.GREEN_FLAGS +
ddi_flag_data.RED_FLAGS``'s own drug set (confirmed to be a strict subset of
the 44-drug genetics panel) -- the same curated, clinically-reviewed pairs
that already backed the Drug Interaction Flag Sheet. The other 8
(Oxcarbazepine, Clobazam, Ethosuximide, Perampanel, Citalopram, Venlafaxine,
Olanzapine, Quetiapine) extend that set with clinically common
antiepileptic/psychiatric companions for a patient on
lamotrigine+levetiracetam, still drawn only from the same real 44-drug
panel -- never an arbitrary addition.
"""

from __future__ import annotations

from .normalizer import Medication, normalize_name

DDI_SCREENING_CANDIDATES: tuple[str, ...] = (
    # Already curated in ddi_flag_data.py (GREEN_FLAGS + RED_FLAGS's own drug set).
    "Aripiprazole",
    "Atomoxetine",
    "Atorvastatin",
    "Bupropion",
    "Carbamazepine",
    "Duloxetine",
    "Escitalopram",
    "Fluoxetine",
    "Haloperidol",
    "Lithium",
    "Methylphenidate",
    "Nortriptyline",
    "Paroxetine",
    "Phenytoin",
    "Sertraline",
    "Valproic Acid",
    # New additions -- clinically common antiepileptic/psychiatric companions,
    # still drawn from the same real 44-drug genetics panel.
    "Oxcarbazepine",
    "Clobazam",
    "Ethosuximide",
    "Perampanel",
    "Citalopram",
    "Venlafaxine",
    "Olanzapine",
    "Quetiapine",
)
# Lamotrigine and Levetiracetam are deliberately NOT listed above -- they are
# the patient's real current-regimen drugs (see normalizer.normalize_regimen),
# not screening candidates; merged_candidate_medications below excludes any
# name that collides with the real regimen regardless.


def merged_candidate_medications(current_medications: list[Medication]) -> list[Medication]:
    """Build synthetic ``Medication`` records (status="proposed") for every
    name in ``DDI_SCREENING_CANDIDATES`` that is not already part of the
    patient's real current regimen (deduplicated by the same normalized-name
    comparison ``normalizer.normalize_regimen`` uses).

    These are marked ``status="proposed"`` -- never "current" -- deliberately:
    they are a curated screening panel this codebase checked pairwise
    interactions against, not medications the patient is confirmed to be
    taking. This keeps ``pairing.generate_pairs``'s existing
    current_current/current_proposed/proposed_proposed distinction accurate,
    so the Medication Reconciliation table and "N current medication(s)
    reconciled" language never overstate the patient's real regimen.
    """
    current_names = {m.normalized_name for m in current_medications}
    candidates: list[Medication] = []
    for index, source_name in enumerate(DDI_SCREENING_CANDIDATES, start=1):
        normalized = normalize_name(source_name)
        if normalized in current_names:
            continue
        candidates.append(
            Medication(
                id=f"ddi-screen-{index:02d}-{normalized.replace(' ', '-')}",
                normalized_name=normalized,
                source_name=source_name,
                dose_value=None,
                dose_unit=None,
                frequency=None,
                route=None,
                status="proposed",
                source_reference="path_d.ddi.candidate_drugs.DDI_SCREENING_CANDIDATES (curated screening panel, drawn from the patient's own genetics_clinical_summary.json 44-drug PGx panel -- not part of the patient's actual current regimen)",
                reconciliation_flags=[],
            )
        )
    return candidates
