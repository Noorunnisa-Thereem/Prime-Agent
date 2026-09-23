"""Live PubMed lookups via NCBI E-utils (esearch + esummary).

Two real HTTP calls per query: ``esearch`` resolves a drug/gene search term
to a list of PMIDs, then ``esummary`` fetches the title/author/date for
each PMID. Response fields below (``pubdate``, ``lastauthor``, ``source``,
``title``) were confirmed against a live call to
``eutils.ncbi.nlm.nih.gov`` during development -- see the module docstring
of ``http_client.py`` for the shared fetch/cache/rate-limit contract.

Before either HTTP call, ``memory_store.recall_or_compute`` checks the
long-term memory store (keyed by this term, 30-day TTL) so a re-run of the
pipeline on the same patient data does not re-issue an identical live call
-- see ``memory_store.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import memory_store
from .guardrails import sanitize_response_field, validate_query_term, validate_source_url
from .http_client import DEFAULT_CACHE_DIR, build_envelope, fetch_json

RESOURCE_NAME = "PubMed (NCBI E-utils)"
_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
_EXPECTED_DOMAIN = "pubmed.ncbi.nlm.nih.gov"
_REMEMBER_STATUSES = frozenset({"ok", "no_results"})


def search_pubmed(
    term: str,
    *,
    retmax: int = 5,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """Search PubMed for ``term`` (a drug name, gene symbol, or a combined
    ``"GENE AND DRUG"`` query -- see external_evidence_summary.py's
    gene-drug-pair lookup) and return the top ``retmax`` citations with a
    full provenance envelope."""
    term = validate_query_term(term, field_name="term")
    # A combined pair query is always built as "GENE AND DRUG" (see
    # external_evidence_summary.py); the last component is the drug either
    # way (a bare single-term query has only one component). Used below to
    # pick which already-fetched candidate to show first -- see
    # _reorder_by_drug_relevance for why esearch's own ranking isn't
    # trusted blindly. (A [tiab]-scoped query was tried and rejected: it
    # empirically narrowed esearch's own candidate window away from the
    # paper that actually names the drug, for real "GENE AND DRUG" queries
    # against this dataset's genes/drugs -- confirmed live during
    # development. The unscoped query still combines both terms; this
    # reordering step is what makes the result the DRUG is verifiably
    # referenced in, not the query itself.)
    _term_parts = [p.strip() for p in term.split(" AND ") if p.strip()]
    _drug_anchor = _term_parts[-1] if _term_parts else None
    _other_terms = _term_parts[:-1]

    def _live_lookup() -> dict[str, Any]:
        esearch_endpoint = f"{_ESEARCH_URL}?db=pubmed&term={_quote(term)}&retmode=json&retmax={int(retmax)}&sort=relevance"
        esearch_result = fetch_json(esearch_endpoint, cache_dir=cache_dir, refresh=refresh)

        if esearch_result.get("error") is not None:
            return build_envelope(
                resource=RESOURCE_NAME,
                endpoint=esearch_endpoint,
                query={"term": term, "retmax": retmax},
                fetch_result=esearch_result,
                records=[],
            )

        pmids: list[str] = (
            (esearch_result.get("body") or {}).get("esearchresult", {}).get("idlist", [])
        )
        if not pmids:
            return build_envelope(
                resource=RESOURCE_NAME,
                endpoint=esearch_endpoint,
                query={"term": term, "retmax": retmax},
                fetch_result=esearch_result,
                records=[],
            )

        esummary_endpoint = f"{_ESUMMARY_URL}?db=pubmed&id={','.join(pmids)}&retmode=json"
        esummary_result = fetch_json(esummary_endpoint, cache_dir=cache_dir, refresh=refresh)
        if esummary_result.get("error") is not None:
            # esearch succeeded but esummary failed: report the PMIDs we do have (bare, with
            # title=None) as records for provenance, while build_envelope's own classification
            # of the esummary failure (rate-limited/network/http/invalid) becomes this
            # envelope's status -- so a caller sees exactly why the titles are missing rather
            # than a generic "partial" that erases the reason.
            records = [_pubmed_record(pmid, None) for pmid in pmids]
            return build_envelope(
                resource=RESOURCE_NAME,
                endpoint=esummary_endpoint,
                query={"term": term, "retmax": retmax},
                fetch_result=esummary_result,
                records=records,
            )

        summary_by_uid: dict[str, Any] = (esummary_result.get("body") or {}).get("result", {})
        records = [_pubmed_record(pmid, summary_by_uid.get(pmid)) for pmid in pmids]
        records = _reorder_by_drug_relevance(records, _drug_anchor, _other_terms)

        return build_envelope(
            resource=RESOURCE_NAME,
            endpoint=esummary_endpoint,
            query={"term": term, "retmax": retmax},
            fetch_result=esummary_result,
            records=records,
        )

    return memory_store.recall_or_compute(
        RESOURCE_NAME,
        term,
        _live_lookup,
        store_path=memory_path,
        refresh=refresh,
        remember_statuses=_REMEMBER_STATUSES,
    )


def search_pubmed_drug_pair(
    drug_a: str,
    drug_b: str,
    *,
    retmax: int = 5,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """A drug-drug pair query with a real relevance bar, for callers (e.g.
    the Drug Interaction Flag Sheet) that need a citation genuinely about
    *both* named drugs together -- not just a paper where both terms happen
    to appear somewhere in PubMed's "All Fields" index (author affiliation,
    MeSH heading, substance registry entry, etc.).

    Every record ``search_pubmed`` returns is required to actually name
    *both* drugs in its title (a real, observed failure mode: a
    "Levetiracetam AND Atomoxetine" query returned a Huntington Disease
    chapter, and a "Levetiracetam AND Escitalopram" query returned an
    unrelated deer-mice behavioral study -- both matched somewhere in
    PubMed's index, neither one's title has anything to do with the queried
    pair). A record whose title doesn't name both drugs is dropped entirely,
    not just reordered (contrast ``_reorder_by_drug_relevance``, used by the
    gene-drug path, which never drops a candidate) -- this never fabricates
    a replacement result, it only narrows what's reported down to what's
    verifiably on-topic; a pair with no on-topic title comes back exactly
    like a real "no_results" response.

    A ``[tiab]``-scoped query (title/abstract search, tightening the
    candidate pool itself rather than only filtering it afterward) was
    tried and rejected: ``validate_query_term`` -- the shared input
    guardrail every ``external_lookup`` query term passes through, see
    ``guardrails.py`` -- deliberately allows only letters, digits, spaces,
    and hyphens, precisely so free text or an injection attempt can never
    reach a live API; ``[`` and ``]`` fail that allowlist by design. Loosening
    the shared guardrail to admit PubMed field-tag bracket syntax was judged
    not worth it for a precision gain the title filter below already fully
    delivers: a false positive here is false exactly when its title doesn't
    name both drugs, regardless of which index field the underlying esearch
    match came from, so the post-fetch filter alone already excludes every
    observed failure case.

    This never invents evidence: everything returned is a real esearch/
    esummary hit; this only decides whether it is relevant enough to
    display for a specific named pair.
    """
    envelope = search_pubmed(f"{drug_a} AND {drug_b}", retmax=retmax, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path)
    if envelope.get("status") != "ok":
        return envelope

    relevant = [r for r in envelope["records"] if _title_names_both_drugs(r.get("title"), drug_a, drug_b)]
    if relevant:
        return {**envelope, "records": relevant, "count": len(relevant)}
    return {**envelope, "status": "no_results", "records": [], "count": 0}


def _title_names_both_drugs(title: Any, drug_a: str, drug_b: str) -> bool:
    """True only if ``title`` names both drugs as whole words -- the same
    word-boundary approach dailymed_lookup._title_names_drug already uses to
    reject a loose substring match."""
    if not isinstance(title, str) or not title.strip():
        return False
    lowered = title.lower()
    return all(re.search(r"\b" + re.escape(name.strip().lower()) + r"\b", lowered) for name in (drug_a, drug_b) if name and name.strip())


def _reorder_by_drug_relevance(
    records: list[dict[str, Any]], drug_term: str | None, other_terms: list[str] | None = None
) -> list[dict[str, Any]]:
    """Promote the first already-fetched record whose title actually names
    the drug this query was for, ahead of NCBI's own relevance-sorted
    order. PubMed's ``sort=relevance`` (even scoped to ``[tiab]``) can
    still rank a paper that mentions the drug only in passing -- e.g. in a
    drug-interaction table for a different index drug -- ahead of one that
    is directly about it (confirmed live: a "CYP3A4 AND lamotrigine" query
    ranked a Cenobamate pharmacology paper above one titled "Effects of
    lamotrigine and phenytoin on the pharmacokinetics of atorvastatin").

    For a drug-drug pair query (``other_terms`` non-empty -- e.g. a DDI
    literature search built as "Lamotrigine AND Levetiracetam"), a record
    naming BOTH drugs in its title is promoted ahead of one naming only the
    anchor drug, since that is verifiably the more specific, on-topic
    citation for the pair; a gene-drug query (where the gene essentially
    never appears in a paper's title) is unaffected by this refinement --
    it still falls back to "names the anchor drug" exactly as before.

    This never adds, drops, or invents a record -- it only changes which of
    the real, already-retrieved candidates is shown first, and leaves the
    order untouched when none of them name the drug in the title at all."""
    if not drug_term:
        return records
    needle = drug_term.lower()
    other_needles = [t.lower() for t in (other_terms or []) if t]

    def _rank(record: dict[str, Any]) -> int:
        title = str(record.get("title") or "").lower()
        if needle not in title:
            return 2
        if other_needles and all(other in title for other in other_needles):
            return 0
        return 1

    if all(_rank(r) == 2 for r in records):
        return records
    return sorted(records, key=_rank)


def _pubmed_record(pmid: str, summary: dict[str, Any] | None) -> dict[str, Any]:
    """Build one output record, running every third-party text field through
    sanitize_response_field and the URL through validate_source_url before
    either ever reaches External_Evidence_Report.json or a rendered report."""
    clean_pmid = sanitize_response_field(pmid, max_length=20)
    url = validate_source_url(f"https://pubmed.ncbi.nlm.nih.gov/{clean_pmid}/", _EXPECTED_DOMAIN)
    if not isinstance(summary, dict):
        return {"pmid": clean_pmid, "url": url, "title": None}
    return {
        "pmid": clean_pmid,
        "url": url,
        "title": sanitize_response_field(summary.get("title")) or None,
        "source_journal": sanitize_response_field(summary.get("source"), max_length=200) or None,
        "pubdate": sanitize_response_field(summary.get("pubdate"), max_length=60) or None,
        "last_author": sanitize_response_field(summary.get("lastauthor"), max_length=120) or None,
    }


def _quote(term: str) -> str:
    from urllib.parse import quote

    return quote(term, safe="")
