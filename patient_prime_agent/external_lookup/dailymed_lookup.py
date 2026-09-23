"""Live DailyMed lookups via the NLM DailyMed REST API v2.

One call: ``GET /dailymed/services/v2/spls.json?drug_name=<name>`` returns
matching Structured Product Labels (SPLs) with a ``setid`` that resolves to
the canonical label page. Field shape (``setid``, ``spl_version``,
``published_date``, ``title``) confirmed against a live call during
development.

Before the HTTP call, ``memory_store.recall_or_compute`` checks the
long-term memory store (keyed by drug name, 30-day TTL) -- see
``memory_store.py``.

Label content (``fetch_label_interactions_section``)
------------------------------------------------------
``search_dailymed`` above only ever returns a label *pointer* (setid,
title) -- the search endpoint has no interaction/warnings section content.
Getting the label's actual text requires a second, different real
endpoint: ``GET /dailymed/services/v2/spls/{setid}.xml``, the full
Structured Product Label (SPL) document -- confirmed live (the ``.json``
variant of this specific endpoint returns ``415 Unsupported Media Type``;
XML is the only representation DailyMed serves it in). This is HL7 CDA/SPL
XML with a default ``urn:hl7-org:v3`` namespace; the section covering drug
interactions is standardized (not DailyMed-specific) as LOINC code
``34073-7`` "DRUG INTERACTIONS SECTION" -- confirmed live against a real
label (aripiprazole's), which does carry a ``<section><code
code="34073-7".../>`` element containing real prose ("no dosage adjustment
is necessary for valproate, lithium, lamotrigine, lorazepam, or sertraline
when coadministered with aripiprazole..."). ``fetch_label_interactions_section``
fetches and extracts exactly that section's text, verbatim (tags stripped,
whitespace collapsed) -- never a summary or paraphrase of it. A label with
no such section, or a fetch/parse failure, is reported as ``no_results`` or
the real error, never silently treated as "no interaction" -- see that
function's own docstring.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import memory_store
from .guardrails import sanitize_response_field, validate_query_term, validate_source_url
from .http_client import DEFAULT_CACHE_DIR, build_envelope, fetch_json, fetch_text

RESOURCE_NAME = "DailyMed (NLM REST API v2)"
_SPLS_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json"
_SPL_DOCUMENT_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{setid}.xml"
_EXPECTED_DOMAIN = "dailymed.nlm.nih.gov"
_REMEMBER_STATUSES = frozenset({"ok", "no_results"})

_LABEL_CONTENT_RESOURCE_NAME = "DailyMed label content (NLM REST API v2, SPL document)"
_HL7_V3_NS = "urn:hl7-org:v3"
_DRUG_INTERACTIONS_LOINC = "34073-7"  # standardized LOINC code, not DailyMed-specific -- see module docstring


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


def _extract_drug_interactions_section_text(xml_text: str) -> str | None:
    """Parse a full SPL XML document and return the plain-text content of
    its DRUG INTERACTIONS section (LOINC 34073-7), tags stripped and
    whitespace collapsed -- the section's own real words, verbatim, never a
    summary. Returns ``None`` when the document has no such section (a
    real, valid outcome -- not every label carries one) or the XML cannot
    be parsed at all (a malformed/unexpected response, reported by the
    caller as an error, not silently treated as "no section")."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for section in root.iter(f"{{{_HL7_V3_NS}}}section"):
        code = section.find(f"{{{_HL7_V3_NS}}}code")
        if code is not None and code.get("code") == _DRUG_INTERACTIONS_LOINC:
            text = " ".join(section.itertext())
            return " ".join(text.split()) or None
    return None


_MENTION_EXCERPT_WINDOW = 300


