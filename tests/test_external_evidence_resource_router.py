"""Tests for the clinical-question resource router added to
external_evidence_summary.py: classify_question() (pure routing logic) and
answer_clinical_question() (the actual call-only-what's-relevant dispatcher).

No test here makes a real network call -- every per-source lookup function
is monkeypatched, exactly like test_external_lookup.py's convention.
"""

from __future__ import annotations

import pytest

from patient_prime_agent import external_evidence_summary as ees


# ---------------------------------------------------------------------------
# classify_question: routes each of the 9 canonical question types to
# exactly the implemented-source subset the task specifies, and leaves
# genuinely out-of-scope / unrecognized questions with no sources at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question_text, expected_sources",
    [
        ("Does patient genetics change interpretation", ("clinvar", "cpic")),
        ("Could two therapies interact", ("flockhart", "pharmacodynamic_rules", "pubmed_pair")),
        ("What established safety evidence exists", ("dailymed",)),
        ("What has been studied in people", ("clinicaltrials", "pubmed_single")),
    ],
)
def test_classify_question_routes_in_scope_types_to_the_correct_source_subset(question_text, expected_sources):
    classification = ees.classify_question(question_text)
    assert classification["recognized"] is True
    assert classification["in_scope"] is True
    assert classification["sources"] == expected_sources


@pytest.mark.parametrize(
    "question_text",
    [
        "What does the drug act on",
        "Which biological systems are affected",
        "Is the mechanism relevant in brain",
        "What changes after exposure",
        "Is structural support available",
    ],
)
def test_classify_question_marks_unimplemented_question_types_out_of_scope(question_text):
    classification = ees.classify_question(question_text)
    assert classification["recognized"] is True
    assert classification["in_scope"] is False
    assert classification["sources"] == ()
    # Every preferred resource for an out-of-scope row is also its own uncovered set --
    # nothing in that row is implemented.
    assert classification["uncovered_resources"] == classification["preferred_resources"]
    assert classification["uncovered_resources"] != ()


def test_classify_question_reports_uncovered_resources_for_partially_covered_types():
    genetics = ees.classify_question("Does patient genetics change interpretation")
    assert "Genomics England PanelApp" in genetics["uncovered_resources"]
    assert "PharmGKB" in genetics["uncovered_resources"]
    assert "ClinVar" not in genetics["uncovered_resources"]

    interaction = ees.classify_question("Could two therapies interact")
    assert interaction["uncovered_resources"] == ("SuperCYPsPred",)

    safety = ees.classify_question("What established safety evidence exists")
    assert set(safety["uncovered_resources"]) == {"Drugs@FDA", "Tox21", "EPA CompTox"}

    studied = ees.classify_question("What has been studied in people")
    assert studied["uncovered_resources"] == ("OHDSI/OMOP",)


def test_classify_question_unrecognized_text_is_out_of_scope_without_guessing():
    classification = ees.classify_question("zzz not a real clinical question zzz")
    assert classification["recognized"] is False
    assert classification["in_scope"] is False
    assert classification["matched_route"] is None
    assert classification["sources"] == ()


def test_classify_question_handles_empty_and_non_string_input_without_crashing():
    assert ees.classify_question("")["in_scope"] is False
    assert ees.classify_question("   ")["in_scope"] is False
    assert ees.classify_question(None)["in_scope"] is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# answer_clinical_question: out-of-scope / unrecognized questions never call
# any lookup at all; in-scope questions call ONLY their mapped sources.
# ---------------------------------------------------------------------------


def _fail_if_called(name):
    def _fn(*args, **kwargs):
        raise AssertionError(f"{name} must not be called for this question")

    return _fn


def test_answer_clinical_question_out_of_scope_makes_no_lookup_call_at_all(monkeypatch):
    for mod, attr in [
        (ees.clinvar_lookup, "search_clinvar_by_gene"),
        (ees.cpic_lookup, "lookup_cpic_pair"),
        (ees.dailymed_lookup, "search_dailymed"),
        (ees.trials_lookup, "search_trials"),
        (ees.pubmed_lookup, "search_pubmed"),
        (ees.pubmed_lookup, "search_pubmed_drug_pair"),
    ]:
        monkeypatch.setattr(mod, attr, _fail_if_called(f"{mod.__name__}.{attr}"))

    result = ees.answer_clinical_question("What does the drug act on", drug_name="lamotrigine")

    assert result["status"] == "out_of_scope"
    assert result["sources_called"] == []
    assert result["results"] == {}
    assert "Known Limitations" in result["note"]


def test_answer_clinical_question_unrecognized_text_makes_no_lookup_call_at_all(monkeypatch):
    monkeypatch.setattr(ees.pubmed_lookup, "search_pubmed", _fail_if_called("pubmed_lookup.search_pubmed"))
    result = ees.answer_clinical_question("this is gibberish not a mapped question")
    assert result["status"] == "out_of_scope"
    assert result["matched_route"] is None
    assert "Known Limitations" in result["note"]


