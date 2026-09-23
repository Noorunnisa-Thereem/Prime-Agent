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


def test_therapy_assessment_card_shows_supporting_and_counter_evidence_and_impression():
    html_fragment = report_html._therapy_assessment_card(_therapy())

    assert "Supporting Evidence" in html_fragment and "SCN1A (CT): efficacy" in html_fragment
    assert "Counter-Evidence" in html_fragment and "ABCB1 (GG): reduced efficacy" in html_fragment
    assert "Clinical Impression" in html_fragment
    assert "1 supporting, 1 counter, and 1 unresolved" in html_fragment
    assert "Continue EEG-derived seizure-risk monitoring." in html_fragment


def test_therapy_assessment_card_does_not_render_an_unresolved_evidence_column():
    # By request, matching the reference report's 2-column current-therapy layout:
    # Supporting Evidence | Counter-Evidence only. The underlying unresolved_evidence
    # data is untouched (still in DDI_Clinical_Assessment.json) -- it's just not
    # rendered in this specific card.
    html_fragment = report_html._therapy_assessment_card(_therapy())

    assert "Unresolved Evidence" not in html_fragment
    assert "not resolved against a live DDI knowledge base" not in html_fragment


def test_therapy_assessment_card_keeps_only_the_top_3_most_load_bearing_points_per_column():
    therapy = _therapy(
        supporting_evidence=[
            _evidence("pharmacogenomic", "supporting", "Low-value predicted finding.") | {"evidence_level": "predicted"},
            _evidence("laboratory", "supporting", "High-value observed lab finding.") | {"evidence_level": "high"},
            _evidence("pharmacogenomic", "supporting", "Moderate-value PGx finding A.") | {"evidence_level": "moderate"},
            _evidence("clinical", "supporting", "High-value clinical finding.") | {"evidence_level": "high"},
            _evidence("pharmacogenomic", "supporting", "Moderate-value PGx finding B.") | {"evidence_level": "moderate"},
        ]
    )
    html_fragment = report_html._therapy_assessment_card(therapy)

    assert "High-value observed lab finding." in html_fragment
    assert "High-value clinical finding." in html_fragment
    assert "Low-value predicted finding." not in html_fragment, "the lowest-ranked item must be dropped once there are more than 3"
    # Exactly one of the two tied "moderate" items should fill the third slot -- which
    # one is a stable-sort implementation detail, not asserted here.
    assert html_fragment.count("Moderate-value PGx finding") == 1


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
                    {"source_name": "Lamotrigine", "normalized_name": "lamotrigine", "dose": {"value": 150.0, "unit": "mg"}, "frequency": "BID", "status": "current", "reconciliation_flags": []},
                    {"source_name": "Levetiracetam (Keppra)", "normalized_name": "levetiracetam", "dose": {"value": 1000.0, "unit": "mg"}, "frequency": "BID", "status": "current", "reconciliation_flags": []},
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

    assert "Clinical inference:" in html_fragment, "the section's single load-bearing caution must be present"
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
    cards_index = html_fragment.index("Clinical Impression")  # unique to a rendered therapy card
    assert summary_index < reconciliation_index < cards_index, "the summary overview must come first, then reconciliation, then the detailed cards"


def test_summary_card_header_counts_clinically_relevant_vs_unresolved_from_real_flags(monkeypatch):
    # 1 pair (no_interaction_detected, both drugs resolved -> green/"clinically relevant")
    # + 1 therapy (effectiveness_incomplete -> sev-mod -> amber/"unresolved"). The Flag
    # Sheet's own curated pairs are cleared here so this test isolates
    # therapy_assessments + current_pair_assessments counting specifically -- their
    # contribution is covered separately below.
    monkeypatch.setattr(report_html, "DDI_GREEN_FLAGS", [])
    monkeypatch.setattr(report_html, "DDI_RED_FLAGS", [])
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


def test_summary_card_returns_empty_when_there_is_nothing_to_summarize(monkeypatch):
    # Now that pair-level counting also draws on the curated Flag Sheet pairs, this must
    # be isolated (no therapies, no current pairs, no curated Flag Sheet pairs) to still
    # be a genuine "nothing to summarize" case.
    monkeypatch.setattr(report_html, "DDI_GREEN_FLAGS", [])
    monkeypatch.setattr(report_html, "DDI_RED_FLAGS", [])
    assert report_html._ddi_summary_overview_card({"current_pair_assessments": [], "therapy_assessments": []}) == ""


