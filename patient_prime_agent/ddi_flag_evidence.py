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

"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from collections import Counter
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

DEFAULT_GENETICS_PATH = Path("reports") / "genetics" / "genetics_clinical_summary.json"
_PGX_ENZYME_ROLES = ("substrate", "inhibitor", "inducer")
# SKILL.md Part 3: PGx is patient-specific context beside a pair, never a pair verdict.
PGX_CONTEXT_STATUS = "predicted"


def evidence_status(primary_evidence_label: str) -> str:
    """SKILL.md Part 5 tag for the source a Finding came from: ``curated`` (label,
    Flockhart table, pharmacodynamic rule) or ``inferred`` (literature, trial registry,
    general web search -- Tavily is never ``curated``). PGx is not a Finding source
    here; its context lines carry their own ``predicted`` tag (``PGX_CONTEXT_STATUS``)."""
    if primary_evidence_label.startswith(("Curated DDI", "Pharmacodynamic interaction reference", "DailyMed label")):
        return "curated"
    return "inferred"


# Other names for the same drug. Without these the exactly-two-drugs rule only sees the exact
# screened name: a real mouse study "...fluoxetine on the anticonvulsant action of VALPROATE and
# ethosuximide" slipped through as a two-drug pair because the screened name is "valproic acid".
_DRUG_ALIASES: dict[str, tuple[str, ...]] = {
    "valproic acid": ("valproate", "sodium valproate", "valproate sodium", "divalproex", "divalproex sodium"),
    "lithium": ("lithium carbonate",),
}
# Standard literature abbreviations, matched case-sensitively and only as whole words.
_DRUG_ABBREVIATIONS: dict[str, str] = {
    "VPA": "valproic acid", "CBZ": "carbamazepine", "LTG": "lamotrigine", "LEV": "levetiracetam",
    "OXC": "oxcarbazepine", "PHT": "phenytoin", "ETX": "ethosuximide", "FXT": "fluoxetine", "FLX": "fluoxetine",
    "ATX": "atomoxetine", "MPH": "methylphenidate",
}


def _canonical_drug(name: str) -> str:
    name = name.strip().lower()
    for canonical, aliases in _DRUG_ALIASES.items():
        if name == canonical or name in aliases:
            return canonical
    return name


def known_drug_names(medications: list[Medication], genetics: dict[str, Any] | None) -> frozenset[str]:
    """Drug names the exactly-two-drugs rule can recognise in a source's text: every
    screened drug plus every drug on the patient's PGx panel, with their alternate names. A
    third drug outside this set is not detected -- there is no general drug dictionary in
    this codebase."""
    names = {name.lower() for m in medications for name in (_display_name(m), m.normalized_name) if name}
    names |= set(_pgx_rows_by_drug(genetics or {}))
    names = {n.strip() for n in names if n and n.strip()}
    for canonical, aliases in _DRUG_ALIASES.items():
        if canonical in names:
            names.update(aliases)
    return frozenset(names)


def _third_drug_in(text: str | None, drug_a: str, drug_b: str, known_drugs: frozenset[str] | None) -> str | None:
    """SKILL.md Part 4 "Exactly-two-drugs rule": the first known drug other than the
    pair named in ``text``, or None. Word-bounded, so one drug name inside another
    (e.g. a racemate's name inside its enantiomer's) never matches on its own; the
    pair's own alternate names and abbreviations are never a third drug."""
    if not text or not known_drugs:
        return None
    lowered = text.lower()
    pair = {_canonical_drug(drug_a), _canonical_drug(drug_b)}
    for name in sorted(known_drugs):
        if _canonical_drug(name) not in pair and re.search(r"\b" + re.escape(name) + r"\b", lowered):
            return name
    known_canonical = {_canonical_drug(n) for n in known_drugs}
    for abbreviation, canonical in _DRUG_ABBREVIATIONS.items():
        if canonical in known_canonical and canonical not in pair and re.search(r"\b" + abbreviation + r"\b", text):
            return canonical
    return None


def _other_substance_in(text: str | None, drug_a: str, drug_b: str, known_drugs: frozenset[str] | None) -> str | None:
    """SKILL.md Part 4 "Pair-identity rule", applied to a generated Finding: the first substance
    named in ``text`` that is not one of the pair's own -- a known third drug, or a word that
    contains / is contained in a pair drug's name (a racemate for its enantiomer, a prodrug,
    another salt). Such a name means the Finding carries an outcome from a related substance."""
    if not text:
        return None
    other = _third_drug_in(text, drug_a, drug_b, known_drugs)
    if other:
        return other
    pair_names = {n for d in (drug_a, drug_b) for n in (_canonical_drug(d), d.strip().lower()) if n}
    pair_names |= {alias for d in (drug_a, drug_b) for alias in _DRUG_ALIASES.get(_canonical_drug(d), ())}
    for token in set(re.findall(r"[a-z][a-z-]{5,}", text.lower())):
        if token in pair_names:
            continue
        for name in pair_names:
            if len(name) >= 6 and (name in token or token in name):
                return token
    return None


# Why a candidate source was NOT used for a pair, counted per run so that "the checks are
# actually firing" can be verified from the report rather than taken on trust.
_REJECTION_COUNTS: Counter[str] = Counter()
_REJECTION_LOCK = threading.Lock()