def find_drug_mention_excerpt(section_text: str, other_drug_name: str) -> str | None:
    """A real excerpt of ``section_text`` (a DRUG INTERACTIONS section
    already fetched by ``fetch_label_interactions_section``) centered on
    the first word-boundary mention of ``other_drug_name``, trimmed toward
    sentence boundaries where one falls within the excerpt window -- never
    the drug's full interactions section (which usually covers many
    unrelated drug classes and would bury the one relevant sentence in
    noise), and never paraphrased beyond trimming: every word inside the
    excerpt is the label's own.

    Returns ``None`` when ``other_drug_name`` is not mentioned as a whole
    word anywhere in the section -- the caller (ddi_flag_evidence.py) is
    responsible for turning that into an honest "not mentioned in reviewed
    label section" note, never silence-as-safe."""
    if not isinstance(section_text, str) or not section_text.strip():
        return None
    needle = other_drug_name.strip()
    if not needle:
        return None
    match = re.search(r"\b" + re.escape(needle.lower()) + r"\b", section_text.lower())
    if match is None:
        return None

    start = max(0, match.start() - _MENTION_EXCERPT_WINDOW)
    end = min(len(section_text), match.end() + _MENTION_EXCERPT_WINDOW)
    excerpt = section_text[start:end].strip()

    first_period = excerpt.find(". ")
    if 0 < first_period < _MENTION_EXCERPT_WINDOW:
        excerpt = excerpt[first_period + 2 :]
    last_period = excerpt.rfind(". ")
    if last_period != -1 and last_period > len(excerpt) - _MENTION_EXCERPT_WINDOW:
        excerpt = excerpt[: last_period + 1]

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(section_text) else ""
    return sanitize_response_field(f"{prefix}{excerpt}{suffix}", max_length=700)


def fetch_label_interactions_section(
    setid: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """Fetch one label's real DRUG INTERACTIONS section text (LOINC
    34073-7) by its DailyMed ``setid`` (from a ``search_dailymed`` record).

    Returns a standard envelope (``status`` "ok"/"no_results"/one of
    ``http_client.ERROR_STATUSES``): ``"ok"`` with the section's verbatim
    text in ``records[0]["section_text"]`` when the label has one;
    ``"no_results"`` when the document was fetched successfully but simply
    has no DRUG INTERACTIONS section (some labels genuinely don't -- e.g. a
    product with no known interactions to report) or could not be parsed as
    XML; a real error status when the fetch itself failed. This never
    reports "no_results" as if it meant "no interactions exist" -- a caller
    checking a specific other drug's name against this text must still
    treat "the section itself doesn't exist" and "the section exists but
    doesn't mention this drug" as two different, both-real outcomes (see
    ddi_flag_evidence.py's use of this function)."""
    clean_setid = sanitize_response_field(setid, max_length=60)
    if not clean_setid:
        return build_envelope(
            resource=_LABEL_CONTENT_RESOURCE_NAME,
            endpoint="",
            query={"setid": setid},
            fetch_result={"error": "empty or invalid setid", "retrieved_at": None, "from_cache": False, "http_status": None},
            records=[],
        )

    def _live_lookup() -> dict[str, Any]:
        endpoint = _SPL_DOCUMENT_URL.format(setid=clean_setid)
        fetch_result = fetch_text(endpoint, cache_dir=cache_dir, refresh=refresh)
        query = {"setid": clean_setid}

        if fetch_result.get("error") is not None:
            return build_envelope(resource=_LABEL_CONTENT_RESOURCE_NAME, endpoint=endpoint, query=query, fetch_result=fetch_result, records=[])

        section_text = _extract_drug_interactions_section_text(fetch_result.get("body") or "")
        if section_text is None:
            return build_envelope(resource=_LABEL_CONTENT_RESOURCE_NAME, endpoint=endpoint, query=query, fetch_result=fetch_result, records=[])

        clean_text = sanitize_response_field(section_text, max_length=4000)
        record = {
            "setid": clean_setid,
            "section_text": clean_text,
            "url": validate_source_url(f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={clean_setid}", _EXPECTED_DOMAIN),
        }
        return build_envelope(resource=_LABEL_CONTENT_RESOURCE_NAME, endpoint=endpoint, query=query, fetch_result=fetch_result, records=[record])

    return memory_store.recall_or_compute(
        _LABEL_CONTENT_RESOURCE_NAME,
        clean_setid,
        _live_lookup,
        store_path=memory_path,
        refresh=refresh,
        remember_statuses=_REMEMBER_STATUSES,
    )


def _quote(term: str) -> str:
    from urllib.parse import quote

    return quote(term, safe="")
