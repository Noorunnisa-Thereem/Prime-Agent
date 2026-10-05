"""Fixes from the mentor's review of the DDI report: the patient's own pair, per-source evidence
tags, optional Finding details, and the PGx-source / counting statements."""

from __future__ import annotations

import pytest

from patient_prime_agent import report_html
from patient_prime_agent.path_d.ddi import aggregation
from patient_prime_agent.path_d.ddi.normalizer import Medication


def _med(name: str) -> Medication:
    return Medication(
        id=name, normalized_name=name.lower(), source_name=name.title(), dose_value=None, dose_unit=None,
        frequency=None, route=None, status="current", source_reference="test",
    )


_CNS = ["Daytime drowsiness", "Dizziness/lightheadedness", "Unsteady gait (ataxia)"]


def _assess(a: str, b: str, side_effects, pair_context="current_current"):
    return aggregation.build_pair_assessment(_med(a), _med(b), pair_context, {}, regimen_side_effects=side_effects)


# --- the patient's own pair: CNS effects are surfaced, never silently "no concern" ------------------


def test_two_antiepileptics_with_recorded_cns_effects_get_an_unresolved_pharmacodynamic_followup():
    result = _assess("lamotrigine", "levetiracetam", _CNS + ["Headache"])
    assert result["status"] == "no_interaction_detected"  # nothing curated documents an interaction
    follow = result["pharmacodynamic_followup"]
    assert follow["status"] == "unresolved"
    assert follow["patient_reported_effects"] == _CNS  # only the CNS effects, as written
    assert "no antiepileptic + antiepileptic rule" in follow["why_unresolved"]
    assert "not attributed to either drug" in follow["why_unresolved"]
    assert "unresolved" in result["patient_specific_interpretation"] and "clinician review" in result["patient_specific_interpretation"]
    assert any(e["modality"] == "clinical" and e["direction"] == "unresolved" and e["patient_specific"] for e in result["evidence"])


@pytest.mark.parametrize(
    ("a", "b", "effects"),
    [
        ("lamotrigine", "levetiracetam", ["Headache", "Nausea"]),  # no CNS effect recorded
        ("lamotrigine", "levetiracetam", None),
        ("lamotrigine", "sertraline", _CNS),  # not two antiepileptics
    ],
)
def test_no_followup_without_cns_effects_or_without_two_antiepileptics(a, b, effects):
    assert "pharmacodynamic_followup" not in _assess(a, b, effects)


def test_the_followup_never_asserts_an_interaction():
    result = _assess("lamotrigine", "levetiracetam", _CNS)
    assert result["mechanisms"] == []
    assert result["status"] != "interaction_detected"


def test_cns_effect_filter_keeps_the_sources_own_wording():
    assert aggregation.cns_effects_in(["Unsteady gait (ataxia)", "Rash", "Somnolence"]) == ["Unsteady gait (ataxia)", "Somnolence"]
    assert aggregation.cns_effects_in(None) == []


# --- per-pair rendering ------------------------------------------------------------------------------


def _entry(**extra):
    base = {
        "_self": "Drug A", "_partner": "Drug B", "finding": "Drug A may decrease Drug B exposure.",
        "evidence_labels": ["DailyMed label (Drug Interactions section)", "General web search (Tavily)"],
        "evidence_sources": [
            {"label": "DailyMed label (Drug Interactions section)", "status": "curated"},
            {"label": "General web search (Tavily)", "status": "inferred"},
        ],
        "evidence_details": {}, "pgx_context": None, "hyperlink_url": "https://dailymed.example/x",
        "primary_evidence_label": "DailyMed label (Drug Interactions section)",
    }
    base.update(extra)
    return base


def test_each_listed_source_is_tagged_so_tavily_is_never_read_as_curated():
    html = report_html._ddi_flag_sheet_entry_block(_entry())
    assert "DailyMed label (Drug Interactions section) [curated]" in html
    assert "General web search (Tavily) [inferred]" in html
    assert "General web search (Tavily) [curated]" not in html


