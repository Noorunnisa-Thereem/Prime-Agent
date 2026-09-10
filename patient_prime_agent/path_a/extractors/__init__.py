from __future__ import annotations

from .cbc import CBCExtractor
from .clinical_notes import ClinicalNotesExtractor
from .electrophysiology import ECGExtractor, EEGExtractor
from .genetics import GeneticsExtractor
from .imaging import CTExtractor, MRIExtractor
from .questionnaire import QuestionnaireExtractor


CATEGORY_EXTRACTORS = {
    "clinical_notes": ClinicalNotesExtractor,
    "cbc": CBCExtractor,
    "ct": CTExtractor,
    "mri": MRIExtractor,
    "ecg": ECGExtractor,
    "eeg": EEGExtractor,
    "genetics": GeneticsExtractor,
    "questionnaire": QuestionnaireExtractor,
}

