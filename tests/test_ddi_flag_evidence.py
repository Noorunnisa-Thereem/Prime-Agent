"""Tests for patient_prime_agent.ddi_flag_evidence -- the live multi-source
lookup layer backing the Drug Interaction Flag Sheet's per-drug
Positive/Negative Impact tables (report_html.py's per-drug renderer).

Never a real network call, and never a real Tavily API call even if a
real TAVILY_API_KEY happens to be configured in this environment's .env
(see ``_mock_network`` -- it forces ``search_tools._get_api_key`` to
return ``None`` unless a test explicitly opts into a fake key). Same
monkeypatch-the-HTTP-layer convention as test_external_lookup.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from patient_prime_agent import ddi_flag_evidence
from patient_prime_agent.path_d.ddi import candidate_drugs, reference_data
from patient_prime_agent.path_d.ddi.finding_agent import FindingAgentOutcome
from patient_prime_agent.external_lookup import dailymed_lookup, pubmed_lookup, search_tools, trials_lookup

# A DailyMed "not mentioned" result, for tests exercising _pair_finding directly without
# caring about the DailyMed-mention candidates.
_NO_MENTION = {"mentioned": False, "excerpt": None, "url": None, "note": "not mentioned in reviewed label section"}


@dataclass(slots=True)
class _StubFindingAgent:
    """A ``FindingAgent``-shaped double for exercising ``_pair_finding``'s own
    fall-through contract (Part 1 of the NO_FINDING fix) in isolation from
    FindingAgent's real LLM-calling machinery, which is unit-tested
    separately in test_finding_agent.py. ``replies`` maps an evidence_label
    (exactly what ``_pair_finding`` builds, e.g. "External evidence
    (PubMed)") to the ``FindingAgentOutcome`` that candidate's call should
    receive."""

    replies: dict[str, FindingAgentOutcome]
    calls: list[tuple[str, str, str, str]] = field(default_factory=list)

    def generate_finding(self, drug_a, drug_b, source_label, resource_text, fallback_text):
        self.calls.append((drug_a, drug_b, source_label, resource_text))
        return self.replies[source_label]


def _clinical_notes() -> dict:
    return {
        "clinical_inference": {
            "medication_response": {
                "current_regimen": [
                    {"drug": "Lamotrigine", "dose": "150mg BID"},
                    {"drug": "Levetiracetam (Keppra)", "dose": "1000mg BID"},
                ]
            }
        }
    }


def _fake_fetch_json(body_by_url_prefix: dict[str, object]):
    def _fake(url: str, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, json_body=None, extra_headers=None):
        for prefix, body in body_by_url_prefix.items():
            if url.startswith(prefix):
                return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}
        return {
            "url": url,
            "http_status": 200,
            "body": {"esearchresult": {"idlist": []}, "data": [], "studies": [], "totalCount": 0, "results": []},
            "retrieved_at": "2026-01-01T00:00:00Z",
            "from_cache": False,
            "error": None,
        }

    return _fake


def _fake_fetch_text(body_by_url_prefix: dict[str, str] | None = None):
    body_by_url_prefix = body_by_url_prefix or {}

    def _fake(url: str, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, accept="*/*"):
        for prefix, body in body_by_url_prefix.items():
            if url.startswith(prefix):
                return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}
        return {"url": url, "http_status": 404, "body": None, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": "HTTP 404: Not Found"}

    return _fake


def _mock_network(
    monkeypatch,
    *,
    pubmed_bodies: dict[str, object] | None = None,
    pubmed_abstract_bodies: dict[str, str] | None = None,
    trials_bodies: dict[str, object] | None = None,
    dailymed_pointer_bodies: dict[str, object] | None = None,
    dailymed_xml_bodies: dict[str, str] | None = None,
    tavily_key: bool = False,
    tavily_bodies: dict[str, object] | None = None,
    tavily_page_bodies: dict[str, str] | None = None,
):
    """Neutralize all 4 live pair-level sources for one build_report test --
    every one of PubMed (esearch/esummary AND efetch abstracts)/DailyMed
    (pointer+content)/ClinicalTrials.gov (search AND single-study results)/
    Tavily (search AND the independent real-page fetch -- see
    ``search_tools.fetch_general_page_text``) is mocked, regardless of
    whether the test cares about that source's output, so a test can never
    make a real network call or a real Tavily API call by omission.
    ``pubmed_lookup.fetch_abstract_sections`` uses ``fetch_text`` (efetch is
    XML), not ``fetch_json`` -- a real, observed gap: this mock didn't cover
    it before that function existed, which let a test make a genuine live
    efetch call by omission. ``tavily_page_bodies`` defaults to empty, which
    makes every page fetch a clean, honest HTTP 404 -- the correct safe
    default for a test that doesn't care about Tavily's Finding text, since
    it never falls back to title/snippet (see ``_tavily_finding_and_url``)."""
    monkeypatch.setattr(pubmed_lookup, "fetch_json", _fake_fetch_json(pubmed_bodies or {}))
    monkeypatch.setattr(pubmed_lookup, "fetch_text", _fake_fetch_text(pubmed_abstract_bodies or {}))
    monkeypatch.setattr(dailymed_lookup, "fetch_json", _fake_fetch_json(dailymed_pointer_bodies or {}))
    monkeypatch.setattr(dailymed_lookup, "fetch_text", _fake_fetch_text(dailymed_xml_bodies or {}))
    monkeypatch.setattr(trials_lookup, "fetch_json", _fake_fetch_json(trials_bodies or {}))
    monkeypatch.setattr(search_tools, "fetch_text", _fake_fetch_text(tavily_page_bodies or {}))
    if tavily_key:
        monkeypatch.setattr(search_tools, "_get_api_key", lambda env_var: "fake-test-key")
        monkeypatch.setattr(search_tools, "fetch_json", _fake_fetch_json(tavily_bodies or {}))
    else:
        monkeypatch.setattr(search_tools, "_get_api_key", lambda env_var: None)


# ---------------------------------------------------------------------------
# Screening list / pair generation
# ---------------------------------------------------------------------------


def test_screening_medications_merges_real_regimen_with_candidates_deduped():
    medications = ddi_flag_evidence._screening_medications(_clinical_notes())

    names = {m.normalized_name for m in medications}
    assert "lamotrigine" in names and "levetiracetam" in names
    # 2 real regimen drugs + all 24 candidates (none of the candidates collide by name).
    assert len(medications) == 2 + len(candidate_drugs.DDI_SCREENING_CANDIDATES)
    # The real regimen drugs must be marked "current", every candidate "proposed" --
    # never blurred together as if the patient were actually taking all 26.
    by_name = {m.normalized_name: m for m in medications}
    assert by_name["lamotrigine"].status == "current"
    assert by_name["levetiracetam"].status == "current"
    assert by_name["sertraline"].status == "proposed"


def test_unique_pairs_covers_every_combination_exactly_once():
    medications = ddi_flag_evidence._screening_medications(_clinical_notes())
    pairs = ddi_flag_evidence._unique_pairs(medications)

    n = len(medications)
    assert len(pairs) == n * (n - 1) // 2
    keys = {frozenset({a.normalized_name, b.normalized_name}) for a, b in pairs}
    assert len(keys) == len(pairs), "no pair should be generated twice"


def _medication(normalized_name: str, med_id: str, *, status: str = "proposed") -> "ddi_flag_evidence.Medication":
    from patient_prime_agent.path_d.ddi.normalizer import Medication

    return Medication(
        id=med_id,
        normalized_name=normalized_name,
        source_name=normalized_name.title(),
        dose_value=None,
        dose_unit=None,
        frequency=None,
        route=None,
        status=status,
        source_reference="test",
        reconciliation_flags=[],
    )


def test_unique_pairs_dedupes_even_when_the_input_list_itself_has_a_repeated_drug_name():
    # A real-world safeguard, not a currently-observed bug: _screening_medications never
    # actually produces two Medication records sharing a normalized_name today, but if a
    # future change to candidate_drugs.py or the regimen merge ever did, _unique_pairs must
    # still never generate the same unordered pair twice -- caught before any of the 4 live
    # sources are queried, not only at render time.
    medications = [
        _medication("lamotrigine", "med-01", status="current"),
        _medication("lamotrigine", "med-01-dup"),  # same drug, a second (buggy) record
        _medication("levetiracetam", "med-02", status="current"),
    ]
    pairs = ddi_flag_evidence._unique_pairs(medications)

    keys = [frozenset({a.normalized_name, b.normalized_name}) for a, b in pairs]
    assert len(keys) == len(set(keys)), "no pair should be generated twice"
    assert keys.count(frozenset({"lamotrigine", "levetiracetam"})) == 1


# ---------------------------------------------------------------------------
# dedupe_pairs_output -- one-time cleanup of an already-written report's pairs
# ---------------------------------------------------------------------------


def _pair_entry(drug_a: str, drug_b: str, finding: str) -> dict:
    return {"drug_a": drug_a, "drug_b": drug_b, "impact": "negative", "finding": finding, "evidence_labels": ["External evidence (PubMed)"]}


def test_dedupe_pairs_output_removes_reversed_and_re_generated_duplicates_keeping_the_first():
    pairs = [
        _pair_entry("Lamotrigine", "Levetiracetam", "first real finding"),
        _pair_entry("Aripiprazole", "Sertraline", "unrelated pair, kept"),
        _pair_entry("Levetiracetam", "Lamotrigine", "reversed duplicate of pair 1"),
        _pair_entry("lamotrigine", "levetiracetam", "same-case-insensitive duplicate of pair 1"),
    ]

    deduped, removed = ddi_flag_evidence.dedupe_pairs_output(pairs)

    assert len(deduped) == 2
    assert len(removed) == 2
    # The first occurrence is kept, verbatim -- not merged or rewritten.
    assert deduped[0]["finding"] == "first real finding"
    assert deduped[1]["finding"] == "unrelated pair, kept"
    assert {p["finding"] for p in removed} == {"reversed duplicate of pair 1", "same-case-insensitive duplicate of pair 1"}


def test_dedupe_pairs_output_is_a_no_op_when_there_are_no_duplicates():
    pairs = [_pair_entry("Lamotrigine", "Levetiracetam", "a"), _pair_entry("Aripiprazole", "Sertraline", "b")]
    deduped, removed = ddi_flag_evidence.dedupe_pairs_output(pairs)
    assert deduped == pairs
    assert removed == []


# ---------------------------------------------------------------------------
# Classification: PubMed title keywords, and the 3-source priority order.
# ---------------------------------------------------------------------------


def test_classify_text_recognizes_explicit_positive_language():
    assert ddi_flag_evidence._classify_text("Combination is generally well tolerated in adults.") == "positive"


def test_classify_text_recognizes_explicit_negative_language():
    assert ddi_flag_evidence._classify_text("Carbamazepine may decrease lamotrigine plasma concentrations.") == "negative"


def test_classify_text_returns_none_for_a_plain_topic_description():
    # Real PubMed titles are usually topic descriptions, not verdicts -- this must not be
    # force-classified either way; the caller (_pair_finding) decides what None means.
    assert ddi_flag_evidence._classify_text(
        "Effects of levetiracetam, carbamazepine, and phenytoin on presynaptic hippocampal channels."
    ) is None
    assert ddi_flag_evidence._classify_text(None) is None
    assert ddi_flag_evidence._classify_text("") is None


def test_classify_text_no_longer_treats_absence_of_interaction_as_positive_evidence():
    # Real fix: "no significant interaction"/"no clinically significant [interaction]"
    # were removed from _POSITIVE_KEYWORDS. Stating that no interaction was documented is
    # not the same as affirmatively stating a favorable efficacy/tolerability/benefit
    # outcome -- a source that never evaluated efficacy or tolerability could otherwise
    # resolve a pair Positive purely by stating an absence.
    assert ddi_flag_evidence._classify_text("No significant interaction was observed between the two drugs.") is None
    assert ddi_flag_evidence._classify_text("No clinically significant interaction was reported.") is None
    # Genuinely affirmative language must still classify Positive.
    assert ddi_flag_evidence._classify_text("The combination was well tolerated and generally safe.") == "positive"


@pytest.mark.parametrize(
    "text",
    [
        "Efficacy and tolerability of combination therapy.",  # the exact example from the bug report
        "The combination was generally effective.",
        "Improved seizure control with combination therapy.",
        "Clinical benefit observed with combined treatment.",
        "The regimen proved successful in most patients.",
        "Symptoms were manageable throughout the study.",
        "Levels remained compatible with continued dosing.",
    ],
)
def test_classify_text_recognizes_the_broadened_positive_fragments(text):
    # Before this fix, _POSITIVE_KEYWORDS only had 8 mostly-exact phrases ("well tolerated",
    # "safe and effective", ...) against _NEGATIVE_KEYWORDS' 14 broad fragments -- real
    # positive-sounding titles almost never matched. These fragment-style additions
    # ("effective", "efficacy", "tolerat", "improv", "benefit", "successful", "manageable",
    # "compatible") close that gap.
    assert ddi_flag_evidence._classify_text(text) == "positive"


def test_pair_finding_flockhart_finding_is_classified_by_its_own_text_not_hardcoded_negative():
    # Before this fix, ANY Flockhart mechanism_found=True was hard-coded to Negative Impact
    # regardless of what its statement actually said. Now it goes through the same
    # _classify_text as everything else -- it can resolve either Positive or Negative.
    negative_statement = {"mechanism_found": True, "statement": "Overlapping CYP3A4 pathway increases plasma levels and toxicity risk.", "evidence": []}
    result = ddi_flag_evidence._pair_finding(negative_statement, [], _NO_MENTION, _NO_MENTION, None, None, None, None, None, None)
    assert result[0] == "negative"

    positive_statement = {"mechanism_found": True, "statement": "Curated data shows the combination is compatible and generally well tolerated.", "evidence": []}
    result = ddi_flag_evidence._pair_finding(positive_statement, [], _NO_MENTION, _NO_MENTION, None, None, None, None, None, None)
    assert result[0] == "positive"

    # A Flockhart statement with no clear positive/negative language must not default to
    # negative, and there is no "needs_review" bucket to fall into either -- with no other
    # candidate present, the pair is simply unresolved.
    neutral_statement = {"mechanism_found": True, "statement": "Curated reference data shows an overlapping CYP pathway between drug A and drug B.", "evidence": []}
    result = ddi_flag_evidence._pair_finding(neutral_statement, [], _NO_MENTION, _NO_MENTION, None, None, None, None, None, None)
    assert result is None


def test_pair_finding_falls_through_to_the_next_candidate_when_the_higher_priority_one_is_ambiguous():
    # The core new behavior: an ambiguous Flockhart statement must not block a real,
    # classifiable PubMed title from resolving the pair -- it is skipped, not forced into a
    # verdict and not treated as if it were the only evidence available.
    neutral_statement = {"mechanism_found": True, "statement": "Curated reference data shows an overlapping CYP pathway between drug A and drug B.", "evidence": []}
    result = ddi_flag_evidence._pair_finding(
        neutral_statement, [], _NO_MENTION, _NO_MENTION, "Combination is well tolerated.", "https://pubmed.example/9", None, None, None, None
    )
    assert result is not None
    impact, finding, evidence_labels, hyperlink_url, primary_evidence_label = result
    assert impact == "positive"
    assert finding == "Combination is well tolerated."
    assert hyperlink_url == "https://pubmed.example/9"
    # Both candidates still show up in evidence_labels, even though only PubMed's text
    # resolved the pair -- and primary_evidence_label correctly names PubMed, not the
    # ambiguous, skipped Flockhart candidate that happens to be evidence_labels[0].
    assert len(evidence_labels) == 2
    assert primary_evidence_label == "External evidence (PubMed)"
    assert primary_evidence_label != evidence_labels[0]


def test_pair_finding_pharmacodynamic_rule_is_classified_by_its_own_text_not_hardcoded_negative():
    # Same fix applied to pharmacodynamic rules: a fired rule is no longer automatically
    # Negative Impact. In practice every rule in pharmacodynamic_rules.RULES today still
    # classifies Negative because its real expected_consequence text describes a risk (see
    # test_build_report_drops_pairs_with_no_evidence_and_keeps_real_pd_rule_pairs for that
    # real-data case) -- this test confirms the *mechanism*, using rule text that isn't.
    negative_rule = [{"mechanism": "Additive CNS depression.", "expected_consequence": "Increased sedation and impaired coordination."}]
    result = ddi_flag_evidence._pair_finding({"mechanism_found": False}, negative_rule, _NO_MENTION, _NO_MENTION, None, None, None, None, None, None)
    assert result[0] == "negative"

    positive_rule = [{"mechanism": "Overlapping receptor activity.", "expected_consequence": "Generally well tolerated in combination."}]
    result = ddi_flag_evidence._pair_finding({"mechanism_found": False}, positive_rule, _NO_MENTION, _NO_MENTION, None, None, None, None, None, None)
    assert result[0] == "positive"

    # No clear positive/negative language, and no other candidate present -> unresolved.
    neutral_rule = [{"mechanism": "Overlapping receptor activity.", "expected_consequence": "Both agents act on overlapping receptor systems."}]
    result = ddi_flag_evidence._pair_finding({"mechanism_found": False}, neutral_rule, _NO_MENTION, _NO_MENTION, None, None, None, None, None, None)
    assert result is None


def test_pair_finding_still_prioritizes_flockhart_then_pd_rules_then_dailymed_then_pubmed_then_trials_then_tavily():
    # The classification-by-source-alone rule was removed, but which source's TEXT becomes
    # the Finding is unchanged in spirit: curated sources outrank a real regulatory label
    # mention, which outranks literature, which outranks a trial listing, which outranks
    # general web search, when more than one source fires -- as long as that top candidate's
    # own text actually resolves clearly (an ambiguous one would fall through instead; see
    # test_pair_finding_falls_through_to_the_next_candidate_when_the_higher_priority_one_is_ambiguous).
    flockhart = {"mechanism_found": True, "statement": "Overlapping CYP3A4 pathway increases toxicity risk.", "evidence": []}
    pd_rules = [{"mechanism": "Additive CNS depression.", "expected_consequence": "Increased sedation."}]
    mention_a = {"mentioned": True, "excerpt": "Drug A's DailyMed label states: well tolerated.", "url": "https://dailymed.example/a", "note": None}
    result = ddi_flag_evidence._pair_finding(
        flockhart, pd_rules, mention_a, _NO_MENTION,
        "Well tolerated in combination.", "https://pubmed.example/1",
        "A real trial title.", "https://clinicaltrials.example/1",
        "A general web result.", "https://example.com/1",
    )
    assert result is not None
    impact, finding, evidence_labels, hyperlink_url, primary_evidence_label = result
    assert impact == "negative"
    assert finding == "Overlapping CYP3A4 pathway increases toxicity risk."
    assert hyperlink_url is None  # Flockhart's own candidate carries no URL
    assert any("Flockhart" in label or "Curated DDI" in label for label in evidence_labels)
    # All 6 candidate sources contributed evidence_labels even though only Flockhart's text
    # is the Finding -- and primary_evidence_label correctly names Flockhart here, matching
    # evidence_labels[0], since Flockhart's own text resolved the pair this time.
    assert len(evidence_labels) == 6
    assert primary_evidence_label == evidence_labels[0]


def test_pair_finding_pubmed_only_uses_the_titles_own_classification():
    positive = ddi_flag_evidence._pair_finding(
        {"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION, "Combination is well tolerated.", "https://pubmed.example/2", None, None, None, None
    )
    assert positive == (
        "positive", "Combination is well tolerated.", ["External evidence (PubMed)"],
        "https://pubmed.example/2", "External evidence (PubMed)",
    )

    negative = ddi_flag_evidence._pair_finding(
        {"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION, "May increase risk of toxicity.", None, None, None, None, None
    )
    assert negative[0] == "negative"


def test_pair_finding_pubmed_title_with_efficacy_and_tolerability_is_positive():
    # The exact reported bug: a positive-sounding real title fell through to Negative/
    # needs_review because the old keyword list required exact phrases like "well tolerated"
    # rather than fragments like "efficacy"/"tolerab".
    result = ddi_flag_evidence._pair_finding(
        {"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION,
        "Efficacy and tolerability of combination antiepileptic therapy: a systematic review.", None,
        None, None, None, None,
    )
    assert result is not None
    assert result[0] == "positive"


def test_pair_finding_excludes_a_retracted_citation_from_positive_classification():
    # Real, observed failure: broadening _POSITIVE_KEYWORDS made "RETRACTED: Efficacy and
    # safety of aripiprazole or bupropion augmentation..." match "Efficacy" and classify
    # Positive -- the retraction itself was invisible to the keyword classifier. A retracted
    # citation must never contribute to Positive OR Negative, regardless of what other
    # keywords its title contains. With nothing else to fall through to, the pair is simply
    # unresolved -- there is no "needs_review" bucket for it to land in instead.
    result = ddi_flag_evidence._pair_finding(
        {"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION,
        "RETRACTED: Efficacy and safety of aripiprazole or bupropion augmentation and switching in patients with treatment-resistant depression.", None,
        None, None, None, None,
    )
    assert result is None


def test_pair_finding_falls_through_a_retracted_citation_to_a_lower_priority_source():
    # A retracted PubMed title must not block a real ClinicalTrials.gov result from
    # resolving the pair -- it is skipped like any other unresolving candidate, not treated
    # as if the pair had no other evidence.
    result = ddi_flag_evidence._pair_finding(
        {"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION,
        "RETRACTED: Efficacy and safety of aripiprazole or bupropion augmentation.", "https://pubmed.example/retracted",
        "This trial found increased risk of adverse events.", "https://clinicaltrials.example/1",
        None, None,
    )
    assert result is not None
    impact, finding, evidence_labels, hyperlink_url, primary_evidence_label = result
    assert impact == "negative"
    assert finding == "This trial found increased risk of adverse events."
    assert hyperlink_url == "https://clinicaltrials.example/1"
    assert len(evidence_labels) == 2
    # The retracted PubMed candidate is still listed first in evidence_labels, but it must
    # never be named as the link's source -- ClinicalTrials.gov actually resolved the pair.
    assert primary_evidence_label == "ClinicalTrials.gov"
    assert primary_evidence_label != evidence_labels[0]


def test_pair_finding_dailymed_mention_becomes_the_finding_when_no_curated_source_fires():
    mention_a = {
        "mentioned": True,
        "excerpt": 'Aripiprazole\'s DailyMed label states: "the combination is well tolerated with lamotrigine when coadministered with aripiprazole."',
        "url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=abc",
        "note": None,
    }
    result = ddi_flag_evidence._pair_finding({"mechanism_found": False}, [], mention_a, _NO_MENTION, None, None, None, None, None, None)
    assert result is not None
    impact, finding, evidence_labels, hyperlink_url, primary_evidence_label = result
    assert "DailyMed label" in evidence_labels[0]
    assert "DailyMed label" in primary_evidence_label
    assert "lamotrigine" in finding.lower()
    assert hyperlink_url == "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=abc"


def test_pair_finding_tavily_is_lowest_priority_and_carries_the_distinct_label():
    result = ddi_flag_evidence._pair_finding(
        {"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION,
        None, None, None, None,
        "A general web page describing this combination as well tolerated.", "https://example.com/page",
    )
    assert result is not None
    impact, finding, evidence_labels, hyperlink_url, primary_evidence_label = result
    assert impact == "positive"
    assert finding == "A general web page describing this combination as well tolerated."
    assert evidence_labels == [ddi_flag_evidence._TAVILY_EVIDENCE_LABEL]
    assert "General web search" in evidence_labels[0]
    assert primary_evidence_label == ddi_flag_evidence._TAVILY_EVIDENCE_LABEL
    assert hyperlink_url == "https://example.com/page"


def test_classify_text_is_not_itself_responsible_for_the_retraction_check():
    # _is_retracted is checked by the caller (_pair_finding) before _classify_text ever
    # runs -- confirm _classify_text alone still finds the positive keyword (it has no
    # opinion about retraction; that responsibility lives in _is_retracted/_pair_finding).
    assert ddi_flag_evidence._classify_text("RETRACTED: Efficacy and safety of the combination.") == "positive"
    assert ddi_flag_evidence._is_retracted("RETRACTED: Efficacy and safety of the combination.") is True
    assert ddi_flag_evidence._is_retracted("Efficacy and safety of the combination.") is False
    assert ddi_flag_evidence._is_retracted(None) is False


def test_pair_finding_leaves_an_ambiguous_pubmed_title_unresolved_never_positive_or_negative():
    # Real, observed failure of an earlier two-bucket design: defaulting every unmatched
    # title to Negative buried genuinely concerning titles (whose vocabulary the keyword
    # lists don't cover) in the same bucket as plainly neutral citations. Per SKILL.md Part 4
    # there is no third "needs_review" bucket to put this in either -- with no other
    # candidate present, an unclassifiable title simply leaves the pair unresolved.
    result = ddi_flag_evidence._pair_finding(
        {"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION,
        "Effects of levetiracetam and lamotrigine on hippocampal channels.", None,
        None, None, None, None,
    )
    assert result is None, "an unclassifiable title must never be guessed Positive or Negative, and must not force a needs_review verdict either"


def test_pair_finding_returns_none_when_no_source_has_anything():
    assert ddi_flag_evidence._pair_finding({"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION, None, None, None, None, None, None) is None


# ---------------------------------------------------------------------------
# build_report integration -- real local pharmacodynamic-rule pairs still
# surface even with a fully empty fake network; everything else is dropped.
# ---------------------------------------------------------------------------


def test_build_report_drops_pairs_with_no_evidence_and_keeps_real_pd_rule_pairs(monkeypatch, tmp_path):
    _mock_network(monkeypatch)

    report = ddi_flag_evidence.build_report(clinical_notes=_clinical_notes(), cache_dir=tmp_path / "cache", memory_path=tmp_path / "memory.json")

    # The 5 SSRIs in the candidate panel (sertraline/citalopram/escitalopram/fluoxetine/
    # paroxetine) pairwise-fire the real serotonergic_burden pharmacodynamic rule
    # regardless of any live network data -- exactly 10 = C(5,2) such pairs.
    assert report["pairs_with_evidence"] == 10
    assert report["pairs_checked"] == report["pairs_with_evidence"] + report["pairs_dropped_unresolved"]
    assert len(report["pairs"]) == 10
    for pair in report["pairs"]:
        assert pair["impact"] == "negative"
        assert "Increased serotonin-syndrome risk" in pair["finding"]
        # Every dropped/undropped pair's shape carries the same fields.
        assert set(pair) == {
            "drug_a", "drug_b", "impact", "finding", "evidence_labels", "hyperlink_url",
            "primary_evidence_label", "flockhart", "pharmacodynamic_rules", "pubmed_combined_query",
            "clinicaltrials_combined_query", "tavily_combined_query", "dailymed_drug_a", "dailymed_drug_b",
            "dailymed_mention_in_drug_a_label", "dailymed_mention_in_drug_b_label",
        }
    assert report["source_hit_counts"] == {"pubmed": 0, "dailymed": 0, "clinicaltrials": 0, "tavily": 0}


def test_build_report_carries_a_real_relevant_pubmed_hit_through_with_its_own_classification(monkeypatch, tmp_path):
    esearch_body = {"esearchresult": {"idlist": ["21635243"]}}
    esummary_body = {"result": {"21635243": {"title": "Carbamazepine and atorvastatin combination is generally well tolerated."}}}
    _mock_network(
        monkeypatch,
        pubmed_bodies={
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": esearch_body,
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi": esummary_body,
        },
    )

    report = ddi_flag_evidence.build_report(clinical_notes=_clinical_notes(), cache_dir=tmp_path / "cache", memory_path=tmp_path / "memory.json")

    hit = next(p for p in report["pairs"] if {p["drug_a"], p["drug_b"]} == {"Carbamazepine", "Atorvastatin"})
    assert hit["impact"] == "positive"
    assert hit["hyperlink_url"] == "https://pubmed.ncbi.nlm.nih.gov/21635243/"
    assert hit["pubmed_combined_query"]["status"] == "ok"
    assert report["source_hit_counts"]["pubmed"] >= 1


def test_build_report_pubmed_finding_uses_the_real_results_text_not_the_misleading_title(monkeypatch, tmp_path):
    # Real, observed bug this is a regression test for: a title-only Finding can state the
    # paper's aim, not its result -- here the title alone sounds neutral/positive-leaning
    # ("...tolerated" nowhere in it, so it wouldn't even resolve from the title), but the
    # paper's own real Results text explicitly reports increased risk. The Finding must come
    # from that Results text, and classify from it, not stop at the title.
    esearch_body = {"esearchresult": {"idlist": ["21635243"]}}
    esummary_body = {"result": {"21635243": {"title": "Effects of carbamazepine on the pharmacokinetics of atorvastatin."}}}
    abstract_xml = """<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><Article><Abstract>
<AbstractText Label="RESULTS" NlmCategory="RESULTS">Carbamazepine coadministration increased the risk of reduced atorvastatin exposure.</AbstractText>
</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    _mock_network(
        monkeypatch,
        pubmed_bodies={
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": esearch_body,
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi": esummary_body,
        },
        pubmed_abstract_bodies={"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi": abstract_xml},
    )

    report = ddi_flag_evidence.build_report(clinical_notes=_clinical_notes(), cache_dir=tmp_path / "cache", memory_path=tmp_path / "memory.json")

    hit = next(p for p in report["pairs"] if {p["drug_a"], p["drug_b"]} == {"Carbamazepine", "Atorvastatin"})
    assert hit["impact"] == "negative"
    assert "increased the risk" in hit["finding"]
    assert "pharmacokinetics of atorvastatin" not in hit["finding"], "the title must not be used once real Results text is available"


def test_build_report_trials_finding_uses_the_real_outcome_summary_not_the_brief_title(monkeypatch, tmp_path):
    # Real, observed bug this is a regression test for: NCT01111552's own brief_title names a
    # different drug (Escitalopram) than the pair actually being screened even though both are
    # genuinely listed interventions. The Finding must come from the study's real posted
    # outcome comparison, not the brief_title.
    search_body = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT01111552", "briefTitle": "Study of an Oral Aripiprazole/Escitalopram Combination"},
                    "statusModule": {"overallStatus": "COMPLETED"},
                    "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Aripiprazole"}, {"type": "DRUG", "name": "Escitalopram"}]},
                }
            }
        ],
        "totalCount": 1,
    }
    outcome_body = {
        "hasResults": True,
        "resultsSection": {
            "outcomeMeasuresModule": {
                "outcomeMeasures": [
                    {
                        "type": "PRIMARY",
                        "reportingStatus": "POSTED",
                        "title": "Mean Change in Depression Score",
                        "description": "A negative change indicates improvement in depressive symptoms.",
                        "groups": [
                            {"id": "OG000", "title": "Escitalopram Monotherapy"},
                            {"id": "OG001", "title": "Aripiprazole/Escitalopram Combination Therapy"},
                        ],
                        "analyses": [{"groupIds": ["OG000", "OG001"], "pValue": "=0.595"}],
                    }
                ]
            }
        },
    }
    _mock_network(
        monkeypatch,
        trials_bodies={
            "https://clinicaltrials.gov/api/v2/studies?": search_body,
            "https://clinicaltrials.gov/api/v2/studies/NCT01111552": outcome_body,
        },
    )

    report = ddi_flag_evidence.build_report(clinical_notes=_clinical_notes(), cache_dir=tmp_path / "cache", memory_path=tmp_path / "memory.json")

    hit = next(p for p in report["pairs"] if {p["drug_a"], p["drug_b"]} == {"Aripiprazole", "Escitalopram"})
    assert "NCT01111552" in hit["finding"]
    assert "not statistically significant" in hit["finding"]
    assert "Study of an Oral Aripiprazole/Escitalopram Combination" not in hit["finding"], "the brief_title must not be used once real outcome data is available"


