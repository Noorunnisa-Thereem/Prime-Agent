"""Agent memory CRUD and the targeted refinement engine with rollback."""

from __future__ import annotations

from pathlib import Path

import pytest

from patient_prime_agent.agentic.harness import ContinualHarness
from patient_prime_agent.agentic.memory import (
    KIND_EPISODIC,
    KIND_PROCEDURAL,
    KIND_SEMANTIC,
    AgentMemory,
)
from patient_prime_agent.agentic.refine import (
    TARGET_AGENT_INSTRUCTION,
    TARGET_MEMORY,
    TARGET_SKILL,
    RefinementEngine,
    classify_target,
)
from patient_prime_agent.config import ProjectPaths
from patient_prime_agent.memory_store import MemoryStore
from patient_prime_agent.models import ValidationIssue
from patient_prime_agent.skill_store import SkillRegistry


@pytest.fixture
def memory(tmp_path: Path) -> AgentMemory:
    return AgentMemory(tmp_path / "agent_memory")


@pytest.fixture
def engine(project: ProjectPaths) -> RefinementEngine:
    agentic_root = project.memory_root / "agentic"
    return RefinementEngine(
        memory_store=MemoryStore(project.memory_root),
        agent_memory=AgentMemory(agentic_root / "agent_memory"),
        skills=SkillRegistry(project.skills_root),
        harness=ContinualHarness(agentic_root / "harness", paths=project),
        root=agentic_root / "refinements",
        threshold=2,
    )


def schema_issue(category: str = "cbc", path: str = "$.hemoglobin_g_per_dL") -> ValidationIssue:
    return ValidationIssue(
        schema_name=category,
        path=path,
        message="Expected type ['number', 'null'], got str",
        category=category,
        issue_key=f"{category}:{path}:type-mismatch",
    )


def loader_issue(category: str = "cbc") -> ValidationIssue:
    return ValidationIssue(
        schema_name=category,
        path="$.files[report.pdf]",
        message="pdf_parse_failed: no extractable text",
        category=category,
        issue_key=f"{category}:file-note:pdf-parse-failed",
    )


def generic_issue(category: str = "cbc") -> ValidationIssue:
    return ValidationIssue(
        schema_name=category,
        path="$",
        message="Section merged from conflicting collection dates",
        category=category,
        issue_key=f"{category}:merge-conflict",
    )


# ----------------------------------------------------------------------
# memory CRUD
# ----------------------------------------------------------------------
def test_write_read_update_delete(memory: AgentMemory):
    record = memory.write("rule::units", "CBC hemoglobin is g/dL.", scope="agent-cbc")
    assert record.version == 1 and record.kind == KIND_SEMANTIC

    read_back = memory.read("rule::units", scope="agent-cbc")
    assert read_back is not None and read_back.hits == 1

    updated = memory.update("rule::units", scope="agent-cbc", content="CBC hemoglobin is g/dL only.")
    assert updated is not None and updated.version == 2

    assert memory.delete("rule::units", scope="agent-cbc") is True
    assert memory.delete("rule::units", scope="agent-cbc") is False
    assert memory.read("rule::units", scope="agent-cbc") is None


def test_rewriting_identical_content_does_not_bump_the_version(memory: AgentMemory):
    memory.write("k", "same", scope="s")
    assert memory.write("k", "same", scope="s").version == 1
    assert memory.write("k", "different", scope="s").version == 2


def test_records_are_isolated_per_scope(memory: AgentMemory):
    memory.write("k", "cbc value", scope="agent-cbc")
    memory.write("k", "ct value", scope="agent-ct")
    assert memory.read("k", scope="agent-cbc").content == "cbc value"
    assert memory.read("k", scope="agent-ct").content == "ct value"
    assert set(memory.scopes()) == {"agent-cbc", "agent-ct"}


def test_records_survive_a_new_store_over_the_same_directory(tmp_path: Path):
    AgentMemory(tmp_path / "m").write("k", "durable", scope="agent-cbc")
    assert AgentMemory(tmp_path / "m").read("k", scope="agent-cbc").content == "durable"


