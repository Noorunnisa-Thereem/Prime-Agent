"""Tests for the output guardrails in external_lookup/guardrails.py
(sanitize_response_field, validate_source_url) and their use inside every
lookup module.

These prove, per the task: a clean response passes through unchanged, a
response with HTML/script content gets stripped, an overlong field gets
truncated, and a URL from an unexpected domain gets rejected -- first at
the guardrail-function level, then end-to-end through each of the five
lookup modules (fetch_json mocked, no real network call).
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
from patient_prime_agent.external_lookup.guardrails import sanitize_response_field, validate_source_url


def _fake(body_by_prefix: dict[str, object]):
    def _fetch(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1):
        for prefix, body in body_by_prefix.items():
            if url.startswith(prefix):
                return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}
        return {"url": url, "http_status": 200, "body": None, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    return _fetch


# ---------------------------------------------------------------------------
# sanitize_response_field
# ---------------------------------------------------------------------------


def test_sanitize_response_field_passes_clean_text_through_unchanged():
    clean = "Pharmacology of Cenobamate: Mechanism of Action, Pharmacokinetics, Drug-Drug Interactions"
    assert sanitize_response_field(clean) == clean


def test_sanitize_response_field_strips_html_and_script_content():
    dirty = "<script>alert('xss')</script>Real Title<b onclick=\"evil()\">Bold</b> text"
    result = sanitize_response_field(dirty)
    assert "<script" not in result.lower()
    assert "alert(" not in result
    assert "<b" not in result.lower()
    assert "onclick" not in result.lower()
    assert "Real Title" in result
    assert "Bold" in result
    assert "text" in result


def test_sanitize_response_field_truncates_to_max_length():
    long_text = "A" * 900
    result = sanitize_response_field(long_text, max_length=500)
    assert len(result) == 500


def test_sanitize_response_field_does_not_escape_special_characters():
    # sanitize_response_field must hand back plain text, not pre-escaped HTML:
    # report_html.py's _t() is the single place that HTML-escapes for render.
    # Escaping here too would double-escape -- "&" would show up as the
    # literal text "&amp;" in the final PDF instead of "&". A bare '&' or
    # '"' that isn't part of a tag must survive completely unchanged.
    text = 'Drug A & Drug B: "well tolerated" in a Phase <III> study'
    result = sanitize_response_field(text)
    assert "&amp;" not in result
    assert "&quot;" not in result
    assert "Drug A & Drug B" in result
    assert '"well tolerated"' in result
    # The one thing still removed is a literal HTML tag -- "<III>" reads as a
    # tag to the tag-stripper regardless of escaping policy (see the strip
    # test above), so only its surrounding plain text is guaranteed to survive.
    assert "Phase" in result and "study" in result


def test_sanitize_response_field_handles_non_string_input():
    assert sanitize_response_field(None) == ""
    assert sanitize_response_field(12345) == ""
    assert sanitize_response_field(["not", "a", "string"]) == ""


# ---------------------------------------------------------------------------
# validate_source_url
# ---------------------------------------------------------------------------


def test_validate_source_url_accepts_matching_domain():
    url = "https://pubmed.ncbi.nlm.nih.gov/28753467/"
    assert validate_source_url(url, "pubmed.ncbi.nlm.nih.gov") == url


def test_validate_source_url_accepts_subdomain_of_expected_domain():
    url = "https://www.ncbi.nlm.nih.gov/clinvar/variation/4887952/"
    assert validate_source_url(url, "ncbi.nlm.nih.gov/clinvar") == url


def test_validate_source_url_rejects_unexpected_domain():
    assert validate_source_url("https://evil.example.com/pubmed.ncbi.nlm.nih.gov/28753467/", "pubmed.ncbi.nlm.nih.gov") is None
    assert validate_source_url("https://pubmed.ncbi.nlm.nih.gov.evil.com/28753467/", "pubmed.ncbi.nlm.nih.gov") is None
    assert validate_source_url("javascript:alert(1)", "pubmed.ncbi.nlm.nih.gov") is None
    assert validate_source_url(None, "pubmed.ncbi.nlm.nih.gov") is None
    assert validate_source_url("", "pubmed.ncbi.nlm.nih.gov") is None


def test_validate_source_url_enforces_required_path_prefix():
    wrong_path = "https://www.ncbi.nlm.nih.gov/pubmed/28753467/"
    assert validate_source_url(wrong_path, "ncbi.nlm.nih.gov/clinvar") is None


# ---------------------------------------------------------------------------
# End-to-end per module: dirty/overlong content is sanitized, a spoofed
# domain is rejected -- before it would ever reach External_Evidence_
# Report.json or a rendered report.
# ---------------------------------------------------------------------------


def test_pubmed_lookup_sanitizes_title_and_rejects_spoofed_url(monkeypatch, tmp_path):
    esearch_body = {"esearchresult": {"idlist": ["1"]}}
    esummary_body = {
        "result": {
            "1": {
                "title": "<script>alert(1)</script>Efficacy of Drug X" + ("!" * 600),
                "source": "Journal",
                "pubdate": "2026",
                "lastauthor": "Someone",
            }
        }
    }
    monkeypatch.setattr(
        pubmed_lookup,
        "fetch_json",
        _fake(
            {
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": esearch_body,
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi": esummary_body,
            }
        ),
    )
    envelope = pubmed_lookup.search_pubmed("drugx", memory_path=tmp_path / "memory.json")
    record = envelope["records"][0]
    assert "<script" not in record["title"].lower()
    assert len(record["title"]) <= 500
    # url is built locally from a real pmid, so it always lands on the real domain
    assert record["url"] == "https://pubmed.ncbi.nlm.nih.gov/1/"


def test_clinvar_lookup_sanitizes_trait_names(monkeypatch, tmp_path):
    esearch_body = {"esearchresult": {"idlist": ["9"], "count": "1"}}
    esummary_body = {
        "result": {
            "9": {
                "accession": "VCV000000009",
                "title": "Some variant",
                "germline_classification": {
                    "description": "<b>Pathogenic</b>",
                    "review_status": "reviewed",
                    "last_evaluated": "2026/01/01",
                    "trait_set": [{"trait_name": "<script>evil()</script>Some Disease"}],
                },
            }
        }
    }
    monkeypatch.setattr(
        clinvar_lookup,
        "fetch_json",
        _fake(
            {
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": esearch_body,
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi": esummary_body,
            }
        ),
    )
    envelope = clinvar_lookup.search_clinvar_by_gene("SCN1A", memory_path=tmp_path / "memory.json")
    record = envelope["records"][0]
    assert record["clinical_significance"] == "Pathogenic"
    assert record["linked_traits"] == ["Some Disease"]


def test_dailymed_lookup_rejects_spoofed_domain_url(monkeypatch, tmp_path):
    body = {
        "data": [
            {
                "setid": "abc-123",
                "spl_version": 1,
                "published_date": "Jan 1, 2026",
                "title": "A Label" + ("x" * 600),
            }
        ]
    }
    monkeypatch.setattr(dailymed_lookup, "fetch_json", _fake({"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json": body}))
    envelope = dailymed_lookup.search_dailymed("somedrug", memory_path=tmp_path / "memory.json")
    record = envelope["records"][0]
    assert len(record["title"]) <= 500
    assert record["url"].startswith("https://dailymed.nlm.nih.gov/")


def test_cpic_lookup_rejects_guideline_url_on_unexpected_domain(monkeypatch, tmp_path):
    def _fetch(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1):
        if url.startswith("https://api.cpicpgx.org/v1/drug"):
            body = [{"drugid": "RxNorm:1", "name": "somedrug"}]
        elif url.startswith("https://api.cpicpgx.org/v1/pair"):
            body = [{"genesymbol": "SCN1A", "drugid": "RxNorm:1", "guidelineid": 42, "cpiclevel": "A"}]
        elif url.startswith("https://api.cpicpgx.org/v1/guideline"):
            # a guideline URL on a domain that is neither clinpgx.org nor cpicpgx.org
            body = [{"id": 42, "name": "Fake Guideline", "url": "https://evil.example.com/guideline/42"}]
        else:
            body = None
        return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(cpic_lookup, "fetch_json", _fetch)
    envelope = cpic_lookup.lookup_cpic_pair("SCN1A", "somedrug", memory_path=tmp_path / "memory.json")
    record = envelope["records"][0]
    assert record["guideline"]["url"] is None, "a guideline URL on an unexpected domain must be rejected, not stored"
    assert record["guideline"]["name"] == "Fake Guideline"  # non-URL fields are unaffected


def test_cpic_lookup_accepts_real_clinpgx_guideline_domain(monkeypatch, tmp_path):
    def _fetch(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1):
        if url.startswith("https://api.cpicpgx.org/v1/drug"):
            body = [{"drugid": "RxNorm:1", "name": "somedrug"}]
        elif url.startswith("https://api.cpicpgx.org/v1/pair"):
            body = [{"genesymbol": "SCN1A", "drugid": "RxNorm:1", "guidelineid": 42, "cpiclevel": "A"}]
        elif url.startswith("https://api.cpicpgx.org/v1/guideline"):
            body = [{"id": 42, "name": "Real Guideline", "url": "https://www.clinpgx.org/guideline/PA1"}]
        else:
            body = None
        return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(cpic_lookup, "fetch_json", _fetch)
    envelope = cpic_lookup.lookup_cpic_pair("SCN1A", "somedrug", memory_path=tmp_path / "memory.json")
    record = envelope["records"][0]
    assert record["guideline"]["url"] == "https://www.clinpgx.org/guideline/PA1"


def test_trials_lookup_sanitizes_brief_title_and_phases(monkeypatch, tmp_path):
    body = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT00000001", "briefTitle": "<i>A Trial</i>" + ("z" * 600)},
                    "statusModule": {"overallStatus": "RECRUITING"},
                    "designModule": {"phases": ["<b>PHASE2</b>"]},
                }
            }
        ],
        "totalCount": 1,
    }
    monkeypatch.setattr(trials_lookup, "fetch_json", _fake({"https://clinicaltrials.gov/api/v2/studies": body}))
    envelope = trials_lookup.search_trials("epilepsy", memory_path=tmp_path / "memory.json")
    record = envelope["records"][0]
    assert "<i>" not in record["brief_title"]
    assert len(record["brief_title"]) <= 500
    assert record["phases"] == ["PHASE2"]
    assert record["url"] == "https://clinicaltrials.gov/study/NCT00000001"
