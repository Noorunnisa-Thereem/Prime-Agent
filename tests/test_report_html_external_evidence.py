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


def test_pubmed_cell_does_not_repeat_the_pmid_between_its_link_and_provenance_line():
    # Bug: the main line linked "PMID 12345678" as its own text, and the provenance
    # line right below it printed "PMID 12345678" again -- the same fact stated twice.
    # (The PMID legitimately appears twice even after the fix -- once in the href
    # URL, once as the visible link text -- so this checks the provenance <div>
    # specifically, not a raw substring count.)
    html_fragment = report_html._pubmed_cell(_pubmed_envelope_with("Some title"))
    link_html, provenance_html = html_fragment.split('<div class="small muted"', 1)

    assert "12345678" in link_html
    assert "12345678" not in provenance_html, "the PMID must not be repeated in the provenance line"
    assert "Retrieved" in provenance_html, "the retrieval date must still be shown"


def test_clinvar_cell_does_not_repeat_the_accession_between_its_link_and_provenance_line():
    clinvar = {
        "status": "ok",
        "total_matches_in_clinvar": "5406",
        "records": [{"accession": "VCV004887952", "uid": "4887952", "url": "https://www.ncbi.nlm.nih.gov/clinvar/variation/4887952/", "clinical_significance": "Uncertain significance"}],
    }
    html_fragment = report_html._clinvar_cell(clinvar)

    assert html_fragment.count("VCV004887952") == 1, "the accession must be shown once, not once as link text and again in the provenance line"
    assert "Retrieved" in html_fragment


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


# ---------------------------------------------------------------------------
# Section 06 redesign: Table 1 "Patient-Specific PGx Findings" (Therapy /
# Gene-Genotype / Patient-Specific Interpretation / Clinical Relevance) and
# Table 2 "External Evidence Verification" (Gene-Drug Pair / CPIC / ClinVar /
# PubMed / Overall Evidence Status) -- plain uniform styling, no colored
# badges/borders on either table, detailed provenance moved below Table 2.
# ---------------------------------------------------------------------------


def test_table1_has_the_four_requested_columns_and_no_colored_badges():
    sections = {
        report_html.SEC_GENETICS: {
            "patient": {"variants_analyzed": 76, "drugs_covered": 47, "report_date": "2026-09-10"},
            "findings_by_therapeutic_class": {
                "mood_stabilizers_antiepileptics": [
                    {"drug": "Lamotrigine", "genetic_basis": "CYP3A4 (TT)", "predicted_effect": "reduced efficacy", "significant": True, "citation": "PMID:28343093"},
                ]
            },
        }
    }
    html_fragment = report_html._sec_pharmacogenomics(sections)

    assert "Patient-Specific PGx Findings" in html_fragment
    for header in ("<th>Therapy</th>", "<th>Gene/Genotype</th>", "<th>Patient-Specific Interpretation</th>", "<th>Clinical Relevance</th>"):
        assert header in html_fragment

    table1_html = html_fragment.split("Patient-Specific PGx Findings", 1)[1].split("</table>", 1)[0]
    assert "Lamotrigine" in table1_html and "CYP3A4 (TT)" in table1_html
    assert "reduced-efficacy response" in table1_html  # plain-language sentence, not the raw enum
    assert "reduced efficacy</span>" not in table1_html, "the raw enum must not be shown as a colored badge"
    assert 'class="badge' not in table1_html, "Table 1 must use plain text, no colored badges"
    assert "PMID:28343093" in table1_html  # citation retained, just moved into the Clinical Relevance cell


def test_table1_interpretation_and_relevance_columns_state_different_facts():
    # Regression guard: these two columns must not just repeat each other -- one says
    # what the genotype means, the other says how much weight/significance it carries.
    entry = {"drug": "Lamotrigine", "genetic_basis": "CYP3A4 (TT)", "predicted_effect": "toxicity", "significant": False, "citation": "PMID:1"}

    interpretation = report_html._pgx_interpretation_sentence(entry)
    relevance = report_html._pgx_clinical_relevance(entry)

    assert interpretation != relevance
    assert "toxicity" in interpretation.lower()
    assert "not flagged as clinically significant" in relevance


