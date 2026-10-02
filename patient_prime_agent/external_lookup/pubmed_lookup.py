"""Live PubMed lookups via NCBI E-utils (esearch + esummary).

Two real HTTP calls per query: ``esearch`` resolves a drug/gene search term
to a list of PMIDs, then ``esummary`` fetches the title/author/date for
each PMID. Response fields below (``pubdate``, ``lastauthor``, ``source``,
``title``) were confirmed against a live call to
``eutils.ncbi.nlm.nih.gov`` during development -- see the module docstring
of ``http_client.py`` for the shared fetch/cache/rate-limit contract.

Before either HTTP call, ``memory_store.recall_or_compute`` checks the
long-term memory store (keyed by this term, 30-day TTL) so a re-run of the
pipeline on the same patient data does not re-issue an identical live call
-- see ``memory_store.py``.

Structured abstracts (``fetch_abstract_sections``)
------------------------------------------------------
``search_pubmed``/``search_pubmed_drug_pair`` above only ever return a
citation *pointer* (title, journal, date) -- a real, observed bug
downstream (see ``ddi_flag_evidence.py``) used a paper's title as if it
were the pair's actual finding, when a title states the paper's *aim*, not
its *result* (the same class of bug ``trials_lookup.
fetch_trial_outcome_summary`` fixes for ClinicalTrials.gov).
``fetch_abstract_sections`` fetches the real abstract via
``GET efetch.fcgi?db=pubmed&id={pmid}&rettype=abstract&retmode=xml``
(confirmed live) and returns it split by its own structured
``<AbstractText Label="..." NlmCategory="...">`` sections when the journal
provided them (confirmed live against a real structured abstract:
PURPOSE/OBJECTIVE, METHODS, KEY FINDINGS/RESULTS, and CONCLUSIONS sections,
each its own element) -- so a caller can select the Results/Conclusions
text specifically, never the Background/Objective text a title-like
sentence usually lives in. An unstructured abstract (a single block, no
``Label``) is returned as one ``"UNLABELED"`` section -- still the real
abstract text, better grounded than a title alone, but the caller is
expected to treat it as lower-confidence than a labeled Results/Conclusions
section since it cannot be split by role.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from . import memory_store
from .guardrails import sanitize_response_field, validate_query_term, validate_source_url
from .http_client import DEFAULT_CACHE_DIR, build_envelope, fetch_json, fetch_text

RESOURCE_NAME = "PubMed (NCBI E-utils)"
_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_EXPECTED_DOMAIN = "pubmed.ncbi.nlm.nih.gov"
_REMEMBER_STATUSES = frozenset({"ok", "no_results"})

_ABSTRACT_RESOURCE_NAME = "PubMed abstract (NCBI E-utils, efetch)"
_RESULT_LABELS = ("RESULTS", "RESULT", "KEY FINDINGS", "FINDINGS")
_CONCLUSION_LABELS = ("CONCLUSIONS", "CONCLUSION", "INTERPRETATION")

# Phrases distinguishing "these two drugs were genuinely given/evaluated together" from
# "these two drugs were compared against each other as separate treatments" -- see
# describes_genuine_combination's own docstring for the real failure mode this rejects.
_COMBINATION_PHRASES = (
    "combined with", "combination with", "combination therapy", "coadministered",
    "co-administered", "coadministration", "co-administration", "concomitant",
    "concomitantly", "adjunct", "adjunctive", "augmentation with", "augmented with",
    "added to", "add-on", "in combination", "together with",
)
_COMPARATOR_PHRASES = (
    " versus ", " vs ", " vs. ", "compared with", "compared to", "as compared",
    "non-inferiority", "switching to", "switched to", "switch to", "head-to-head",
    "randomized to either",
)


def search_pubmed(
    term: str,
    *,
    retmax: int = 5,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """Search PubMed for ``term`` (a drug name, gene symbol, or a combined
    ``"GENE AND DRUG"`` query -- see external_evidence_summary.py's
    gene-drug-pair lookup) and return the top ``retmax`` citations with a
    full provenance envelope."""
    term = validate_query_term(term, field_name="term")
    # A combined pair query is always built as "GENE AND DRUG" (see
    # external_evidence_summary.py); the last component is the drug either
    # way (a bare single-term query has only one component). Used below to
    # pick which already-fetched candidate to show first -- see
    # _reorder_by_drug_relevance for why esearch's own ranking isn't
    # trusted blindly. (A [tiab]-scoped query was tried and rejected: it
    # empirically narrowed esearch's own candidate window away from the
    # paper that actually names the drug, for real "GENE AND DRUG" queries
    # against this dataset's genes/drugs -- confirmed live during
    # development. The unscoped query still combines both terms; this
    # reordering step is what makes the result the DRUG is verifiably
    # referenced in, not the query itself.)
    _term_parts = [p.strip() for p in term.split(" AND ") if p.strip()]
    _drug_anchor = _term_parts[-1] if _term_parts else None
    _other_terms = _term_parts[:-1]

    def _live_lookup() -> dict[str, Any]:
        esearch_endpoint = f"{_ESEARCH_URL}?db=pubmed&term={_quote(term)}&retmode=json&retmax={int(retmax)}&sort=relevance"
        esearch_result = fetch_json(esearch_endpoint, cache_dir=cache_dir, refresh=refresh)

        if esearch_result.get("error") is not None:
            return build_envelope(
                resource=RESOURCE_NAME,
                endpoint=esearch_endpoint,
                query={"term": term, "retmax": retmax},
                fetch_result=esearch_result,
                records=[],
            )

        pmids: list[str] = (
            (esearch_result.get("body") or {}).get("esearchresult", {}).get("idlist", [])
        )
        if not pmids:
            return build_envelope(
                resource=RESOURCE_NAME,
                endpoint=esearch_endpoint,
                query={"term": term, "retmax": retmax},
                fetch_result=esearch_result,
                records=[],
            )

        esummary_endpoint = f"{_ESUMMARY_URL}?db=pubmed&id={','.join(pmids)}&retmode=json"
        esummary_result = fetch_json(esummary_endpoint, cache_dir=cache_dir, refresh=refresh)
        if esummary_result.get("error") is not None:
            # esearch succeeded but esummary failed: report the PMIDs we do have (bare, with
            # title=None) as records for provenance, while build_envelope's own classification
            # of the esummary failure (rate-limited/network/http/invalid) becomes this
            # envelope's status -- so a caller sees exactly why the titles are missing rather
            # than a generic "partial" that erases the reason.
            records = [_pubmed_record(pmid, None) for pmid in pmids]
            return build_envelope(
                resource=RESOURCE_NAME,
                endpoint=esummary_endpoint,
                query={"term": term, "retmax": retmax},
                fetch_result=esummary_result,
                records=records,
            )

        summary_by_uid: dict[str, Any] = (esummary_result.get("body") or {}).get("result", {})
        records = [_pubmed_record(pmid, summary_by_uid.get(pmid)) for pmid in pmids]
        records = _reorder_by_drug_relevance(records, _drug_anchor, _other_terms)

        return build_envelope(
            resource=RESOURCE_NAME,
            endpoint=esummary_endpoint,
            query={"term": term, "retmax": retmax},
            fetch_result=esummary_result,
            records=records,
        )

    return memory_store.recall_or_compute(
        RESOURCE_NAME,
        term,
        _live_lookup,
        store_path=memory_path,
        refresh=refresh,
        remember_statuses=_REMEMBER_STATUSES,
    )


def search_pubmed_drug_pair(
    drug_a: str,
    drug_b: str,
    *,
    retmax: int = 5,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """A drug-drug pair query with a real relevance bar, for callers (e.g.
    the Drug Interaction Flag Sheet) that need a citation genuinely about
    *both* named drugs together -- not just a paper where both terms happen
    to appear somewhere in PubMed's "All Fields" index (author affiliation,
    MeSH heading, substance registry entry, etc.).

    Every record ``search_pubmed`` returns is required to actually name
    *both* drugs in its title (a real, observed failure mode: a
    "Levetiracetam AND Atomoxetine" query returned a Huntington Disease
    chapter, and a "Levetiracetam AND Escitalopram" query returned an
    unrelated deer-mice behavioral study -- both matched somewhere in
    PubMed's index, neither one's title has anything to do with the queried
    pair). A record whose title doesn't name both drugs is dropped entirely,
    not just reordered (contrast ``_reorder_by_drug_relevance``, used by the
    gene-drug path, which never drops a candidate) -- this never fabricates
    a replacement result, it only narrows what's reported down to what's
    verifiably on-topic; a pair with no on-topic title comes back exactly
    like a real "no_results" response.

    A ``[tiab]``-scoped query (title/abstract search, tightening the
    candidate pool itself rather than only filtering it afterward) was
    tried and rejected: ``validate_query_term`` -- the shared input
    guardrail every ``external_lookup`` query term passes through, see
    ``guardrails.py`` -- deliberately allows only letters, digits, spaces,
    and hyphens, precisely so free text or an injection attempt can never
    reach a live API; ``[`` and ``]`` fail that allowlist by design. Loosening
    the shared guardrail to admit PubMed field-tag bracket syntax was judged
    not worth it for a precision gain the title filter below already fully
    delivers: a false positive here is false exactly when its title doesn't
    name both drugs, regardless of which index field the underlying esearch
    match came from, so the post-fetch filter alone already excludes every
    observed failure case.

    This never invents evidence: everything returned is a real esearch/
    esummary hit; this only decides whether it is relevant enough to
    display for a specific named pair.
    """
    envelope = search_pubmed(f"{drug_a} AND {drug_b}", retmax=retmax, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path)
    if envelope.get("status") != "ok":
        return envelope

    relevant = [r for r in envelope["records"] if _title_names_both_drugs(r.get("title"), drug_a, drug_b)]
    if relevant:
        return {**envelope, "records": relevant, "count": len(relevant)}
    return {**envelope, "status": "no_results", "records": [], "count": 0}


def _title_names_both_drugs(title: Any, drug_a: str, drug_b: str) -> bool:
    """True only if ``title`` names both drugs as whole words -- the same
    word-boundary approach dailymed_lookup._title_names_drug already uses to
    reject a loose substring match."""
    if not isinstance(title, str) or not title.strip():
        return False
    lowered = title.lower()
    return all(re.search(r"\b" + re.escape(name.strip().lower()) + r"\b", lowered) for name in (drug_a, drug_b) if name and name.strip())


def _reorder_by_drug_relevance(
    records: list[dict[str, Any]], drug_term: str | None, other_terms: list[str] | None = None
) -> list[dict[str, Any]]:
    """Promote the first already-fetched record whose title actually names
    the drug this query was for, ahead of NCBI's own relevance-sorted
    order. PubMed's ``sort=relevance`` (even scoped to ``[tiab]``) can
    still rank a paper that mentions the drug only in passing -- e.g. in a
    drug-interaction table for a different index drug -- ahead of one that
    is directly about it (confirmed live: a "CYP3A4 AND lamotrigine" query
    ranked a Cenobamate pharmacology paper above one titled "Effects of
    lamotrigine and phenytoin on the pharmacokinetics of atorvastatin").

    For a drug-drug pair query (``other_terms`` non-empty -- e.g. a DDI
    literature search built as "Lamotrigine AND Levetiracetam"), a record
    naming BOTH drugs in its title is promoted ahead of one naming only the
    anchor drug, since that is verifiably the more specific, on-topic
    citation for the pair; a gene-drug query (where the gene essentially
    never appears in a paper's title) is unaffected by this refinement --
    it still falls back to "names the anchor drug" exactly as before.

    This never adds, drops, or invents a record -- it only changes which of
    the real, already-retrieved candidates is shown first, and leaves the
    order untouched when none of them name the drug in the title at all."""
    if not drug_term:
        return records
    needle = drug_term.lower()
    other_needles = [t.lower() for t in (other_terms or []) if t]

    def _rank(record: dict[str, Any]) -> int:
        title = str(record.get("title") or "").lower()
        if needle not in title:
            return 2
        if other_needles and all(other in title for other in other_needles):
            return 0
        return 1

    if all(_rank(r) == 2 for r in records):
        return records
    return sorted(records, key=_rank)


def _pubmed_record(pmid: str, summary: dict[str, Any] | None) -> dict[str, Any]:
    """Build one output record, running every third-party text field through
    sanitize_response_field and the URL through validate_source_url before
    either ever reaches External_Evidence_Report.json or a rendered report."""
    clean_pmid = sanitize_response_field(pmid, max_length=20)
    url = validate_source_url(f"https://pubmed.ncbi.nlm.nih.gov/{clean_pmid}/", _EXPECTED_DOMAIN)
    if not isinstance(summary, dict):
        return {"pmid": clean_pmid, "url": url, "title": None}
    return {
        "pmid": clean_pmid,
        "url": url,
        "title": sanitize_response_field(summary.get("title")) or None,
        "source_journal": sanitize_response_field(summary.get("source"), max_length=200) or None,
        "pubdate": sanitize_response_field(summary.get("pubdate"), max_length=60) or None,
        "last_author": sanitize_response_field(summary.get("lastauthor"), max_length=120) or None,
    }


def fetch_abstract_sections(
    pmid: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
) -> dict[str, Any]:
    """Fetch one paper's real abstract, split by its own structured section
    labels (see module docstring). Returns a standard envelope; ``status``
    is ``"ok"`` with ``records[0]["sections"]`` (a ``{label: text}`` dict,
    e.g. ``{"RESULTS": "...", "CONCLUSIONS": "..."}``, or ``{"UNLABELED":
    "..."}`` for an abstract with no structured labels), ``"no_results"``
    when the paper has no abstract at all, or a real error status."""
    clean_pmid = sanitize_response_field(pmid, max_length=20)
    if not clean_pmid:
        return build_envelope(
            resource=_ABSTRACT_RESOURCE_NAME,
            endpoint="",
            query={"pmid": pmid},
            fetch_result={"error": "empty or invalid pmid", "retrieved_at": None, "from_cache": False, "http_status": None},
            records=[],
        )

    def _live_lookup() -> dict[str, Any]:
        endpoint = f"{_EFETCH_URL}?db=pubmed&id={clean_pmid}&rettype=abstract&retmode=xml"
        fetch_result = fetch_text(endpoint, cache_dir=cache_dir, refresh=refresh)
        query = {"pmid": clean_pmid}

        if fetch_result.get("error") is not None:
            return build_envelope(resource=_ABSTRACT_RESOURCE_NAME, endpoint=endpoint, query=query, fetch_result=fetch_result, records=[])

        sections = _parse_abstract_sections(fetch_result.get("body") or "")
        if not sections:
            return build_envelope(resource=_ABSTRACT_RESOURCE_NAME, endpoint=endpoint, query=query, fetch_result=fetch_result, records=[])

        clean_sections = {
            sanitize_response_field(label, max_length=40) or "UNLABELED": sanitize_response_field(text, max_length=1200)
            for label, text in sections.items()
        }
        record = {"pmid": clean_pmid, "sections": {k: v for k, v in clean_sections.items() if v}}
        return build_envelope(resource=_ABSTRACT_RESOURCE_NAME, endpoint=endpoint, query=query, fetch_result=fetch_result, records=[record])

    return memory_store.recall_or_compute(
        _ABSTRACT_RESOURCE_NAME,
        clean_pmid,
        _live_lookup,
        store_path=memory_path,
        refresh=refresh,
        remember_statuses=_REMEMBER_STATUSES,
    )


def _parse_abstract_sections(xml_text: str) -> dict[str, str]:
    """Real ``{label: text}`` pairs from a PubMed efetch abstract XML
    document -- each ``<AbstractText>`` element's own ``NlmCategory``
    (preferred) or ``Label`` attribute, verbatim. A single unlabeled
    ``<AbstractText>`` (no attribute) is returned as ``{"UNLABELED": ...}``.
    Never a summary or paraphrase -- the label's own real text, joined
    verbatim when a category repeats."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    sections: dict[str, list[str]] = {}
    for node in root.iter("AbstractText"):
        label = node.get("NlmCategory") or node.get("Label") or "UNLABELED"
        text = "".join(node.itertext()).strip()
        if not text:
            continue
        sections.setdefault(label.strip().upper(), []).append(text)
    return {label: " ".join(parts) for label, parts in sections.items()}


def trim_to_sentences(text: str, max_sentences: int = 2, *, from_end: bool = False) -> str:
    """The first (or, if ``from_end``, the last) ``max_sentences`` real
    sentences of ``text`` -- never paraphrased, just less of the source's
    own verbatim words, so a Finding stays to roughly the "1-2 sentences"
    format real evidence needs to fit a report row. A text already within
    the limit is returned unchanged. ``from_end`` matters specifically for
    an unstructured abstract (see ``pick_results_or_conclusion_text``): a
    clinical abstract conventionally states background/aims first and its
    actual conclusion last, so grabbing the *first* sentences of an
    unlabeled block would systematically return the aim, not the outcome --
    exactly the bug this whole module exists to fix."""
    if not isinstance(text, str) or not text.strip():
        return text
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    if len(sentences) <= max_sentences:
        return text.strip()
    chosen = sentences[-max_sentences:] if from_end else sentences[:max_sentences]
    return " ".join(chosen)


def pick_results_or_conclusion_text(sections: dict[str, str]) -> str | None:
    """The Results/Conclusions text from an abstract's own structured
    sections (see ``fetch_abstract_sections``) -- never the Background/
    Objective/Purpose text a title-like sentence usually lives in, and
    trimmed to roughly 1-2 sentences per side (``trim_to_sentences``) so
    the Finding stays report-row-sized rather than reproducing a full
    paragraph verbatim.

    Priority: Results + Conclusions together (the fullest, most direct
    account of what the paper actually found, one sentence from each) ->
    Conclusions alone (2 sentences) -> Results alone (2 sentences) -> the
    single unlabeled block's own LAST 2 sentences (a real abstract
    conventionally states its conclusion last -- see
    ``trim_to_sentences``'s own docstring for why this must never be the
    *first* sentences) -> ``None`` when nothing usable is present, letting
    the caller fall back to the title at an explicitly lower confidence."""
    result_text = next((sections[label] for label in _RESULT_LABELS if sections.get(label)), None)
    conclusion_text = next((sections[label] for label in _CONCLUSION_LABELS if sections.get(label)), None)
    if result_text and conclusion_text:
        return f"{trim_to_sentences(result_text, 1)} {trim_to_sentences(conclusion_text, 1)}"
    if conclusion_text:
        return trim_to_sentences(conclusion_text, 2)
    if result_text:
        return trim_to_sentences(result_text, 2)
    if sections.get("UNLABELED"):
        return trim_to_sentences(sections["UNLABELED"], 2, from_end=True)
    return None


def describes_genuine_combination(sections: dict[str, str]) -> bool:
    """True when this abstract's own text describes ``drug_a`` and
    ``drug_b`` genuinely administered or evaluated *together* -- not merely
    two drugs that both appear somewhere in the same paper.

    A real, named failure mode this rejects: a comparator trial ("Effect of
    augmentation with aripiprazole... versus switching to the antidepressant
    venlafaxine") names both drugs, but they were never combined -- one
    patient group got aripiprazole added to their regimen, a *different*
    group was switched to venlafaxine instead. Drug A **vs.** Drug B is not
    evidence for Drug A **+** Drug B, even though both names genuinely
    appear in the same real abstract.

    Checked against every section's text jointly (Methods is where a trial
    states who actually received what, not just Results/Conclusions) --
    never just the picked Finding text, since the combination/comparator
    signal often lives in Methods, which ``pick_results_or_conclusion_text``
    deliberately never returns as the Finding itself.

    Rule: combination language anywhere in the abstract accepts it (even a
    trial that *also* compares combination therapy against monotherapy is
    genuine combination evidence); comparator language with NO combination
    language anywhere rejects it; neither signal present accepts it by
    default (silence is not itself a comparator red flag -- this only
    rejects an explicit, named comparator design, never merely because a
    phrase it expects happens not to appear)."""
    combined_text = " ".join(sections.values()).lower()
    if not combined_text.strip():
        return True
    has_combination = any(phrase in combined_text for phrase in _COMBINATION_PHRASES)
    if has_combination:
        return True
    has_comparator = any(phrase in combined_text for phrase in _COMPARATOR_PHRASES)
    return not has_comparator


def find_combination_excerpt(text: str, max_sentences: int = 2) -> str | None:
    """The sentence containing the first *explicit* combination-language
    match (see ``_COMBINATION_PHRASES``), plus up to ``max_sentences - 1``
    following sentences -- the real, relevant excerpt, not necessarily
    ``text``'s own opening sentences.

    This exists for ``ddi_flag_evidence._tavily_finding_and_url``, not for
    a structured PubMed abstract: an arbitrary fetched web page's raw
    extracted text frequently has navigation/menu boilerplate concatenated
    immediately after the page's title, before the real article body --
    confirmed live (an "Oxcarbazepine/Aripiprazole Interaction..." case
    report's own extracted text began "...Interaction and Bipolar Relapse
    Skip to content JCP PCC Insights About Authors..."). Blindly taking the
    text's first ``max_sentences`` sentences (``trim_to_sentences``) in that
    case returns the boilerplate, not the finding, even when the real
    combination-language sentence genuinely exists further down the same
    page. This locates that sentence instead of assuming it comes first.

    Deliberately stricter than ``describes_genuine_combination``: that
    function also accepts when *neither* a combination nor a comparator
    phrase is found at all, on the reasoning that a short, structured
    PubMed abstract's silence on a comparator design is itself informative.
    That reasoning does not hold for several thousand characters of
    unstructured general web content, which will often simply not mention
    either phrase within the fetched window while still being irrelevant --
    so this requires an *explicit* combination-language match and returns
    ``None`` otherwise; callers must treat ``None`` as "no genuine
    combination evidence found here", not fall back to the text's raw
    leading content."""
    if not isinstance(text, str) or not text.strip():
        return None
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    for index, sentence in enumerate(sentences):
        if any(phrase in sentence.lower() for phrase in _COMBINATION_PHRASES):
            return " ".join(sentences[index : index + max_sentences])
    return None


def _quote(term: str) -> str:
    from urllib.parse import quote

    return quote(term, safe="")
