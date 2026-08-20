"""Continual Harness: Prompt + Skills + Memory + Sub-agent config, CRUD, rollback."""

from __future__ import annotations

from pathlib import Path

import pytest

from patient_prime_agent.agentic.harness import (
    COMPONENTS,
    COMPONENT_MEMORY,
    COMPONENT_PROMPT,
    COMPONENT_SKILLS,
    COMPONENT_SUBAGENTS,
    ContinualHarness,
    SubAgentConfig,
)
from patient_prime_agent.config import CATEGORY_ORDER, ProjectPaths


@pytest.fixture
def harness(project: ProjectPaths) -> ContinualHarness:
    return ContinualHarness(project.memory_root / "agentic" / "harness", paths=project)


# ----------------------------------------------------------------------
# construction
# ----------------------------------------------------------------------
def test_harness_has_all_four_components(harness: ContinualHarness):
    assert set(harness.list_components()) == set(COMPONENTS)
    assert set(COMPONENTS) == {COMPONENT_PROMPT, COMPONENT_SKILLS, COMPONENT_MEMORY, COMPONENT_SUBAGENTS}


def test_harness_registers_a_subagent_for_every_category(harness: ContinualHarness):
    assert [config.category for config in harness.list_subagents()] == list(CATEGORY_ORDER)


def test_harness_persists_itself_on_first_build(project: ProjectPaths, harness: ContinualHarness):
    assert harness.state_path.exists()
    reopened = ContinualHarness(project.memory_root / "agentic" / "harness", paths=project)
    assert reopened.system_prompt == harness.system_prompt
    assert len(reopened.list_subagents()) == len(harness.list_subagents())


def test_skill_entries_point_at_real_skill_files(project: ProjectPaths, harness: ContinualHarness):
    for category in CATEGORY_ORDER:
        entry = harness.get_skill(category)
        assert entry is not None
        assert Path(entry["path"]).exists()


# ----------------------------------------------------------------------
# component CRUD
# ----------------------------------------------------------------------
def test_create_read_update_delete_a_component(harness: ContinualHarness):
    created = harness.create_component("tools", {"allowed": ["schema_validator"]})
    assert created.version == 1
    assert harness.read_component("tools")["allowed"] == ["schema_validator"]

    updated = harness.update_component("tools", {"allowed": ["schema_validator", "report_builder"]})
    assert updated.version == 2

    assert harness.delete_component("tools") is True
    assert harness.delete_component("tools") is False
    with pytest.raises(KeyError):
        harness.read_component("tools")


def test_creating_a_duplicate_component_is_rejected(harness: ContinualHarness):
    with pytest.raises(KeyError):
        harness.create_component(COMPONENT_PROMPT, {})


def test_update_can_merge_or_replace(harness: ContinualHarness):
    harness.update_component(COMPONENT_MEMORY, {"retain_sessions": 5})
    assert harness.memory_policy()["retain_sessions"] == 5
    assert "scopes" in harness.memory_policy()  # merged, not replaced

    harness.update_component(COMPONENT_MEMORY, {"retain_sessions": 9}, merge=False)
    assert harness.memory_policy() == {"retain_sessions": 9}


# ----------------------------------------------------------------------
# prompt component
# ----------------------------------------------------------------------
def test_prompt_policies_can_be_added_and_removed(harness: ContinualHarness):
    policy = "Reject any section whose evidence lacks a source file."
    harness.add_policy(policy)
    assert policy in harness.policies()
    harness.add_policy(policy)  # idempotent
    assert harness.policies().count(policy) == 1
    harness.remove_policy(policy)
    assert policy not in harness.policies()


def test_rendered_prompt_contains_objective_policies_and_workflow(harness: ContinualHarness):
    rendered = harness.render_prompt(extra=["session note"])
    assert "Objective:" in rendered
    assert "Plan -> Delegate -> Execute -> Validate -> Fix/Retry -> Verify -> Integrate" in rendered
    assert "never invent a patient value" in rendered
    assert "session note" in rendered


# ----------------------------------------------------------------------
# skills component
# ----------------------------------------------------------------------
def test_skill_can_be_upserted_and_deleted(harness: ContinualHarness):
    harness.upsert_skill("custom", "/tmp/custom/SKILL.md")
    assert harness.get_skill("custom")["path"] == "/tmp/custom/SKILL.md"
    assert harness.delete_skill("custom") is True
    assert harness.delete_skill("custom") is False


