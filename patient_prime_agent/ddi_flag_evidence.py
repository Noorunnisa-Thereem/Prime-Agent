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
     real failure mode this fixes). The Finding text itself then comes from
     that paper's real Results/Conclusions abstract text
     (``pubmed_lookup.fetch_abstract_sections`` +
     ``pick_results_or_conclusion_text``), not its title -- a title states
     the paper's aim, not its result (a real, observed bug: see
     ``_pubmed_finding_and_url``'s own docstring). Falls back to the title,
     at lower confidence, only when no structured Results/Conclusions text
     is available for that PMID.
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
     study still required to structurally, word-boundary-match list *both*
     drugs as real interventions (armsInterventionsModule) -- the same
     never-trust-the-raw-match discipline ``search_trials`` already
     applies (word-boundary, not substring, matching fixes a real bug:
     "Citalopram" matching inside "Escitalopram"). The Finding text then
     comes from that study's real posted outcome data
     (``trials_lookup.fetch_trial_outcome_summary``), not its
     ``brief_title`` -- a title states the study's aim, not its result. When
     results aren't posted (the common case) or no confident
     combination-vs-comparator comparison can be identified, that real,
     honest reason becomes the Finding text instead -- never a guessed
     verdict, and never silently using the title as a stand-in.
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
     becomes the Finding when every other source returned nothing. The
     Finding text itself is always the underlying page's own real content,
     independently fetched (``search_tools.fetch_general_page_text``,
     trimmed to ~2 sentences) -- never the bare search-result title or
     snippet, and never Tavily's own bundled ``include_raw_content``
     extraction either, which the live API does not reliably return (a
     real, observed bug: an earlier version of this module fell back to the
     title whenever that field was absent). When the page can't be fetched
     or has no pair-specific content, Tavily contributes nothing for that
     pair at all, exactly like any other source that returned nothing --
     and is checked for an explicit comparator-only design before being
     trusted, same as PubMed (see ``_tavily_finding_and_url``).

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

By default, the Finding text is always the source's own real sentence/title,
verbatim -- never paraphrased or cleaned up into prose the source doesn't
itself contain (the same rule the original 16-pair version of this module
already applied).

Evidence/Finding Agent (opt-in, disabled by default)
------------------------------------------------------
``_pair_finding`` hands the winning candidate's own text to
``path_d.ddi.finding_agent.FindingAgent`` after classification, not before
-- Positive/Negative Impact, ``evidence_labels``, and ``hyperlink_url`` are
decided exactly as described above regardless of whether this agent is
enabled. When enabled (``PRIME_AGENT_DDI_FINDING_AGENT=1`` -- a dedicated
flag, independent of this project's existing ``PRIME_AGENT_ENABLE_LLM``;
see ``finding_agent.py``'s own docstring for why), the agent reads that
same candidate text and SKILL.md's own Part 6 ("Finding-text generation")
and returns a concise, pair-specific Finding sentence instead of the raw
verbatim text -- grounded in, never beyond, what that candidate's text
already said. Disabled (the default), it returns that text unchanged, so
the Finding is byte-identical to the pipeline's behaviour before this
agent existed.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from .core.utils import ensure_dir
from .path_d.ddi import candidate_drugs, pharmacodynamic_rules, reference_data
from .path_d.ddi.finding_agent import FindingAgent
from .path_d.ddi.finding_agent import agent_enabled as finding_agent_enabled
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
    "well tolerated", "well-tolerated", "generally safe", "safe and effective",
    "additive seizure control", "compatible", "favorable outcome", "favorable",
    "effective", "efficacy", "tolerat", "tolerab", "improv", "benefit",
    "successful", "manageable",
)
# Deliberately NOT positive keywords: "no significant interaction" / "no clinically
# significant [interaction]" -- removed (a real fix, not an oversight). A source stating
# only the ABSENCE of a documented interaction is not the same as affirmatively stating a
# favorable efficacy/tolerability/benefit outcome (see the module docstring's Positive-
# Impact guardrail) -- "no interaction was observed" describes what wasn't found, not what
# was. Keeping it as a positive trigger meant a source that never actually evaluated
# efficacy or tolerability could still resolve a pair Positive purely by stating an
# absence. The remaining keywords above all name a real, stated, affirmative outcome.
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