def test_summary_card_shows_the_new_finding_level_stat_tiles_matching_the_headline(monkeypatch):
    # Per request: the stat tiles must be built from the exact same clinically-significant/
    # resolved-no-concern/unresolved counts as the headline sentence, not a separate
    # curated-source drug-coverage computation (the old "Resolved in Curated DDI Source" /
    # "Unresolved / Not in Curated Source" tiles, which answered a different question and
    # could show numbers that contradicted the headline).
    monkeypatch.setattr(report_html, "DDI_GREEN_FLAGS", [])
    monkeypatch.setattr(report_html, "DDI_RED_FLAGS", [])
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

    assert "Resolved in Curated DDI Source" not in html_fragment
    assert "Unresolved / Not in Curated Source" not in html_fragment
    # _therapy()'s position is "effectiveness_incomplete" -> sev-mod -> unresolved.
    assert '<div class="stat"><b>1</b><span>Total Findings Assessed</span></div>' in html_fragment
    assert '<div class="stat"><b>0</b><span>Clinically Significant</span></div>' in html_fragment
    assert '<div class="stat"><b>0</b><span>Resolved — No Concern</span></div>' in html_fragment
    assert '<div class="stat"><b>1</b><span>Unresolved</span></div>' in html_fragment
    assert "Overall: 0 clinically relevant findings | 1 unresolved finding(s)." in html_fragment


def test_summary_card_headline_counts_always_match_the_sum_of_the_stat_tiles():
    """Regression guard for the real reported bug: the headline sentence
    ("Overall: N clinically relevant findings | M unresolved finding(s)")
    and the stat tiles below it used to be computed independently and could
    show contradictory numbers (e.g. headline said 2 unresolved, tiles said
    0). Both must now derive from the same _ddi_finding_counts pass, so this
    parses the real rendered HTML and checks the arithmetic holds -- against
    the real 16-pair curated Flag Sheet list plus real therapy/pair data,
    not an isolated/monkeypatched scenario."""
    import re as _re

    html_fragment = report_html._sec_drug_interactions(_sections_with_pairs_and_therapies([_pair()], [_therapy(), _therapy(medication={"source_name": "Levetiracetam (Keppra)"}, position="high_caution")]))

    headline_match = _re.search(r"Overall: (\d+) clinically relevant findings \| (\d+) unresolved finding\(s\)\.", html_fragment)
    assert headline_match, "the headline sentence must be present"
    headline_relevant, headline_unresolved = int(headline_match.group(1)), int(headline_match.group(2))

    tile_values = [int(v) for v in _re.findall(r'<div class="stat"><b>(\d+)</b><span>(?:Clinically Significant|Resolved — No Concern|Unresolved)</span></div>', html_fragment)]
    assert len(tile_values) == 3, "expected exactly the 3 finding-bucket tiles (plus the separate Total Findings Assessed tile)"
    clinically_significant, resolved_no_concern, unresolved = tile_values

    assert headline_relevant == clinically_significant + resolved_no_concern
    assert headline_unresolved == unresolved

    total_tile_match = _re.search(r'<div class="stat"><b>(\d+)</b><span>Total Findings Assessed</span></div>', html_fragment)
    assert total_tile_match
    assert int(total_tile_match.group(1)) == clinically_significant + resolved_no_concern + unresolved


def test_shared_clinically_relevant_interactions_table_no_longer_exists():
    # Per-pair detail for the patient's real regimen now lives entirely in each therapy's own
    # card and the pair-level summary row -- the old shared "Clinically Relevant Interactions"
    # table that only listed a subset of pairs has been removed.
    html_fragment = report_html._sec_drug_interactions(_sections_with_pairs_and_therapies([_pair()], [_therapy()]))

    assert "Clinically Relevant Interactions" not in html_fragment


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


# ---------------------------------------------------------------------------
# Drug Interaction Flag Sheet -- redesigned as one section per screened drug
# (patient_prime_agent.ddi_flag_evidence's ~325-pair multi-source screening
# panel over path_d.ddi.candidate_drugs), each with its own 2-column
# Positive Impact / Negative Impact table -- every pair reaching this layer
# has already been resolved to exactly one of the two by ddi_flag_evidence's
# own _pair_finding (there is no third "needs review" bucket; an unresolved
# pair is dropped upstream, never passed here). Decoupled from
# ddi.current_pair_assessments entirely -- the screening panel is a separate
# input (sections[SEC_DDI_FLAG_EVIDENCE]), never derived from the patient's
# real current_pair_assessments the way the old curated 16-pair design was.
# ---------------------------------------------------------------------------


