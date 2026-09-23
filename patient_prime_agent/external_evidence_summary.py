"""Live external-evidence lookup summary (PubMed, ClinVar, DailyMed, CPIC,
ClinicalTrials.gov), following the same shape as the other ``*_summary.py``
modules in this package: it reads already-generated category summaries,
extracts the patient's actual current-regimen drug names and the gene
symbols the patient's own genetics panel links to those drugs, makes real
HTTP calls to five public biomedical APIs for exactly those drugs/genes,
and writes one JSON report -- which ``digital_twin_report.py`` can merge
like every other section.

This module never hardcodes a drug or gene name: the query list comes
entirely from ``clinical__notes_summary.json`` (current regimen, via
``ddi.normalizer.normalize_regimen``) and ``genetics_clinical_summary.json``
(``findings_by_therapeutic_class`` / ``metabolizer_profile``). If an API
call fails, times out, or returns nothing, that is recorded verbatim --
``status`` is ``"ok"``, ``"no_results"``, or one of
``http_client.ERROR_STATUSES`` (``"rate_limited"``, ``"network_error"``,
``"http_error"``, ``"invalid_response"``), never a single generic failure
state -- and never silently dropped or replaced with an invented result.
See ``external_lookup/`` for the five API-specific modules and
``external_lookup/http_client.py`` for the shared fetch/cache/rate-limit
contract.

Three distinct query shapes are produced, each matching a different
downstream question -- never conflated with one another:
``by_drug`` (one drug, e.g. "Lamotrigine") feeds each drug's own
therapy-level effectiveness/safety assessment; ``by_gene_drug_pair`` (one
gene + one drug, e.g. "SCN1A AND Lamotrigine") feeds the pharmacogenomics
section; ``by_drug_pair`` (two drugs combined in a single query, e.g.
"Lamotrigine AND Levetiracetam", one per real current-current pair from
``path_d.ddi.pairing.generate_pairs``) is the only one that answers a
drug-drug interaction question, and is what ``ddi_summary.py`` folds into
``current_pair_assessments``. A pair's interaction evidence is never
assembled by merging two single-drug (``by_drug``) results after the fact.

Every entry in all three shapes also carries a ``web_search_fallback`` field
(via ``external_lookup.search_tools.search_with_medical_fallback``): ``None``
unless every medical-specific source actually queried for that same
question (never one that was skipped, e.g. ClinicalTrials.gov with no
primary diagnosis) came back a genuine ``"no_results"``, in which case it
holds one Tavily general-web-search envelope. That envelope always carries
``is_medical_database: False`` and an explicit "general web search, not a
medical database" label, is never folded into ``coverage.resources_queried``
or the error/no-results counts computed from the five medical sources, and
is reported separately under ``coverage.web_search_fallback``. See
``search_tools.py`` for why Tavily is fallback-only and never a first
choice.
"""

from __future__ import annotations

import argparse
import json
import re
from itertools import combinations
from pathlib import Path
from typing import Any

from .core.utils import dedupe_preserve_order, ensure_dir, utc_now_iso
from .external_lookup import clinvar_lookup, cpic_lookup, dailymed_lookup, pubmed_lookup, search_tools, trials_lookup
from .external_lookup.http_client import DEFAULT_CACHE_DIR, ERROR_STATUSES
from .path_d.ddi import pharmacodynamic_rules, reference_data
from .path_d.ddi.normalizer import normalize_regimen

REPORT_TYPE = "External Biomedical Evidence Lookup (Live)"

DEFAULT_CLINICAL_NOTES_PATH = Path("reports") / "clinical_notes" / "clinical__notes_summary.json"
DEFAULT_GENETICS_PATH = Path("reports") / "genetics" / "genetics_clinical_summary.json"
DEFAULT_OUTPUT_PATH = Path("reports") / "external_evidence" / "External_Evidence_Report.json"

