"""Pair classification follows SKILL.md (ddi-therapeutic-response) Parts 3-5.

One section per rule: Part 4 tier 1 (patient PGx via Part 3's shared-mechanism
check), tier 2 (curated sources never Positive), tier 3 (literature:
section-aware, negation-aware, conclusion-weighted, co-occurrence-checked), and
Part 5 evidence-status tags.
"""

from __future__ import annotations

import pytest

from patient_prime_agent import ddi_flag_evidence as dfe
from patient_prime_agent.external_lookup import pubmed_lookup

_NO_MENTION = {"mentioned": False, "excerpt": None, "url": None, "note": "not mentioned in reviewed label section"}
_NO_FLOCKHART = {"mechanism_found": False}


def _genetics(*rows):
    return {"findings_by_therapeutic_class": {"any": [dict(r, significant=r.get("significant", True)) for r in rows]}}


def _row(drug, basis, effect, pmid="PMID:111"):
    return {"drug": drug, "genetic_basis": basis, "predicted_effect": effect, "citation": pmid}


# --- Part 3: patient PGx is context, never a verdict ---------------------------------------


def test_shared_gene_links_a_pair_for_context_and_keeps_each_findings_own_citation():
    rows = dfe._pgx_rows_by_drug(_genetics(_row("Drug A", "GENE1 (AG)", "toxicity", "PMID:111"), _row("Drug B", "GENE1 (AG)", "efficacy", "PMID:222")))
    ctx = dfe._pgx_context("Drug A", "Drug B", "drug a", "drug b", rows)
    assert "shared gene GENE1" in ctx["link"]
    assert ctx["status"] == "predicted"
    assert {(f["drug"], f["predicted_effect"], f["citation"]) for f in ctx["findings"]} == {
        ("Drug A", "toxicity", "PMID:111"),
        ("Drug B", "efficacy", "PMID:222"),
    }
    assert "impact" not in ctx  # context only: no pair verdict


def test_unrelated_genes_show_no_pgx_context():
    rows = dfe._pgx_rows_by_drug(_genetics(_row("Drug A", "GENE1 (AG)", "toxicity"), _row("Drug B", "GENE2 (GG)", "toxicity")))
    assert dfe._pgx_context("Drug A", "Drug B", "drug a", "drug b", rows) is None


def test_only_one_drug_having_a_pgx_row_shows_no_context():
    rows = dfe._pgx_rows_by_drug(_genetics(_row("Drug A", "GENE1 (AG)", "toxicity")))
    assert dfe._pgx_context("Drug A", "Drug B", "drug a", "drug b", rows) is None


def test_non_significant_pgx_rows_are_ignored():
    rows = dfe._pgx_rows_by_drug(_genetics(_row("Drug A", "GENE1 (AG)", "toxicity"), dict(_row("Drug B", "GENE1 (AG)", "toxicity"), significant=False)))
    assert dfe._pgx_context("Drug A", "Drug B", "drug a", "drug b", rows) is None


def test_curated_enzyme_relationship_links_a_pair_for_context(monkeypatch):
    monkeypatch.setattr(
        dfe.reference_data, "curated_cyp_relationships",
        lambda name: [{"enzyme": "CYP2D6", "role": "inhibitor"}] if name == "drug b" else [],
    )
    rows = dfe._pgx_rows_by_drug(_genetics(_row("Drug A", "CYP2D6 (GG)", "toxicity"), _row("Drug B", "GENE9 (AA)", "efficacy")))
    ctx = dfe._pgx_context("Drug A", "Drug B", "drug a", "drug b", rows)
    assert "inhibitor of CYP2D6" in ctx["link"]


def test_pgx_never_classifies_a_pair_even_when_it_is_the_only_linkage():
    # A pair with PGx context but no pair-specific evidence is simply unresolved.
    rows = dfe._pgx_rows_by_drug(_genetics(_row("Drug A", "GENE1 (AG)", "efficacy"), _row("Drug B", "GENE1 (AG)", "efficacy")))
    assert dfe._pgx_context("Drug A", "Drug B", "drug a", "drug b", rows) is not None
    assert dfe._pair_finding(_NO_FLOCKHART, [], _NO_MENTION, _NO_MENTION, None, None, None, None, None, None) is None


def test_pair_finding_has_no_pgx_input_at_all():
    import inspect

    assert not any("pgx" in name for name in inspect.signature(dfe._pair_finding).parameters)