def _pubmed_finding_and_url(
    pubmed_result: dict[str, Any],
    *,
    cache_dir: Path,
    refresh: bool,
    memory_path: Path,
) -> tuple[str | None, str | None]:
    """The pair's PubMed-sourced Finding text and URL -- the paper's own
    Results/Conclusions text (``pubmed_lookup.fetch_abstract_sections`` +
    ``pick_results_or_conclusion_text``), never its title alone. A real,
    observed bug this fixes: a title states the paper's *aim*
    ("Effects of lamotrigine and phenytoin on the pharmacokinetics of
    atorvastatin...") never its actual result -- confirmed live that the
    same paper's real Results/Conclusions text states a specific, useful
    finding ("Pharmacokinetics of atorvastatin were not significantly
    affected by coadministration with lamotrigine...") a title-only Finding
    could never surface. Falls back to the title, at lower confidence, only
    when no abstract or no Results/Conclusions section is available for
    this PMID.

    Before any of that: ``pubmed_lookup.describes_genuine_combination``
    checks the abstract (title included) for an explicit comparator design
    (Drug A **vs.** Drug B) with no combination language anywhere -- a real
    failure mode this rejects, not just a theoretical one: a title naming
    both drugs can still be a trial that never combined them (one arm got
    Drug A added to their regimen, a *different* arm was switched to a
    different drug entirely). When that check fails, PubMed does not
    resolve this pair at all -- not even via the title -- the caller falls
    through to the next source in priority order, exactly like a source
    that returned nothing."""
    if pubmed_result.get("status") != "ok" or not pubmed_result.get("records"):
        return None, None
    record = pubmed_result["records"][0]
    title = record.get("title")
    url = record.get("url") or (f"https://pubmed.ncbi.nlm.nih.gov/{record.get('pmid')}/" if record.get("pmid") else None)
    title_text = title if isinstance(title, str) and title.strip() else None

    pmid = record.get("pmid")
    if pmid:
        abstract_result = pubmed_lookup.fetch_abstract_sections(pmid, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path)
        if abstract_result.get("status") == "ok" and abstract_result.get("records"):
            sections = dict(abstract_result["records"][0].get("sections") or {})
            if title_text:
                sections.setdefault("TITLE", title_text)
            if not pubmed_lookup.describes_genuine_combination(sections):
                return None, None
            finding_text = pubmed_lookup.pick_results_or_conclusion_text({k: v for k, v in sections.items() if k != "TITLE"})
            if finding_text:
                return finding_text, url

    # No abstract available -- the title alone still must not describe an explicit
    # comparator-only design before being trusted as a (lower-confidence) Finding.
    if title_text and not pubmed_lookup.describes_genuine_combination({"TITLE": title_text}):
        return None, None
    return title_text, url


def _trials_finding_and_url(
    trials_result: dict[str, Any],
    display_a: str,
    display_b: str,
    *,
    cache_dir: Path,
    refresh: bool,
    memory_path: Path,
) -> tuple[str | None, str | None]:
    """The pair's ClinicalTrials.gov-sourced Finding text and URL -- the
    study's own real outcome summary (``trials_lookup.
    fetch_trial_outcome_summary``), never its ``brief_title`` alone. A
    real, observed bug this fixes: NCT01111552's title named a different
    drug (Escitalopram) than the one actually being screened (Citalopram)
    even though both were genuinely listed interventions -- a title-only
    Finding stated the wrong pair's story. When results aren't posted (the
    common case) or no confident comparison can be identified, the real,
    honest reason becomes the Finding text -- never the title, and never
    silence."""
    if trials_result.get("status") != "ok" or not trials_result.get("records"):
        return None, None
    record = trials_result["records"][0]
    title = record.get("brief_title")
    title_text = title if isinstance(title, str) and title.strip() else None
    url = record.get("url")

    nct_id = record.get("nct_id")
    if nct_id:
        outcome_result = trials_lookup.fetch_trial_outcome_summary(
            nct_id, display_a, display_b, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path
        )
        if outcome_result.get("status") == "ok" and outcome_result.get("records"):
            outcome_record = outcome_result["records"][0]
            text = outcome_record.get("summary") or outcome_record.get("note")
            if text:
                return text, (outcome_record.get("url") or url)

    return title_text, url


