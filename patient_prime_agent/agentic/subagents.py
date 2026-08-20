"""Persistent category sub-agents.

One agent per category (Clinical Notes, CBC, CT, MRI, ECG, EEG, Genetics,
Questionnaire).  Each agent is a :class:`~.runtime.PersistentAgent` that keeps
its own on-disk state and *wraps the existing extractor* -- the extraction logic
in ``patient_prime_agent/extractors`` is reused unchanged.

The agent adds the phases the extractor does not own:

* reads its ``SKILL.md`` and its learned memory directives before working
* Execute   -> run the extractor over the assigned files
* Validate  -> validate the section against ``schemas/<category>.schema.json``
* Fix       -> normalize/repair to the schema and re-validate
* report    -> fail the task when the section still does not validate, so the
               runtime's retry policy takes over
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import ProjectPaths
from ..extractors import CATEGORY_EXTRACTORS
from ..memory_store import MemoryStore
from ..models import CategoryResult, ValidationIssue
from ..refinement import RefinementManager
from ..repair import repair_to_schema
from ..schema_validator import SchemaValidator
from ..skill_store import SkillRegistry
from ..utils import utc_now_iso
from .harness import SubAgentConfig
from .memory import KIND_EPISODIC, AgentMemory
from .runtime import PersistentAgent, TaskEnvelope, TaskResult


class DeferredRefinementManager(RefinementManager):
    """Refinement no-op used inside the extractors.

    The agentic layer owns refinement (see :class:`~.refine.RefinementEngine`),
    so the extractor must not also record issue counts -- otherwise every issue
    would be counted twice and refine one run too early.
    """

    def consider(self, category: str, skill_name: str, issues: list[ValidationIssue]) -> list[Any]:
        return []


class CategorySubAgent(PersistentAgent):
    """A persistent agent that owns one data category end to end."""

    role = "category-subagent"

    def __init__(
        self,
        config: SubAgentConfig,
        paths: ProjectPaths,
        validator: SchemaValidator,
        memory_store: MemoryStore,
        skills: SkillRegistry,
        agent_memory: AgentMemory,
        state_root: Path,
    ) -> None:
        super().__init__(
            agent_id=config.agent_id,
            state_root=state_root,
            memory=agent_memory,
            memory_scope=config.memory_scope,
        )
        self.config = config
        self.category = config.category
        self.paths = paths
        self.validator = validator
        self.memory_store = memory_store
        self.skills = skills
        self.agent_memory = agent_memory
        self._extractor = None

    # ------------------------------------------------------------------
    def boot(self):
        state = super().boot()
        state.custom["skill_path"] = self.config.skill_path
        state.custom["schema"] = f"{self.config.schema_name}.schema.json"
        state.custom["instruction"] = self.config.instruction
        self.checkpoint()
        self.memory_store.append_agent_event(
            self.category,
            "agent_booted",
            {
                "agent_id": self.agent_id,
                "boot_count": state.boot_count,
                "directives": len(state.directives),
            },
        )
        return state

    def extractor(self):
        """Build (once) the existing extractor for this category."""

        if self._extractor is None:
            extractor_cls = CATEGORY_EXTRACTORS[self.category]
            self._extractor = extractor_cls(
                category=self.category,
                paths=self.paths,
                validator=self.validator,
                memory=self.memory_store,
                skills=self.skills,
                refiner=DeferredRefinementManager(self.memory_store, self.skills),
            )
        return self._extractor

    def skill_text(self) -> str:
        try:
            return self.skills.load(self.category).content
        except FileNotFoundError:
            return ""

    def working_instruction(self) -> str:
        """Instruction + learned directives, i.e. what the agent works from."""

        lines = [self.config.instruction]
        if self.state.directives:
            lines.append("Learned directives:")
            lines.extend(f"- {directive}" for directive in self.state.directives)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def handle(self, envelope: TaskEnvelope) -> TaskResult:
        if envelope.action == "describe":
            return TaskResult(
                task_id=envelope.task_id,
                agent_id=self.agent_id,
                action=envelope.action,
                output=self.describe(),
            )
        if envelope.action != "extract":
            return TaskResult(
                task_id=envelope.task_id,
                agent_id=self.agent_id,
                action=envelope.action,
                status="failed",
                error=f"Unsupported action {envelope.action!r}",
            )
        return self._handle_extract(envelope)

    def _handle_extract(self, envelope: TaskEnvelope) -> TaskResult:
        files = [Path(item) for item in envelope.payload.get("files", [])]
        skill = self.skill_text()
        if not skill:
            return TaskResult(
                task_id=envelope.task_id,
                agent_id=self.agent_id,
                action=envelope.action,
                status="failed",
                error=f"Missing SKILL.md for category {self.category}",
            )

        # --- Execute: reuse the existing extractor -----------------------
        result: CategoryResult = self.extractor().run(files)

        # --- Validate ----------------------------------------------------
        issues = self.validator.validate(result.section, self.config.schema_name)
        fixed = False
        if issues:
            # --- Fix: normalize back onto the schema and re-validate -----
            result.section = repair_to_schema(result.section, self.config.schema_name, self.validator)
            remaining = self.validator.validate(result.section, self.config.schema_name)
            fixed = not remaining
            result.retries += 1
            issues = remaining

        # Extraction-time notes (unparseable files etc.) are reported but do
        # not block integration; only schema issues do.
        schema_issues = list(issues)
        all_issues = [issue for issue in result.validation_issues if issue not in schema_issues] + schema_issues
        result.validation_issues = all_issues

        self.agent_memory.write(
            key=f"last_run::{envelope.task_id}",
            content=(
                f"attempt={envelope.attempt} files={len(files)} "
                f"schema_issues={len(schema_issues)} fixed={fixed}"
            ),
            scope=self.memory_scope,
            kind=KIND_EPISODIC,
            tags=["run"],
            metadata={
                "task_id": envelope.task_id,
                "at": utc_now_iso(),
                "source_files": [str(path) for path in files],
            },
        )
        self.memory_store.append_agent_event(
            self.category,
            "agent_task_completed",
            {
                "agent_id": self.agent_id,
                "task_id": envelope.task_id,
                "attempt": envelope.attempt,
                "file_count": len(files),
                "schema_issue_count": len(schema_issues),
                "repaired": fixed,
            },
        )

        status = "ok" if not schema_issues else "failed"
        error = None
        if schema_issues:
            error = f"{len(schema_issues)} schema issue(s): " + "; ".join(
                f"{issue.path}: {issue.message}" for issue in schema_issues[:3]
            )

        return TaskResult(
            task_id=envelope.task_id,
            agent_id=self.agent_id,
            action=envelope.action,
            status=status,
            output=result,
            error=error,
            issues=[issue.to_dict() for issue in all_issues],
        )

    def describe(self) -> dict[str, Any]:
        info = super().describe()
        info.update(
            {
                "category": self.category,
                "config": self.config.to_dict(),
                "skill_loaded": bool(self.skill_text()),
                "instruction": self.working_instruction(),
            }
        )
        return info


def build_category_subagents(
    configs: list[SubAgentConfig],
    paths: ProjectPaths,
    validator: SchemaValidator,
    memory_store: MemoryStore,
    skills: SkillRegistry,
    agent_memory: AgentMemory,
    state_root: Path,
) -> list[CategorySubAgent]:
    """Instantiate one persistent sub-agent per registered category config."""

    agents: list[CategorySubAgent] = []
    for config in configs:
        if config.category not in CATEGORY_EXTRACTORS:
            continue
        agents.append(
            CategorySubAgent(
                config=config,
                paths=paths,
                validator=validator,
                memory_store=memory_store,
                skills=skills,
                agent_memory=agent_memory,
                state_root=state_root,
            )
        )
    return agents