def _reject(source: str, reason: str) -> None:
    with _REJECTION_LOCK:
        _REJECTION_COUNTS[f"{source}:{reason}"] += 1


_ANIMAL_TERMS = re.compile(
    r"\b(?:rats?|mice|mouse|murine|rodents?|hamsters?|guinea pigs?|rabbits?|canine|beagles?|porcine|piglets?|monkeys?|"
    r"macaques?|zebrafish|drosophila|in[- ]vitro|cell lines?|cell cultures?|cultured (?:cells|neurons)|sh-sy5y|hek ?293|"
    r"animal models?|preclinical|wistar|sprague[- ]dawley|c57bl)\b",
    re.IGNORECASE,
)
_HUMAN_STUDY_MARKERS = re.compile(
    r"\b(?:healthy (?:volunteers|subjects|participants)|case report|case series|were enrolled|patients (?:were|received|with)|"
    r"participants (?:were|received)|double-blind|randomi[sz]ed (?:controlled|placebo|clinical)|clinical trial|"
    r"children and adolescents|adults? (?:with|aged))\b",
    re.IGNORECASE,
)


def _non_human_evidence(text: str | None) -> bool:
    """SKILL.md Part 4 "Human-evidence-only rule": an animal or cell/in-vitro study does not
    count for a pair. True when the study text names an animal or cell system and carries no
    marker of human subjects. Heuristic: a study using animals AND humans passes."""
    return bool(text) and bool(_ANIMAL_TERMS.search(text)) and not _HUMAN_STUDY_MARKERS.search(text)


def _inferred_source_rejection(text: str, reading: str, drug_a: str, drug_b: str, known_drugs: frozenset[str] | None) -> str | None:
    """Hard gates for a literature/trial/web candidate, applied to both the short text and the
    fuller text the agent reads: human evidence only, exactly two drugs."""
    for candidate in (text, reading):
        if _non_human_evidence(candidate):
            return "animal_or_cell_study"
        if drug_a and drug_b and _third_drug_in(candidate, drug_a, drug_b, known_drugs):
            return "third_drug"
    return None