def _tavily_finding_and_url(
    tavily_envelope: dict[str, Any],
    *,
    cache_dir: Path,
    refresh: bool,
    memory_path: Path,
) -> tuple[str | None, str | None]:
    """``tavily_envelope`` is ``search_tools.search_tavily``'s own unified
    envelope shape (``{"status", "result": {"records": [...]}, ...}``), not
    the ``{"status", "records": [...]}`` shape the other lookup modules in
    this package return -- see ``search_tools.py``'s module docstring for
    why (it normalizes every backend, including Tavily, into one common
    shape).

    The underlying page's real content, never the bare search-result title
    or snippet -- a real, observed bug this fixes: an earlier version of
    this function preferred Tavily's own bundled ``include_raw_content``
    field when present, but silently fell back to the bare ``title`` (a
    search-result title is itself a teaser, not what the page actually
    says) whenever Tavily didn't return one, which in practice is common.
    This independently fetches the result's own URL
    (``search_tools.fetch_general_page_text``) instead of depending on
    whatever Tavily's bundled extraction happens to return -- the same
    discipline PubMed's and ClinicalTrials.gov's own Finding extraction
    already applies (fetch the real resource, never settle for a title).
    The fetched page text must contain an *explicit* combination-language
    match (``pubmed_lookup.find_combination_excerpt``), which also locates
    and returns the actual matching sentence (plus one following) as the
    Finding text -- not merely ``trim_to_sentences``'s first two sentences
    of the raw page, which for an arbitrary fetched web page are frequently
    navigation/menu boilerplate concatenated right after the title, not the
    real article body (a real, observed second bug this fixes -- see
    ``find_combination_excerpt``'s own docstring). This is deliberately
    stricter than ``pubmed_lookup.describes_genuine_combination`` (used by
    PubMed and Trials): that function also accepts silently when neither a
    combination nor a comparator phrase is found at all, which is a
    reasonable default for a short, structured PubMed abstract but far too
    permissive for several thousand characters of unstructured general web
    content. If the page cannot be fetched, has no extractable text, or has
    no sentence with an explicit combination-language match, this returns
    ``(None, None)`` -- Tavily contributes no candidate for this pair at
    all, exactly like a source that returned nothing; it is never allowed
    to fall back to the title or snippet. ``title``/``content_snippet`` are
    therefore never read by this function."""
    if tavily_envelope.get("status") != "ok":
        return None, None
    records = ((tavily_envelope.get("result") or {}).get("records")) or []
    if not records:
        return None, None
    record = records[0]
    url = record.get("url")
    if not url:
        return None, None

    page_result = search_tools.fetch_general_page_text(url, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path)
    if page_result.get("status") != "ok" or not page_result.get("records"):
        return None, None

    page_text = page_result["records"][0].get("page_text")
    if not page_text:
        return None, None

    excerpt = pubmed_lookup.find_combination_excerpt(page_text, max_sentences=2)
    if not excerpt:
        return None, None

    return excerpt, url


# --- Rich evidence context for the Evidence/Finding Agent ----------------------------------
# The candidate texts above stay short on purpose: they are what _classify_text decides
# Impact from, and what the report shows if the agent is off or rejects its own output.
# The agent needs more than that to write a useful Finding (study design, population, effect
# size, the source's own recommendation), so these build a fuller view of the SAME source.
# Each reads with refresh=False, i.e. from the cache the main lookup just filled -- no extra
# network calls -- and build_report only calls them when the agent is enabled.

_AGENT_CONTEXT_MAX_CHARS = 6000


