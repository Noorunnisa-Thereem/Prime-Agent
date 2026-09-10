"""Regression test for the sanitize_response_field / _t() double-escaping fix.

sanitize_response_field() (external_lookup/guardrails.py) used to HTML-escape
its output; report_html.py's _t() escapes again at render time. Together
that double-escaped anything with '&', '<', '>', or a quote -- a PubMed
title containing "&" would show the literal text "&amp;" in the final PDF
instead of "&". sanitize_response_field() no longer escapes (it only
strips/truncates); _t() is now the single place escaping happens.

This is checked at two levels:
  - test_pubmed_cell_escapes_ampersand_exactly_once: fast, always runs, no
    external tools -- calls report_html._pubmed_cell directly and checks
    the HTML string.
  - test_ampersand_title_renders_as_a_single_ampersand_in_the_actual_pdf:
    renders real HTML through the project's own headless-Edge pipeline and
    reads the resulting PDF back with PyMuPDF, so the fix is verified in
    the literal artifact the task asked about. Skipped (not failed) if
    PyMuPDF or Edge is unavailable, since neither is a declared project
    dependency -- see pyproject.toml.
"""

from __future__ import annotations

import pytest

from patient_prime_agent import report_html
from patient_prime_agent.external_lookup.guardrails import sanitize_response_field

_TITLE_WITH_AMPERSAND = "Efficacy & Safety of Levetiracetam in Refractory Epilepsy"


def _pubmed_envelope_with(title: str) -> dict:
    return {
        "status": "ok",
        "records": [
            {
                "pmid": "12345678",
                "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                "title": title,
            }
        ],
    }


def test_sanitize_response_field_leaves_ampersand_for_t_to_escape():
    # The bug required two things to both be true: sanitize escaping AND _t()
    # escaping. This half confirms sanitize_response_field no longer does.
    assert sanitize_response_field(_TITLE_WITH_AMPERSAND) == _TITLE_WITH_AMPERSAND


def test_pubmed_cell_escapes_ampersand_exactly_once():
    sanitized_title = sanitize_response_field(_TITLE_WITH_AMPERSAND)
    html_fragment = report_html._pubmed_cell(_pubmed_envelope_with(sanitized_title))

    assert "&amp;" in html_fragment, "the '&' must be escaped for safe HTML embedding"
    assert "&amp;amp;" not in html_fragment, "the '&' must not be double-escaped"
    assert "Efficacy" in html_fragment and "Safety" in html_fragment


def test_ampersand_title_renders_as_a_single_ampersand_in_the_actual_pdf(tmp_path):
    fitz = pytest.importorskip("fitz", reason="PyMuPDF is not a project dependency; skipping real-PDF verification")
    try:
        report_html._find_edge()
    except RuntimeError:
        pytest.skip("Microsoft Edge was not found; skipping real-PDF verification")

    sanitized_title = sanitize_response_field(_TITLE_WITH_AMPERSAND)
    cell_html = report_html._pubmed_cell(_pubmed_envelope_with(sanitized_title))

    html_path = tmp_path / "ampersand_check.html"
    pdf_path = tmp_path / "ampersand_check.pdf"
    html_path.write_text(f"<!doctype html><html><body>{cell_html}</body></html>", encoding="utf-8")
    report_html._print_html_to_pdf(html_path, pdf_path)

    doc = fitz.open(pdf_path)
    try:
        rendered_text = "".join(page.get_text() for page in doc)
    finally:
        doc.close()

    assert _TITLE_WITH_AMPERSAND in rendered_text, "the PDF must show a real '&', not an HTML entity"
    assert "&amp;" not in rendered_text, "the PDF must never show the literal escaped text '&amp;'"


def test_pubmed_cell_never_truncates_the_title_mid_word():
    long_title = "The Biallelic Inheritance of Two Novel SCN1A Variants Results in Developmental and Epileptic Encephalopathy Responsive to Levetiracetam"
    pubmed = {"status": "ok", "records": [{"pmid": "39200163", "url": "https://pubmed.ncbi.nlm.nih.gov/39200163/", "title": long_title}]}

    html_fragment = report_html._pubmed_cell(pubmed)

    assert long_title in html_fragment, "the full title must be shown, not cut off mid-word"
    assert "Epilept<" not in html_fragment and "Epilept " not in html_fragment


def test_dailymed_and_trials_cells_never_truncate_the_title_mid_word():
    long_title = "LEVETIRACETAM TABLET, FILM COATED, EXTENDED RELEASE [SOME VERY LONG MANUFACTURER NAME THAT USED TO GET CUT OFF]"
    dailymed = {"status": "ok", "records": [{"setid": "abc", "spl_version": 1, "published_date": "2026", "title": long_title, "url": "https://dailymed.nlm.nih.gov/x"}]}
    assert long_title in report_html._dailymed_cell(dailymed)

    long_brief_title = "Prophylactic Anti-Seizure Medication vs No Anti-Seizure Medication for Post-Traumatic Epilepsy Prevention Trial"
    trials = {
        "status": "ok",
        "total_matches_on_clinicaltrials_gov": 1,
        "records": [{"nct_id": "NCT1", "url": "https://clinicaltrials.gov/study/NCT1", "brief_title": long_brief_title, "overall_status": "RECRUITING"}],
    }
    assert long_brief_title in report_html._trials_cell(trials)


def test_clinvar_cell_labels_the_total_as_gene_wide_not_variant_specific():
    clinvar = {
        "status": "ok",
        "total_matches_in_clinvar": "5406",
        "records": [{"accession": "VCV004887952", "uid": "4887952", "url": "https://www.ncbi.nlm.nih.gov/clinvar/variation/4887952/", "clinical_significance": "Uncertain significance"}],
    }
    html_fragment = report_html._clinvar_cell(clinvar)
    assert "gene-wide count, not variant-specific" in html_fragment
    assert "5406" in html_fragment


def _finding(drug: str, genetic_basis: str, citation: str) -> dict:
    return {"drug": drug, "genetic_basis": genetic_basis, "predicted_effect": "reduced efficacy", "citation": citation}


def _pair(gene: str, drug: str, pubmed_pmid: str, pubmed_title: str) -> dict:
    return {
        "gene_symbol": gene,
        "drug_name": drug,
        "cpic": {"status": "no_results", "note": "no pair on record"},
        "clinvar": {"status": "no_results"},
        "pubmed_combined_query": {
            "status": "ok",
            "records": [{"pmid": pubmed_pmid, "url": f"https://pubmed.ncbi.nlm.nih.gov/{pubmed_pmid}/", "title": pubmed_title}],
        },
    }


def test_pgx_card_flags_a_citation_discrepancy_between_patient_report_and_live_pubmed():
    external = {"by_gene_drug_pair": [_pair("CYP3A4", "lamotrigine", "21635243", "Effects of lamotrigine and phenytoin...")]}
    worst_findings = [_finding("lamotrigine", "CYP3A4 (TT)", "PMID:28343093")]  # a different PMID than the live result

    html_fragment = report_html._pgx_live_evidence_card(external, worst_findings)

    assert "independent live PubMed search, may differ" in html_fragment


def test_pgx_card_omits_the_discrepancy_note_when_pmids_match():
    external = {"by_gene_drug_pair": [_pair("CYP3A4", "lamotrigine", "28343093", "Some paper")]}
    worst_findings = [_finding("lamotrigine", "CYP3A4 (TT)", "PMID:28343093")]  # same PMID as the live result

    html_fragment = report_html._pgx_live_evidence_card(external, worst_findings)

    assert "independent live PubMed search, may differ" not in html_fragment
