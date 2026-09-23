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

import re
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
        # query.cond/query.intr already scope the search server-side, but this never trusts
        # that scoping blindly (confirmed live: a "Focal impaired-awareness seizures" +
        # "Levetiracetam" query returned a genuinely on-topic epilepsy trial alongside a
        # cancer-context trial whose only real link was a shared "Seizures" condition token
        # and an incidental levetiracetam arm) -- each study's own structured
        # armsInterventionsModule/conditionsModule is checked before it is shown as a match.
        relevant_studies = [
            study
            for study in studies
            if isinstance(study, dict)
            and _record_intervention_matches(study, drug_name)
            and _record_condition_overlaps(study, condition)
        ]
        records = [_trial_record(study) for study in relevant_studies]

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


def search_trials_drug_pair(
    drug_a: str,
    drug_b: str,
    *,
    page_size: int = 5,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """Search ClinicalTrials.gov for studies whose structured interventions
    genuinely include BOTH ``drug_a`` and ``drug_b`` -- a real drug-pair
    query (no ``condition`` needed), the same combined-query shape
    ``pubmed_lookup.search_pubmed_drug_pair`` already uses for PubMed.

    Same never-trust-the-raw-match discipline ``search_trials`` itself
    already applies: ``query.intr=DrugA AND DrugB`` scopes the search
    server-side (confirmed live it returns real studies), but a study is
    only kept when its own structured ``armsInterventionsModule`` lists
    BOTH drugs as real interventions -- a study that merely mentions one of
    them, or names both only in an unrelated field, is dropped, not shown
    as a match."""
    drug_a = validate_query_term(drug_a, field_name="drug_a")
    drug_b = validate_query_term(drug_b, field_name="drug_b")
    memory_term = f"{drug_a}|{drug_b}"

    def _live_lookup() -> dict[str, Any]:
        params = f"query.intr={_quote(f'{drug_a} AND {drug_b}')}&pageSize={int(page_size)}&format=json&countTotal=true"
        endpoint = f"{_STUDIES_URL}?{params}"
        fetch_result = fetch_json(endpoint, cache_dir=cache_dir, refresh=refresh)
        query = {"drug_a": drug_a, "drug_b": drug_b, "page_size": page_size}

        if fetch_result.get("error") is not None:
            return build_envelope(resource=RESOURCE_NAME, endpoint=endpoint, query=query, fetch_result=fetch_result, records=[])

        studies = (fetch_result.get("body") or {}).get("studies", [])
        total_count = (fetch_result.get("body") or {}).get("totalCount")
        relevant_studies = [
            study
            for study in studies
            if isinstance(study, dict) and _record_intervention_matches(study, drug_a) and _record_intervention_matches(study, drug_b)
        ]
        records = [_trial_record(study) for study in relevant_studies]

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


_CONDITION_STOPWORDS = frozenset(
    {"and", "the", "with", "from", "for", "of", "in", "on", "to", "adult", "adults", "patients", "patient"}
)


def _condition_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 3 and w not in _CONDITION_STOPWORDS}


def _record_intervention_matches(study: dict[str, Any], drug_name: str | None) -> bool:
    """True if ``drug_name`` genuinely appears as a structured intervention
    on this study -- not merely because the API's own ``query.intr`` said so.
    When no ``drug_name`` was requested, every study passes (nothing to
    verify)."""
    if not drug_name:
        return True
    arms = (study.get("protocolSection") or {}).get("armsInterventionsModule") or {}
    interventions = arms.get("interventions")
    if not isinstance(interventions, list):
        return False
    needle = drug_name.strip().lower()
    return any(isinstance(iv, dict) and needle in str(iv.get("name") or "").lower() for iv in interventions)


def _record_condition_overlaps(study: dict[str, Any], condition: str) -> bool:
    """True if this study's own ``conditionsModule.conditions`` shares at
    least one meaningful word with the queried ``condition`` -- rejects a
    trial whose only real link to the query is an unrelated condition list
    (confirmed live: a "Seizures"-labeled cancer trial surfaced for a
    "Focal impaired-awareness seizures" query via keyword overlap alone,
    while sharing no other clinical context with the patient's epilepsy
    diagnosis). This is a coarse word-overlap heuristic, not clinical
    judgment -- it cannot and does not try to distinguish "Partial Seizures"
    from "Seizures" in an unrelated oncology context; it only rejects
    conditions with zero token overlap at all. A study missing structured
    condition data is passed through rather than rejected, since there is
    nothing here to verify against."""
    conditions = ((study.get("protocolSection") or {}).get("conditionsModule") or {}).get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return True
    query_tokens = _condition_tokens(condition)
    if not query_tokens:
        return True
    return any(isinstance(c, str) and _condition_tokens(c) & query_tokens for c in conditions)


def _trial_record(study: dict[str, Any]) -> dict[str, Any]:
    """Build one output record, running every third-party text field through
    sanitize_response_field and the URL through validate_source_url before
    either ever reaches External_Evidence_Report.json or a rendered report."""
    protocol = study.get("protocolSection") or {}
    identification = protocol.get("identificationModule") or {}
    status_module = protocol.get("statusModule") or {}
    design_module = protocol.get("designModule") or {}

    conditions_module = protocol.get("conditionsModule") or {}

    clean_nct_id = sanitize_response_field(identification.get("nctId"), max_length=20) or None
    url = validate_source_url(f"https://clinicaltrials.gov/study/{clean_nct_id}", _EXPECTED_DOMAIN) if clean_nct_id else None
    phases = design_module.get("phases")
    clean_phases = (
        [sanitize_response_field(p, max_length=40) for p in phases if isinstance(p, str)]
        if isinstance(phases, list)
        else None
    )
    conditions = conditions_module.get("conditions")
    clean_conditions = (
        [sanitize_response_field(c, max_length=200) for c in conditions if isinstance(c, str)]
        if isinstance(conditions, list)
        else None
    )
    return {
        "nct_id": clean_nct_id,
        "url": url,
        "brief_title": sanitize_response_field(identification.get("briefTitle")) or None,
        "overall_status": sanitize_response_field(status_module.get("overallStatus"), max_length=60) or None,
        "phases": [p for p in clean_phases if p] if clean_phases is not None else None,
        "conditions": [c for c in clean_conditions if c] if clean_conditions is not None else None,
    }


def _quote(term: str) -> str:
    from urllib.parse import quote

    return quote(term, safe="")
