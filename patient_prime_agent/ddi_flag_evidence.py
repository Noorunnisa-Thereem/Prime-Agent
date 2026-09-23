"""Live, multi-source evidence for the Drug Interaction Flag Sheet's
per-drug Positive/Negative Impact tables.

Standalone script following the same shape as the other ``*_summary.py`` /
``*_evidence.py`` modules in this package: it checks every unique pair from
a curated drug-screening list against every implemented interaction source
this project has, and writes one JSON report, which
``digital_twin_report.py`` merges verbatim and ``report_html.py``'s
per-drug Flag Sheet renderer reads.

Screening drug list
--------------------
The patient's real current regimen (``normalizer.normalize_regimen``) merged
with ``path_d.ddi.candidate_drugs.DDI_SCREENING_CANDIDATES`` -- a curated
~24-drug panel drawn from the patient's own real 44-drug pharmacogenomic
panel (``genetics_clinical_summary.json``), not the full ~102-drug
NeuroPrecisionDx catalog (see that module's own docstring for why). Every
unique pair among the merged set (today: 26 drugs -> 325 pairs) is checked.

Sources checked per pair
-------------------------
Two local/curated sources cost nothing and are always checked first:

  - **Flockhart** (``reference_data.pharmacokinetic_pair_summary``): a real
    curated CYP-pathway overlap between the two drugs, when this project's
    small hand-curated reference table covers both of them.
  - **Pharmacodynamic rules** (``pharmacodynamic_rules.evaluate_pair``): a
    real class-level pharmacodynamic-risk rule (e.g. additive CNS
    depression), when both drugs have a class in ``reference_data.
    DRUG_CLASS_MAP``.

Four more sources are queried live, **in parallel for every pair** (not one
first-choice source with the others as fallback) -- every one of the four
is always called, regardless of what any of the others returned:

  1. **PubMed** (``pubmed_lookup.search_pubmed_drug_pair``): the "DrugA AND
     DrugB" combined query, with every returned record required to name
     *both* drugs in its title (see that function's own docstring for the
     real failure mode this fixes).
  2. **DailyMed** -- two real checks, not one: ``search_dailymed`` still
     resolves each drug's label *pointer* (setid) once per unique drug (26
     calls, not once per pair), but this version also fetches each drug's
     real DRUG INTERACTIONS section text (``dailymed_lookup.
     fetch_label_interactions_section``, also once per unique drug -- see
     ``_fetch_interactions_sections``) and checks, for each pair, whether
     the *other* drug's name is mentioned in it
     (``dailymed_lookup.find_drug_mention_excerpt``). A real mention is a
     genuine finding (a regulatory label explicitly naming the other drug);
     when the name is not mentioned, that is recorded honestly as "not
     mentioned in reviewed label section" -- never silence-as-safe, and
     never treated as "confirmed no interaction" (see
     ``_dailymed_mention_result``).
  3. **ClinicalTrials.gov** (``trials_lookup.search_trials_drug_pair``): a
     combined ``query.intr=DrugA AND DrugB`` query, with every returned
     study still required to structurally list *both* drugs as real
     interventions (armsInterventionsModule) -- the same never-trust-the-
     raw-match discipline ``search_trials`` already applies.
  4. **Tavily** (``search_tools.search_tavily``): a combined "DrugA AND
     DrugB drug interaction" web-search query, called directly and
     unconditionally alongside the other three -- **not** through
     ``search_tools.search_with_medical_fallback``'s exhausted-medical-
     sources gate (that gate exists for a different use case,
     ``external_evidence_summary.py``'s question-answering paths, where
     Tavily really is a last resort; here it is one of four co-equal pair-
     level sources, per explicit instruction). Tavily is still never
     treated as equivalent evidence to the three dedicated medical/
     regulatory sources above it: every Tavily-sourced ``evidence_labels``
     entry is the literal string ``"General web search (Tavily)"`` (see
     ``_TAVILY_EVIDENCE_LABEL``), so a reader can always tell a general-web
     result apart from PubMed/DailyMed/ClinicalTrials.gov in the rendered
     Evidence line, and Tavily's text is always the lowest-priority
     candidate for the primary Finding sentence (see below) -- it only
     becomes the Finding when every other source returned nothing.

**CPIC and ClinVar are deliberately not queried here**, for the same reason
the original (16-pair) version of this module gave: both require a gene
symbol and have no drug-pair query path. Widening the screening list here
does not change that -- CPIC/ClinVar findings for any of these drugs (where
the patient's own genetics panel covers them) already live in the
Pharmacogenomics section / ``External_Evidence_Report.json``'s
``by_gene_drug_pair``; folding a single drug's own gene-driven PGx finding
into a *pair*'s Positive/Negative verdict here would conflate two different
questions (this drug's own genotype-predicted response vs. these two drugs'
combined effect) -- the same axis-conflation this codebase's own DDI
Therapeutic Response skill explicitly warns against.

Positive vs. Negative Impact classification
--------------------------------------------
Every pair resolves to exactly one of two outcomes: **Positive Impact**,
**Negative Impact**, or it does not appear in the report at all. There is
no third "in-between" bucket -- an earlier version of this module put an
ambiguous pair in a "needs_review" column instead of Positive/Negative;
per this module's own reference skill
(``skills/patient_prime_agent/ddi-therapeutic-response/SKILL.md``, Part
4), that column has been removed. A pair whose evidence does not clearly
resolve either way is **unresolved**, exactly like a pair with no evidence
at all -- both are dropped from the report entirely, never shown with a
guessed or default verdict (``_pair_finding`` returns ``None`` in both
cases). A DailyMed "not mentioned" result never counts as a finding on its
own (an absence-of-mention is not evidence of anything); it only ever
appears as supplementary provenance alongside whatever else the pair has.

For a pair with a real finding, candidates are gathered in priority order
-- Flockhart mechanism, then a fired pharmacodynamic rule, then a real
DailyMed mention (a regulatory label is more authoritative than a single
paper or a web page), then a PubMed title, then a ClinicalTrials.gov study
title, then a Tavily web result (lowest priority, per above). **Every
candidate's text is classified the same way, by its own content, never by
which source produced it** (see ``_classify_text``):

  - Text matching an explicit positive-language keyword/fragment (see
    ``_POSITIVE_KEYWORDS`` -- ``well tolerated``/``effective``/``efficacy``/
    ``tolerat``/``improv``/``benefit``/``compatible``-type language) ->
    **Positive Impact**.
  - Text matching an explicit negative-language keyword/fragment (see
    ``_NEGATIVE_KEYWORDS`` -- ``decrease``/``increase risk``/``toxicity``-
    type language) -> **Negative Impact**.
  - Text matching neither (or a retracted citation, see ``_is_retracted``)
    -> this candidate does not resolve the pair. **Fall through to the
    next candidate in priority order instead of forcing a verdict** --
    per SKILL.md Part 4: "mixed or unclear language does not resolve the
    pair from this source." A higher-priority source's ambiguous or
    retracted text never blocks a lower-priority source's clear one from
    deciding the pair; it is simply skipped.

This applies uniformly: a Flockhart curated mechanism or a pharmacodynamic
rule can resolve either Positive or Negative depending on what its own
statement actually says -- an earlier version of this module hard-coded
both of those sources to always classify Negative regardless of content,
purely because of which source they came from (a real, reported bug: it
meant a real curated mechanism could never be Positive even when the
underlying pharmacology was benign). In practice, every pharmacodynamic
rule in ``pharmacodynamic_rules.RULES`` today still classifies Negative,
because its own ``expected_consequence`` text genuinely describes a risk
(e.g. "Increased sedation...") -- but that is now a consequence of what
the text says, not a rule about the source.

If every candidate's text is ambiguous, or the only candidates are
retracted citations, the pair is unresolved and dropped -- this module
never guesses a direction from vocabulary it does not recognize. Medical
adverse-event vocabulary is effectively unbounded, so no keyword list can
close that gap by growing longer; dropping the pair says plainly what is
true (no source here stated a clear direction) instead of forcing it into
a label that overstates this module's own confidence.

The Finding text itself is always the source's own real sentence/title,
verbatim -- never paraphrased or cleaned up into prose the source doesn't
itself contain (the same rule the original 16-pair version of this module
already applied).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from .core.utils import ensure_dir
from .path_d.ddi import candidate_drugs, pharmacodynamic_rules, reference_data
from .path_d.ddi.normalizer import Medication, normalize_regimen
from .external_lookup import dailymed_lookup, memory_store, pubmed_lookup, search_tools, trials_lookup
from .external_lookup.http_client import DEFAULT_CACHE_DIR

DEFAULT_CLINICAL_NOTES_PATH = Path("reports") / "clinical_notes" / "clinical__notes_summary.json"
DEFAULT_OUTPUT_PATH = Path("reports") / "drug_interaction_flags" / "Flag_Sheet_Live_Evidence.json"

_TAVILY_EVIDENCE_LABEL = "General web search (Tavily)"

# Explicit keyword/fragment lists -- see the module docstring's "Positive
# vs. Negative Impact classification" section. Deliberately matched in
# breadth and style to each other: both are a mix of a few exact phrases
# and short word-fragments that catch multiple real inflections (e.g.
# "tolerat" catches "tolerated"/"tolerating", "tolerab" catches
# "tolerable"/"tolerability") -- an earlier version of this list was much
# narrower on the positive side (8 mostly-exact phrases vs. 14 broad
# fragments on the negative side), which meant a real positive-sounding
# title almost never matched while an incidental negative-sounding word
# anywhere in an unrelated title often did. Checked positive-first (see
# _classify_text) -- unchanged from before -- so a title naming both an
# efficacy word and a risk word (the reference report's own worked example:
# "...may increase lamotrigine levels, and the combination can be
# effective...") still resolves Positive, matching that precedent; this is
# a known, accepted tradeoff, not an oversight.
_POSITIVE_KEYWORDS: tuple[str, ...] = (
    "well tolerated", "well-tolerated", "generally safe", "no significant interaction",
    "no clinically significant", "safe and effective", "additive seizure control",
    "compatible", "favorable outcome", "favorable", "effective", "efficacy",
    "tolerat", "tolerab", "improv", "benefit", "successful", "manageable",
)
_NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "decrease", "decreas", "reduc", "increase", "increas", "risk", "toxicity", "adverse",
    "caution", "contraindicat", "impair", "danger", "worsen", "overdose",
)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _screening_medications(clinical_notes: dict[str, Any]) -> list[Medication]:
    """The merged current-regimen + screening-candidate list (see
    ``candidate_drugs.py``), deduplicated by normalized name -- a candidate
    already in the real regimen is excluded by
    ``merged_candidate_medications`` itself."""
    current, _conflicts = normalize_regimen(clinical_notes)
    candidates = candidate_drugs.merged_candidate_medications(current)
    return current + candidates


def _unique_pairs(medications: list[Medication]) -> list[tuple[Medication, Medication]]:
    """Every unique unordered pair from ``medications``, deduplicated by an
    unordered comparison of normalized drug names (``frozenset({a, b})``) --
    not just relying on ``medications`` itself having no two records that
    share a normalized name. In practice today it doesn't (``_screening_
    medications`` merges an already name-deduped current regimen with
    candidates explicitly excluded from colliding with it -- see
    ``candidate_drugs.merged_candidate_medications``), so this guard is
    currently a no-op; it exists so a future change to either of those (e.g.
    a screening-candidate name accidentally re-added, or a regimen-merge
    change) can never silently double a pair's evidence gathering -- run
    twice across all 4 live sources (PubMed/DailyMed/ClinicalTrials.gov/
    Tavily) for what is really the same drug pair -- instead of failing loud.
    Dedup happens here, before any evidence gathering, not only when the
    Flag Sheet renders (see ``dedupe_pairs_output`` for cleaning output
    already written before this guard existed)."""
    seen: set[frozenset[str]] = set()
    pairs: list[tuple[Medication, Medication]] = []
    for med_a, med_b in combinations(sorted(medications, key=lambda m: m.normalized_name), 2):
        key = frozenset({med_a.normalized_name, med_b.normalized_name})
        if key in seen:
            continue
        seen.add(key)
        pairs.append((med_a, med_b))
    return pairs


def dedupe_pairs_output(pairs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One-time cleanup for a ``pairs`` list from an already-written report
    (e.g. a prior ``Flag_Sheet_Live_Evidence.json``'s own ``"pairs"`` array)
    that may predate the ``_unique_pairs`` dedup guard above. Uses the same
    unordered ``frozenset({drug_a, drug_b})`` comparison (case-insensitive,
    whitespace-trimmed), keeping the FIRST entry seen for each pair and
    dropping the rest.

    Returns ``(deduped_pairs, removed_pairs)`` -- ``removed_pairs`` is every
    dropped duplicate entry itself (not just a count), so a caller can show
    concretely which pairs were removed rather than only how many."""
    seen: set[frozenset[str]] = set()
    deduped: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for pair in pairs:
        key = frozenset({str(pair.get("drug_a") or "").strip().lower(), str(pair.get("drug_b") or "").strip().lower()})
        if key in seen:
            removed.append(pair)
            continue
        seen.add(key)
        deduped.append(pair)
    return deduped, removed


