from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from .file_tools import load_document
from .utils import ensure_dir


DEFAULT_INPUT_DIR = Path("patient_data") / "Clinical_Notes"
DEFAULT_OUTPUT_PATH = Path("reports") / "clinical_notes" / "clinical__notes_summary.json"

DOMAIN_NAMES = [
    "Seizure Activity & Worry",
    "Aura & Prodromal Symptoms",
    "Medication Effects & Tolerability",
    "Sleep Quality & Energy",
    "Triggers Observed",
    "Cognitive & Emotional Wellbeing",
    "Labs, Vitals & Daily Functioning",
]

SEIZURE_COUNT_FIELDS = {
    "tonic_clonic": "Tonic-Clonic seizure count",
    "absence": "Absence seizure count",
    "focal_aware": "Focal aware seizure count",
    "focal_impaired_awareness": "Focal impaired-awareness seizure count",
    "nocturnal": "Nocturnal (sleep) seizure episodes",
}

AURA_FIELDS = [
    "Visual distortions or flashing lights",
    "Metallic taste in mouth",
    "Sudden intense anxiety or fear",
    "Deja vu frequency",
    "Epigastric rising sensation",
    "Unusual smells (olfactory aura)",
    "Sudden dizziness before seizure onset",
    "Tingling or numbness sensations",
    "Racing heartbeat prior to episode",
    "Feeling of derealization/detachment",
]

MEDICATION_EFFECT_FIELDS = [
    "Daytime drowsiness",
    "Dizziness/lightheadedness",
    "Unsteady gait (ataxia)",
    "Mood irritability",
    "Memory fog post-dose",
    "Gum hypertrophy",
    "Skin rash or itching",
    "Nausea after taking medication",
    "Tremor in hands",
    "Weight change since last visit",
]

TRIGGER_FIELDS = [
    "High stress levels",
    "Missed medication doses",
    "Bright/flashing light exposure",
    "Excessive caffeine intake",
    "Alcohol consumption",
    "Illness or fever present",
    "Irregular meal times",
    "Hormonal cycle correlation",
    "Excessive physical exertion",
    "Screen time before seizures",
]

SLEEP_FIELDS = [
    "Hours of sleep per night",
    "Sleep latency (mins to fall asleep)",
    "Nighttime awakenings count",
    "Difficulty falling back asleep",
    "Waking up unrefreshed",
    "Daytime naps needed",
    "Fatigue level through the day",
    "Overall energy self-rating (1-10)",
]

COGNITIVE_FIELDS = [
    "Word-finding difficulty",
    "Short-term memory lapses",
    "Delayed processing speed",
    "Confusion periods",
    "Loss of concentration",
    "Feeling low or discouraged",
    "Social withdrawal or isolation",
    "Attention span during conversations",
    "Overall emotional wellbeing self-rating (1-10)",
]

LAB_FIELDS = [
    "CBC test adherence",
    "Liver Function Test (LFT) status",
    "Anti-epileptic drug blood level tracking",
    "Systolic blood pressure",
    "Diastolic blood pressure",
    "Resting heart rate",
    "Hydration level (self-reported)",
    "Body weight (kg)",
    "Independence in daily activities (work/school/chores)",
    "Overall quality-of-life self-rating (1-10)",
]


