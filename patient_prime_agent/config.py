from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


CATEGORY_ORDER = (
    "clinical_notes",
    "cbc",
    "ct",
    "mri",
    "ecg",
    "eeg",
    "genetics",
    "questionnaire",
)

CATEGORY_LABELS = {
    "clinical_notes": "Clinical Notes",
    "cbc": "CBC",
    "ct": "CT",
    "mri": "MRI",
    "ecg": "ECG",
    "eeg": "EEG",
    "genetics": "Genetics",
    "questionnaire": "Questionnaire",
}

CATEGORY_ALIASES = {
    "clinicalnotes": "clinical_notes",
    "clinical_note": "clinical_notes",
    "clinicalnotes": "clinical_notes",
    "clinical_notes": "clinical_notes",
    "notes": "clinical_notes",
    "cbc": "cbc",
    "ct": "ct",
    "mri": "mri",
    "ecg": "ecg",
    "eeg": "eeg",
    "genetics": "genetics",
    "questionnaire": "questionnaire",
}

REPORT_FILENAME = "Digital_Twin_Integrated_Report.json"
HARNESS_STATE_FILENAME = "harness_state.json"
ISSUE_COUNTS_FILENAME = "issue_counts.json"
LESSONS_FILENAME = "lessons.jsonl"
SESSION_LOG_FILENAME = "session_log.jsonl"
DEFAULT_OBJECTIVE = "Scan patient files and generate one validated integrated digital twin report."


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    data_root: Path
    reports_root: Path
    memory_root: Path
    skills_root: Path
    schemas_root: Path

    @classmethod
    def discover(cls, root: Path | None = None) -> "ProjectPaths":
        project_root = (root or Path(__file__).resolve().parents[1]).resolve()
        return cls(
            root=project_root,
            data_root=project_root / "patient_data",
            reports_root=project_root / "reports",
            memory_root=project_root / "memory",
            skills_root=project_root / "patient_prime_agent" / "skills",
            schemas_root=project_root / "schemas",
        )

    def category_skill_path(self, category: str) -> Path:
        return self.skills_root / category / "SKILL.md"

    def category_agent_memory_dir(self, category: str) -> Path:
        return self.memory_root / "agents" / category

    def category_schema_path(self, category: str) -> Path:
        return self.schemas_root / f"{category}.schema.json"

    @property
    def integrated_schema_path(self) -> Path:
        return self.schemas_root / "digital_twin_report.schema.json"

    @property
    def report_path(self) -> Path:
        return self.reports_root / REPORT_FILENAME

