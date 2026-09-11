"""Tests for synthesizing the live external-evidence layer (PubMed, ClinVar,
DailyMed, CPIC, ClinicalTrials.gov -- see external_evidence_summary.py) into
each therapy's supporting/counter/unresolved evidence, per the NeuroTwin
Multimodal Biomedical Resource Guide's "Information available" and "How
green and red evidence should be stated" sections.

Key rules under test:
  - Only a source that actually returned data for this exact drug/pair
    contributes an item -- no fixed column, no empty placeholder for a
    source that has nothing to say (except an explicit "not retrieved"/
    "no match" note, which is itself real information, not a placeholder).
  - ClinVar's own asserted clinical_significance can genuinely support
    (Benign) or counter (Pathogenic/Conflicting); every other source stays
    "unresolved" regardless of whether something was found, because the
    guide says each of them needs further review before becoming a
    patient-specific flag.
  - Every synthesized item is patient_specific=False (population-level
    plausibility, not a patient-verified finding).
"""

from __future__ import annotations

from patient_prime_agent import ddi_summary
from patient_prime_agent.path_d.ddi import aggregation
from patient_prime_agent.path_d.ddi.normalizer import Medication


def _medication(name: str) -> Medication:
    return Medication(
        id=f"med-{name}",
        normalized_name=name,
        source_name=name.title(),
        dose_value=100.0,
        dose_unit="mg",
        route=None,
        frequency="BID",
        status="current",
        source_reference="test",
        reconciliation_flags=[],
    )


# ---------------------------------------------------------------------------
# Per-source synthesis
# ---------------------------------------------------------------------------


def test_pubmed_item_is_always_unresolved_whether_found_or_not():
    found = aggregation._external_pubmed_item(
        {"status": "ok", "records": [{"pmid": "123", "title": "A real title", "url": "https://pubmed.ncbi.nlm.nih.gov/123/"}]}
    )
    assert found["direction"] == "unresolved"
    assert found["patient_specific"] is False
    assert "123" in found["statement"] and "A real title" in found["statement"]

    no_results = aggregation._external_pubmed_item({"status": "no_results", "records": []})
    assert no_results["direction"] == "unresolved"

    error = aggregation._external_pubmed_item({"status": "rate_limited", "error": "HTTP 429", "note": "Not retrieved in this session: rate-limited."})
    assert error["direction"] == "unresolved"
    assert "rate-limited" in error["statement"].lower()

    assert aggregation._external_pubmed_item(None) is None


def test_clinvar_item_direction_follows_its_own_asserted_significance():
    pathogenic = aggregation._external_clinvar_item(
        {"status": "ok", "total_matches_in_clinvar": "12", "records": [{"accession": "VCV1", "clinical_significance": "Pathogenic", "url": "https://x"}]}
    )
    assert pathogenic["direction"] == "counter"

    conflicting = aggregation._external_clinvar_item(
        {"status": "ok", "records": [{"accession": "VCV2", "clinical_significance": "Conflicting interpretations of pathogenicity", "url": "https://x"}]}
    )
    assert conflicting["direction"] == "counter"

    benign = aggregation._external_clinvar_item(
        {"status": "ok", "records": [{"accession": "VCV3", "clinical_significance": "Benign", "url": "https://x"}]}
    )
    assert benign["direction"] == "supporting"

    uncertain = aggregation._external_clinvar_item(
        {"status": "ok", "records": [{"accession": "VCV4", "clinical_significance": "Uncertain significance", "url": "https://x"}]}
    )
    assert uncertain["direction"] == "unresolved"

    no_results = aggregation._external_clinvar_item({"status": "no_results", "records": []})
    assert no_results["direction"] == "unresolved"

    for item in (pathogenic, conflicting, benign, uncertain, no_results):
        assert item["patient_specific"] is False