def test_ddi_clinical_inference_callout_appears_exactly_once():
    # Before this fix, the section carried two separately-worded callouts that both said
    # "absence of a resolved finding does not mean no risk/interaction" -- one source of
    # truth for that statement now, not two.
    html_fragment = report_html._sec_drug_interactions(_sections_with([_therapy()]))

    assert html_fragment.count("Clinical inference:") == 1
    assert "Therapy-Level Evidence Assessment" not in html_fragment, "the old, separately-worded duplicate callout must be gone"


def _flag_pair(drug_a: str, drug_b: str, impact: str, finding: str, *, evidence_labels=None, hyperlink_url=None, primary_evidence_label=None) -> dict:
    labels = evidence_labels if evidence_labels is not None else ["External evidence (PubMed)"]
    return {
        "drug_a": drug_a,
        "drug_b": drug_b,
        "impact": impact,
        "finding": finding,
        "evidence_labels": labels,
        "hyperlink_url": hyperlink_url,
        # Defaults to evidence_labels[0] for tests that don't care about the distinction --
        # but see ddi_flag_evidence._pair_finding: real data is not guaranteed to have the
        # two match (priority-order fall-through can resolve a pair from a lower-priority
        # source than evidence_labels[0] names), so any test exercising that distinction
        # must pass primary_evidence_label explicitly rather than rely on this default.
        "primary_evidence_label": primary_evidence_label if primary_evidence_label is not None else (labels[0] if labels else None),
    }


def _flag_evidence(pairs: list[dict], *, checked: int | None = None, dropped: int = 3, limitations: list[str] | None = None) -> dict:
    return {
        "pairs_checked": checked if checked is not None else len(pairs) + dropped,
        "pairs_with_evidence": len(pairs),
        "pairs_dropped_unresolved": dropped,
        "pairs": pairs,
        "limitations": limitations if limitations is not None else ["A sample limitation for testing."],
    }


def test_flag_sheet_card_with_no_evidence_states_the_gap_explicitly_without_crashing():
    assert report_html._ddi_flag_sheet_card(None) == report_html._ddi_flag_sheet_card({})
    html_fragment = report_html._ddi_flag_sheet_card({})
    assert "Drug Interaction Flag Sheet" in html_fragment
    assert "No live multi-source evidence is available" in html_fragment
    assert "<table" not in html_fragment


def test_flag_sheet_card_renders_one_section_per_drug_with_a_two_column_table():
    flag_evidence = _flag_evidence(
        [
            _flag_pair("Lamotrigine", "Levetiracetam", "positive", "Well tolerated combination."),
            _flag_pair("Lamotrigine", "Carbamazepine", "negative", "Carbamazepine may decrease lamotrigine levels."),
        ]
    )
    html_fragment = report_html._ddi_flag_sheet_card(flag_evidence)

    # 3 distinct drugs across the 2 pairs -> 3 sections, each its own 2-column table.
    for heading in ("LAMOTRIGINE", "LEVETIRACETAM", "CARBAMAZEPINE"):
        assert f'<div class="h2" style="margin-top:10px">{heading}</div>' in html_fragment
    assert html_fragment.count("<th>Positive Impact</th>") == 3
    assert html_fragment.count("<th>Negative Impact</th>") == 3
    assert "Needs Review" not in html_fragment, "there is no third column -- every pair reaching this layer is already resolved"


def test_flag_sheet_card_shows_each_pair_from_both_drugs_own_point_of_view():
    flag_evidence = _flag_evidence([_flag_pair("Lamotrigine", "Levetiracetam", "positive", "Well tolerated combination.")])
    html_fragment = report_html._ddi_flag_sheet_card(flag_evidence)

    lamotrigine_section = html_fragment.split("LAMOTRIGINE</div>", 1)[1].split("</table>", 1)[0]
    levetiracetam_section = html_fragment.split("LEVETIRACETAM</div>", 1)[1].split("</table>", 1)[0]
    assert "Lamotrigine + Levetiracetam</div>" in lamotrigine_section
    assert "Levetiracetam + Lamotrigine</div>" in levetiracetam_section


