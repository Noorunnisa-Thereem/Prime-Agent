"""Section 06 / Section 07 swap: Drug Interactions now renders first (06),
Pharmacogenomics second (07) -- the reverse of the original numbering. These
tests guard the section-number badges, the assembly order, and the
cross-references between the two sections that name each other by number.
"""

from __future__ import annotations

from patient_prime_agent import report_html


def test_drug_interactions_section_is_now_numbered_06():
    html_fragment = report_html._sec_drug_interactions_gap({})

    assert '<span class="section-num">06</span>' in html_fragment
    assert '<span class="section-title">Drug Interactions</span>' in html_fragment


def test_pharmacogenomics_section_is_now_numbered_07():
    sections = {
        report_html.SEC_GENETICS: {
            "patient": {"variants_analyzed": 76, "drugs_covered": 47, "report_date": "2026-09-10"},
            "findings_by_therapeutic_class": {"mood_stabilizers_antiepileptics": []},
        }
    }
    html_fragment = report_html._sec_pharmacogenomics(sections)

    assert '<span class="section-num">07</span>' in html_fragment
    assert '<span class="section-title">Pharmacogenomics</span>' in html_fragment


def test_drug_interactions_renders_before_pharmacogenomics_in_the_assembled_report():
    html = report_html._build_html({}, {}, {})

    drug_interactions_index = html.index('<span class="section-title">Drug Interactions</span>')
    pharmacogenomics_index = html.index('<span class="section-title">Pharmacogenomics</span>')
    assert drug_interactions_index < pharmacogenomics_index


def test_pharmacogenomics_cross_references_drug_interactions_as_section_06():
    sections = {
        report_html.SEC_GENETICS: {
            "patient": {"variants_analyzed": 76, "drugs_covered": 47, "report_date": "2026-09-10"},
            "findings_by_therapeutic_class": {"mood_stabilizers_antiepileptics": []},
        }
    }
    html_fragment = report_html._sec_pharmacogenomics(sections)

    assert "is presented in Section 06, Drug Interactions" in html_fragment
    assert "Section 07, Drug Interactions" not in html_fragment


def test_drug_interactions_gap_cross_references_pharmacogenomics_as_section_07():
    html_fragment = report_html._sec_drug_interactions_gap({})

    assert "different check from Pharmacogenomics (Section 07)" in html_fragment
    assert "Pharmacogenomics (Section 06)" not in html_fragment


def test_action_plan_cross_references_the_ddi_gap_as_section_06():
    html_fragment = report_html._action_plan_reconciliation_item({})

    assert "see Section 06" in html_fragment
    assert "see Section 07" not in html_fragment
