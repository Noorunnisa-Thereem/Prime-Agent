"""Agent-to-agent (A2A) messaging.

Every message carries a sender agent id, a recipient agent id, a session id and
a conversation id, so a full request/response chain can be replayed from disk.
Messages are appended to ``memory/a2a/<session_id>.jsonl`` as they are sent and
mirrored in an in-process history for fast queries.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from ..utils import ensure_dir, utc_now_iso

BROADCAST = "*"


class MessageType(str, Enum):
    REQUEST = "request"
    RESPONSE = "response"
    ERROR = "error"
    EVENT = "event"
    HEARTBEAT = "heartbeat"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass(slots=True)
class A2AMessage:
    sender: str
    recipient: str
    message_type: MessageType = MessageType.REQUEST
    payload: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    conversation_id: str | None = None
    correlation_id: str | None = None
    message_id: str = field(default_factory=lambda: new_id("msg"))
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["message_type"] = self.message_type.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "A2AMessage":
        return cls(
            sender=data["sender"],
            recipient=data["recipient"],
            message_type=MessageType(data.get("message_type", "request")),
            payload=data.get("payload") or {},
            session_id=data.get("session_id"),
            conversation_id=data.get("conversation_id"),
            correlation_id=data.get("correlation_id"),
            message_id=data.get("message_id") or new_id("msg"),
            timestamp=data.get("timestamp") or utc_now_iso(),
        )

    def reply(
        self,
        payload: dict[str, Any],
        message_type: MessageType = MessageType.RESPONSE,
        sender: str | None = None,
    ) -> "A2AMessage":
        """Build the response message for this request, keeping the thread."""

        return A2AMessage(
            sender=sender or self.recipient,
            recipient=self.sender,
            message_type=message_type,
            payload=payload,
            session_id=self.session_id,
            conversation_id=self.conversation_id,
            correlation_id=self.message_id,
        )


class A2ABus:
    """A persistent, in-process message bus with per-agent inboxes."""

    def __init__(self, root: Path, session_id: str):
        self.root = ensure_dir(Path(root))
        self.session_id = session_id
        self.log_path = self.root / f"{session_id}.jsonl"
        self._history: list[A2AMessage] = []
        self._handlers: dict[str, Callable[[A2AMessage], A2AMessage | None]] = {}
        self._inboxes: dict[str, list[A2AMessage]] = {}
        self._lock = threading.RLock()
        if not self.log_path.exists():
            self.log_path.write_text("", encoding="utf-8")

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------
    def register(self, agent_id: str, handler: Callable[[A2AMessage], A2AMessage | None] | None = None) -> None:
        with self._lock:
            self._inboxes.setdefault(agent_id, [])
            if handler is not None:
                self._handlers[agent_id] = handler

    def unregister(self, agent_id: str) -> None:
        with self._lock:
            self._handlers.pop(agent_id, None)
            self._inboxes.pop(agent_id, None)

    @property
    def registered_agents(self) -> list[str]:
        with self._lock:
            return sorted(self._inboxes)

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------
    def publish(self, message: A2AMessage) -> A2AMessage:
        """Record a message and drop it into the recipient inbox (no dispatch)."""

        if message.session_id is None:
            message.session_id = self.session_id
        if message.conversation_id is None:
            message.conversation_id = new_id("conv")
        with self._lock:
            self._history.append(message)
            self._append_log(message)
            if message.recipient == BROADCAST:
                for inbox in self._inboxes.values():
                    inbox.append(message)
            else:
                self._inboxes.setdefault(message.recipient, []).append(message)
        return message

    def send(self, message: A2AMessage) -> A2AMessage | None:
        """Publish a message and synchronously dispatch it to the handler.

        Returns the handler's reply (also recorded), or ``None`` when the
        recipient has no handler registered.
        """

        self.publish(message)
        with self._lock:
            handler = self._handlers.get(message.recipient)
        if handler is None:
            return None
        try:
            reply = handler(message)
        except Exception as exc:
            error = message.reply(
                {"error": f"{type(exc).__name__}: {exc}"},
                message_type=MessageType.ERROR,
            )
            self.publish(error)
            return error
        if reply is None:
            return None
        if reply.session_id is None:
            reply.session_id = message.session_id
        if reply.conversation_id is None:
            reply.conversation_id = message.conversation_id
        if reply.correlation_id is None:
            reply.correlation_id = message.message_id
        self.publish(reply)
        return reply

    def request(
        self,
        sender: str,
        recipient: str,
        payload: dict[str, Any],
        conversation_id: str | None = None,
    ) -> A2AMessage | None:
        """Convenience wrapper: build a REQUEST and dispatch it."""

        message = A2AMessage(
            sender=sender,
            recipient=recipient,
            message_type=MessageType.REQUEST,
            payload=payload,
            session_id=self.session_id,
            conversation_id=conversation_id or new_id("conv"),
        )
        return self.send(message)

    def emit(self, sender: str, event: str, payload: dict[str, Any], conversation_id: str | None = None) -> A2AMessage:
        """Broadcast a non-addressed event onto the bus."""

        return self.publish(
            A2AMessage(
                sender=sender,
                recipient=BROADCAST,
                message_type=MessageType.EVENT,
                payload={"event": event, **payload},
                session_id=self.session_id,
                conversation_id=conversation_id,
            )
        )

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def inbox(self, agent_id: str, drain: bool = False) -> list[A2AMessage]:
        with self._lock:
            messages = list(self._inboxes.get(agent_id, []))
            if drain:
                self._inboxes[agent_id] = []
        return messages

    def history(
        self,
        agent_id: str | None = None,
        conversation_id: str | None = None,
        message_type: MessageType | None = None,
        limit: int | None = None,
    ) -> list[A2AMessage]:
        with self._lock:
            messages: Iterable[A2AMessage] = list(self._history)
        if agent_id is not None:
            messages = [m for m in messages if m.sender == agent_id or m.recipient in {agent_id, BROADCAST}]
        if conversation_id is not None:
            messages = [m for m in messages if m.conversation_id == conversation_id]
        if message_type is not None:
            messages = [m for m in messages if m.message_type == message_type]
        result = list(messages)
        if limit is not None:
            result = result[-limit:]
        return result

    def conversation(self, conversation_id: str) -> list[A2AMessage]:
        return self.history(conversation_id=conversation_id)

    def load_persisted(self) -> list[A2AMessage]:
        """Read the whole message log back from disk."""

        if not self.log_path.exists():
            return []
        messages: list[A2AMessage] = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(A2AMessage.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
        return messages

    def stats(self) -> dict[str, Any]:
        with self._lock:
            history = list(self._history)
        by_type: dict[str, int] = {}
        for message in history:
            by_type[message.message_type.value] = by_type.get(message.message_type.value, 0) + 1
        return {
            "session_id": self.session_id,
            "total_messages": len(history),
            "by_type": by_type,
            "agents": self.registered_agents,
            "log_path": str(self.log_path),
        }

    def _append_log(self, message: A2AMessage) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message.to_dict(), ensure_ascii=False, default=str) + "\n")
