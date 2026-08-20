"""Shared fixtures: an isolated copy of the project in a temp directory.

Every test runs against copied schemas and copied ``SKILL.md`` files so that
refinement tests can mutate them without touching the repository.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from patient_prime_agent.config import ProjectPaths  # noqa: E402
from patient_prime_agent.agentic.settings import AgentSettings  # noqa: E402

CATEGORY_FOLDERS = {
    "clinical_notes": "Clinical_Notes",
    "cbc": "CBC",
    "ct": "CT",
    "mri": "MRI",
    "ecg": "ECG",
    "eeg": "EEG",
    "genetics": "Genetics",
    "questionnaire": "Questionnaire",
}

SAMPLE_DOCUMENTS = {
    "clinical_notes": """Visit Date: 2026-03-04
Patient ID: PT-0001
Chief Complaint: Recurrent headaches.
History: Patient reports intermittent headaches for three weeks.
Medications: levetiracetam 500 mg twice daily
Allergies: penicillin
Assessment: Focal epilepsy, well controlled.
Plan: Continue current medication and repeat EEG in six months.
""",
    "cbc": """Collection Date: 2026-03-04
Hemoglobin 13.4 g/dL
Hematocrit 40.2 %
RBC 4.6 million/uL
WBC 6.8 10e3/uL
Platelets 245 10e3/uL
MCV 88 fL
MCH 29.1 pg
MCHC 33.2 g/dL
RDW 13.1 %
Neutrophils 58 %
Lymphocytes 31 %
Monocytes 7 %
Eosinophils 3 %
Basophils 1 %
""",
    "ct": """Study Date: 2026-03-11
Exam: CT Head without contrast
Indication: Headache evaluation.
Findings: No acute intracranial hemorrhage. No mass effect.
Impression: Unremarkable non-contrast CT of the brain.
""",
    "mri": """Study Date: 2026-03-15
Exam: MRI Brain with contrast
Indication: Seizure workup.
Findings: Small right temporal lesion is unchanged.
Impression: Stable right temporal lesion.
""",
    "ecg": """Recording Date: 2026-03-09
Heart Rate: 72 bpm
Rhythm: normal sinus rhythm
PR Interval: 158 ms
QRS Duration: 92 ms
QT Interval: 388 ms
QTc: 402 ms
Interpretation: Normal ECG.
""",
    "eeg": """Recording Date: 2026-03-08
Duration: 40 minutes
Background: Well organized posterior dominant rhythm.
Findings: Interictal epileptiform discharges over the right temporal region.
Impression: Abnormal EEG.
""",
    "genetics": """Report Date: 2026-02-10
Test: Epilepsy gene panel
Gene: SCN1A
Variant: c.4933C>T
Classification: pathogenic
Interpretation: Pathogenic SCN1A variant identified.
""",
    "questionnaire": """Date: 2026-03-01
Symptoms: headache, fatigue, dizziness
Onset: Symptoms started three weeks ago.
Duration: Present for three weeks.
Severity: moderate
History: Family history of epilepsy.
Concerns: Patient is worried about seizure recurrence.
""",
}


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, dirs_exist_ok=True)


def build_project(root: Path, categories: list[str] | None = None) -> ProjectPaths:
    """Materialise a self-contained project under ``root``."""

    categories = categories if categories is not None else list(CATEGORY_FOLDERS)
    data_root = root / "patient_data"
    schemas_root = root / "schemas"
    skills_root = root / "skills"

    _copy_tree(REPO_ROOT / "schemas", schemas_root)
    _copy_tree(REPO_ROOT / "patient_prime_agent" / "skills", skills_root)

    for category in categories:
        folder = data_root / CATEGORY_FOLDERS[category]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{category}_sample.txt").write_text(SAMPLE_DOCUMENTS[category], encoding="utf-8")

    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "memory").mkdir(parents=True, exist_ok=True)

    return ProjectPaths(
        root=root,
        data_root=data_root,
        reports_root=root / "reports",
        memory_root=root / "memory",
        skills_root=skills_root,
        schemas_root=schemas_root,
    )


@pytest.fixture
def project(tmp_path: Path) -> ProjectPaths:
    return build_project(tmp_path / "project")


@pytest.fixture
def settings() -> AgentSettings:
    """Offline settings: never load a model during tests."""

    return AgentSettings(enable_llm=False, max_retries=1, refinement_threshold=2, max_workers=4)
