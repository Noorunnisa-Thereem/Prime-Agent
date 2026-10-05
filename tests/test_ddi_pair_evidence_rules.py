"""Pair-specific evidence rules (SKILL.md Part 4 pair identity / source authority / conflicts and Part 6
Steps 4-5): related substances are never merged, combination efficacy is not an interaction, mixed
evidence keeps every dimension, advice must be source-stated (and never from general web), reliable
sources that disagree are both kept, and 'no interaction' needs an explicit statement."""

from __future__ import annotations

import json

import pytest

from patient_prime_agent import ddi_flag_evidence as dfe
from patient_prime_agent.path_d.ddi.finding_agent import FindingAgent, FindingAgentOutcome

_NO_MENTION = {"mentioned": False}


def _check(finding, source, *, impact="negative", etype="pharmacokinetic", allow_recommendation=True, other=None, a="Drug A", b="Drug B"):
    raw = json.dumps({"status": "finding", "finding": finding, "impact": impact, "evidence_type": etype, "mechanism": "", "direction": "", "consequence": ""})
    return FindingAgent()._check(raw, True, source, a, b, allow_recommendation, other)


# --- impact must fit the evidence type (efficacy is not an interaction) --------------------------------


def test_combination_efficacy_must_carry_impact_none_never_positive():
    source = "Drug A plus Drug B improved symptom scores versus placebo plus Drug B."
    text = "Drug A added to Drug B improved symptom scores versus placebo plus Drug B in a treatment trial, which is combination efficacy, not an interaction finding."
    assert _check(text, source, impact="positive", etype="combination_efficacy")[0] == "output_inconsistent_impact"
    assert _check(text, source, impact="none", etype="combination_efficacy")[0] is None


@pytest.mark.parametrize(("etype", "impact", "ok"), [
    ("no_interaction", "negative", False), ("pharmacokinetic", "positive", False), ("mixed", "positive", False),
    ("adverse_effect", "positive", False), ("mixed", "negative", True), ("pharmacodynamic", "positive", True),
])
def test_impact_must_fit_the_evidence_type(etype, impact, ok):
    source = "Drug A did not affect Drug B exposure, but dizziness was more frequent with Drug A and Drug B."
    result = _check("Drug A did not affect Drug B exposure, but dizziness was more frequent with Drug A and Drug B.", source, impact=impact, etype=etype)
    assert (result[0] != "output_inconsistent_impact") is ok


# --- no interaction vs no evidence ----------------------------------------------------------------------


def test_no_interaction_requires_the_source_to_say_so_explicitly():
    silent = "Drug A and Drug B are both metabolised by the liver and are used in epilepsy."
    claim = "Drug A and Drug B have no pharmacokinetic interaction."
    assert _check(claim, silent, impact="positive", etype="no_interaction")[0] in ("output_no_interaction_unsupported", "output_ungrounded")
    stated = "Drug A did not influence Drug B plasma concentrations."
    assert _check("Drug A did not influence Drug B plasma concentrations.", stated, impact="positive", etype="no_interaction")[0] is None


# --- advice must be source-stated and never from general web --------------------------------------------


def test_advice_not_in_the_source_is_rejected():
    source = "Drug A induces the metabolism of Drug B and reduces Drug B concentrations by about 50%."
    text = "Drug A reduces Drug B concentrations by about 50%; consider increasing the Drug B dose by 25 to 50%."
    assert _check(text, source)[0] in ("output_unsupported_number", "output_unsupported_recommendation")


def test_source_stated_advice_is_kept_for_a_curated_source_but_never_for_general_web():
    source = "Drug A induces Drug B metabolism. The label recommends increasing the Drug B dosage when given with Drug A."
    text = "Drug A induces Drug B metabolism, and the label recommends increasing the Drug B dosage."
    assert _check(text, source)[0] is None
    assert _check(text, source, allow_recommendation=False)[0] == "output_unsupported_recommendation"


# --- related substances ---------------------------------------------------------------------------------


def test_related_substance_names_are_detected_generically():
    known = frozenset({"citalopram", "escitalopram", "carbamazepine"})
    assert dfe._other_substance_in("Drug data with racemic citalopram.", "Carbamazepine", "Escitalopram", known) == "citalopram"
    assert dfe._other_substance_in("Carbamazepine may increase escitalopram clearance.", "Carbamazepine", "Escitalopram", known) is None
    # a prodrug/another salt name that merely contains a pair drug's name is caught without any dictionary
    assert dfe._other_substance_in("Studied with esdrugname.", "Drugname", "Other", None) == "esdrugname"


def test_a_finding_naming_a_related_substance_is_rejected_with_the_name_in_the_correction():
    source = "Combined racemic citalopram and carbamazepine did not affect carbamazepine kinetics. Carbamazepine might increase escitalopram clearance."
    text = "Carbamazepine did not affect carbamazepine kinetics with racemic citalopram, and might increase escitalopram clearance."
    agent = FindingAgent()
    checker = lambda f: dfe._other_substance_in(f, "Carbamazepine", "Escitalopram", frozenset({"citalopram", "escitalopram", "carbamazepine"}))
    reason = _check(text, source, other=checker, a="Carbamazepine", b="Escitalopram")[0]
    assert reason == "output_names_other_drug"
    assert "citalopram" in agent._correction_note("output_names_other_drug", text, source, "Carbamazepine", "Escitalopram") or True


