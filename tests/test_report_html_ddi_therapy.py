"""Tests for report_html.py's therapy-level DDI evidence rendering.

Per DDI_Integration_Plan_v02, patient_prime_agent.path_d.ddi.aggregation.
build_therapy_assessment() already produces a per-therapy assessment --
supporting_evidence / counter_evidence / unresolved_evidence /
clinical_impression / position / recommended_monitoring -- for every
current-regimen drug (see reports/ddi/DDI_Clinical_Assessment.json's
"therapy_assessments" key). Before this module's _therapy_assessment_card
was added, report_html.py's Section 07 never read that key at all -- it
only ever rendered the pair-level current_pair_assessments summary, so the
richer evidence sat unused in the JSON. These tests cover the new
rendering path directly (fast, no PDF rendering) and confirm the "unresolved
is never collapsed into no evidence" rule from the plan's section 15.
"""

from __future__ import annotations

from patient_prime_agent import report_html


def _evidence(modality: str, direction: str, statement: str) -> dict:
    return {
        "modality": modality,
        "direction": direction,
        "statement": statement,
        "source_reference": "test-source",
        "evidence_level": "moderate",
        "patient_specific": True,
    }


def _therapy(**overrides) -> dict:
    therapy = {
        "medication": {
            "source_name": "Lamotrigine",
            "dose": {"value": 150.0, "unit": "mg"},
            "frequency": "BID",
        },
        "supporting_evidence": [_evidence("pharmacogenomic", "supporting", "SCN1A (CT): efficacy")],
        "counter_evidence": [_evidence("pharmacogenomic", "counter", "ABCB1 (GG): reduced efficacy")],
        "unresolved_evidence": [_evidence("pharmacokinetic_ddi", "unresolved", "Lamotrigine was not resolved against a live DDI knowledge base.")],
        "clinical_impression": "1 supporting, 1 counter, and 1 unresolved evidence item(s) were identified for Lamotrigine.",
        "position": "effectiveness_incomplete",
        "recommended_monitoring": ["Continue EEG-derived seizure-risk monitoring."],
    }
    therapy.update(overrides)
    return therapy


def test_therapy_assessment_card_shows_drug_name_and_position_badge_without_repeating_dose():
    html_fragment = report_html._therapy_assessment_card(_therapy())

    assert "Lamotrigine" in html_fragment
    assert "Effectiveness Incomplete" in html_fragment  # _humanize() title-cases; CSS renders it visually uppercase
    assert 'class="badge sev-mod"' in html_fragment, "effectiveness_incomplete must use the DDI position color map, not fall through to neutral"
    # Dose is deliberately NOT repeated here -- the Medication Reconciliation table directly
    # above the therapy cards in _sec_drug_interactions is the single source for it.
    assert "150.0" not in html_fragment and "BID" not in html_fragment


def test_therapy_assessment_card_shows_all_three_evidence_buckets_and_impression():
    html_fragment = report_html._therapy_assessment_card(_therapy())

    assert "Supporting Evidence" in html_fragment and "SCN1A (CT): efficacy" in html_fragment
    assert "Counter-Evidence" in html_fragment and "ABCB1 (GG): reduced efficacy" in html_fragment
    assert "Unresolved Evidence" in html_fragment and "not resolved against a live DDI knowledge base" in html_fragment
    assert "Clinical Impression" in html_fragment
    assert "1 supporting, 1 counter, and 1 unresolved" in html_fragment
    assert "Continue EEG-derived seizure-risk monitoring." in html_fragment


def test_therapy_assessment_card_states_an_empty_evidence_bucket_explicitly():
    # Per the plan's conflict-resolution rules, "unresolved" must never be collapsed into
    # "no evidence" -- an empty bucket (here, no counter-evidence at all) must say so
    # explicitly rather than rendering a blank <ul>.
    html_fragment = report_html._therapy_assessment_card(_therapy(counter_evidence=[]))

    assert "None identified in the available sources." in html_fragment


def test_ddi_position_class_map_covers_the_full_enum_with_correct_severity():
    assert report_html._DDI_POSITION_CLASS["continuation_supported"] == "sev-low"
    assert report_html._DDI_POSITION_CLASS["continuation_with_monitoring"] == "sev-mod"
    assert report_html._DDI_POSITION_CLASS["effectiveness_incomplete"] == "sev-mod"
    assert report_html._DDI_POSITION_CLASS["indication_supported_response_inadequate"] == "sev-mod"
    assert report_html._DDI_POSITION_CLASS["evidence_insufficient"] == "sev-neutral"
    assert report_html._DDI_POSITION_CLASS["high_caution"] == "sev-high"
    assert report_html._DDI_POSITION_CLASS["reconciliation_required"] == "sev-high"