def test_flag_sheet_card_places_a_pair_in_exactly_one_impact_column():
    import re as _re

    flag_evidence = _flag_evidence([_flag_pair("Lamotrigine", "Levetiracetam", "negative", "May decrease efficacy.")])
    html_fragment = report_html._ddi_flag_sheet_card(flag_evidence)

    lamotrigine_table = html_fragment.split("LAMOTRIGINE</div>", 1)[1].split("</table>", 1)[0]
    cells = _re.findall(r"<td>(.*?)</td>", lamotrigine_table, _re.DOTALL)
    assert len(cells) == 2  # Positive, Negative, in that order -- no third column
    positive_cell, negative_cell = cells
    assert "Lamotrigine + Levetiracetam" not in positive_cell
    assert "Lamotrigine + Levetiracetam" in negative_cell


def test_flag_sheet_card_shows_a_clickable_pubmed_resource_name_not_the_raw_url():
    # Per request: the link text is "PubMed" (a clickable resource name), not the raw URL.
    flag_evidence = _flag_evidence(
        [_flag_pair("Lamotrigine", "Levetiracetam", "negative", "Finding.", hyperlink_url="https://pubmed.ncbi.nlm.nih.gov/12345/")]
    )
    html_fragment = report_html._ddi_flag_sheet_card(flag_evidence)

    assert '<a href="https://pubmed.ncbi.nlm.nih.gov/12345/">PubMed</a>' in html_fragment
    assert ">https://pubmed.ncbi.nlm.nih.gov/12345/<" not in html_fragment


def test_flag_sheet_card_names_the_hyperlink_after_its_real_primary_source_not_always_pubmed():
    # An earlier version hard-coded the link-anchor text "PubMed" for every pair regardless
    # of which of the 4 live sources actually supplied hyperlink_url -- a real bug once
    # DailyMed/ClinicalTrials.gov/Tavily started supplying real urls too (see
    # _ddi_flag_sheet_hyperlink_anchor_text). Each pair's anchor text must match its own
    # primary_evidence_label, the same candidate ddi_flag_evidence._pair_finding drew the
    # url from -- NOT evidence_labels[0] (see the next test for why those two can differ).
    flag_evidence = _flag_evidence(
        [
            _flag_pair(
                "Lamotrigine", "Carbamazepine", "negative", "A relevant trial found increased risk.",
                evidence_labels=["ClinicalTrials.gov"], hyperlink_url="https://clinicaltrials.gov/study/NCT00000001",
            ),
            _flag_pair(
                "Lamotrigine", "Valproic Acid", "positive", "Label states no dosage adjustment necessary.",
                evidence_labels=["DailyMed label (Drug Interactions section)"],
                hyperlink_url="https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=abc",
            ),
            _flag_pair(
                "Lamotrigine", "Sertraline", "positive", "A general web result describes it as well tolerated.",
                evidence_labels=["General web search (Tavily)"], hyperlink_url="https://example.com/web-result",
            ),
        ]
    )
    html_fragment = report_html._ddi_flag_sheet_card(flag_evidence)

    assert '<a href="https://clinicaltrials.gov/study/NCT00000001">ClinicalTrials.gov</a>' in html_fragment
    assert '<a href="https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=abc">DailyMed</a>' in html_fragment
    assert '<a href="https://example.com/web-result">General web search (Tavily)</a>' in html_fragment


