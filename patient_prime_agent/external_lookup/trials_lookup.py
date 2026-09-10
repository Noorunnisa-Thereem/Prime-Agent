"""Live ClinicalTrials.gov lookups via the public API v2.

One call: ``GET /api/v2/studies?query.cond=<condition>&query.intr=<drug>``.
Field shape (``protocolSection.identificationModule.nctId``/``briefTitle``,
``statusModule.overallStatus``, ``designModule.phases``) confirmed against
a live call during development.

Before the HTTP call, ``memory_store.recall_or_compute`` checks the
long-term memory store (keyed by the condition+drug pair, 30-day TTL) --
see ``memory_store.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import memory_store
from .guardrails import sanitize_response_field, validate_query_term, validate_source_url
from .http_client import DEFAULT_CACHE_DIR, build_envelope, fetch_json

RESOURCE_NAME = "ClinicalTrials.gov (API v2)"
_STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"
_EXPECTED_DOMAIN = "clinicaltrials.gov"
_REMEMBER_STATUSES = frozenset({"ok", "no_results"})


def search_trials(
    condition: str,
    drug_name: str | None = None,
    *,
    page_size: int = 5,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """Search ClinicalTrials.gov for studies matching ``condition`` (and,
    when given, also filtered to studies mentioning ``drug_name`` as an
    intervention)."""
    condition = validate_query_term(condition, field_name="condition")
    if drug_name:
        drug_name = validate_query_term(drug_name, field_name="drug_name")
    memory_term = f"{condition}|{drug_name or ''}"

    def _live_lookup() -> dict[str, Any]:
        params = f"query.cond={_quote(condition)}&pageSize={int(page_size)}&format=json&countTotal=true"
        if drug_name:
            params += f"&query.intr={_quote(drug_name)}"
        endpoint = f"{_STUDIES_URL}?{params}"
        fetch_result = fetch_json(endpoint, cache_dir=cache_dir, refresh=refresh)

        query = {"condition": condition, "drug_name": drug_name, "page_size": page_size}
        if fetch_result.get("error") is not None:
            return build_envelope(resource=RESOURCE_NAME, endpoint=endpoint, query=query, fetch_result=fetch_result, records=[])

        studies = (fetch_result.get("body") or {}).get("studies", [])
        total_count = (fetch_result.get("body") or {}).get("totalCount")
        records = [_trial_record(study) for study in studies if isinstance(study, dict)]

        envelope = build_envelope(resource=RESOURCE_NAME, endpoint=endpoint, query=query, fetch_result=fetch_result, records=records)
        envelope["total_matches_on_clinicaltrials_gov"] = total_count
        return envelope

    return memory_store.recall_or_compute(
        RESOURCE_NAME,
        memory_term,
        _live_lookup,
        store_path=memory_path,
        refresh=refresh,
        remember_statuses=_REMEMBER_STATUSES,
    )


def _trial_record(study: dict[str, Any]) -> dict[str, Any]:
    """Build one output record, running every third-party text field through
    sanitize_response_field and the URL through validate_source_url before
    either ever reaches External_Evidence_Report.json or a rendered report."""
    protocol = study.get("protocolSection") or {}
    identification = protocol.get("identificationModule") or {}
    status_module = protocol.get("statusModule") or {}
    design_module = protocol.get("designModule") or {}

    clean_nct_id = sanitize_response_field(identification.get("nctId"), max_length=20) or None
    url = validate_source_url(f"https://clinicaltrials.gov/study/{clean_nct_id}", _EXPECTED_DOMAIN) if clean_nct_id else None
    phases = design_module.get("phases")
    clean_phases = (
        [sanitize_response_field(p, max_length=40) for p in phases if isinstance(p, str)]
        if isinstance(phases, list)
        else None
    )
    return {
        "nct_id": clean_nct_id,
        "url": url,
        "brief_title": sanitize_response_field(identification.get("briefTitle")) or None,
        "overall_status": sanitize_response_field(status_module.get("overallStatus"), max_length=60) or None,
        "phases": [p for p in clean_phases if p] if clean_phases is not None else None,
    }


def _quote(term: str) -> str:
    from urllib.parse import quote

    return quote(term, safe="")