def test_cpic_item_supports_only_when_an_active_guideline_exists():
    active_guideline = aggregation._external_cpic_item(
        {"status": "ok", "records": [{"cpic_level": "A", "guideline": {"name": "Some Guideline", "url": "https://clinpgx.org/x"}}]}
    )
    assert active_guideline["direction"] == "supporting"
    assert active_guideline["evidence_level"] == "high"

    assessed_no_guideline = aggregation._external_cpic_item(
        {"status": "ok", "records": [{"cpic_level": "C", "guideline": None}]}
    )
    assert assessed_no_guideline["direction"] == "unresolved"

    no_pair = aggregation._external_cpic_item({"status": "no_results", "note": "CPIC has no record for this gene-drug pair."})
    assert no_pair["direction"] == "unresolved"


def test_dailymed_item_is_always_unresolved_since_label_content_is_not_parsed():
    found = aggregation._external_dailymed_item(
        {"status": "ok", "records": [{"title": "SOME LABEL", "published_date": "2026", "url": "https://dailymed.nlm.nih.gov/x"}]}
    )
    assert found["direction"] == "unresolved"
    assert "requires direct review" in found["statement"]

    no_results = aggregation._external_dailymed_item({"status": "no_results", "records": []})
    assert no_results["direction"] == "unresolved"


def test_trials_item_is_unresolved_and_skipped_status_contributes_nothing():
    found = aggregation._external_trials_item(
        {"status": "ok", "records": [{"nct_id": "NCT1", "overall_status": "RECRUITING", "brief_title": "A trial", "url": "https://x"}]}
    )
    assert found["direction"] == "unresolved"

    assert aggregation._external_trials_item({"status": "skipped", "note": "No diagnosis."}) is None
    assert aggregation._external_trials_item(None) is None


# ---------------------------------------------------------------------------
# Synthesis / aggregation
# ---------------------------------------------------------------------------


def test_synthesize_external_evidence_skips_sources_with_nothing_to_contribute():
    external_by_drug = {
        "pubmed": {"status": "ok", "records": [{"pmid": "1", "title": "T", "url": "https://x"}]},
        "dailymed": None,  # not queried / absent entirely
        "clinicaltrials_gov": {"status": "skipped", "note": "no diagnosis"},
    }
    items = aggregation._synthesize_external_evidence(external_by_drug, external_gene_drug_pairs=None)

    # Only PubMed contributed -- dailymed (None) and the skipped trials lookup add nothing,
    # proving this never forces every source into a fixed layout.
    assert len(items) == 1
    assert items[0]["modality"] == "literature"


def test_synthesize_external_evidence_includes_cpic_and_clinvar_per_gene_drug_pair():
    external_gene_drug_pairs = [
        {
            "gene_symbol": "CYP3A4",
            "cpic": {"status": "ok", "records": [{"cpic_level": "A", "guideline": {"name": "G", "url": "https://clinpgx.org/g"}}]},
            "clinvar": {"status": "ok", "records": [{"accession": "VCV1", "clinical_significance": "Pathogenic", "url": "https://x"}]},
        }
    ]
    items = aggregation._synthesize_external_evidence(external_by_drug={}, external_gene_drug_pairs=external_gene_drug_pairs)

    directions = {item["modality"]: item["direction"] for item in items}
    assert directions == {"pharmacogenomic": "supporting", "genomic_evidence": "counter"}


