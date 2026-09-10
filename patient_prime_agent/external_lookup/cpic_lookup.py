"""Live CPIC lookups via the public CPIC API (api.cpicpgx.org, PostgREST).

Three possible real calls per gene-drug pair:
  1. ``drug?name=ilike.*<name>*`` to resolve a free-text drug name to CPIC's
     ``drugid`` (an ``RxNorm:<code>`` identifier) -- confirmed live that
     e.g. "levetiracetam" resolves to zero rows (CPIC has no drug entry for
     it at all), while "lamotrigine" resolves to ``RxNorm:28439``.
  2. ``pair?genesymbol=eq.<gene>&drugid=eq.<drugid>`` to find whether CPIC
     has assessed that specific gene-drug pair, and at what evidence level
     (``cpiclevel``: A/B/C/D) and whether an active dosing guideline exists
     (``guidelineid``, nullable).
  3. ``guideline?id=eq.<guidelineid>`` to resolve the guideline's name/URL,
     only when step 2 returned a non-null ``guidelineid``.

A drug with no CPIC entry, or a pair CPIC has graded but not written a
guideline for, is reported as such explicitly -- it is not the same thing
as an error, and must not be conflated with one.

Before any of these calls, ``memory_store.recall_or_compute`` checks the
long-term memory store (keyed by the gene-drug pair, 30-day TTL) -- see
``memory_store.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import memory_store
from .guardrails import CPIC_GUIDELINE_DOMAINS, sanitize_response_field, validate_query_term, validate_source_url
from .http_client import DEFAULT_CACHE_DIR, classify_error, fetch_json, not_retrieved_note

RESOURCE_NAME = "CPIC (api.cpicpgx.org)"
_BASE_URL = "https://api.cpicpgx.org/v1"
_REMEMBER_STATUSES = frozenset({"ok", "no_results"})


def _validate_cpic_url(url: Any) -> str | None:
    """CPIC's guideline content lives on clinpgx.org (CPIC's knowledge base was
    rebranded "ClinPGx"), confirmed against a live call during development;
    cpicpgx.org (the API host) is accepted too in case a future field points
    back at it. Rejects anything on neither domain."""
    for domain in CPIC_GUIDELINE_DOMAINS:
        validated = validate_source_url(url, domain)
        if validated is not None:
            return validated
    return None


def lookup_cpic_pair(
    gene_symbol: str,
    drug_name: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """Look up whether CPIC has assessed the ``gene_symbol``/``drug_name`` pair."""
    gene_symbol = validate_query_term(gene_symbol, field_name="gene_symbol")
    drug_name = validate_query_term(drug_name, field_name="drug_name")
    memory_term = f"{gene_symbol}|{drug_name}"

    def _live_lookup() -> dict[str, Any]:
        drug_endpoint = f"{_BASE_URL}/drug?name=ilike.*{_quote(drug_name)}*&select=drugid,name"
        drug_result = fetch_json(drug_endpoint, cache_dir=cache_dir, refresh=refresh)

        envelope: dict[str, Any] = {
            "resource": RESOURCE_NAME,
            "endpoint": drug_endpoint,
            "query": {"gene_symbol": gene_symbol, "drug_name": drug_name},
            "retrieved_at": drug_result.get("retrieved_at"),
            "from_cache": drug_result.get("from_cache", False),
            "http_status": drug_result.get("http_status"),
            "error": drug_result.get("error"),
            "status": None,
            "records": [],
        }

        drug_error_category = classify_error(drug_result)
        if drug_error_category is not None:
            envelope["status"] = drug_error_category
            envelope["note"] = not_retrieved_note(drug_error_category)
            return envelope

        drug_rows = drug_result.get("body") or []
        if not drug_rows:
            envelope["status"] = "no_results"
            envelope["note"] = f"CPIC has no drug entry matching '{drug_name}'; no gene-drug pair can exist for it."
            return envelope

        drugid = drug_rows[0].get("drugid")
        pair_endpoint = (
            f"{_BASE_URL}/pair?select=genesymbol,drugid,guidelineid,cpiclevel"
            f"&genesymbol=eq.{_quote(gene_symbol)}&drugid=eq.{_quote(drugid)}"
        )
        pair_result = fetch_json(pair_endpoint, cache_dir=cache_dir, refresh=refresh)
        envelope["endpoint"] = pair_endpoint
        envelope["retrieved_at"] = pair_result.get("retrieved_at")
        envelope["from_cache"] = pair_result.get("from_cache", False)
        envelope["http_status"] = pair_result.get("http_status")
        envelope["error"] = pair_result.get("error")

        pair_error_category = classify_error(pair_result)
        if pair_error_category is not None:
            envelope["status"] = pair_error_category
            envelope["note"] = not_retrieved_note(pair_error_category)
            return envelope

        pair_rows = pair_result.get("body") or []
        if not pair_rows:
            envelope["status"] = "no_results"
            envelope["note"] = f"CPIC lists '{drug_name}' ({drugid}) but has no {gene_symbol}-{drug_name} pair on record."
            return envelope

        records = []
        for row in pair_rows:
            record = {
                "gene_symbol": sanitize_response_field(row.get("genesymbol"), max_length=20) or None,
                "drugid": sanitize_response_field(row.get("drugid"), max_length=40) or None,
                "cpic_level": sanitize_response_field(row.get("cpiclevel"), max_length=5) or None,
                "guideline": None,
            }
            guideline_id = row.get("guidelineid")
            if guideline_id is not None:
                guideline_endpoint = f"{_BASE_URL}/guideline?id=eq.{guideline_id}&select=id,name,url"
                guideline_result = fetch_json(guideline_endpoint, cache_dir=cache_dir, refresh=refresh)
                guideline_rows = (guideline_result.get("body") or []) if guideline_result.get("error") is None else []
                if guideline_rows:
                    record["guideline"] = {
                        "id": guideline_rows[0].get("id") if isinstance(guideline_rows[0].get("id"), (int, float)) else None,
                        "name": sanitize_response_field(guideline_rows[0].get("name"), max_length=200) or None,
                        "url": _validate_cpic_url(guideline_rows[0].get("url")),
                    }
            records.append(record)

        envelope["records"] = records
        envelope["count"] = len(records)
        envelope["status"] = "ok"
        return envelope

    return memory_store.recall_or_compute(
        RESOURCE_NAME,
        memory_term,
        _live_lookup,
        store_path=memory_path,
        refresh=refresh,
        remember_statuses=_REMEMBER_STATUSES,
    )


def _quote(term: str) -> str:
    from urllib.parse import quote

    return quote(term, safe="")