def test_skill_refinements_are_recorded_with_timestamps(harness: ContinualHarness):
    harness.record_skill_refinement("cbc", "Keep nulls for absent metrics.")
    refinements = harness.get_skill("cbc")["refinements"]
    assert len(refinements) == 1
    assert refinements[0]["note"] == "Keep nulls for absent metrics."
    assert refinements[0]["at"]


# ----------------------------------------------------------------------
# sub-agent component
# ----------------------------------------------------------------------
def test_subagent_crud_round_trip(harness: ContinualHarness):
    config = SubAgentConfig(
        category="pathology",
        agent_id="agent-pathology",
        label="Pathology",
        skill_path="/tmp/pathology/SKILL.md",
        schema_name="pathology",
        instruction="Extract pathology results only when explicitly stated.",
    )
    harness.create_subagent(config)
    assert harness.get_subagent("pathology") is not None
    assert config.memory_scope == "agent-pathology"

    updated = harness.update_subagent("pathology", max_retries=5, enabled=False)
    assert updated is not None and updated.max_retries == 5
    assert "pathology" not in [item.category for item in harness.list_subagents(enabled_only=True)]

    assert harness.delete_subagent("pathology") is True
    assert harness.get_subagent("pathology") is None
    assert harness.delete_subagent("pathology") is False


def test_creating_a_duplicate_subagent_is_rejected(harness: ContinualHarness):
    existing = harness.get_subagent("cbc")
    assert existing is not None
    with pytest.raises(KeyError):
        harness.create_subagent(existing)


def test_updating_an_unknown_subagent_returns_none(harness: ContinualHarness):
    assert harness.update_subagent("nope", enabled=False) is None


def test_disabling_a_subagent_removes_it_from_the_enabled_list(harness: ContinualHarness):
    harness.update_subagent("eeg", enabled=False)
    enabled = [config.category for config in harness.list_subagents(enabled_only=True)]
    assert "eeg" not in enabled
    assert "eeg" in [config.category for config in harness.list_subagents()]


def test_default_instruction_names_the_skill_and_the_schema(harness: ContinualHarness):
    config = harness.get_subagent("mri")
    assert config is not None
    assert "SKILL.md" in config.instruction
    assert "mri.schema.json" in config.instruction
    assert "null" in config.instruction


# ----------------------------------------------------------------------
# revisions and rollback
# ----------------------------------------------------------------------
def test_commit_creates_a_numbered_revision(harness: ContinualHarness):
    snapshot = harness.commit("baseline")
    assert snapshot["revision_id"] == "rev-0001"
    assert harness.list_revisions()[0]["reason"] == "baseline"
    assert harness.read_revision("rev-0001")["state"]["components"]


def test_rollback_restores_a_previous_state(harness: ContinualHarness):
    original = harness.system_prompt
    baseline = harness.commit("baseline")["revision_id"]

    harness.update_component(COMPONENT_PROMPT, {"system": "A bad refinement."})
    harness.update_subagent("cbc", instruction="Broken instruction.")
    assert harness.system_prompt == "A bad refinement."

    assert harness.rollback(baseline) is True
    assert harness.system_prompt == original
    assert harness.get_subagent("cbc").instruction != "Broken instruction."


def test_rollback_snapshots_the_current_state_first_so_it_can_be_undone(harness: ContinualHarness):
    baseline = harness.commit("baseline")["revision_id"]
    harness.update_component(COMPONENT_PROMPT, {"system": "changed"})
    harness.rollback(baseline)

    # The pre-rollback state was itself committed, so we can go forward again.
    pre_rollback = [rev for rev in harness.list_revisions() if "pre-rollback" in rev["reason"]]
    assert pre_rollback
    assert harness.rollback(pre_rollback[-1]["revision_id"]) is True
    assert harness.system_prompt == "changed"


def test_rollback_to_an_unknown_revision_is_a_no_op(harness: ContinualHarness):
    before = harness.system_prompt
    assert harness.rollback("rev-9999") is False
    assert harness.system_prompt == before


def test_rollback_survives_a_reload_from_disk(project: ProjectPaths, harness: ContinualHarness):
    baseline = harness.commit("baseline")["revision_id"]
    harness.update_component(COMPONENT_PROMPT, {"system": "changed"})
    harness.rollback(baseline)

    reopened = ContinualHarness(project.memory_root / "agentic" / "harness", paths=project)
    assert reopened.system_prompt != "changed"


def test_summary_reports_versions_subagents_and_revision_count(harness: ContinualHarness):
    harness.commit("baseline")
    summary = harness.summary()
    assert summary["revisions"] == 1
    assert summary["components"][COMPONENT_PROMPT] >= 1
    assert len(summary["subagents"]) == len(CATEGORY_ORDER)