def _pubmed_agent_context(pubmed_result: dict[str, Any], *, cache_dir: Path, memory_path: Path) -> str | None:
    """Title, journal, date, and the paper's whole abstract, section by section."""
    if pubmed_result.get("status") != "ok" or not pubmed_result.get("records"):
        return None
    record = pubmed_result["records"][0]
    pmid = record.get("pmid")
    if not pmid:
        return None
    abstract_result = pubmed_lookup.fetch_abstract_sections(pmid, cache_dir=cache_dir, refresh=False, memory_path=memory_path)
    if abstract_result.get("status") != "ok" or not abstract_result.get("records"):
        return None
    sections = {k: v for k, v in (abstract_result["records"][0].get("sections") or {}).items() if k != "TITLE" and v}
    if not sections:
        return None
    lines = [f"Title: {record['title']}"] if record.get("title") else []
    published = ", ".join(str(record[k]) for k in ("source_journal", "pubdate") if record.get(k))
    if published:
        lines.append(f"Published: {published} (PMID {pmid})")
    lines += [f"{name}: {text}" for name, text in sections.items()]
    return "\n".join(lines)[:_AGENT_CONTEXT_MAX_CHARS]


def _trials_agent_context(
    trials_result: dict[str, Any], display_a: str, display_b: str, *, cache_dir: Path, memory_path: Path
) -> str | None:
    """Registry facts (title, status, phase, conditions) plus the outcome summary."""
    if trials_result.get("status") != "ok" or not trials_result.get("records"):
        return None
    record = trials_result["records"][0]
    lines = []
    for label, key in (("Trial", "nct_id"), ("Title", "brief_title"), ("Status", "overall_status")):
        if record.get(key):
            lines.append(f"{label}: {record[key]}")
    for label, key in (("Phase", "phases"), ("Conditions", "conditions")):
        if record.get(key):
            lines.append(f"{label}: {', '.join(map(str, record[key]))}")
    if record.get("nct_id"):
        outcome = trials_lookup.fetch_trial_outcome_summary(
            record["nct_id"], display_a, display_b, cache_dir=cache_dir, refresh=False, memory_path=memory_path
        )
        if outcome.get("status") == "ok" and outcome.get("records"):
            text = outcome["records"][0].get("summary") or outcome["records"][0].get("note")
            if text:
                lines.append(f"Outcome: {text}")
    return "\n".join(lines)[:_AGENT_CONTEXT_MAX_CHARS] if lines else None


def _tavily_agent_context(tavily_url: str | None, *, cache_dir: Path, memory_path: Path) -> str | None:
    """The page's own text around the combination mention -- 8 sentences, not 2."""
    if not tavily_url:
        return None
    page_result = search_tools.fetch_general_page_text(tavily_url, cache_dir=cache_dir, refresh=False, memory_path=memory_path)
    if page_result.get("status") != "ok" or not page_result.get("records"):
        return None
    page_text = page_result["records"][0].get("page_text")
    if not page_text:
        return None
    excerpt = pubmed_lookup.find_combination_excerpt(page_text, max_sentences=8)
    return excerpt[:_AGENT_CONTEXT_MAX_CHARS] if excerpt else None


def _dailymed_agent_context(subject_display_name: str, subject_section_result: dict[str, Any], other_display_name: str) -> str | None:
    if subject_section_result.get("status") != "ok" or not subject_section_result.get("records"):
        return None
    section_text = subject_section_result["records"][0].get("section_text") or ""
    context = dailymed_lookup.find_drug_mention_context(section_text, other_display_name)
    return f"{subject_display_name}'s DailyMed label, Drug Interactions section:\n{context}" if context else None


