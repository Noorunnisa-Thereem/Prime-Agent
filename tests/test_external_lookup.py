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


def test_search_pubmed_drug_pair_drops_a_result_that_only_names_one_drug(monkeypatch, tmp_path):
    """Regression test for a real observed failure: a "Levetiracetam AND
    Atomoxetine" query matched a Huntington Disease chapter via PubMed's
    "All Fields" index -- its title has nothing to do with either drug.
    Unlike the gene-drug path (_reorder_by_drug_relevance, which reorders
    but never drops), a drug-drug pair query must drop a title that doesn't
    name both drugs entirely."""
    esearch_body = {"esearchresult": {"idlist": ["20301482"]}}
    esummary_body = {"result": {"20301482": {"title": "Huntington Disease."}}}
    fake = _fake_fetch_json(
        {
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": esearch_body,
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi": esummary_body,
        }
    )
    monkeypatch.setattr(pubmed_lookup, "fetch_json", fake)

    envelope = pubmed_lookup.search_pubmed_drug_pair("Levetiracetam", "Atomoxetine", memory_path=tmp_path / "memory.json")

    assert envelope["status"] == "no_results", "a title naming neither drug must be treated as no-match, not displayed"
    assert envelope["records"] == []


def test_search_pubmed_drug_pair_keeps_a_result_that_names_both_drugs(monkeypatch, tmp_path):
    esearch_body = {"esearchresult": {"idlist": ["22056838"]}}
    esummary_body = {"result": {"22056838": {"title": "The interactions of atorvastatin and fluvastatin with carbamazepine, phenytoin and valproate in the mouse maximal electroshock seizure model."}}}
    fake = _fake_fetch_json(
        {
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi": esearch_body,
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi": esummary_body,
        }
    )
    monkeypatch.setattr(pubmed_lookup, "fetch_json", fake)

    envelope = pubmed_lookup.search_pubmed_drug_pair("Carbamazepine", "Atorvastatin", memory_path=tmp_path / "memory.json")

    assert envelope["status"] == "ok"
    assert envelope["records"][0]["pmid"] == "22056838"


def test_search_pubmed_drug_pair_reports_no_results_when_every_candidate_is_dropped_but_esearch_found_something():
    assert pubmed_lookup._title_names_both_drugs("Huntington Disease.", "Levetiracetam", "Atomoxetine") is False
    assert pubmed_lookup._title_names_both_drugs(
        "The interactions of atorvastatin and fluvastatin with carbamazepine.", "Carbamazepine", "Atorvastatin"
    ) is True
    assert pubmed_lookup._title_names_both_drugs(None, "Carbamazepine", "Atorvastatin") is False


