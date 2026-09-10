"""Live ClinVar lookups via NCBI E-utils (esearch + esummary, db=clinvar).

Same two-step E-utils pattern as ``pubmed_lookup.py`` but against
``db=clinvar``, searched by gene symbol (e.g. ``SCN1A[gene]``). Each record
carries the variant's germline classification (clinical significance),
review status, and any linked disease trait -- confirmed against a live
call during development.

Before either HTTP call, ``memory_store.recall_or_compute`` checks the
long-term memory store (keyed by gene symbol, 30-day TTL) -- see
``memory_store.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import memory_store
from .guardrails import sanitize_response_field, validate_query_term, validate_source_url
from .http_client import DEFAULT_CACHE_DIR, build_envelope, fetch_json

RESOURCE_NAME = "ClinVar (NCBI E-utils)"
_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
_EXPECTED_DOMAIN = "ncbi.nlm.nih.gov/clinvar"
_REMEMBER_STATUSES = frozenset({"ok", "no_results"})


def search_clinvar_by_gene(
    gene_symbol: str,
    *,
    retmax: int = 5,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """Search ClinVar for variants annotated to ``gene_symbol`` and return the
    top ``retmax`` records (accession, classification, linked trait) with
    full provenance."""
    gene_symbol = validate_query_term(gene_symbol, field_name="gene_symbol")

    def _live_lookup() -> dict[str, Any]:
        term = f"{gene_symbol}[gene]"
        esearch_endpoint = f"{_ESEARCH_URL}?db=clinvar&term={_quote(term)}&retmode=json&retmax={int(retmax)}"
        esearch_result = fetch_json(esearch_endpoint, cache_dir=cache_dir, refresh=refresh)

        if esearch_result.get("error") is not None:
            return build_envelope(
                resource=RESOURCE_NAME,
                endpoint=esearch_endpoint,
                query={"gene_symbol": gene_symbol, "retmax": retmax},
                fetch_result=esearch_result,
                records=[],
            )

        uids: list[str] = (esearch_result.get("body") or {}).get("esearchresult", {}).get("idlist", [])
        total_count = (esearch_result.get("body") or {}).get("esearchresult", {}).get("count")
        if not uids:
            return build_envelope(
                resource=RESOURCE_NAME,
                endpoint=esearch_endpoint,
                query={"gene_symbol": gene_symbol, "retmax": retmax},
                fetch_result=esearch_result,
                records=[],
            )

        esummary_endpoint = f"{_ESUMMARY_URL}?db=clinvar&id={','.join(uids)}&retmode=json"
        esummary_result = fetch_json(esummary_endpoint, cache_dir=cache_dir, refresh=refresh)
        if esummary_result.get("error") is not None:
            # esearch succeeded but esummary failed: report the UIDs we do have (bare, with
            # title=None) as records for provenance, while build_envelope's own classification
            # of the esummary failure (rate-limited/network/http/invalid) becomes this
            # envelope's status -- so a caller sees exactly why the details are missing rather
            # than a generic "partial" that erases the reason.
            records = [_clinvar_record(uid, None) for uid in uids]
            envelope = build_envelope(
                resource=RESOURCE_NAME,
                endpoint=esummary_endpoint,
                query={"gene_symbol": gene_symbol, "retmax": retmax},
                fetch_result=esummary_result,
                records=records,
            )
            envelope["total_matches_in_clinvar"] = total_count
            return envelope

        summary_by_uid: dict[str, Any] = (esummary_result.get("body") or {}).get("result", {})
        records = [_clinvar_record(uid, summary_by_uid.get(uid)) for uid in uids]

        envelope = build_envelope(
            resource=RESOURCE_NAME,
            endpoint=esummary_endpoint,
            query={"gene_symbol": gene_symbol, "retmax": retmax},
            fetch_result=esummary_result,
            records=records,
        )
        envelope["total_matches_in_clinvar"] = total_count
        return envelope

    return memory_store.recall_or_compute(
        RESOURCE_NAME,
        gene_symbol,
        _live_lookup,
        store_path=memory_path,
        refresh=refresh,
        remember_statuses=_REMEMBER_STATUSES,
    )


def _clinvar_record(uid: str, summary: dict[str, Any] | None) -> dict[str, Any]:
    """Build one output record, running every third-party text field through
    sanitize_response_field and the URL through validate_source_url before
    either ever reaches External_Evidence_Report.json or a rendered report."""
    clean_uid = sanitize_response_field(uid, max_length=20)
    url = validate_source_url(f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{clean_uid}/", _EXPECTED_DOMAIN)
    if not isinstance(summary, dict):
        return {"uid": clean_uid, "url": url, "title": None}

    germline = summary.get("germline_classification") or {}
    traits = germline.get("trait_set") or []
    linked_traits = [
        sanitize_response_field(t.get("trait_name"), max_length=200)
        for t in traits
        if isinstance(t, dict) and t.get("trait_name")
    ]
    return {
        "uid": clean_uid,
        "accession": sanitize_response_field(summary.get("accession"), max_length=60) or None,
        "url": url,
        "title": sanitize_response_field(summary.get("title")) or None,
        "clinical_significance": sanitize_response_field(germline.get("description"), max_length=200) or None,
        "review_status": sanitize_response_field(germline.get("review_status"), max_length=200) or None,
        "last_evaluated": sanitize_response_field(germline.get("last_evaluated"), max_length=60) or None,
        "linked_traits": [t for t in linked_traits if t],
    }


def _quote(term: str) -> str:
    from urllib.parse import quote

    return quote(term, safe="")
