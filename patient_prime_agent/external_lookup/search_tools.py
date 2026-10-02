"""One interface over the pipeline's external evidence-search backends.

Four backends live behind :func:`search`: ``pubmed``, ``dailymed``,
``clinicaltrials`` (each a thin wrapper around the existing, already-tested
``pubmed_lookup``/``dailymed_lookup``/``trials_lookup`` modules -- nothing
about their live-call, caching, or matching behavior changes here), and
``tavily`` (new in this module).

Every backend returns the same envelope shape, regardless of which one
answered:

    {
        "source": "pubmed" | "dailymed" | "clinicaltrials" | "tavily",
        "query": <the query this backend was actually asked>,
        "result": {"count": int, "records": [...], ...backend-specific detail...},
        "retrieved_at": "<ISO 8601 timestamp>" | None,
        "verification": "live" | "cached" | "unavailable",
        "status": "ok" | "no_results" | "missing_api_key" | one of
                   http_client.ERROR_STATUSES ("rate_limited" / "network_error" /
                   "http_error" / "invalid_response"),
        "is_medical_database": bool,
    }

``verification`` extends the live/cached distinction every other
external_lookup module already reports (see ``memory_store.recall_or_
compute``) with one more honest state: ``"unavailable"``, for a backend that
was never actually called because it has no usable API key configured. That
is reported as ``status="missing_api_key"``, never silently skipped and never
degraded into a misleading ``"no_results"`` -- "we didn't check" must stay
distinguishable from "we checked and found nothing", exactly the same
principle ``classify_error``'s rate_limited/network_error/http_error split
already applies to a failed live call (see ``http_client.py``).

PubMed, DailyMed, and ClinicalTrials.gov need no API key in this codebase
today (all three are called as public, unauthenticated endpoints -- see
their modules); ``missing_api_key`` can therefore only ever be produced by
the Tavily backend. This module does not invent a key requirement for the
other three where none exists.

Tavily is fundamentally different from the other three and is treated
differently everywhere in this module:

- It is general web search, not a curated medical database. Every Tavily
  envelope carries ``is_medical_database: False`` and a
  ``result["label"] = "General web search (not a medical database)"`` --
  callers (report_html.py, in a later change) must surface that label
  wherever a Tavily finding is shown, and must not give it the same
  badge/styling as a PubMed/DailyMed/ClinicalTrials.gov finding.
- It is never called as a first choice. :func:`search_with_medical_fallback`
  is the only sanctioned way to reach it: it takes the envelopes a caller
  already obtained from the medical-specific sources (PubMed, ClinVar,
  DailyMed, CPIC, ClinicalTrials.gov -- ClinVar and CPIC are not among this
  module's own 4 backends, but their envelopes share the same
  ``status``-bearing shape from ``clinvar_lookup``/``cpic_lookup``, so they
  can be passed in here too) and only calls Tavily when every single one of
  them is a genuine, checked ``"no_results"``. A source that errored
  (rate-limited, network failure, ...) or that found something is never
  treated as "exhausted" -- Tavily fills the gap left by "nothing was
  found", not the gap left by "we couldn't check".

Every backend still goes through the same guardrails every other
external_lookup module uses: ``guardrails.validate_query_term`` rejects
free text before it ever leaves the process (this applies to Tavily too --
this module accepts only the same normalized drug name / gene symbol /
short combined term the medical sources already validated for the same
lookup, never a hand-typed natural-language question), and every
third-party text field is run through ``sanitize_response_field`` before
it is returned. Tavily's results link to arbitrary domains (that is the
point of general web search), so ``guardrails.validate_source_url``'s
domain-pinned check does not apply to it; ``_validate_general_url`` below
is the deliberately looser, scheme-only check used for Tavily links only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

from ..agentic.settings import parse_env_file
from . import dailymed_lookup, memory_store, pubmed_lookup, trials_lookup
from .guardrails import sanitize_response_field, validate_query_term
from .http_client import DEFAULT_CACHE_DIR, build_envelope, fetch_json, fetch_text

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE_PATH = _PROJECT_ROOT / ".env"

TAVILY_API_KEY_ENV = "TAVILY_API_KEY"
TAVILY_RESOURCE_NAME = "Tavily (general web search)"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_LABEL = "General web search (not a medical database)"

MISSING_API_KEY_STATUS = "missing_api_key"

_REMEMBER_STATUSES = frozenset({"ok", "no_results"})

# Any medical-specific source's envelope can be passed to
# search_with_medical_fallback, whether or not it's one of this module's own
# 4 backends -- see the module docstring.
MEDICAL_SOURCE_NAMES = ("pubmed", "clinvar", "dailymed", "cpic", "clinicaltrials")


def _get_api_key(env_var: str) -> str | None:
    """Look up ``env_var`` in the process environment first, then in the
    project's ``.env`` file (already gitignored -- see ``.env.example``),
    reusing the same minimal parser ``agentic/settings.py`` uses so this
    module does not grow a second ``.env`` parsing implementation. Returns
    ``None`` -- never ``""`` -- when the key is absent or blank, so callers
    can use a single truthiness check."""
    value = os.environ.get(env_var)
    if value and value.strip():
        return value.strip()
    file_values = parse_env_file(_ENV_FILE_PATH)
    value = file_values.get(env_var)
    return value.strip() if value and value.strip() else None


def _validate_general_url(url: object) -> str | None:
    """The Tavily-only counterpart to ``guardrails.validate_source_url``.

    Tavily returns links to arbitrary sites across the open web -- there is
    no single ``expected_domain`` to pin them to the way every other
    external_lookup module pins its records to the one source it queried.
    This keeps only the http(s)-scheme check (rejecting ``javascript:``,
    ``data:``, and similar), which is the part of the domain-pinned
    validator that still makes sense with no fixed domain to compare
    against."""
    if not isinstance(url, str):
        return None
    stripped = url.strip()
    if stripped.lower().startswith("http://") or stripped.lower().startswith("https://"):
        return stripped
    return None


def _missing_api_key_envelope(resource: str, query: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource": resource,
        "endpoint": None,
        "query": query,
        "retrieved_at": None,
        "from_cache": False,
        "http_status": None,
        "status": MISSING_API_KEY_STATUS,
        "error": None,
        "count": 0,
        "records": [],
        "note": (
            f"Not queried: {TAVILY_API_KEY_ENV} is not configured. Set it in the environment or "
            "in a .env file (see .env.example) to enable the Tavily web-search fallback."
        ),
        "verification": "unavailable",
    }


def _normalize(source: str, raw_envelope: dict[str, Any], *, is_medical: bool) -> dict[str, Any]:
    """Reshape one of the existing per-source envelopes (``resource``,
    ``endpoint``, ``from_cache``, ``count``, ``records``, ...) into this
    module's standard ``source``/``query``/``result``/``retrieved_at``/
    ``verification``/``status`` shape, without dropping any of the original
    detail -- everything besides the five promoted fields is kept, verbatim,
    under ``result``."""
    verification = raw_envelope.get("verification")
    if verification not in ("live", "cached", "unavailable"):
        verification = "cached" if raw_envelope.get("from_cache") else "live"

    result = {
        key: value
        for key, value in raw_envelope.items()
        if key not in {"query", "retrieved_at", "verification", "status", "from_cache", "resource"}
    }

    envelope: dict[str, Any] = {
        "source": source,
        "query": raw_envelope.get("query"),
        "result": result,
        "retrieved_at": raw_envelope.get("retrieved_at"),
        "verification": verification,
        "status": raw_envelope.get("status"),
        "is_medical_database": is_medical,
    }
    if not is_medical:
        result["label"] = TAVILY_LABEL
    return envelope


# ---------------------------------------------------------------------------
# The 4 backends
# ---------------------------------------------------------------------------


def search_pubmed_backend(term: str, **kwargs: Any) -> dict[str, Any]:
    """PubMed via the existing, already-tested ``pubmed_lookup.search_pubmed``
    -- unchanged live-call/caching/relevance-filtering behavior, reshaped to
    this module's standard envelope."""
    raw = pubmed_lookup.search_pubmed(term, **kwargs)
    return _normalize("pubmed", raw, is_medical=True)