def test_build_therapy_assessment_folds_external_evidence_into_the_real_buckets():
    external_by_drug = {
        "pubmed": {"status": "no_results", "records": []},
        "dailymed": {"status": "ok", "records": [{"title": "Label", "published_date": "2026", "url": "https://dailymed.nlm.nih.gov/x"}]},
        "clinicaltrials_gov": {"status": "skipped", "note": "no diagnosis"},
    }
    external_gene_drug_pairs = [
        {
            "gene_symbol": "SCN1A",
            "cpic": {"status": "no_results", "note": "CPIC has no drug entry."},
            "clinvar": {"status": "ok", "records": [{"accession": "VCV9", "clinical_significance": "Benign", "url": "https://x"}]},
        }
    ]

    assessment = aggregation.build_therapy_assessment(
        medication=_medication("lamotrigine"),
        pgx_evidence=[],
        regimen_wide_evidence=[],
        medication_response={},
        external_by_drug=external_by_drug,
        external_gene_drug_pairs=external_gene_drug_pairs,
    )

    supporting_sources = {e["modality"] for e in assessment["supporting_evidence"]}
    unresolved_sources = {e["modality"] for e in assessment["unresolved_evidence"]}

    assert "genomic_evidence" in supporting_sources  # the Benign ClinVar hit
    assert "literature" in unresolved_sources  # PubMed no_results
    assert "labeling" in unresolved_sources  # DailyMed found, but content unparsed
    assert "pharmacogenomic" in unresolved_sources  # CPIC no_results
    assert "clinical_trial" not in supporting_sources and "clinical_trial" not in unresolved_sources  # skipped -> nothing

    # clinical_impression is derived from the real counts, not hardcoded -- the baseline
    # "no documented toxicity" supporting item plus the Benign ClinVar hit make 2; the
    # unresolved-DDI-coverage note plus PubMed/DailyMed/CPIC make 4.
    assert "2 supporting, 0 counter, and 4 unresolved" in assessment["clinical_impression"]


def test_build_therapy_assessment_with_no_external_evidence_is_unchanged():
    # external_by_drug/external_gene_drug_pairs are optional -- omitting them entirely
    # (as every existing caller/tests did before this feature) must not error or alter
    # the pre-existing evidence shape.
    assessment = aggregation.build_therapy_assessment(
        medication=_medication("levetiracetam"),
        pgx_evidence=[],
        regimen_wide_evidence=[],
        medication_response={},
    )
    assert isinstance(assessment["supporting_evidence"], list)
    assert isinstance(assessment["unresolved_evidence"], list)


# ---------------------------------------------------------------------------
# ddi_summary.py wiring
# ---------------------------------------------------------------------------


def test_index_external_evidence_groups_by_drug_name_case_insensitively():
    external_evidence = {
        "by_drug": [{"drug_name": "Lamotrigine", "pubmed": {"status": "ok", "records": []}}],
        "by_gene_drug_pair": [
            {"gene_symbol": "SCN1A", "drug_name": "lamotrigine", "cpic": {}, "clinvar": {}},
            {"gene_symbol": "CYP3A4", "drug_name": "lamotrigine", "cpic": {}, "clinvar": {}},
        ],
        "by_drug_pair": [
            {"drug_a": "lamotrigine", "drug_b": "Levetiracetam", "pubmed_combined_query": {"status": "ok", "records": []}},
        ],
    }
    by_drug, gene_drug_pairs_by_drug, by_drug_pair = ddi_summary._index_external_evidence(external_evidence)

    assert "lamotrigine" in by_drug
    assert len(gene_drug_pairs_by_drug["lamotrigine"]) == 2
    assert frozenset({"lamotrigine", "levetiracetam"}) in by_drug_pair


def test_index_external_evidence_degrades_to_empty_on_missing_input():
    by_drug, gene_drug_pairs_by_drug, by_drug_pair = ddi_summary._index_external_evidence({})
    assert by_drug == {}
    assert gene_drug_pairs_by_drug == {}
    assert by_drug_pair == {}


def test_build_report_is_unaffected_when_external_evidence_is_omitted(tmp_path):
    # build_report's external_evidence parameter defaults to None -- confirms the DDI
    # screen still works standalone (e.g. external_evidence_summary.py hasn't run yet).
    clinical_notes = {
        "clinical_inference": {
            "medication_response": {"current_regimen": [{"drug": "Lamotrigine", "dose": "150 mg BID"}]}
        }
    }
    report = ddi_summary.build_report(clinical_notes=clinical_notes, genetics={}, eeg={}, ecg={}, cbc={})
    assert report["therapy_assessments"][0]["medication"]["normalized_name"] == "lamotrigine"


