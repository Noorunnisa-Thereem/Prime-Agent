"""Medication normalization and dose-conflict detection.

Reads the patient's actual current regimen from the already-generated
``clinical_notes`` summary (``clinical_inference.medication_response.
current_regimen``) -- never a hard-coded example regimen. If a future run's
source data lists the same drug twice with different doses, the conflict
detector below will surface it; today's dataset has one dose per drug, so it
correctly reports zero conflicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_DOSE_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mg|mcg|g|iu|ml)\b\s*(?P<frequency>[a-z0-9./ ]*)",
    re.IGNORECASE,
)
_BRAND_RE = re.compile(r"\s*\([^)]*\)\s*")


@dataclass(slots=True)
class Medication:
    id: str
    normalized_name: str
    source_name: str
    dose_value: float | None
    dose_unit: str | None
    frequency: str | None
    route: str | None
    status: str
    source_reference: str
    reconciliation_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "normalized_name": self.normalized_name,
            "source_name": self.source_name,
            "dose": (
                {"value": self.dose_value, "unit": self.dose_unit}
                if self.dose_value is not None
                else None
            ),
            "frequency": self.frequency,
            "route": self.route,
            "status": self.status,
            "source_reference": self.source_reference,
            "reconciliation_flags": self.reconciliation_flags,
        }


def _normalize_name(source_name: str) -> str:
    without_brand = _BRAND_RE.sub("", source_name).strip()
    return without_brand.lower()


def _parse_dose(dose_text: str | None) -> tuple[float | None, str | None, str | None]:
    if not dose_text:
        return None, None, None
    match = _DOSE_RE.search(dose_text)
    if not match:
        return None, None, dose_text.strip() or None
    value = float(match.group("value"))
    unit = match.group("unit").lower()
    frequency = match.group("frequency").strip().upper() or None
    return value, unit, frequency


def normalize_regimen(
    clinical_notes: dict[str, Any],
    source_reference: str = "clinical_notes.clinical_inference.medication_response.current_regimen",
) -> tuple[list[Medication], list[dict[str, Any]]]:
    """Build normalized :class:`Medication` records plus reconciliation conflicts.

    Only "current" status is produced -- this dataset has no proposed,
    rescue, or discontinued medication list to draw from.
    """

    inference = clinical_notes.get("clinical_inference") if isinstance(clinical_notes, dict) else None
    medication_response = inference.get("medication_response") if isinstance(inference, dict) else None
    raw_regimen = medication_response.get("current_regimen") if isinstance(medication_response, dict) else None
    if not isinstance(raw_regimen, list):
        raw_regimen = []

    by_normalized_name: dict[str, list[dict[str, Any]]] = {}
    for entry in raw_regimen:
        if not isinstance(entry, dict):
            continue
        source_name = str(entry.get("drug") or "").strip()
        if not source_name:
            continue
        normalized = _normalize_name(source_name)
        by_normalized_name.setdefault(normalized, []).append(entry)

    conflicts: list[dict[str, Any]] = []
    medications: list[Medication] = []
    for index, (normalized_name, entries) in enumerate(sorted(by_normalized_name.items()), start=1):
        dose_texts = [str(e.get("dose") or "").strip() for e in entries if e.get("dose")]
        distinct_doses = sorted(set(d for d in dose_texts if d))
        if len(distinct_doses) > 1:
            conflicts.append(
                {
                    "type": "dose_conflict",
                    "drug": normalized_name,
                    "details": f"Source data lists conflicting doses for {normalized_name}: "
                    + ", ".join(distinct_doses),
                    "values": distinct_doses,
                }
            )

        # Use the first entry as the record of note; flag the conflict on it.
        primary = entries[0]
        source_name = str(primary.get("drug") or normalized_name).strip()
        value, unit, frequency = _parse_dose(str(primary.get("dose") or ""))
        flags = ["dose_conflict"] if len(distinct_doses) > 1 else []

        medications.append(
            Medication(
                id=f"med-{index:02d}-{normalized_name.replace(' ', '-')}",
                normalized_name=normalized_name,
                source_name=source_name,
                dose_value=value,
                dose_unit=unit,
                frequency=frequency,
                route=None,
                status="current",
                source_reference=source_reference,
                reconciliation_flags=flags,
            )
        )

    return medications, conflicts