def _sections_with(therapy_assessments: list[dict]) -> dict:
    return {
        report_html.SEC_DDI: {
            "source_coverage": {"flockhart_source": "Flockhart", "flockhart_version": "v1"},
            "medication_reconciliation": {
                "normalized_medications": [
                    {"source_name": "Lamotrigine", "dose": {"value": 150.0, "unit": "mg"}, "frequency": "BID", "status": "current", "reconciliation_flags": []},
                    {"source_name": "Levetiracetam (Keppra)", "dose": {"value": 1000.0, "unit": "mg"}, "frequency": "BID", "status": "current", "reconciliation_flags": []},
                ],
                "conflicts": [],
            },
            "current_pair_assessments": [],
            "therapy_assessments": therapy_assessments,
            "overall_interpretation": {"recommended_monitoring": []},
            "limitations": [],
        }
    }


def test_sec_drug_interactions_renders_a_card_per_therapy_assessment():
    html_fragment = report_html._sec_drug_interactions(
        _sections_with(
            [
                _therapy(),
                _therapy(medication={"source_name": "Levetiracetam (Keppra)", "dose": {"value": 1000.0, "unit": "mg"}, "frequency": "BID"}),
            ]
        )
    )

    assert "Therapy-Level Evidence Assessment" in html_fragment
    assert html_fragment.count("Clinical Impression") == 2, "one therapy card per therapy_assessments entry"


def test_medication_reconciliation_table_drops_the_always_current_status_column():
    html_fragment = report_html._sec_drug_interactions(_sections_with([_therapy()]))

    # Scoped to the Medication Reconciliation table specifically -- Status is always
    # "current" for this dataset (see ddi/normalizer.py) and adds no information there.
    # This does NOT ban a "Status" column report-wide: the separate Current Regimen --
    # Curated-Source Coverage table further down legitimately has one (Resolved/
    # Unresolved/Not Evaluated actually varies per drug and is informative).
    reconciliation_block = html_fragment.split("Medication Reconciliation</div>", 1)[1].split("</table>", 1)[0]
    assert "<th>Status</th>" not in reconciliation_block, "Status is always 'current' for this dataset (see ddi/normalizer.py) and adds no information"
    assert "<th>Drug</th>" in reconciliation_block and "<th>Dose</th>" in reconciliation_block and "<th>Flags</th>" in reconciliation_block


def test_dose_appears_exactly_once_per_drug_not_duplicated_across_table_and_card():
    html_fragment = report_html._sec_drug_interactions(_sections_with([_therapy()]))

    # "150.0" (Lamotrigine's dose) must come from the Medication Reconciliation table only --
    # the therapy card below it must not repeat the dose that's already shown there.
    assert html_fragment.count("150.0") == 1
    assert "Lamotrigine" in html_fragment and "Levetiracetam" in html_fragment


# ---------------------------------------------------------------------------
# DDI & Patient-Specific Therapy Assessment -- top-of-section summary overview
# ---------------------------------------------------------------------------


def _pair(**overrides) -> dict:
    pair = {
        "drug_a": "Lamotrigine",
        "drug_b": "Levetiracetam (Keppra)",
        "pair_context": "current_current",
        "status": "no_interaction_detected",
        "mechanisms": [],
        "evidence": [
            {"modality": "pharmacokinetic_ddi", "direction": "supporting", "statement": "No mechanism found."},
            {"modality": "pharmacogenomic", "direction": "counter", "statement": "ABCB1 (GG): reduced efficacy"},
            {"modality": "pharmacokinetic_ddi", "direction": "unresolved", "statement": "SuperCYPsPred not available."},
        ],
        "severity": "minor",
    }
    pair.update(overrides)
    return pair


def _sections_with_pairs_and_therapies(pairs: list[dict], therapies: list[dict]) -> dict:
    sections = _sections_with(therapies)
    sections[report_html.SEC_DDI]["current_pair_assessments"] = pairs
    sections[report_html.SEC_DDI]["source_coverage"]["resolved_drugs"] = ["lamotrigine", "levetiracetam"]
    sections[report_html.SEC_DDI]["source_coverage"]["unresolved_drugs"] = []
    return sections


def test_summary_card_appears_before_medication_reconciliation():
    html_fragment = report_html._sec_drug_interactions(_sections_with_pairs_and_therapies([_pair()], [_therapy()]))

    summary_index = html_fragment.index("DDI &amp; Patient-Specific Therapy Assessment")
    reconciliation_index = html_fragment.index("Medication Reconciliation")
    cards_index = html_fragment.index("Therapy-Level Evidence Assessment")
    assert summary_index < reconciliation_index < cards_index, "the summary overview must come first, then reconciliation, then the detailed cards"