def search_dailymed_backend(drug_name: str, **kwargs: Any) -> dict[str, Any]:
    """DailyMed via the existing ``dailymed_lookup.search_dailymed``."""
    raw = dailymed_lookup.search_dailymed(drug_name, **kwargs)
    return _normalize("dailymed", raw, is_medical=True)


def search_clinicaltrials_backend(condition: str, drug_name: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """ClinicalTrials.gov via the existing ``trials_lookup.search_trials``."""
    raw = trials_lookup.search_trials(condition, drug_name, **kwargs)
    return _normalize("clinicaltrials", raw, is_medical=True)


def search_tavily(
    query: str,
    *,
    max_results: int = 5,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """General web search via the Tavily API -- see the module docstring for
    why this is a fallback-only source, never a first choice.

    ``query`` goes through the same ``validate_query_term`` guardrail every
    other external_lookup query does: a bare drug name, gene symbol, or
    short combined term, never free text.

    When ``TAVILY_API_KEY`` is not configured, this makes no network call
    and no cache/memory access at all -- it returns
    ``status="missing_api_key"`` immediately, so a misconfigured deployment
    is visibly "not queried", never silently or incorrectly "no_results".
    """
    query = validate_query_term(query, field_name="query")

    api_key = _get_api_key(TAVILY_API_KEY_ENV)
    if not api_key:
        raw = _missing_api_key_envelope(TAVILY_RESOURCE_NAME, {"query": query, "max_results": max_results})
        return _normalize("tavily", raw, is_medical=False)

    def _live_lookup() -> dict[str, Any]:
        body = {
            "api_key": api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": int(max_results),
            "include_answer": False,
            # The underlying page's real content, not just the search-result snippet --
            # a search snippet is itself a kind of title/teaser (the same problem a bare
            # PubMed title or ClinicalTrials.gov brief_title has), so a Finding built only
            # from it risks restating what the page is about rather than what it actually
            # says. Tavily includes this at no extra cost when requested.
            "include_raw_content": True,
        }
        fetch_result = fetch_json(TAVILY_SEARCH_URL, cache_dir=cache_dir, refresh=refresh, json_body=body)

        query_for_envelope = {"query": query, "max_results": max_results}
        if fetch_result.get("error") is not None:
            return build_envelope(
                resource=TAVILY_RESOURCE_NAME,
                endpoint=TAVILY_SEARCH_URL,
                query=query_for_envelope,
                fetch_result=fetch_result,
                records=[],
            )

        results = (fetch_result.get("body") or {}).get("results", [])
        records = [_tavily_record(entry) for entry in results if isinstance(entry, dict) and entry.get("url")]

        return build_envelope(
            resource=TAVILY_RESOURCE_NAME,
            endpoint=TAVILY_SEARCH_URL,
            query=query_for_envelope,
            fetch_result=fetch_result,
            records=records,
        )

    raw = memory_store.recall_or_compute(
        TAVILY_RESOURCE_NAME,
        query,
        _live_lookup,
        store_path=memory_path,
        refresh=refresh,
        remember_statuses=_REMEMBER_STATUSES,
    )
    return _normalize("tavily", raw, is_medical=False)


def _tavily_record(entry: dict[str, Any]) -> dict[str, Any]:
    """Build one output record, running every third-party text field through
    sanitize_response_field and the URL through the scheme-only Tavily
    validator (see ``_validate_general_url``) before either ever reaches
    External_Evidence_Report.json or a rendered report."""
    return {
        "title": sanitize_response_field(entry.get("title")) or None,
        "url": _validate_general_url(entry.get("url")),
        "content_snippet": sanitize_response_field(entry.get("content"), max_length=400) or None,
        # The underlying page's real text (see search_tavily's include_raw_content) --
        # None when Tavily didn't return one (not every page can be fetched/parsed
        # server-side); callers prefer this over content_snippet when present, since a
        # snippet is itself a teaser, not necessarily the page's own stated finding.
        # Tavily's own bundled extraction, when it returns one -- NOT used for Finding
        # generation any more (see fetch_general_page_text below): it is absent often
        # enough in practice that a caller relying on it alone ends up silently falling
        # back to the bare title/snippet, which is exactly the bug fetch_general_page_text
        # fixes by independently fetching the result's own URL instead. Kept here only as
        # raw provenance/audit data.
        "raw_content": sanitize_response_field(entry.get("raw_content"), max_length=2000) or None,
        "score": entry.get("score") if isinstance(entry.get("score"), (int, float)) else None,
    }


PAGE_FETCH_RESOURCE_NAME = "General web page fetch"

# Turns common block-level closing tags (and <br>) into a sentence boundary before the
# rest of the markup is stripped -- see fetch_general_page_text's own docstring for why:
# these carry no punctuation of their own, so without this a page's navigation menu and
# its real article text merge into a single run-on "sentence" with no boundary between
# them for pubmed_lookup.find_combination_excerpt to use.
_BLOCK_BOUNDARY_RE = re.compile(r"</(?:p|div|li|h[1-6]|tr|section|article|header|footer|nav)\s*>|<br\s*/?>", re.IGNORECASE)
_REPEATED_SENTENCE_PUNCTUATION_RE = re.compile(r"[.!?]\s*(?:[.!?]\s*)+")


def fetch_general_page_text(
    url: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
    max_length: int = 4000,
) -> dict[str, Any]:
    """Independently fetch ``url`` (a Tavily search result's own link) and
    return its real page text -- this is the actual resource a Tavily
    Finding must be grounded in, not Tavily's own bundled
    ``include_raw_content`` extraction (``_tavily_record``'s ``raw_content``
    field), which the live API simply does not return for every result and
    whose extraction quality this project does not control either way. This
    gives ``ddi_flag_evidence._tavily_finding_and_url`` a resource it
    fetched itself, exactly the same way ``dailymed_lookup.
    fetch_label_interactions_section`` and ``pubmed_lookup.
    fetch_abstract_sections`` fetch their own sources' real content rather
    than relying on a search result's title or snippet.

    Returns a standard envelope: ``status="ok"`` with the page's readable
    text in ``records[0]["page_text"]`` when the fetch succeeded and the
    body had extractable text; ``status="no_results"`` when the fetch
    succeeded but there was nothing extractable (an empty body, or a body
    that is entirely markup/script with no text content); a real error
    status (``http_client.ERROR_STATUSES``) when the HTTP fetch itself
    failed. Never raises, and never returns fabricated text -- a caller
    must treat anything other than ``"ok"`` with a non-empty ``page_text``
    exactly like a source that returned nothing (see ``_tavily_finding_and_
    url``, which never falls back to title/snippet when this fails).

    Text extraction is intentionally simple: the same tag-stripping
    ``sanitize_response_field`` already applies to every other third-party
    field in this package (strip ``<script>``/``<style>`` blocks and
    remaining tags, collapse whitespace, length-cap), not a readability or
    boilerplate-removal algorithm -- so navigation/footer text can still be
    mixed into ``page_text``. To keep that boilerplate from merging into one
    giant run-on "sentence" with the real content (a real, observed second
    bug: raw HTML block elements such as ``<p>``/``<div>``/``<li>`` carry no
    sentence-ending punctuation of their own, so naive tag-stripping alone
    leaves no boundary between a page's navigation menu and its actual
    article text), block-level closing tags are first turned into sentence
    boundaries before the remaining markup is stripped -- a deliberately
    simple heuristic, not a layout-aware readability algorithm, but one that
    lets ``pubmed_lookup.find_combination_excerpt`` actually land on the
    real matching sentence instead of a boilerplate-prefixed blob. The
    caller still requires an explicit combination-language match
    (``find_combination_excerpt``) before trusting any of this as a
    Finding, exactly as it does for every other source, so boilerplate that
    fails that check simply yields no Finding from this source -- never a
    fabricated one.
    """
    validated_url = _validate_general_url(url)
    if not validated_url:
        return build_envelope(
            resource=PAGE_FETCH_RESOURCE_NAME,
            endpoint="",
            query={"url": url},
            fetch_result={"error": "invalid or unsafe URL", "retrieved_at": None, "from_cache": False, "http_status": None},
            records=[],
        )

    def _live_fetch() -> dict[str, Any]:
        fetch_result = fetch_text(validated_url, cache_dir=cache_dir, refresh=refresh)
        query = {"url": validated_url}
        if fetch_result.get("error") is not None:
            return build_envelope(resource=PAGE_FETCH_RESOURCE_NAME, endpoint=validated_url, query=query, fetch_result=fetch_result, records=[])

        with_sentence_boundaries = _BLOCK_BOUNDARY_RE.sub(". ", fetch_result.get("body") or "")
        page_text = sanitize_response_field(with_sentence_boundaries, max_length=max_length)
        # A block tag inserted right after text that already ended in its own punctuation
        # (e.g. "...levels significantly.</p>") would otherwise leave a doubled "..".
        page_text = _REPEATED_SENTENCE_PUNCTUATION_RE.sub(".", page_text)
        if not page_text:
            return build_envelope(resource=PAGE_FETCH_RESOURCE_NAME, endpoint=validated_url, query=query, fetch_result=fetch_result, records=[])

        return build_envelope(
            resource=PAGE_FETCH_RESOURCE_NAME,
            endpoint=validated_url,
            query=query,
            fetch_result=fetch_result,
            records=[{"url": validated_url, "page_text": page_text}],
        )

    return memory_store.recall_or_compute(
        PAGE_FETCH_RESOURCE_NAME,
        validated_url,
        _live_fetch,
        store_path=memory_path,
        refresh=refresh,
        remember_statuses=_REMEMBER_STATUSES,
    )


# ---------------------------------------------------------------------------
# One dispatcher over all 4 backends
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, Callable[..., dict[str, Any]]] = {
    "pubmed": search_pubmed_backend,
    "dailymed": search_dailymed_backend,
    "clinicaltrials": search_clinicaltrials_backend,
    "tavily": search_tavily,
}


def search(backend: str, **kwargs: Any) -> dict[str, Any]:
    """Call one of the 4 backends by name and get back the standard envelope.
    Raises ``ValueError`` for an unknown backend name -- this never silently
    falls back to a different backend than the one asked for."""
    try:
        backend_fn = _BACKENDS[backend]
    except KeyError:
        raise ValueError(f"Unknown search backend {backend!r}; must be one of {sorted(_BACKENDS)}") from None
    return backend_fn(**kwargs)


# ---------------------------------------------------------------------------
# The sanctioned way to reach Tavily: only after the medical sources exhaust
# ---------------------------------------------------------------------------


def all_medical_sources_exhausted(medical_envelopes: list[dict[str, Any]]) -> bool:
    """True only if every envelope in ``medical_envelopes`` explicitly
    reports ``status == "no_results"``.

    An empty list returns ``False`` -- "no medical source was even checked"
    is not the same as "every medical source checked and found nothing",
    and must not trigger the web-search fallback either. A source that
    errored (rate_limited/network_error/http_error/invalid_response) also
    returns ``False`` for the whole set: Tavily is a fallback for "nothing
    found", not a substitute for a source that simply failed to answer.
    """
    if not medical_envelopes:
        return False
    return all(envelope.get("status") == "no_results" for envelope in medical_envelopes)


def search_with_medical_fallback(
    query: str,
    medical_envelopes: list[dict[str, Any]],
    *,
    max_results: int = 5,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any] | None:
    """Call Tavily for ``query`` only when ``medical_envelopes`` (the
    envelopes a caller already obtained from PubMed, ClinVar, DailyMed,
    CPIC, and ClinicalTrials.gov for the same query) are all a genuine
    ``"no_results"`` -- see ``all_medical_sources_exhausted``.

    Returns ``None`` -- meaning: do not call Tavily, and there is no Tavily
    envelope to show -- when that condition is not met, so a caller never
    has to special-case "Tavily wasn't asked for" separately from "Tavily
    was asked and is missing its API key" (which is a real envelope with
    ``status="missing_api_key"``, returned only once Tavily is actually
    called).
    """
    if not all_medical_sources_exhausted(medical_envelopes):
        return None
    return search_tavily(query, max_results=max_results, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path)
