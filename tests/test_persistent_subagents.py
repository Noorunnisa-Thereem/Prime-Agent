"""Persistent category sub-agents wrapping the existing extractors."""

from __future__ import annotations

from pathlib import Path

import pytest

from patient_prime_agent.agentic.harness import ContinualHarness, SubAgentConfig
from patient_prime_agent.agentic.memory import KIND_EPISODIC, AgentMemory
from patient_prime_agent.agentic.runtime import TaskEnvelope
from patient_prime_agent.agentic.subagents import (
    CategorySubAgent,
    DeferredRefinementManager,
    build_category_subagents,
)
from patient_prime_agent.config import CATEGORY_ORDER, ProjectPaths
from patient_prime_agent.extractors import CATEGORY_EXTRACTORS
from patient_prime_agent.memory_store import MemoryStore
from patient_prime_agent.models import CategoryResult
from patient_prime_agent.schema_validator import SchemaValidator
from patient_prime_agent.skill_store import SkillRegistry


@pytest.fixture
def wiring(project: ProjectPaths):
    agentic_root = project.memory_root / "agentic"
    harness = ContinualHarness(agentic_root / "harness", paths=project)
    return {
        "paths": project,
        "harness": harness,
        "validator": SchemaValidator(project.schemas_root),
        "memory_store": MemoryStore(project.memory_root),
        "skills": SkillRegistry(project.skills_root),
        "agent_memory": AgentMemory(agentic_root / "agent_memory"),
        "state_root": agentic_root / "runtime" / "agents",
    }


def make_agent(wiring: dict, category: str) -> CategorySubAgent:
    config = wiring["harness"].get_subagent(category)
    assert config is not None
    return CategorySubAgent(
        config=config,
        paths=wiring["paths"],
        validator=wiring["validator"],
        memory_store=wiring["memory_store"],
        skills=wiring["skills"],
        agent_memory=wiring["agent_memory"],
        state_root=wiring["state_root"],
    )


def files_for(project: ProjectPaths, category: str) -> list[str]:
    from patient_prime_agent.file_tools import collect_files

    return [str(path) for path in collect_files(project.data_root).get(category, [])]


def extract_envelope(project: ProjectPaths, category: str) -> TaskEnvelope:
    return TaskEnvelope(
        action="extract",
        recipient=f"agent-{category}",
        payload={"category": category, "files": files_for(project, category)},
    )


# ----------------------------------------------------------------------
# construction
# ----------------------------------------------------------------------
def test_one_subagent_is_built_per_registered_category(wiring: dict):
    agents = build_category_subagents(
        configs=wiring["harness"].list_subagents(enabled_only=True),
        paths=wiring["paths"],
        validator=wiring["validator"],
        memory_store=wiring["memory_store"],
        skills=wiring["skills"],
        agent_memory=wiring["agent_memory"],
        state_root=wiring["state_root"],
    )
    assert [agent.category for agent in agents] == list(CATEGORY_ORDER)
    assert {agent.agent_id for agent in agents} == {f"agent-{c}" for c in CATEGORY_ORDER}


def test_subagents_reuse_the_existing_extractor_classes(wiring: dict):
    for category in CATEGORY_ORDER:
        agent = make_agent(wiring, category)
        assert isinstance(agent.extractor(), CATEGORY_EXTRACTORS[category])


def test_the_extractor_is_built_once_and_cached(wiring: dict):
    agent = make_agent(wiring, "cbc")
    assert agent.extractor() is agent.extractor()


def test_the_extractor_refiner_is_deferred_to_the_agentic_engine(wiring: dict):
    agent = make_agent(wiring, "cbc")
    refiner = agent.extractor().refiner
    assert isinstance(refiner, DeferredRefinementManager)
    assert refiner.consider("cbc", "cbc", []) == []


def test_a_subagent_loads_its_own_skill_document(wiring: dict):
    agent = make_agent(wiring, "cbc")
    assert "# CBC" in agent.skill_text()


# ----------------------------------------------------------------------
# extraction
# ----------------------------------------------------------------------
def test_extraction_returns_a_schema_valid_category_result(project: ProjectPaths, wiring: dict):
    agent = make_agent(wiring, "cbc")
    result = agent.execute(extract_envelope(project, "cbc"))

    assert result.ok, result.error
    assert isinstance(result.output, CategoryResult)
    assert wiring["validator"].validate(result.output.section, "cbc") == []


