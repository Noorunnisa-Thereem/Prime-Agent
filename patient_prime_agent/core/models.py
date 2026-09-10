from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Evidence:
    section: str
    field: str
    source_file: str
    source_lines: list[int] = field(default_factory=list)
    source_excerpt: str = ""
    confidence: float = 0.8

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ValidationIssue:
    schema_name: str
    path: str
    message: str
    category: str | None = None
    severity: str = "error"
    issue_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PlanStep:
    order: int
    category: str
    action: str
    status: str = "pending"
    file_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CategoryResult:
    category: str
    section: dict[str, Any]
    evidence: list[Evidence]
    source_files: list[str]
    validation_issues: list[ValidationIssue] = field(default_factory=list)
    retries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "section": self.section,
            "evidence": [item.to_dict() for item in self.evidence],
            "source_files": self.source_files,
            "validation_issues": [item.to_dict() for item in self.validation_issues],
            "retries": self.retries,
        }


@dataclass(slots=True)
class HarnessState:
    objective: str
    run_count: int = 0
    last_run_at: str | None = None
    last_report_path: str | None = None
    last_status: str | None = None
    last_category_count: int = 0
    last_issue_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_objective: str) -> "HarnessState":
        return cls(
            objective=data.get("objective") or default_objective,
            run_count=int(data.get("run_count", 0)),
            last_run_at=data.get("last_run_at"),
            last_report_path=data.get("last_report_path"),
            last_status=data.get("last_status"),
            last_category_count=int(data.get("last_category_count", 0)),
            last_issue_count=int(data.get("last_issue_count", 0)),
        )


@dataclass(slots=True)
class LoadedDocument:
    path: Path
    category: str
    text: str
    lines: list[str]
    structured: Any | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "category": self.category,
            "text": self.text,
            "lines": self.lines,
            "structured": self.structured,
            "notes": self.notes,
        }