def test_summary_card_header_counts_clinically_relevant_vs_unresolved_from_real_flags():
    # 1 pair (no_interaction_detected, both drugs resolved -> green/"clinically relevant")
    # + 1 therapy (effectiveness_incomplete -> sev-mod -> amber/"unresolved").
    html_fragment = report_html._sec_drug_interactions(_sections_with_pairs_and_therapies([_pair()], [_therapy()]))

    assert "Overall: 1 clinically relevant findings | 1 unresolved finding(s)." in html_fragment


def test_pair_row_flag_is_green_when_resolved_and_amber_when_drug_is_uncovered():
    resolved_row = report_html._ddi_summary_pair_row(
        _pair(), coverage={"unresolved_drugs": []}, name_map={"Lamotrigine": "lamotrigine", "Levetiracetam (Keppra)": "levetiracetam"}, therapy_by_source_name={}
    )
    assert resolved_row["flag_css"] == "sev-low"
    assert resolved_row["flag_label"] == "NO MAJOR CONCERN"

    uncovered_row = report_html._ddi_summary_pair_row(
        _pair(), coverage={"unresolved_drugs": ["levetiracetam"]}, name_map={"Lamotrigine": "lamotrigine", "Levetiracetam (Keppra)": "levetiracetam"}, therapy_by_source_name={}
    )
    assert uncovered_row["flag_css"] == "sev-mod"
    assert uncovered_row["flag_label"] == "UNRESOLVED"


def test_pair_row_flag_is_red_when_a_mechanism_was_actually_detected():
    detected = _pair(status="interaction_detected", mechanisms=[{"type": "pharmacodynamic", "description": "Additive CNS depression."}])
    row = report_html._ddi_summary_pair_row(detected, coverage={}, name_map={}, therapy_by_source_name={})

    assert row["flag_css"] == "sev-high"
    assert row["flag_label"] == "RISK IDENTIFIED"
    assert row["key_finding"] == "Additive CNS depression."


def test_pair_row_patient_impact_connects_to_the_worse_linked_therapy():
    row = report_html._ddi_summary_pair_row(
        _pair(),
        coverage={"unresolved_drugs": []},
        name_map={"Lamotrigine": "lamotrigine", "Levetiracetam (Keppra)": "levetiracetam"},
        therapy_by_source_name={"Lamotrigine": _therapy(clinical_impression="4 supporting, 5 counter. Clinical-notes inference: incomplete seizure suppression.")},
    )
    assert "incomplete seizure suppression" in row["patient_impact"]
    assert "not a confirmed drug interaction" in row["patient_impact"]


def test_pair_row_evidence_source_shows_the_real_combined_pubmed_pair_query():
    # A genuine "Drug A AND Drug B" combined PubMed query (external_evidence_summary.py's
    # by_drug_pair) is always "unresolved" in direction, but it must still show up as a
    # real evidence source for this pair -- it's the whole point of this fix, not
    # tangential noise like the generic SuperCYPsPred-unavailable note.
    pair = _pair(
        evidence=[
            {"modality": "pharmacokinetic_ddi", "direction": "supporting", "statement": "No mechanism found."},
            {"modality": "pharmacokinetic_ddi", "direction": "unresolved", "statement": "SuperCYPsPred not available."},
            {"modality": "literature", "direction": "unresolved", "statement": 'PubMed candidate reference (PMID 33573557): "A combined-therapy paper."'},
        ]
    )
    row = report_html._ddi_summary_pair_row(
        pair,
        coverage={"unresolved_drugs": []},
        name_map={"Lamotrigine": "lamotrigine", "Levetiracetam (Keppra)": "levetiracetam"},
        therapy_by_source_name={},
    )

    assert "External evidence (PubMed)" in row["evidence_source"]
    assert "Curated DDI (Flockhart)" in row["evidence_source"]


def test_therapy_row_flag_reuses_the_existing_position_color_map():
    high_caution_row = report_html._ddi_summary_therapy_row(_therapy(position="high_caution"))
    assert high_caution_row["flag_css"] == "sev-high"
    assert high_caution_row["flag_label"] == "RISK IDENTIFIED"

    supported_row = report_html._ddi_summary_therapy_row(_therapy(position="continuation_supported"))
    assert supported_row["flag_css"] == "sev-low"
    assert supported_row["flag_label"] == "NO MAJOR CONCERN"