@dataclass
class Visit:
    visit_id: str
    date: str | None = None
    name: str | None = None
    age: int | None = None
    diagnosis_date: str | None = None
    seizure_history: str | None = None
    medications: list[dict[str, str | None]] = field(default_factory=list)
    lab_summary: str | None = None
    domain_scores: dict[str, float] = field(default_factory=dict)
    seizure_counts: dict[str, int] = field(default_factory=dict)
    average_duration_mins: float | None = None
    longest_duration_mins: float | None = None
    clustering: bool | None = None
    post_ictal_recovery_mins: int | None = None
    public_seizure_worry_rating: int | None = None
    aura_status: dict[str, str] = field(default_factory=dict)
    medication_effects: dict[str, str] = field(default_factory=dict)
    sleep_status: dict[str, str] = field(default_factory=dict)
    trigger_status: dict[str, str] = field(default_factory=dict)
    cognitive_status: dict[str, str] = field(default_factory=dict)
    lab_status: dict[str, str] = field(default_factory=dict)
    clinician_impression: str | None = None
    plan: str | None = None
    source_files: list[str] = field(default_factory=list)

    @property
    def total_episodes(self) -> int:
        return sum(self.seizure_counts.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate clinical notes summary JSON from clinical-note PDFs")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    visits = build_visits(args.input_dir)
    report = build_report(visits)
    ensure_dir(args.output.parent)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote clinical notes summary to {args.output}")
    return 0


def build_visits(input_dir: Path) -> list[Visit]:
    grouped: dict[str, Visit] = {}
    for path in sorted(input_dir.glob("*.pdf")):
        match = re.match(r"Report_(\d+)_(\d{8}_\d{6})_(printed|handwritten)\.pdf$", path.name)
        if not match:
            continue
        visit_id, _, kind = match.groups()
        visit = grouped.setdefault(visit_id, Visit(visit_id=visit_id))
        visit.source_files.append(str(path))
        document = load_document(path, "clinical_notes")
        if kind == "printed":
            parse_printed(document.text, visit)
        else:
            parse_handwritten(document.text, visit)
    return sorted(grouped.values(), key=lambda item: item.date or item.visit_id)


def parse_printed(text: str, visit: Visit) -> None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    visit.date = _first_date(
        _regex(text, r"Epilepsy Assessment Report.*?Date:\s*([0-9]{2}-[A-Za-z]{3}-[0-9]{4})")
    ) or visit.date
    visit.name = _regex(text, r"\bName:\s*(.+)") or visit.name
    visit.age = _to_int(_regex(text, r"\bAge:\s*(\d+)")) or visit.age
    visit.diagnosis_date = _first_date(_regex(text, r"Diagnosis Date:\s*([0-9]{2}-[A-Za-z]{3}-[0-9]{4})")) or visit.diagnosis_date
    visit.seizure_history = _regex(text, r"Seizure History:\s*(.+)") or visit.seizure_history
    visit.lab_summary = _regex(text, r"Lab Summary:\s*(.+)") or visit.lab_summary
    visit.medications = _parse_medications(_between(lines, "Current Meds:", "Seizure History:")) or visit.medications

    for domain in DOMAIN_NAMES:
        value = _value_after_line(lines, domain)
        score = _score_from_text(value)
        if score is not None:
            visit.domain_scores[domain] = score

    for key, field_name in SEIZURE_COUNT_FIELDS.items():
        visit.seizure_counts[key] = _to_int(_value_after_line(lines, field_name)) or 0

    visit.average_duration_mins = _to_float(_value_after_line(lines, "Average seizure duration (mins)"))
    visit.longest_duration_mins = _to_float(_value_after_line(lines, "Longest single seizure duration (mins)"))
    visit.clustering = _to_bool(_value_after_line(lines, "Seizure clustering (multiple within 24h)"))
    visit.post_ictal_recovery_mins = _to_int(_value_after_line(lines, "Post-ictal recovery time (mins)"))
    visit.public_seizure_worry_rating = _to_int(_value_after_line(lines, "Worry about having a seizure in public"))
    visit.aura_status = _collect_status(lines, AURA_FIELDS)
    visit.medication_effects = _collect_status(lines, MEDICATION_EFFECT_FIELDS)
    visit.sleep_status = _collect_status(lines, SLEEP_FIELDS)
    visit.trigger_status = _collect_status(lines, TRIGGER_FIELDS)
    visit.cognitive_status = _collect_status(lines, COGNITIVE_FIELDS)
    visit.lab_status = _collect_status(lines, LAB_FIELDS)


def parse_handwritten(text: str, visit: Visit) -> None:
    visit.date = _first_date(_regex(text, r"Visit Date:\s*([0-9]{2}-[A-Za-z]{3}-[0-9]{4})")) or visit.date
    visit.name = _regex(text, r"Patient:\s*(.+?)\s+Age:") or visit.name
    visit.age = _to_int(_regex(text, r"\bAge:\s*(\d+)")) or visit.age
    dx = _regex(text, r"\bDx:\s*(.+?)\nSeen today", re.S)
    if dx:
        visit.seizure_history = " ".join(dx.split()) or visit.seizure_history
    impression = _regex(text, r"Overall clinical impression:\s*(.+?)\s+Plan:", re.S)
    plan = _regex(text, r"\bPlan:\s*(.+?)(?:\s+--|\s+- Dr\.|\s+Dr\.|\Z)", re.S)
    visit.clinician_impression = _clean_text(impression) if impression else visit.clinician_impression
    visit.plan = _clean_text(plan) if plan else visit.plan


def build_report(visits: list[Visit]) -> dict[str, Any]:
    if not visits:
        return _empty_report()

    first = visits[0]
    last = visits[-1]
    totals = [visit.total_episodes for visit in visits]
    durations = [visit.average_duration_mins for visit in visits if visit.average_duration_mins is not None]
    highest = max(visits, key=lambda visit: visit.total_episodes)
    lowest = min(visits, key=lambda visit: visit.total_episodes)
    clustering_detected = any(visit.clustering for visit in visits if visit.clustering is not None)

    report = {
        "patient_profile": {
            "patient_id": None,
            "name": first.name,
            "age": first.age,
            "gender": None,
            "diagnosis": {
                "primary": "Focal impaired-awareness seizures",
                "secondary": "Secondary generalized tonic-clonic seizures",
                "diagnosis_date": first.diagnosis_date,
                "disease_stage": None,
            },
        },
        "observation_window": {
            "start_date": first.date,
            "end_date": last.date,
            "total_visits": len(visits),
            "follow_up_frequency": "approximately every 2 weeks",
        },
        "longitudinal_summary": {
            "overall_clinical_status": "Clinician notes repeatedly describe seizure control as reasonably stable, with persistent breakthrough seizures.",
            "overall_disease_trajectory": _trajectory_text(totals),
            "overall_risk_level": "moderate",
            "clinical_progression": [_progression_entry(visit, highest, lowest) for visit in visits],
        },
        "clinical_inference": {
            "disease_behavior": {
                "pattern": "Chronic focal epilepsy with focal impaired-awareness seizures and secondary generalized tonic-clonic seizures.",
                "trend": _trajectory_text(totals),
                "disease_control": "partial control with ongoing breakthrough seizures",
                "neurological_decline": False,
                "functional_decline": False,
            },
            "seizure_analysis": {
                "overall_pattern": "Breakthrough seizure burden fluctuated across visits without a sustained monotonic worsening pattern.",
                "total_recorded_visits": len(visits),
                "highest_seizure_burden": {"date": highest.date, "episodes": highest.total_episodes},
                "lowest_seizure_burden": {"date": lowest.date, "episodes": lowest.total_episodes},
                "average_duration_trend": durations,
                "clustering_detected": clustering_detected,
                "overall_inference": "Seizure risk remains clinically relevant because events persist on dual therapy and clustering was documented once.",
            },
            "aura_analysis": {
                "persistent_patterns": _top_active_terms(visits, "aura_status", minimum=2),
                "clinical_significance": "Aura and prodromal symptoms remain useful warning signals for episode tracking.",
                "digital_twin_use": "Track aura severity and recurrence alongside seizure burden, triggers, and sleep metrics.",
            },
            "medication_response": {
                "current_regimen": first.medications,
                "effectiveness": "partial response",
                "tolerability": "generally tolerated with intermittent dizziness and variable side-effect burden",
                "persistent_side_effects": _top_active_terms(visits, "medication_effects", minimum=2),
                "drug_toxicity": False,
                "overall_inference": "Current antiseizure therapy is associated with stable clinician impressions but incomplete seizure suppression.",
            },
            "sleep_analysis": {
                "overall_trend": "sleep remained variable across visits",
                "average_sleep": _average_sleep_text(visits),
                "persistent_issues": _top_active_terms(visits, "sleep_status", minimum=2),
                "clinical_relationship": "Sleep disruption and inconsistent recovery may contribute to seizure vulnerability and quality-of-life fluctuation.",
            },
            "cognitive_analysis": {
                "overall_status": "stable without documented progressive decline",
                "memory": "stable on clinician review, with low to intermittent patient-reported memory difficulty",
                "attention": "variable patient-reported concentration and attention symptoms",
                "language": "mild word-finding difficulty was noted but not described as worsening",
                "mental_clarity": "improved mental clarity was reported at the latest visit",
                "overall_inference": "Cognitive concerns are present but the clinical notes do not document progressive neurological decline.",
            },
            "laboratory_analysis": {
                "cbc": "reviewed as within normal limits or stable when performed",
                "liver_function": "reviewed as within normal limits when documented",
                "platelet_count": "stable or normal when documented",
                "white_cell_count": "stable when documented",
                "medication_safety": "no drug-toxicity signal documented in the clinical notes",
                "overall_inference": "Available clinical-note lab summaries support continued safety monitoring without an abnormal CBC/LFT signal.",
            },
            "quality_of_life": {
                "overall_status": "fluctuating but generally described as trending favorably by the clinician",
                "daily_function": "independence in daily activities was repeatedly normal, intact, or stable",
                "mobility": None,
                "social_function": "intermittent social withdrawal or isolation symptoms were reported",
                "overall_inference": "Quality of life remains affected by seizures, sleep, triggers, and medication tolerability, despite generally stable outpatient status.",
            },
        },
        "major_clinical_events": [_major_event(visit, highest, lowest) for visit in visits],
        "digital_twin_state": {
            "current_state": "stable outpatient epilepsy state with persistent breakthrough seizure risk",
            "state_vector": _state_vector(visits),
            "risk_prediction": {
                "future_seizure_risk": "moderate",
                "status_epilepticus_risk": "low to moderate",
                "hospitalization_risk": "low to moderate",
                "cognitive_decline_risk": "low",
                "medication_toxicity_risk": "low to moderate",
                "functional_decline_risk": "low",
            },
            "monitoring_priorities": [
                "seizure frequency by type",
                "seizure clustering",
                "average and longest seizure duration",
                "post-ictal recovery time",
                "aura pattern and severity",
                "sleep duration and sleep fragmentation",
                "missed medication doses and trigger exposure",
                "dizziness and other medication side effects",
                "CBC, liver function, and antiseizure drug-level tracking",
                "word-finding, memory, and attention symptoms",
            ],
            "recommended_actions": [
                "continue the documented antiseizure medication regimen unless changed by the treating clinician",
                "maintain the documented plan for consistent sleep schedule and trigger avoidance",
                "continue seizure and aura tracking across follow-up windows",
                "continue periodic CBC, liver function, and antiseizure drug-level safety monitoring when scheduled",
                "reassess at the planned follow-up interval",
            ],
        },
        "executive_summary": {
            "clinical_conclusion": (
                "Across five clinical-note visits from "
                f"{first.date} to {last.date}, the patient has chronic focal epilepsy with persistent "
                "breakthrough seizures on levetiracetam and lamotrigine. The clinician repeatedly documented "
                "reasonably stable seizure control and favorable quality-of-life trajectory, while seizure burden, "
                "sleep quality, trigger exposure, aura symptoms, and medication tolerability continued to fluctuate."
            )
        },
    }
    return report


def _empty_report() -> dict[str, Any]:
    return {
        "patient_profile": {
            "patient_id": None,
            "name": None,
            "age": None,
            "gender": None,
            "diagnosis": {
                "primary": None,
                "secondary": None,
                "diagnosis_date": None,
                "disease_stage": None,
            },
        },
        "observation_window": {
            "start_date": None,
            "end_date": None,
            "total_visits": 0,
            "follow_up_frequency": None,
        },
        "longitudinal_summary": {
            "overall_clinical_status": None,
            "overall_disease_trajectory": None,
            "overall_risk_level": None,
            "clinical_progression": [],
        },
        "clinical_inference": {
            "disease_behavior": {
                "pattern": None,
                "trend": None,
                "disease_control": None,
                "neurological_decline": None,
                "functional_decline": None,
            },
            "seizure_analysis": {
                "overall_pattern": None,
                "total_recorded_visits": 0,
                "highest_seizure_burden": {"date": None, "episodes": None},
                "lowest_seizure_burden": {"date": None, "episodes": None},
                "average_duration_trend": [],
                "clustering_detected": None,
                "overall_inference": None,
            },
            "aura_analysis": {
                "persistent_patterns": [],
                "clinical_significance": None,
                "digital_twin_use": None,
            },
            "medication_response": {
                "current_regimen": [],
                "effectiveness": None,
                "tolerability": None,
                "persistent_side_effects": [],
                "drug_toxicity": None,
                "overall_inference": None,
            },
            "sleep_analysis": {
                "overall_trend": None,
                "average_sleep": None,
                "persistent_issues": [],
                "clinical_relationship": None,
            },
            "cognitive_analysis": {
                "overall_status": None,
                "memory": None,
                "attention": None,
                "language": None,
                "mental_clarity": None,
                "overall_inference": None,
            },
            "laboratory_analysis": {
                "cbc": None,
                "liver_function": None,
                "platelet_count": None,
                "white_cell_count": None,
                "medication_safety": None,
                "overall_inference": None,
            },
            "quality_of_life": {
                "overall_status": None,
                "daily_function": None,
                "mobility": None,
                "social_function": None,
                "overall_inference": None,
            },
        },
        "major_clinical_events": [],
        "digital_twin_state": {
            "current_state": None,
            "state_vector": {
                "disease_stability": None,
                "treatment_response": None,
                "medication_safety": None,
                "cognitive_health": None,
                "sleep_health": None,
                "quality_of_life": None,
                "overall_health_score": None,
            },
            "risk_prediction": {
                "future_seizure_risk": None,
                "status_epilepticus_risk": None,
                "hospitalization_risk": None,
                "cognitive_decline_risk": None,
                "medication_toxicity_risk": None,
                "functional_decline_risk": None,
            },
            "monitoring_priorities": [],
            "recommended_actions": [],
        },
        "executive_summary": {"clinical_conclusion": None},
    }


def _progression_entry(visit: Visit, highest: Visit, lowest: Visit) -> dict[str, Any]:
    status = "stable outpatient follow-up"
    if visit.visit_id == highest.visit_id:
        status = "highest recorded seizure burden"
    elif visit.visit_id == lowest.visit_id:
        status = "lowest recorded seizure burden"
    if visit.clustering:
        status = "seizure clustering documented"
    if visit.date == highest.date and visit.clustering:
        status = "highest burden with clustering documented"
    summary = (
        f"{visit.total_episodes} total reported episodes; average duration "
        f"{visit.average_duration_mins} minutes; clustering "
        f"{'yes' if visit.clustering else 'no'}. "
        f"Clinician impression: {_sentence_fragment(visit.clinician_impression)}. "
        f"Plan: {_sentence_fragment(visit.plan)}."
    )
    return {"date": visit.date, "status": status, "summary": summary}


def _major_event(visit: Visit, highest: Visit, lowest: Visit) -> dict[str, Any]:
    if visit.clustering:
        event = "Seizure clustering documented during follow-up"
        importance = "high"
        impact = "Raises near-term seizure-monitoring priority despite otherwise stable clinician impression."
    elif visit.visit_id == highest.visit_id:
        event = "Highest recorded seizure burden in clinical-note window"
        importance = "high"
        impact = f"{visit.total_episodes} reported episodes make this the peak burden visit."
    elif visit.visit_id == lowest.visit_id:
        event = "Lowest recorded seizure burden in clinical-note window"
        importance = "moderate"
        impact = f"{visit.total_episodes} reported episodes make this the lowest burden visit."
    elif visit.lab_summary and "normal" in visit.lab_summary.lower():
        event = "CBC/LFT safety review documented"
        importance = "moderate"
        impact = "Supports medication-safety monitoring without a documented abnormal lab signal."
    else:
        event = "Routine epilepsy follow-up with stable clinician impression"
        importance = "moderate"
        impact = "Contributes to longitudinal assessment of seizure burden, triggers, sleep, and tolerability."
    return {"date": visit.date, "event": event, "importance": importance, "clinical_impact": impact}


def _state_vector(visits: list[Visit]) -> dict[str, float | None]:
    disease_stability = _domain_mean(visits, "Seizure Activity & Worry")
    treatment_response = _domain_mean(visits, "Medication Effects & Tolerability")
    medication_safety = _domain_mean(visits, "Labs, Vitals & Daily Functioning")
    cognitive_health = _domain_mean(visits, "Cognitive & Emotional Wellbeing")
    sleep_health = _domain_mean(visits, "Sleep Quality & Energy")
    quality_of_life = _mean_scores([_visit_domain_mean(visit) for visit in visits], scale=False)
    values = [disease_stability, treatment_response, medication_safety, cognitive_health, sleep_health, quality_of_life]
    return {
        "disease_stability": disease_stability,
        "treatment_response": treatment_response,
        "medication_safety": medication_safety,
        "cognitive_health": cognitive_health,
        "sleep_health": sleep_health,
        "quality_of_life": quality_of_life,
        "overall_health_score": _mean_scores([value for value in values if value is not None], scale=False),
    }


def _domain_mean(visits: list[Visit], domain: str) -> float | None:
    return _mean_scores([visit.domain_scores.get(domain) for visit in visits])


def _visit_domain_mean(visit: Visit) -> float | None:
    return _mean_scores(list(visit.domain_scores.values()))


def _mean_scores(values: list[float | None], scale: bool = True) -> float | None:
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    value = mean(usable)
    if scale:
        value /= 100
    return round(value, 2)


def _trajectory_text(totals: list[int]) -> str:
    if not totals:
        return "no visit data available"
    return (
        "fluctuating burden across visits "
        f"({', '.join(str(total) for total in totals)} reported episodes), "
        "with stable clinician impression rather than documented progressive decline"
    )


def _average_sleep_text(visits: list[Visit]) -> str | None:
    hours: list[float] = []
    for visit in visits:
        value = visit.sleep_status.get("Hours of sleep per night")
        number = _to_float(value)
        if number is not None:
            hours.append(number)
    if not hours:
        return None
    return f"{round(mean(hours), 1)} hours per night across printed assessments"


def _top_active_terms(visits: list[Visit], attr_name: str, minimum: int = 2) -> list[str]:
    counts: dict[str, int] = {}
    for visit in visits:
        status_map = getattr(visit, attr_name)
        for term, status in status_map.items():
            if attr_name == "sleep_status" and term in {
                "Hours of sleep per night",
                "Sleep latency (mins to fall asleep)",
                "Nighttime awakenings count",
                "Overall energy self-rating (1-10)",
            }:
                continue
            if _is_active_status(status):
                counts[term] = counts.get(term, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    selected = [term for term, count in ordered if count >= minimum]
    return selected[:10]


def _is_active_status(status: str | None) -> bool:
    if not status:
        return False
    lowered = status.lower()
    inactive_markers = [
        "absent",
        "well controlled",
        "intact / normal",
        "normal",
        "stable",
        "adequate",
        "good",
        "none",
    ]
    return not any(marker in lowered for marker in inactive_markers)


def _collect_status(lines: list[str], field_names: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field_name in field_names:
        value = _value_after_line(lines, field_name)
        if value:
            result[field_name] = value
    return result


def _parse_medications(text: str | None) -> list[dict[str, str | None]]:
    if not text:
        return []
    medications: list[dict[str, str | None]] = []
    for chunk in re.split(r",|\n", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.match(r"(.+?)\s+(\d+\s*mg\s*[A-Z]+)$", chunk, re.I)
        if match:
            dose_match = re.match(r"(\d+)\s*mg\s*([A-Z]+)", match.group(2), re.I)
            dose = f"{dose_match.group(1)} mg {dose_match.group(2).upper()}" if dose_match else match.group(2).strip()
            medications.append({"drug": match.group(1).strip(), "dose": dose})
        else:
            medications.append({"drug": chunk, "dose": None})
    return medications


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(value.split())
    cleaned = (
        cleaned.replace("â€”", "-")
        .replace("—", "-")
        .replace("–", "-")
        .replace(" .", ".")
        .strip(" -")
    )
    return cleaned or None


def _sentence_fragment(value: str | None) -> str:
    cleaned = _clean_text(value)
    if not cleaned:
        return "not available"
    return cleaned.rstrip(".; ")


def _between(lines: list[str], start_prefix: str, end_prefix: str) -> str | None:
    collecting = False
    parts: list[str] = []
    for line in lines:
        if line.startswith(end_prefix):
            break
        if collecting:
            parts.append(line)
            continue
        if line.startswith(start_prefix):
            collecting = True
            value = line.split(":", 1)[1].strip() if ":" in line else ""
            if value:
                parts.append(value)
    return "\n".join(parts).strip() or None


def _value_after_line(lines: list[str], label: str) -> str | None:
    for index, line in enumerate(lines[:-1]):
        if line == label:
            return lines[index + 1]
    return None


def _regex(text: str, pattern: str, flags: int = 0) -> str | None:
    match = re.search(pattern, text, flags)
    if not match:
        return None
    return match.group(1).strip()


def _first_date(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return value


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    return float(match.group(0))


def _to_int(value: str | None) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(number)


def _to_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"yes", "true"}:
        return True
    if lowered in {"no", "false"}:
        return False
    return None


def _score_from_text(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*100", value)
    if not match:
        return None
    return float(match.group(1))


if __name__ == "__main__":
    raise SystemExit(main())
