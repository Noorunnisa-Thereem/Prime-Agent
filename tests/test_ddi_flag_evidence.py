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

import pytest

from patient_prime_agent import ddi_flag_evidence
from patient_prime_agent.path_d.ddi import candidate_drugs
from patient_prime_agent.external_lookup import dailymed_lookup, pubmed_lookup, search_tools, trials_lookup

# A DailyMed "not mentioned" result, for tests exercising _pair_finding directly without
# caring about the DailyMed-mention candidates.
_NO_MENTION = {"mentioned": False, "excerpt": None, "url": None, "note": "not mentioned in reviewed label section"}


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
    trials_bodies: dict[str, object] | None = None,
    dailymed_pointer_bodies: dict[str, object] | None = None,
    dailymed_xml_bodies: dict[str, str] | None = None,
    tavily_key: bool = False,
    tavily_bodies: dict[str, object] | None = None,
):
    """Neutralize all 4 live pair-level sources for one build_report test --
    every one of PubMed/DailyMed(pointer+content)/ClinicalTrials.gov/Tavily
    is mocked, regardless of whether the test cares about that source's
    output, so a test can never make a real network call or a real Tavily
    API call by omission."""
    monkeypatch.setattr(pubmed_lookup, "fetch_json", _fake_fetch_json(pubmed_bodies or {}))
    monkeypatch.setattr(dailymed_lookup, "fetch_json", _fake_fetch_json(dailymed_pointer_bodies or {}))
    monkeypatch.setattr(dailymed_lookup, "fetch_text", _fake_fetch_text(dailymed_xml_bodies or {}))
    monkeypatch.setattr(trials_lookup, "fetch_json", _fake_fetch_json(trials_bodies or {}))
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
    negative_rule = [{"expected_consequence": "Increased sedation and impaired coordination."}]
    result = ddi_flag_evidence._pair_finding({"mechanism_found": False}, negative_rule, _NO_MENTION, _NO_MENTION, None, None, None, None, None, None)
    assert result[0] == "negative"

    positive_rule = [{"expected_consequence": "Generally well tolerated with no significant additive effect."}]
    result = ddi_flag_evidence._pair_finding({"mechanism_found": False}, positive_rule, _NO_MENTION, _NO_MENTION, None, None, None, None, None, None)
    assert result[0] == "positive"

    # No clear positive/negative language, and no other candidate present -> unresolved.
    neutral_rule = [{"expected_consequence": "Both agents act on overlapping receptor systems."}]
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
    pd_rules = [{"expected_consequence": "Increased sedation."}]
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
        "excerpt": 'Aripiprazole\'s DailyMed label states: "no clinically significant interaction with lamotrigine was observed when coadministered with aripiprazole."',
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