def test_build_report_rejects_a_pure_comparator_study_and_falls_through(monkeypatch, tmp_path):
    # Real, named failure mode: a head-to-head comparator trial names both drugs but never
    # combines them (Drug A vs. Drug B is not evidence for Drug A + Drug B). It must not
    # become the Finding -- PubMed falls through, leaving the pair resolved by whatever
    # else genuinely fires (here, nothing else does, so it's correctly unresolved).
    esearch_body = {"esearchresult": {"idlist": ["11111111"]}}
    esummary_body = {"result": {"11111111": {"title": "A randomized trial comparing carbamazepine versus atorvastatin monotherapy for an unrelated indication."}}}
    abstract_xml = """<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><Article><Abstract>
<AbstractText Label="METHODS" NlmCategory="METHODS">Participants were randomized to either carbamazepine monotherapy or atorvastatin monotherapy; no participant received both drugs.</AbstractText>
<AbstractText Label="RESULTS" NlmCategory="RESULTS">Carbamazepine monotherapy and atorvastatin monotherapy had similar efficacy in their respective indications.</AbstractText>
</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    _mock_network(
        monkeypatch,
        pubmed_bodies={
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": esearch_body,
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi": esummary_body,
        },
        pubmed_abstract_bodies={"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi": abstract_xml},
    )

    report = ddi_flag_evidence.build_report(clinical_notes=_clinical_notes(), cache_dir=tmp_path / "cache", memory_path=tmp_path / "memory.json")

    hit = next((p for p in report["pairs"] if {p["drug_a"], p["drug_b"]} == {"Carbamazepine", "Atorvastatin"}), None)
    assert hit is None, "a pure comparator study must not resolve the pair -- it is correctly unresolved, not shown with a fabricated combination Finding"


def test_build_report_calls_dailymed_pointer_once_per_unique_drug_not_once_per_pair(monkeypatch, tmp_path):
    calls: list[str] = []

    def _counting_fetch(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, json_body=None, extra_headers=None):
        calls.append(url)
        return {"url": url, "http_status": 200, "body": {"data": []}, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    _mock_network(monkeypatch)
    monkeypatch.setattr(dailymed_lookup, "fetch_json", _counting_fetch)  # overrides _mock_network's own default for this one check

    report = ddi_flag_evidence.build_report(clinical_notes=_clinical_notes(), cache_dir=tmp_path / "cache", memory_path=tmp_path / "memory.json")

    medications = ddi_flag_evidence._screening_medications(_clinical_notes())
    assert len(calls) == len(medications), "DailyMed pointer search must be called once per unique screening drug, not once per pair"


def test_build_report_states_the_comparative_effectiveness_title_caveat(monkeypatch, tmp_path):
    _mock_network(monkeypatch)

    report = ddi_flag_evidence.build_report(clinical_notes=_clinical_notes(), cache_dir=tmp_path / "cache", memory_path=tmp_path / "memory.json")

    assert any(
        "comparative-effectiveness study titles" in item and "not a per-pair clinical conclusion" in item
        for item in report["limitations"]
    )


def test_build_report_reuses_the_memory_store_cache_on_a_second_run(monkeypatch, tmp_path):
    calls = {"count": 0}

    def _counting_fetch_json(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, json_body=None, extra_headers=None):
        calls["count"] += 1
        return {
            "url": url,
            "http_status": 200,
            "body": {"esearchresult": {"idlist": []}, "data": [], "studies": [], "totalCount": 0, "results": []},
            "retrieved_at": "2026-01-01T00:00:00Z",
            "from_cache": False,
            "error": None,
        }

    def _counting_fetch_text(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, accept="*/*"):
        calls["count"] += 1
        return {"url": url, "http_status": 404, "body": None, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": "HTTP 404: Not Found"}

    monkeypatch.setattr(pubmed_lookup, "fetch_json", _counting_fetch_json)
    monkeypatch.setattr(dailymed_lookup, "fetch_json", _counting_fetch_json)
    monkeypatch.setattr(dailymed_lookup, "fetch_text", _counting_fetch_text)
    monkeypatch.setattr(trials_lookup, "fetch_json", _counting_fetch_json)
    monkeypatch.setattr(search_tools, "_get_api_key", lambda env_var: None)  # never a real Tavily call in tests

    cache_dir = tmp_path / "cache"
    memory_path = tmp_path / "memory.json"
    ddi_flag_evidence.build_report(clinical_notes=_clinical_notes(), cache_dir=cache_dir, memory_path=memory_path)
    first_run_calls = calls["count"]
    assert first_run_calls > 0

    ddi_flag_evidence.build_report(clinical_notes=_clinical_notes(), cache_dir=cache_dir, memory_path=memory_path)
    assert calls["count"] == first_run_calls, "a second run must be served entirely from the memory-store cache, not re-fetch"


# ---------------------------------------------------------------------------
# Evidence/Finding Agent NO_FINDING fall-through (see finding_agent.py and
# SKILL.md Part 6's "Insufficient evidence" rule). impact = _classify_text(text)
# is never touched by any of these -- only whether a candidate that already
# classified is allowed to actually resolve the pair.
# ---------------------------------------------------------------------------


def test_pair_finding_skips_a_no_finding_candidate_and_uses_the_next_one():
    # Test A: Flockhart classifies Positive but the agent explicitly rejects it as not
    # pair-specific; PubMed also classifies Positive and the agent accepts it. The published
    # Finding/Evidence/Hyperlink/source must all come from PubMed, not Flockhart, even
    # though Flockhart is the higher-priority candidate.
    flockhart_label = f"Curated DDI ({reference_data.FLOCKHART_SOURCE})"
    flockhart = {"mechanism_found": True, "statement": "Curated data shows the combination is compatible and generally well tolerated.", "evidence": []}
    stub = _StubFindingAgent({
        flockhart_label: FindingAgentOutcome("unused", False, "llm_reported_insufficient_evidence"),
        "External evidence (PubMed)": FindingAgentOutcome("Combination improved seizure control with acceptable tolerability.", True, None),
    })
    result = ddi_flag_evidence._pair_finding(
        flockhart, [], _NO_MENTION, _NO_MENTION,
        "Well tolerated in combination.", "https://pubmed.example/42",
        None, None, None, None,
        finding_agent=stub,
    )
    assert result is not None
    impact, finding, evidence_labels, hyperlink_url, primary_evidence_label = result
    assert impact == "positive"
    assert finding == "Combination improved seizure control with acceptable tolerability."
    assert hyperlink_url == "https://pubmed.example/42"
    assert primary_evidence_label == "External evidence (PubMed)"
    # Flockhart's candidate still appears in evidence_labels even though it was rejected.
    assert evidence_labels[0] == flockhart_label
    # Both candidates were actually consulted, in priority order -- the agent saw Flockhart
    # before PubMed, exactly as _pair_finding's own priority ordering requires.
    assert [call[2] for call in stub.calls] == [flockhart_label, "External evidence (PubMed)"]


def test_pair_finding_drops_the_pair_when_the_only_candidate_is_no_finding():
    # Test B: the only candidate present is explicitly rejected -- the pair must be dropped
    # (None), the existing unresolved convention, not published with the rejected text.
    stub = _StubFindingAgent({
        "External evidence (PubMed)": FindingAgentOutcome("unused", False, "llm_reported_insufficient_evidence"),
    })
    result = ddi_flag_evidence._pair_finding(
        {"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION,
        "Well tolerated in combination.", "https://pubmed.example/1",
        None, None, None, None,
        finding_agent=stub,
    )
    assert result is None


def test_pair_finding_publishes_original_text_when_agent_errors_rather_than_rejecting():
    # Test D: a model failure is NOT an explicit rejection -- the candidate must still
    # resolve the pair with its original deterministic text, exactly as if the agent did
    # not exist. The pipeline must not crash either.
    stub = _StubFindingAgent({
        "External evidence (PubMed)": FindingAgentOutcome("Well tolerated in combination.", False, "llm_error"),
    })
    result = ddi_flag_evidence._pair_finding(
        {"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION,
        "Well tolerated in combination.", "https://pubmed.example/1",
        None, None, None, None,
        finding_agent=stub,
    )
    assert result is not None
    impact, finding, evidence_labels, hyperlink_url, primary_evidence_label = result
    assert impact == "positive"
    assert finding == "Well tolerated in combination."
    assert hyperlink_url == "https://pubmed.example/1"


def test_explicit_no_finding_and_model_error_are_handled_differently():
    # Test E: identical candidate text, differing only in why the agent didn't return a
    # refined Finding -- an explicit rejection drops the pair, a model error does not.
    text = "Well tolerated in combination."
    base_args = ({"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION, text, "https://pubmed.example/1", None, None, None, None)

    no_finding_agent = _StubFindingAgent({"External evidence (PubMed)": FindingAgentOutcome(text, False, "llm_reported_insufficient_evidence")})
    error_agent = _StubFindingAgent({"External evidence (PubMed)": FindingAgentOutcome(text, False, "llm_error")})

    assert ddi_flag_evidence._pair_finding(*base_args, finding_agent=no_finding_agent) is None
    result = ddi_flag_evidence._pair_finding(*base_args, finding_agent=error_agent)
    assert result is not None and result[1] == text


def test_aripiprazole_escitalopram_no_longer_republishes_a_non_pair_specific_literature_review_finding():
    # Part 6 regression test -- real text from this project's own
    # Flag_Sheet_Live_Evidence.json (Aripiprazole + Escitalopram, PubMed primary_evidence_label):
    # a general pharmacogenomics literature-review conclusion, not a pair-specific combination
    # finding. _classify_text keyword-matches "improve efficacy"/"enhance" and resolves
    # Positive regardless of pair-specificity -- that classification logic is intentionally
    # UNCHANGED here (Part 3 of this fix: impact logic is out of scope).
    real_text = (
        "This reactive approach prolongs symptom burden, increases adverse effects, and delays "
        "effective treatment.Advances in pharmacogenomics now offer a means to individualize "
        "therapy by identifying genetic polymorphisms - such as those found in CYP2C19 and "
        "CYP2D6-that influence escitalopram and aripiprazole metabolism and response. This "
        "literature review examines the evidence linking pharmacogenomic variability to "
        "antidepressant outcomes and explores how implementing genetic-guided prescribing can "
        "improve efficacy, reduce adverse effects, and enhance the pharmacists' role"
    )
    # Confirms impact logic is untouched -- this text still classifies Positive on its own,
    # exactly as it did before this fix.
    assert ddi_flag_evidence._classify_text(real_text) == "positive"

    stub = _StubFindingAgent({
        "External evidence (PubMed)": FindingAgentOutcome(real_text, False, "llm_reported_insufficient_evidence"),
    })
    result = ddi_flag_evidence._pair_finding(
        {"mechanism_found": False}, [], _NO_MENTION, _NO_MENTION,
        real_text, "https://pubmed.ncbi.nlm.nih.gov/42544431/",
        None, None, None, None,
        display_a="Aripiprazole", display_b="Escitalopram", finding_agent=stub,
    )
    # With no other candidate present, the pair is now dropped -- the existing unresolved
    # convention -- instead of republishing the non-pair-specific literature-review text
    # under a Positive verdict, which is what happened before this fix.
    assert result is None


# ---------------------------------------------------------------------------
# Tavily source-quality fix: _tavily_finding_and_url must independently fetch the
# discovered URL's real page content and must never fall back to the bare
# search-result title or snippet.
# ---------------------------------------------------------------------------


def _tavily_envelope(*, title: str | None, url: str | None, content_snippet: str | None = None) -> dict:
    return {
        "status": "ok",
        "result": {"records": [{"title": title, "url": url, "content_snippet": content_snippet, "raw_content": None}]},
    }


def test_tavily_finding_uses_independently_fetched_page_text_not_the_title(monkeypatch, tmp_path):
    def _fake_fetch_text(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, accept="*/*"):
        assert url == "https://example.com/real-article"
        html = "<html><body><p>Coadministration increased plasma concentration and the combination was well tolerated.</p></body></html>"
        return {"url": url, "http_status": 200, "body": html, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(search_tools, "fetch_text", _fake_fetch_text)

    envelope = _tavily_envelope(title="Some generic article title", url="https://example.com/real-article", content_snippet="A teaser snippet")
    finding, url = ddi_flag_evidence._tavily_finding_and_url(envelope, cache_dir=tmp_path / "cache", refresh=False, memory_path=tmp_path / "memory.json")

    assert url == "https://example.com/real-article"
    assert finding == "Coadministration increased plasma concentration and the combination was well tolerated."
    assert finding != "Some generic article title"
    assert "teaser" not in (finding or "")


def test_tavily_finding_skips_navigation_boilerplate_to_the_real_sentence(monkeypatch, tmp_path):
    # Real, observed second bug: an arbitrary fetched page's extracted text often has
    # navigation/menu text concatenated right after the title, before the real article
    # body. The returned Finding must be the actual matching sentence, never the page's
    # raw leading boilerplate.
    def _fake_fetch_text(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, accept="*/*"):
        html = (
            "<html><body><p>Oxcarbazepine/Aripiprazole Interaction and Bipolar Relapse</p>"
            "<nav>Skip to content JCP PCC Insights About Authors and Reviewers Sign In Subscribe</nav>"
            "<p>The patient was treated with oxcarbazepine coadministered with aripiprazole for "
            "six months before relapse occurred.</p></body></html>"
        )
        return {"url": url, "http_status": 200, "body": html, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(search_tools, "fetch_text", _fake_fetch_text)

    envelope = _tavily_envelope(title="Oxcarbazepine/Aripiprazole Interaction and Bipolar Relapse", url="https://example.com/case-report")
    finding, url = ddi_flag_evidence._tavily_finding_and_url(envelope, cache_dir=tmp_path / "cache", refresh=False, memory_path=tmp_path / "memory.json")

    assert finding is not None
    assert "coadministered" in finding
    assert "Skip to content" not in finding


def test_tavily_finding_returns_nothing_when_the_real_page_cannot_be_fetched(monkeypatch, tmp_path):
    # The real bug this fixes: an earlier version fell back to the bare title here. Now the
    # candidate must simply not exist -- exactly like a source that returned nothing.
    def _fake_fetch_text(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, accept="*/*"):
        return {"url": url, "http_status": 404, "body": None, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": "HTTP 404: Not Found"}

    monkeypatch.setattr(search_tools, "fetch_text", _fake_fetch_text)

    envelope = _tavily_envelope(title="A title that must never become the Finding", url="https://example.com/unreachable")
    finding, url = ddi_flag_evidence._tavily_finding_and_url(envelope, cache_dir=tmp_path / "cache", refresh=False, memory_path=tmp_path / "memory.json")

    assert finding is None
    assert url is None


def test_tavily_finding_rejects_a_comparator_only_page_even_with_real_content(monkeypatch, tmp_path):
    def _fake_fetch_text(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, accept="*/*"):
        html = "<html><body><p>This network meta-analysis compared Drug A versus placebo and Drug B versus placebo.</p></body></html>"
        return {"url": url, "http_status": 200, "body": html, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(search_tools, "fetch_text", _fake_fetch_text)

    envelope = _tavily_envelope(title="Looks relevant", url="https://example.com/comparator-only")
    finding, url = ddi_flag_evidence._tavily_finding_and_url(envelope, cache_dir=tmp_path / "cache", refresh=False, memory_path=tmp_path / "memory.json")

    assert finding is None
    assert url is None


def test_build_report_tavily_pair_uses_real_page_content_and_preserves_the_hyperlink(monkeypatch, tmp_path):
    # End-to-end: build_report -> _tavily_finding_and_url -> the real report fields. The
    # discovered URL is preserved as Hyperlink unchanged; the Finding is the independently
    # fetched page's own text, never the search-result title.
    tavily_bodies = {
        search_tools.TAVILY_SEARCH_URL: {
            "results": [
                {
                    "title": "Generic drug interaction overview article",
                    "url": "https://example.com/combo-article",
                    "content": "A teaser snippet about the combination",
                }
            ]
        }
    }
    tavily_page_bodies = {
        "https://example.com/combo-article": (
            "<html><body><p>Coadministration of the combination increased sedation and was "
            "associated with impaired coordination in the studied population.</p></body></html>"
        )
    }
    _mock_network(monkeypatch, tavily_key=True, tavily_bodies=tavily_bodies, tavily_page_bodies=tavily_page_bodies)

    report = ddi_flag_evidence.build_report(clinical_notes=_clinical_notes(), cache_dir=tmp_path / "cache", memory_path=tmp_path / "memory.json")

    tavily_pairs = [p for p in report["pairs"] if p["primary_evidence_label"] == ddi_flag_evidence._TAVILY_EVIDENCE_LABEL]
    assert tavily_pairs, "expected at least one pair resolved by Tavily's real page content"
    pair = tavily_pairs[0]
    assert pair["hyperlink_url"] == "https://example.com/combo-article"
    assert "Generic drug interaction overview article" not in pair["finding"]
    assert "teaser" not in pair["finding"]
    assert "sedation" in pair["finding"].lower()


# ---------------------------------------------------------------------------
# Rich evidence context for the Finding Agent: the agent reads the fuller view of
# a source, while Impact classification and the fallback text stay the short text.
# ---------------------------------------------------------------------------


class _RecordingAgent:
    def __init__(self, outcome_text: str | None = None):
        self.calls: list[tuple[str, str]] = []
        self.outcome_text = outcome_text

    def generate_finding(self, drug_a, drug_b, source_label, resource_text, fallback_text):
        self.calls.append((resource_text, fallback_text))
        if self.outcome_text is None:
            return FindingAgentOutcome(fallback_text, False, "pattern_agent_disabled")
        return FindingAgentOutcome(self.outcome_text, True, None)


def _pubmed_only_args(short_text: str):
    no_mention = {"mentioned": False, "excerpt": None, "url": None, "note": "not mentioned in reviewed label section"}
    return ({"mechanism_found": False}, [], no_mention, no_mention, short_text, "https://pubmed.example/1", None, None, None, None)


def test_agent_reads_the_rich_context_but_impact_and_fallback_use_the_short_text():
    short = "Combination was well tolerated."
    rich = "Title: A trial.\nMETHODS: 40 adults received both drugs.\nRESULTS: Combination was well tolerated."
    agent = _RecordingAgent()
    result = ddi_flag_evidence._pair_finding(*_pubmed_only_args(short), finding_agent=agent, agent_contexts={"pubmed": rich})
    assert agent.calls == [(rich, short)]
    assert result[0] == "positive"  # classified from the short text, unchanged
    assert result[1] == short  # fallback published when the agent declines


def test_missing_context_falls_back_to_the_short_text_for_the_agent():
    short = "Combination was well tolerated."
    agent = _RecordingAgent()
    ddi_flag_evidence._pair_finding(*_pubmed_only_args(short), finding_agent=agent, agent_contexts={"pubmed": None})
    assert agent.calls == [(short, short)]


def test_rich_contexts_are_gated_on_the_agent_flag(monkeypatch):
    monkeypatch.delenv("PRIME_AGENT_DDI_FINDING_AGENT", raising=False)
    assert ddi_flag_evidence.finding_agent_enabled() is False
    monkeypatch.setenv("PRIME_AGENT_DDI_FINDING_AGENT", "1")
    assert ddi_flag_evidence.finding_agent_enabled() is True


# ---------------------------------------------------------------------------
# Impact segregation: when the agent writes the Finding, the Impact comes from the
# same reading of the evidence -- never from a keyword in the short snippet.
# ---------------------------------------------------------------------------


class _ImpactAgent:
    def __init__(self, impact, text="Agent Finding."):
        self.impact, self.text, self.calls = impact, text, 0

    def generate_finding(self, drug_a, drug_b, source_label, resource_text, fallback_text):
        self.calls += 1
        return FindingAgentOutcome(self.text, True, None, self.impact)


def test_agent_impact_overrides_a_misleading_keyword(monkeypatch):
    # Real case: "tolerability" in the snippet made a 50-67% exposure drop classify Positive.
    monkeypatch.setenv("PRIME_AGENT_DDI_FINDING_AGENT", "1")
    snippet = "Inducers decrease plasma levels; monitor clinical response and tolerability."
    assert ddi_flag_evidence._classify_text(snippet) == "positive"
    result = ddi_flag_evidence._pair_finding(*_pubmed_only_args(snippet), finding_agent=_ImpactAgent("negative"))
    assert result[0] == "negative" and result[1] == "Agent Finding."


def test_unclassifiable_snippet_reaches_the_agent_only_when_enabled(monkeypatch):
    snippet = "No dosage adjustment is necessary when the two are coadministered."
    assert ddi_flag_evidence._classify_text(snippet) is None

    monkeypatch.delenv("PRIME_AGENT_DDI_FINDING_AGENT", raising=False)
    off_agent = _ImpactAgent("positive")
    assert ddi_flag_evidence._pair_finding(*_pubmed_only_args(snippet), finding_agent=off_agent) is None
    assert off_agent.calls == 0  # agent off: unchanged behavior

    monkeypatch.setenv("PRIME_AGENT_DDI_FINDING_AGENT", "1")
    on_agent = _ImpactAgent("positive")
    result = ddi_flag_evidence._pair_finding(*_pubmed_only_args(snippet), finding_agent=on_agent)
    assert result[0] == "positive"


def test_agent_failure_on_an_unclassifiable_snippet_falls_through(monkeypatch):
    monkeypatch.setenv("PRIME_AGENT_DDI_FINDING_AGENT", "1")
    snippet = "No dosage adjustment is necessary when the two are coadministered."
    agent = _StubFindingAgent({"External evidence (PubMed)": FindingAgentOutcome(snippet, False, "llm_error")})
    assert ddi_flag_evidence._pair_finding(*_pubmed_only_args(snippet), finding_agent=agent) is None
