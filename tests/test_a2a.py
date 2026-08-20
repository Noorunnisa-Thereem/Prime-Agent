"""A2A messaging: agent/session ids, threading, history and persistence."""

from __future__ import annotations

import json
from pathlib import Path

from patient_prime_agent.agentic.a2a import BROADCAST, A2ABus, A2AMessage, MessageType


def make_bus(tmp_path: Path, session_id: str = "session-test") -> A2ABus:
    return A2ABus(tmp_path / "a2a", session_id)


def test_message_carries_ids_and_is_round_trippable():
    message = A2AMessage(sender="agent-main", recipient="agent-cbc", payload={"action": "extract"})
    assert message.message_id.startswith("msg-")
    assert message.timestamp
    restored = A2AMessage.from_dict(json.loads(json.dumps(message.to_dict())))
    assert restored.message_id == message.message_id
    assert restored.sender == "agent-main"
    assert restored.message_type is MessageType.REQUEST


def test_publish_stamps_session_and_conversation_ids(tmp_path: Path):
    bus = make_bus(tmp_path, "session-abc")
    message = bus.publish(A2AMessage(sender="agent-main", recipient="agent-ct", payload={}))
    assert message.session_id == "session-abc"
    assert message.conversation_id is not None


def test_request_response_pair_shares_a_conversation_and_correlates(tmp_path: Path):
    bus = make_bus(tmp_path)
    received: list[A2AMessage] = []

    def handler(message: A2AMessage) -> A2AMessage:
        received.append(message)
        return message.reply({"status": "done"})

    bus.register("agent-cbc", handler)
    reply = bus.request("agent-main", "agent-cbc", {"action": "extract", "files": []})

    assert reply is not None
    assert reply.sender == "agent-cbc"
    assert reply.recipient == "agent-main"
    assert reply.message_type is MessageType.RESPONSE
    assert reply.correlation_id == received[0].message_id
    assert reply.conversation_id == received[0].conversation_id


def test_handler_exception_becomes_an_error_message_not_a_crash(tmp_path: Path):
    bus = make_bus(tmp_path)

    def broken(message: A2AMessage) -> A2AMessage:
        raise ValueError("extractor exploded")

    bus.register("agent-eeg", broken)
    reply = bus.request("agent-main", "agent-eeg", {"action": "extract"})

    assert reply is not None
    assert reply.message_type is MessageType.ERROR
    assert "extractor exploded" in reply.payload["error"]


def test_request_to_unregistered_agent_returns_none_but_is_still_logged(tmp_path: Path):
    bus = make_bus(tmp_path)
    assert bus.request("agent-main", "agent-nobody", {"action": "extract"}) is None
    assert len(bus.history()) == 1


def test_inbox_collects_messages_and_can_be_drained(tmp_path: Path):
    bus = make_bus(tmp_path)
    bus.register("agent-mri")
    bus.publish(A2AMessage(sender="agent-main", recipient="agent-mri", payload={"n": 1}))
    bus.publish(A2AMessage(sender="agent-main", recipient="agent-mri", payload={"n": 2}))

    assert len(bus.inbox("agent-mri")) == 2
    assert len(bus.inbox("agent-mri", drain=True)) == 2
    assert bus.inbox("agent-mri") == []


def test_broadcast_events_reach_every_registered_agent(tmp_path: Path):
    bus = make_bus(tmp_path)
    bus.register("agent-ct")
    bus.register("agent-mri")
    bus.emit("agent-main", "run_started", {"session_id": "session-test"})

    assert len(bus.inbox("agent-ct")) == 1
    assert len(bus.inbox("agent-mri")) == 1
    assert bus.inbox("agent-ct")[0].recipient == BROADCAST


def test_history_filters_by_agent_conversation_and_type(tmp_path: Path):
    bus = make_bus(tmp_path)
    bus.register("agent-cbc", lambda message: message.reply({"ok": True}))
    bus.register("agent-ct", lambda message: message.reply({"ok": True}))

    first = bus.request("agent-main", "agent-cbc", {"action": "extract"}, conversation_id="conv-1")
    bus.request("agent-main", "agent-ct", {"action": "extract"}, conversation_id="conv-2")

    assert first is not None
    assert {m.conversation_id for m in bus.conversation("conv-1")} == {"conv-1"}
    assert len(bus.conversation("conv-1")) == 2  # request + response
    assert all(m.sender == "agent-cbc" or m.recipient == "agent-cbc" for m in bus.history(agent_id="agent-cbc"))
    assert len(bus.history(message_type=MessageType.REQUEST)) == 2
    assert len(bus.history(limit=1)) == 1


def test_every_message_is_persisted_and_reloadable(tmp_path: Path):
    bus = make_bus(tmp_path, "session-persist")
    bus.register("agent-ecg", lambda message: message.reply({"ok": True}))
    bus.request("agent-main", "agent-ecg", {"action": "extract"})

    assert bus.log_path.exists()
    persisted = bus.load_persisted()
    assert len(persisted) == 2
    assert [m.message_id for m in persisted] == [m.message_id for m in bus.history()]

    # A fresh bus over the same session reads the same log back.
    reopened = A2ABus(tmp_path / "a2a", "session-persist")
    assert len(reopened.load_persisted()) == 2


def test_stats_report_counts_by_type_and_registered_agents(tmp_path: Path):
    bus = make_bus(tmp_path)
    bus.register("agent-eeg", lambda message: message.reply({"ok": True}))
    bus.request("agent-main", "agent-eeg", {"action": "extract"})
    bus.emit("agent-main", "run_completed", {})

    stats = bus.stats()
    assert stats["total_messages"] == 3
    assert stats["by_type"]["request"] == 1
    assert stats["by_type"]["response"] == 1
    assert stats["by_type"]["event"] == 1
    assert "agent-eeg" in stats["agents"]


def test_unregister_removes_the_agent_from_the_bus(tmp_path: Path):
    bus = make_bus(tmp_path)
    bus.register("agent-genetics", lambda message: message.reply({"ok": True}))
    bus.unregister("agent-genetics")
    assert "agent-genetics" not in bus.registered_agents
    assert bus.request("agent-main", "agent-genetics", {"action": "extract"}) is None
