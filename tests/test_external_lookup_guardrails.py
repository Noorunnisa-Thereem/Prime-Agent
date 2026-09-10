"""Tests for the external_lookup input guardrail (guardrails.py).

Every one of the five lookup modules must validate its query term(s)
before building a URL or calling ``fetch_json`` -- this file proves both
halves of that contract per module: a normal, Path-B-shaped term (a plain
drug name or gene symbol) passes straight through to the (mocked) HTTP
layer, and a notes/patient-identifier-shaped term is rejected by
``QueryValidationError`` with zero calls ever reaching ``fetch_json``.
"""

from __future__ import annotations

import pytest

from patient_prime_agent.external_lookup import (
    clinvar_lookup,
    cpic_lookup,
    dailymed_lookup,
    pubmed_lookup,
    trials_lookup,
)
from patient_prime_agent.external_lookup.guardrails import QueryValidationError, validate_query_term

# Notes/PHI-shaped free text: long, punctuated, sentence-like -- exactly what
# the guardrail exists to keep out of an external API query. Also doubles as
# a stand-in for a patient name/ID string with punctuation.
_NOTES_LIKE_TEXT = (
    "Patient John Doe (SYN-100482), DOB 1990-01-01: reports increased anxiety, "
    "a new rash, and dizziness since the 03/02/2026 visit; see full note."
)


class _FetchSpy:
    """Stand-in for http_client.fetch_json that records every call it receives,
    so a test can assert zero network calls were attempted."""

    def __init__(self, body=None):
        self.calls: list[str] = []
        self.body = body if body is not None else {}

    def __call__(self, url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1):
        self.calls.append(url)
        return {
            "url": url,
            "http_status": 200,
            "body": self.body,
            "retrieved_at": "2026-01-01T00:00:00Z",
            "from_cache": False,
            "error": None,
        }


# ---------------------------------------------------------------------------
# validate_query_term itself
# ---------------------------------------------------------------------------


def test_validate_query_term_accepts_plain_drug_and_gene_terms():
    assert validate_query_term("lamotrigine") == "lamotrigine"
    assert validate_query_term("SCN1A") == "SCN1A"
    assert validate_query_term("SCN1A AND lamotrigine") == "SCN1A AND lamotrigine"
    assert validate_query_term("Focal impaired-awareness seizures") == "Focal impaired-awareness seizures"


def test_validate_query_term_rejects_notes_like_free_text():
    with pytest.raises(QueryValidationError):
        validate_query_term(_NOTES_LIKE_TEXT)


def test_validate_query_term_rejects_non_string_empty_and_overlong_terms():
    with pytest.raises(QueryValidationError):
        validate_query_term(None)
    with pytest.raises(QueryValidationError):
        validate_query_term("   ")
    with pytest.raises(QueryValidationError):
        validate_query_term("a" * 61)


# ---------------------------------------------------------------------------
# One test per module: a normal term reaches fetch_json; a notes-like term
# is rejected before fetch_json is ever called.
# ---------------------------------------------------------------------------


def test_pubmed_lookup_validates_term_before_any_http_call(monkeypatch, tmp_path):
    spy = _FetchSpy({"esearchresult": {"idlist": []}})
    monkeypatch.setattr(pubmed_lookup, "fetch_json", spy)
    memory_path = tmp_path / "memory.json"

    pubmed_lookup.search_pubmed("lamotrigine", memory_path=memory_path)
    assert spy.calls, "a valid term must reach fetch_json"

    spy.calls.clear()
    with pytest.raises(QueryValidationError):
        pubmed_lookup.search_pubmed(_NOTES_LIKE_TEXT, memory_path=memory_path)
    assert spy.calls == [], "a notes-like term must never reach fetch_json"


def test_clinvar_lookup_validates_term_before_any_http_call(monkeypatch, tmp_path):
    spy = _FetchSpy({"esearchresult": {"idlist": []}})
    monkeypatch.setattr(clinvar_lookup, "fetch_json", spy)
    memory_path = tmp_path / "memory.json"

    clinvar_lookup.search_clinvar_by_gene("SCN1A", memory_path=memory_path)
    assert spy.calls, "a valid gene symbol must reach fetch_json"

    spy.calls.clear()
    with pytest.raises(QueryValidationError):
        clinvar_lookup.search_clinvar_by_gene(_NOTES_LIKE_TEXT, memory_path=memory_path)
    assert spy.calls == [], "a notes-like term must never reach fetch_json"


def test_dailymed_lookup_validates_term_before_any_http_call(monkeypatch, tmp_path):
    spy = _FetchSpy({"data": []})
    monkeypatch.setattr(dailymed_lookup, "fetch_json", spy)
    memory_path = tmp_path / "memory.json"

    dailymed_lookup.search_dailymed("lamotrigine", memory_path=memory_path)
    assert spy.calls, "a valid drug name must reach fetch_json"

    spy.calls.clear()
    with pytest.raises(QueryValidationError):
        dailymed_lookup.search_dailymed(_NOTES_LIKE_TEXT, memory_path=memory_path)
    assert spy.calls == [], "a notes-like term must never reach fetch_json"


def test_cpic_lookup_validates_both_terms_before_any_http_call(monkeypatch, tmp_path):
    spy = _FetchSpy([])
    monkeypatch.setattr(cpic_lookup, "fetch_json", spy)
    memory_path = tmp_path / "memory.json"

    cpic_lookup.lookup_cpic_pair("SCN1A", "lamotrigine", memory_path=memory_path)
    assert spy.calls, "valid gene/drug terms must reach fetch_json"

    spy.calls.clear()
    with pytest.raises(QueryValidationError):
        cpic_lookup.lookup_cpic_pair(_NOTES_LIKE_TEXT, "lamotrigine", memory_path=memory_path)
    assert spy.calls == [], "a notes-like gene term must never reach fetch_json"

    spy.calls.clear()
    with pytest.raises(QueryValidationError):
        cpic_lookup.lookup_cpic_pair("SCN1A", _NOTES_LIKE_TEXT, memory_path=memory_path)
    assert spy.calls == [], "a notes-like drug term must never reach fetch_json"


def test_trials_lookup_validates_condition_and_drug_before_any_http_call(monkeypatch, tmp_path):
    spy = _FetchSpy({"studies": []})
    monkeypatch.setattr(trials_lookup, "fetch_json", spy)
    memory_path = tmp_path / "memory.json"

    trials_lookup.search_trials("epilepsy", "lamotrigine", memory_path=memory_path)
    assert spy.calls, "a valid condition/drug pair must reach fetch_json"

    spy.calls.clear()
    with pytest.raises(QueryValidationError):
        trials_lookup.search_trials(_NOTES_LIKE_TEXT, memory_path=memory_path)
    assert spy.calls == [], "a notes-like condition must never reach fetch_json"

    spy.calls.clear()
    with pytest.raises(QueryValidationError):
        trials_lookup.search_trials("epilepsy", _NOTES_LIKE_TEXT, memory_path=memory_path)
    assert spy.calls == [], "a notes-like drug term must never reach fetch_json"
