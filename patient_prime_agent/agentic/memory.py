"""Persistent, per-agent structured memory.

This sits on top of the existing :class:`~patient_prime_agent.memory_store.MemoryStore`
(which owns the append-only session log, lessons and issue counters) and adds
addressable, versioned records that agents read back on their next run:

    memory/agent_memory/<scope>.json

``scope`` is an agent id, or ``global`` for cross-agent knowledge.  Records are
the unit that the refinement engine updates when a reusable issue is detected.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from ..utils import atomic_write_json, ensure_dir, read_json, slugify, utc_now_iso

GLOBAL_SCOPE = "global"

KIND_SEMANTIC = "semantic"      # durable facts about the data or the domain
KIND_EPISODIC = "episodic"      # what happened during a specific run
KIND_PROCEDURAL = "procedural"  # directives that change how an agent works


@dataclass(slots=True)
class MemoryRecord:
    key: str
    scope: str
    kind: str
    content: str
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    hits: int = 0
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryRecord":
        return cls(
            key=data["key"],
            scope=data.get("scope", GLOBAL_SCOPE),
            kind=data.get("kind", KIND_SEMANTIC),
            content=data.get("content", ""),
            tags=list(data.get("tags") or []),
            metadata=data.get("metadata") or {},
            version=int(data.get("version", 1)),
            hits=int(data.get("hits", 0)),
            created_at=data.get("created_at") or utc_now_iso(),
            updated_at=data.get("updated_at") or utc_now_iso(),
        )


class AgentMemory:
    """CRUD over versioned memory records, grouped by scope."""

    def __init__(self, root: Path):
        self.root = ensure_dir(Path(root))
        self._lock = threading.RLock()

    def scope_path(self, scope: str) -> Path:
        return self.root / f"{slugify(scope)}.json"

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def write(
        self,
        key: str,
        content: str,
        scope: str = GLOBAL_SCOPE,
        kind: str = KIND_SEMANTIC,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        """Create the record, or bump its version when the content changes."""

        with self._lock:
            records = self._load_scope(scope)
            existing = records.get(key)
            if existing is None:
                record = MemoryRecord(
                    key=key,
                    scope=scope,
                    kind=kind,
                    content=content,
                    tags=list(tags or []),
                    metadata=dict(metadata or {}),
                )
            else:
                record = existing
                if record.content != content:
                    record.version += 1
                record.content = content
                record.kind = kind
                record.updated_at = utc_now_iso()
                if tags:
                    record.tags = sorted(set(record.tags) | set(tags))
                if metadata:
                    record.metadata.update(metadata)
            records[key] = record
            self._save_scope(scope, records)
            return record

    def read(self, key: str, scope: str = GLOBAL_SCOPE, count_hit: bool = True) -> MemoryRecord | None:
        with self._lock:
            records = self._load_scope(scope)
            record = records.get(key)
            if record is None:
                return None
            if count_hit:
                record.hits += 1
                records[key] = record
                self._save_scope(scope, records)
            return record

    def update(self, key: str, scope: str = GLOBAL_SCOPE, **changes: Any) -> MemoryRecord | None:
        with self._lock:
            records = self._load_scope(scope)
            record = records.get(key)
            if record is None:
                return None
            content_changed = "content" in changes and changes["content"] != record.content
            for name, value in changes.items():
                if hasattr(record, name):
                    setattr(record, name, value)
            if content_changed:
                record.version += 1
            record.updated_at = utc_now_iso()
            records[key] = record
            self._save_scope(scope, records)
            return record

    def delete(self, key: str, scope: str = GLOBAL_SCOPE) -> bool:
        with self._lock:
            records = self._load_scope(scope)
            if key not in records:
                return False
            del records[key]
            self._save_scope(scope, records)
            return True

    def list_records(
        self,
        scope: str = GLOBAL_SCOPE,
        kind: str | None = None,
        tag: str | None = None,
    ) -> list[MemoryRecord]:
        records = list(self._load_scope(scope).values())
        if kind is not None:
            records = [record for record in records if record.kind == kind]
        if tag is not None:
            records = [record for record in records if tag in record.tags]
        return sorted(records, key=lambda record: record.key)

    def scopes(self) -> list[str]:
        return sorted(path.stem for path in self.root.glob("*.json"))

    # ------------------------------------------------------------------
    # convenience used by the agents
    # ------------------------------------------------------------------
    def directives(self, scope: str) -> list[str]:
        """Procedural notes an agent should honour on its next run."""

        return [record.content for record in self.list_records(scope, kind=KIND_PROCEDURAL)]

    def remember_issue(self, scope: str, issue_key: str, lesson: str, count: int) -> MemoryRecord:
        return self.write(
            key=f"issue::{issue_key}",
            content=lesson,
            scope=scope,
            kind=KIND_PROCEDURAL,
            tags=["validation", "reusable-issue"],
            metadata={"issue_key": issue_key, "observed_count": count},
        )

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {scope: [record.to_dict() for record in self.list_records(scope)] for scope in self.scopes()}

    # ------------------------------------------------------------------
    # storage
    # ------------------------------------------------------------------
    def _load_scope(self, scope: str) -> dict[str, MemoryRecord]:
        payload = read_json(self.scope_path(scope), {"records": []})
        records: dict[str, MemoryRecord] = {}
        for item in payload.get("records", []):
            try:
                record = MemoryRecord.from_dict(item)
            except KeyError:
                continue
            records[record.key] = record
        return records

    def _save_scope(self, scope: str, records: dict[str, MemoryRecord]) -> None:
        payload = {
            "scope": scope,
            "updated_at": utc_now_iso(),
            "records": [records[key].to_dict() for key in sorted(records)],
        }
        atomic_write_json(self.scope_path(scope), payload)