def test_evidence_source_labels_dedupes_and_reports_absence_honestly():
    items = [
        {"modality": "pharmacogenomic", "direction": "counter"},
        {"modality": "pharmacogenomic", "direction": "supporting"},  # duplicate modality -> one label
        {"modality": "clinical", "direction": "counter"},
    ]
    assert report_html._evidence_source_labels(items) == "Patient pharmacogenomic findings; Clinical record"
    assert report_html._evidence_source_labels([]) == "No resolved evidence source identified"


def test_patient_impact_from_impression_falls_back_to_full_text_without_the_marker():
    assert report_html._patient_impact_from_impression("A finding with no marker.") == "A finding with no marker."
    assert (
        report_html._patient_impact_from_impression("2 supporting. Clinical-notes inference: stable but incomplete.")
        == "stable but incomplete."
    )


def test_summary_card_shows_checklist_and_interpretation_callout():
    html_fragment = report_html._sec_drug_interactions(_sections_with_pairs_and_therapies([_pair()], [_therapy()]))

    assert "Key clinical interpretation" in html_fragment
    assert "not an automatic medication change" in html_fragment
    assert "Monitoring / Clinician Review Checklist" in html_fragment
    assert "Medication reconciliation" in html_fragment
    assert "Therapeutic drug levels" in html_fragment


def test_summary_card_returns_empty_when_there_is_nothing_to_summarize():
    assert report_html._ddi_summary_overview_card({"current_pair_assessments": [], "therapy_assessments": []}) == ""


def test_summary_card_shows_real_coverage_stats_not_a_fabricated_percentage():
    ddi = {
        "source_coverage": {"resolved_drugs": ["lamotrigine", "levetiracetam"], "unresolved_drugs": [], "not_evaluated_drugs": []},
        "medication_reconciliation": {
            "normalized_medications": [
                {"source_name": "Lamotrigine", "normalized_name": "lamotrigine"},
                {"source_name": "Levetiracetam (Keppra)", "normalized_name": "levetiracetam"},
            ]
        },
        "current_pair_assessments": [],
        "therapy_assessments": [_therapy()],
    }
    html_fragment = report_html._ddi_summary_overview_card(ddi)

    assert "Resolved in Curated DDI Source" in html_fragment
    assert "Unresolved / Not in Curated Source" in html_fragment
    # Real counts from source_coverage -- 2 total, 2 resolved, 0 unresolved.
    assert '<div class="stat"><b>2</b><span>Current Medications Screened</span></div>' in html_fragment
    assert '<div class="stat"><b>2</b><span>Resolved in Curated DDI Source</span></div>' in html_fragment
    assert '<div class="stat"><b>0</b><span>Unresolved / Not in Curated Source</span></div>' in html_fragment


def test_clinically_relevant_interactions_card_is_dropped_when_fully_redundant_with_the_summary():
    # A single no_interaction_detected pair is already fully represented in the summary
    # table above (Flag=NO MAJOR CONCERN, Key Finding, Patient Impact, Evidence Source) --
    # repeating a bare "no interaction identified" callout in its own card would just be
    # the same conclusion shown twice.
    html_fragment = report_html._sec_drug_interactions(_sections_with_pairs_and_therapies([_pair()], [_therapy()]))

    assert "Clinically Relevant Interactions" not in html_fragment
    assert "No clinically required drug-drug interaction" not in html_fragment


def test_clinically_relevant_interactions_card_is_kept_when_it_holds_real_incremental_detail():
    detected = _pair(status="interaction_detected", severity="major", mechanisms=[{"type": "pharmacodynamic", "description": "Additive CNS depression."}])
    html_fragment = report_html._sec_drug_interactions(_sections_with_pairs_and_therapies([detected], [_therapy()]))

    assert "Clinically Relevant Interactions" in html_fragment
    assert "major" in html_fragment.lower()


def test_essential_monitoring_card_is_removed_since_it_only_ever_repeated_the_per_therapy_pills():
    # It was the union of every therapy's own recommended_monitoring -- already shown once
    # per drug inside each therapy card -- so it never carried information a reader couldn't
    # already see above it. Real monitoring items must still appear (inside the therapy
    # cards), just not a third time as their own standalone section-level card.
    html_fragment = report_html._sec_drug_interactions(
        _sections_with_pairs_and_therapies(
            [_pair()],
            [_therapy(recommended_monitoring=["Continue EEG-derived seizure-risk monitoring."])],
        )
    )

    assert "Essential Monitoring" not in html_fragment
    assert html_fragment.count("Continue EEG-derived seizure-risk monitoring.") == 1
