from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from ..config import ProjectPaths
from ..file_tools import load_document
from ..memory_store import MemoryStore
from ..models import CategoryResult, Evidence, ValidationIssue
from ..refinement import RefinementManager
from ..repair import repair_to_schema
from ..schema_validator import SchemaValidator
from ..skill_store import SkillRegistry
from ..utils import deep_merge_dict, slugify


class CategoryExtractorBase:
    schema_name: str = ""

    def __init__(
        self,
        category: str,
        paths: ProjectPaths,
        validator: SchemaValidator,
        memory: MemoryStore,
        skills: SkillRegistry,
        refiner: RefinementManager,
    ) -> None:
        self.category = category
        self.paths = paths
        self.validator = validator
        self.memory = memory
        self.skills = skills
        self.refiner = refiner
        self.skill_doc = self.skills.load(category)
        self.base_section = self.validator.default(self.schema_name)

    def run(self, files: list[Path]) -> CategoryResult:
        source_files = [str(path) for path in files]
        self.memory.append_event(
            "category_started",
            {"category": self.category, "files": source_files, "skill": str(self.skill_doc.path)},
        )
        self.memory.append_agent_event(
            self.category,
            "category_started",
            {"files": source_files, "skill": str(self.skill_doc.path)},
        )
        section = deepcopy(self.base_section)
        evidence: list[Evidence] = []
        issues: list[ValidationIssue] = []

        for path in files:
            document = load_document(path, self.category)
            self.memory.append_event(
                "file_loaded",
                {"category": self.category, "file": str(path), "notes": document.notes},
            )
            self.memory.append_agent_event(
                self.category,
                "file_loaded",
                {"file": str(path), "notes": document.notes},
            )
            for note in document.notes:
                issues.append(
                    ValidationIssue(
                        schema_name=self.schema_name,
                        path=f"$.files[{path.name}]",
                        message=note,
                        category=self.category,
                        issue_key=f"{self.category}:file-note:{slugify(note)}",
                    )
                )

            partial_section, partial_evidence, partial_issues = self.extract_document(document)
            section = deep_merge_dict(section, partial_section)
            evidence.extend(partial_evidence)
            issues.extend(partial_issues)

        section = repair_to_schema(section, self.schema_name, self.validator)
        section_issues = self.validator.validate(section, self.schema_name)
        issues.extend(section_issues)
        if section_issues:
            outcomes = self.refiner.consider(self.category, self.category, section_issues)
            self.memory.append_event(
                "category_refinement",
                {"category": self.category, "outcomes": [outcome.to_dict() for outcome in outcomes]},
            )
            self.memory.append_agent_event(
                self.category,
                "category_refinement",
                {"outcomes": [outcome.to_dict() for outcome in outcomes]},
            )

        result = CategoryResult(
            category=self.category,
            section=section,
            evidence=evidence,
            source_files=source_files,
            validation_issues=issues,
        )
        self.memory.append_event(
            "category_completed",
            {"category": self.category, "issue_count": len(issues), "evidence_count": len(evidence)},
        )
        self.memory.append_agent_event(
            self.category,
            "category_completed",
            {"issue_count": len(issues), "evidence_count": len(evidence)},
        )
        return result

    def extract_document(self, document: Any) -> tuple[dict[str, Any], list[Evidence], list[ValidationIssue]]:
        raise NotImplementedError

    def _evidence(
        self,
        field: str,
        document_path: Path,
        source_lines: list[int] | None = None,
        source_excerpt: str = "",
        section: str | None = None,
        confidence: float = 0.8,
    ) -> Evidence:
        return Evidence(
            section=section or self.category,
            field=field,
            source_file=str(document_path),
            source_lines=source_lines or [],
            source_excerpt=source_excerpt,
            confidence=confidence,
        )