def test_optional_details_are_shown_only_when_present():
    bare = report_html._ddi_flag_sheet_entry_block(_entry())
    for label in ("Mechanism:", "Direction:", "Consequence:"):
        assert label not in bare
    rich = report_html._ddi_flag_sheet_entry_block(_entry(evidence_details={"mechanism": "strong CYP3A4 inducer", "recommendation": "monitor levels"}))
    assert "<b>Mechanism:</b> strong CYP3A4 inducer" in rich
    assert "Consequence:" not in rich and "Direction:" not in rich
    assert "Recommendation:" not in rich  # not part of the Finding format any more


def test_a_pair_is_listed_once_not_under_both_drugs():
    report = {"pairs": [{"drug_a": "Drug A", "drug_b": "Drug B"}, {"drug_a": "Drug B", "drug_b": "Drug A"}, {"drug_a": "Drug C", "drug_b": "Drug A"}]}
    by_drug = report_html._ddi_flag_sheet_pairs_by_drug(report)
    assert [e["_partner"] for e in by_drug["drug a"]] == ["Drug B"]
    assert "drug b" not in by_drug
    assert [e["_partner"] for e in by_drug["drug c"]] == ["Drug A"]


def test_pgx_context_is_shown_with_each_findings_own_citation_and_is_marked_as_not_part_of_the_verdict():
    pgx = {
        "status": "predicted", "link": "both drugs carry a finding for the shared gene GENE1",
        "findings": [
            {"drug": "Drug A", "gene": "GENE1", "genotype": "GENE1 (AG)", "predicted_effect": "toxicity", "citation": "PMID:111"},
            {"drug": "Drug B", "gene": "GENE1", "genotype": "GENE1 (AG)", "predicted_effect": "efficacy", "citation": "PMCID:PMC222"},
        ],
    }
    html = report_html._ddi_flag_sheet_entry_block(_entry(pgx_context=pgx))
    assert "Patient PGx context [predicted] (not part of this verdict)" in html
    assert "PMID:111" in html and "PMCID:PMC222" in html  # each finding stays traceable
    assert "shared gene GENE1" in html
    assert "PGx context" not in report_html._ddi_flag_sheet_entry_block(_entry())


def _flag_report(**extra):
    base = {
        "pairs": [dict(_entry(), drug_a="Drug A", drug_b="Drug B", impact="negative")],
        "pairs_checked": 3, "pairs_with_evidence": 1, "unique_pairs_with_evidence": 1, "pairs_dropped_unresolved": 2,
        "classification_method": "finding_agent (openai/gpt-5.5): the agent decides each pair's Impact",
        "counting_basis": "unique unordered drug pairs: A+B and B+A are one pair, counted once",
        "pgx_source": {
            "origin": "patient-specific: this patient's own PGx panel", "document": "Pharmacogenomic (PGx) Clinical Interpretation Report",
            "report_date": "2026-09-10", "loci_analyzed": 76, "drugs_covered": 47, "pooled_with_external_sources": False,
            "traceability": "Each finding keeps its own citation.", "role": "context only",
        },
        "source_rejection_counts": {"pubmed:animal_or_cell_study": 2, "pubmed:third_drug": 5},
    }
    base.update(extra)
    return base


def test_flag_sheet_card_states_method_counting_basis_pgx_source_and_rejections():
    html = report_html._ddi_flag_sheet_card(_flag_report())
    assert "How pairs were classified:" in html and "finding_agent (openai/gpt-5.5)" in html
    assert "A+B and B+A are one pair, counted once" in html
    assert "Where the PGx context comes from:" in html and "patient-specific" in html
    assert "Pooled with external sources: no" in html and "Each finding keeps its own citation." in html
    assert "pubmed - animal or cell study: 2" in html and "pubmed - third drug: 5" in html
    assert "3 unique pairs; 1 unique pair(s) resolved" in html


def test_flag_sheet_card_still_renders_for_a_report_written_before_these_fields_existed():
    old = {"pairs": [dict(_entry(), drug_a="Drug A", drug_b="Drug B", impact="negative")], "pairs_checked": 3, "pairs_with_evidence": 1, "pairs_dropped_unresolved": 2}
    html = report_html._ddi_flag_sheet_card(old)
    assert "Drug A" in html and "Where the PGx context comes from" not in html