def _display_name(medication: Medication) -> str:
    """A clean, guardrail-safe display/query name -- e.g. "Levetiracetam",
    not the real regimen's raw source text "Levetiracetam (Keppra)".
    ``validate_query_term`` (see guardrails.py) rejects parentheses, so the
    brand-suffixed source_name a real regimen entry carries can never be
    sent to a live API directly; ``normalized_name`` is already brand-
    stripped and lowercased (see normalizer.normalize_name), so
    title-casing it back gives a name that is both a valid query term and a
    clean section header for the report."""
    return medication.normalized_name.title()


def _is_retracted(text: str | None) -> bool:
    """True if the evidence text explicitly says the underlying citation
    was retracted (case-insensitive "RETRACTED" -- PubMed prefixes a
    retracted article's own title with exactly this word, e.g. "RETRACTED:
    Efficacy and safety of..."). A retracted paper's stated conclusion
    cannot be trusted as evidence in either direction, so this must
    override whatever positive/negative keywords its title also happens to
    contain -- a real, observed failure otherwise: "RETRACTED: Efficacy and
    safety of aripiprazole or bupropion augmentation..." matched "Efficacy"
    and was classified Positive, with the retraction itself invisible to
    the classifier. See _pair_finding, which checks this before calling
    _classify_text at all."""
    return isinstance(text, str) and "retracted" in text.lower()