_GENE_TOKEN_RE = re.compile(r"^\s*([A-Za-z0-9]+)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the live external-evidence lookup report")
    parser.add_argument("--clinical-notes", type=Path, default=DEFAULT_CLINICAL_NOTES_PATH)
    parser.add_argument("--genetics", type=Path, default=DEFAULT_GENETICS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--refresh", action="store_true", help="Bypass the on-disk cache and re-query every live API.")
    parser.add_argument("--retmax", type=int, default=5, help="Max records to request per API call.")
    args = parser.parse_args(argv)

    report = build_report(
        clinical_notes=_load_json(args.clinical_notes),
        genetics=_load_json(args.genetics),
        cache_dir=args.cache_dir,
        refresh=args.refresh,
        retmax=args.retmax,
    )
    ensure_dir(args.output.parent)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote external evidence report to {args.output}")
    return 0


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _extract_gene_symbol(genetic_basis: str | None) -> str | None:
    if not genetic_basis:
        return None
    match = _GENE_TOKEN_RE.match(genetic_basis)
    return match.group(1) if match else None


def _current_drug_names(clinical_notes: dict[str, Any]) -> list[str]:
    medications, _conflicts = normalize_regimen(clinical_notes)
    return dedupe_preserve_order([m.normalized_name for m in medications])


def _current_drug_pairs(drug_names: list[str]) -> list[tuple[str, str]]:
    """Every unique current-current drug pair -- sorted combinations of two, exactly
    matching patient_prime_agent.path_d.ddi.pairing.generate_pairs' own current-current
    pairing -- so this module's pair-level DDI literature query lines up one-to-one with
    ddi_summary.py's current_pair_assessments. This is what makes a query like "Lamotrigine
    AND Levetiracetam" a genuine per-pair lookup rather than two single-drug lookups for
    "Lamotrigine" and "Levetiracetam" merged together after the fact.

    Deduplicated by an unordered comparison (``frozenset({a, b})``) before returning --
    ``drug_names`` is already deduped by ``_current_drug_names`` (``dedupe_preserve_order``),
    so today this never actually drops anything, but it means a future caller that passes a
    list with a repeated name (or two names that differ only in case) can never end up
    double-querying every live source for the same pair -- caught here, before any evidence
    gathering, not only if/when the caller happens to dedupe its own input first."""
    seen: set[frozenset[str]] = set()
    pairs: list[tuple[str, str]] = []
    for drug_a, drug_b in combinations(sorted(drug_names), 2):
        key = frozenset({drug_a.strip().lower(), drug_b.strip().lower()})
        if key in seen:
            continue
        seen.add(key)
        pairs.append((drug_a, drug_b))
    return pairs


def _relevant_gene_drug_pairs(genetics: dict[str, Any], drug_names: list[str]) -> list[dict[str, str]]:
    """Genes the patient's own genetics panel links to a current-regimen drug --
    never a gene the panel associates only with a drug the patient is not on."""
    drug_name_set = {name.lower() for name in drug_names}
    pairs: list[dict[str, str]] = []
    findings_by_class = genetics.get("findings_by_therapeutic_class")
    if not isinstance(findings_by_class, dict):
        return pairs
    for rows in findings_by_class.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            drug = (row.get("drug") or "").strip()
            if not drug:
                continue
            drug_key = drug.lower()
            matched_regimen_drug = next((name for name in drug_names if name.lower() in drug_key or drug_key in name.lower()), None)
            if matched_regimen_drug is None:
                continue
            gene_symbol = _extract_gene_symbol(row.get("genetic_basis"))
            if not gene_symbol:
                continue
            pairs.append(
                {
                    "gene_symbol": gene_symbol,
                    "drug_name": matched_regimen_drug,
                    "source_finding": row.get("genetic_basis"),
                    "predicted_effect": row.get("predicted_effect"),
                    "citation": row.get("citation"),
                }
            )
    return pairs


def _web_search_fallback(
    query_term: str,
    medical_envelopes: list[dict[str, Any]],
    *,
    cache_dir: Path,
    refresh: bool,
) -> dict[str, Any] | None:
    """Tavily general-web-search fallback for one query -- see
    ``external_lookup/search_tools.py``. Returns ``None`` (no fallback
    envelope at all, not an empty/placeholder one) unless every envelope in
    ``medical_envelopes`` is a genuine, checked ``"no_results"``.

    Callers must pass only the medical envelopes actually obtained for this
    exact question -- a source that was never queried for it (e.g.
    ClinicalTrials.gov when no primary diagnosis exists, reported as
    ``status="skipped"``) must be left out entirely rather than passed in as
    if it had been checked. This never widens when Tavily fires beyond
    search_tools' own "every supplied medical source says no_results" gate;
    it only decides which envelopes are honestly eligible to supply to that
    gate for a given question shape (by_drug / by_drug_pair /
    by_gene_drug_pair).
    """
    return search_tools.search_with_medical_fallback(query_term, medical_envelopes, cache_dir=cache_dir, refresh=refresh)


def _primary_diagnosis(clinical_notes: dict[str, Any]) -> str | None:
    profile = clinical_notes.get("patient_profile") if isinstance(clinical_notes, dict) else None
    if not isinstance(profile, dict):
        return None
    diagnosis = profile.get("diagnosis")
    if not isinstance(diagnosis, dict):
        return None
    return diagnosis.get("primary")


def build_report(
    clinical_notes: dict[str, Any],
    genetics: dict[str, Any],
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    retmax: int = 5,
) -> dict[str, Any]:
    drug_names = _current_drug_names(clinical_notes)
    gene_drug_pairs = _relevant_gene_drug_pairs(genetics, drug_names)
    gene_symbols = dedupe_preserve_order([pair["gene_symbol"] for pair in gene_drug_pairs])
    diagnosis = _primary_diagnosis(clinical_notes)

    if not drug_names:
        return {
            "report_type": REPORT_TYPE,
            "generated_at": utc_now_iso(),
            "status": "no_current_regimen",
            "note": "No current-regimen medication was found in clinical__notes_summary.json; no live lookups were issued.",
            "by_drug": [],
            "by_gene_drug_pair": [],
            "by_drug_pair": [],
            "coverage": {"resources_queried": [], "resources_not_in_scope": _OUT_OF_SCOPE_RESOURCES},
        }

    by_drug = []
    for drug_name in drug_names:
        pubmed_result = pubmed_lookup.search_pubmed(drug_name, retmax=retmax, cache_dir=cache_dir, refresh=refresh)
        dailymed_result = dailymed_lookup.search_dailymed(drug_name, pagesize=retmax, cache_dir=cache_dir, refresh=refresh)
        trials_result = (
            trials_lookup.search_trials(diagnosis, drug_name, page_size=retmax, cache_dir=cache_dir, refresh=refresh)
            if diagnosis
            else {"status": "skipped", "note": "No primary diagnosis found to use as the trial condition."}
        )
        # ClinicalTrials.gov was never actually queried when there's no diagnosis to search
        # for ("skipped", not "no_results") -- it must not count as an exhausted medical
        # source, so it's left out of the fallback gate's input entirely in that case.
        drug_medical_envelopes = [pubmed_result, dailymed_result]
        if trials_result.get("status") != "skipped":
            drug_medical_envelopes.append(trials_result)
        web_search_fallback = _web_search_fallback(drug_name, drug_medical_envelopes, cache_dir=cache_dir, refresh=refresh)
        by_drug.append(
            {
                "drug_name": drug_name,
                "pubmed": pubmed_result,
                "dailymed": dailymed_result,
                "clinicaltrials_gov": trials_result,
                "web_search_fallback": web_search_fallback,
            }
        )

    # Genuine drug-drug interaction evidence: ONE combined "Drug A AND Drug B" PubMed query
    # per real current-current pair (see _current_drug_pairs), not two single-drug queries
    # merged together after the fact. This is separate from by_drug's per-drug PubMed
    # lookup above, which feeds each drug's own therapy-level effectiveness/safety
    # assessment, not pair-level interaction evidence.
    by_drug_pair = []
    for drug_a, drug_b in _current_drug_pairs(drug_names):
        combined_term = f"{drug_a} AND {drug_b}"
        pubmed_pair_result = pubmed_lookup.search_pubmed(combined_term, retmax=retmax, cache_dir=cache_dir, refresh=refresh)
        web_search_fallback = _web_search_fallback(combined_term, [pubmed_pair_result], cache_dir=cache_dir, refresh=refresh)
        by_drug_pair.append(
            {
                "drug_a": drug_a,
                "drug_b": drug_b,
                "pubmed_combined_query": pubmed_pair_result,
                "web_search_fallback": web_search_fallback,
            }
        )

    clinvar_by_gene: dict[str, Any] = {}
    for gene_symbol in gene_symbols:
        clinvar_by_gene[gene_symbol] = clinvar_lookup.search_clinvar_by_gene(
            gene_symbol, retmax=retmax, cache_dir=cache_dir, refresh=refresh
        )

    by_gene_drug_pair = []
    for pair in gene_drug_pairs:
        cpic_result = cpic_lookup.lookup_cpic_pair(pair["gene_symbol"], pair["drug_name"], cache_dir=cache_dir, refresh=refresh)
        combined_term = f"{pair['gene_symbol']} AND {pair['drug_name']}"
        pubmed_pair_result = pubmed_lookup.search_pubmed(combined_term, retmax=retmax, cache_dir=cache_dir, refresh=refresh)
        clinvar_result = clinvar_by_gene.get(pair["gene_symbol"])
        gene_drug_medical_envelopes = [cpic_result, pubmed_pair_result]
        if isinstance(clinvar_result, dict):
            gene_drug_medical_envelopes.append(clinvar_result)
        web_search_fallback = _web_search_fallback(combined_term, gene_drug_medical_envelopes, cache_dir=cache_dir, refresh=refresh)
        by_gene_drug_pair.append(
            {
                "gene_symbol": pair["gene_symbol"],
                "drug_name": pair["drug_name"],
                "patient_source_finding": pair["source_finding"],
                "patient_predicted_effect": pair["predicted_effect"],
                "patient_citation": pair["citation"],
                "cpic": cpic_result,
                "clinvar": clinvar_result,
                "pubmed_combined_query": pubmed_pair_result,
                "web_search_fallback": web_search_fallback,
            }
        )

    all_statuses = [entry["pubmed"]["status"] for entry in by_drug] + [entry["dailymed"]["status"] for entry in by_drug]
    all_statuses += [entry["cpic"]["status"] for entry in by_gene_drug_pair]
    all_statuses += [v["status"] for v in clinvar_by_gene.values()]
    all_statuses += [entry["pubmed_combined_query"]["status"] for entry in by_drug_pair]

    # Tavily fallback envelopes are tracked separately from the five medical sources above --
    # never folded into all_statuses/error_count/no_results_count, which must reflect only
    # PubMed/ClinVar/DailyMed/CPIC/ClinicalTrials.gov (see the module docstring).
    fallback_envelopes = [
        entry["web_search_fallback"]
        for entry in (by_drug + by_drug_pair + by_gene_drug_pair)
        if entry.get("web_search_fallback") is not None
    ]
    fallback_status_counts = {
        status: sum(1 for e in fallback_envelopes if e.get("status") == status)
        for status in sorted({e.get("status") for e in fallback_envelopes})
    }

    return {
        "report_type": REPORT_TYPE,
        "generated_at": utc_now_iso(),
        "inputs": {
            "current_regimen_drugs": drug_names,
            "primary_diagnosis_used_for_trials": diagnosis,
            "gene_drug_pairs_from_patient_genetics": gene_drug_pairs,
        },
        "by_drug": by_drug,
        "by_gene": clinvar_by_gene,
        "by_gene_drug_pair": by_gene_drug_pair,
        "by_drug_pair": by_drug_pair,
        "coverage": {
            "resources_queried": [
                pubmed_lookup.RESOURCE_NAME,
                dailymed_lookup.RESOURCE_NAME,
                clinvar_lookup.RESOURCE_NAME,
                cpic_lookup.RESOURCE_NAME,
                trials_lookup.RESOURCE_NAME,
            ],
            "resources_not_in_scope": _OUT_OF_SCOPE_RESOURCES,
            "error_count": sum(1 for s in all_statuses if s in ERROR_STATUSES),
            "error_counts_by_category": {
                category: sum(1 for s in all_statuses if s == category) for category in sorted(ERROR_STATUSES)
            },
            "no_results_count": sum(1 for s in all_statuses if s == "no_results"),
            "web_search_fallback": {
                "resource": search_tools.TAVILY_RESOURCE_NAME,
                "is_medical_database": False,
                "label": search_tools.TAVILY_LABEL,
                "invocation_count": len(fallback_envelopes),
                "status_counts": fallback_status_counts,
                "note": (
                    "Tavily general web search is queried only when every medical-specific source actually "
                    "checked for the same question (never one that was skipped) returned no_results. It is "
                    "never counted in resources_queried, error_count, error_counts_by_category, or "
                    "no_results_count above, and must never be given the same clinical weight as a "
                    "PubMed/ClinVar/DailyMed/CPIC/ClinicalTrials.gov finding when rendered."
                ),
            },
        },
        "limitations": [
            "This module queries exactly five public APIs (PubMed, ClinVar, DailyMed, CPIC, ClinicalTrials.gov); it does "
            "not cover the rest of the NeuroTwin resource catalog (Reactome, STRING, GTEx, RCSB PDB, etc.) -- those "
            "remain out of scope for this module and must not be inferred from it.",
            "A 'no_results' status means the API returned zero matching records for the exact query issued; it is not "
            "evidence of absence, and must not be reported as a negative finding.",
            "Responses may be served from the on-disk cache in reports/external_evidence/.cache/ rather than a fresh "
            "call; each record's 'from_cache' and 'retrieved_at' fields state which. Pass --refresh to force live calls.",
            "'web_search_fallback' (present on every by_drug/by_drug_pair/by_gene_drug_pair entry) is Tavily general "
            "web search, not a medical database -- it is populated only when every medical-specific source actually "
            "checked for that question returned no_results, is labeled as general web search wherever shown, and "
            "must never be treated with the same clinical weight as a finding from the five sources above.",
        ],
    }


# ---------------------------------------------------------------------------
# Resource router: route a clinical QUESTION to only the sources actually
# relevant to it, instead of calling all five (plus the DDI module's curated
# reference data) for every query. This is a separate, additive entry point
# from build_report() above -- build_report always queries the patient's
# full current regimen against all five APIs regardless of question, and
# nothing about that behavior changes here.
#
# QUESTION_ROUTES mirrors the project's own "resource map" (the full,
# aspirational preferred-resources-per-question-type table; see README.md),
# but lists, per question type, ONLY the sources genuinely implemented in
# this codebase. A preferred resource this module cannot call (ChEMBL,
# Reactome, GTEx, RCSB PDB, PanelApp, ...) is recorded under
# "uncovered_resources" for a partially-covered question type, or makes the
# whole question type out of scope when nothing in its row is implemented --
# never silently dropped, and never answered with an empty-looking result
# from an irrelevant source instead.
# ---------------------------------------------------------------------------

QUESTION_ROUTES: dict[str, dict[str, Any]] = {
    "drug_target": {
        "label": "What does the drug act on",
        "keywords": ("act on", "acts on", "drug target", "target of the drug", "mechanism of action"),
        "preferred_resources": ("ChEMBL", "IUPHAR/BPS Guide to Pharmacology", "Open Targets"),
        "sources": (),
        "uncovered_resources": ("ChEMBL", "IUPHAR/BPS Guide to Pharmacology", "Open Targets"),
    },
    "biological_systems": {
        "label": "Which biological systems are affected",
        "keywords": ("biological system", "biological systems", "affected pathway", "pathways affected"),
        "preferred_resources": ("Reactome", "STRING", "BioGRID", "Comparative Toxicogenomics Database"),
        "sources": (),
        "uncovered_resources": ("Reactome", "STRING", "BioGRID", "Comparative Toxicogenomics Database"),
    },
    "genetics_interpretation": {
        "label": "Does patient genetics change interpretation",
        "keywords": ("patient genetics", "genetics change", "genotype", "pharmacogenomic", "pgx", "variant classification"),
        "preferred_resources": ("Genomics England PanelApp", "ClinVar", "CPIC", "PharmGKB", "NHGRI-EBI GWAS Catalog"),
        "sources": ("clinvar", "cpic"),
        "uncovered_resources": ("Genomics England PanelApp", "PharmGKB", "NHGRI-EBI GWAS Catalog"),
    },
    "drug_interaction": {
        "label": "Could two therapies interact",
        "keywords": ("could two therapies", "therapies interact", "drugs interact", "drug interaction", "drug-drug interaction", "ddi"),
        "preferred_resources": ("Flockhart Table (used internally, not queried live)", "SuperCYPsPred", "pharmacodynamic rules"),
        "sources": ("flockhart", "pharmacodynamic_rules", "pubmed_pair"),
        "uncovered_resources": ("SuperCYPsPred",),
    },
    "brain_relevance": {
        "label": "Is the mechanism relevant in brain",
        "keywords": ("relevant in brain", "in the brain", "brain expression", "cns relevance"),
        "preferred_resources": ("Human Protein Atlas Brain Atlas", "GTEx Portal", "Allen Brain Atlas"),
        "sources": (),
        "uncovered_resources": ("Human Protein Atlas Brain Atlas", "GTEx Portal", "Allen Brain Atlas"),
    },
    "exposure_changes": {
        "label": "What changes after exposure",
        "keywords": ("changes after exposure", "after exposure", "perturbation", "differential expression"),
        "preferred_resources": ("LINCS/Connectivity Map", "NCBI GEO"),
        "sources": (),
        "uncovered_resources": ("LINCS/Connectivity Map", "NCBI GEO"),
    },
    "structural_support": {
        "label": "Is structural support available",
        "keywords": ("structural support", "structure available", "protein structure", "crystal structure"),
        "preferred_resources": ("RCSB PDB", "AlphaFold DB"),
        "sources": (),
        "uncovered_resources": ("RCSB PDB", "AlphaFold DB"),
    },
    "safety_evidence": {
        "label": "What established safety evidence exists",
        "keywords": ("safety evidence", "established safety", "label warning", "contraindication"),
        "preferred_resources": ("DailyMed", "Drugs@FDA", "Tox21", "EPA CompTox"),
        "sources": ("dailymed",),
        "uncovered_resources": ("Drugs@FDA", "Tox21", "EPA CompTox"),
    },
    "studied_in_people": {
        "label": "What has been studied in people",
        "keywords": ("studied in people", "clinical trial", "published studies", "human studies", "studied clinically"),
        "preferred_resources": ("ClinicalTrials.gov", "PubMed", "OHDSI/OMOP"),
        "sources": ("clinicaltrials", "pubmed_single"),
        "uncovered_resources": ("OHDSI/OMOP",),
    },
}

_IMPLEMENTED_RESOURCE_LABELS: dict[str, str] = {
    "clinvar": "ClinVar",
    "cpic": "CPIC",
    "dailymed": "DailyMed",
    "clinicaltrials": "ClinicalTrials.gov",
    "pubmed_single": "PubMed",
    "pubmed_pair": "PubMed",
    "flockhart": "Flockhart Cytochrome P450 Drug Interaction Table (curated)",
    "pharmacodynamic_rules": "Pharmacodynamic interaction rules (curated)",
}

_QUESTION_TEXT_STOPCHARS_RE = re.compile(r"[^a-z0-9\s]")


def _normalize_question_text(question_text: str) -> str:
    return _QUESTION_TEXT_STOPCHARS_RE.sub(" ", question_text.lower())


def classify_question(question_text: str) -> dict[str, Any]:
    """Map free-text ``question_text`` to one of this module's known clinical
    question types (see ``QUESTION_ROUTES``, drawn from the project's
    resource map) and report exactly which IMPLEMENTED sources answer it --
    never all five sources, and never a source this module cannot actually
    call.

    Matching is a keyword-phrase count per question type: the type with the
    most matching phrases wins (ties broken by earliest definition in
    ``QUESTION_ROUTES``). Text matching none of the known question types, or
    a question type with no implemented source at all, both come back with
    ``in_scope: False`` and an empty ``sources`` tuple -- this function
    never guesses a plausible-looking route for text it cannot actually
    match, and never treats "unrecognized" as if it meant "answerable by
    everything".
    """
    unrecognized = {
        "question": question_text,
        "matched_route": None,
        "label": None,
        "recognized": False,
        "in_scope": False,
        "sources": (),
        "preferred_resources": (),
        "uncovered_resources": (),
    }
    if not isinstance(question_text, str) or not question_text.strip():
        return unrecognized

    normalized = _normalize_question_text(question_text)
    best_route: str | None = None
    best_score = 0
    for route_key, route in QUESTION_ROUTES.items():
        score = sum(1 for phrase in route["keywords"] if phrase in normalized)
        if score > best_score:
            best_score = score
            best_route = route_key

    if best_route is None:
        return unrecognized

    route = QUESTION_ROUTES[best_route]
    return {
        "question": question_text,
        "matched_route": best_route,
        "label": route["label"],
        "recognized": True,
        "in_scope": bool(route["sources"]),
        "sources": route["sources"],
        "preferred_resources": route["preferred_resources"],
        "uncovered_resources": route["uncovered_resources"],
    }


def _call_router_source(
    source: str,
    *,
    drug_name: str | None,
    drug_a: str | None,
    drug_b: str | None,
    gene_symbol: str | None,
    condition: str | None,
    cache_dir: Path,
    refresh: bool,
    retmax: int,
) -> dict[str, Any]:
    """Call exactly one implemented source by its router key.

    Returns an explicit ``status="missing_required_input"`` result -- never
    a crash, and never a guessed/default value -- when the caller of
    :func:`answer_clinical_question` didn't supply the argument this
    particular source needs (e.g. asking a genetics question without a
    ``gene_symbol``)."""

    def _missing(required: list[str]) -> dict[str, Any]:
        return {
            "status": "missing_required_input",
            "required": required,
            "note": (
                f"{_IMPLEMENTED_RESOURCE_LABELS[source]} needs {', '.join(required)}, which was not "
                "provided to answer_clinical_question()."
            ),
        }

    if source == "clinvar":
        if not gene_symbol:
            return _missing(["gene_symbol"])
        return clinvar_lookup.search_clinvar_by_gene(gene_symbol, retmax=retmax, cache_dir=cache_dir, refresh=refresh)
    if source == "cpic":
        if not gene_symbol or not drug_name:
            return _missing(["gene_symbol", "drug_name"])
        return cpic_lookup.lookup_cpic_pair(gene_symbol, drug_name, cache_dir=cache_dir, refresh=refresh)
    if source == "dailymed":
        if not drug_name:
            return _missing(["drug_name"])
        return dailymed_lookup.search_dailymed(drug_name, pagesize=retmax, cache_dir=cache_dir, refresh=refresh)
    if source == "clinicaltrials":
        if not condition:
            return _missing(["condition"])
        return trials_lookup.search_trials(condition, drug_name, page_size=retmax, cache_dir=cache_dir, refresh=refresh)
    if source == "pubmed_single":
        if not drug_name:
            return _missing(["drug_name"])
        return pubmed_lookup.search_pubmed(drug_name, retmax=retmax, cache_dir=cache_dir, refresh=refresh)
    if source == "pubmed_pair":
        if not drug_a or not drug_b:
            return _missing(["drug_a", "drug_b"])
        return pubmed_lookup.search_pubmed_drug_pair(drug_a, drug_b, retmax=retmax, cache_dir=cache_dir, refresh=refresh)
    if source == "flockhart":
        if not drug_a or not drug_b:
            return _missing(["drug_a", "drug_b"])
        return reference_data.pharmacokinetic_pair_summary(drug_a, drug_b)
    if source == "pharmacodynamic_rules":
        if not drug_a or not drug_b:
            return _missing(["drug_a", "drug_b"])
        fired_rules = pharmacodynamic_rules.evaluate_pair(drug_a, drug_b)
        return {"status": "ok" if fired_rules else "no_results", "rules": fired_rules}
    raise ValueError(f"Unknown router source {source!r}")  # pragma: no cover -- QUESTION_ROUTES is this module's only caller


def answer_clinical_question(
    question_text: str,
    *,
    drug_name: str | None = None,
    drug_a: str | None = None,
    drug_b: str | None = None,
    gene_symbol: str | None = None,
    condition: str | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    retmax: int = 5,
) -> dict[str, Any]:
    """Route ``question_text`` to only the sources actually relevant to it
    (see :func:`classify_question`) and call exactly those -- never all five
    sources (plus the DDI module's curated reference data) for every
    question, avoiding wasted calls to irrelevant sources.

    An out-of-scope or unrecognized question returns ``status="out_of_scope"``
    immediately, with no lookup call made at all -- never a fabricated or
    empty-looking answer -- and points to this report's Known Limitations
    section. A recognized, in-scope question calls only its mapped sources,
    passing through whichever of ``drug_name``/``drug_a``/``drug_b``/
    ``gene_symbol``/``condition`` the caller supplied; a source missing its
    required argument reports ``status="missing_required_input"`` rather
    than being silently skipped (see :func:`_call_router_source`).
    """
    classification = classify_question(question_text)

    if not classification["in_scope"]:
        if classification["recognized"]:
            scope_note = (
                f"'{classification['label']}' is not answerable by any source this module implements live "
                f"({', '.join(classification['preferred_resources'])})."
            )
        else:
            scope_note = "The question text did not match any of the clinical question types this router recognizes."
        return {
            "question": question_text,
            "matched_route": classification["matched_route"],
            "label": classification["label"],
            "status": "out_of_scope",
            "sources_called": [],
            "results": {},
            "preferred_resources": classification["preferred_resources"],
            "note": (
                "Out of scope for this module: " + scope_note +
                " See this report's Known Limitations section for the full list of resources not queried live."
            ),
        }

    results = {
        source: _call_router_source(
            source,
            drug_name=drug_name,
            drug_a=drug_a,
            drug_b=drug_b,
            gene_symbol=gene_symbol,
            condition=condition,
            cache_dir=cache_dir,
            refresh=refresh,
            retmax=retmax,
        )
        for source in classification["sources"]
    }

    response: dict[str, Any] = {
        "question": question_text,
        "matched_route": classification["matched_route"],
        "label": classification["label"],
        "status": "ok",
        "sources_called": list(classification["sources"]),
        "results": results,
    }
    if classification["uncovered_resources"]:
        queried_labels = ", ".join(dict.fromkeys(_IMPLEMENTED_RESOURCE_LABELS[s] for s in classification["sources"]))
        response["note"] = (
            f"Partially covered: {', '.join(classification['uncovered_resources'])} are part of the preferred "
            f"resources for this question but are not implemented in this module -- see Known Limitations. Only "
            f"{queried_labels} were queried."
        )
    return response


_OUT_OF_SCOPE_RESOURCES = [
    "ChEMBL", "IUPHAR/BPS Guide to Pharmacology", "Open Targets", "BindingDB", "DrugBank", "Reactome", "STRING",
    "BioGRID", "Comparative Toxicogenomics Database", "Genomics England PanelApp", "PharmGKB", "NHGRI-EBI GWAS Catalog",
    "Flockhart Table (used internally, not queried live)", "SuperCYPsPred", "Human Protein Atlas Brain Atlas",
    "GTEx Portal", "Allen Brain Atlas", "LINCS/Connectivity Map", "NCBI GEO", "RCSB PDB", "AlphaFold DB",
    "Drugs@FDA", "Tox21", "EPA CompTox", "OHDSI/OMOP", "RxNorm", "LOINC/UCUM", "SNOMED CT/HPO",
]


if __name__ == "__main__":
    raise SystemExit(main())