def _pgx_rows_by_drug(genetics: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The patient's significant PGx rows, keyed by lower-cased drug name, each
    with its gene symbol parsed from ``genetic_basis`` ("GSK3B (AG)" -> "GSK3B")."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for entries in (genetics.get("findings_by_therapeutic_class") or {}).values():
        for row in entries or []:
            if not isinstance(row, dict) or not row.get("significant"):
                continue
            drug = str(row.get("drug") or "").strip().lower()
            gene = str(row.get("genetic_basis") or "").split("(")[0].strip()
            if drug and gene:
                rows.setdefault(drug, []).append({**row, "gene": gene})
    return rows


def pgx_source_note(genetics: dict[str, Any] | None, path: Path = DEFAULT_GENETICS_PATH) -> dict[str, Any]:
    """Where this report's PGx findings come from, stated from the PGx file's own metadata
    (item: the report must say whether PGx is patient-specific or pooled with external sources)."""
    genetics = genetics or {}
    patient = genetics.get("patient") or {}
    return {
        "file": str(path).replace("\\", "/"),
        "document": genetics.get("report_type"),
        "report_date": patient.get("report_date"),
        "loci_analyzed": patient.get("variants_analyzed"),
        "drugs_covered": patient.get("drugs_covered"),
        "origin": "patient-specific: this patient's own PGx panel (genotype cross-referenced against the PGx source report's gene-drug annotations)",
        "pooled_with_external_sources": False,
        "traceability": "Each finding keeps the PMID/PMCID citation the PGx report attaches to it, shown in its context line. "
        "CPIC and ClinVar are not pooled into this section (see the Pharmacogenomics section).",
        "role": "context only -- PGx never decides a pair's Positive or Negative Impact (SKILL.md Part 3)",
    }


def _pgx_context(
    display_a: str, display_b: str, normalized_a: str, normalized_b: str, rows_by_drug: dict[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """SKILL.md Part 3: this patient's PGx findings shown beside a pair -- as context, never
    a verdict -- and only when the two drugs' findings are genuinely linked: both name the
    same gene, or one drug's gene is an enzyme the curated reference lists the other drug as
    a substrate/inhibitor/inducer of. Unlinked findings, or a pair where only one drug has a
    row, return None. Every finding keeps its own genotype, effect and citation."""
    rows_a = rows_by_drug.get(display_a.lower(), [])
    rows_b = rows_by_drug.get(display_b.lower(), [])
    if not rows_a or not rows_b:
        return None

    linked: list[tuple[str, dict[str, Any]]] = []
    reasons: list[str] = []
    shared_genes = sorted({r["gene"] for r in rows_a} & {r["gene"] for r in rows_b})
    for gene in shared_genes:
        linked += [(display_a, r) for r in rows_a if r["gene"] == gene] + [(display_b, r) for r in rows_b if r["gene"] == gene]
    if shared_genes:
        reasons.append(f"both drugs carry a finding for the shared gene {', '.join(shared_genes)}")
    for subject, rows, other, other_norm in ((display_a, rows_a, display_b, normalized_b), (display_b, rows_b, display_a, normalized_a)):
        enzymes = {
            rel.get("enzyme"): rel.get("role")
            for rel in reference_data.curated_cyp_relationships(other_norm)
            if rel.get("role") in _PGX_ENZYME_ROLES
        }
        for row in rows:
            if row["gene"] in enzymes and row["gene"] not in shared_genes:
                linked.append((subject, row))
                reasons.append(f"the curated reference lists {other} as a {enzymes[row['gene']]} of {row['gene']}")
    if not linked:
        return None

    seen: set[tuple[str, str, str]] = set()
    findings = []
    for drug, row in linked:
        key = (drug, str(row.get("genetic_basis")), str(row.get("predicted_effect")))
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            {
                "drug": drug,
                "gene": row["gene"],
                "genotype": row.get("genetic_basis"),
                "predicted_effect": row.get("predicted_effect"),
                "citation": row.get("citation"),
            }
        )
    return {"status": PGX_CONTEXT_STATUS, "link": "; ".join(reasons), "findings": findings}



_POSITIVE_KEYWORDS: tuple[str, ...] = (
    "well tolerated", "well-tolerated", "generally safe", "safe and effective",
    "additive seizure control", "compatible", "favorable outcome", "favorable",
    "effective", "efficacy", "tolerat", "tolerab", "improv", "benefit",
    "successful", "manageable",
    # Comparative/response wording a real efficacy result uses (audit of real report text:
    # "Response to treatment was significantly better in ..." resolved nothing).
    "significantly better", "superior to", "remission",
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
    # Interaction outcomes the original list missed (audit of real report text: a case report
    # of "subtherapeutic levels" and a warning to monitor for serotonin syndrome both resolved
    # nothing). All name a harm or a failure of therapy, never a neutral change.
    "toxic", "subtherapeutic", "supratherapeutic", "serotonin syndrome", "serotonin-syndrome",
    "qt prolong", "qtc prolong", "prolonged qt", "respiratory depression", "sedation", "sedative",
    # "potentiate" (not "potentiat"): must not match "long-term potentiation". Deficits are
    # listed by kind: a bare "deficit" would flag every "attention-deficit" (ADHD) abstract.
    "potentiate", "elevat", "accelerat", "memory deficit", "cognitive deficit", "motor deficit",
    "deficits induced", "bleeding", "not recommended",
    "should be avoided", "avoid concomitant", "avoid coadministration", "avoid the combination",
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


# Phrases checked before single keywords, so "reduced seizure frequency" (a benefit) is never
# also counted as the risk word "reduc", and "no significant interaction" is read as a whole.
_BENEFIT_PHRASE = (
    r"(?:reduc|decreas|lower|improv)\w*\s+(?:[\w-]+\s+){0,3}?(?:risk|frequency|severity|incidence|occurrence|number"
    r"|symptoms?|seizures?|attacks?|nausea|vomiting|depressi\w*|anxiety|pain|relapse|irritability|aggression|mania)"
)
_NO_INTERACTION_PHRASE = (
    r"no\s+(?:clinically\s+)?(?:significant|meaningful|relevant|important)\s+(?:pharmacokinetic\s+|drug[- ]drug\s+)?interaction"
    r"|did\s+not\s+significantly\s+(?:affect|alter|change)|(?:was|were)\s+not\s+significantly\s+(?:affected|altered|changed)"
    r"|no\s+(?:dose|dosage)\s+adjustment|(?:does|do)\s+not\s+require\s+(?:a\s+)?(?:dose|dosage)\s+adjustment"
)
_INTERACTION_PHRASE = r"(?:clinically\s+)?significant\s+(?:pharmacokinetic\s+|drug[- ]drug\s+)?interaction"
# A stated "no difference / no change / no effect" -- the affirmative no-interaction outcome
# (SKILL.md Part 6-style evaluated statement), never mere silence.
_NO_DIFFERENCE_PHRASE = (
    r"no\s+(?:clinically\s+)?(?:significant|meaningful|relevant|important)\s+(?:differences?|changes?|effects?|alterations?|impacts?)"
    r"|not\s+significantly\s+different|did\s+not\s+differ\s+significantly|no\s+evidence\s+of\s+(?:an?\s+)?interaction"
)
# SKILL.md Part 2: a stated mechanism of interaction -- an enzyme or transporter being inhibited
# or induced -- is itself a documented interaction. Deliberately specific: a bare "inhibitor"
# would also match every class name ("selective serotonin reuptake inhibitor").
_MECHANISM_PHRASE = (
    r"(?:inhibit|induc)\w*\s+(?:of\s+)?(?:cyp\s?\w*|p450|cytochrome|ugt\w*|p-?gp|p-glycoprotein|the\s+metabolism|metabolism)"
    r"|(?:strong|moderate|potent|weak|prototype)\s+(?:cyp\s?\w+\s+)?(?:inhibitors?|inducers?)"
    r"|cyp\s?\w+\s+(?:inhibitors?|inducers?)"
    r"|enzyme\s+(?:induction|inhibition)"
)
# Pharmacokinetic quantity changed: unambiguous because the object is a drug level, not an
# outcome -- "increased plasma concentrations", "exposure ... could increase".
_PK_QUANTITY = r"(?:concentrations?|levels?|exposure|auc|cmax|clearance|half-life|plasma|serum)"
_PK_CHANGE_PHRASE = (
    r"(?:increas|decreas|reduc|elevat|rais|lower|rose|fell)\w*\s+(?:[\w-]+\s+){0,3}?" + _PK_QUANTITY +
    r"|" + _PK_QUANTITY + r"\s+(?:of\s+[\w-]+\s+)?(?:could|may|might|can|will|was|were|is|are)?\s*(?:increas|decreas|reduc|elevat|rais|lower)\w*"
)
# A stated dosing consequence of the combination ("dosage adjustment ... may be necessary").
_DOSE_CHANGE_PHRASE = (
    r"(?:dos(?:e|age)\s+(?:adjustment|reduction|increase|modification|change)|(?:increase|decrease|reduce|adjust)\s+(?:the\s+)?dos(?:e|age))"
    r"[^.;]{0,90}?(?:may|might|should|will|must)\s+(?:be\s+)?(?:necessary|required|needed|recommended|considered)"
)
_NEGATION_WORDS = re.compile(
    r"\b(?:no|not|non|without|never|neither|nor|lack|absence|failed|unlikely|cannot)\b|n't\b"
)
_KEYWORD_PATTERN = re.compile(
    "(?P<nointeraction>" + _NO_INTERACTION_PHRASE + "|" + _NO_DIFFERENCE_PHRASE + ")"
    "|(?P<benefit>" + _BENEFIT_PHRASE + ")"
    "|(?P<pk>" + _PK_CHANGE_PHRASE + ")"
    "|(?P<mech>" + _MECHANISM_PHRASE + ")"
    "|(?P<dose>" + _DOSE_CHANGE_PHRASE + ")"
    "|(?P<interaction>" + _INTERACTION_PHRASE + ")"
    "|(?P<pos>" + "|".join(re.escape(k) for k in sorted(_POSITIVE_KEYWORDS, key=len, reverse=True)) + ")"
    "|(?P<neg>" + "|".join(re.escape(k) for k in sorted(_NEGATIVE_KEYWORDS, key=len, reverse=True)) + ")"
)


# Warning words whose negation only says a concern was not raised -- not an outcome.
_SILENCE_WORDS = frozenset({"risk", "caution", "danger", "contraindicat"})


def _is_negated(lowered: str, start: int) -> bool:
    """True when a negation word sits within the 4 words before ``start``, in
    the same clause -- "was **not** well tolerated", "did **not** increase"."""
    window = lowered[max(0, start - 80) : start]
    for boundary in ".;:,":
        window = window.rsplit(boundary, 1)[-1]
    return bool(_NEGATION_WORDS.search(" ".join(window.split()[-4:])))


def _sentence_verdict(sentence: str, allow_positive: bool) -> str | None:
    """"positive" / "negative" / "mixed" / None for one sentence."""
    lowered = sentence.lower()
    pos = neg = 0
    for match in _KEYWORD_PATTERN.finditer(lowered):
        kind = match.lastgroup
        negated = _is_negated(lowered, match.start())
        if kind == "nointeraction":
            pos += 1  # already a negated phrase ("no significant interaction")
        elif kind == "benefit":
            if negated:
                neg += 1  # "did not improve symptoms"
            else:
                pos += 1
        elif kind in ("interaction", "neg", "pk", "mech", "dose"):
            if not negated:
                neg += 1
            elif kind == "interaction" or match.group(0) not in _SILENCE_WORDS:
                pos += 1  # "no significant interaction", "did not increase", "no adverse events"
            # else: "without caution", "no risk" -- the source just doesn't raise a concern.
            # That is silence, which SKILL.md Part 4 says is never evidence the pair is fine.
        elif kind == "pos":
            if negated:
                neg += 1  # "not well tolerated", "no improvement", "no benefit"
            else:
                pos += 1
    if not allow_positive:
        return "negative" if neg else None
    if pos and neg:
        return "mixed"
    return "positive" if pos else "negative" if neg else None


def _classify_text(text: str | None, *, allow_positive: bool = True) -> str | None:
    """Positive / Negative / None (unresolved) for one source's own evidence
    text, following SKILL.md Part 4.

    - **Negation-aware**: "no significant interaction was observed" and "a
      significant interaction was observed" classify oppositely; "not well
      tolerated" is never read as "well tolerated".
    - **Conclusion-weighted, not conclusion-only**: each sentence gets its own
      verdict. The pair resolves only when the sentences that take a clear side
      agree -- a Conclusion contradicted by its own Results resolves nothing.
    - **Curated sources** (a label, the Flockhart table, a pharmacodynamic rule)
      pass ``allow_positive=False``: they only ever state known risks, so they
      can establish Negative but never Positive (Part 4 tier 2). A real bug this
      fixes: a label line ending "...monitor clinical response and tolerability"
      used to classify Positive purely on the word "tolerab".

    Unresolved text makes the caller fall through to the next source rather
    than guess. Does not check for retraction -- see ``_is_retracted``."""
    if not isinstance(text, str) or not text.strip():
        return None
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    verdicts = {_sentence_verdict(s, allow_positive) for s in sentences} - {None, "mixed"}
    return verdicts.pop() if len(verdicts) == 1 else None


def _pubmed_finding_and_url(
    pubmed_result: dict[str, Any],
    *,
    cache_dir: Path,
    refresh: bool,
    memory_path: Path,
    display_a: str | None = None,
    display_b: str | None = None,
    known_drugs: frozenset[str] | None = None,
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
            if not pubmed_lookup.describes_genuine_combination(sections, display_a, display_b):
                _reject("pubmed", "not_studied_together")
                return None, None
            # The study itself (title, Methods, Results, Conclusion -- not Background, which
            # describes prior work) must be human evidence (Part 4 human-evidence-only rule)
            # and involve only this pair (exactly-two-drugs rule).
            study_text = " ".join(v for k, v in sections.items() if v and k.upper() not in pubmed_lookup._BACKGROUND_LABELS)
            if _non_human_evidence(study_text):
                _reject("pubmed", "animal_or_cell_study")
                return None, None
            if display_a and display_b and _third_drug_in(study_text, display_a, display_b, known_drugs):
                _reject("pubmed", "third_drug")
                return None, None
            finding_text = pubmed_lookup.pick_results_or_conclusion_text({k: v for k, v in sections.items() if k != "TITLE"})
            if finding_text:
                return finding_text, url

    # No abstract, or no Results/Conclusion in it: SKILL.md Part 4 -- a title is never enough
    # on its own (it states what a paper set out to do, not what it found).
    _reject("pubmed", "no_usable_abstract")
    return None, None


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
    url = record.get("url")

    nct_id = record.get("nct_id")
    if nct_id:
        outcome_result = trials_lookup.fetch_trial_outcome_summary(
            nct_id, display_a, display_b, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path
        )
        if outcome_result.get("status") == "ok" and outcome_result.get("records"):
            outcome_record = outcome_result["records"][0]
            # Only posted results are an outcome. A "results not posted" note, or the
            # trial's title, says what the trial set out to do -- SKILL.md Part 4: never
            # enough on its own to resolve a pair.
            if outcome_record.get("summary"):
                return outcome_record["summary"], (outcome_record.get("url") or url)

    _reject("trials", "no_posted_results")
    return None, None


def _tavily_finding_and_url(
    tavily_envelope: dict[str, Any],
    *,
    cache_dir: Path,
    refresh: bool,
    memory_path: Path,
    display_a: str | None = None,
    display_b: str | None = None,
    known_drugs: frozenset[str] | None = None,
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
        _reject("tavily", "page_unreadable")
        return None, None

    page_text = page_result["records"][0].get("page_text")
    if not page_text:
        _reject("tavily", "page_unreadable")
        return None, None

    excerpt = pubmed_lookup.find_combination_excerpt(page_text, max_sentences=2)
    if not excerpt:
        _reject("tavily", "no_combination_text")
        return None, None
    # SKILL.md Part 4: a title is never enough -- and a page's own title (or its navigation
    # text) often sits at the top of the fetched page text, where the combination phrase
    # search can land on it. Real case: "Caution in co-administration of aripiprazole and
    # lithium ... - MedCrave online --> --> About Us . Paper Submission ." was published
    # as a Finding. Drop the title, reject page chrome, and require a real sentence left.
    title = str(record.get("title") or "").strip()
    remainder = excerpt
    for piece in {title, title.split(" - ")[0].strip(), title.split(" | ")[0].strip()}:
        if len(piece) > 15:
            remainder = re.sub(re.escape(piece), " ", remainder, flags=re.IGNORECASE)
    if "-->" in remainder or len(remainder.split()) < 8:
        _reject("tavily", "page_title_or_navigation_only")
        return None, None
    # Exactly-two-drugs rule: the page's title names what the study is about. A real case: an
    # excerpt that only said "FXT, antiepileptic drugs ... VPA" came from a page titled "Effects
    # of fluoxetine on the anticonvulsant action of valproate and ethosuximide".
    if display_a and display_b and _third_drug_in(title, display_a, display_b, known_drugs):
        _reject("tavily", "third_drug")
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
    known_drugs: frozenset[str] | None = None,
) -> tuple[dict[str, Any], str | None, str | None]:
    """One pair's PubMed lookup + its own finding/url extraction, bundled so
    ``build_report`` can run it on a worker thread alongside the
    ClinicalTrials.gov/Tavily equivalents below -- see that function's own
    docstring for why (these 3 network round trips were previously
    sequential, confirmed via live process inspection: a single TCP
    connection open at a time for the whole pipeline run)."""
    pubmed_result = pubmed_lookup.search_pubmed_drug_pair(display_a, display_b, cache_dir=cache_dir, memory_path=memory_path, refresh=refresh)
    pubmed_title, pubmed_url = _pubmed_finding_and_url(
        pubmed_result, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path, display_a=display_a, display_b=display_b,
        known_drugs=known_drugs,
    )
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
    known_drugs: frozenset[str] | None = None,
) -> tuple[dict[str, Any], str | None, str | None]:
    tavily_result = search_tools.search_tavily(
        f"{display_a} AND {display_b} drug interaction", cache_dir=cache_dir, memory_path=memory_path, refresh=refresh
    )
    tavily_title, tavily_url = _tavily_finding_and_url(
        tavily_result, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path,
        display_a=display_a, display_b=display_b, known_drugs=known_drugs,
    )
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


class _PairVerdict(tuple):
    """``(impact, finding, evidence_labels, hyperlink_url, primary_evidence_label)`` -- unpacks as
    exactly that 5-tuple. ``details`` carries the agent's mechanism / direction / consequence,
    recommendation, only those its source states (empty otherwise). ``evidence_type`` is the
    agent's SKILL.md Step 5 type ("conflict" when reliable sources disagree, in which case
    ``conflict`` lists what each source found); an ``impact`` of ``"none"`` means the only evidence
    was combination-treatment efficacy, which is not an interaction verdict."""

    details: dict[str, str] | None = None
    evidence_type: str | None = None
    conflict: list[dict[str, Any]] | None = None


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
    known_drugs: frozenset[str] | None = None,
) -> _PairVerdict | None:
    """The pair's verdict -- ``(impact, finding, evidence_labels, hyperlink_url,
    primary_evidence_label)`` -- or ``None`` when no source resolves it (the pair is excluded;
    SKILL.md Part 4 has no third bucket). A DailyMed "not mentioned" result is never a finding.

    Candidates are tried in priority order (Flockhart, pharmacodynamic rules, DailyMed, PubMed,
    ClinicalTrials.gov, Tavily). Retracted sources are skipped, and a literature/trial/web
    candidate must pass the human-evidence-only and exactly-two-drugs gates (Part 4) first.

    **Agent mode** (``finding_agent`` given and enabled): the Finding Agent reads each surviving
    candidate and decides the Impact and writes the 1-2 sentence Finding from that candidate's
    own evidence (SKILL.md Part 6). Keyword classification is never consulted, and if the agent
    cannot answer for a candidate (an API error, or its answer fails a guardrail) that candidate
    is skipped -- raw source text is never published, and no keyword verdict stands in for the
    agent. A ``NO_FINDING`` answer skips the candidate exactly like a source that returned nothing.

    **Legacy mode** (no agent): the earlier keyword classifier (``_classify_text``) with the
    source's own text as the Finding. Kept only for offline runs and tests; the report states
    which mode produced it (``classification_method``).

    ``evidence_labels`` lists every source that returned something real, in priority order, whether
    or not it resolved the pair; ``primary_evidence_label`` is the one that did."""
    candidates: list[tuple[str, str, str, str | None]] = []  # (candidate_id, label, text, url)

    if flockhart.get("mechanism_found"):
        candidates.append(("flockhart", f"Curated DDI ({reference_data.FLOCKHART_SOURCE})", flockhart["statement"], None))
    if pd_rules:
        # Mechanism + consequence, both real hand-written text (pharmacodynamic_rules.RULES);
        # no classification-biasing prefix is ever added.
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

    evidence_labels = [label for _cid, label, _text, _url in candidates]
    agent_contexts = agent_contexts or {}
    use_agent = finding_agent is not None and finding_agent_enabled()

    # Agent mode reads every non-web candidate that survives the gates (so two reliable sources that
    # disagree are both seen) and general web search only when none of them resolved the pair.
    accepted: list[tuple[str, str, str | None, Any]] = []  # (candidate_id, label, url, outcome)
    efficacy_only: list[tuple[str, str, str | None, Any]] = []

    for candidate_id, label, text, url in candidates:
        if candidate_id == "tavily" and accepted:
            continue  # general web evidence never overrides or contradicts a stronger source
        # A retracted citation's stated conclusion can't be trusted either way (_is_retracted).
        if _is_retracted(text):
            _reject(candidate_id, "retracted")
            continue
        status = evidence_status(label)
        reading = agent_contexts.get(candidate_id) or text
        if status == "inferred":
            gate = _inferred_source_rejection(text, reading, display_a, display_b, known_drugs)
            if gate:
                _reject(candidate_id, gate)
                continue

        if use_agent:
            outcome = finding_agent.generate_finding(
                display_a, display_b, label, reading, fallback_text=text,
                allow_recommendation=candidate_id != "tavily",  # general web evidence never supplies advice
                other_drug_check=lambda finding: _other_substance_in(finding, display_a, display_b, known_drugs),
            )
            if outcome.reason == "llm_reported_insufficient_evidence":
                _reject(candidate_id, "agent_no_finding")
                continue
            if not outcome.used_llm or outcome.impact not in ("positive", "negative", "none"):
                _reject(candidate_id, f"agent_unusable:{outcome.reason}")
                continue
            if outcome.evidence_type == "combination_efficacy":
                # The combination treated a condition; nothing about an interaction (SKILL.md Step 5).
                _reject(candidate_id, "combination_efficacy_only")
                efficacy_only.append((candidate_id, label, url, outcome))
                continue
            if outcome.impact not in ("positive", "negative"):
                _reject(candidate_id, "agent_unusable:no_interaction_verdict")
                continue
            accepted.append((candidate_id, label, url, outcome))
            continue

        # Legacy keyword mode. Curated sources state known risks, so they can establish Negative
        # but not Positive on keywords alone.
        impact = _classify_text(text, allow_positive=status != "curated")
        if impact is None:
            continue
        return _PairVerdict((impact, text, evidence_labels, url, label))

    if not accepted:
        if efficacy_only:
            _cid, label, url, outcome = efficacy_only[0]
            verdict = _PairVerdict(("none", outcome.text, evidence_labels, url, label))
            verdict.details = outcome.details
            verdict.evidence_type = "combination_efficacy"
            return verdict
        return None

    _cid, label, url, outcome = accepted[0]
    verdict = _PairVerdict((outcome.impact, outcome.text, evidence_labels, url, label))
    verdict.details = outcome.details
    verdict.evidence_type = outcome.evidence_type

    # Reliable sources that genuinely disagree (one: no interaction; another: a pharmacokinetic or
    # pharmacodynamic interaction) are both kept -- never a silent pick of one (SKILL.md Part 4).
    no_effect = next((a for a in accepted if a[3].evidence_type == "no_interaction"), None)
    interaction = next((a for a in accepted if a[3].evidence_type in ("pharmacokinetic", "pharmacodynamic") and a[3].impact == "negative"), None)
    if no_effect and interaction:
        def _short(source_label: str) -> str:
            for key, name in (("DailyMed", "DailyMed label"), ("PubMed", "PubMed study"), ("ClinicalTrials", "ClinicalTrials.gov record"), ("Curated", "Curated reference")):
                if key in source_label:
                    return name
            return source_label

        first, second = (no_effect, interaction)
        verdict = _PairVerdict((
            "negative",
            f"Sources disagree. {_short(first[1])}: {first[3].text} {_short(second[1])}: {second[3].text}",
            evidence_labels, accepted[0][2], accepted[0][1],
        ))
        verdict.evidence_type = "conflict"
        verdict.details = None
        verdict.conflict = [
            {"source": src[1], "evidence_type": src[3].evidence_type, "impact": src[3].impact, "finding": src[3].text, "url": src[2]}
            for src in (first, second)
        ]
    return verdict


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
    genetics: dict[str, Any] | None = None,
    finding_agent: FindingAgent | None = None,
) -> dict[str, Any]:
    """``genetics`` is the patient's PGx panel summary (``reports/genetics/
    genetics_clinical_summary.json``), used only as patient-specific *context* beside a linked
    pair (SKILL.md Part 3) -- never to classify. ``finding_agent`` (when given and enabled) writes
    each Finding and decides its Impact from the pair's own evidence; without it the legacy keyword
    classifier runs, and the report says so in ``classification_method``. An enabled agent that
    cannot be built, or that fails for a large share of calls, raises -- the report is never
    silently produced by a different classifier.

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
    use_agent = finding_agent is not None and finding_agent_enabled()
    if use_agent and finding_agent.backend is None:
        from .path_d.ddi.finding_agent import build_backend

        finding_agent.backend = build_backend()  # raises on a missing key / unknown provider; no silent keyword fallback
    with _REJECTION_LOCK:
        _REJECTION_COUNTS.clear()
    pgx_rows = _pgx_rows_by_drug(genetics or {})
    medications = _screening_medications(clinical_notes or {})
    known_drugs = known_drug_names(medications, genetics)
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
    efficacy_only_pairs: list[dict[str, Any]] = []
    seen_pairs: set[frozenset[str]] = set()
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
            pubmed_future = source_executor.submit(
                _fetch_pubmed_candidate, display_a, display_b, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path, known_drugs=known_drugs
            )
            trials_future = source_executor.submit(_fetch_trials_candidate, display_a, display_b, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path)
            tavily_future = source_executor.submit(
                _fetch_tavily_candidate, display_a, display_b, cache_dir=cache_dir, refresh=refresh, memory_path=memory_path, known_drugs=known_drugs
            )

            pubmed_result, pubmed_title, pubmed_url = pubmed_future.result()
            trials_result, trials_title, trials_url = trials_future.result()
            tavily_result, tavily_title, tavily_url = tavily_future.result()

            mention_a = _dailymed_mention_result(display_a, interactions_sections_by_drug.get(drug_a.normalized_name, {}), display_b)
            mention_b = _dailymed_mention_result(display_b, interactions_sections_by_drug.get(drug_b.normalized_name, {}), display_a)

            agent_contexts = None
            if use_agent:
                agent_contexts = {
                    "dailymed_a": _dailymed_agent_context(display_a, interactions_sections_by_drug.get(drug_a.normalized_name, {}), display_b),
                    "dailymed_b": _dailymed_agent_context(display_b, interactions_sections_by_drug.get(drug_b.normalized_name, {}), display_a),
                    "pubmed": _pubmed_agent_context(pubmed_result, cache_dir=cache_dir, memory_path=memory_path) if pubmed_title else None,
                    "trials": _trials_agent_context(trials_result, display_a, display_b, cache_dir=cache_dir, memory_path=memory_path) if trials_title else None,
                    "tavily": _tavily_agent_context(tavily_url, cache_dir=cache_dir, memory_path=memory_path) if tavily_title else None,
                }
            finding = _pair_finding(
                flockhart, pd_rules, mention_a, mention_b, pubmed_title, pubmed_url, trials_title, trials_url, tavily_title, tavily_url,
                display_a=display_a, display_b=display_b, finding_agent=finding_agent if use_agent else None,
                agent_contexts=agent_contexts, known_drugs=known_drugs,
            )
            if finding is None:
                dropped += 1
                continue
            if finding[0] == "none":  # combination-treatment efficacy only: not an interaction finding
                dropped += 1
                efficacy_only_pairs.append(
                    {
                        "drug_a": display_a, "drug_b": display_b, "finding": finding[1], "source": finding[4],
                        "evidence_status": evidence_status(finding[4]), "hyperlink_url": finding[3],
                        "note": "Combination-treatment efficacy evidence, not an interaction finding; the pair is unresolved as a DDI.",
                    }
                )
                continue
            pair_key = frozenset({drug_a.normalized_name, drug_b.normalized_name})
            if pair_key in seen_pairs:  # defensive: a pair is counted once, whichever order it was screened in
                continue
            seen_pairs.add(pair_key)

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
                    "evidence_status": evidence_status(primary_evidence_label),
                    # Each listed source with its own SKILL.md Part 5 tag, so e.g. a general web
                    # search listed beside a curated label is never read as curated itself.
                    "evidence_sources": [{"label": label, "status": evidence_status(label)} for label in evidence_labels],
                    "evidence_details": dict(getattr(finding, "details", None) or {}),
                    # SKILL.md Step 5: pharmacokinetic / pharmacodynamic / adverse_effect / no_interaction / mixed
                    # (or "conflict" when reliable sources disagree, with each source's own finding below).
                    "evidence_type": getattr(finding, "evidence_type", None),
                    "evidence_conflict": list(getattr(finding, "conflict", None) or []),
                    "pgx_context": _pgx_context(display_a, display_b, drug_a.normalized_name, drug_b.normalized_name, pgx_rows),
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

    agent_stats = dict(finding_agent.stats) if use_agent else {}
    if use_agent:
        calls = sum(v for k, v in agent_stats.items() if k in ("accepted", "no_finding", "error") or k.startswith("rejected:"))
        if calls and agent_stats.get("error", 0) > 0.25 * calls:
            raise RuntimeError(
                f"The Finding Agent failed on {agent_stats['error']} of {calls} calls ({finding_agent.last_error_type}); "
                "not writing a report that would silently exclude those pairs."
            )
        backend_info = finding_agent.backend.describe() if hasattr(finding_agent.backend, "describe") else {}
        classification_method = (
            f"finding_agent ({backend_info.get('backend', '?')}/{backend_info.get('model', '?')}): the agent decides each pair's "
            "Impact and writes its Finding from that pair's own evidence (SKILL.md Part 6)"
        )
    else:
        classification_method = "keyword_legacy: Finding Agent not enabled -- Impact by keyword matching on the source's own text"
    with _REJECTION_LOCK:
        rejection_counts = dict(sorted(_REJECTION_COUNTS.items()))

    return {
        "report_type": "Drug Interaction Flag Sheet -- Live Multi-Source Evidence",
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "screening_drugs": [m.source_name for m in medications],
        "pairs_checked": len(pairs),
        "pairs_with_evidence": len(pairs_out),
        "unique_pairs_with_evidence": len(seen_pairs),
        "counting_basis": "unique unordered drug pairs: A+B and B+A are one pair, counted once (the per-drug sections of the "
        "rendered report may show a pair under both of its drugs for readability; the totals never double-count it)",
        "pairs_dropped_unresolved": dropped,
        "combination_efficacy_only": efficacy_only_pairs,
        "classification_method": classification_method,
        "finding_agent_stats": agent_stats,
        "source_rejection_counts": rejection_counts,
        "pgx_source": pgx_source_note(genetics),
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
            "Positive/Negative Impact and the Finding come from the Finding Agent reading each pair's own source "
            "evidence (see classification_method); a title, snippet or keyword hit alone never decides a pair. "
            "Animal/cell studies, comparator-only studies, retracted papers and studies involving a third drug are "
            "excluded (source_rejection_counts shows how many of each). Patient PGx findings are shown as context "
            "only and never classify a pair.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch live multi-source evidence for the expanded Drug Interaction Flag Sheet screening pairs")
    parser.add_argument("--clinical-notes", type=Path, default=DEFAULT_CLINICAL_NOTES_PATH)
    parser.add_argument("--genetics", type=Path, default=DEFAULT_GENETICS_PATH, help="Patient PGx panel summary (SKILL.md Part 4 tier 1)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--refresh", action="store_true", help="Bypass the 30-day memory-store cache and re-query every live source")
    parser.add_argument(
        "--legacy-keyword-classifier",
        action="store_true",
        help="Offline/testing only: classify by keyword matching instead of the Finding Agent. By default the "
        "Finding Agent is REQUIRED -- a missing API key is an error, never a silent fall-back to keywords.",
    )
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

    agent: FindingAgent | None = None
    if not args.legacy_keyword_classifier:
        os.environ["PRIME_AGENT_DDI_FINDING_AGENT"] = "1"
        agent = FindingAgent()
    try:
        report = build_report(clinical_notes=_load_json(args.clinical_notes), refresh=args.refresh, genetics=_load_json(args.genetics), finding_agent=agent)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        print("No report was written. Fix the cause (e.g. set OPENAI_API_KEY) or pass --legacy-keyword-classifier for an offline keyword run.")
        return 1
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
    print(f"Classification: {report['classification_method']}")
    if report["finding_agent_stats"]:
        print(f"Finding Agent outcomes: {report['finding_agent_stats']}")
    print(f"Sources rejected (reason counts): {report['source_rejection_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
