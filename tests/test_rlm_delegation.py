"""RLM runtime: persistent agents, delegation, retries and trajectories."""

from __future__ import annotations

from pathlib import Path

from patient_prime_agent.agentic.a2a import A2ABus, MessageType
from patient_prime_agent.agentic.memory import AgentMemory
from patient_prime_agent.agentic.runtime import (
    PersistentAgent,
    RLMRuntime,
    TaskEnvelope,
    TaskResult,
)
from patient_prime_agent.agentic.session import SessionStore


class CountingAgent(PersistentAgent):
    """Fails ``fail_times`` attempts, then succeeds."""

    role = "test-agent"

    def __init__(self, agent_id: str, state_root: Path, fail_times: int = 0, **kwargs):
        super().__init__(agent_id, state_root, **kwargs)
        self.fail_times = fail_times
        self.calls = 0

    def handle(self, envelope: TaskEnvelope) -> TaskResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return TaskResult(
                task_id=envelope.task_id,
                agent_id=self.agent_id,
                action=envelope.action,
                status="failed",
                error=f"attempt {self.calls} failed",
            )
        return TaskResult(
            task_id=envelope.task_id,
            agent_id=self.agent_id,
            action=envelope.action,
            output={"calls": self.calls, "echo": envelope.payload},
        )


class ExplodingAgent(PersistentAgent):
    role = "test-agent"

    def handle(self, envelope: TaskEnvelope) -> TaskResult:
        raise RuntimeError("boom")


def build_runtime(tmp_path: Path, max_retries: int = 2) -> tuple[RLMRuntime, Path]:
    sessions = SessionStore(tmp_path / "sessions")
    session = sessions.create("test objective", session_id="session-rlm")
    bus = A2ABus(tmp_path / "a2a", session.session_id)
    trajectory = sessions.trajectory(session.session_id)
    memory = AgentMemory(tmp_path / "agent_memory")
    runtime = RLMRuntime(
        root=tmp_path / "runtime",
        bus=bus,
        trajectory=trajectory,
        memory=memory,
        default_max_retries=max_retries,
    )
    return runtime, tmp_path / "runtime" / "agents"


