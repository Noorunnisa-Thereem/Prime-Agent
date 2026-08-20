"""End-to-end: Plan -> Delegate -> Execute -> Validate -> Fix/Retry -> Verify -> Integrate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import build_project

from patient_prime_agent.agentic.main_agent import (
    MAIN_AGENT_ID,
    MainOrchestratorAgent,
    OrchestrationOutcome,
    run_agentic_pipeline,
)
from patient_prime_agent.agentic.settings import AgentSettings
from patient_prime_agent.config import CATEGORY_ORDER, ProjectPaths
from patient_prime_agent.schema_validator import SchemaValidator


@pytest.fixture
def outcome(project: ProjectPaths, settings: AgentSettings) -> OrchestrationOutcome:
    return run_agentic_pipeline(paths=project, settings=settings)


# ----------------------------------------------------------------------
# the report
# ----------------------------------------------------------------------
def test_the_integrated_report_is_written_to_the_expected_path(project: ProjectPaths, outcome: OrchestrationOutcome):
    assert outcome.report_path == project.reports_root / "Digital_Twin_Integrated_Report.json"
    assert outcome.report_path.exists()


def test_the_written_report_validates_against_its_schema(project: ProjectPaths, outcome: OrchestrationOutcome):
    validator = SchemaValidator(project.schemas_root)
    written = json.loads(outcome.report_path.read_text(encoding="utf-8"))
    assert validator.validate(written, "digital_twin_report") == []
    assert written["report_type"] == "Digital_Twin_Integrated_Report"


def test_every_category_section_is_present_and_valid(project: ProjectPaths, outcome: OrchestrationOutcome):
    validator = SchemaValidator(project.schemas_root)
    sections = outcome.report["sections"]
    assert set(sections) == set(CATEGORY_ORDER)
    for category in CATEGORY_ORDER:
        assert validator.validate(sections[category], category) == []


def test_extracted_values_match_the_source_documents(outcome: OrchestrationOutcome):
    sections = outcome.report["sections"]
    assert sections["cbc"]["hemoglobin_g_per_dL"] == 13.4
    assert sections["cbc"]["differential"]["neutrophils_pct"] == 58


def test_source_traceability_is_preserved_for_every_evidence_item(project: ProjectPaths, outcome: OrchestrationOutcome):
    from patient_prime_agent.file_tools import collect_files

    known = {str(path) for paths in collect_files(project.data_root).values() for path in paths}
    traceability = outcome.report["source_traceability"]
    assert traceability
    for item in traceability:
        assert item["source_file"] in known
        assert item["field"]


# ----------------------------------------------------------------------
# the loop
# ----------------------------------------------------------------------
def test_all_seven_phases_are_recorded_in_the_trajectory(project: ProjectPaths, outcome: OrchestrationOutcome):
    from patient_prime_agent.agentic.session import SessionStore

    store = SessionStore(project.memory_root / "agentic" / "sessions")
    phases = {step.phase for step in store.trajectory(outcome.session.session_id).steps}
    assert {"Plan", "Delegate", "Execute", "Validate", "Verify", "Integrate"} <= phases


def test_the_plan_covers_every_registered_category(outcome: OrchestrationOutcome):
    assert [step.category for step in outcome.plan] == list(CATEGORY_ORDER)
    assert all(step.status == "done" for step in outcome.plan)


def test_each_category_was_delegated_to_its_own_persistent_agent(outcome: OrchestrationOutcome):
    assert set(outcome.task_results) == set(CATEGORY_ORDER)
    for category, result in outcome.task_results.items():
        assert result.agent_id == f"agent-{category}"
        assert result.ok


def test_verification_passes_every_check(outcome: OrchestrationOutcome):
    assert outcome.verified
    names = {check["check"] for check in outcome.verification}
    assert names == {
        "sections_validate_against_schema",
        "evidence_traces_to_source_files",
        "no_values_without_source_files",
        "all_registered_categories_present",
    }


def test_a_category_without_source_files_stays_null_and_invents_nothing(tmp_path: Path, settings: AgentSettings):
    project = build_project(tmp_path / "partial", categories=["cbc", "clinical_notes"])
    outcome = run_agentic_pipeline(paths=project, settings=settings)
    validator = SchemaValidator(project.schemas_root)

    assert outcome.verified
    for category in ("ct", "mri", "ecg", "eeg", "genetics", "questionnaire"):
        assert outcome.report["sections"][category] == validator.default(category)
    assert outcome.report["sections"]["cbc"]["hemoglobin_g_per_dL"] == 13.4


# ----------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------
def test_the_run_manifest_records_the_agent_layer(project: ProjectPaths, outcome: OrchestrationOutcome):
    manifest = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))
    assert manifest["workflow"] == [
        "Plan",
        "Delegate",
        "Execute",
        "Validate",
        "Fix/Retry",
        "Verify",
        "Integrate",
    ]
    assert manifest["model"]["plan"]["model_id"]
    assert set(manifest["categories"]) == set(CATEGORY_ORDER)
    assert manifest["a2a"]["total_messages"] > 0
    assert manifest["trajectory_steps"] > 0


def test_run_metadata_is_kept_out_of_the_validated_report(outcome: OrchestrationOutcome):
    assert "session" not in outcome.report
    assert outcome.manifest_path.name == "agent_run_manifest.json"
    assert outcome.manifest_path != outcome.report_path


def test_sessions_messages_and_trajectories_are_persisted(project: ProjectPaths, outcome: OrchestrationOutcome):
    agentic = project.memory_root / "agentic"
    session_id = outcome.session.session_id
    assert (agentic / "sessions" / session_id / "session.json").exists()
    assert (agentic / "sessions" / session_id / "trajectory.jsonl").exists()
    assert (agentic / "a2a" / f"{session_id}.jsonl").exists()
    assert (agentic / "harness" / "harness.json").exists()
    assert (agentic / "runtime" / "agents" / MAIN_AGENT_ID / "state.json").exists()
    for category in CATEGORY_ORDER:
        assert (agentic / "runtime" / "agents" / f"agent-{category}" / "state.json").exists()


def test_a_second_run_resumes_the_same_session_when_asked(project: ProjectPaths, settings: AgentSettings):
    first = run_agentic_pipeline(paths=project, settings=settings)
    second = run_agentic_pipeline(paths=project, settings=settings, session_id=first.session.session_id)

    assert second.session.session_id == first.session.session_id
    assert second.session.run_count == 2


def test_agent_state_accumulates_across_runs(project: ProjectPaths, settings: AgentSettings):
    run_agentic_pipeline(paths=project, settings=settings)
    run_agentic_pipeline(paths=project, settings=settings)

    agent = MainOrchestratorAgent(paths=project, settings=settings)
    cbc = agent.subagents["cbc"]
    assert cbc.state.task_count >= 2
    assert cbc.state.boot_count >= 2


def test_the_legacy_harness_state_is_still_updated(project: ProjectPaths, outcome: OrchestrationOutcome):
    state = json.loads((project.memory_root / "harness_state.json").read_text(encoding="utf-8"))
    assert state["run_count"] >= 1
    assert state["last_report_path"] == str(outcome.report_path)
    assert state["last_status"] == "passed"


def test_the_orchestrator_answers_status_over_a2a(project: ProjectPaths, settings: AgentSettings):
    agent = MainOrchestratorAgent(paths=project, settings=settings)
    reply = agent.bus.request("agent-observer", MAIN_AGENT_ID, {"action": "status"})
    assert reply is not None
    assert reply.payload["result"]["status"] == "ok"


def test_status_reports_the_model_plan_without_loading_weights(project: ProjectPaths, settings: AgentSettings):
    agent = MainOrchestratorAgent(paths=project, settings=settings)
    status = agent.status()
    assert status["model"]["loaded"] is False
    assert status["llm"]["enabled"] is False
    assert status["model"]["plan"]["model_id"]


# ----------------------------------------------------------------------
# harness integration
# ----------------------------------------------------------------------
def test_disabling_a_subagent_removes_its_section_source_but_keeps_the_report_valid(
    project: ProjectPaths, settings: AgentSettings
):
    agent = MainOrchestratorAgent(paths=project, settings=settings)
    agent.harness.update_subagent("eeg", enabled=False)

    outcome = run_agentic_pipeline(paths=project, settings=settings)
    validator = SchemaValidator(project.schemas_root)

    assert "eeg" not in outcome.category_results
    assert outcome.report["sections"]["eeg"] == validator.default("eeg")
    assert validator.validate(outcome.report, "digital_twin_report") == []


def test_the_cli_runs_the_agentic_pipeline(project: ProjectPaths, capsys, monkeypatch):
    from patient_prime_agent.cli import main

    # ProjectPaths.discover() always resolves `root` to the real repo root (only the
    # data/reports/memory/skills/schemas paths below are redirected to the temp test
    # project), so settings resolution here would otherwise read the developer's real
    # .env. Force the LLM off so this test stays fast and deterministic regardless of
    # the ambient environment.
    monkeypatch.setenv("PRIME_AGENT_ENABLE_LLM", "0")

    exit_code = main(
        [
            "--agentic",
            "--data-root",
            str(project.data_root),
            "--reports-root",
            str(project.reports_root),
            "--memory-root",
            str(project.memory_root),
            "--skills-root",
            str(project.skills_root),
            "--schemas-root",
            str(project.schemas_root),
        ]
    )
    assert exit_code == 0
    assert "Digital_Twin_Integrated_Report.json" in capsys.readouterr().out
