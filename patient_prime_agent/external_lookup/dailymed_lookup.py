"""Live DailyMed lookups via the NLM DailyMed REST API v2.

One call: ``GET /dailymed/services/v2/spls.json?drug_name=<name>`` returns
matching Structured Product Labels (SPLs) with a ``setid`` that resolves to
the canonical label page. Field shape (``setid``, ``spl_version``,
``published_date``, ``title``) confirmed against a live call during
development.

Before the HTTP call, ``memory_store.recall_or_compute`` checks the
long-term memory store (keyed by drug name, 30-day TTL) -- see
``memory_store.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import memory_store
from .guardrails import sanitize_response_field, validate_query_term, validate_source_url
from .http_client import DEFAULT_CACHE_DIR, build_envelope, fetch_json

RESOURCE_NAME = "DailyMed (NLM REST API v2)"
_SPLS_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json"
_EXPECTED_DOMAIN = "dailymed.nlm.nih.gov"
_REMEMBER_STATUSES = frozenset({"ok", "no_results"})


def search_dailymed(
    drug_name: str,
    *,
    pagesize: int = 5,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """Search DailyMed for structured product labels matching ``drug_name``."""
    drug_name = validate_query_term(drug_name, field_name="drug_name")

    def _live_lookup() -> dict[str, Any]:
        endpoint = f"{_SPLS_URL}?drug_name={_quote(drug_name)}&pagesize={int(pagesize)}"
        fetch_result = fetch_json(endpoint, cache_dir=cache_dir, refresh=refresh)

        if fetch_result.get("error") is not None:
            return build_envelope(
                resource=RESOURCE_NAME,
                endpoint=endpoint,
                query={"drug_name": drug_name, "pagesize": pagesize},
                fetch_result=fetch_result,
                records=[],
            )

        entries = (fetch_result.get("body") or {}).get("data", [])
        records = [
            _dailymed_record(entry)
            for entry in entries
            if isinstance(entry, dict) and entry.get("setid") and _title_names_drug(entry.get("title"), drug_name)
        ]

        return build_envelope(
            resource=RESOURCE_NAME,
            endpoint=endpoint,
            query={"drug_name": drug_name, "pagesize": pagesize},
            fetch_result=fetch_result,
            records=records,
        )

    return memory_store.recall_or_compute(
        RESOURCE_NAME,
        drug_name,
        _live_lookup,
        store_path=memory_path,
        refresh=refresh,
        remember_statuses=_REMEMBER_STATUSES,
    )


def _title_names_drug(title: Any, drug_name: str) -> bool:
    """True only if ``title`` actually names ``drug_name`` as a whole word.
    DailyMed's own ``drug_name=`` parameter does loose substring matching --
    confirmed live that ``drug_name=depa`` returns an unrelated hand-sanitizer
    label matched only via a substring hit inside "DEPArtment". A plain
    ``in`` check would repeat that mistake; the word-boundary regex used here
    still accepts a real match like "LAMOTRIGINE TABLET..." for a
    "lamotrigine" query while rejecting the "depa"/"department" case."""
    if not isinstance(title, str) or not title.strip():
        return False
    needle = drug_name.strip()
    if not needle:
        return False
    return re.search(r"\b" + re.escape(needle.lower()) + r"\b", title.lower()) is not None


def _dailymed_record(entry: dict[str, Any]) -> dict[str, Any]:
    """Build one output record, running every third-party text field through
    sanitize_response_field and the URL through validate_source_url before
    either ever reaches External_Evidence_Report.json or a rendered report."""
    clean_setid = sanitize_response_field(entry.get("setid"), max_length=60)
    return {
        "setid": clean_setid or None,
        "spl_version": entry.get("spl_version") if isinstance(entry.get("spl_version"), (int, float)) else None,
        "published_date": sanitize_response_field(entry.get("published_date"), max_length=60) or None,
        "title": sanitize_response_field(entry.get("title")) or None,
        "url": validate_source_url(f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={clean_setid}", _EXPECTED_DOMAIN),
    }


def _quote(term: str) -> str:
    from urllib.parse import quote

    return quote(term, safe="")
