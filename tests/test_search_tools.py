"""Tests for the unified 4-backend search interface
(patient_prime_agent/external_lookup/search_tools.py).

Same convention as test_external_lookup.py: no real network call is ever
made -- either the underlying lookup module's own ``search_*`` function is
monkeypatched directly (for the 3 backends that wrap existing, already-
tested modules), or ``http_client.fetch_json`` is monkeypatched (for Tavily,
the one backend genuinely new in this module).
"""

from __future__ import annotations

import pytest

from patient_prime_agent.external_lookup import dailymed_lookup, pubmed_lookup, search_tools, trials_lookup


# ---------------------------------------------------------------------------
# pubmed / dailymed / clinicaltrials backends: success + no_results, reshaped
# into the standard envelope without needing any API key.
# ---------------------------------------------------------------------------


def test_pubmed_backend_success_is_reshaped_into_standard_envelope(monkeypatch):
    raw = {
        "resource": "PubMed (NCBI E-utils)",
        "query": {"term": "lamotrigine", "retmax": 5},
        "retrieved_at": "2026-01-01T00:00:00Z",
        "from_cache": False,
        "status": "ok",
        "count": 1,
        "records": [{"pmid": "1", "title": "A lamotrigine paper"}],
    }
    monkeypatch.setattr(pubmed_lookup, "search_pubmed", lambda term, **kwargs: raw)

    envelope = search_tools.search_pubmed_backend("lamotrigine")

    assert envelope["source"] == "pubmed"
    assert envelope["status"] == "ok"
    assert envelope["verification"] == "live"
    assert envelope["is_medical_database"] is True
    assert envelope["retrieved_at"] == "2026-01-01T00:00:00Z"
    assert envelope["query"] == {"term": "lamotrigine", "retmax": 5}
    assert envelope["result"]["records"] == [{"pmid": "1", "title": "A lamotrigine paper"}]
    assert envelope["result"]["count"] == 1
    assert "label" not in envelope["result"]


def test_pubmed_backend_no_results_does_not_crash(monkeypatch):
    raw = {"resource": "PubMed (NCBI E-utils)", "query": {"term": "obscure-term"}, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "status": "no_results", "count": 0, "records": []}
    monkeypatch.setattr(pubmed_lookup, "search_pubmed", lambda term, **kwargs: raw)

    envelope = search_tools.search_pubmed_backend("obscure-term")

    assert envelope["status"] == "no_results"
    assert envelope["result"]["records"] == []