def _fetch_pubmed_candidate(
    display_a: str,
    display_b: str,
    *,
    cache_dir: Path,
    refresh: bool,
    memory_path: Path,
) -> tuple[dict[str, Any], str | None, str | None]:
    """One pair's PubMed lookup + its own finding/url extraction, bundled so
    ``build_report`` can run it on a worker thread alongside the
    ClinicalTrials.gov/Tavily equivalents below -- see that function's own
    docstring for why (these 3 network round trips were previously
    sequential, confirmed via live process inspection: a single TCP
    connection open at a time for the whole pipeline run)."""
    pubmed_result = pubmed_lookup.search_pubmed_drug_pair(display_a, display_b, cache_dir=cache_dir, memory_path=memory_path, refresh=refresh)
    pubmed_title, pubmed_url = _pubmed_finding_and_url(pubmed_result, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path)
    return pubmed_result, pubmed_title, pubmed_url


def _fetch_trials_candidate(
    display_a: str,
    display_b: str,
    *,
    cache_dir: Path,
    refresh: bool,
    memory_path: Path,
) -> tuple[dict[str, Any], str | None, str | None]:
    trials_result = trials_lookup.search_trials_drug_pair(display_a, display_b, cache_dir=cache_dir, memory_path=memory_path, refresh=refresh)
    trials_title, trials_url = _trials_finding_and_url(
        trials_result, display_a, display_b, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path
    )
    return trials_result, trials_title, trials_url