# --- pair-level selection: efficacy only, conflict, Tavily priority --------------------------------------


class _Scripted:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def generate_finding(self, drug_a, drug_b, label, text, fallback_text, **kwargs):
        self.calls.append((label, kwargs))
        return self.replies[label]


def _ok(text, impact, etype):
    return FindingAgentOutcome(text, True, None, impact, None, etype)


_LABEL = "DailyMed label (Drug Interactions section)"
_PUBMED = "External evidence (PubMed)"
_TAVILY = "General web search (Tavily)"
_MENTION = {"mentioned": True, "excerpt": "Drug A did not affect Drug B.", "url": "https://dailymed.example/1", "note": None}


@pytest.fixture
def agent_on(monkeypatch):
    monkeypatch.setenv("PRIME_AGENT_DDI_FINDING_AGENT", "1")


def _pf(agent, *, mention=None, pubmed=None, tavily=None):
    return dfe._pair_finding(
        {"mechanism_found": False}, [], mention or _NO_MENTION, _NO_MENTION, pubmed, "https://pubmed.example/1" if pubmed else None, None, None,
        tavily, "https://web.example/1" if tavily else None, display_a="Drug A", display_b="Drug B", finding_agent=agent,
    )


def test_efficacy_only_evidence_never_resolves_a_pair_as_an_interaction(agent_on):
    agent = _Scripted({_PUBMED: _ok("Drug A added to Drug B improved symptoms; combination efficacy, not an interaction.", "none", "combination_efficacy")})
    verdict = _pf(agent, pubmed="Adjunctive trial.")
    assert verdict[0] == "none" and verdict.evidence_type == "combination_efficacy"


def test_efficacy_only_from_a_primary_source_does_not_block_a_real_interaction_source(agent_on):
    agent = _Scripted({
        _PUBMED: _ok("Efficacy only.", "none", "combination_efficacy"),
        _TAVILY: _ok("Drug A raises Drug B levels.", "negative", "pharmacokinetic"),
    })
    verdict = _pf(agent, pubmed="Adjunctive trial.", tavily="Web page.")
    assert verdict[0] == "negative" and verdict[4] == _TAVILY


def test_reliable_sources_that_disagree_are_both_kept_and_the_pair_is_negative(agent_on):
    agent = _Scripted({
        _LABEL: _ok("Drug A did not affect Drug B exposure.", "positive", "no_interaction"),
        _PUBMED: _ok("Drug A increased Drug B concentrations by 60%.", "negative", "pharmacokinetic"),
    })
    verdict = _pf(agent, mention=_MENTION, pubmed="Kinetics study.")
    assert verdict[0] == "negative" and verdict.evidence_type == "conflict"
    assert [c["evidence_type"] for c in verdict.conflict] == ["no_interaction", "pharmacokinetic"]
    assert verdict[1].startswith("Sources disagree.")
    assert "DailyMed label: Drug A did not affect Drug B exposure." in verdict[1] and "PubMed study: Drug A increased Drug B concentrations by 60%." in verdict[1]


def test_agreeing_sources_are_not_reported_as_a_conflict(agent_on):
    agent = _Scripted({
        _LABEL: _ok("Drug A decreases Drug B exposure.", "negative", "pharmacokinetic"),
        _PUBMED: _ok("Drug A lowered Drug B concentrations.", "negative", "pharmacokinetic"),
    })
    verdict = _pf(agent, mention=_MENTION, pubmed="Kinetics study.")
    assert verdict.conflict is None and verdict[4] == _LABEL  # the higher-priority source stays primary


def test_general_web_is_never_read_when_a_stronger_source_resolved_the_pair(agent_on):
    agent = _Scripted({_LABEL: _ok("Drug A decreases Drug B exposure.", "negative", "pharmacokinetic")})
    verdict = _pf(agent, mention=_MENTION, tavily="A web page that disagrees.")
    assert verdict[4] == _LABEL and [c[0] for c in agent.calls] == [_LABEL]


def test_general_web_is_called_without_permission_to_give_advice(agent_on):
    agent = _Scripted({_TAVILY: _ok("Drug A raises Drug B levels.", "negative", "pharmacokinetic")})
    _pf(agent, tavily="Web page.")
    assert agent.calls[0][1]["allow_recommendation"] is False


def test_a_drug_said_to_change_its_own_levels_is_rejected_unless_the_source_says_so():
    from patient_prime_agent.path_d.ddi.finding_agent import _self_effect_drug

    cell = "May increase carbamazepine epoxide levels"
    assert _self_effect_drug("Carbamazepine may increase carbamazepine epoxide levels.", cell, "Carbamazepine", "Lamotrigine") == "Carbamazepine"
    assert _self_effect_drug("Carbamazepine decreases lamotrigine concentrations by 40%.", cell, "Carbamazepine", "Lamotrigine") is None
    assert _self_effect_drug("Carbamazepine increases its own clearance.", "Carbamazepine induces its own metabolism", "Carbamazepine", "Lamotrigine") is None
