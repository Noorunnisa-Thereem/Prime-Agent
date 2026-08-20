from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .config import CATEGORY_ORDER, DEFAULT_OBJECTIVE, ProjectPaths
from .file_tools import collect_files
from .memory_store import MemoryStore
from .models import CategoryResult, HarnessState, PlanStep, ValidationIssue
from .planner import TaskPlanner
from .refinement import RefinementManager
from .report_builder import build_integrated_report
from .schema_validator import SchemaValidator
from .skill_store import SkillRegistry
from .utils import ensure_dir, utc_now_iso
from .repair import repair_to_schema


@dataclass(slots=True)
class RunOutcome:
    report_path: Path
    report: dict[str, Any]
    category_results: dict[str, CategoryResult]
    plan: list[PlanStep]
    issues: list[ValidationIssue]


class PrimeAgentHarness:
    def __init__(self, paths: ProjectPaths):
        self.paths = paths
        ensure_dir(self.paths.reports_root)
        ensure_dir(self.paths.memory_root)
        ensure_dir(self.paths.skills_root)
        ensure_dir(self.paths.schemas_root)
        self.memory = MemoryStore(self.paths.memory_root)
        self.skills = SkillRegistry(self.paths.skills_root)
        self.validator = SchemaValidator(self.paths.schemas_root)
        self.refiner = RefinementManager(self.memory, self.skills)
        self.planner = TaskPlanner()

    def run(self) -> RunOutcome:
        state = self.memory.load_state()
        self.memory.append_event("run_started", {"objective": state.objective, "started_at": utc_now_iso()})
        loaded_skills = self.skills.load_all()
        self.memory.append_event(
            "skills_loaded",
            {"skills": [skill.to_dict() for skill in loaded_skills]},
        )

        files_by_category = collect_files(self.paths.data_root)
        plan = self.planner.plan(files_by_category)
        self.memory.append_event("plan_created", {"plan": [item.to_dict() for item in plan]})

        category_results = self._run_category_agents(files_by_category, plan)
        issues = [issue for result in category_results.values() for issue in result.validation_issues]

        report = build_integrated_report(category_results, self.validator)
        report = repair_to_schema(report, "digital_twin_report", self.validator)
        report_issues = self.validator.validate(report, "digital_twin_report")
        if report_issues:
            self.memory.append_event("integrated_validation_failed", {"issues": [issue.to_dict() for issue in report_issues]})
            report = repair_to_schema(report, "digital_twin_report", self.validator)
            report_issues = self.validator.validate(report, "digital_twin_report")
        if report_issues:
            for issue in report_issues:
                self.memory.record_issue(issue)
            raise RuntimeError("Unable to produce a validated integrated report")

        report_path = self.paths.report_path
        report_path.write_text(__import__("json").dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

        state.run_count += 1
        state.last_run_at = utc_now_iso()
        state.last_report_path = str(report_path)
        state.last_status = "passed"
        state.last_category_count = len(category_results)
        state.last_issue_count = len(issues)
        self.memory.save_state(state)
        self.memory.append_event("report_written", {"report_path": str(report_path)})

        return RunOutcome(report_path=report_path, report=report, category_results=category_results, plan=plan, issues=issues)

    def _run_category_agents(
        self,
        files_by_category: dict[str, list[Path]],
        plan: list[PlanStep],
    ) -> dict[str, CategoryResult]:
        results: dict[str, CategoryResult] = {}
        from .extractors import CATEGORY_EXTRACTORS

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(CATEGORY_ORDER)))) as pool:
            future_map = {}
            for step in plan:
                extractor_cls = CATEGORY_EXTRACTORS[step.category]
                extractor = extractor_cls(
                    category=step.category,
                    paths=self.paths,
                    validator=self.validator,
                    memory=self.memory,
                    skills=self.skills,
                    refiner=self.refiner,
                )
                future = pool.submit(extractor.run, files_by_category.get(step.category, []))
                future_map[future] = step.category
                self.memory.append_event(
                    "subagent_spawned",
                    {"category": step.category, "file_count": step.file_count, "skill": str(self.paths.category_skill_path(step.category))},
                )

            for future in as_completed(future_map):
                category = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    fallback_issue = ValidationIssue(
                        schema_name=category,
                        path="$",
                        message=f"Sub-agent failure: {exc}",
                        category=category,
                        issue_key=f"{category}:subagent-failure",
                    )
                    result = CategoryResult(
                        category=category,
                        section=self.validator.default(category),
                        evidence=[],
                        source_files=[str(path) for path in files_by_category.get(category, [])],
                        validation_issues=[fallback_issue],
                    )
                    self.memory.append_event(
                        "subagent_failed",
                        {"category": category, "error": str(exc), "fallback_used": True},
                    )
                results[category] = result
                self.memory.append_event(
                    "subagent_completed",
                    {"category": category, "issues": [issue.to_dict() for issue in result.validation_issues]},
                )

        ordered_results: dict[str, CategoryResult] = {}
        for category in CATEGORY_ORDER:
            if category in results:
                ordered_results[category] = results[category]
        return ordered_results
