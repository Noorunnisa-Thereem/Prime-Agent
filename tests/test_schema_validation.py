"""Schema validation gates: every category result and the integrated report."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from patient_prime_agent.config import CATEGORY_ORDER, ProjectPaths
from patient_prime_agent.repair import repair_to_schema
from patient_prime_agent.schema_validator import SchemaValidator


@pytest.fixture
def validator(project: ProjectPaths) -> SchemaValidator:
    return SchemaValidator(project.schemas_root)


# ----------------------------------------------------------------------
# schema availability
# ----------------------------------------------------------------------
def test_every_category_has_a_schema(project: ProjectPaths, validator: SchemaValidator):
    for category in CATEGORY_ORDER:
        assert project.category_schema_path(category).exists()
        assert validator.load(category)["type"] == "object"


def test_the_integrated_schema_exists_and_covers_every_category(validator: SchemaValidator):
    schema = validator.load("digital_twin_report")
    assert set(schema["properties"]["sections"]["properties"]) == set(CATEGORY_ORDER)


def test_a_missing_schema_raises_rather_than_silently_passing(validator: SchemaValidator):
    with pytest.raises(FileNotFoundError):
        validator.load("does_not_exist")


# ----------------------------------------------------------------------
# defaults
# ----------------------------------------------------------------------
def test_schema_defaults_validate_and_use_null_for_unknown_scalars(validator: SchemaValidator):
    for category in CATEGORY_ORDER:
        default = validator.default(category)
        assert validator.validate(default, category) == []
        for key, value in default.items():
            assert value is None or isinstance(value, (list, dict, str, int, float, bool))


def test_default_lists_are_empty_not_fabricated(validator: SchemaValidator):
    default = validator.default("cbc")
    assert default["abnormal_flags"] == []
    assert default["hemoglobin_g_per_dL"] is None


# ----------------------------------------------------------------------
# rejection
# ----------------------------------------------------------------------
def test_a_wrong_scalar_type_is_rejected(validator: SchemaValidator):
    section = validator.default("cbc")
    section["hemoglobin_g_per_dL"] = "thirteen point four"
    issues = validator.validate(section, "cbc")
    assert issues
    assert issues[0].path == "$.hemoglobin_g_per_dL"
    assert "Expected type" in issues[0].message


def test_a_missing_required_property_is_reported(validator: SchemaValidator):
    section = validator.default("cbc")
    del section["platelets_10e3_uL"]
    issues = validator.validate(section, "cbc")
    assert any(issue.path == "$.platelets_10e3_uL" and "Missing required" in issue.message for issue in issues)


def test_an_unexpected_property_is_reported(validator: SchemaValidator):
    section = validator.default("cbc")
    section["invented_metric"] = 42
    issues = validator.validate(section, "cbc")
    assert any("additional property" in issue.message for issue in issues)


def test_a_malformed_date_is_reported(validator: SchemaValidator):
    section = validator.default("cbc")
    section["collection_date"] = "March 4th, 2026"
    issues = validator.validate(section, "cbc")
    assert any("ISO date" in issue.message for issue in issues)


def test_every_issue_carries_a_stable_key_for_refinement(validator: SchemaValidator):
    section = validator.default("mri")
    section["study_date"] = 12345
    issues = validator.validate(section, "mri")
    assert issues
    assert all(issue.issue_key for issue in issues)
    assert issues[0].issue_key == validator.validate(deepcopy(section), "mri")[0].issue_key


# ----------------------------------------------------------------------
# repair
# ----------------------------------------------------------------------
def test_repair_coerces_a_numeric_string_without_inventing_a_value(validator: SchemaValidator):
    section = validator.default("cbc")
    section["hemoglobin_g_per_dL"] = "13.4"
    repaired = repair_to_schema(section, "cbc", validator)
    assert repaired["hemoglobin_g_per_dL"] == 13.4
    assert validator.validate(repaired, "cbc") == []


def test_repair_drops_unrepresentable_values_back_to_null(validator: SchemaValidator):
    section = validator.default("cbc")
    section["hemoglobin_g_per_dL"] = {"value": "unknown"}
    repaired = repair_to_schema(section, "cbc", validator)
    assert repaired["hemoglobin_g_per_dL"] is None
    assert validator.validate(repaired, "cbc") == []


def test_repair_removes_properties_the_schema_does_not_allow(validator: SchemaValidator):
    section = validator.default("cbc")
    section["invented_metric"] = 42
    repaired = repair_to_schema(section, "cbc", validator)
    assert "invented_metric" not in repaired
    assert validator.validate(repaired, "cbc") == []


def test_repair_is_idempotent(validator: SchemaValidator):
    section = validator.default("questionnaire")
    section["severity"] = 7
    once = repair_to_schema(section, "questionnaire", validator)
    assert repair_to_schema(once, "questionnaire", validator) == once


# ----------------------------------------------------------------------
# integrated report
# ----------------------------------------------------------------------
def test_the_integrated_default_only_lacks_its_timestamps(validator: SchemaValidator):
    # The skeleton uses "" for date-time fields; the report builder fills them.
    issues = validator.validate(validator.default("digital_twin_report"), "digital_twin_report")
    assert {issue.path for issue in issues} == {"$.generated_at", "$.validation.checked_at"}
    assert all("date-time" in issue.message for issue in issues)


def test_the_integrated_skeleton_validates_once_timestamps_are_filled(validator: SchemaValidator):
    from patient_prime_agent.utils import utc_now_iso

    report = validator.default("digital_twin_report")
    report["generated_at"] = utc_now_iso()
    report["validation"]["checked_at"] = utc_now_iso()
    assert validator.validate(report, "digital_twin_report") == []


def test_the_integrated_report_rejects_extra_top_level_keys(validator: SchemaValidator):
    report = validator.default("digital_twin_report")
    report["agent_metadata"] = {"session": "x"}
    assert validator.validate(report, "digital_twin_report")


def test_a_generated_report_on_disk_still_validates(project: ProjectPaths, validator: SchemaValidator, settings):
    from patient_prime_agent.agentic.main_agent import run_agentic_pipeline

    outcome = run_agentic_pipeline(paths=project, settings=settings)
    written = json.loads(Path(outcome.report_path).read_text(encoding="utf-8"))
    assert validator.validate(written, "digital_twin_report") == []