def test_listing_filters_by_kind_and_tag(memory: AgentMemory):
    memory.write("a", "fact", scope="s", kind=KIND_SEMANTIC, tags=["units"])
    memory.write("b", "what happened", scope="s", kind=KIND_EPISODIC)
    memory.write("c", "do this next time", scope="s", kind=KIND_PROCEDURAL, tags=["units"])

    assert [r.key for r in memory.list_records("s", kind=KIND_PROCEDURAL)] == ["c"]
    assert {r.key for r in memory.list_records("s", tag="units")} == {"a", "c"}


def test_directives_only_return_procedural_records(memory: AgentMemory):
    memory.write("a", "fact", scope="agent-cbc", kind=KIND_SEMANTIC)
    memory.remember_issue("agent-cbc", "cbc:$.x:type-mismatch", "Use null when absent.", 2)
    assert memory.directives("agent-cbc") == ["Use null when absent."]


# ----------------------------------------------------------------------
# target classification
# ----------------------------------------------------------------------
def test_schema_shape_issues_target_the_skill_file():
    assert classify_target(schema_issue()) == TARGET_SKILL


def test_loader_issues_target_the_agent_instruction():
    assert classify_target(loader_issue()) == TARGET_AGENT_INSTRUCTION


def test_unclassified_issues_target_memory():
    assert classify_target(generic_issue()) == TARGET_MEMORY


# ----------------------------------------------------------------------
# refinement lifecycle
# ----------------------------------------------------------------------
def test_a_single_observation_does_not_trigger_refinement(engine: RefinementEngine):
    assert engine.consider("cbc", [schema_issue()]) == []
    assert engine.history() == []


def test_a_repeated_issue_becomes_a_reusable_refinement(engine: RefinementEngine):
    engine.consider("cbc", [schema_issue()])
    applied = engine.consider("cbc", [schema_issue()])

    assert len(applied) == 1
    assert applied[0].observed_count == 2
    assert applied[0].target == TARGET_SKILL


def test_refinement_updates_only_the_skill_file_for_shape_issues(project: ProjectPaths, engine: RefinementEngine):
    skill_path = project.skills_root / "cbc" / "SKILL.md"
    other_skill = (project.skills_root / "ct" / "SKILL.md").read_text(encoding="utf-8")
    instruction_before = engine.harness.get_subagent("cbc").instruction

    engine.consider("cbc", [schema_issue()])
    engine.consider("cbc", [schema_issue()])

    assert "Learned Refinements" in skill_path.read_text(encoding="utf-8")
    assert (project.skills_root / "ct" / "SKILL.md").read_text(encoding="utf-8") == other_skill
    assert engine.harness.get_subagent("cbc").instruction == instruction_before


def test_refinement_updates_only_the_agent_instruction_for_loader_issues(project: ProjectPaths, engine: RefinementEngine):
    skill_before = (project.skills_root / "cbc" / "SKILL.md").read_text(encoding="utf-8")

    engine.consider("cbc", [loader_issue()])
    applied = engine.consider("cbc", [loader_issue()])

    assert applied[0].target == TARGET_AGENT_INSTRUCTION
    assert "Learned rule:" in engine.harness.get_subagent("cbc").instruction
    assert (project.skills_root / "cbc" / "SKILL.md").read_text(encoding="utf-8") == skill_before


def test_refinement_updates_only_memory_for_other_issues(project: ProjectPaths, engine: RefinementEngine):
    skill_before = (project.skills_root / "cbc" / "SKILL.md").read_text(encoding="utf-8")

    engine.consider("cbc", [generic_issue()])
    applied = engine.consider("cbc", [generic_issue()])

    assert applied[0].target == TARGET_MEMORY
    assert engine.agent_memory.directives("agent-cbc")
    assert (project.skills_root / "cbc" / "SKILL.md").read_text(encoding="utf-8") == skill_before


def test_the_same_issue_is_not_refined_twice_in_a_row(engine: RefinementEngine):
    engine.consider("cbc", [schema_issue()])
    assert len(engine.consider("cbc", [schema_issue()])) == 1
    assert engine.consider("cbc", [schema_issue()]) == []
    assert len(engine.history()) == 1


