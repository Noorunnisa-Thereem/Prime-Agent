"""RLM-style persistent agent runtime.

"Persistent" means an agent is a long-lived object with its own identity and its
own on-disk state:

    memory/runtime/agents/<agent_id>/state.json

The state survives process exit, so an agent booted on the next run already
knows how many times it has run, what it last failed on, and which learned
directives apply to it.  The runtime owns the registry, routes delegated tasks
over the A2A bus, applies the retry policy, and writes every step into the
session trajectory.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Iterable

from ..utils import atomic_write_json, ensure_dir, read_json, utc_now_iso
from .a2a import A2ABus, A2AMessage, MessageType
from .memory import AgentMemory
from .session import Trajectory

STATE_FILENAME = "state.json"


def new_task_id() -> str:
    return f"task-{uuid.uuid4().hex[:12]}"


@dataclass(slots=True)
class TaskEnvelope:
    """A unit of delegated work."""

    action: str
    recipient: str
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=new_task_id)
    sender: str = "agent-main"
    session_id: str | None = None
    conversation_id: str | None = None
    attempt: int = 1
    max_retries: int = 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_message(self) -> A2AMessage:
        return A2AMessage(
            sender=self.sender,
            recipient=self.recipient,
            message_type=MessageType.REQUEST,
            payload={
                "task_id": self.task_id,
                "action": self.action,
                "attempt": self.attempt,
                **self.payload,
            },
            session_id=self.session_id,
            conversation_id=self.conversation_id,
        )


@dataclass(slots=True)
class TaskResult:
    task_id: str
    agent_id: str
    action: str
    status: str = "ok"
    output: Any = None
    error: str | None = None
    attempts: int = 1
    duration_ms: float = 0.0
    issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "action": self.action,
            "status": self.status,
            "error": self.error,
            "attempts": self.attempts,
            "duration_ms": round(self.duration_ms, 2),
            "issue_count": len(self.issues),
        }


@dataclass(slots=True)
class AgentState:
    agent_id: str
    role: str
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    boot_count: int = 0
    task_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    retry_count: int = 0
    last_task_id: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_run_at: str | None = None
    directives: list[str] = field(default_factory=list)
    custom: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentState":
        return cls(
            agent_id=data["agent_id"],
            role=data.get("role", "agent"),
            created_at=data.get("created_at") or utc_now_iso(),
            updated_at=data.get("updated_at") or utc_now_iso(),
            boot_count=int(data.get("boot_count", 0)),
            task_count=int(data.get("task_count", 0)),
            success_count=int(data.get("success_count", 0)),
            failure_count=int(data.get("failure_count", 0)),
            retry_count=int(data.get("retry_count", 0)),
            last_task_id=data.get("last_task_id"),
            last_status=data.get("last_status"),
            last_error=data.get("last_error"),
            last_run_at=data.get("last_run_at"),
            directives=list(data.get("directives") or []),
            custom=data.get("custom") or {},
        )


class PersistentAgent:
    """Base class for agents whose state outlives the process."""

    role = "agent"

    def __init__(self, agent_id: str, state_root: Path, memory: AgentMemory | None = None, memory_scope: str | None = None):
        self.agent_id = agent_id
        self.state_dir = ensure_dir(Path(state_root) / agent_id)
        self.state_path = self.state_dir / STATE_FILENAME
        self.memory = memory
        self.memory_scope = memory_scope or agent_id
        self.state = self._load_state()
        self._lock = threading.RLock()
        self.booted = False

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load_state(self) -> AgentState:
        payload = read_json(self.state_path, None)
        if isinstance(payload, dict) and "agent_id" in payload:
            return AgentState.from_dict(payload)
        return AgentState(agent_id=self.agent_id, role=self.role)

    def checkpoint(self) -> Path:
        self.state.updated_at = utc_now_iso()
        atomic_write_json(self.state_path, self.state.to_dict())
        return self.state_path

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def boot(self) -> AgentState:
        """Rehydrate the agent and pull its learned directives from memory."""

        self.state.boot_count += 1
        if self.memory is not None:
            self.state.directives = self.memory.directives(self.memory_scope)
        self.booted = True
        self.checkpoint()
        return self.state

    def shutdown(self) -> None:
        self.booted = False
        self.checkpoint()

    # ------------------------------------------------------------------
    # work
    # ------------------------------------------------------------------
    def handle(self, envelope: TaskEnvelope) -> TaskResult:
        """Subclasses do the real work here."""

        raise NotImplementedError

    def execute(self, envelope: TaskEnvelope) -> TaskResult:
        """Run one attempt, updating persistent counters either way."""

        if not self.booted:
            self.boot()
        started = time.perf_counter()
        with self._lock:
            self.state.task_count += 1
            self.state.last_task_id = envelope.task_id
            self.state.last_run_at = utc_now_iso()
        try:
            result = self.handle(envelope)
        except Exception as exc:
            result = TaskResult(
                task_id=envelope.task_id,
                agent_id=self.agent_id,
                action=envelope.action,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                attempts=envelope.attempt,
            )
        result.duration_ms = (time.perf_counter() - started) * 1000.0
        result.attempts = envelope.attempt
        with self._lock:
            if result.ok:
                self.state.success_count += 1
            else:
                self.state.failure_count += 1
                self.state.last_error = result.error
            self.state.last_status = result.status
            self.checkpoint()
        return result

    def message_handler(self, message: A2AMessage) -> A2AMessage:
        """Adapter so the agent can be registered directly on the A2A bus."""

        payload = dict(message.payload)
        envelope = TaskEnvelope(
            action=payload.pop("action", "run"),
            recipient=self.agent_id,
            task_id=payload.pop("task_id", new_task_id()),
            attempt=int(payload.pop("attempt", 1)),
            payload=payload,
            sender=message.sender,
            session_id=message.session_id,
            conversation_id=message.conversation_id,
        )
        result = self.execute(envelope)
        return message.reply(
            {"result": result.to_dict()},
            message_type=MessageType.RESPONSE if result.ok else MessageType.ERROR,
            sender=self.agent_id,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "booted": self.booted,
            "state_path": str(self.state_path),
            "state": self.state.to_dict(),
        }


class RLMRuntime:
    """Registry + delegation loop for persistent agents."""

    def __init__(
        self,
        root: Path,
        bus: A2ABus,
        trajectory: Trajectory,
        memory: AgentMemory | None = None,
        default_max_retries: int = 2,
    ) -> None:
        self.root = ensure_dir(Path(root))
        self.agents_root = ensure_dir(self.root / "agents")
        self.bus = bus
        self.trajectory = trajectory
        self.memory = memory
        self.default_max_retries = default_max_retries
        self._agents: dict[str, PersistentAgent] = {}
        self._results: dict[str, TaskResult] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # registry
    # ------------------------------------------------------------------
    def register(self, agent: PersistentAgent) -> PersistentAgent:
        with self._lock:
            self._agents[agent.agent_id] = agent
        self.bus.register(agent.agent_id, agent.message_handler)
        return agent

    def unregister(self, agent_id: str) -> bool:
        with self._lock:
            agent = self._agents.pop(agent_id, None)
        if agent is None:
            return False
        agent.shutdown()
        self.bus.unregister(agent_id)
        return True

    def get(self, agent_id: str) -> PersistentAgent | None:
        return self._agents.get(agent_id)

    @property
    def agents(self) -> list[PersistentAgent]:
        return [self._agents[key] for key in sorted(self._agents)]

    def boot_all(self) -> dict[str, AgentState]:
        return {agent.agent_id: agent.boot() for agent in self.agents}

    def checkpoint_all(self) -> None:
        for agent in self.agents:
            agent.checkpoint()

    def shutdown(self) -> None:
        for agent in self.agents:
            agent.shutdown()

    # ------------------------------------------------------------------
    # delegation
    # ------------------------------------------------------------------
    def delegate(self, envelope: TaskEnvelope, max_retries: int | None = None) -> TaskResult:
        """Send a task to an agent, retrying failures up to the retry budget.

        The A2A request/response pair is recorded on the bus for every attempt,
        and each attempt lands in the session trajectory.
        """

        agent = self.get(envelope.recipient)
        if agent is None:
            result = TaskResult(
                task_id=envelope.task_id,
                agent_id=envelope.recipient,
                action=envelope.action,
                status="failed",
                error=f"No agent registered with id {envelope.recipient!r}",
            )
            self.trajectory.record(
                agent_id="runtime", phase="Delegate", action=envelope.action, status="failed",
                detail={"task_id": envelope.task_id, "error": result.error},
            )
            return result

        budget = envelope.max_retries if max_retries is None else max_retries
        attempts = max(1, budget + 1)
        last_result: TaskResult | None = None

        for attempt in range(1, attempts + 1):
            envelope.attempt = attempt
            message = envelope.to_message()
            self.bus.publish(message)
            self.trajectory.record(
                agent_id=envelope.sender,
                phase="Delegate",
                action=f"{envelope.action}->{envelope.recipient}",
                detail={"task_id": envelope.task_id, "attempt": attempt, "message_id": message.message_id},
            )

            result = agent.execute(envelope)
            self._results[envelope.task_id] = result
            self.bus.publish(
                message.reply(
                    {"result": result.to_dict()},
                    message_type=MessageType.RESPONSE if result.ok else MessageType.ERROR,
                    sender=agent.agent_id,
                )
            )
            self.trajectory.record(
                agent_id=agent.agent_id,
                phase="Execute",
                action=envelope.action,
                status=result.status,
                detail={
                    "task_id": envelope.task_id,
                    "attempt": attempt,
                    "duration_ms": round(result.duration_ms, 2),
                    "error": result.error,
                    "issue_count": len(result.issues),
                },
            )
            last_result = result
            if result.ok:
                if attempt > 1:
                    result.status = "recovered"
                return result

            if attempt < attempts:
                agent.state.retry_count += 1
                agent.checkpoint()
                self.trajectory.record(
                    agent_id=agent.agent_id,
                    phase="Fix/Retry",
                    action=f"retry:{envelope.action}",
                    status="retry",
                    detail={"task_id": envelope.task_id, "next_attempt": attempt + 1, "error": result.error},
                )

        assert last_result is not None
        return last_result

    def delegate_many(self, envelopes: Iterable[TaskEnvelope]) -> dict[str, TaskResult]:
        return {envelope.recipient: self.delegate(envelope) for envelope in envelopes}

    def result(self, task_id: str) -> TaskResult | None:
        return self._results.get(task_id)

    def stats(self) -> dict[str, Any]:
        return {
            "agents": [agent.describe() for agent in self.agents],
            "tasks": len(self._results),
            "failed_tasks": sum(1 for result in self._results.values() if not result.ok),
            "bus": self.bus.stats(),
        }
