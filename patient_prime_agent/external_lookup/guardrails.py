"""Input/output guardrails shared by every module in external_lookup/.

Two independent checkpoints live here:

``validate_query_term`` is the INPUT guardrail. By the time a value reaches
this package, Path B (``genetics_summary.py``, ``clinical_notes_summary.py``)
has already reduced the patient's record down to a normalized drug name or
gene symbol (e.g. ``"lamotrigine"``, ``"SCN1A"``) -- see
``external_evidence_summary.py``'s ``_current_drug_names`` and
``_relevant_gene_drug_pairs``. This is the last checkpoint before such a
value leaves the process in an HTTP request: it enforces a narrow allowlist
and a short max length so that clinical-notes free text, a patient name, an
address, or an ID string with stray punctuation can never reach PubMed,
ClinVar, DailyMed, CPIC, or ClinicalTrials.gov as a query term. It is
fail-closed -- a term that does not pass raises ``QueryValidationError``
immediately, before any URL is built or any socket is opened.

``sanitize_response_field`` and ``validate_source_url`` are the OUTPUT
guardrails. Everything these five modules get back is third-party content
(a title, a journal name, a review status, a label URL, ...); before any of
it is written into ``External_Evidence_Report.json`` or rendered anywhere,
every text field is stripped of HTML/script content, length-capped, and
whitespace-collapsed (escaping for render is report_html.py's ``_t()`` job,
not this module's -- see sanitize_response_field's docstring), and every URL
is confirmed to actually belong to the source it claims to come from.
Unlike the input guardrail, these do not raise --
a field that fails sanitization degrades to an empty string, and a URL that
fails domain validation degrades to ``None``, so one malformed record from
a live API cannot break the whole pipeline run.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# A normalized drug name ("levetiracetam"), gene symbol ("SCN1A", "CYP3A4"),
# short diagnosis phrase used as a trial condition ("Focal impaired-awareness
# seizures"), or a simple combined query ("SCN1A AND lamotrigine") all fit
# this shape. Clinical notes, patient names/IDs with punctuation, and other
# free text generally do not.
MAX_TERM_LENGTH = 60
_ALLOWED_CHARS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\- ]*$")


class QueryValidationError(ValueError):
    """Raised when a value is not a bare drug name/gene symbol/short query
    term -- e.g. it looks like free text, carries punctuation outside the
    allowlist, or is unreasonably long. Callers must not catch this and
    substitute a default term; the correct response is to not query at
    all."""


def validate_query_term(term: object, *, field_name: str = "term") -> str:
    """Validate that ``term`` is safe to send to a public external API.

    Returns the stripped term unchanged when valid. Raises
    ``QueryValidationError`` otherwise -- callers must invoke this on every
    value before it is used to build a request URL, and must not perform
    the HTTP call if it raises.
    """
    if not isinstance(term, str):
        raise QueryValidationError(f"{field_name} must be a string, got {type(term).__name__!r}")

    stripped = term.strip()
    if not stripped:
        raise QueryValidationError(f"{field_name} is empty")

    if len(stripped) > MAX_TERM_LENGTH:
        raise QueryValidationError(
            f"{field_name} is {len(stripped)} characters, exceeding the {MAX_TERM_LENGTH}-character "
            "limit for a normalized drug name or gene symbol -- this looks like free text, not a "
            "single term, and will not be sent to any external API"
        )

    if "  " in stripped:
        raise QueryValidationError(
            f"{field_name} {stripped!r} contains repeated whitespace, which does not match a "
            "normalized drug name or gene symbol"
        )

    if not _ALLOWED_CHARS_RE.match(stripped):
        raise QueryValidationError(
            f"{field_name} {stripped!r} contains characters outside the allowed set (letters, "
            "digits, spaces, hyphens) -- this looks like free text or notes content, not a "
            "normalized drug name or gene symbol, and will not be sent to any external API"
        )

    return stripped


# ---------------------------------------------------------------------------
# Output guardrails: sanitize third-party response content before it is
# stored or rendered.
# ---------------------------------------------------------------------------

DEFAULT_MAX_FIELD_LENGTH = 500

_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_response_field(text: object, max_length: int = DEFAULT_MAX_FIELD_LENGTH) -> str:
    """Sanitize one text field returned by an external API (a title, journal
    name, clinical-significance description, trial status, ...) before it is
    written into ``External_Evidence_Report.json`` or rendered anywhere.

    1. Strips ``<script>``/``<style>`` blocks (tag AND content) and any
       remaining HTML tags.
    2. Collapses whitespace left behind by stripped tags.
    3. Truncates to ``max_length`` characters.

    This deliberately does NOT HTML-escape the result: the JSON this feeds
    (``External_Evidence_Report.json``) should hold plain text, and
    ``report_html.py``'s own ``_t()`` is the single place that HTML-escapes
    for rendering. Escaping here too would double-escape on render -- a
    title containing "&" would show the literal text "&amp;" in the PDF
    instead of "&". Stripping tags/scripts is still required at this layer
    regardless, since a title containing real markup must never reach the
    render layer intact even before ``_t()`` gets to it.

    A non-string input (``None``, a number, ...) returns ``""`` -- callers
    already treat an empty string the same as a missing value. This never
    raises: a malformed field from a live API degrades to a shorter or
    emptier string, it does not abort the lookup.
    """
    if not isinstance(text, str):
        return ""

    without_scripts = _SCRIPT_BLOCK_RE.sub(" ", text)
    without_styles = _STYLE_BLOCK_RE.sub(" ", without_scripts)
    without_tags = _TAG_RE.sub(" ", without_styles)
    without_control_chars = _CONTROL_CHARS_RE.sub("", without_tags)
    collapsed = _WHITESPACE_RE.sub(" ", without_control_chars).strip()

    return collapsed[: max(0, int(max_length))]


# (domain, required path prefix or "") pairs an external_lookup module may pass
# as `expected_domain` to validate_source_url, e.g. "ncbi.nlm.nih.gov/clinvar".
# CPIC's own guideline content now lives on clinpgx.org (CPIC's knowledge base
# was rebranded from "CPIC" to "ClinPGx"), confirmed against a live call to
# api.cpicpgx.org during development -- cpicpgx.org (the API host) is kept as
# an allowed fallback in case a future field points back at the API host
# itself. Both must be listed; validate_source_url only ever checks one
# `expected_domain` per call, so cpic_lookup.py tries each in turn.
CPIC_GUIDELINE_DOMAINS = ("clinpgx.org", "cpicpgx.org")


def validate_source_url(url: object, expected_domain: str) -> str | None:
    """Confirm ``url`` is an http(s) URL whose host matches ``expected_domain``
    (or a subdomain of it).

    ``expected_domain`` may include a required path prefix after a ``/``
    (e.g. ``"ncbi.nlm.nih.gov/clinvar"``) for a source where the domain
    alone is shared with unrelated content.

    Returns ``url`` unchanged when it matches. Returns ``None`` -- rejecting
    the URL -- when it does not: a missing/malformed URL, a URL on a
    different domain than the source it claims to come from, or a
    non-http(s) scheme (``javascript:``, ``data:``, ...). Callers must not
    render or store a URL this function rejects.
    """
    if not isinstance(url, str) or not url.strip():
        return None

    domain_part, _, path_prefix = expected_domain.partition("/")
    domain_part = domain_part.lower()

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    host = (parsed.hostname or "").lower()
    host_matches = host == domain_part or host.endswith("." + domain_part)
    if not host_matches:
        return None

    if path_prefix and not parsed.path.startswith("/" + path_prefix.strip("/")):
        return None

    return url.strip()