def _classify_text(text: str | None) -> str | None:
    """Positive/Negative/None (unclassifiable) for one piece of real
    evidence text -- a PubMed title, a Flockhart curated-mechanism
    statement, or a pharmacodynamic-rule's expected-consequence summary.
    Used uniformly across all three pair-level sources (see
    _pair_finding): a Flockhart or pharmacodynamic-rule finding is
    evaluated on its own text, exactly like a PubMed title, never assumed
    Negative purely because of which source produced it. See the module
    docstring for why an unmatched text resolves nothing (the caller falls
    through to the next candidate rather than guessing). Does not itself
    check for a retracted citation -- see _is_retracted, which the caller
    checks first."""
    if not isinstance(text, str) or not text.strip():
        return None
    lowered = text.lower()
    if any(keyword in lowered for keyword in _POSITIVE_KEYWORDS):
        return "positive"
    if any(keyword in lowered for keyword in _NEGATIVE_KEYWORDS):
        return "negative"
    return None


def _pubmed_title_and_url(pubmed_result: dict[str, Any]) -> tuple[str | None, str | None]:
    if pubmed_result.get("status") != "ok" or not pubmed_result.get("records"):
        return None, None
    record = pubmed_result["records"][0]
    title = record.get("title")
    url = record.get("url") or (f"https://pubmed.ncbi.nlm.nih.gov/{record.get('pmid')}/" if record.get("pmid") else None)
    return (title if isinstance(title, str) and title.strip() else None), url