def test_pubmed_backend_cached_result_is_marked_verification_cached(monkeypatch):
    raw = {"resource": "PubMed (NCBI E-utils)", "query": {"term": "lamotrigine"}, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": True, "status": "ok", "count": 1, "records": [{"pmid": "1"}]}
    monkeypatch.setattr(pubmed_lookup, "search_pubmed", lambda term, **kwargs: raw)

    envelope = search_tools.search_pubmed_backend("lamotrigine")

    assert envelope["verification"] == "cached"


def test_dailymed_backend_success_and_no_results(monkeypatch):
    ok_raw = {"resource": "DailyMed (NLM REST API v2)", "query": {"drug_name": "lamotrigine"}, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "status": "ok", "count": 1, "records": [{"setid": "abc"}]}
    monkeypatch.setattr(dailymed_lookup, "search_dailymed", lambda drug_name, **kwargs: ok_raw)
    envelope = search_tools.search_dailymed_backend("lamotrigine")
    assert envelope["source"] == "dailymed"
    assert envelope["status"] == "ok"
    assert envelope["is_medical_database"] is True

    empty_raw = {"resource": "DailyMed (NLM REST API v2)", "query": {"drug_name": "not-a-drug"}, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "status": "no_results", "count": 0, "records": []}
    monkeypatch.setattr(dailymed_lookup, "search_dailymed", lambda drug_name, **kwargs: empty_raw)
    envelope = search_tools.search_dailymed_backend("not-a-drug")
    assert envelope["status"] == "no_results"


def test_clinicaltrials_backend_success_and_no_results(monkeypatch):
    ok_raw = {"resource": "ClinicalTrials.gov (API v2)", "query": {"condition": "epilepsy", "drug_name": "lamotrigine"}, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "status": "ok", "count": 1, "records": [{"nct_id": "NCT1"}]}
    monkeypatch.setattr(trials_lookup, "search_trials", lambda condition, drug_name=None, **kwargs: ok_raw)
    envelope = search_tools.search_clinicaltrials_backend("epilepsy", "lamotrigine")
    assert envelope["source"] == "clinicaltrials"
    assert envelope["status"] == "ok"
    assert envelope["is_medical_database"] is True

    empty_raw = {"resource": "ClinicalTrials.gov (API v2)", "query": {"condition": "epilepsy"}, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "status": "no_results", "count": 0, "records": []}
    monkeypatch.setattr(trials_lookup, "search_trials", lambda condition, drug_name=None, **kwargs: empty_raw)
    envelope = search_tools.search_clinicaltrials_backend("epilepsy")
    assert envelope["status"] == "no_results"


def test_pubmed_backend_error_status_passes_through_without_crashing(monkeypatch):
    """The 3 wrapped backends must forward a real error status (never
    fabricate an ok/no_results in its place) -- classify_error's
    rate_limited/network_error split, already produced by the wrapped
    module, must survive reshaping untouched."""
    raw = {
        "resource": "PubMed (NCBI E-utils)",
        "query": {"term": "lamotrigine"},
        "retrieved_at": "2026-01-01T00:00:00Z",
        "from_cache": False,
        "status": "rate_limited",
        "error": "HTTP 429: Too Many Requests",
        "count": 0,
        "records": [],
        "note": "Not retrieved in this session: the external API rate-limited this request (HTTP 429).",
    }
    monkeypatch.setattr(pubmed_lookup, "search_pubmed", lambda term, **kwargs: raw)

    envelope = search_tools.search_pubmed_backend("lamotrigine")

    assert envelope["status"] == "rate_limited"
    assert envelope["result"]["error"] == "HTTP 429: Too Many Requests"


# ---------------------------------------------------------------------------
# Tavily: success, no_results, missing API key -- none of them crash, and a
# missing key makes no network call and no memory/cache access at all.
# ---------------------------------------------------------------------------


def test_tavily_missing_api_key_returns_honest_status_without_network_call(monkeypatch, tmp_path):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(search_tools, "_ENV_FILE_PATH", tmp_path / "no-such-.env")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("fetch_json must not be called when TAVILY_API_KEY is missing")

    monkeypatch.setattr(search_tools, "fetch_json", _fail_if_called)

    envelope = search_tools.search_tavily("lamotrigine", memory_path=tmp_path / "memory.json")

    assert envelope["status"] == "missing_api_key"
    assert envelope["verification"] == "unavailable"
    assert envelope["source"] == "tavily"
    assert envelope["is_medical_database"] is False
    assert envelope["result"]["label"] == search_tools.TAVILY_LABEL
    assert envelope["retrieved_at"] is None


def test_tavily_success_is_labeled_general_web_search_not_medical(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key-for-tests")

    def _fake_fetch_json(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, json_body=None, extra_headers=None):
        assert url == search_tools.TAVILY_SEARCH_URL
        assert json_body["api_key"] == "fake-key-for-tests"
        assert json_body["query"] == "lamotrigine rash mechanism"
        body = {"results": [{"title": "Some article", "url": "https://example.com/a", "content": "long content here", "score": 0.9}]}
        return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(search_tools, "fetch_json", _fake_fetch_json)

    envelope = search_tools.search_tavily("lamotrigine rash mechanism", memory_path=tmp_path / "memory.json")

    assert envelope["status"] == "ok"
    assert envelope["source"] == "tavily"
    assert envelope["is_medical_database"] is False
    assert envelope["result"]["label"] == search_tools.TAVILY_LABEL
    record = envelope["result"]["records"][0]
    assert record["url"] == "https://example.com/a"
    assert record["title"] == "Some article"


def test_tavily_no_results_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key-for-tests")

    def _fake_fetch_json(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, json_body=None, extra_headers=None):
        return {"url": url, "http_status": 200, "body": {"results": []}, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(search_tools, "fetch_json", _fake_fetch_json)

    envelope = search_tools.search_tavily("a-term-with-no-hits", memory_path=tmp_path / "memory.json")

    assert envelope["status"] == "no_results"
    assert envelope["result"]["records"] == []


def test_tavily_rejects_a_non_http_url_without_crashing(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key-for-tests")

    def _fake_fetch_json(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, json_body=None, extra_headers=None):
        body = {"results": [{"title": "Suspicious", "url": "javascript:alert(1)", "content": "x"}]}
        return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(search_tools, "fetch_json", _fake_fetch_json)

    envelope = search_tools.search_tavily("term", memory_path=tmp_path / "memory.json")

    assert envelope["result"]["records"][0]["url"] is None


def test_tavily_reads_api_key_from_env_file_when_not_in_process_environ(monkeypatch, tmp_path):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TAVILY_API_KEY=from-dotenv-file\n", encoding="utf-8")
    monkeypatch.setattr(search_tools, "_ENV_FILE_PATH", env_file)

    seen_keys = []

    def _fake_fetch_json(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, json_body=None, extra_headers=None):
        seen_keys.append(json_body["api_key"])
        return {"url": url, "http_status": 200, "body": {"results": []}, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(search_tools, "fetch_json", _fake_fetch_json)
    search_tools.search_tavily("term", memory_path=tmp_path / "memory.json")

    assert seen_keys == ["from-dotenv-file"]


def test_tavily_rejects_free_text_query():
    from patient_prime_agent.external_lookup.guardrails import QueryValidationError

    with pytest.raises(QueryValidationError):
        search_tools.search_tavily("what is the mechanism of action of lamotrigine, exactly?")


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


def test_search_dispatches_to_the_named_backend(monkeypatch):
    raw = {"resource": "PubMed (NCBI E-utils)", "query": {"term": "x"}, "retrieved_at": "t", "from_cache": False, "status": "ok", "count": 0, "records": []}
    monkeypatch.setattr(pubmed_lookup, "search_pubmed", lambda term, **kwargs: raw)

    envelope = search_tools.search("pubmed", term="x")

    assert envelope["source"] == "pubmed"


def test_search_raises_for_an_unknown_backend_name():
    with pytest.raises(ValueError):
        search_tools.search("not-a-real-backend", query="x")


# ---------------------------------------------------------------------------
# Tavily only fires once every medical source is exhausted.
# ---------------------------------------------------------------------------


def test_all_medical_sources_exhausted_true_only_when_every_status_is_no_results():
    assert search_tools.all_medical_sources_exhausted(
        [{"status": "no_results"}, {"status": "no_results"}, {"status": "no_results"}]
    ) is True


def test_all_medical_sources_exhausted_false_when_empty():
    assert search_tools.all_medical_sources_exhausted([]) is False


def test_all_medical_sources_exhausted_false_when_one_source_found_something():
    assert search_tools.all_medical_sources_exhausted(
        [{"status": "no_results"}, {"status": "ok"}, {"status": "no_results"}]
    ) is False


def test_all_medical_sources_exhausted_false_when_one_source_errored():
    """A source that merely failed to answer (rate-limited, network error)
    must never be treated as 'checked and found nothing' -- Tavily is a
    fallback for a real no_results consensus, not a substitute for a source
    that couldn't be reached."""
    assert search_tools.all_medical_sources_exhausted(
        [{"status": "no_results"}, {"status": "rate_limited"}, {"status": "no_results"}]
    ) is False


def test_search_with_medical_fallback_calls_tavily_only_once_all_medical_sources_report_no_results(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        search_tools,
        "search_tavily",
        lambda query, **kwargs: calls.append(query) or {"source": "tavily", "status": "ok"},
    )

    all_no_results = [{"status": "no_results"}, {"status": "no_results"}, {"status": "no_results"}, {"status": "no_results"}, {"status": "no_results"}]
    result = search_tools.search_with_medical_fallback("lamotrigine", all_no_results)

    assert calls == ["lamotrigine"]
    assert result == {"source": "tavily", "status": "ok"}


def test_search_with_medical_fallback_never_calls_tavily_when_a_medical_source_already_found_something(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Tavily must never be called when a medical source already found a result")

    monkeypatch.setattr(search_tools, "search_tavily", _fail_if_called)

    medical_envelopes = [{"status": "ok"}, {"status": "no_results"}]
    result = search_tools.search_with_medical_fallback("lamotrigine", medical_envelopes)

    assert result is None


def test_search_with_medical_fallback_never_calls_tavily_when_a_medical_source_errored(monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Tavily must never be called as a substitute for a medical source that failed to answer")

    monkeypatch.setattr(search_tools, "search_tavily", _fail_if_called)

    medical_envelopes = [{"status": "no_results"}, {"status": "network_error"}]
    result = search_tools.search_with_medical_fallback("lamotrigine", medical_envelopes)

    assert result is None
