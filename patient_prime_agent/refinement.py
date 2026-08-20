from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .memory_store import MemoryStore
from .models import ValidationIssue
from .skill_store import SkillRegistry
from .utils import utc_now_iso


@dataclass(slots=True)
class RefinementOutcome:
    issue_key: str
    refined: bool
    count: int
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_key": self.issue_key,
            "refined": self.refined,
            "count": self.count,
            "note": self.note,
        }


class RefinementManager:
    def __init__(self, memory: MemoryStore, skills: SkillRegistry, threshold: int = 2):
        self.memory = memory
        self.skills = skills
        self.threshold = threshold

    def consider(self, category: str, skill_name: str, issues: list[ValidationIssue]) -> list[RefinementOutcome]:
        outcomes: list[RefinementOutcome] = []
        for issue in issues:
            state = self.memory.record_issue(issue)
            issue_key = issue.issue_key or f"{issue.schema_name}:{issue.path}:{issue.message}"
            if state.count < self.threshold or state.last_refined_count >= state.count:
                outcomes.append(RefinementOutcome(issue_key=issue_key, refined=False, count=state.count))
                continue
            note = self._compose_note(category, issue, state.count)
            self.skills.append_refinement(skill_name, note)
            self.memory.append_lesson(
                {
                    "lesson_id": f"{category}-{issue_key}",
                    "category": category,
                    "issue_key": issue_key,
                    "lesson": note,
                    "observed_count": state.count,
                    "trigger": issue.to_dict(),
                }
            )
            self.memory.mark_refined(issue_key, state.count)
            outcomes.append(RefinementOutcome(issue_key=issue_key, refined=True, count=state.count, note=note))
        return outcomes

    def _compose_note(self, category: str, issue: ValidationIssue, count: int) -> str:
        return (
            f"Observed repeated issue in {category}: {issue.message} at {issue.path}. "
            f"Seen {count} times. Tighten the extraction rules, preserve nulls for unknown fields, "
            f"and keep source traceability tied to the original file excerpt."
        )