def test_table2_is_now_separate_flagged_blocks_per_gene_drug_pair_not_a_shared_table():
    # Per request: External Evidence Verification is no longer one shared table across every
    # gene-drug pair -- each pair gets its own flagged block (VERIFIED/PARTIALLY VERIFIED/
    # UNVERIFIED label + 2 short bullets), the same visual pattern as the curated
    # Green-Flags/Red-Flags sheet.
    external = {"by_gene_drug_pair": [_pair("CYP3A4", "lamotrigine", "21635243", "Effects of lamotrigine and phenytoin...")]}
    worst_findings = [_finding("lamotrigine", "CYP3A4 (TT)", "PMID:28343093")]

    html_fragment = report_html._pgx_live_evidence_card(external, worst_findings)

    assert "External Evidence Verification" in html_fragment
    assert "<table" not in html_fragment, "Table 2 must no longer render as a shared <table>"
    blocks_html, rest = html_fragment.split('<div class="callout" style="margin-top:8px"><b>Provenance</b>', 1)

    assert "CYP3A4 &rarr; Lamotrigine" in blocks_html
    # _pair()'s fixture: CPIC and ClinVar are no_results, PubMed is ok -- 1 of 3 resolves.
    assert 'class="sev-mod">PARTIALLY VERIFIED' in blocks_html, "the flag must reflect the real per-source resolution count, plain text color only"
    assert "CPIC: Unresolved. ClinVar: Unresolved." in blocks_html
    assert "PubMed: PMID 21635243 found. Overall: Partially resolved (1 of 3 sources)." in blocks_html
    assert 'class="badge' not in blocks_html, "the flag must be plain colored text, no colored badge box"

    # Detailed provenance (retrieval date) must be below the blocks, not inline in one.
    assert "Retrieved" not in blocks_html
    assert "Retrieved" in rest
    assert "PMID 21635243" in rest


def test_table2_overall_status_reflects_full_resolution_when_all_three_sources_resolve():
    external = {
        "by_gene_drug_pair": [
            {
                "gene_symbol": "CYP3A4",
                "drug_name": "lamotrigine",
                "cpic": {"status": "ok", "records": [{"drugid": "RxNorm:1", "cpic_level": "C", "guideline": None}], "retrieved_at": "2026-09-11T00:00:00Z"},
                "clinvar": {"status": "ok", "total_matches_in_clinvar": "10", "records": [{"accession": "VCV1", "clinical_significance": "Uncertain significance"}], "retrieved_at": "2026-09-11T00:00:00Z"},
                "pubmed_combined_query": {"status": "ok", "records": [{"pmid": "1", "url": "https://pubmed.ncbi.nlm.nih.gov/1/", "title": "A paper"}], "retrieved_at": "2026-09-11T00:00:00Z"},
            }
        ]
    }
    worst_findings = [_finding("lamotrigine", "CYP3A4 (TT)", "PMID:1")]

    html_fragment = report_html._pgx_live_evidence_card(external, worst_findings)

    assert "Resolved across all 3 sources" in html_fragment


def test_provenance_line_is_compact_no_resource_name_no_cache_badge_no_placeholder_version():
    # Per user feedback: the resource name just repeats the column header, the LIVE/CACHED
    # badge added clutter without changing what a reader does with the finding, and a
    # "Version —" placeholder for sources with no real version is noise, not information.
    envelope_without_version = {"resource": "PubMed (NCBI E-utils)", "retrieved_at": "2026-09-10T00:00:00Z", "from_cache": True, "verification": "cached"}
    line = report_html._provenance_line(envelope_without_version, id_label="PMID", record_id="12345")

    assert "PMID 12345" in line and "Retrieved 10 Sep 2026" in line
    assert "NCBI E-utils" not in line
    assert "CACHED" not in line and "LIVE" not in line
    assert "Version" not in line

    envelope_with_version = {"resource": "DailyMed (NLM REST API v2)", "retrieved_at": "2026-09-10T00:00:00Z", "from_cache": False}
    line_with_version = report_html._provenance_line(envelope_with_version, id_label="SetID", record_id="abc-123", version=20)

    assert "v20" in line_with_version
    assert "NLM REST API" not in line_with_version


def test_pgx_summary_chips_render_every_real_gene_from_the_metabolizer_profile():
    genetics = {
        "metabolizer_profile": [
            {"gene": "CYP2D6", "status": "GG", "impact": "Methylphenidate - reduced efficacy signal"},
            {"gene": "CYP3A4", "status": "TT", "impact": "Lamotrigine - reduced efficacy signal"},
        ]
    }
    html_fragment = report_html._pgx_summary_chips(genetics)

    assert html_fragment.count('class="chip"') == 2
    assert "CYP2D6" in html_fragment and "GG" in html_fragment
    assert "CYP3A4" in html_fragment and "TT" in html_fragment


def test_pgx_summary_chips_degrade_to_nothing_without_inventing_a_profile():
    assert report_html._pgx_summary_chips({}) == ""
    assert report_html._pgx_summary_chips({"metabolizer_profile": []}) == ""


def test_sec_pharmacogenomics_explains_interpretive_method_and_points_to_section_06():
    sections = {
        report_html.SEC_GENETICS: {
            "patient": {"variants_analyzed": 76, "drugs_covered": 47, "report_date": "2026-09-10"},
            "metabolizer_profile": [{"gene": "CYP3A4", "status": "TT", "impact": "Lamotrigine - reduced efficacy signal"}],
            "findings_by_therapeutic_class": {"mood_stabilizers_antiepileptics": []},
        }
    }
    html_fragment = report_html._sec_pharmacogenomics(sections)

    assert "Pharmacogenomic Panel Summary" in html_fragment
    assert "Interpretive method" in html_fragment
    assert "never used as an isolated prescribing instruction" in html_fragment
    assert "Section 06" in html_fragment
    assert "CYP3A4" in html_fragment
