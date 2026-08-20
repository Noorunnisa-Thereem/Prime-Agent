"""Continual refinement with targeted updates and rollback.

When the same validation issue is observed ``threshold`` times it is treated as
*reusable* -- a defect in the harness rather than in one file -- and exactly one
artifact is updated:

``skill``            the category ``SKILL.md`` (extraction-rule problems)
``agent_instruction`` the sub-agent instruction in the continual harness
                     (loader/coverage problems: the agent is looking in the
                     wrong place or at the wrong files)
``memory``           a procedural memory record (everything else)

Each refinement is journalled with a before/after snapshot so
:meth:`RefinementEngine.rollback` can undo it.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from ..memory_store import MemoryStore
from ..models import ValidationIssue
from ..skill_store import SkillRegistry
from ..utils import atomic_write_json, atomic_write_text, ensure_dir, read_json, read_text, utc_now_iso
from .harness import ContinualHarness
from .memory import AgentMemory

TARGET_SKILL = "skill"
TARGET_AGENT_INSTRUCTION = "agent_instruction"
TARGET_MEMORY = "memory"

HISTORY_FILENAME = "history.jsonl"
INDEX_FILENAME = "refinement_index.json"

# Issue-key fragments that mean "the agent looked at the wrong thing", which is
# an instruction problem rather than an extraction-rule problem.
INSTRUCTION_MARKERS = (
    ":file-note:",
    "unsupported-extension",
    "parse-failed",
    "subagent-failure",
)

# Fragments that mean "the extraction rule produced the wrong shape/value".
SKILL_MARKERS = (
    ":type-mismatch",
    ":missing",
    ":enum-mismatch",
    ":const-mismatch",
    ":bad-date",
    ":bad-datetime",
    ":extra",
)


@dataclass(slots=True)
class RefinementRecord:
    refinement_id: str
    issue_key: str
    category: str
    target: str
    target_ref: str
    note: str
    observed_count: int
    before: str
    after: str
    created_at: str = field(default_factory=utc_now_iso)
    rolled_back: bool = False
    rolled_back_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RefinementRecord":
        return cls(
            refinement_id=data["refinement_id"],
            issue_key=data.get("issue_key", ""),
            category=data.get("category", ""),
            target=data.get("target", TARGET_MEMORY),
            target_ref=data.get("target_ref", ""),
            note=data.get("note", ""),
            observed_count=int(data.get("observed_count", 0)),
            before=data.get("before", ""),
            after=data.get("after", ""),
            created_at=data.get("created_at") or utc_now_iso(),
            rolled_back=bool(data.get("rolled_back", False)),
            rolled_back_at=data.get("rolled_back_at"),
        )


def classify_target(issue: ValidationIssue) -> str:
    """Pick the narrowest artifact that can fix this class of issue."""

    key = (issue.issue_key or f"{issue.schema_name}:{issue.path}:{issue.message}").lower()
    message = (issue.message or "").lower()
    if any(marker in key for marker in INSTRUCTION_MARKERS) or "sub-agent failure" in message:
        return TARGET_AGENT_INSTRUCTION
    if any(marker in key for marker in SKILL_MARKERS):
        return TARGET_SKILL
    return TARGET_MEMORY


def compose_note(category: str, issue: ValidationIssue, count: int, target: str) -> str:
    base = (
        f"Repeated issue in {category} ({count}x): {issue.message} at {issue.path}."
    )
    if target == TARGET_SKILL:
        return (
            f"{base} Tighten the extraction rule for this field: read the value only when the source "
            f"states it explicitly, keep the schema field name exactly, and emit null when it is absent."
        )
    if target == TARGET_AGENT_INSTRUCTION:
        return (
            f"{base} Before extracting, confirm the assigned files are loadable for this category and "
            f"record an explicit note (not a guessed value) for any file that cannot be parsed."
        )
    return (
        f"{base} Remember this failure mode for the next run and check it during the Verify phase "
        f"before integrating the section."
    )


class RefinementEngine:
    """Detects reusable issues, applies one targeted fix, and can undo it."""

    def __init__(
        self,
        memory_store: MemoryStore,
        agent_memory: AgentMemory,
        skills: SkillRegistry,
        harness: ContinualHarness,
        root: Path,
        threshold: int = 2,
    ) -> None:
        self.memory_store = memory_store
        self.agent_memory = agent_memory
        self.skills = skills
        self.harness = harness
        self.root = ensure_dir(Path(root))
        self.threshold = threshold
        self.history_path = self.root / HISTORY_FILENAME
        self.index_path = self.root / INDEX_FILENAME
        self._lock = threading.RLock()
        if not self.history_path.exists():
            self.history_path.write_text("", encoding="utf-8")
        if not self.index_path.exists():
            atomic_write_json(self.index_path, {"count": 0})

    # ------------------------------------------------------------------
    def consider(self, category: str, issues: list[ValidationIssue]) -> list[RefinementRecord]:
        """Record every issue; refine only those that have become reusable."""

        applied: list[RefinementRecord] = []
        for issue in issues:
            state = self.memory_store.record_issue(issue)
            issue_key = issue.issue_key or f"{issue.schema_name}:{issue.path}:{issue.message}"
            if state.count < self.threshold or state.last_refined_count >= state.count:
                continue
            if self.has_active_refinement(issue_key):
                # Already fixed once and not rolled back; re-applying the same
                # note would only add noise to the skill file.
                self.memory_store.mark_refined(issue_key, state.count)
                continue
            record = self.apply(category, issue, state.count)
            if record is not None:
                self.memory_store.mark_refined(issue_key, state.count)
                applied.append(record)
        return applied

    def has_active_refinement(self, issue_key: str) -> bool:
        """True when this issue was already refined and not rolled back."""

        return any(
            record.issue_key == issue_key and not record.rolled_back for record in self.history()
        )

    def apply(self, category: str, issue: ValidationIssue, count: int) -> RefinementRecord | None:
        issue_key = issue.issue_key or f"{issue.schema_name}:{issue.path}:{issue.message}"
        target = classify_target(issue)
        note = compose_note(category, issue, count, target)

        with self._lock:
            refinement_id = self._next_id()
            if target == TARGET_SKILL:
                before, after, target_ref = self._apply_skill(category, note)
            elif target == TARGET_AGENT_INSTRUCTION:
                before, after, target_ref = self._apply_instruction(category, note)
            else:
                before, after, target_ref = self._apply_memory(category, issue_key, note, count)

            if before == after:
                return None

            record = RefinementRecord(
                refinement_id=refinement_id,
                issue_key=issue_key,
                category=category,
                target=target,
                target_ref=target_ref,
                note=note,
                observed_count=count,
                before=before,
                after=after,
            )
            self._journal(record)

        self.memory_store.append_lesson(
            {
                "lesson_id": record.refinement_id,
                "category": category,
                "issue_key": issue_key,
                "lesson": note,
                "observed_count": count,
                "target": target,
                "target_ref": record.target_ref,
                "trigger": issue.to_dict(),
            }
        )
        self.harness.commit(reason=f"refinement {record.refinement_id} on {target}", actor="refinement-engine")
        return record

    # ------------------------------------------------------------------
    # targeted updates
    # ------------------------------------------------------------------
    def _apply_skill(self, category: str, note: str) -> tuple[str, str, str]:
        path = self.skills.skills_root / category / "SKILL.md"
        before = read_text(path) if path.exists() else ""
        try:
            self.skills.append_refinement(category, note)
        except FileNotFoundError:
            return before, before, str(path)
        after = read_text(path)
        self.harness.record_skill_refinement(category, note)
        return before, after, str(path)

    def _apply_instruction(self, category: str, note: str) -> tuple[str, str, str]:
        config = self.harness.get_subagent(category)
        if config is None:
            return "", "", f"subagents.{category}"
        before = config.instruction
        line = f"Learned rule: {note}"
        if line in before:
            return before, before, f"subagents.{category}.instruction"
        after = f"{before.rstrip()}\n{line}"
        self.harness.update_subagent(category, instruction=after)
        return before, after, f"subagents.{category}.instruction"

    def _apply_memory(self, category: str, issue_key: str, note: str, count: int) -> tuple[str, str, str]:
        scope = f"agent-{category}"
        key = f"issue::{issue_key}"
        existing = self.agent_memory.read(key, scope=scope, count_hit=False)
        before = existing.content if existing else ""
        if before == note:
            return before, before, f"{scope}:{key}"
        self.agent_memory.remember_issue(scope, issue_key, note, count)
        return before, note, f"{scope}:{key}"

    # ------------------------------------------------------------------
    # rollback
    # ------------------------------------------------------------------
    def rollback(self, refinement_id: str) -> bool:
        """Restore the artifact this refinement changed to its previous state."""

        with self._lock:
            records = self.history()
            record = next((item for item in records if item.refinement_id == refinement_id), None)
            if record is None or record.rolled_back:
                return False

            if record.target == TARGET_SKILL:
                path = Path(record.target_ref)
                if not path.exists():
                    return False
                atomic_write_text(path, record.before)
                self.skills._cache.pop(record.category, None)
            elif record.target == TARGET_AGENT_INSTRUCTION:
                if self.harness.update_subagent(record.category, instruction=record.before) is None:
                    return False
            else:
                scope, _, key = record.target_ref.partition(":")
                if record.before:
                    self.agent_memory.update(key, scope=scope, content=record.before)
                else:
                    self.agent_memory.delete(key, scope=scope)

            record.rolled_back = True
            record.rolled_back_at = utc_now_iso()
            self._rewrite_history(records)

        self.harness.commit(reason=f"rollback of {refinement_id}", actor="refinement-engine")
        return True

    def rollback_last(self, category: str | None = None) -> RefinementRecord | None:
        for record in reversed(self.history()):
            if record.rolled_back:
                continue
            if category is not None and record.category != category:
                continue
            if self.rollback(record.refinement_id):
                return record
            return None
        return None

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------
    def history(self, category: str | None = None, target: str | None = None) -> list[RefinementRecord]:
        records: list[RefinementRecord] = []
        if self.history_path.exists():
            for line in self.history_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(RefinementRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError):
                    continue
        if category is not None:
            records = [record for record in records if record.category == category]
        if target is not None:
            records = [record for record in records if record.target == target]
        return records

    def _journal(self, record: RefinementRecord) -> None:
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def _rewrite_history(self, records: list[RefinementRecord]) -> None:
        lines = [json.dumps(record.to_dict(), ensure_ascii=False) for record in records]
        atomic_write_text(self.history_path, "\n".join(lines) + ("\n" if lines else ""))

    def _next_id(self) -> str:
        index = read_json(self.index_path, {"count": 0})
        count = int(index.get("count", 0)) + 1
        atomic_write_json(self.index_path, {"count": count})
        return f"ref-{count:04d}"

    def stats(self) -> dict[str, Any]:
        records = self.history()
        by_target: dict[str, int] = {}
        for record in records:
            by_target[record.target] = by_target.get(record.target, 0) + 1
        return {
            "total": len(records),
            "active": sum(1 for record in records if not record.rolled_back),
            "rolled_back": sum(1 for record in records if record.rolled_back),
            "by_target": by_target,
            "threshold": self.threshold,
        }