def _trials_title_and_url(trials_result: dict[str, Any]) -> tuple[str | None, str | None]:
    if trials_result.get("status") != "ok" or not trials_result.get("records"):
        return None, None
    record = trials_result["records"][0]
    title = record.get("brief_title")
    return (title if isinstance(title, str) and title.strip() else None), record.get("url")


def _tavily_title_and_url(tavily_envelope: dict[str, Any]) -> tuple[str | None, str | None]:
    """``tavily_envelope`` is ``search_tools.search_tavily``'s own unified
    envelope shape (``{"status", "result": {"records": [...]}, ...}``), not
    the ``{"status", "records": [...]}`` shape the other lookup modules in
    this package return -- see ``search_tools.py``'s module docstring for
    why (it normalizes every backend, including Tavily, into one common
    shape)."""
    if tavily_envelope.get("status") != "ok":
        return None, None
    records = ((tavily_envelope.get("result") or {}).get("records")) or []
    if not records:
        return None, None
    title = records[0].get("title")
    return (title if isinstance(title, str) and title.strip() else None), records[0].get("url")


def _fetch_interactions_sections(
    medications: list[Medication],
    dailymed_by_drug: dict[str, dict[str, Any]],
    *,
    cache_dir: Path,
    refresh: bool,
    memory_path: Path,
) -> dict[str, dict[str, Any]]:
    """Once per unique drug (never once per pair -- 26 calls, not 650):
    fetch each drug's own real DRUG INTERACTIONS label section via its
    DailyMed setid (``dailymed_lookup.fetch_label_interactions_section``).
    Every pair's "does X's label mention Y" check (``_dailymed_mention_
    result``) reuses this same already-fetched text in memory rather than
    re-fetching it once per pair it happens to be part of."""
    sections: dict[str, dict[str, Any]] = {}
    for med in medications:
        pointer = dailymed_by_drug.get(med.normalized_name) or {}
        setid = pointer["records"][0].get("setid") if pointer.get("status") == "ok" and pointer.get("records") else None
        if not setid:
            sections[med.normalized_name] = {"status": "no_results", "records": []}
            continue
        sections[med.normalized_name] = dailymed_lookup.fetch_label_interactions_section(
            setid, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path
        )
    return sections


