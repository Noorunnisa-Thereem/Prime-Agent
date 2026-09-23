"""Shared HTTP-GET-JSON helper for the external_lookup package.

Handles the three concerns every lookup module would otherwise duplicate:
per-host rate limiting (so we never hammer NCBI E-utils past its published
unauthenticated limit), an on-disk cache of real API responses (keyed by
the exact request URL, so re-running the pipeline doesn't re-issue an
identical live call every time), and a uniform provenance envelope so the
caller always knows whether a result is live-fresh or cached, and exactly
what URL and timestamp back it.

Nothing in this module invents a response: a network failure, a timeout, or
a non-2xx status is always surfaced with the real error message attached,
never swallowed into an empty-but-successful result. ``build_envelope``
below classifies *why* a call failed (rate-limited vs. unreachable vs. an
unexpected server error -- see ``classify_error``/``ERROR_STATUSES``)
rather than collapsing every failure into one generic ``"error"`` status.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..core.utils import atomic_write_json, ensure_dir, read_json, utc_now_iso

USER_AGENT = "PrimeAgent-NeuroTwin-ExternalLookup/0.1 (research/decision-support; contact: patient-prime-agent)"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_CACHE_DIR = Path("reports") / "external_evidence" / ".cache"

# Minimum seconds between requests to a given host. NCBI's published
# unauthenticated E-utils rate limit is 3 requests/second; 0.34s keeps us
# comfortably under that for both PubMed and ClinVar (same host). Other
# hosts get a lighter, still-polite default.
_MIN_INTERVAL_BY_HOST = {
    "eutils.ncbi.nlm.nih.gov": 0.34,
}
_DEFAULT_MIN_INTERVAL = 0.2

_last_call_at_by_host: dict[str, float] = {}


def _throttle(host: str) -> None:
    min_interval = _MIN_INTERVAL_BY_HOST.get(host, _DEFAULT_MIN_INTERVAL)
    last_call = _last_call_at_by_host.get(host)
    if last_call is not None:
        elapsed = time.monotonic() - last_call
        remaining = min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
    _last_call_at_by_host[host] = time.monotonic()


def _cache_path(cache_dir: Path, cache_key: str) -> Path:
    key = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return cache_dir / f"{key}.json"


def fetch_json(
    url: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = 1,
    json_body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """GET (or, when ``json_body`` is given, POST) ``url`` and parse the
    response as JSON, using an on-disk cache of real responses.

    ``json_body`` exists for APIs that only accept POST-with-JSON-body (e.g.
    Tavily's search endpoint) -- every other caller in this package leaves it
    ``None`` and gets the original GET behavior unchanged. When given, it is
    JSON-encoded as the request body with ``Content-Type: application/json``,
    and it is folded into the cache key alongside ``url`` (two different
    bodies posted to the same endpoint must not collide on one cache entry).
    ``extra_headers`` lets a caller add e.g. an ``Authorization`` header
    without duplicating this function's retry/cache/throttle logic.

    Returns an envelope: ``{"url", "http_status", "body", "retrieved_at",
    "from_cache", "error"}``. ``body`` is ``None`` whenever the request
    failed or the response was not valid JSON -- callers must not treat a
    missing ``body`` as an empty-but-successful result.
    """
    cache_key = url if json_body is None else f"{url}|{json.dumps(json_body, sort_keys=True)}"
    cache_file = _cache_path(cache_dir, cache_key)
    if not refresh:
        cached = read_json(cache_file, default=None)
        if isinstance(cached, dict) and cached.get("url") == url:
            cached_copy = dict(cached)
            cached_copy["from_cache"] = True
            return cached_copy

    host = urlparse(url).netloc
    last_error: str | None = None
    http_status: int | None = None
    body: Any = None

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    request_data: bytes | None = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        request_data = json.dumps(json_body).encode("utf-8")

    for attempt in range(max_retries + 1):
        _throttle(host)
        request = urllib.request.Request(url, data=request_data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                http_status = response.status
                raw = response.read()
            try:
                body = json.loads(raw.decode("utf-8"))
                last_error = None
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                body = None
                last_error = f"Response was not valid JSON: {exc}"
            break
        except urllib.error.HTTPError as exc:
            http_status = exc.code
            last_error = f"HTTP {exc.code}: {exc.reason}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))

    envelope = {
        "url": url,
        "http_status": http_status,
        "body": body,
        "retrieved_at": utc_now_iso(),
        "from_cache": False,
        "error": last_error,
    }
    ensure_dir(cache_file.parent)
    if last_error is None and body is not None:
        atomic_write_json(cache_file, envelope)
    return envelope


def fetch_text(
    url: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = 1,
    accept: str = "*/*",
) -> dict[str, Any]:
    """GET ``url`` and return the raw response body as text, using the same
    on-disk cache / throttle / retry contract as ``fetch_json`` -- for an
    endpoint that returns XML or plain text rather than JSON (e.g.
    DailyMed's SPL document endpoint, ``/dailymed/services/v2/spls/
    {setid}.xml``, confirmed live to return ``415 Unsupported Media Type``
    for a ``.json`` request -- there is no JSON variant of that endpoint).
    ``accept`` defaults to ``"*/*"`` deliberately -- confirmed live that
    this specific DailyMed endpoint returns ``406 Not Acceptable`` for an
    explicit ``Accept: application/xml`` or ``Accept: text/xml``, even
    though XML is exactly what it serves; only an unrestricted Accept
    header works.

    Returns the same envelope shape as ``fetch_json``: ``{"url",
    "http_status", "body", "retrieved_at", "from_cache", "error"}`` --
    ``classify_error``/``build_envelope`` work identically on it, since
    neither inspects ``body``'s type. ``body`` is the raw decoded text (or
    ``None`` on failure/non-decodable response), never parsed -- parsing is
    the caller's job (e.g. dailymed_lookup's own XML section extraction),
    kept out of this shared layer the same way JSON parsing is kept out of
    ``fetch_json``'s callers.
    """
    cache_file = _cache_path(cache_dir, url)
    if not refresh:
        cached = read_json(cache_file, default=None)
        if isinstance(cached, dict) and cached.get("url") == url:
            cached_copy = dict(cached)
            cached_copy["from_cache"] = True
            return cached_copy

    host = urlparse(url).netloc
    last_error: str | None = None
    http_status: int | None = None
    body: str | None = None

    headers = {"User-Agent": USER_AGENT, "Accept": accept}

    for attempt in range(max_retries + 1):
        _throttle(host)
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                http_status = response.status
                raw = response.read()
            try:
                body = raw.decode("utf-8")
                last_error = None
            except UnicodeDecodeError as exc:
                body = None
                last_error = f"Response was not valid UTF-8 text: {exc}"
            break
        except urllib.error.HTTPError as exc:
            http_status = exc.code
            last_error = f"HTTP {exc.code}: {exc.reason}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))

    envelope = {
        "url": url,
        "http_status": http_status,
        "body": body,
        "retrieved_at": utc_now_iso(),
        "from_cache": False,
        "error": last_error,
    }
    ensure_dir(cache_file.parent)
    if last_error is None and body is not None:
        atomic_write_json(cache_file, envelope)
    return envelope


# Every failure classify_error() can produce. Deliberately not a single
# generic "error": a caller (or a test) must be able to tell "the API
# rate-limited us" apart from "we couldn't reach the API at all" apart from
# "the API returned an unexpected error" -- these call for different
# responses (wait and retry vs. check connectivity vs. investigate), and
# collapsing them loses exactly the information needed to choose between
# those.
ERROR_STATUSES = frozenset({"rate_limited", "network_error", "http_error", "invalid_response"})

_NOT_RETRIEVED_NOTES = {
    "rate_limited": (
        "Not retrieved in this session: the external API rate-limited this request (HTTP 429). "
        "This is not evidence that no data exists -- retry later, outside the rate-limit window."
    ),
    "network_error": "Not retrieved in this session: a network error prevented contacting the external API.",
    "http_error": "Not retrieved in this session: the external API returned an unexpected error response.",
    "invalid_response": "Not retrieved in this session: the external API returned a response that could not be parsed as JSON.",
}


def classify_error(fetch_result: dict[str, Any]) -> str | None:
    """Categorize a fetch_json failure so callers can distinguish a rate limit
    from a genuine network failure from any other HTTP error, instead of
    every failure collapsing into one indistinguishable state.

    Returns ``None`` when ``fetch_result`` carries no error, otherwise one of
    the values in ``ERROR_STATUSES``.
    """
    error = fetch_result.get("error")
    if error is None:
        return None
    http_status = fetch_result.get("http_status")
    if http_status == 429:
        return "rate_limited"
    if http_status is not None and "was not valid JSON" in error:
        return "invalid_response"
    if http_status is not None:
        return "http_error"
    return "network_error"


def not_retrieved_note(error_category: str) -> str:
    """The doctor-facing "Not retrieved in this session" explanation for one
    of ``ERROR_STATUSES`` -- used both in the JSON envelope's ``note`` field
    and by report_html.py when rendering a failed lookup."""
    return _NOT_RETRIEVED_NOTES[error_category]


def build_envelope(
    *,
    resource: str,
    endpoint: str,
    query: dict[str, Any],
    fetch_result: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Wrap a parsed set of records with the standard provenance envelope.

    ``status`` is one of ``"ok"``, ``"no_results"``, or a value from
    ``ERROR_STATUSES`` -- never a single generic ``"error"``.
    """
    error_category = classify_error(fetch_result)
    if error_category is not None:
        status = error_category
    elif not records:
        status = "no_results"
    else:
        status = "ok"
    envelope = {
        "resource": resource,
        "endpoint": endpoint,
        "query": query,
        "retrieved_at": fetch_result.get("retrieved_at"),
        "from_cache": fetch_result.get("from_cache", False),
        "http_status": fetch_result.get("http_status"),
        "status": status,
        "error": fetch_result.get("error"),
        "count": len(records),
        "records": records,
    }
    if error_category is not None:
        envelope["note"] = not_retrieved_note(error_category)
    return envelope