def test_extracted_values_come_from_the_source_document(project: ProjectPaths, wiring: dict):
    agent = make_agent(wiring, "cbc")
    section = agent.execute(extract_envelope(project, "cbc")).output.section
    assert section["hemoglobin_g_per_dL"] == 13.4
    assert section["platelets_10e3_uL"] == 245


def test_every_category_produces_a_valid_section(project: ProjectPaths, wiring: dict):
    for category in CATEGORY_ORDER:
        agent = make_agent(wiring, category)
        result = agent.execute(extract_envelope(project, category))
        assert result.ok, f"{category}: {result.error}"
        assert wiring["validator"].validate(result.output.section, category) == []


def test_no_source_files_yields_the_schema_default_and_invents_nothing(wiring: dict):
    agent = make_agent(wiring, "cbc")
    result = agent.execute(TaskEnvelope(action="extract", recipient="agent-cbc", payload={"files": []}))

    assert result.ok
    assert result.output.section == wiring["validator"].default("cbc")
    assert result.output.evidence == []


def test_evidence_keeps_the_originating_source_file(project: ProjectPaths, wiring: dict):
    agent = make_agent(wiring, "cbc")
    result = agent.execute(extract_envelope(project, "cbc"))
    sources = files_for(project, "cbc")

    assert result.output.evidence
    assert all(evidence.source_file in sources for evidence in result.output.evidence)
    assert result.output.source_files == sources


def test_an_unknown_action_is_rejected(wiring: dict):
    agent = make_agent(wiring, "cbc")
    result = agent.execute(TaskEnvelope(action="delete_everything", recipient="agent-cbc"))
    assert not result.ok
    assert "Unsupported action" in (result.error or "")


def test_describe_action_reports_the_agent_configuration(wiring: dict):
    agent = make_agent(wiring, "ct")
    result = agent.execute(TaskEnvelope(action="describe", recipient="agent-ct"))
    assert result.ok
    assert result.output["category"] == "ct"
    assert result.output["skill_loaded"] is True


def test_a_missing_skill_file_fails_the_task_instead_of_guessing(wiring: dict, project: ProjectPaths):
    config = SubAgentConfig(
        category="cbc",
        agent_id="agent-cbc-noskill",
        label="CBC",
        skill_path="missing",
        schema_name="cbc",
        instruction="x",
    )
    agent = CategorySubAgent(
        config=config,
        paths=project,
        validator=wiring["validator"],
        memory_store=wiring["memory_store"],
        skills=SkillRegistry(project.root / "empty_skills"),
        agent_memory=wiring["agent_memory"],
        state_root=wiring["state_root"],
    )
    result = agent.execute(extract_envelope(project, "cbc"))
    assert not result.ok
    assert "Missing SKILL.md" in (result.error or "")


# ----------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------
def test_agent_counters_persist_across_instances(project: ProjectPaths, wiring: dict):
    make_agent(wiring, "cbc").execute(extract_envelope(project, "cbc"))
    revived = make_agent(wiring, "cbc")

    assert revived.state.task_count == 1
    assert revived.state.success_count == 1
    assert revived.state.last_run_at is not None


def test_boot_persists_the_skill_and_schema_binding(wiring: dict):
    agent = make_agent(wiring, "mri")
    state = agent.boot()
    assert state.custom["schema"] == "mri.schema.json"
    assert state.custom["skill_path"].endswith("SKILL.md")
    assert make_agent(wiring, "mri").state.custom["schema"] == "mri.schema.json"


def test_each_run_is_written_to_episodic_memory(project: ProjectPaths, wiring: dict):
    agent = make_agent(wiring, "cbc")
    agent.execute(extract_envelope(project, "cbc"))

    episodes = wiring["agent_memory"].list_records("agent-cbc", kind=KIND_EPISODIC)
    assert len(episodes) == 1
    assert episodes[0].metadata["source_files"] == files_for(project, "cbc")


def test_learned_directives_appear_in_the_working_instruction(wiring: dict):
    wiring["agent_memory"].remember_issue("agent-cbc", "cbc:$.x:type-mismatch", "Always keep units.", 2)
    agent = make_agent(wiring, "cbc")
    agent.boot()

    instruction = agent.working_instruction()
    assert "Learned directives:" in instruction
    assert "Always keep units." in instruction


def test_agent_events_are_appended_to_the_shared_memory_store(project: ProjectPaths, wiring: dict):
    make_agent(wiring, "cbc").execute(extract_envelope(project, "cbc"))
    log = (project.memory_root / "agents" / "cbc" / "session_log.jsonl").read_text(encoding="utf-8")
    assert "agent_task_completed" in log