def test_answer_clinical_question_genetics_only_calls_clinvar_and_cpic(monkeypatch):
    calls = []
    monkeypatch.setattr(ees.clinvar_lookup, "search_clinvar_by_gene", lambda gene, **kw: calls.append(("clinvar", gene)) or {"status": "ok", "records": []})
    monkeypatch.setattr(ees.cpic_lookup, "lookup_cpic_pair", lambda gene, drug, **kw: calls.append(("cpic", gene, drug)) or {"status": "ok", "records": []})
    for mod, attr in [
        (ees.dailymed_lookup, "search_dailymed"),
        (ees.trials_lookup, "search_trials"),
        (ees.pubmed_lookup, "search_pubmed"),
        (ees.pubmed_lookup, "search_pubmed_drug_pair"),
    ]:
        monkeypatch.setattr(mod, attr, _fail_if_called(f"{mod.__name__}.{attr}"))

    result = ees.answer_clinical_question(
        "Does patient genetics change interpretation", gene_symbol="SCN1A", drug_name="lamotrigine"
    )

    assert result["status"] == "ok"
    assert set(result["sources_called"]) == {"clinvar", "cpic"}
    assert {c[0] for c in calls} == {"clinvar", "cpic"}
    assert "PanelApp" in result["note"] or "uncovered" not in result  # partial-coverage note present


def test_answer_clinical_question_drug_interaction_only_calls_flockhart_pd_rules_and_pubmed_pair(monkeypatch):
    calls = []
    monkeypatch.setattr(ees.reference_data, "pharmacokinetic_pair_summary", lambda a, b: calls.append(("flockhart", a, b)) or {"mechanism_found": False, "statement": "x", "evidence": []})
    monkeypatch.setattr(ees.pharmacodynamic_rules, "evaluate_pair", lambda a, b: calls.append(("pd_rules", a, b)) or [])
    monkeypatch.setattr(ees.pubmed_lookup, "search_pubmed_drug_pair", lambda a, b, **kw: calls.append(("pubmed_pair", a, b)) or {"status": "no_results", "records": []})
    for mod, attr in [
        (ees.clinvar_lookup, "search_clinvar_by_gene"),
        (ees.cpic_lookup, "lookup_cpic_pair"),
        (ees.dailymed_lookup, "search_dailymed"),
        (ees.trials_lookup, "search_trials"),
        (ees.pubmed_lookup, "search_pubmed"),
    ]:
        monkeypatch.setattr(mod, attr, _fail_if_called(f"{mod.__name__}.{attr}"))

    result = ees.answer_clinical_question("Could two therapies interact", drug_a="lamotrigine", drug_b="levetiracetam")

    assert result["status"] == "ok"
    assert set(result["sources_called"]) == {"flockhart", "pharmacodynamic_rules", "pubmed_pair"}
    assert {c[0] for c in calls} == {"flockhart", "pd_rules", "pubmed_pair"}


def test_answer_clinical_question_safety_evidence_only_calls_dailymed(monkeypatch):
    calls = []
    monkeypatch.setattr(ees.dailymed_lookup, "search_dailymed", lambda name, **kw: calls.append(name) or {"status": "ok", "records": []})
    for mod, attr in [
        (ees.clinvar_lookup, "search_clinvar_by_gene"),
        (ees.cpic_lookup, "lookup_cpic_pair"),
        (ees.trials_lookup, "search_trials"),
        (ees.pubmed_lookup, "search_pubmed"),
        (ees.pubmed_lookup, "search_pubmed_drug_pair"),
    ]:
        monkeypatch.setattr(mod, attr, _fail_if_called(f"{mod.__name__}.{attr}"))

    result = ees.answer_clinical_question("What established safety evidence exists", drug_name="lamotrigine")

    assert result["status"] == "ok"
    assert result["sources_called"] == ["dailymed"]
    assert calls == ["lamotrigine"]


def test_answer_clinical_question_studied_in_people_only_calls_clinicaltrials_and_pubmed(monkeypatch):
    calls = []
    monkeypatch.setattr(ees.trials_lookup, "search_trials", lambda cond, drug=None, **kw: calls.append(("trials", cond, drug)) or {"status": "ok", "records": []})
    monkeypatch.setattr(ees.pubmed_lookup, "search_pubmed", lambda term, **kw: calls.append(("pubmed", term)) or {"status": "ok", "records": []})
    for mod, attr in [
        (ees.clinvar_lookup, "search_clinvar_by_gene"),
        (ees.cpic_lookup, "lookup_cpic_pair"),
        (ees.dailymed_lookup, "search_dailymed"),
        (ees.pubmed_lookup, "search_pubmed_drug_pair"),
    ]:
        monkeypatch.setattr(mod, attr, _fail_if_called(f"{mod.__name__}.{attr}"))

    result = ees.answer_clinical_question("What has been studied in people", drug_name="lamotrigine", condition="epilepsy")

    assert result["status"] == "ok"
    assert set(result["sources_called"]) == {"clinicaltrials", "pubmed_single"}
    assert ("trials", "epilepsy", "lamotrigine") in calls
    assert ("pubmed", "lamotrigine") in calls


def test_answer_clinical_question_reports_missing_required_input_instead_of_crashing_or_guessing(monkeypatch):
    monkeypatch.setattr(ees.dailymed_lookup, "search_dailymed", _fail_if_called("dailymed_lookup.search_dailymed"))

    result = ees.answer_clinical_question("What established safety evidence exists")  # no drug_name given

    assert result["status"] == "ok"  # the question itself is in-scope; the missing input is per-source
    assert result["results"]["dailymed"]["status"] == "missing_required_input"
    assert "drug_name" in result["results"]["dailymed"]["required"]