def test_pgx_source_is_documented_as_patient_specific_and_traceable():
    note = dfe.pgx_source_note(
        {"report_type": "Pharmacogenomic (PGx) Clinical Interpretation Report", "patient": {"report_date": "2026-09-10", "variants_analyzed": 76, "drugs_covered": 47}}
    )
    assert note["pooled_with_external_sources"] is False
    assert "patient-specific" in note["origin"] and "PMID/PMCID" in note["traceability"]
    assert note["report_date"] == "2026-09-10" and note["loci_analyzed"] == 76
    assert "never decides" in note["role"]


# --- Part 4 tier 2: curated sources never Positive ------------------------------


def test_label_line_mentioning_tolerability_is_negative_not_positive():
    # Real bug: "...monitor clinical response and tolerability" classified a 50-67% exposure drop Positive.
    mention = {
        "mentioned": True,
        "excerpt": "Inducers decreased perampanel plasma levels by 50-67%; monitor clinical response and tolerability.",
        "url": "https://dailymed.example/1",
        "note": None,
    }
    result = dfe._pair_finding(_NO_FLOCKHART, [], mention, _NO_MENTION, None, None, None, None, None, None)
    assert result[0] == "negative"


def test_curated_no_adjustment_statement_resolves_nothing_and_falls_through():
    mention = {"mentioned": True, "excerpt": "No dosage adjustment is necessary.", "url": "https://dailymed.example/1", "note": None}
    assert dfe._pair_finding(_NO_FLOCKHART, [], mention, _NO_MENTION, None, None, None, None, None, None) is None


# --- Part 4 tier 3: literature ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("The combination was not well tolerated.", "negative"),
        ("The combination did not improve symptoms.", "negative"),
        ("There was no benefit of adding the drug.", "negative"),
        ("Pharmacokinetics were not significantly affected by coadministration.", "positive"),
        ("Atorvastatin added to nortriptyline significantly reduced migraine attack frequency.", "positive"),
        ("Coadministration reduced plasma concentrations of the second drug by 40%.", "negative"),
        # Conclusion-weighted, not conclusion-only: a Conclusion contradicted by its Results resolves nothing.
        ("Adverse events increased with the combination. The combination was well tolerated.", None),
    ],
)
def test_literature_classification_is_negation_aware_and_conclusion_weighted(text, want):
    assert dfe._classify_text(text) == want


def _sections(**kw):
    return {k.upper(): v for k, v in kw.items()}


def test_co_occurrence_requires_both_drugs_named_together_with_combination_wording():
    together = _sections(methods="Patients received drug a combined with drug b for 8 weeks.", results="Symptoms improved.")
    assert pubmed_lookup.describes_genuine_combination(together, "Drug A", "Drug B") is True
    joined = _sections(results="Drug A plus drug B improved response.")
    assert pubmed_lookup.describes_genuine_combination(joined, "Drug A", "Drug B") is True


def test_co_occurrence_rejects_a_review_listing_both_drugs_among_many():
    review = _sections(results="Exposure to drug c, drug a, drug d, drug b and drug e was associated with malformations.")
    assert pubmed_lookup.describes_genuine_combination(review, "Drug A", "Drug B") is False


def test_co_occurrence_rejects_a_comparator_design():
    comparator = _sections(methods="Drug a was compared with drug b in two separate arms.")
    assert pubmed_lookup.describes_genuine_combination(comparator, "Drug A", "Drug B") is False


def test_co_occurrence_ignores_names_that_appear_only_in_background():
    background_only = _sections(
        background="Drug a combined with drug b is common.", results="Response improved in the studied group."
    )
    assert pubmed_lookup.describes_genuine_combination(background_only, "Drug A", "Drug B") is False


def test_pubmed_title_alone_never_resolves_a_pair(monkeypatch, tmp_path):
    envelope = {"status": "ok", "records": [{"pmid": "1", "title": "Drug A and drug B combination is well tolerated."}]}
    monkeypatch.setattr(dfe.pubmed_lookup, "fetch_abstract_sections", lambda *a, **k: {"status": "no_results", "records": []})
    text, url = dfe._pubmed_finding_and_url(
        envelope, cache_dir=tmp_path, refresh=False, memory_path=tmp_path / "m.json", display_a="Drug A", display_b="Drug B"
    )
    assert (text, url) == (None, None)