def _fetch_tavily_candidate(
    display_a: str,
    display_b: str,
    *,
    cache_dir: Path,
    refresh: bool,
    memory_path: Path,
) -> tuple[dict[str, Any], str | None, str | None]:
    tavily_result = search_tools.search_tavily(
        f"{display_a} AND {display_b} drug interaction", cache_dir=cache_dir, memory_path=memory_path, refresh=refresh
    )
    tavily_title, tavily_url = _tavily_finding_and_url(tavily_result, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path)
    return tavily_result, tavily_title, tavily_url


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
    *,
    display_a: str = "Drug A",
    display_b: str = "Drug B",
    finding_agent: FindingAgent | None = None,
    agent_contexts: dict[str, str | None] | None = None,
) -> tuple[str, str, list[str], str | None, str] | None:
    """(impact, finding, evidence_labels, hyperlink_url, primary_evidence_label) for
    one pair, or ``None`` if the pair is unresolved -- either no source
    returned anything, or every source that did return something failed to
    resolve clearly to Positive or Negative (SKILL.md Part 4: there is no
    third bucket; unresolved is dropped exactly like no-evidence, never
    shown with a guessed or default verdict). A DailyMed "not mentioned"
    result never counts as a finding here -- only a real ``mentioned: True``
    does (see module docstring).

    ``display_a``/``display_b``/``finding_agent`` are consulted for every
    candidate that reaches classification (see below), not only the one
    ultimately returned -- the Evidence/Finding Agent
    (``path_d.ddi.finding_agent.FindingAgent``) reads that candidate's own
    already-extracted text and SKILL.md's governing Finding-generation rules
    to determine what the resource actually found about this exact pair.
    ``impact`` is still decided purely by ``_classify_text`` -- the agent
    never redefines Positive/Negative classification -- but the agent CAN
    reject a candidate's usability as evidence: when it returns an explicit
    ``reason == "llm_reported_insufficient_evidence"`` (the resource does
    not genuinely concern this pair, or only restates background/cited-
    reference material -- see ``FindingAgent``), that candidate is skipped
    entirely, exactly like a retracted one, and the next candidate in
    priority order is tried instead. A model failure, a disabled agent, or
    any other non-explicit fallback reason is NOT treated this way -- the
    candidate still resolves the pair with its original deterministic text,
    exactly as before this agent existed (see ``FindingAgentOutcome.reason``
    in finding_agent.py for the full set of reasons and which of them fall
    through vs. which keep today's behavior). By default (``finding_agent``
    disabled) every candidate's ``reason`` is ``"llm_disabled"``, never the
    insufficient-evidence one, so the Finding is byte-identical to the
    pre-agent pipeline.

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
    # (candidate_id, evidence_label, text, url), priority order. ``text`` is what Impact is
    # classified from and what is published if the agent is off or rejects its own output;
    # ``agent_contexts[candidate_id]``, when given, is the fuller view of the same source the
    # agent reads instead (see _pubmed_agent_context and friends).
    candidates: list[tuple[str, str, str, str | None]] = []

    if flockhart.get("mechanism_found"):
        candidates.append(("flockhart", f"Curated DDI ({reference_data.FLOCKHART_SOURCE})", flockhart["statement"], None))
    if pd_rules:
        # Mechanism + consequence, not consequence alone -- states which property of each
        # drug class creates the risk, not just the risk itself (e.g. "Additive central
        # nervous system depression from combined GABAergic/sedative agents. Increased
        # sedation, dizziness, impaired coordination, cognitive slowing." rather than just
        # the second sentence). Both fields are already real, curated, hand-written text
        # (pharmacodynamic_rules.RULES) -- this never adds a classification-biasing prefix
        # (an earlier version prepended "Pharmacodynamic interaction risk: ", which
        # reintroduced the exact bug this fix removes: the word "risk" alone was enough to
        # classify Negative regardless of what the rule's own text said) -- mechanism text
        # itself contains no positive/negative keyword, confirmed against every real rule in
        # pharmacodynamic_rules.RULES.
        summary = "; ".join(f"{rule['mechanism']} {rule['expected_consequence']}" for rule in pd_rules)
        candidates.append(("pharmacodynamic", "Pharmacodynamic interaction reference", summary, None))
    if dailymed_mention_a.get("mentioned"):
        candidates.append(("dailymed_a", "DailyMed label (Drug Interactions section)", dailymed_mention_a["excerpt"], dailymed_mention_a.get("url")))
    if dailymed_mention_b.get("mentioned"):
        candidates.append(("dailymed_b", "DailyMed label (Drug Interactions section)", dailymed_mention_b["excerpt"], dailymed_mention_b.get("url")))
    if pubmed_title is not None:
        candidates.append(("pubmed", "External evidence (PubMed)", pubmed_title, pubmed_url))
    if trials_title is not None:
        candidates.append(("trials", "ClinicalTrials.gov", trials_title, trials_url))
    if tavily_title is not None:
        candidates.append(("tavily", _TAVILY_EVIDENCE_LABEL, tavily_title, tavily_url))

    if not candidates:
        return None

    evidence_labels = [label for _id, label, _text, _url in candidates]
    agent_contexts = agent_contexts or {}

    for candidate_id, label, text, url in candidates:
        # A retracted citation's stated conclusion can't be trusted either way (see
        # _is_retracted) -- checked before classification, not folded into _classify_text's
        # own keyword check, so it is skipped exactly like ambiguous text: try the next
        # candidate rather than forcing a verdict from it.
        if _is_retracted(text):
            continue
        keyword_impact = _classify_text(text)
        # With the agent off, an unclassifiable snippet resolves nothing (unchanged). With it on,
        # the agent still gets to read it: a snippet like "no dosage adjustment is necessary"
        # contains no classifier keyword at all, yet is exactly the explicit no-interaction
        # statement SKILL.md Part 6 Step 5 counts as Positive.
        if keyword_impact is None and not finding_agent_enabled():
            continue
        agent = finding_agent or FindingAgent()
        resource_text = agent_contexts.get(candidate_id) or text
        outcome = agent.generate_finding(display_a, display_b, label, resource_text, fallback_text=text)
        if outcome.reason == "llm_reported_insufficient_evidence":
            # The agent explicitly determined this resource does not genuinely concern this
            # pair (or only restates background/cited-reference material) -- this candidate
            # does not resolve the pair, exactly like a retracted one. Any other fallback
            # reason (disabled, model error, empty output, ...) is NOT this path -- those
            # keep today's behavior and fall through to `return` below with the original text.
            continue
        # The agent's Impact comes from the same evidence as its Finding, so the two can't
        # disagree (a real bug: "tolerability" in a snippet filed a 50-67% exposure drop under
        # Positive). The keyword verdict is used only when the agent produced no Finding.
        impact = outcome.impact if outcome.used_llm and outcome.impact in ("positive", "negative") else keyword_impact
        if impact is None:
            continue
        return impact, outcome.text, evidence_labels, url, label

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
    finding_agent: FindingAgent | None = None,
) -> dict[str, Any]:
    """``finding_agent`` defaults to a disabled :class:`FindingAgent`
    (``PRIME_AGENT_DDI_FINDING_AGENT`` unset -- see that module's docstring
    for why this is independent of ``PRIME_AGENT_ENABLE_LLM``), in which
    case every pair's Finding text is exactly what it already was before
    that agent existed. Passing an explicitly-enabled instance lets a
    winning candidate's Finding sentence be composed by the model per
    SKILL.md Part 6 instead -- Positive/Negative Impact, evidence_labels,
    and hyperlink_url are unaffected either way (see ``_pair_finding``).

    Per pair, the PubMed/ClinicalTrials.gov/Tavily lookups (each its own
    network round trip, previously issued one after another) run
    concurrently on a small worker pool -- confirmed via live process
    inspection that this pipeline was otherwise holding a single TCP
    connection open at a time for its entire run. Pairs themselves are
    still processed strictly one at a time (never more than 3 outstanding
    network calls at once, one per source, each to a different host), so
    this does not risk tripping any single source's own rate limit the way
    fanning out across many pairs at once would. DailyMed is unaffected --
    it is already fetched once per unique drug, outside this per-pair loop,
    not once per pair."""
    finding_agent = finding_agent or FindingAgent()
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
    # 3 workers, one per concurrently-fired source (PubMed/ClinicalTrials.gov/Tavily) for
    # the pair currently being processed -- not one worker per pair; pairs are still handled
    # strictly one at a time (see build_report's own docstring).
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="ddi-source-lookup") as source_executor:
        for drug_a, drug_b in pairs:
            display_a, display_b = _display_name(drug_a), _display_name(drug_b)

            flockhart = reference_data.pharmacokinetic_pair_summary(drug_a.normalized_name, drug_b.normalized_name)
            pd_rules = pharmacodynamic_rules.evaluate_pair(drug_a.normalized_name, drug_b.normalized_name)

            # The 4 live sources -- all 4 always called for every pair, none conditional on
            # what any of the others returned (see module docstring). The 3 network-bound
            # ones are fired together and awaited below, rather than one after another.
            pubmed_future = source_executor.submit(_fetch_pubmed_candidate, display_a, display_b, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path)
            trials_future = source_executor.submit(_fetch_trials_candidate, display_a, display_b, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path)
            tavily_future = source_executor.submit(_fetch_tavily_candidate, display_a, display_b, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path)

            pubmed_result, pubmed_title, pubmed_url = pubmed_future.result()
            trials_result, trials_title, trials_url = trials_future.result()
            tavily_result, tavily_title, tavily_url = tavily_future.result()

            mention_a = _dailymed_mention_result(display_a, interactions_sections_by_drug.get(drug_a.normalized_name, {}), display_b)
            mention_b = _dailymed_mention_result(display_b, interactions_sections_by_drug.get(drug_b.normalized_name, {}), display_a)

            agent_contexts = None
            if finding_agent_enabled():
                agent_contexts = {
                    "dailymed_a": _dailymed_agent_context(display_a, interactions_sections_by_drug.get(drug_a.normalized_name, {}), display_b),
                    "dailymed_b": _dailymed_agent_context(display_b, interactions_sections_by_drug.get(drug_b.normalized_name, {}), display_a),
                    "pubmed": _pubmed_agent_context(pubmed_result, cache_dir=cache_dir, memory_path=memory_path) if pubmed_title else None,
                    "trials": _trials_agent_context(trials_result, display_a, display_b, cache_dir=cache_dir, memory_path=memory_path) if trials_title else None,
                    "tavily": _tavily_agent_context(tavily_url, cache_dir=cache_dir, memory_path=memory_path) if tavily_title else None,
                }

            finding = _pair_finding(
                flockhart, pd_rules, mention_a, mention_b, pubmed_title, pubmed_url, trials_title, trials_url, tavily_title, tavily_url,
                display_a=display_a, display_b=display_b, finding_agent=finding_agent, agent_contexts=agent_contexts,
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