def _dailymed_mention_result(
    subject_display_name: str,
    subject_section_result: dict[str, Any],
    other_display_name: str,
) -> dict[str, Any]:
    """Given one drug's already-fetched interactions-section envelope (see
    _fetch_interactions_sections), check whether ``other_display_name`` is
    mentioned -- a pure in-memory string search, no network or cache call
    here. Always returns ``mentioned`` (bool); ``note`` is populated
    (never left implicit) whenever ``mentioned`` is False, distinguishing
    "no label on file", "label has no Drug Interactions section", and "the
    section exists but doesn't name this drug" -- three different real
    outcomes this never collapses into one generic "not found"."""
    if subject_section_result.get("status") != "ok" or not subject_section_result.get("records"):
        note = (
            f"{subject_display_name}'s DailyMed label has no Drug Interactions section on file to check."
            if subject_section_result.get("status") == "no_results"
            else f"{subject_display_name}'s DailyMed label content could not be retrieved this run."
        )
        return {"mentioned": False, "excerpt": None, "url": None, "note": note}

    record = subject_section_result["records"][0]
    excerpt = dailymed_lookup.find_drug_mention_excerpt(record.get("section_text") or "", other_display_name)
    if excerpt is None:
        return {"mentioned": False, "excerpt": None, "url": record.get("url"), "note": "not mentioned in reviewed label section"}

    return {
        "mentioned": True,
        "excerpt": f'{subject_display_name}\'s DailyMed label states: "{excerpt}"',
        "url": record.get("url"),
        "note": None,
    }


