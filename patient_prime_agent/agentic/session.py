"""Persistent sessions and agent trajectories.

Layout on disk::

    memory/sessions/index.json                     -- all known sessions
    memory/sessions/<session_id>/session.json      -- session record
    memory/sessions/<session_id>/trajectory.jsonl  -- one line per step
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from ..utils import atomic_write_json, ensure_dir, read_json, utc_now_iso

SESSION_INDEX_FILENAME = "index.json"
SESSION_FILENAME = "session.json"
TRAJECTORY_FILENAME = "trajectory.jsonl"


def new_session_id() -> str:
    return f"session-{uuid.uuid4().hex[:16]}"


@dataclass(slots=True)
class TrajectoryStep:
    """A single observable step taken by an agent."""

    step: int
    agent_id: str
    phase: str
    action: str
    status: str = "ok"
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrajectoryStep":
        return cls(
            step=int(data.get("step", 0)),
            agent_id=data.get("agent_id", "unknown"),
            phase=data.get("phase", "unknown"),
            action=data.get("action", ""),
            status=data.get("status", "ok"),
            detail=data.get("detail") or {},
            timestamp=data.get("timestamp") or utc_now_iso(),
        )


class Trajectory:
    """Append-only, disk-backed record of the steps taken in a session."""

    def __init__(self, path: Path):
        self.path = path
        ensure_dir(path.parent)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        self._steps: list[TrajectoryStep] = self.load()
        self._lock = threading.Lock()

    def record(
        self,
        agent_id: str,
        phase: str,
        action: str,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
    ) -> TrajectoryStep:
        with self._lock:
            step = TrajectoryStep(
                step=len(self._steps) + 1,
                agent_id=agent_id,
                phase=phase,
                action=action,
                status=status,
                detail=detail or {},
            )
            self._steps.append(step)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(step.to_dict(), ensure_ascii=False, default=str) + "\n")
        return step

    def load(self) -> list[TrajectoryStep]:
        if not self.path.exists():
            return []
        steps: list[TrajectoryStep] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                steps.append(TrajectoryStep.from_dict(json.loads(line)))
            except json.JSONDecodeError:
                continue
        return steps

    @property
    def steps(self) -> list[TrajectoryStep]:
        with self._lock:
            return list(self._steps)

    def by_agent(self, agent_id: str) -> list[TrajectoryStep]:
        return [step for step in self.steps if step.agent_id == agent_id]

    def by_phase(self, phase: str) -> list[TrajectoryStep]:
        return [step for step in self.steps if step.phase == phase]

    def failures(self) -> list[TrajectoryStep]:
        return [step for step in self.steps if step.status in {"failed", "error"}]

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "steps": [step.to_dict() for step in self.steps]}


@dataclass(slots=True)
class Session:
    session_id: str
    objective: str
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    status: str = "open"
    run_count: int = 0
    agent_ids: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        return cls(
            session_id=data["session_id"],
            objective=data.get("objective", ""),
            created_at=data.get("created_at") or utc_now_iso(),
            updated_at=data.get("updated_at") or utc_now_iso(),
            status=data.get("status", "open"),
            run_count=int(data.get("run_count", 0)),
            agent_ids=list(data.get("agent_ids") or []),
            metrics=data.get("metrics") or {},
            notes=list(data.get("notes") or []),
        )


class SessionStore:
    """Creates, resumes and persists sessions plus their trajectories."""

    def __init__(self, root: Path):
        self.root = ensure_dir(Path(root))
        self.index_path = self.root / SESSION_INDEX_FILENAME
        if not self.index_path.exists():
            atomic_write_json(self.index_path, {"sessions": []})

    def session_dir(self, session_id: str) -> Path:
        return ensure_dir(self.root / session_id)

    def create(self, objective: str, session_id: str | None = None) -> Session:
        session = Session(session_id=session_id or new_session_id(), objective=objective)
        self.save(session)
        return session

    def open(self, objective: str, session_id: str | None = None) -> Session:
        """Resume ``session_id`` if it exists, otherwise create it."""

        if session_id:
            existing = self.load(session_id)
            if existing is not None:
                existing.updated_at = utc_now_iso()
                existing.status = "open"
                self.save(existing)
                return existing
        return self.create(objective, session_id)

    def load(self, session_id: str) -> Session | None:
        path = self.root / session_id / SESSION_FILENAME
        payload = read_json(path, None)
        if not isinstance(payload, dict) or "session_id" not in payload:
            return None
        return Session.from_dict(payload)

    def save(self, session: Session) -> Session:
        session.updated_at = utc_now_iso()
        directory = self.session_dir(session.session_id)
        atomic_write_json(directory / SESSION_FILENAME, session.to_dict())
        self._index_add(session)
        return session

    def close(self, session: Session, status: str = "closed") -> Session:
        session.status = status
        return self.save(session)

    def delete(self, session_id: str) -> bool:
        directory = self.root / session_id
        if not directory.exists():
            return False
        for path in sorted(directory.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        directory.rmdir()
        index = read_json(self.index_path, {"sessions": []})
        index["sessions"] = [item for item in index.get("sessions", []) if item.get("session_id") != session_id]
        atomic_write_json(self.index_path, index)
        return True

    def list_sessions(self) -> list[dict[str, Any]]:
        index = read_json(self.index_path, {"sessions": []})
        return list(index.get("sessions", []))

    def trajectory(self, session_id: str) -> Trajectory:
        return Trajectory(self.session_dir(session_id) / TRAJECTORY_FILENAME)

    def _index_add(self, session: Session) -> None:
        index = read_json(self.index_path, {"sessions": []})
        entries = [item for item in index.get("sessions", []) if item.get("session_id") != session.session_id]
        entries.append(
            {
                "session_id": session.session_id,
                "objective": session.objective,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "status": session.status,
                "run_count": session.run_count,
            }
        )
        entries.sort(key=lambda item: item.get("created_at") or "")
        atomic_write_json(self.index_path, {"sessions": entries})