def test_flag_sheet_card_hyperlink_anchor_survives_priority_order_fall_through():
    # Real, observed bug: since ddi_flag_evidence._pair_finding can fall through an
    # ambiguous higher-priority candidate to resolve a pair from a lower-priority one,
    # evidence_labels[0] is not always the source that actually supplied hyperlink_url --
    # a live run showed a real ClinicalTrials.gov study link captioned "DailyMed", and a
    # real general-web-search link captioned "ClinicalTrials.gov", because both cases used
    # evidence_labels[0] (a skipped, ambiguous higher-priority candidate) for the anchor
    # text instead of the label that actually resolved the pair. primary_evidence_label
    # fixes this: the anchor text must always match it, never evidence_labels[0], whenever
    # the two disagree.
    flag_evidence = _flag_evidence(
        [
            _flag_pair(
                "Aripiprazole", "Escitalopram", "positive", "Study to Evaluate the Efficacy, Safety and Tolerability of...",
                evidence_labels=["DailyMed label (Drug Interactions section)", "ClinicalTrials.gov"],
                hyperlink_url="https://clinicaltrials.gov/study/NCT01111552",
                primary_evidence_label="ClinicalTrials.gov",
            ),
            _flag_pair(
                "Carbamazepine", "Ethosuximide", "negative", "A web page describing increased risk.",
                evidence_labels=["ClinicalTrials.gov", "General web search (Tavily)"],
                hyperlink_url="https://emedicine.medscape.com/article/813654-overview",
                primary_evidence_label="General web search (Tavily)",
            ),
        ]
    )
    html_fragment = report_html._ddi_flag_sheet_card(flag_evidence)

    assert '<a href="https://clinicaltrials.gov/study/NCT01111552">ClinicalTrials.gov</a>' in html_fragment
    assert '<a href="https://clinicaltrials.gov/study/NCT01111552">DailyMed</a>' not in html_fragment
    assert '<a href="https://emedicine.medscape.com/article/813654-overview">General web search (Tavily)</a>' in html_fragment
    assert '<a href="https://emedicine.medscape.com/article/813654-overview">ClinicalTrials.gov</a>' not in html_fragment


def test_flag_sheet_card_omits_the_hyperlink_line_when_there_is_no_real_url():
    # "Hyperlink (if there)" -- a pair whose only evidence is Flockhart/pharmacodynamic
    # rules (no PubMed hit) has no hyperlink_url at all; the line must not appear, never a
    # placeholder or fabricated link.
    flag_evidence = _flag_evidence([_flag_pair("Lamotrigine", "Levetiracetam", "negative", "Curated CYP overlap.", evidence_labels=["Curated DDI (Flockhart)"])])
    html_fragment = report_html._ddi_flag_sheet_card(flag_evidence)

    assert "<b>Hyperlink:</b>" not in html_fragment


def test_flag_sheet_card_shows_findings_and_evidence_for_every_entry():
    flag_evidence = _flag_evidence([_flag_pair("Lamotrigine", "Levetiracetam", "negative", "Carbamazepine may decrease lamotrigine levels.", evidence_labels=["External evidence (PubMed)"])])
    html_fragment = report_html._ddi_flag_sheet_card(flag_evidence)

    assert "<b>Findings:</b> Carbamazepine may decrease lamotrigine levels." in html_fragment
    assert "<b>Evidence:</b> External evidence (PubMed)" in html_fragment


def test_flag_sheet_card_shows_screening_coverage_stats():
    flag_evidence = _flag_evidence([_flag_pair("Lamotrigine", "Levetiracetam", "positive", "Finding.")], checked=325, dropped=183)
    html_fragment = report_html._ddi_flag_sheet_card(flag_evidence)

    assert "325" in html_fragment  # pairs_checked
    assert "183" in html_fragment  # pairs_dropped_unresolved
    assert "never shown as a negative/no-concern finding" in html_fragment


def test_flag_sheet_card_renders_its_own_known_limitations_block():
    # The Flag Sheet's own intro paragraph says "(see Known Limitations)" -- confirm that
    # reference actually resolves to a rendered block (ddi_flag_evidence.py's own
    # limitations list), not a dangling pointer to nothing.
    caveat = (
        "Positive/Negative classification is derived from keyword matching on publication "
        "titles, which can misread comparative-effectiveness study titles (where multiple "
        "drugs are studied together) as a verdict about one specific pair. This is a known "
        "limitation of title-only keyword classification, not a per-pair clinical conclusion."
    )
    flag_evidence = _flag_evidence([_flag_pair("Lamotrigine", "Levetiracetam", "positive", "Finding.")], limitations=[caveat])
    html_fragment = report_html._ddi_flag_sheet_card(flag_evidence)

    assert "Known Limitations (Drug Interaction Flag Sheet)" in html_fragment
    assert caveat in html_fragment
    # The block must appear after the per-drug tables, not interleaved with them.
    assert html_fragment.index("LAMOTRIGINE</div>") < html_fragment.index("Known Limitations (Drug Interaction Flag Sheet)")


def test_flag_sheet_card_omits_the_known_limitations_block_when_there_are_no_limitations():
    flag_evidence = _flag_evidence([_flag_pair("Lamotrigine", "Levetiracetam", "positive", "Finding.")], limitations=[])
    html_fragment = report_html._ddi_flag_sheet_card(flag_evidence)

    assert "Known Limitations (Drug Interaction Flag Sheet)" not in html_fragment