# ---------------------------------------------------------------------------
# Pair-level DDI evidence: a genuine "Drug A AND Drug B" combined query, never
# two single-drug lookups merged together after the fact.
# ---------------------------------------------------------------------------


def test_build_pair_assessment_folds_in_a_real_combined_pubmed_query():
    lamotrigine = _medication("lamotrigine")
    levetiracetam = _medication("levetiracetam")
    external_pubmed_pair = {
        "status": "ok",
        "records": [{"pmid": "111", "title": "Lamotrigine and levetiracetam combination therapy in refractory epilepsy.", "url": "https://x"}],
    }

    assessment = aggregation.build_pair_assessment(
        lamotrigine, levetiracetam, "current_current", pgx_evidence_by_drug={}, external_pubmed_pair=external_pubmed_pair
    )

    literature_items = [e for e in assessment["evidence"] if e["modality"] == "literature"]
    assert len(literature_items) == 1
    assert literature_items[0]["direction"] == "unresolved"
    assert literature_items[0]["patient_specific"] is False
    assert "111" in literature_items[0]["statement"]


def test_build_pair_assessment_marks_a_no_results_pair_query_unresolved_never_no_interaction():
    lamotrigine = _medication("lamotrigine")
    levetiracetam = _medication("levetiracetam")
    external_pubmed_pair = {"status": "no_results", "records": []}

    assessment = aggregation.build_pair_assessment(
        lamotrigine, levetiracetam, "current_current", pgx_evidence_by_drug={}, external_pubmed_pair=external_pubmed_pair
    )

    literature_items = [e for e in assessment["evidence"] if e["modality"] == "literature"]
    assert len(literature_items) == 1
    assert literature_items[0]["direction"] == "unresolved"
    # The pair's own curated-evidence status (no PK/PD mechanism in this test's inputs)
    # is computed independently of the literature query -- a missing/empty PubMed result
    # must never be the reason a pair reads as "no interaction."
    assert assessment["status"] == "no_interaction_detected"
    assert "absence of data does not establish" in assessment["patient_specific_interpretation"].lower()


def test_build_pair_assessment_without_external_pubmed_pair_is_unchanged():
    # external_pubmed_pair is optional -- every pre-existing caller/test omitted it, and
    # must keep working exactly as before.
    lamotrigine = _medication("lamotrigine")
    levetiracetam = _medication("levetiracetam")

    assessment = aggregation.build_pair_assessment(lamotrigine, levetiracetam, "current_current", pgx_evidence_by_drug={})

    assert not [e for e in assessment["evidence"] if e["modality"] == "literature"]


def test_ddi_summary_build_report_wires_the_combined_pair_query_into_current_pair_assessments():
    clinical_notes = {
        "clinical_inference": {
            "medication_response": {
                "current_regimen": [
                    {"drug": "Lamotrigine", "dose": "150 mg BID"},
                    {"drug": "Levetiracetam", "dose": "1000 mg BID"},
                ]
            }
        }
    }
    external_evidence = {
        "by_drug_pair": [
            {
                "drug_a": "lamotrigine",
                "drug_b": "levetiracetam",
                "pubmed_combined_query": {
                    "status": "ok",
                    "records": [{"pmid": "222", "title": "A combined-therapy paper.", "url": "https://x"}],
                },
            }
        ]
    }

    report = ddi_summary.build_report(
        clinical_notes=clinical_notes, genetics={}, eeg={}, ecg={}, cbc={}, external_evidence=external_evidence
    )

    assert len(report["current_pair_assessments"]) == 1
    literature_items = [e for e in report["current_pair_assessments"][0]["evidence"] if e["modality"] == "literature"]
    assert len(literature_items) == 1
    assert "222" in literature_items[0]["statement"]