# ----------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------
def test_registering_an_agent_puts_it_on_the_bus(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    agent = runtime.register(CountingAgent("agent-a", state_root))
    assert runtime.get("agent-a") is agent
    assert "agent-a" in runtime.bus.registered_agents


def test_unregister_removes_and_checkpoints_the_agent(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    runtime.register(CountingAgent("agent-a", state_root))
    assert runtime.unregister("agent-a") is True
    assert runtime.get("agent-a") is None
    assert runtime.unregister("agent-a") is False


def test_boot_all_boots_every_registered_agent(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    runtime.register(CountingAgent("agent-a", state_root))
    runtime.register(CountingAgent("agent-b", state_root))
    states = runtime.boot_all()
    assert set(states) == {"agent-a", "agent-b"}
    assert all(state.boot_count == 1 for state in states.values())


# ----------------------------------------------------------------------
# delegation
# ----------------------------------------------------------------------
def test_delegation_runs_the_agent_and_returns_its_output(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    runtime.register(CountingAgent("agent-a", state_root))
    result = runtime.delegate(TaskEnvelope(action="work", recipient="agent-a", payload={"x": 1}))
    assert result.ok
    assert result.output["echo"] == {"x": 1}
    assert result.attempts == 1


def test_delegation_to_an_unknown_agent_fails_cleanly(tmp_path: Path):
    runtime, _ = build_runtime(tmp_path)
    result = runtime.delegate(TaskEnvelope(action="work", recipient="agent-missing"))
    assert not result.ok
    assert "No agent registered" in (result.error or "")


def test_delegation_records_request_and_response_on_the_bus(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    runtime.register(CountingAgent("agent-a", state_root))
    runtime.delegate(TaskEnvelope(action="work", recipient="agent-a"))

    history = runtime.bus.history(agent_id="agent-a")
    assert [m.message_type for m in history] == [MessageType.REQUEST, MessageType.RESPONSE]
    assert history[1].correlation_id == history[0].message_id


def test_delegation_writes_plan_and_execute_steps_into_the_trajectory(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    runtime.register(CountingAgent("agent-a", state_root))
    runtime.delegate(TaskEnvelope(action="work", recipient="agent-a"))

    phases = [step.phase for step in runtime.trajectory.steps]
    assert "Delegate" in phases
    assert "Execute" in phases


# ----------------------------------------------------------------------
# retries
# ----------------------------------------------------------------------
def test_a_transient_failure_is_retried_and_reported_as_recovered(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    agent = runtime.register(CountingAgent("agent-a", state_root, fail_times=1))
    result = runtime.delegate(TaskEnvelope(action="work", recipient="agent-a", max_retries=2))

    assert result.status == "recovered"
    assert result.attempts == 2
    assert agent.state.retry_count == 1
    assert any(step.phase == "Fix/Retry" for step in runtime.trajectory.steps)


def test_retry_budget_is_respected_and_exhaustion_fails(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    agent = runtime.register(CountingAgent("agent-a", state_root, fail_times=99))
    result = runtime.delegate(TaskEnvelope(action="work", recipient="agent-a", max_retries=2))

    assert not result.ok
    assert agent.calls == 3  # first attempt + 2 retries
    assert agent.state.failure_count == 3


def test_an_exception_inside_an_agent_is_captured_as_a_failed_result(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    runtime.register(ExplodingAgent("agent-boom", state_root))
    result = runtime.delegate(TaskEnvelope(action="work", recipient="agent-boom", max_retries=0))

    assert not result.ok
    assert "RuntimeError: boom" in (result.error or "")


def test_delegate_many_fans_out_over_several_agents(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    runtime.register(CountingAgent("agent-a", state_root))
    runtime.register(CountingAgent("agent-b", state_root))
    results = runtime.delegate_many(
        [
            TaskEnvelope(action="work", recipient="agent-a"),
            TaskEnvelope(action="work", recipient="agent-b"),
        ]
    )
    assert set(results) == {"agent-a", "agent-b"}
    assert all(result.ok for result in results.values())


# ----------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------
def test_agent_state_survives_process_restart(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    runtime.register(CountingAgent("agent-a", state_root))
    runtime.delegate(TaskEnvelope(action="work", recipient="agent-a"))
    runtime.shutdown()

    # A brand-new object over the same state directory rehydrates the counters.
    revived = CountingAgent("agent-a", state_root)
    assert revived.state.task_count == 1
    assert revived.state.success_count == 1
    assert revived.state.last_status == "ok"
    assert revived.state_path.exists()


def test_boot_count_increases_across_runs(tmp_path: Path):
    _, state_root = build_runtime(tmp_path)
    CountingAgent("agent-a", state_root).boot()
    assert CountingAgent("agent-a", state_root).boot().boot_count == 2


def test_boot_pulls_learned_directives_from_memory(tmp_path: Path):
    _, state_root = build_runtime(tmp_path)
    memory = AgentMemory(tmp_path / "agent_memory")
    memory.remember_issue("agent-a", "cbc:$.hemoglobin:type-mismatch", "Keep nulls for absent metrics.", 2)

    agent = CountingAgent("agent-a", state_root, memory=memory, memory_scope="agent-a")
    state = agent.boot()
    assert state.directives == ["Keep nulls for absent metrics."]


def test_message_handler_lets_an_agent_be_driven_purely_over_a2a(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    agent = runtime.register(CountingAgent("agent-a", state_root))
    reply = runtime.bus.request("agent-main", "agent-a", {"action": "work", "value": 7})

    assert reply is not None
    assert reply.message_type is MessageType.RESPONSE
    assert reply.payload["result"]["status"] == "ok"
    assert agent.state.task_count == 1


def test_runtime_stats_summarise_agents_tasks_and_bus(tmp_path: Path):
    runtime, state_root = build_runtime(tmp_path)
    runtime.register(CountingAgent("agent-a", state_root))
    runtime.delegate(TaskEnvelope(action="work", recipient="agent-a"))

    stats = runtime.stats()
    assert stats["tasks"] == 1
    assert stats["failed_tasks"] == 0
    assert stats["bus"]["total_messages"] >= 2
    assert stats["agents"][0]["agent_id"] == "agent-a"
