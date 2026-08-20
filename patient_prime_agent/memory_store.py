from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import HARNESS_STATE_FILENAME, ISSUE_COUNTS_FILENAME, LESSONS_FILENAME, SESSION_LOG_FILENAME, DEFAULT_OBJECTIVE
from .models import HarnessState, ValidationIssue
from .utils import atomic_write_json, ensure_dir, read_json, utc_now_iso


@dataclass(slots=True)
class IssueState:
    count: int = 0
    last_seen: str | None = None
    last_refined_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "last_seen": self.last_seen,
            "last_refined_count": self.last_refined_count,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IssueState":
        return cls(
            count=int(payload.get("count", 0)),
            last_seen=payload.get("last_seen"),
            last_refined_count=int(payload.get("last_refined_count", 0)),
        )


class MemoryStore:
    def __init__(self, memory_root: Path):
        self.memory_root = ensure_dir(memory_root)
        self.session_log_path = self.memory_root / SESSION_LOG_FILENAME
        self.lessons_path = self.memory_root / LESSONS_FILENAME
        self.harness_state_path = self.memory_root / HARNESS_STATE_FILENAME
        self.issue_counts_path = self.memory_root / ISSUE_COUNTS_FILENAME
        self._lock = threading.Lock()
        self._ensure_files()

    def _ensure_files(self) -> None:
        if not self.harness_state_path.exists():
            atomic_write_json(self.harness_state_path, HarnessState(objective=DEFAULT_OBJECTIVE).to_dict())
        if not self.issue_counts_path.exists():
            atomic_write_json(self.issue_counts_path, {})
        if not self.session_log_path.exists():
            self.session_log_path.write_text("", encoding="utf-8")
        if not self.lessons_path.exists():
            self.lessons_path.write_text("", encoding="utf-8")

    def load_state(self) -> HarnessState:
        payload = read_json(self.harness_state_path, {})
        return HarnessState.from_dict(payload, DEFAULT_OBJECTIVE)

    def save_state(self, state: HarnessState) -> None:
        atomic_write_json(self.harness_state_path, state.to_dict())

    def append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {"timestamp": utc_now_iso(), "event": event_type, "payload": payload}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self.session_log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def append_agent_event(self, category: str, event_type: str, payload: dict[str, Any]) -> None:
        agent_dir = ensure_dir(self.memory_root / "agents" / category)
        log_path = agent_dir / SESSION_LOG_FILENAME
        if not log_path.exists():
            log_path.write_text("", encoding="utf-8")
        record = {"timestamp": utc_now_iso(), "event": event_type, "payload": payload}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def append_lesson(self, lesson: dict[str, Any]) -> None:
        lesson_record = {"timestamp": utc_now_iso(), **lesson}
        line = json.dumps(lesson_record, ensure_ascii=False)
        with self._lock:
            with self.lessons_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def load_issue_counts(self) -> dict[str, IssueState]:
        payload = read_json(self.issue_counts_path, {})
        return {key: IssueState.from_dict(value) for key, value in payload.items()}

    def save_issue_counts(self, issue_counts: dict[str, IssueState]) -> None:
        atomic_write_json(self.issue_counts_path, {key: value.to_dict() for key, value in issue_counts.items()})

    def record_issue(self, issue: ValidationIssue) -> IssueState:
        issue_counts = self.load_issue_counts()
        key = issue.issue_key or f"{issue.schema_name}:{issue.path}:{issue.message}"
        state = issue_counts.get(key, IssueState())
        state.count += 1
        state.last_seen = utc_now_iso()
        issue_counts[key] = state
        self.save_issue_counts(issue_counts)
        self.append_event("issue_recorded", {"issue": issue.to_dict(), "count": state.count})
        return state

    def mark_refined(self, issue_key: str, count: int) -> None:
        issue_counts = self.load_issue_counts()
        state = issue_counts.get(issue_key)
        if state is None:
            state = IssueState(count=count, last_seen=utc_now_iso(), last_refined_count=count)
        else:
            state.last_refined_count = count
        issue_counts[issue_key] = state
        self.save_issue_counts(issue_counts)
