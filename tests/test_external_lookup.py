"""Tests for the live external-evidence lookup layer.

These never make a real network call: the HTTP layer (``http_client.fetch_
json``) is monkeypatched everywhere below, so what's under test is (a) the
on-disk cache logic, (b) each API module's real-response parsing (using
JSON shapes captured from live calls during development), and (c) the
orchestrator's patient-data-only extraction logic (drug names, gene-drug
pairs, diagnosis) against this project's own real fixtures under reports/,
the same convention ``test_ddi.py`` already uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patient_prime_agent import external_evidence_summary
from patient_prime_agent.external_lookup import (
    clinvar_lookup,
    cpic_lookup,
    dailymed_lookup,
    http_client,
    pubmed_lookup,
    trials_lookup,
)

REPORTS_ROOT = Path(__file__).resolve().parents[1] / "reports"


def _load(relative_path: str) -> dict:
    path = REPORTS_ROOT / relative_path
    if not path.exists():
        pytest.skip(f"fixture not present: {relative_path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _fake_fetch_json(body_by_url: dict[str, object], *, error_urls: set[str] | None = None):
    error_urls = error_urls or set()

    def _fake(url: str, *, cache_dir=None, refresh=False, timeout=15, max_retries=1):
        if url in error_urls:
            return {"url": url, "http_status": None, "body": None, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": "TimeoutError: simulated"}
        for prefix, body in body_by_url.items():
            if url.startswith(prefix):
                return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}
        return {"url": url, "http_status": 200, "body": None, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    return _fake


# ---------------------------------------------------------------------------
# http_client: cache + envelope behaviour
# ---------------------------------------------------------------------------


def test_fetch_json_writes_and_reuses_cache(tmp_path, monkeypatch):
    calls = {"count": 0}

    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"hello": "world"}'

    def _fake_urlopen(request, timeout):
        calls["count"] += 1
        return _FakeResponse()

    monkeypatch.setattr(http_client.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(http_client.time, "sleep", lambda *_: None)

    url = "https://example.invalid/api?q=1"
    first = http_client.fetch_json(url, cache_dir=tmp_path)
    assert first["body"] == {"hello": "world"}
    assert first["from_cache"] is False
    assert calls["count"] == 1

    second = http_client.fetch_json(url, cache_dir=tmp_path)
    assert second["body"] == {"hello": "world"}
    assert second["from_cache"] is True
    assert calls["count"] == 1  # no second network call

    third = http_client.fetch_json(url, cache_dir=tmp_path, refresh=True)
    assert third["from_cache"] is False
    assert calls["count"] == 2


def test_fetch_json_surfaces_error_without_fabricating_body(tmp_path, monkeypatch):
    def _fake_urlopen(request, timeout):
        raise OSError("simulated network failure")

    monkeypatch.setattr(http_client.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(http_client.time, "sleep", lambda *_: None)

    result = http_client.fetch_json("https://example.invalid/broken", cache_dir=tmp_path, max_retries=0)
    assert result["error"] is not None
    assert result["body"] is None


# ---------------------------------------------------------------------------
# Per-API parsing, against JSON shapes captured from real live calls
# ---------------------------------------------------------------------------


def test_pubmed_lookup_parses_esearch_and_esummary(monkeypatch, tmp_path):
    esearch_body = {"esearchresult": {"idlist": ["28753467"]}}
    esummary_body = {
        "result": {
            "28753467": {
                "title": "The association of SCN1A p.Thr1067Ala polymorphism with epilepsy risk...",
                "source": "Seizure",
                "pubdate": "2017 Oct",
                "lastauthor": "Rener-Primec Z",
            }
        }
    }
    fake = _fake_fetch_json(
        {
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": esearch_body,
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi": esummary_body,
        }
    )
    monkeypatch.setattr(pubmed_lookup, "fetch_json", fake)

    envelope = pubmed_lookup.search_pubmed("lamotrigine AND SCN1A", memory_path=tmp_path / "memory.json")
    assert envelope["status"] == "ok"
    assert envelope["count"] == 1
    record = envelope["records"][0]
    assert record["pmid"] == "28753467"
    assert record["url"] == "https://pubmed.ncbi.nlm.nih.gov/28753467/"
    assert "SCN1A" in record["title"]


def test_pubmed_lookup_reports_no_results_without_inventing_a_record(monkeypatch, tmp_path):
    fake = _fake_fetch_json({"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": {"esearchresult": {"idlist": []}}})
    monkeypatch.setattr(pubmed_lookup, "fetch_json", fake)

    envelope = pubmed_lookup.search_pubmed("a-term-with-no-hits", memory_path=tmp_path / "memory.json")
    assert envelope["status"] == "no_results"
    assert envelope["records"] == []


def test_pubmed_lookup_gene_drug_pair_query_combines_both_terms(monkeypatch, tmp_path):
    """external_evidence_summary.py builds a pair query as "GENE AND DRUG" --
    confirm search_pubmed actually sends both terms to esearch, not the gene
    alone (the bug this accuracy fix addresses)."""
    esearch_urls: list[str] = []

    def _fake(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1):
        esearch_urls.append(url)
        if "esearch" in url:
            return {"url": url, "http_status": 200, "body": {"esearchresult": {"idlist": ["1"]}}, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}
        body = {"result": {"1": {"title": "Something about CYP3A4 and lamotrigine."}}}
        return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(pubmed_lookup, "fetch_json", _fake)
    pubmed_lookup.search_pubmed("CYP3A4 AND lamotrigine", memory_path=tmp_path / "memory.json")

    esearch_call = next(u for u in esearch_urls if "esearch" in u)
    assert "CYP3A4" in esearch_call
    assert "lamotrigine" in esearch_call, "the drug must reach the query, not just the gene"


def test_pubmed_lookup_promotes_a_result_whose_title_names_the_queried_drug(monkeypatch, tmp_path):
    """Regression test for a real observed failure: querying "CYP3A4 AND
    lamotrigine" returned a Cenobamate pharmacology paper (mentions both
    terms, but its title says nothing about lamotrigine) ranked ahead of a
    paper titled "Effects of lamotrigine and phenytoin on the
    pharmacokinetics of atorvastatin..." -- both PMIDs and titles below are
    the real ones NCBI returned during development. The fix must promote
    the record whose title actually references the drug queried, not just
    trust esearch's own relevance order (which reflects the gene appearing
    somewhere, not necessarily the drug appearing in the title)."""
    esearch_body = {"esearchresult": {"idlist": ["33993416", "21635243"]}}
    esummary_body = {
        "result": {
            "33993416": {"title": "Pharmacology of Cenobamate: Mechanism of Action, Pharmacokinetics, Drug-Drug Interactions and Tolerability."},
            "21635243": {"title": "Effects of lamotrigine and phenytoin on the pharmacokinetics of atorvastatin in healthy volunteers."},
        }
    }
    fake = _fake_fetch_json(
        {
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": esearch_body,
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi": esummary_body,
        }
    )
    monkeypatch.setattr(pubmed_lookup, "fetch_json", fake)

    envelope = pubmed_lookup.search_pubmed("CYP3A4 AND lamotrigine", memory_path=tmp_path / "memory.json")

    top = envelope["records"][0]
    assert top["pmid"] == "21635243"
    assert "lamotrigine" in top["title"].lower(), "the top-shown citation must actually reference the drug queried, not just the gene"
    # Nothing was dropped -- the off-topic-looking record is still present, just not first.
    assert {r["pmid"] for r in envelope["records"]} == {"33993416", "21635243"}


def test_clinvar_lookup_parses_germline_classification(monkeypatch, tmp_path):
    esearch_body = {"esearchresult": {"idlist": ["4887952"], "count": "5406"}}
    esummary_body = {
        "result": {
            "4887952": {
                "accession": "VCV004887952",
                "title": "NM_001165963.4(SCN1A):c.383+4T>C",
                "germline_classification": {
                    "description": "Uncertain significance",
                    "review_status": "criteria provided, single submitter",
                    "last_evaluated": "2026/09/02 00:00",
                    "trait_set": [{"trait_name": "Severe myoclonic epilepsy in infancy"}],
                },
            }
        }
    }
    fake = _fake_fetch_json(
        {
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": esearch_body,
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi": esummary_body,
        }
    )
    monkeypatch.setattr(clinvar_lookup, "fetch_json", fake)

    envelope = clinvar_lookup.search_clinvar_by_gene("SCN1A", memory_path=tmp_path / "memory.json")
    assert envelope["status"] == "ok"
    assert envelope["total_matches_in_clinvar"] == "5406"
    record = envelope["records"][0]
    assert record["clinical_significance"] == "Uncertain significance"
    assert record["linked_traits"] == ["Severe myoclonic epilepsy in infancy"]


def test_dailymed_lookup_parses_spl_entries(monkeypatch, tmp_path):
    body = {
        "data": [
            {
                "setid": "17a20462-d9d2-43ac-bcad-603e8cc76e3b",
                "spl_version": 44,
                "published_date": "Aug 20, 2026",
                "title": "LAMICTAL (LAMOTRIGINE) TABLET",
            }
        ]
    }
    fake = _fake_fetch_json({"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json": body})
    monkeypatch.setattr(dailymed_lookup, "fetch_json", fake)

    envelope = dailymed_lookup.search_dailymed("lamotrigine", memory_path=tmp_path / "memory.json")
    assert envelope["status"] == "ok"
    record = envelope["records"][0]
    assert record["setid"] == "17a20462-d9d2-43ac-bcad-603e8cc76e3b"
    assert record["url"].endswith(record["setid"])


def test_trials_lookup_parses_studies(monkeypatch, tmp_path):
    body = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT05450978", "briefTitle": "A trial"},
                    "statusModule": {"overallStatus": "RECRUITING"},
                    "designModule": {"phases": ["PHASE2"]},
                }
            }
        ],
        "totalCount": 1,
    }
    fake = _fake_fetch_json({"https://clinicaltrials.gov/api/v2/studies": body})
    monkeypatch.setattr(trials_lookup, "fetch_json", fake)

    envelope = trials_lookup.search_trials("epilepsy", "lamotrigine", memory_path=tmp_path / "memory.json")
    assert envelope["status"] == "ok"
    record = envelope["records"][0]
    assert record["nct_id"] == "NCT05450978"
    assert record["overall_status"] == "RECRUITING"


def test_cpic_lookup_reports_no_results_when_drug_not_in_cpic(monkeypatch, tmp_path):
    fake = _fake_fetch_json({"https://api.cpicpgx.org/v1/drug": []})
    monkeypatch.setattr(cpic_lookup, "fetch_json", fake)

    envelope = cpic_lookup.lookup_cpic_pair("SCN1A", "levetiracetam", memory_path=tmp_path / "memory.json")
    assert envelope["status"] == "no_results"
    assert envelope["records"] == []


def test_cpic_lookup_resolves_pair_and_guideline(monkeypatch, tmp_path):
    def _fake(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1):
        if url.startswith("https://api.cpicpgx.org/v1/drug"):
            body = [{"drugid": "RxNorm:28439", "name": "lamotrigine"}]
        elif url.startswith("https://api.cpicpgx.org/v1/pair"):
            body = [{"genesymbol": "UGT1A4", "drugid": "RxNorm:28439", "guidelineid": 100999, "cpiclevel": "D"}]
        elif url.startswith("https://api.cpicpgx.org/v1/guideline"):
            body = [{"id": 100999, "name": "UGT1A4 and Lamotrigine", "url": "https://www.clinpgx.org/guideline/fake"}]
        else:
            body = None
        return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(cpic_lookup, "fetch_json", _fake)

    envelope = cpic_lookup.lookup_cpic_pair("UGT1A4", "lamotrigine", memory_path=tmp_path / "memory.json")
    assert envelope["status"] == "ok"
    record = envelope["records"][0]
    assert record["cpic_level"] == "D"
    assert record["guideline"]["name"] == "UGT1A4 and Lamotrigine"


# ---------------------------------------------------------------------------
# Orchestrator: patient-data-only extraction (no network)
# ---------------------------------------------------------------------------


def test_current_drug_names_come_only_from_patient_regimen():
    clinical_notes = _load("clinical_notes/clinical__notes_summary.json")
    names = external_evidence_summary._current_drug_names(clinical_notes)
    assert set(names) == {"levetiracetam", "lamotrigine"}


def test_relevant_gene_drug_pairs_only_include_current_regimen_drugs():
    genetics = _load("genetics/genetics_clinical_summary.json")
    drug_names = ["lamotrigine", "levetiracetam"]
    pairs = external_evidence_summary._relevant_gene_drug_pairs(genetics, drug_names)

    assert pairs, "expected at least one gene-drug pair from the patient's own panel"
    for pair in pairs:
        assert pair["drug_name"] in drug_names
        assert pair["gene_symbol"]
    # Olanzapine is in the source panel but is not part of this patient's current
    # regimen -- its genetic findings (e.g. UGT1A4) must not leak into this list.
    assert all(pair["drug_name"] != "olanzapine" for pair in pairs)


def test_build_report_returns_no_current_regimen_status_when_regimen_missing():
    report = external_evidence_summary.build_report(clinical_notes={}, genetics={})
    assert report["status"] == "no_current_regimen"
    assert report["by_drug"] == []
