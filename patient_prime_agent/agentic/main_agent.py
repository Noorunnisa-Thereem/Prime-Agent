"""The main orchestrator agent.

Implements the required loop end to end:

    Plan -> Delegate -> Execute -> Validate -> Fix/Retry -> Verify -> Integrate

Plan
    Reuses ``file_tools.collect_files`` and ``planner.TaskPlanner``.
Delegate
    Sends one A2A task per category to its persistent sub-agent through the RLM
    runtime.
Execute / Validate / Fix
    Owned by the sub-agent (existing extractor + schema validator + repair).
Retry
    Owned by the runtime's retry budget; persistent failures fall back to the
    schema default section so a bad category can never inject invented values.
Verify
    Independent re-validation of every section, plus traceability and
    "no invented values" checks, before anything is merged.
Integrate
    Reuses ``report_builder.build_integrated_report`` and writes
    ``reports/Digital_Twin_Integrated_Report.json``.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import CATEGORY_ORDER, DEFAULT_OBJECTIVE, ProjectPaths
from ..file_tools import collect_files
from ..memory_store import MemoryStore
from ..models import CategoryResult, PlanStep, ValidationIssue
from ..planner import TaskPlanner
from ..report_builder import build_integrated_report
from ..repair import repair_to_schema
from ..schema_validator import SchemaValidator
from ..skill_store import SkillRegistry
from ..utils import atomic_write_json, ensure_dir, utc_now_iso
from .a2a import A2ABus
from .harness import ContinualHarness
from .llm import LanguageModel
from .memory import KIND_EPISODIC, AgentMemory
from .model_loader import ModelLoader
from .refine import RefinementEngine, RefinementRecord
from .runtime import PersistentAgent, RLMRuntime, TaskEnvelope, TaskResult
from .session import Session, SessionStore, Trajectory
from .settings import AgentSettings, load_settings
from .subagents import CategorySubAgent, build_category_subagents

MAIN_AGENT_ID = "agent-main"
MANIFEST_FILENAME = "agent_run_manifest.json"


@dataclass(slots=True)
class OrchestrationOutcome:
    report_path: Path
    report: dict[str, Any]
    manifest_path: Path
    manifest: dict[str, Any]
    session: Session
    plan: list[PlanStep]
    category_results: dict[str, CategoryResult]
    task_results: dict[str, TaskResult]
    verification: list[dict[str, Any]]
    refinements: list[RefinementRecord] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return all(check.get("passed") for check in self.verification)


class MainOrchestratorAgent(PersistentAgent):
    """Persistent orchestrator that drives the whole run."""

    role = "main-orchestrator"

    def __init__(
        self,
        paths: ProjectPaths,
        settings: AgentSettings | None = None,
        session_id: str | None = None,
    ) -> None:
        self.paths = paths
        self.settings = settings or load_settings(project_root=paths.root)

        ensure_dir(paths.reports_root)
        ensure_dir(paths.memory_root)

        self.agentic_root = ensure_dir(paths.memory_root / "agentic")
        self.memory_store = MemoryStore(paths.memory_root)
        self.skills = SkillRegistry(paths.skills_root)
        self.validator = SchemaValidator(paths.schemas_root)
        self.planner = TaskPlanner()

        self.agent_memory = AgentMemory(self.agentic_root / "agent_memory")
        self.harness = ContinualHarness(self.agentic_root / "harness", paths=paths)
        self.sessions = SessionStore(self.agentic_root / "sessions")

        self.session = self.sessions.open(
            objective=self.harness.objective or DEFAULT_OBJECTIVE,
            session_id=session_id or self.settings.session_id,
        )
        self.trajectory: Trajectory = self.sessions.trajectory(self.session.session_id)
        self.bus = A2ABus(self.agentic_root / "a2a", self.session.session_id)

        super().__init__(
            agent_id=MAIN_AGENT_ID,
            state_root=self.agentic_root / "runtime" / "agents",
            memory=self.agent_memory,
            memory_scope="agent-main",
        )

        self.runtime = RLMRuntime(
            root=self.agentic_root / "runtime",
            bus=self.bus,
            trajectory=self.trajectory,
            memory=self.agent_memory,
            default_max_retries=self.settings.max_retries,
        )
        self.runtime.register(self)

        self.refiner = RefinementEngine(
            memory_store=self.memory_store,
            agent_memory=self.agent_memory,
            skills=self.skills,
            harness=self.harness,
            root=self.agentic_root / "refinements",
            threshold=self.settings.refinement_threshold,
        )

        self.model_loader = ModelLoader(self.settings)
        self.llm = LanguageModel.build(self.settings, self.model_loader)

        self.subagents: dict[str, CategorySubAgent] = {}
        self._register_subagents()

    # ------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------
    def _register_subagents(self) -> None:
        configs = self.harness.list_subagents(enabled_only=True)
        agents = build_category_subagents(
            configs=configs,
            paths=self.paths,
            validator=self.validator,
            memory_store=self.memory_store,
            skills=self.skills,
            agent_memory=self.agent_memory,
            state_root=self.agentic_root / "runtime" / "agents",
        )
        for agent in agents:
            self.runtime.register(agent)
            self.subagents[agent.category] = agent

    def handle(self, envelope: TaskEnvelope) -> TaskResult:
        """The orchestrator answers status queries over A2A like any other agent."""

        if envelope.action == "status":
            return TaskResult(
                task_id=envelope.task_id,
                agent_id=self.agent_id,
                action=envelope.action,
                output=self.status(),
            )
        return TaskResult(
            task_id=envelope.task_id,
            agent_id=self.agent_id,
            action=envelope.action,
            status="failed",
            error=f"Unsupported action {envelope.action!r}",
        )

    def status(self) -> dict[str, Any]:
        return {
            "session": self.session.to_dict(),
            "harness": self.harness.summary(),
            "runtime_agents": [agent.agent_id for agent in self.runtime.agents],
            "model": self.model_loader.describe(),
            "llm": self.llm.describe(),
            "refinements": self.refiner.stats(),
        }

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------
    def run(self) -> OrchestrationOutcome:
        self.boot()
        self.runtime.boot_all()
        self.session.run_count += 1
        self.session.agent_ids = [agent.agent_id for agent in self.runtime.agents]
        self.sessions.save(self.session)

        self.memory_store.append_event(
            "agentic_run_started",
            {
                "session_id": self.session.session_id,
                "objective": self.session.objective,
                "agents": self.session.agent_ids,
                "model_plan": self.model_loader.plan.to_dict(),
            },
        )
        self.bus.emit(self.agent_id, "run_started", {"session_id": self.session.session_id})

        files_by_category, plan = self._phase_plan()
        task_results = self._phase_delegate(files_by_category, plan)
        category_results, refinements = self._phase_validate(files_by_category, task_results)
        verification = self._phase_verify(files_by_category, category_results)
        report, report_path = self._phase_integrate(category_results, verification)

        manifest, manifest_path = self._write_manifest(
            plan=plan,
            files_by_category=files_by_category,
            task_results=task_results,
            category_results=category_results,
            verification=verification,
            refinements=refinements,
            report_path=report_path,
        )

        self.session.metrics = {
            "categories": len(category_results),
            "failed_tasks": sum(1 for result in task_results.values() if not result.ok),
            "refinements": len(refinements),
            "verified": all(check["passed"] for check in verification),
            "report_path": str(report_path),
        }
        self.sessions.save(self.session)
        self.runtime.checkpoint_all()
        self.bus.emit(self.agent_id, "run_completed", dict(self.session.metrics))
        self.memory_store.append_event("agentic_run_completed", dict(self.session.metrics))

        return OrchestrationOutcome(
            report_path=report_path,
            report=report,
            manifest_path=manifest_path,
            manifest=manifest,
            session=self.session,
            plan=plan,
            category_results=category_results,
            task_results=task_results,
            verification=verification,
            refinements=refinements,
        )

    # -- Plan ----------------------------------------------------------
    def _phase_plan(self) -> tuple[dict[str, list[Path]], list[PlanStep]]:
        files_by_category = collect_files(self.paths.data_root)
        plan = self.planner.plan(files_by_category)
        registered = set(self.subagents)
        plan = [step for step in plan if step.category in registered]

        self.trajectory.record(
            agent_id=self.agent_id,
            phase="Plan",
            action="build_plan",
            detail={
                "steps": [step.to_dict() for step in plan],
                "total_files": sum(len(files) for files in files_by_category.values()),
            },
        )
        self.memory_store.append_event("agentic_plan_created", {"plan": [step.to_dict() for step in plan]})

        if self.llm.is_live:
            narration = self.llm.advise(
                self.harness.render_prompt()
                + "\n\nSummarise this extraction plan in two sentences. Do not mention any patient value.\n"
                + json.dumps([step.to_dict() for step in plan], indent=2),
            )
            self.agent_memory.write(
                key=f"plan_narration::{self.session.session_id}",
                content=narration,
                scope="agent-main",
                kind=KIND_EPISODIC,
                tags=["plan", "llm"],
            )
            self.trajectory.record(
                agent_id=self.agent_id,
                phase="Plan",
                action="llm_narration",
                detail={"model": self.model_loader.plan.model_id, "summary": narration},
            )
        return files_by_category, plan

    # -- Delegate / Execute --------------------------------------------
    def _phase_delegate(
        self,
        files_by_category: dict[str, list[Path]],
        plan: list[PlanStep],
    ) -> dict[str, TaskResult]:
        envelopes = [
            TaskEnvelope(
                action="extract",
                recipient=self.subagents[step.category].agent_id,
                sender=self.agent_id,
                session_id=self.session.session_id,
                max_retries=self.subagents[step.category].config.max_retries,
                payload={
                    "category": step.category,
                    "files": [str(path) for path in files_by_category.get(step.category, [])],
                    "skill_path": self.subagents[step.category].config.skill_path,
                    "schema": self.subagents[step.category].config.schema_name,
                    "instruction": self.subagents[step.category].working_instruction(),
                },
            )
            for step in plan
        ]

        results: dict[str, TaskResult] = {}
        workers = max(1, min(self.settings.max_workers, len(envelopes) or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.runtime.delegate, envelope): envelope for envelope in envelopes}
            for future, envelope in futures.items():
                category = envelope.payload["category"]
                try:
                    results[category] = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    results[category] = TaskResult(
                        task_id=envelope.task_id,
                        agent_id=envelope.recipient,
                        action=envelope.action,
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
        for step in plan:
            step_result = results.get(step.category)
            step.status = "done" if step_result is not None and step_result.ok else "failed"
        return results

    # -- Validate / Fix ------------------------------------------------
    def _phase_validate(
        self,
        files_by_category: dict[str, list[Path]],
        task_results: dict[str, TaskResult],
    ) -> tuple[dict[str, CategoryResult], list[RefinementRecord]]:
        category_results: dict[str, CategoryResult] = {}
        refinements: list[RefinementRecord] = []

        for category in CATEGORY_ORDER:
            result = task_results.get(category)
            if result is None:
                continue
            source_files = [str(path) for path in files_by_category.get(category, [])]
            payload = result.output

            if isinstance(payload, CategoryResult):
                category_result = payload
            else:
                category_result = self._fallback_result(category, source_files, result.error)

            issues = self.validator.validate(category_result.section, category)
            if issues:
                # Last-chance fix before the section is dropped for a safe default.
                category_result.section = repair_to_schema(category_result.section, category, self.validator)
                issues = self.validator.validate(category_result.section, category)
            if issues:
                self.trajectory.record(
                    agent_id=self.agent_id,
                    phase="Validate",
                    action=f"reject:{category}",
                    status="failed",
                    detail={"issues": [issue.to_dict() for issue in issues][:5]},
                )
                category_result = self._fallback_result(
                    category, source_files, f"{len(issues)} unresolved schema issue(s)", issues
                )
            else:
                self.trajectory.record(
                    agent_id=self.agent_id,
                    phase="Validate",
                    action=f"accept:{category}",
                    detail={"evidence": len(category_result.evidence), "files": len(source_files)},
                )

            refinements.extend(self.refiner.consider(category, category_result.validation_issues))
            category_results[category] = category_result

        if refinements:
            self.trajectory.record(
                agent_id=self.agent_id,
                phase="Fix/Retry",
                action="refine_harness",
                detail={
                    "refinements": [
                        {"id": item.refinement_id, "target": item.target, "ref": item.target_ref}
                        for item in refinements
                    ]
                },
            )
            # Reload sub-agent directives so the next task already sees them.
            for agent in self.subagents.values():
                agent.boot()
        return category_results, refinements

    def _fallback_result(
        self,
        category: str,
        source_files: list[str],
        error: str | None,
        issues: list[ValidationIssue] | None = None,
    ) -> CategoryResult:
        """A schema-default section: all nulls, no invented values."""

        fallback_issue = ValidationIssue(
            schema_name=category,
            path="$",
            message=f"Sub-agent did not produce a valid section: {error or 'unknown error'}",
            category=category,
            issue_key=f"{category}:subagent-failure",
        )
        self.memory_store.append_event(
            "agentic_category_fallback",
            {"category": category, "error": error, "files": len(source_files)},
        )
        return CategoryResult(
            category=category,
            section=self.validator.default(category),
            evidence=[],
            source_files=source_files,
            validation_issues=(issues or []) + [fallback_issue],
        )

    # -- Verify ---------------------------------------------------------
    def _phase_verify(
        self,
        files_by_category: dict[str, list[Path]],
        category_results: dict[str, CategoryResult],
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []

        invalid = [
            category
            for category, result in category_results.items()
            if self.validator.validate(result.section, category)
        ]
        checks.append(
            {
                "check": "sections_validate_against_schema",
                "passed": not invalid,
                "detail": {"invalid_categories": invalid},
            }
        )

        known_files = {str(path) for paths in files_by_category.values() for path in paths}
        untraceable = [
            evidence.source_file
            for result in category_results.values()
            for evidence in result.evidence
            if evidence.source_file not in known_files
        ]
        checks.append(
            {
                "check": "evidence_traces_to_source_files",
                "passed": not untraceable,
                "detail": {"unknown_sources": sorted(set(untraceable))[:10]},
            }
        )

        # A category with no source files must stay at its schema default: any
        # populated field there would be an invented value.
        invented: list[str] = []
        for category, result in category_results.items():
            if files_by_category.get(category):
                continue
            if result.section != self.validator.default(category):
                invented.append(category)
        checks.append(
            {
                "check": "no_values_without_source_files",
                "passed": not invented,
                "detail": {"categories_with_unsourced_values": invented},
            }
        )

        checks.append(
            {
                "check": "all_registered_categories_present",
                "passed": set(category_results) == set(self.subagents),
                "detail": {
                    "missing": sorted(set(self.subagents) - set(category_results)),
                    "expected": sorted(self.subagents),
                },
            }
        )

        for check in checks:
            self.trajectory.record(
                agent_id=self.agent_id,
                phase="Verify",
                action=check["check"],
                status="ok" if check["passed"] else "failed",
                detail=check["detail"],
            )
        self.memory_store.append_event("agentic_verification", {"checks": checks})
        return checks

    # -- Integrate ------------------------------------------------------
    def _phase_integrate(
        self,
        category_results: dict[str, CategoryResult],
        verification: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], Path]:
        report = build_integrated_report(category_results, self.validator)
        report = repair_to_schema(report, "digital_twin_report", self.validator)
        issues = self.validator.validate(report, "digital_twin_report")
        if issues:
            report = repair_to_schema(report, "digital_twin_report", self.validator)
            issues = self.validator.validate(report, "digital_twin_report")
        if issues:
            for issue in issues:
                self.memory_store.record_issue(issue)
            self.trajectory.record(
                agent_id=self.agent_id,
                phase="Integrate",
                action="write_report",
                status="failed",
                detail={"issues": [issue.to_dict() for issue in issues][:5]},
            )
            raise RuntimeError("Unable to produce a validated integrated report")

        failed_checks = [check["check"] for check in verification if not check["passed"]]
        report["validation"] = {
            "schema": "digital_twin_report.schema.json",
            "status": "passed" if not failed_checks else "passed_with_warnings",
            "checked_at": utc_now_iso(),
            "issues_found": failed_checks,
        }
        # Re-validate after touching the validation block.
        issues = self.validator.validate(report, "digital_twin_report")
        if issues:
            raise RuntimeError("Integrated report became invalid after annotation")

        report_path = self.paths.report_path
        ensure_dir(report_path.parent)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

        self.trajectory.record(
            agent_id=self.agent_id,
            phase="Integrate",
            action="write_report",
            detail={"report_path": str(report_path), "sections": len(report.get("sections", {}))},
        )
        self.memory_store.append_event("agentic_report_written", {"report_path": str(report_path)})

        state = self.memory_store.load_state()
        state.run_count += 1
        state.last_run_at = utc_now_iso()
        state.last_report_path = str(report_path)
        state.last_status = report["validation"]["status"]
        state.last_category_count = len(category_results)
        state.last_issue_count = sum(len(result.validation_issues) for result in category_results.values())
        self.memory_store.save_state(state)

        return report, report_path

    # ------------------------------------------------------------------
    def _write_manifest(
        self,
        plan: list[PlanStep],
        files_by_category: dict[str, list[Path]],
        task_results: dict[str, TaskResult],
        category_results: dict[str, CategoryResult],
        verification: list[dict[str, Any]],
        refinements: list[RefinementRecord],
        report_path: Path,
    ) -> tuple[dict[str, Any], Path]:
        """Agent-run metadata, kept beside (not inside) the validated report.

        ``digital_twin_report.schema.json`` sets ``additionalProperties: false``,
        so run metadata belongs in its own sidecar file.
        """

        manifest = {
            "generated_at": utc_now_iso(),
            "session": self.session.to_dict(),
            "objective": self.harness.objective,
            "workflow": ["Plan", "Delegate", "Execute", "Validate", "Fix/Retry", "Verify", "Integrate"],
            "model": self.model_loader.describe(),
            "llm": self.llm.describe(),
            "harness": self.harness.summary(),
            "plan": [step.to_dict() for step in plan],
            "categories": {
                category: {
                    "agent_id": self.subagents[category].agent_id if category in self.subagents else None,
                    "source_files": [str(path) for path in files_by_category.get(category, [])],
                    "issue_count": len(result.validation_issues),
                    "evidence_count": len(result.evidence),
                    "retries": result.retries,
                }
                for category, result in category_results.items()
            },
            "tasks": {category: result.to_dict() for category, result in task_results.items()},
            "verification": verification,
            "refinements": [record.to_dict() | {"before": "", "after": ""} for record in refinements],
            "a2a": self.bus.stats(),
            "trajectory_steps": len(self.trajectory.steps),
            "report_path": str(report_path),
        }
        manifest_path = self.paths.reports_root / MANIFEST_FILENAME
        atomic_write_json(manifest_path, manifest)
        return manifest, manifest_path


def run_agentic_pipeline(
    paths: ProjectPaths | None = None,
    settings: AgentSettings | None = None,
    session_id: str | None = None,
) -> OrchestrationOutcome:
    """Convenience entry point used by the CLI and the tests."""

    paths = paths or ProjectPaths.discover()
    agent = MainOrchestratorAgent(paths=paths, settings=settings, session_id=session_id)
    try:
        return agent.run()
    finally:
        agent.runtime.shutdown()
