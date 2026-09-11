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

# CPIC's own drug table is keyed by generic name (confirmed live: "levetiracetam"
# and "lamotrigine" both resolve, but a brand name never would). This dataset's
# normalizer.py strips a "(Brand)" suffix rather than doing a real brand->generic
# lookup, so a bare brand name reaching this module would otherwise query CPIC for
# a name it can never match -- producing a false "no CPIC drug entry" even when
# real CPIC coverage exists under the generic name. Anti-seizure medications only,
# matching this dataset's regimen; not a general-purpose brand/generic dictionary.
_BRAND_TO_GENERIC = {
    "keppra": "levetiracetam",
    "lamictal": "lamotrigine",
    "depakote": "valproate",
    "depakene": "valproic acid",
    "trileptal": "oxcarbazepine",
    "tegretol": "carbamazepine",
    "dilantin": "phenytoin",
    "onfi": "clobazam",
    "topamax": "topiramate",
    "vimpat": "lacosamide",
    "fycompa": "perampanel",
    "briviact": "brivaracetam",
    "zonegran": "zonisamide",
    "neurontin": "gabapentin",
    "lyrica": "pregabalin",
}


def _to_generic_name(drug_name: str) -> str:
    return _BRAND_TO_GENERIC.get(drug_name.strip().lower(), drug_name)


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
    generic_drug_name = _to_generic_name(drug_name)

    def _live_lookup() -> dict[str, Any]:
        drug_endpoint = f"{_BASE_URL}/drug?name=ilike.*{_quote(generic_drug_name)}*&select=drugid,name"
        drug_result = fetch_json(drug_endpoint, cache_dir=cache_dir, refresh=refresh)

        query: dict[str, Any] = {"gene_symbol": gene_symbol, "drug_name": drug_name}
        if generic_drug_name.lower() != drug_name.strip().lower():
            query["drug_name_generic"] = generic_drug_name

        envelope: dict[str, Any] = {
            "resource": RESOURCE_NAME,
            "endpoint": drug_endpoint,
            "query": query,
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
            envelope["note"] = (
                f"CPIC has no drug entry matching '{generic_drug_name}'; no gene-drug pair can exist for it."
            )
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