def test_trial_without_posted_results_never_resolves_from_its_title(monkeypatch, tmp_path):
    envelope = {"status": "ok", "records": [{"nct_id": "NCT1", "brief_title": "Efficacy of drug a with drug b", "url": "u"}]}
    monkeypatch.setattr(
        dfe.trials_lookup, "fetch_trial_outcome_summary",
        lambda *a, **k: {"status": "ok", "records": [{"summary": None, "note": "Results have not been posted."}]},
    )
    assert dfe._trials_finding_and_url(envelope, "Drug A", "Drug B", cache_dir=tmp_path, refresh=False, memory_path=tmp_path / "m.json") == (None, None)


def test_bare_and_between_two_drugs_is_not_combination_wording():
    # Real case: "aripiprazole and haloperidol" given to separate groups in an animal study.
    separate = _sections(results="Drug a and drug b were each given to separate groups; drug b increased immobility.")
    assert pubmed_lookup.describes_genuine_combination(separate, "Drug A", "Drug B") is False


@pytest.mark.parametrize(
    ("text", "want"),
    [
        # Silence is not evidence: a source that merely raises no concern resolves nothing.
        ("The label mentions this combination without strong caution language.", None),
        ("There is no risk information for this pair.", None),
        # ...but an affirmative, evaluated outcome still counts.
        ("There were no adverse events with the combination.", "positive"),
        ("Exposure did not increase with coadministration.", "positive"),
    ],
)
def test_negated_warning_words_are_silence_not_a_favorable_outcome(text, want):
    assert dfe._classify_text(text) == want


def test_web_excerpt_that_is_only_the_page_title_and_navigation_is_rejected(monkeypatch, tmp_path):
    # Real case: the page's own title + nav text was published as the Finding.
    title = "Caution in co-administration of drug a and drug b in patients with learning disability"
    page = f"{title} - Example Journal online --> --> About Us . Paper Submission ."
    envelope = {"status": "ok", "result": {"records": [{"title": f"{title} - Example Journal", "url": "https://example.org/p"}]}}
    monkeypatch.setattr(
        dfe.search_tools, "fetch_general_page_text",
        lambda *a, **k: {"status": "ok", "records": [{"page_text": page}]},
    )
    assert dfe._tavily_finding_and_url(envelope, cache_dir=tmp_path, refresh=False, memory_path=tmp_path / "m.json") == (None, None)


# --- Keyword accuracy (audit of the real report's classifier inputs) -----------------


@pytest.mark.parametrize(
    "text",
    [
        "Atomoxetine is approved for attention-deficit/hyperactivity disorder in children.",  # not "deficit"
        "Long-term potentiation in the hippocampus was measured.",  # not "potentiat"
        "Selective serotonin reuptake inhibitors are first-line antidepressants.",  # class name, not a mechanism
        "The study enrolled 40 patients with major depressive disorder.",
        "Levels of evidence were graded by two reviewers.",  # "levels" alone is not a drug level
        "Plasma samples were collected at baseline.",
    ],
)
def test_new_keywords_do_not_fire_on_benign_wording(text):
    assert dfe._classify_text(text) is None


@pytest.mark.parametrize(
    ("text", "want"),
    [
        # Interaction outcomes the original keyword list missed.
        ("This case report highlights an interaction resulting in subtherapeutic levels of the first drug.", "negative"),
        ("Monitor for signs and symptoms of serotonin syndrome when these are combined.", "negative"),
        ("QT prolongation was dose-related with the combination.", "negative"),
        ("Memory deficits induced by the first drug were assessed.", "negative"),
        # A stated mechanism of interaction (SKILL.md Part 2).
        ("The interaction occurs primarily through hepatic enzyme induction.", "negative"),
        ("Drug X is a strong CYP3A4 inhibitor.", "negative"),
        ("The SSRIs inhibit P450 2D6, though the extent of inhibition varies.", "negative"),
        # A stated dosing consequence.
        ("Dosage adjustment of drug Y may be necessary if used concomitantly with drug X.", "negative"),
        # A drug-level change is unambiguous whichever word order is used.
        ("Exposure to drug Y could increase if drug X is given.", "negative"),
        ("Coadministration reduced plasma concentrations of drug Y.", "negative"),
        # Favorable results.
        ("There was no significant difference in plasma levels between the groups.", "positive"),
        ("Response to treatment was significantly better with the combination.", "positive"),
        ("Remission was achieved in most patients receiving both drugs.", "positive"),
    ],
)
def test_keyword_audit_cases(text, want):
    assert dfe._classify_text(text) == want