def test_an_issue_can_be_refined_again_after_its_refinement_is_rolled_back(engine: RefinementEngine):
    engine.consider("cbc", [schema_issue()])
    applied = engine.consider("cbc", [schema_issue()])
    engine.rollback(applied[0].refinement_id)

    reapplied = engine.consider("cbc", [schema_issue()])
    assert len(reapplied) == 1
    assert reapplied[0].refinement_id != applied[0].refinement_id


def test_refinement_history_records_before_and_after(engine: RefinementEngine):
    engine.consider("cbc", [schema_issue()])
    engine.consider("cbc", [schema_issue()])

    record = engine.history(category="cbc")[0]
    assert record.before != record.after
    assert record.note
    assert record.rolled_back is False


def test_each_refinement_commits_a_harness_revision(engine: RefinementEngine):
    before = len(engine.harness.list_revisions())
    engine.consider("cbc", [schema_issue()])
    engine.consider("cbc", [schema_issue()])
    assert len(engine.harness.list_revisions()) > before


def test_a_lesson_is_written_to_the_shared_memory_store(project: ProjectPaths, engine: RefinementEngine):
    engine.consider("cbc", [schema_issue()])
    engine.consider("cbc", [schema_issue()])
    lessons = (project.memory_root / "lessons.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lessons) == 1
    assert "cbc" in lessons[0]


# ----------------------------------------------------------------------
# rollback
# ----------------------------------------------------------------------
def test_rolling_back_a_skill_refinement_restores_the_file(project: ProjectPaths, engine: RefinementEngine):
    skill_path = project.skills_root / "cbc" / "SKILL.md"
    original = skill_path.read_text(encoding="utf-8")

    engine.consider("cbc", [schema_issue()])
    applied = engine.consider("cbc", [schema_issue()])
    assert skill_path.read_text(encoding="utf-8") != original

    assert engine.rollback(applied[0].refinement_id) is True
    assert skill_path.read_text(encoding="utf-8") == original


def test_rolling_back_an_instruction_refinement_restores_the_instruction(engine: RefinementEngine):
    original = engine.harness.get_subagent("cbc").instruction

    engine.consider("cbc", [loader_issue()])
    applied = engine.consider("cbc", [loader_issue()])

    assert engine.rollback(applied[0].refinement_id) is True
    assert engine.harness.get_subagent("cbc").instruction == original


def test_rolling_back_a_memory_refinement_removes_the_record(engine: RefinementEngine):
    engine.consider("cbc", [generic_issue()])
    applied = engine.consider("cbc", [generic_issue()])
    assert engine.agent_memory.directives("agent-cbc")

    assert engine.rollback(applied[0].refinement_id) is True
    assert engine.agent_memory.directives("agent-cbc") == []


def test_a_refinement_cannot_be_rolled_back_twice(engine: RefinementEngine):
    engine.consider("cbc", [schema_issue()])
    applied = engine.consider("cbc", [schema_issue()])
    assert engine.rollback(applied[0].refinement_id) is True
    assert engine.rollback(applied[0].refinement_id) is False


def test_rollback_marks_the_history_entry_and_persists_it(engine: RefinementEngine):
    engine.consider("cbc", [schema_issue()])
    applied = engine.consider("cbc", [schema_issue()])
    engine.rollback(applied[0].refinement_id)

    record = engine.history()[0]
    assert record.rolled_back is True
    assert record.rolled_back_at is not None


def test_rollback_last_undoes_the_most_recent_refinement(engine: RefinementEngine):
    engine.consider("cbc", [schema_issue()])
    engine.consider("cbc", [schema_issue()])
    engine.consider("ct", [schema_issue("ct", "$.study_date")])
    engine.consider("ct", [schema_issue("ct", "$.study_date")])

    rolled = engine.rollback_last()
    assert rolled is not None and rolled.category == "ct"
    assert engine.stats()["rolled_back"] == 1


def test_rollback_of_an_unknown_refinement_is_a_no_op(engine: RefinementEngine):
    assert engine.rollback("ref-9999") is False


def test_stats_summarise_targets_and_rollbacks(engine: RefinementEngine):
    engine.consider("cbc", [schema_issue()])
    engine.consider("cbc", [schema_issue()])
    stats = engine.stats()
    assert stats["total"] == 1
    assert stats["active"] == 1
    assert stats["by_target"][TARGET_SKILL] == 1
    assert stats["threshold"] == 2