def _pair_finding(
    flockhart: dict[str, Any],
    pd_rules: list[dict[str, Any]],
    dailymed_mention_a: dict[str, Any],
    dailymed_mention_b: dict[str, Any],
    pubmed_title: str | None,
    pubmed_url: str | None,
    trials_title: str | None,
    trials_url: str | None,
    tavily_title: str | None,
    tavily_url: str | None,
) -> tuple[str, str, list[str], str | None, str] | None:
    """(impact, finding, evidence_labels, hyperlink_url, primary_evidence_label) for
    one pair, or ``None`` if the pair is unresolved -- either no source
    returned anything, or every source that did return something failed to
    resolve clearly to Positive or Negative (SKILL.md Part 4: there is no
    third bucket; unresolved is dropped exactly like no-evidence, never
    shown with a guessed or default verdict). A DailyMed "not mentioned"
    result never counts as a finding here -- only a real ``mentioned: True``
    does (see module docstring).

    Candidates are gathered in priority order (Flockhart, pharmacodynamic
    rules, DailyMed mention, PubMed, ClinicalTrials.gov, Tavily -- see the
    module docstring for the reasoning). ``evidence_labels`` lists every
    source that returned something real, in that same order, regardless of
    which one ultimately resolves the pair. The Finding text, ``impact``,
    and ``hyperlink_url`` all come from the FIRST candidate, in priority
    order, whose own text classifies clearly as Positive or Negative
    (``_classify_text``) -- that candidate's own label is returned
    separately as ``primary_evidence_label``, since it is **not**
    guaranteed to be ``evidence_labels[0]``: a higher-priority candidate
    that is retracted (``_is_retracted``) or whose text does not classify
    clearly is skipped (not forced into a verdict) but still appears in
    ``evidence_labels``, so the candidate that actually supplied the
    Finding/url can be a lower-priority one than ``evidence_labels[0]``
    names. A caller that needs to show the Finding's real source (e.g. a
    hyperlink's link text) must use ``primary_evidence_label``, never
    ``evidence_labels[0]`` -- an earlier version of this function assumed
    they were always the same, which mislabeled a real ClinicalTrials.gov/
    Tavily/PubMed link with whatever higher-priority source's label
    happened to be first, even when that source contributed no url at all."""
    candidates: list[tuple[str, str, str | None]] = []  # (evidence_label, text, url), priority order

    if flockhart.get("mechanism_found"):
        candidates.append((f"Curated DDI ({reference_data.FLOCKHART_SOURCE})", flockhart["statement"], None))
    if pd_rules:
        summary = "; ".join(rule["expected_consequence"] for rule in pd_rules)
        # No classification-biasing prefix here (an earlier version prepended
        # "Pharmacodynamic interaction risk: ", which quietly reintroduced the exact bug
        # this fix removes: the word "risk" in that prefix alone was enough to classify
        # Negative regardless of what the rule's own expected_consequence actually said).
        candidates.append(("Pharmacodynamic interaction reference", summary, None))
    if dailymed_mention_a.get("mentioned"):
        candidates.append(("DailyMed label (Drug Interactions section)", dailymed_mention_a["excerpt"], dailymed_mention_a.get("url")))
    if dailymed_mention_b.get("mentioned"):
        candidates.append(("DailyMed label (Drug Interactions section)", dailymed_mention_b["excerpt"], dailymed_mention_b.get("url")))
    if pubmed_title is not None:
        candidates.append(("External evidence (PubMed)", pubmed_title, pubmed_url))
    if trials_title is not None:
        candidates.append(("ClinicalTrials.gov", trials_title, trials_url))
    if tavily_title is not None:
        candidates.append((_TAVILY_EVIDENCE_LABEL, tavily_title, tavily_url))

    if not candidates:
        return None

    evidence_labels = [label for label, _text, _url in candidates]

    for label, text, url in candidates:
        # A retracted citation's stated conclusion can't be trusted either way (see
        # _is_retracted) -- checked before classification, not folded into _classify_text's
        # own keyword check, so it is skipped exactly like ambiguous text: try the next
        # candidate rather than forcing a verdict from it.
        if _is_retracted(text):
            continue
        impact = _classify_text(text)
        if impact is not None:
            return impact, text, evidence_labels, url, label

    # No candidate's own text resolved clearly to Positive or Negative -- unresolved, per
    # the module docstring, not guessed either way.
    return None


def _tally_source_hits(pairs_out: list[dict[str, Any]]) -> dict[str, int]:
    """How many pairs (of those with evidence) had a real, contributing
    result from each of the 4 live-queried sources -- independent of
    which single source ended up supplying the primary Finding text, so
    this answers "how many pairs did PubMed/DailyMed/ClinicalTrials.gov/
    Tavily each turn up something real for", not just "how many pairs did
    each source win the Finding for"."""
    return {
        "pubmed": sum(1 for p in pairs_out if p["pubmed_combined_query"].get("status") == "ok" and p["pubmed_combined_query"].get("records")),
        "dailymed": sum(
            1
            for p in pairs_out
            if p["dailymed_mention_in_drug_a_label"]["mentioned"] or p["dailymed_mention_in_drug_b_label"]["mentioned"]
        ),
        "clinicaltrials": sum(
            1 for p in pairs_out if p["clinicaltrials_combined_query"].get("status") == "ok" and p["clinicaltrials_combined_query"].get("records")
        ),
        "tavily": sum(
            1
            for p in pairs_out
            if p["tavily_combined_query"].get("status") == "ok" and ((p["tavily_combined_query"].get("result") or {}).get("records"))
        ),
    }