def test_curated_documented_mechanism_is_negative_but_still_never_positive():
    assert dfe._classify_text("Drug X is a strong CYP3A4 inhibitor.", allow_positive=False) == "negative"
    assert dfe._classify_text("There was no significant difference in plasma levels.", allow_positive=False) is None


# --- Part 4 "Curated-interaction override" -------------------------------------------

_LABEL_INTERACTION = {"mentioned": True, "excerpt": "Coadministration increased exposure of the subject drug by 60%.", "url": "https://dailymed.example/1", "note": None}


def test_curated_documented_interaction_resolves_before_favorable_literature():
    result = dfe._pair_finding(
        _NO_FLOCKHART, [], _LABEL_INTERACTION, _NO_MENTION, "The combination was well tolerated and effective.", "u", None, None, None, None,
    )
    assert result[0] == "negative" and result[4].startswith("DailyMed label")


def test_pharmacodynamic_rule_and_flockhart_also_resolve_before_literature():
    rule = [{"mechanism": "Additive CNS depression.", "expected_consequence": "Increased sedation."}]
    result = dfe._pair_finding(_NO_FLOCKHART, rule, _NO_MENTION, _NO_MENTION, "The combination was well tolerated.", "u", None, None, None, None)
    assert result[0] == "negative" and result[4] == "Pharmacodynamic interaction reference"


# --- Part 4 "Exactly-two-drugs rule" -------------------------------------------------

_KNOWN = frozenset({"drug a", "drug b", "drug c", "citalopram", "escitalopram"})


def test_third_drug_detection_is_word_bounded():
    assert dfe._third_drug_in("Patients took drug a with drug b and drug c.", "Drug A", "Drug B", _KNOWN) == "drug c"
    assert dfe._third_drug_in("Patients took drug a with drug b.", "Drug A", "Drug B", _KNOWN) is None
    # an enantiomer's name contains the racemate's name; that alone is not a third drug
    assert dfe._third_drug_in("Escitalopram combined with drug b.", "Escitalopram", "Drug B", _KNOWN) is None


_KNOWN_WITH_ALIASES = frozenset({"valproic acid", "valproate", "divalproex", "fluoxetine", "ethosuximide", "carbamazepine", "lamotrigine"})


def test_a_drugs_other_names_are_the_same_drug_not_a_third():
    # The pair is "valproic acid" + fluoxetine; "valproate" in the text is the pair's own drug.
    text = "Fluoxetine was combined with valproate and divalproex sodium in the study."
    assert dfe._third_drug_in(text, "Valproic Acid", "Fluoxetine", _KNOWN_WITH_ALIASES) is None


def test_an_alternate_name_of_a_third_drug_is_recognised():
    # Real case: a pair (fluoxetine, ethosuximide) whose study also used VALPROATE.
    text = "Effects of fluoxetine on the anticonvulsant action of valproate and ethosuximide."
    assert dfe._third_drug_in(text, "Fluoxetine", "Ethosuximide", _KNOWN_WITH_ALIASES) == "valproate"


def test_standard_abbreviations_are_recognised_but_only_as_whole_uppercase_words():
    assert dfe._third_drug_in("FXT, ETX and VPA were given.", "Fluoxetine", "Ethosuximide", _KNOWN_WITH_ALIASES) == "valproic acid"
    # the pair's own abbreviation is not a third drug
    assert dfe._third_drug_in("FXT and ETX were given.", "Fluoxetine", "Ethosuximide", _KNOWN_WITH_ALIASES) is None
    # lowercase / embedded letters are ordinary words, not abbreviations
    assert dfe._third_drug_in("the vpa signal was weak; ETXs", "Fluoxetine", "Ethosuximide", _KNOWN_WITH_ALIASES) is None


def test_web_source_whose_title_names_a_third_drug_does_not_count(monkeypatch, tmp_path):
    title = "Effects of fluoxetine on the anticonvulsant action of valproate and ethosuximide"
    page = "Motor coordination deficits induced by fluoxetine combined with ethosuximide were assessed in the chimney test in mice and rats."
    envelope = {"status": "ok", "result": {"records": [{"title": title, "url": "https://example.org/p"}]}}
    monkeypatch.setattr(dfe.search_tools, "fetch_general_page_text", lambda *a, **k: {"status": "ok", "records": [{"page_text": page}]})
    args = dict(cache_dir=tmp_path, refresh=False, memory_path=tmp_path / "m.json")
    # With the pair's names and the known list, the title's third drug disqualifies the page...
    assert dfe._tavily_finding_and_url(envelope, display_a="Fluoxetine", display_b="Ethosuximide", known_drugs=_KNOWN_WITH_ALIASES, **args) == (None, None)
    # ...and without them (an unscreened caller) behaviour is unchanged.
    assert dfe._tavily_finding_and_url(envelope, **args)[0] is not None