def test_flag_sheet_card_is_embedded_in_the_drug_interactions_section():
    html_fragment = report_html._sec_drug_interactions(_sections_with([_therapy()]))

    assert "Drug Interaction Flag Sheet" in html_fragment


def test_candidate_pair_blocks_state_the_gap_explicitly_when_there_is_no_proposed_medication_list():
    # This dataset has no proposed-medication list (see ddi_summary.py) -- confirm the
    # section says so rather than inventing a candidate pair to fill it.
    html_fragment = report_html._ddi_candidate_pair_blocks({"proposed_pair_assessments": []})

    assert "Candidate / Proposed Medication-Pair Screening" in html_fragment
    assert "No candidate or proposed medications are present" in html_fragment
    assert "<table" not in html_fragment


def test_candidate_pair_blocks_render_one_flagged_block_per_proposed_pair():
    proposed = {
        "drug_a": "Lamotrigine",
        "drug_b": "Quetiapine",
        "pair_context": "current_proposed",
        "status": "not_evaluated",
        "severity": "unresolved",
        "patient_specific_interpretation": "Quetiapine is a candidate medicine, not part of the current regimen; this pair is not reported as an active interaction.",
    }
    html_fragment = report_html._ddi_candidate_pair_blocks({"proposed_pair_assessments": [proposed]})

    assert "Lamotrigine + Quetiapine" in html_fragment
    assert 'class="sev-mod">NOT EVALUATED' in html_fragment
    assert "Quetiapine is a candidate medicine" in html_fragment


def test_candidate_pair_screening_is_embedded_in_the_drug_interactions_section():
    html_fragment = report_html._sec_drug_interactions(_sections_with([_therapy()]))

    assert "Candidate / Proposed Medication-Pair Screening" in html_fragment


# ---------------------------------------------------------------------------
# Duplicate-content regression guards.
#
# A pypdf-text-extracted read of the real generated PDF (not the HTML source)
# found that Section 07 used to render a second "External Database Evidence
# -- DailyMed / ClinicalTrials.gov / PubMed" table whose PMID/NCT/DailyMed-
# SetID facts were already present, in fuller form, inside each drug's own
# Unresolved Evidence bullets above it -- confirmed by the same record
# identifiers appearing in both places. That table (_ddi_live_evidence_card)
# has been removed; these two tests guard against it (or something like it)
# being reintroduced.
#
# A blind "no two paragraphs exceed 80% similarity" check was tried first and
# rejected: at bullet granularity it flags real, correct content as false
# positives -- e.g. the four genuine CPIC "no gene-drug pair on record"
# bullets for SCN1A/ABCB1/CYP3A4/CACNA1H differ only in the gene name and
# score 80-94% similar to each other by raw text overlap, despite being four
# distinct real findings, not duplicates. The identifier-uniqueness test
# below checks the thing that actually distinguishes a duplicate from a
# templated-but-distinct finding; the block-level test provides the
# "no two paragraphs" style guard at a granularity coarse enough to avoid
# that false-positive class.
# ---------------------------------------------------------------------------


