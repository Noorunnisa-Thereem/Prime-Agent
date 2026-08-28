"""Static, versioned pharmacology reference data.

This is general drug-metabolism knowledge (published, drug-level facts), not
patient data -- the same kind of fixed reference table ``genetics_summary.py``
already uses for its ``RESPONSE_PHRASES`` re-wording. Every entry names the
source and a snapshot label so it stays auditable and distinguishable from
the patient-specific findings in ``reports/genetics``.

SuperCYPsPred is a real predicted-interaction model, but no local snapshot of
its output exists in this project. Rather than inventing plausible-looking
probabilities, the adapter below always reports the drug as unresolved for
predicted evidence -- consistent with the plan's own instruction that a
missing source must show up as "not evaluated", never as fabricated evidence.
"""

from __future__ import annotations

from typing import Any

FLOCKHART_SOURCE = "Flockhart Cytochrome P450 Drug Interaction Table"
FLOCKHART_VERSION = "curated-snapshot-2024"
SUPERCYPSPRED_SOURCE = "SuperCYPsPred"
SUPERCYPSPRED_VERSION = None  # no local snapshot available -- see module docstring

# Keyed by lowercase generic ingredient name. Each relationship: enzyme, role
# (substrate/inhibitor/inducer/non_cyp_primary_pathway), strength, and a short
# cited rationale. An empty list is itself a real finding (drug not listed as
# a significant CYP participant), not a gap.
CURATED_CYP_RELATIONSHIPS: dict[str, list[dict[str, Any]]] = {
    "levetiracetam": [],
    "lamotrigine": [
        {
            "enzyme": "UGT1A4",
            "role": "non_cyp_primary_pathway",
            "strength": "primary",
            "evidence_type": "curated",
            "source": FLOCKHART_SOURCE,
            "source_version": FLOCKHART_VERSION,
            "note": (
                "Lamotrigine clearance is primarily UGT1A4-mediated glucuronidation, "
                "not CYP450-mediated; it is not listed as a significant CYP450 "
                "substrate, inhibitor, or inducer."
            ),
        }
    ],
}

CURATED_RATIONALE_NOTES: dict[str, str] = {
    "levetiracetam": (
        "Levetiracetam undergoes minimal hepatic metabolism (partial non-CYP "
        "enzymatic hydrolysis) and is predominantly renally eliminated; it is not "
        "listed in curated CYP-relationship references as a significant CYP450 "
        "substrate, inhibitor, or inducer."
    ),
}

# General pharmacologic drug-class reference, used only to evaluate the
# pharmacodynamic rule table against whichever drugs actually appear in a
# patient's regimen. Not patient data.
DRUG_CLASS_MAP: dict[str, str] = {
    "levetiracetam": "antiepileptic",
    "lamotrigine": "antiepileptic",
    "diazepam": "benzodiazepine_sedative",
    "clonazepam": "benzodiazepine_sedative",
    "sertraline": "ssri",
    "citalopram": "ssri",
    "escitalopram": "ssri",
    "fluoxetine": "ssri",
    "paroxetine": "ssri",
}


def curated_cyp_relationships(normalized_drug_name: str) -> list[dict[str, Any]]:
    return list(CURATED_CYP_RELATIONSHIPS.get(normalized_drug_name.lower(), []))


def curated_rationale(normalized_drug_name: str) -> str | None:
    return CURATED_RATIONALE_NOTES.get(normalized_drug_name.lower())


def predicted_cyp_relationships(normalized_drug_name: str) -> list[dict[str, Any]]:
    """SuperCYPsPred adapter. Always empty -- see module docstring."""

    return []


def drug_class(normalized_drug_name: str) -> str | None:
    return DRUG_CLASS_MAP.get(normalized_drug_name.lower())


def pharmacokinetic_pair_summary(drug_a: str, drug_b: str) -> dict[str, Any]:
    """Compare curated CYP roles for two drugs and report any real overlap.

    Returns a dict with ``mechanism_found`` (bool), a human-readable
    ``statement``, and the ``evidence`` items backing it. An overlap exists
    only when one drug's curated role is inhibitor/inducer on an enzyme the
    other drug is a substrate of -- never inferred from an altered PGx
    phenotype alone (that belongs to :mod:`.pgx_modifiers`).
    """

    a_rel = curated_cyp_relationships(drug_a)
    b_rel = curated_cyp_relationships(drug_b)
    a_enzymes = {r["enzyme"]: r["role"] for r in a_rel if r["role"] in ("substrate", "inhibitor", "inducer")}
    b_enzymes = {r["enzyme"]: r["role"] for r in b_rel if r["role"] in ("substrate", "inhibitor", "inducer")}

    overlaps: list[dict[str, Any]] = []
    for enzyme, role_a in a_enzymes.items():
        role_b = b_enzymes.get(enzyme)
        if role_b is None:
            continue
        if {role_a, role_b} & {"inhibitor", "inducer"} and "substrate" in (role_a, role_b):
            overlaps.append({"enzyme": enzyme, "role_a": role_a, "role_b": role_b})

    if overlaps:
        statement = (
            f"Curated reference data ({FLOCKHART_SOURCE}, {FLOCKHART_VERSION}) shows an overlapping "
            f"CYP pathway between {drug_a} and {drug_b}: " +
            "; ".join(f"{o['enzyme']} ({o['role_a']}/{o['role_b']})" for o in overlaps)
        )
        return {"mechanism_found": True, "statement": statement, "evidence": overlaps}

    notes = [n for n in (curated_rationale(drug_a), curated_rationale(drug_b)) if n]
    statement = (
        f"No established pharmacokinetic (CYP-mediated) mechanism was identified between "
        f"{drug_a} and {drug_b} in curated reference data ({FLOCKHART_SOURCE}, {FLOCKHART_VERSION})."
    )
    if notes:
        statement += " " + " ".join(notes)
    return {"mechanism_found": False, "statement": statement, "evidence": []}