def build_report(
    *,
    clinical_notes: dict[str, Any] | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    memory_path: Path = memory_store.DEFAULT_STORE_PATH,
    refresh: bool = False,
) -> dict[str, Any]:
    medications = _screening_medications(clinical_notes or {})
    pairs = _unique_pairs(medications)

    # DailyMed label pointer once per unique drug (unchanged) -- and, new, each
    # drug's real DRUG INTERACTIONS section text, also once per unique drug, never
    # once per pair (26 + 26 calls, not 650) -- see _fetch_interactions_sections.
    dailymed_by_drug: dict[str, dict[str, Any]] = {
        med.normalized_name: dailymed_lookup.search_dailymed(_display_name(med), cache_dir=cache_dir, memory_path=memory_path, refresh=refresh)
        for med in medications
    }
    interactions_sections_by_drug = _fetch_interactions_sections(
        medications, dailymed_by_drug, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path
    )

    pairs_out: list[dict[str, Any]] = []
    dropped = 0
    for drug_a, drug_b in pairs:
        display_a, display_b = _display_name(drug_a), _display_name(drug_b)

        flockhart = reference_data.pharmacokinetic_pair_summary(drug_a.normalized_name, drug_b.normalized_name)
        pd_rules = pharmacodynamic_rules.evaluate_pair(drug_a.normalized_name, drug_b.normalized_name)

        # The 4 live sources -- all 4 always called for every pair, none conditional on
        # what any of the others returned (see module docstring).
        pubmed_result = pubmed_lookup.search_pubmed_drug_pair(display_a, display_b, cache_dir=cache_dir, memory_path=memory_path, refresh=refresh)
        pubmed_title, pubmed_url = _pubmed_title_and_url(pubmed_result)

        trials_result = trials_lookup.search_trials_drug_pair(display_a, display_b, cache_dir=cache_dir, memory_path=memory_path, refresh=refresh)
        trials_title, trials_url = _trials_title_and_url(trials_result)

        tavily_result = search_tools.search_tavily(
            f"{display_a} AND {display_b} drug interaction", cache_dir=cache_dir, memory_path=memory_path, refresh=refresh
        )
        tavily_title, tavily_url = _tavily_title_and_url(tavily_result)

        mention_a = _dailymed_mention_result(display_a, interactions_sections_by_drug.get(drug_a.normalized_name, {}), display_b)
        mention_b = _dailymed_mention_result(display_b, interactions_sections_by_drug.get(drug_b.normalized_name, {}), display_a)

        finding = _pair_finding(
            flockhart, pd_rules, mention_a, mention_b, pubmed_title, pubmed_url, trials_title, trials_url, tavily_title, tavily_url
        )
        if finding is None:
            dropped += 1
            continue

        impact, finding_text, evidence_labels, hyperlink_url, primary_evidence_label = finding
        pairs_out.append(
            {
                "drug_a": display_a,
                "drug_b": display_b,
                "impact": impact,
                "finding": finding_text,
                "evidence_labels": evidence_labels,
                "hyperlink_url": hyperlink_url,
                "primary_evidence_label": primary_evidence_label,
                "flockhart": flockhart,
                "pharmacodynamic_rules": pd_rules,
                "pubmed_combined_query": pubmed_result,
                "clinicaltrials_combined_query": trials_result,
                "tavily_combined_query": tavily_result,
                "dailymed_drug_a": dailymed_by_drug.get(drug_a.normalized_name),
                "dailymed_drug_b": dailymed_by_drug.get(drug_b.normalized_name),
                "dailymed_mention_in_drug_a_label": mention_a,
                "dailymed_mention_in_drug_b_label": mention_b,
            }
        )

    return {
        "report_type": "Drug Interaction Flag Sheet -- Live Multi-Source Evidence",
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "screening_drugs": [m.source_name for m in medications],
        "pairs_checked": len(pairs),
        "pairs_with_evidence": len(pairs_out),
        "pairs_dropped_unresolved": dropped,
        "source_hit_counts": _tally_source_hits(pairs_out),
        "pairs": pairs_out,
        "limitations": [
            "A pair with no finding from Flockhart, pharmacodynamic rules, PubMed, DailyMed, ClinicalTrials.gov, "
            "or Tavily is dropped from this report entirely, not shown as a negative/no-concern finding -- "
            "'not listed here' means 'no evidence was found for this exact pair', never 'confirmed safe'.",
            "A DailyMed 'not mentioned in reviewed label section' result is never itself a finding -- it means "
            "this module checked the drug's real DRUG INTERACTIONS label section and the other drug's name was "
            "not there, not that no interaction exists; it only ever appears as supplementary context alongside "
            "whatever other evidence a pair has.",
            "Tavily (general web search) is queried directly and unconditionally alongside PubMed, DailyMed, and "
            "ClinicalTrials.gov for every pair here -- unlike elsewhere in this codebase "
            "(external_evidence_summary.py's question-answering paths), it is not gated behind the other sources "
            "returning no_results first. Every Tavily-sourced evidence entry is labeled "
            f"'{_TAVILY_EVIDENCE_LABEL}' so it is never mistaken for a dedicated medical/regulatory source, and "
            "it is always the lowest-priority candidate for a pair's primary Finding text.",
            "A source whose language does not clearly match an explicit positive-outcome or negative-outcome "
            "keyword never resolves a pair on its own -- this module falls through to the next candidate source "
            "in priority order instead, and if no candidate ever resolves clearly, the pair is dropped from the "
            "report entirely, the same as a pair with no evidence at all. There is no 'Needs Review' column here; "
            "most real titles/excerpts are topic descriptions, not verdicts, and this module never guesses which "
            "way an unclear one points.",
            "CPIC and ClinVar (gene-level sources) are not re-queried per pair here -- see the module docstring "
            "for why folding a single drug's own genotype-predicted finding into a pair's verdict here would "
            "conflate two different questions. Where this patient's genetics panel covers a screened drug, its "
            "real CPIC/ClinVar findings are in the Pharmacogenomics section, not duplicated here.",
            f"{reference_data.FLOCKHART_SOURCE} ({reference_data.FLOCKHART_VERSION}) curated CYP-pathway data "
            "covers only a small hand-curated set of drugs; a pair outside that set relies on the other five "
            "sources, not a gap in this report.",
            "Positive/Negative classification is derived from keyword matching on publication titles, which can "
            "misread comparative-effectiveness study titles (where multiple drugs are studied together) as a "
            "verdict about one specific pair. This is a known limitation of title-only keyword classification, "
            "not a per-pair clinical conclusion.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch live multi-source evidence for the expanded Drug Interaction Flag Sheet screening pairs")
    parser.add_argument("--clinical-notes", type=Path, default=DEFAULT_CLINICAL_NOTES_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--refresh", action="store_true", help="Bypass the 30-day memory-store cache and re-query every live source")
    parser.add_argument(
        "--dedupe-existing",
        action="store_true",
        help="Do not query any live source -- just run the one-time duplicate-pair cleanup "
        "(dedupe_pairs_output) against --output's already-written 'pairs' array and rewrite it "
        "in place if any duplicates were found.",
    )
    args = parser.parse_args(argv)

    if args.dedupe_existing:
        existing = _load_json(args.output)
        pairs = existing.get("pairs")
        if not isinstance(pairs, list):
            print(f"{args.output} has no 'pairs' array to dedupe -- nothing to do.")
            return 0
        deduped, removed = dedupe_pairs_output(pairs)
        print(f"Pair count before dedup: {len(pairs)}; after dedup: {len(deduped)} ({len(removed)} duplicate(s) removed)")
        for pair in removed:
            print(f"  removed duplicate: {pair.get('drug_a')} + {pair.get('drug_b')}")
        if removed:
            existing["pairs"] = deduped
            existing["pairs_with_evidence"] = len(deduped)
            existing["source_hit_counts"] = _tally_source_hits(deduped)
            ensure_dir(args.output.parent)
            args.output.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Rewrote {args.output} with duplicates removed.")
        return 0

    report = build_report(clinical_notes=_load_json(args.clinical_notes), refresh=args.refresh)
    ensure_dir(args.output.parent)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    counts = report["source_hit_counts"]
    print(
        f"Wrote Flag Sheet live evidence to {args.output} "
        f"({report['pairs_with_evidence']} pairs with evidence / {report['pairs_checked']} checked, "
        f"{report['pairs_dropped_unresolved']} dropped as unresolved)"
    )
    print(
        f"Per-source hit counts: PubMed={counts['pubmed']} DailyMed={counts['dailymed']} "
        f"ClinicalTrials.gov={counts['clinicaltrials']} Tavily={counts['tavily']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