def test_no_external_record_identifier_is_rendered_in_two_different_blocks():
    """For every real external-database record identifier (PMID / NCT number /
    DailyMed SetID / ClinVar accession) that appears anywhere in Section 07,
    it must appear inside exactly one <li> or <td> element -- i.e. one place
    in the report -- not restated in a second element elsewhere in the
    section. Two different drugs legitimately citing the same identifier
    (e.g. both have a PGx finding on the same gene, so both cite the same
    ClinVar gene-level record) is fine; the same identifier appearing twice
    *for the same drug* in two different elements is the bug class this
    guards against."""
    import re

    therapy_a = _therapy(
        unresolved_evidence=[
            _evidence("literature", "unresolved", 'PubMed candidate reference (PMID 28862044): "Acute lamotrigine overdose."'),
            _evidence("labeling", "unresolved", 'DailyMed label on file: "LAMOTRIGINE TABLET" SetID 0833d3df-2df1-4abd-abe0-7fb2c0253c80.'),
            _evidence("clinical_trial", "unresolved", "ClinicalTrials.gov NCT00537238: registration does not establish study quality."),
        ]
    )
    therapy_b = _therapy(
        medication={"source_name": "Levetiracetam (Keppra)", "dose": {"value": 1000.0, "unit": "mg"}, "frequency": "BID"},
        unresolved_evidence=[
            _evidence("literature", "unresolved", 'PubMed candidate reference (PMID 30036676): "Levetiracetam."'),
        ],
    )
    html_fragment = report_html._sec_drug_interactions(_sections_with([therapy_a, therapy_b]))

    id_pattern = re.compile(r"PMID\s?:?\s?\d{6,9}|NCT\d{8}|VCV\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    elements = re.findall(r"<(?:li|td)[^>]*>(.*?)</(?:li|td)>", html_fragment, re.DOTALL)

    locations_by_id: dict[str, int] = {}
    for element in elements:
        for ident in set(id_pattern.findall(element)):
            locations_by_id[ident] = locations_by_id.get(ident, 0) + 1

    duplicated = {ident: count for ident, count in locations_by_id.items() if count > 1}
    assert not duplicated, f"these record identifiers each appear in more than one <li>/<td> in Section 07: {duplicated}"


def test_no_two_top_level_blocks_in_section_07_are_near_duplicate_statements():
    """Coarse, block-level version of 'no two paragraphs exceed the similarity
    threshold' -- see the module comment above for why bullet-level
    granularity produces false positives on this content. Compares each
    named block's full text (heading through the next heading) against every
    other block; this is exactly the granularity that caught the section's
    previous real bug (two separately-worded callouts both stating "absence
    of a resolved finding does not mean no risk/interaction")."""
    import difflib
    import html as html_module
    import re

    sections = _sections_with_pairs_and_therapies(
        [_pair()],
        [
            _therapy(),
            _therapy(
                medication={"source_name": "Levetiracetam (Keppra)", "dose": {"value": 1000.0, "unit": "mg"}, "frequency": "BID"},
                supporting_evidence=[_evidence("clinical", "supporting", "Seizure frequency reduced by 80% since starting levetiracetam.")],
                counter_evidence=[_evidence("laboratory", "counter", "Mild renal clearance reduction observed on last panel.")],
                unresolved_evidence=[_evidence("pharmacokinetic_ddi", "unresolved", "Levetiracetam was not resolved against a live DDI knowledge base.")],
                clinical_impression="1 supporting, 1 counter. Clinical-notes inference: seizure control improved, monitor renal function.",
                position="continuation_with_monitoring",
                recommended_monitoring=["Continue renal function monitoring."],
            ),
        ],
    )
    # The Flag Sheet's screening panel is a separate input (sections[SEC_DDI_FLAG_EVIDENCE]),
    # decoupled from current_pair_assessments -- give it a real pair so this near-duplicate
    # guard still exercises that section's content too.
    sections[report_html.SEC_DDI_FLAG_EVIDENCE] = _flag_evidence(
        [_flag_pair("Lamotrigine", "Valproic Acid", "negative", "Valproic acid may increase lamotrigine levels.")]
    )
    html_fragment = report_html._sec_drug_interactions(sections)
    plain = html_module.unescape(re.sub(r"<[^>]+>", " ", html_fragment))
    plain = re.sub(r"\s+", " ", plain).strip()

    headings = [
        "DDI & Patient-Specific Therapy Assessment",
        "Current-Regimen Drug-Pair Screening",
        "Key clinical interpretation:",
        "Monitoring / Clinician Review Checklist",
        "Medication Reconciliation",
        "Current Regimen",
        "Clinical inference:",
        "Candidate / Proposed Medication-Pair Screening",
        "Drug Interaction Flag Sheet",
        "Lamotrigine ",
        "Levetiracetam (Keppra)",
        "Limitations of this screen:",
    ]
    positions = sorted({(plain.index(h), h) for h in headings if h in plain})
    blocks = []
    for i, (pos, label) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(plain)
        blocks.append((label, plain[pos:end]))

    assert len(blocks) >= 8, "expected at least 8 distinct top-level blocks in a fully-populated Section 07"

    offenders = []
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            label_a, text_a = blocks[i]
            label_b, text_b = blocks[j]
            ratio = difflib.SequenceMatcher(None, text_a.lower(), text_b.lower()).ratio()
            if ratio > 0.80:
                offenders.append((ratio, label_a, label_b))

    assert not offenders, f"near-duplicate top-level blocks in Section 07 (similarity > 0.80): {offenders}"
    assert "Lamotrigine + Valproic Acid" in html_fragment, "a real red-flag pair from the shared data module must render"