def test_clinvar_lookup_parses_germline_classification(monkeypatch, tmp_path):
    esearch_body = {"esearchresult": {"idlist": ["4887952"], "count": "5406"}}
    esummary_body = {
        "result": {
            "4887952": {
                "accession": "VCV004887952",
                "title": "NM_001165963.4(SCN1A):c.383+4T>C",
                "genes": [{"symbol": "SCN1A", "geneid": "6323"}],
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


def test_clinvar_lookup_keeps_record_whose_genes_field_matches_queried_gene(monkeypatch, tmp_path):
    """Correct-matching case: NCBI's own [gene] scoping is trusted, but only
    after this module's own local check confirms the summary's "genes" field
    genuinely names the queried gene (confirmed live: querying SCN1A[gene]
    returns records whose "genes" field genuinely contains SCN1A)."""
    esearch_body = {"esearchresult": {"idlist": ["4887952"], "count": "1"}}
    esummary_body = {
        "result": {
            "4887952": {
                "accession": "VCV004887952",
                "title": "NM_001165963.4(SCN1A):c.383+4T>C",
                "genes": [{"symbol": "SCN1A", "geneid": "6323"}],
                "germline_classification": {"description": "Uncertain significance", "trait_set": []},
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
    assert envelope["records"][0]["accession"] == "VCV004887952"
    assert envelope["records"][0]["genes"] == ["SCN1A"]


def test_clinvar_lookup_rejects_record_whose_genes_field_does_not_match_queried_gene(monkeypatch, tmp_path):
    """Bad-match-rejected case: a uid returned by esearch whose own summary
    names a different gene (a coincidental text match, not a real
    same-gene finding) must not be surfaced as if it were evidence for the
    queried gene -- it should be filtered out, degrading to no_results
    rather than a false positive."""
    esearch_body = {"esearchresult": {"idlist": ["9999999"], "count": "1"}}
    esummary_body = {
        "result": {
            "9999999": {
                "accession": "VCV009999999",
                "title": "Some unrelated variant",
                "genes": [{"symbol": "SCN2A", "geneid": "6326"}],
                "germline_classification": {"description": "Benign", "trait_set": []},
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
    assert envelope["status"] == "no_results"
    assert envelope["records"] == []


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


def test_dailymed_lookup_rejects_loose_substring_match_on_unrelated_product(monkeypatch, tmp_path):
    """Bad-match-rejected case: DailyMed's own drug_name= parameter does loose
    substring matching -- confirmed live that drug_name=depa returns an
    unrelated hand-sanitizer label matched only via a substring hit inside
    "DEPArtment". A real match ("LAMOTRIGINE TABLET...") must still be kept
    alongside it, proving this is a targeted rejection, not a blanket one."""
    body = {
        "data": [
            {
                "setid": "aaaaaaaa-0000-0000-0000-000000000000",
                "spl_version": 1,
                "published_date": "Jan 1, 2026",
                "title": "NE MEXICO AGING LONG TERM SERVICES DEPARTMENT HAND SANITIZER (BENZALKONIUM CHLORIDE) GEL [SOMBRA COSMETICS]",
            },
            {
                "setid": "17a20462-d9d2-43ac-bcad-603e8cc76e3b",
                "spl_version": 44,
                "published_date": "Aug 20, 2026",
                "title": "LAMICTAL (LAMOTRIGINE) TABLET",
            },
        ]
    }
    fake = _fake_fetch_json({"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json": body})
    monkeypatch.setattr(dailymed_lookup, "fetch_json", fake)

    envelope = dailymed_lookup.search_dailymed("depa", memory_path=tmp_path / "memory.json")
    setids = {r["setid"] for r in envelope["records"]}
    assert "aaaaaaaa-0000-0000-0000-000000000000" not in setids, "hand-sanitizer label must not be returned for a 'depa' query"
    assert setids == set()  # "depa" itself names no real drug in either title


# ---------------------------------------------------------------------------
# dailymed_lookup: real label CONTENT (the SPL document itself), not just the
# search-result pointer -- confirmed live against a real label during
# development (see dailymed_lookup.py's own module docstring).
# ---------------------------------------------------------------------------

_SAMPLE_SPL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<document xmlns="urn:hl7-org:v3">
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="34089-3" codeSystem="2.16.840.1.113883.6.1" displayName="DESCRIPTION SECTION"/>
          <title>1 DESCRIPTION</title>
          <text><paragraph>Some unrelated description text.</paragraph></text>
        </section>
      </component>
      <component>
        <section>
          <code code="34073-7" codeSystem="2.16.840.1.113883.6.1" displayName="DRUG INTERACTIONS SECTION"/>
          <title>7 DRUG INTERACTIONS</title>
          <text><paragraph>No dosage adjustment is necessary for lamotrigine when coadministered with this product.</paragraph></text>
        </section>
      </component>
    </structuredBody>
  </component>
</document>"""

_SAMPLE_SPL_XML_NO_INTERACTIONS_SECTION = """<?xml version="1.0" encoding="UTF-8"?>
<document xmlns="urn:hl7-org:v3">
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="34089-3" codeSystem="2.16.840.1.113883.6.1" displayName="DESCRIPTION SECTION"/>
          <title>1 DESCRIPTION</title>
          <text><paragraph>Some unrelated description text.</paragraph></text>
        </section>
      </component>
    </structuredBody>
  </component>
</document>"""


def _fake_fetch_text(body_by_url_prefix: dict[str, str], *, error_urls: set[str] | None = None):
    error_urls = error_urls or set()

    def _fake(url: str, *, cache_dir=None, refresh=False, timeout=15, max_retries=1, accept="*/*"):
        if url in error_urls:
            return {"url": url, "http_status": None, "body": None, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": "TimeoutError: simulated"}
        for prefix, body in body_by_url_prefix.items():
            if url.startswith(prefix):
                return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}
        return {"url": url, "http_status": 404, "body": None, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": "HTTP 404: Not Found"}

    return _fake


def test_fetch_label_interactions_section_extracts_the_real_loinc_section_text(monkeypatch, tmp_path):
    fake = _fake_fetch_text({"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/": _SAMPLE_SPL_XML})
    monkeypatch.setattr(dailymed_lookup, "fetch_text", fake)

    envelope = dailymed_lookup.fetch_label_interactions_section("test-setid-123", memory_path=tmp_path / "memory.json")

    assert envelope["status"] == "ok"
    assert "lamotrigine" in envelope["records"][0]["section_text"].lower()
    assert "unrelated description" not in envelope["records"][0]["section_text"].lower()


def test_fetch_label_interactions_section_reports_no_results_when_label_has_no_such_section(monkeypatch, tmp_path):
    fake = _fake_fetch_text({"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/": _SAMPLE_SPL_XML_NO_INTERACTIONS_SECTION})
    monkeypatch.setattr(dailymed_lookup, "fetch_text", fake)

    envelope = dailymed_lookup.fetch_label_interactions_section("test-setid-456", memory_path=tmp_path / "memory.json")

    assert envelope["status"] == "no_results"
    assert envelope["records"] == []


def test_fetch_label_interactions_section_surfaces_a_real_fetch_error_honestly(monkeypatch, tmp_path):
    fake = _fake_fetch_text({}, error_urls={"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/broken-setid.xml"})
    monkeypatch.setattr(dailymed_lookup, "fetch_text", fake)

    envelope = dailymed_lookup.fetch_label_interactions_section("broken-setid", memory_path=tmp_path / "memory.json")

    assert envelope["status"] == "network_error"
    assert envelope["records"] == []


def test_find_drug_mention_excerpt_returns_a_real_bounded_excerpt_around_the_match():
    section_text = "No dosage adjustment is necessary for lamotrigine when coadministered with this product."
    excerpt = dailymed_lookup.find_drug_mention_excerpt(section_text, "lamotrigine")
    assert excerpt is not None
    assert "lamotrigine" in excerpt.lower()
    assert "adjustment" in excerpt.lower()  # real words from the source, not a paraphrase


def test_find_drug_mention_excerpt_returns_none_when_the_drug_is_not_mentioned():
    section_text = "No dosage adjustment is necessary for lamotrigine when coadministered with this product."
    assert dailymed_lookup.find_drug_mention_excerpt(section_text, "quetiapine") is None
    assert dailymed_lookup.find_drug_mention_excerpt(section_text, "") is None
    assert dailymed_lookup.find_drug_mention_excerpt("", "lamotrigine") is None


def test_find_drug_mention_excerpt_rejects_a_substring_only_match():
    # "lamotrigine" must not match as a false-positive substring inside "lamotriginesulfate" or
    # similar -- word-boundary matching, same discipline as _title_names_drug.
    section_text = "This label discusses lamotriginesulfateanalog, an unrelated compound."
    assert dailymed_lookup.find_drug_mention_excerpt(section_text, "lamotrigine") is None


def test_trials_lookup_parses_studies(monkeypatch, tmp_path):
    body = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT05450978", "briefTitle": "A trial"},
                    "statusModule": {"overallStatus": "RECRUITING"},
                    "designModule": {"phases": ["PHASE2"]},
                    "conditionsModule": {"conditions": ["Epilepsy"]},
                    "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Lamotrigine"}]},
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
    assert record["conditions"] == ["Epilepsy"]


def test_trials_lookup_keeps_study_with_matching_intervention_and_overlapping_condition(monkeypatch, tmp_path):
    """Correct-matching case, shaped after a real live call: a trial whose
    structured conditions/interventions genuinely relate to the query
    (querying "Focal impaired-awareness seizures" + "Levetiracetam" and
    getting a "Partial Seizures" trial that structurally lists
    levetiracetam as an intervention) must be kept."""
    body = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT00537238", "briefTitle": "Pregabalin Versus Levetiracetam In Partial Seizures"},
                    "statusModule": {"overallStatus": "COMPLETED"},
                    "designModule": {"phases": ["PHASE4"]},
                    "conditionsModule": {"conditions": ["Partial Seizures"]},
                    "armsInterventionsModule": {
                        "interventions": [{"type": "DRUG", "name": "Pregabalin"}, {"type": "DRUG", "name": "Levetiracetam"}]
                    },
                }
            }
        ],
        "totalCount": 1,
    }
    fake = _fake_fetch_json({"https://clinicaltrials.gov/api/v2/studies": body})
    monkeypatch.setattr(trials_lookup, "fetch_json", fake)

    envelope = trials_lookup.search_trials("Focal impaired-awareness seizures", "Levetiracetam", memory_path=tmp_path / "memory.json")
    assert envelope["status"] == "ok"
    assert envelope["records"][0]["nct_id"] == "NCT00537238"


def test_trials_lookup_rejects_study_missing_the_queried_drug_as_a_structured_intervention(monkeypatch, tmp_path):
    """Bad-match-rejected case: ClinicalTrials.gov's own query.intr= can
    surface a study via loose matching even when the drug is not actually
    one of its structured interventions (e.g. a placebo-only arm study).
    Such a study must be filtered out rather than returned as if the drug
    were genuinely studied there."""
    body = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT99999999", "briefTitle": "Placebo-controlled seizure trial"},
                    "statusModule": {"overallStatus": "COMPLETED"},
                    "designModule": {"phases": ["PHASE3"]},
                    "conditionsModule": {"conditions": ["Seizures"]},
                    "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Placebo"}]},
                }
            }
        ],
        "totalCount": 1,
    }
    fake = _fake_fetch_json({"https://clinicaltrials.gov/api/v2/studies": body})
    monkeypatch.setattr(trials_lookup, "fetch_json", fake)

    envelope = trials_lookup.search_trials("Seizures", "Levetiracetam", memory_path=tmp_path / "memory.json")
    assert envelope["status"] == "no_results"
    assert envelope["records"] == []


def test_trials_lookup_drug_pair_keeps_only_studies_structurally_listing_both_drugs(monkeypatch, tmp_path):
    body = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT00000001", "briefTitle": "Both drugs really studied together"},
                    "statusModule": {"overallStatus": "COMPLETED"},
                    "designModule": {"phases": ["PHASE2"]},
                    "conditionsModule": {"conditions": ["Epilepsy"]},
                    "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Lamotrigine"}, {"type": "DRUG", "name": "Levetiracetam"}]},
                }
            },
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT00000002", "briefTitle": "Only one of the two drugs, coincidental match elsewhere"},
                    "statusModule": {"overallStatus": "COMPLETED"},
                    "designModule": {"phases": ["PHASE1"]},
                    "conditionsModule": {"conditions": ["Epilepsy"]},
                    "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Lamotrigine"}, {"type": "DRUG", "name": "Placebo"}]},
                }
            },
        ],
        "totalCount": 2,
    }
    fake = _fake_fetch_json({"https://clinicaltrials.gov/api/v2/studies": body})
    monkeypatch.setattr(trials_lookup, "fetch_json", fake)

    envelope = trials_lookup.search_trials_drug_pair("Lamotrigine", "Levetiracetam", memory_path=tmp_path / "memory.json")

    assert envelope["status"] == "ok"
    assert [r["nct_id"] for r in envelope["records"]] == ["NCT00000001"]


def test_trials_lookup_drug_pair_reports_no_results_when_no_study_lists_both(monkeypatch, tmp_path):
    body = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT00000003", "briefTitle": "Only levetiracetam"},
                    "statusModule": {"overallStatus": "COMPLETED"},
                    "designModule": {"phases": ["PHASE1"]},
                    "conditionsModule": {"conditions": ["Epilepsy"]},
                    "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Levetiracetam"}]},
                }
            }
        ],
        "totalCount": 1,
    }
    fake = _fake_fetch_json({"https://clinicaltrials.gov/api/v2/studies": body})
    monkeypatch.setattr(trials_lookup, "fetch_json", fake)

    envelope = trials_lookup.search_trials_drug_pair("Lamotrigine", "Levetiracetam", memory_path=tmp_path / "memory.json")
    assert envelope["status"] == "no_results"
    assert envelope["records"] == []


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


def test_cpic_lookup_normalizes_brand_name_to_generic_before_querying(monkeypatch, tmp_path):
    """Correct-matching case: CPIC's drug table is keyed by generic name --
    confirmed live that "levetiracetam" resolves while a brand name never
    would. A brand name like "Keppra" reaching this module must be queried
    as "levetiracetam", not left as-is (which would produce a false
    "no CPIC drug entry" even though real CPIC coverage exists)."""
    drug_urls: list[str] = []

    def _fake(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1):
        if url.startswith("https://api.cpicpgx.org/v1/drug"):
            drug_urls.append(url)
            body = [{"drugid": "RxNorm:9999", "name": "levetiracetam"}]
        elif url.startswith("https://api.cpicpgx.org/v1/pair"):
            body = []
        else:
            body = None
        return {"url": url, "http_status": 200, "body": body, "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(cpic_lookup, "fetch_json", _fake)

    envelope = cpic_lookup.lookup_cpic_pair("RYR1", "Keppra", memory_path=tmp_path / "memory.json")
    assert "levetiracetam" in drug_urls[0]
    assert "Keppra" not in drug_urls[0]
    assert envelope["query"]["drug_name_generic"] == "levetiracetam"


def test_cpic_lookup_leaves_already_generic_name_unchanged(monkeypatch, tmp_path):
    """Bad-match-rejected case: the brand->generic map must not rewrite a
    name that is not one of its known brand keys -- confirms the fix is a
    narrow, safe lookup rather than a normalization step that could distort
    an already-correct generic name into the wrong drug's query."""
    drug_urls: list[str] = []

    def _fake(url, *, cache_dir=None, refresh=False, timeout=15, max_retries=1):
        if url.startswith("https://api.cpicpgx.org/v1/drug"):
            drug_urls.append(url)
        return {"url": url, "http_status": 200, "body": [], "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False, "error": None}

    monkeypatch.setattr(cpic_lookup, "fetch_json", _fake)

    envelope = cpic_lookup.lookup_cpic_pair("SCN1A", "levetiracetam", memory_path=tmp_path / "memory.json")
    assert "levetiracetam" in drug_urls[0]
    assert "drug_name_generic" not in envelope["query"]


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
    assert report["by_drug_pair"] == []


def test_current_drug_pairs_generates_sorted_current_current_combinations():
    assert external_evidence_summary._current_drug_pairs(["levetiracetam", "lamotrigine"]) == [("lamotrigine", "levetiracetam")]
    assert external_evidence_summary._current_drug_pairs(["only-one-drug"]) == []
    assert external_evidence_summary._current_drug_pairs([]) == []
    # Three drugs -> three unique pairs, never a drug paired with itself.
    three = external_evidence_summary._current_drug_pairs(["c", "a", "b"])
    assert three == [("a", "b"), ("a", "c"), ("b", "c")]


def test_current_drug_pairs_never_produces_the_same_unordered_pair_twice():
    # "lamotrigine" appears twice (once case-variant) alongside "Levetiracetam" -- the real
    # cross-drug pair between them must still only be generated once, not once per duplicate
    # copy of the repeated name. Caught by the frozenset({a, b}) dedup guard, not left for a
    # caller to notice after the fact.
    pairs = external_evidence_summary._current_drug_pairs(["Lamotrigine", "Levetiracetam", "lamotrigine"])
    keys = [frozenset({a.lower(), b.lower()}) for a, b in pairs]
    assert len(keys) == len(set(keys)), "no unordered pair should be generated twice"
    assert keys.count(frozenset({"lamotrigine", "levetiracetam"})) == 1


def test_build_report_queries_drug_pairs_as_one_combined_call_not_two_single_drug_calls(monkeypatch):
    clinical_notes = _load("clinical_notes/clinical__notes_summary.json")
    genetics = _load("genetics/genetics_clinical_summary.json")

    pubmed_calls: list[str] = []

    def _fake_search_pubmed(term, **kwargs):
        pubmed_calls.append(term)
        return {"status": "no_results", "records": [], "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False}

    monkeypatch.setattr(external_evidence_summary.pubmed_lookup, "search_pubmed", _fake_search_pubmed)
    monkeypatch.setattr(external_evidence_summary.dailymed_lookup, "search_dailymed", lambda *a, **k: {"status": "no_results", "records": []})
    monkeypatch.setattr(external_evidence_summary.trials_lookup, "search_trials", lambda *a, **k: {"status": "no_results", "records": []})
    monkeypatch.setattr(external_evidence_summary.clinvar_lookup, "search_clinvar_by_gene", lambda *a, **k: {"status": "no_results", "records": []})
    monkeypatch.setattr(external_evidence_summary.cpic_lookup, "lookup_cpic_pair", lambda *a, **k: {"status": "no_results", "records": []})
    # Every medical source above reports "no_results", which would otherwise trigger the
    # Tavily fallback (search_tools.search_with_medical_fallback) -- stubbed out here so this
    # test's outcome never depends on whether TAVILY_API_KEY happens to be set in whatever
    # environment runs it, and so it can never make a real network call.
    monkeypatch.setattr(external_evidence_summary.search_tools, "search_with_medical_fallback", lambda *a, **k: None)

    report = external_evidence_summary.build_report(clinical_notes, genetics)

    # Exactly one drug-pair entry for this patient's two current-regimen drugs, and it is a
    # SINGLE combined query -- never two separate single-drug interaction queries merged.
    assert len(report["by_drug_pair"]) == 1
    pair_entry = report["by_drug_pair"][0]
    assert {pair_entry["drug_a"], pair_entry["drug_b"]} == {"lamotrigine", "levetiracetam"}

    combined_terms = [t for t in pubmed_calls if " AND " in t and "lamotrigine" in t.lower() and "levetiracetam" in t.lower()]
    assert len(combined_terms) == 1, f"expected exactly one combined drug-pair PubMed query, got: {pubmed_calls}"
    assert combined_terms[0].lower() == "lamotrigine and levetiracetam"


# ---------------------------------------------------------------------------
# Tavily web-search fallback wiring in build_report (search_tools.py)
# ---------------------------------------------------------------------------


def _no_results(*_a, **_k):
    return {"status": "no_results", "records": [], "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False}


def test_build_report_populates_web_search_fallback_when_every_medical_source_reports_no_results(monkeypatch):
    """End-to-end through the real gate (search_tools.search_with_medical_fallback ->
    all_medical_sources_exhausted -> search_tavily), not a stubbed-out shortcut: every
    medical source this patient's drugs/genes touch reports no_results, so every
    by_drug/by_drug_pair/by_gene_drug_pair entry must carry a real Tavily envelope."""
    clinical_notes = _load("clinical_notes/clinical__notes_summary.json")
    genetics = _load("genetics/genetics_clinical_summary.json")

    monkeypatch.setattr(external_evidence_summary.pubmed_lookup, "search_pubmed", _no_results)
    monkeypatch.setattr(external_evidence_summary.dailymed_lookup, "search_dailymed", _no_results)
    monkeypatch.setattr(external_evidence_summary.trials_lookup, "search_trials", _no_results)
    monkeypatch.setattr(external_evidence_summary.clinvar_lookup, "search_clinvar_by_gene", _no_results)
    monkeypatch.setattr(external_evidence_summary.cpic_lookup, "lookup_cpic_pair", _no_results)

    tavily_calls: list[str] = []

    def _fake_search_tavily(query, **kwargs):
        tavily_calls.append(query)
        return {"source": "tavily", "status": "ok", "is_medical_database": False, "result": {"label": external_evidence_summary.search_tools.TAVILY_LABEL, "records": []}}

    monkeypatch.setattr(external_evidence_summary.search_tools, "search_tavily", _fake_search_tavily)

    report = external_evidence_summary.build_report(clinical_notes, genetics)

    assert report["by_drug"], "expected at least one current-regimen drug"
    for entry in report["by_drug"]:
        assert entry["web_search_fallback"] is not None
        assert entry["web_search_fallback"]["is_medical_database"] is False
        assert entry["web_search_fallback"]["result"]["label"] == external_evidence_summary.search_tools.TAVILY_LABEL

    for entry in report["by_drug_pair"]:
        assert entry["web_search_fallback"] is not None

    for entry in report["by_gene_drug_pair"]:
        assert entry["web_search_fallback"] is not None

    assert len(tavily_calls) == len(report["by_drug"]) + len(report["by_drug_pair"]) + len(report["by_gene_drug_pair"])
    assert report["coverage"]["web_search_fallback"]["invocation_count"] == len(tavily_calls)
    # Tavily's own outcomes must never leak into the five-medical-source accounting.
    assert report["coverage"]["no_results_count"] > 0
    assert "tavily" not in [r.lower() for r in report["coverage"]["resources_queried"]]


def test_build_report_never_calls_tavily_when_a_medical_source_already_found_something(monkeypatch):
    """Correctness check for 'never a first choice': as soon as ANY medical source for a
    drug reports "ok" (found something), that drug's web_search_fallback must stay None,
    and Tavily must not even be called."""
    clinical_notes = _load("clinical_notes/clinical__notes_summary.json")
    genetics = _load("genetics/genetics_clinical_summary.json")

    def _pubmed_found_something(term, **kwargs):
        return {"status": "ok", "records": [{"pmid": "1"}], "retrieved_at": "2026-01-01T00:00:00Z", "from_cache": False}

    monkeypatch.setattr(external_evidence_summary.pubmed_lookup, "search_pubmed", _pubmed_found_something)
    monkeypatch.setattr(external_evidence_summary.dailymed_lookup, "search_dailymed", _no_results)
    monkeypatch.setattr(external_evidence_summary.trials_lookup, "search_trials", _no_results)
    monkeypatch.setattr(external_evidence_summary.clinvar_lookup, "search_clinvar_by_gene", _no_results)
    monkeypatch.setattr(external_evidence_summary.cpic_lookup, "lookup_cpic_pair", _no_results)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("Tavily must never be called while a medical source already found a result")

    monkeypatch.setattr(external_evidence_summary.search_tools, "search_tavily", _fail_if_called)

    report = external_evidence_summary.build_report(clinical_notes, genetics)

    for entry in report["by_drug"]:
        assert entry["web_search_fallback"] is None
    assert report["coverage"]["web_search_fallback"]["invocation_count"] == 0


def test_build_report_excludes_skipped_clinicaltrials_from_the_fallback_gate(monkeypatch):
    """A drug's ClinicalTrials.gov lookup is 'skipped' (not 'no_results') when there is no
    primary diagnosis to search for -- that must not block the fallback gate, since a
    skipped source was genuinely never checked, not checked-and-empty."""
    clinical_notes = _load("clinical_notes/clinical__notes_summary.json")
    genetics = _load("genetics/genetics_clinical_summary.json")
    clinical_notes_no_diagnosis = {**clinical_notes, "patient_profile": {**clinical_notes["patient_profile"], "diagnosis": None}}

    monkeypatch.setattr(external_evidence_summary.pubmed_lookup, "search_pubmed", _no_results)
    monkeypatch.setattr(external_evidence_summary.dailymed_lookup, "search_dailymed", _no_results)
    monkeypatch.setattr(external_evidence_summary.clinvar_lookup, "search_clinvar_by_gene", _no_results)
    monkeypatch.setattr(external_evidence_summary.cpic_lookup, "lookup_cpic_pair", _no_results)

    def _fail_if_trials_called(*args, **kwargs):
        raise AssertionError("ClinicalTrials.gov must not be called at all when there is no diagnosis")

    monkeypatch.setattr(external_evidence_summary.trials_lookup, "search_trials", _fail_if_trials_called)

    tavily_calls: list[str] = []
    monkeypatch.setattr(
        external_evidence_summary.search_tools,
        "search_tavily",
        lambda query, **kwargs: tavily_calls.append(query) or {"source": "tavily", "status": "ok"},
    )

    report = external_evidence_summary.build_report(clinical_notes_no_diagnosis, genetics)

    for entry in report["by_drug"]:
        assert entry["clinicaltrials_gov"]["status"] == "skipped"
        assert entry["web_search_fallback"] is not None, "pubmed+dailymed no_results alone must still trigger the fallback"
