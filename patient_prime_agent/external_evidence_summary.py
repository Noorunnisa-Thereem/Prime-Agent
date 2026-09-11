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
"""

from __future__ import annotations

import argparse
import json
import re
from itertools import combinations
from pathlib import Path
from typing import Any

from .core.utils import dedupe_preserve_order, ensure_dir, utc_now_iso
from .external_lookup import clinvar_lookup, cpic_lookup, dailymed_lookup, pubmed_lookup, trials_lookup
from .external_lookup.http_client import DEFAULT_CACHE_DIR, ERROR_STATUSES
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
    "Lamotrigine" and "Levetiracetam" merged together after the fact."""
    return list(combinations(sorted(drug_names), 2))


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
        by_drug.append(
            {
                "drug_name": drug_name,
                "pubmed": pubmed_result,
                "dailymed": dailymed_result,
                "clinicaltrials_gov": trials_result,
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
        by_drug_pair.append({"drug_a": drug_a, "drug_b": drug_b, "pubmed_combined_query": pubmed_pair_result})

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
        by_gene_drug_pair.append(
            {
                "gene_symbol": pair["gene_symbol"],
                "drug_name": pair["drug_name"],
                "patient_source_finding": pair["source_finding"],
                "patient_predicted_effect": pair["predicted_effect"],
                "patient_citation": pair["citation"],
                "cpic": cpic_result,
                "clinvar": clinvar_by_gene.get(pair["gene_symbol"]),
                "pubmed_combined_query": pubmed_pair_result,
            }
        )

    all_statuses = [entry["pubmed"]["status"] for entry in by_drug] + [entry["dailymed"]["status"] for entry in by_drug]
    all_statuses += [entry["cpic"]["status"] for entry in by_gene_drug_pair]
    all_statuses += [v["status"] for v in clinvar_by_gene.values()]
    all_statuses += [entry["pubmed_combined_query"]["status"] for entry in by_drug_pair]

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
        },
        "limitations": [
            "This module queries exactly five public APIs (PubMed, ClinVar, DailyMed, CPIC, ClinicalTrials.gov); it does "
            "not cover the rest of the NeuroTwin resource catalog (Reactome, STRING, GTEx, RCSB PDB, etc.) -- those "
            "remain out of scope for this module and must not be inferred from it.",
            "A 'no_results' status means the API returned zero matching records for the exact query issued; it is not "
            "evidence of absence, and must not be reported as a negative finding.",
            "Responses may be served from the on-disk cache in reports/external_evidence/.cache/ rather than a fresh "
            "call; each record's 'from_cache' and 'retrieved_at' fields state which. Pass --refresh to force live calls.",
        ],
    }


_OUT_OF_SCOPE_RESOURCES = [
    "ChEMBL", "IUPHAR/BPS Guide to Pharmacology", "Open Targets", "BindingDB", "DrugBank", "Reactome", "STRING",
    "BioGRID", "Comparative Toxicogenomics Database", "Genomics England PanelApp", "PharmGKB", "NHGRI-EBI GWAS Catalog",
    "Flockhart Table (used internally, not queried live)", "SuperCYPsPred", "Human Protein Atlas Brain Atlas",
    "GTEx Portal", "Allen Brain Atlas", "LINCS/Connectivity Map", "NCBI GEO", "RCSB PDB", "AlphaFold DB",
    "Drugs@FDA", "Tox21", "EPA CompTox", "OHDSI/OMOP", "RxNorm", "LOINC/UCUM", "SNOMED CT/HPO",
]


if __name__ == "__main__":
    raise SystemExit(main())