def test_literature_involving_a_third_drug_does_not_count_and_the_pair_is_excluded():
    three_drug_case = "Overdose of drug a and drug b combined with drug c caused prolonged CNS depression."
    result = dfe._pair_finding(
        _NO_FLOCKHART, [], _NO_MENTION, _NO_MENTION, three_drug_case, "u", None, None, None, None,
        display_a="Drug A", display_b="Drug B", known_drugs=_KNOWN,
    )
    assert result is None


def test_a_three_drug_source_falls_through_to_the_next_source():
    result = dfe._pair_finding(
        _NO_FLOCKHART, [], _NO_MENTION, _NO_MENTION,
        "Drug a plus drug b plus drug c worsened sedation.", "u1", None, None,
        "Drug a with drug b was well tolerated.", "u2",
        display_a="Drug A", display_b="Drug B", known_drugs=_KNOWN,
    )
    assert result[0] == "positive" and result[4] == "General web search (Tavily)"


def test_curated_sources_are_not_subject_to_the_two_drug_rule():
    # A label's interaction text routinely names a drug class and its example members.
    mention = {"mentioned": True, "excerpt": "Inducers such as drug b and drug c decrease drug a exposure.", "url": "u", "note": None}
    result = dfe._pair_finding(
        _NO_FLOCKHART, [], mention, _NO_MENTION, None, None, None, None, None, None,
        display_a="Drug A", display_b="Drug B", known_drugs=_KNOWN,
    )
    assert result[0] == "negative"


def test_pubmed_study_with_a_third_drug_in_methods_does_not_count(monkeypatch, tmp_path):
    def abstract(sections):
        return lambda *a, **k: {"status": "ok", "records": [{"sections": sections}]}

    envelope = {"status": "ok", "records": [{"pmid": "1", "title": "A study."}]}
    args = dict(cache_dir=tmp_path, refresh=False, memory_path=tmp_path / "m.json", display_a="Drug A", display_b="Drug B", known_drugs=_KNOWN)

    monkeypatch.setattr(dfe.pubmed_lookup, "fetch_abstract_sections", abstract({
        "METHODS": "Patients on drug c received drug a combined with drug b.", "RESULTS": "Symptoms improved.",
    }))
    assert dfe._pubmed_finding_and_url(envelope, **args) == (None, None)

    # A third drug mentioned only in Background (prior work) does not disqualify the study.
    monkeypatch.setattr(dfe.pubmed_lookup, "fetch_abstract_sections", abstract({
        "BACKGROUND": "Drug c is widely used.", "METHODS": "Patients received drug a combined with drug b.",
        "RESULTS": "Symptoms improved.",
    }))
    assert dfe._pubmed_finding_and_url(envelope, **args)[0] == "Symptoms improved."


# --- Part 5: evidence status tags -----------------------------------------------------


@pytest.mark.parametrize(
    ("label", "want"),
    [
        ("Curated DDI (Flockhart Cytochrome P450 Drug Interaction Table)", "curated"),
        ("Pharmacodynamic interaction reference", "curated"),
        ("DailyMed label (Drug Interactions section)", "curated"),
        ("External evidence (PubMed)", "inferred"),
        ("ClinicalTrials.gov", "inferred"),
        ("General web search (Tavily)", "inferred"),
    ],
)
def test_evidence_status_tags(label, want):
    assert dfe.evidence_status(label) == want


def test_tavily_is_never_labelled_curated_even_beside_a_curated_source():
    assert dfe.evidence_status(dfe._TAVILY_EVIDENCE_LABEL) == "inferred"
    mention = {"mentioned": True, "excerpt": "Coadministration increased exposure by 60%.", "url": "u", "note": None}
    result = dfe._pair_finding(_NO_FLOCKHART, [], mention, _NO_MENTION, None, None, None, None, "Drug A combined with Drug B increased exposure.", "u2")
    assert "General web search (Tavily)" in result[2]  # listed as evidence...
    assert result[4].startswith("DailyMed label")  # ...but the curated label is the primary
