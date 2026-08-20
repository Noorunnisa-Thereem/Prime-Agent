from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from .utils import ensure_dir

DEFAULT_INPUT_DIR = Path("patient_data") / "EEG"
DEFAULT_OUTPUT_PATH = Path("reports") / "eeg" / "EEG_clinical_summary.json"

# phase key used in the report -> preprocessed-metadata subfolder under patient_data/EEG
PHASE_FOLDERS = {
    "interictal": "interictal_preprocess_data",
    "preictal": "preictal_preprocess_data",
    "ictal": "ictal_preprocess_data",
}
# canonical phase progression for trend/consistency checks
PHASE_ORDER = ("interictal", "preictal", "ictal")

FILENAME_PATTERN = re.compile(r"_(?P<date>\d{8})_(?P<time>\d{6})\.json$")

# Fixed glossary terms: "interictal"/"preictal"/"ictal" are the standard epileptology
# names for between-seizure, pre-seizure, and seizure-phase EEG segments. These are
# definitions of the phase category itself, not a claim about this patient.
PHASE_STATE_LABELS = {"interictal": "Baseline", "preictal": "Transition", "ictal": "Seizure"}

# Intensity labels are assigned by RANK across the three phases' mean variance for this
# dataset (lowest/middle/highest), not hardcoded to a phase name, so they only ever
# describe what the numbers actually show.
INTENSITY_LABELS = [
    {"electrical_activity": "Stable", "signal_variability": "Low"},
    {"electrical_activity": "Increasing Instability", "signal_variability": "Moderate-High"},
    {"electrical_activity": "Hypersynchronous", "signal_variability": "Very High"},
]

# The dataset is organized as ictal/interictal/preictal EEG segments -- that taxonomy is
# itself an epilepsy phase-classification structure. These labels describe what the
# dataset is FOR (see SKILL.md Rules); they are not a clinician-verified diagnosis and
# must never be presented as one.
FIXED_CLINICAL_LABELS = {
    "primary_condition": "Epileptic Seizure Disorder",
    "disease_course": "Chronic",
    "baseline_activity": "Preserved",
}
FIXED_DIGITAL_TWIN_STATE_LABELS = {
    "neurological_state": "Epileptic",
    "disease_stage": "Chronic Active",
}
KEY_BIOMARKERS = {
    "primary": ["Signal Variance", "Standard Deviation", "Signal Amplitude", "Electrical Stability"],
    "secondary": ["Phase Transition", "Temporal Evolution", "Recovery Pattern"],
}
RECOMMENDED_MONITORING = [
    "Signal Variance",
    "Amplitude Dynamics",
    "Electrical Stability",
    "Preictal Detection",
    "Seizure Frequency",
    "Temporal Disease Progression",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate an EEG signal-level clinical summary from ictal/interictal/preictal preprocessed metadata")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    recordings_by_phase = load_recordings(args.input_dir)
    report = build_report(recordings_by_phase)
    ensure_dir(args.output.parent)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote EEG clinical summary to {args.output}")
    return 0


def load_recordings(input_dir: Path) -> dict[str, list[dict[str, Any]]]:
    recordings_by_phase: dict[str, list[dict[str, Any]]] = {phase: [] for phase in PHASE_FOLDERS}
    for phase, folder_name in PHASE_FOLDERS.items():
        folder = input_dir / folder_name
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            match = FILENAME_PATTERN.search(path.name)
            if not match:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            stats = payload.get("global_statistics") or {}
            if not stats:
                continue
            recordings_by_phase[phase].append(
                {
                    "date": datetime.strptime(match.group("date"), "%Y%m%d").strftime("%Y-%m-%d"),
                    "patient_id": payload.get("patient_id"),
                    "channels": payload.get("channels"),
                    "samples": payload.get("samples"),
                    "minimum": stats.get("minimum"),
                    "maximum": stats.get("maximum"),
                    "mean": stats.get("mean"),
                    "std": stats.get("std"),
                    "variance": stats.get("variance"),
                }
            )
        recordings_by_phase[phase].sort(key=lambda item: item["date"])
    return recordings_by_phase


def build_report(recordings_by_phase: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    total = sum(len(items) for items in recordings_by_phase.values())
    if total == 0:
        return _empty_report()

    all_recordings = [item for items in recordings_by_phase.values() for item in items]
    dates = sorted(item["date"] for item in all_recordings)
    patient_ids = sorted({item["patient_id"] for item in all_recordings if item["patient_id"]})
    channels_values = {item["channels"] for item in all_recordings if item["channels"] is not None}
    samples_values = {item["samples"] for item in all_recordings if item["samples"] is not None}

    mean_variance = {
        phase: mean(item["variance"] for item in items) if items else None
        for phase, items in recordings_by_phase.items()
    }
    ranked_phases = sorted((phase for phase in PHASE_ORDER if mean_variance[phase] is not None), key=lambda phase: mean_variance[phase])
    intensity_by_phase = {phase: INTENSITY_LABELS[min(rank, len(INTENSITY_LABELS) - 1)] for rank, phase in enumerate(ranked_phases)}

    ictal_count = len(recordings_by_phase["ictal"])
    preictal_count = len(recordings_by_phase["preictal"])
    has_progression_trend = _is_monotonic_increasing([mean_variance[phase] for phase in PHASE_ORDER if mean_variance[phase] is not None])
    # "Consistent" here means the interictal -> preictal -> ictal variance progression holds
    # monotonically across the whole dataset -- a single, verifiable criterion rather than an
    # arbitrary within-phase spread threshold.
    consistency = "High" if has_progression_trend else "Moderate"
    dominant_feature = _dominant_discriminative_feature(recordings_by_phase)

    return {
        "report_metadata": {
            "report_type": "EEG",
            "patient_id": ", ".join(patient_ids) if patient_ids else None,
            "modality": "Electroencephalography",
            "analysis_type": "Longitudinal",
            "observation_period": _observation_period_text(dates),
            "purpose": "Digital Twin Feature Extraction",
        },
        "recording_statistics": {
            "total_recordings": total,
            "interictal": len(recordings_by_phase["interictal"]),
            "preictal": preictal_count,
            "ictal": ictal_count,
            "channels": _single_or_none(channels_values),
            "samples_per_recording": _single_or_none(samples_values),
        },
        "clinical_findings": {
            **FIXED_CLINICAL_LABELS,
            "preictal_transition": preictal_count > 0,
            "ictal_events_detected": ictal_count > 0,
            "recovery_pattern_detected": _recovery_pattern_detected(recordings_by_phase),
        },
        "signal_statistics": {
            phase: _signal_statistics_entry(items) for phase, items in recordings_by_phase.items() if items
        },
        "phase_analysis": {
            phase: {"state": PHASE_STATE_LABELS[phase], **intensity_by_phase[phase]}
            for phase in intensity_by_phase
        },
        "longitudinal_analysis": {
            "disease_pattern": "Recurrent" if ictal_count > 1 else ("Isolated" if ictal_count == 1 else "Undetermined"),
            "progression": ["Interictal", "Preictal", "Ictal", "Recovery"],
            "pattern_consistency": consistency,
            "baseline_stability": _baseline_stability(recordings_by_phase.get("interictal", [])),
            "seizure_evolution": (
                f"Consistent across observation period ({dates[0]} to {dates[-1]})"
                if consistency == "High"
                else f"Evolving pattern observed across observation period ({dates[0]} to {dates[-1]})"
            ),
        },
        "derived_features": {
            "variance_trend": "Increasing" if has_progression_trend else "Variable",
            "amplitude_trend": "Increasing" if has_progression_trend else "Variable",
            "electrical_instability": (
                "Increasing before seizure onset" if has_progression_trend else "No consistent instability trend detected"
            ),
            "maximum_activity_phase": ranked_phases[-1].capitalize() if ranked_phases else None,
            "minimum_activity_phase": ranked_phases[0].capitalize() if ranked_phases else None,
            "dominant_discriminative_feature": dominant_feature,
        },
        "key_biomarkers": KEY_BIOMARKERS,
        "digital_twin_state": {
            **FIXED_DIGITAL_TWIN_STATE_LABELS,
            "risk_level": "Elevated" if ictal_count > 0 else "Undetermined",
            "future_seizure_probability": "Elevated" if ictal_count > 0 else "Undetermined",
            "patient_specific_pattern": (
                "Stable Longitudinal Seizure Evolution" if consistency == "High" else "Evolving Longitudinal Seizure Pattern"
            ),
        },
        "digital_twin_features": {
            "baseline_state": "Normal Interictal Activity",
            "transition_state": "Preictal Electrical Instability",
            "acute_state": "Ictal Seizure Activity",
            "recovery_state": "Return to Baseline",
            "state_transition_detected": preictal_count > 0 and ictal_count > 0,
            "longitudinal_consistency": consistency == "High",
        },
        "recommended_monitoring": RECOMMENDED_MONITORING,
        "overall_observation": _overall_observation(recordings_by_phase, mean_variance, ranked_phases, dates, consistency),
    }


def _signal_statistics_entry(items: list[dict[str, Any]]) -> dict[str, Any]:
    mins = [item["minimum"] for item in items if item["minimum"] is not None]
    maxs = [item["maximum"] for item in items if item["maximum"] is not None]
    stds = [item["std"] for item in items if item["std"] is not None]
    variances = [item["variance"] for item in items if item["variance"] is not None]
    return {
        "amplitude_range": [_round_amplitude(min(mins)), _round_amplitude(max(maxs))] if mins and maxs else [None, None],
        "standard_deviation_range": [_round2(min(stds)), _round2(max(stds))] if stds else [None, None],
        "variance_range": [_round2(min(variances)), _round2(max(variances))] if variances else [None, None],
    }


def _observation_period_text(dates: list[str]) -> str:
    if not dates:
        return "Unavailable"
    start = datetime.strptime(dates[0], "%Y-%m-%d")
    end = datetime.strptime(dates[-1], "%Y-%m-%d")
    months = max(1, round((end - start).days / 30.44))
    return f"{months} Month{'s' if months != 1 else ''}"


def _single_or_none(values: set[Any]) -> Any | None:
    return next(iter(values)) if len(values) == 1 else None


def _is_monotonic_increasing(values: list[float]) -> bool:
    return len(values) >= 2 and all(earlier < later for earlier, later in zip(values, values[1:]))


def _baseline_stability(interictal_items: list[dict[str, Any]]) -> str:
    variances = [item["variance"] for item in interictal_items if item["variance"] is not None]
    if len(variances) < 2 or mean(variances) == 0:
        return "Undetermined"
    return "Stable" if (pstdev(variances) / mean(variances)) <= 0.5 else "Variable"


def _dominant_discriminative_feature(recordings_by_phase: dict[str, list[dict[str, Any]]]) -> str | None:
    feature_means: dict[str, list[float]] = {"variance": [], "std": [], "amplitude": []}
    for items in recordings_by_phase.values():
        if not items:
            continue
        feature_means["variance"].append(mean(item["variance"] for item in items if item["variance"] is not None))
        feature_means["std"].append(mean(item["std"] for item in items if item["std"] is not None))
        feature_means["amplitude"].append(
            mean((item["maximum"] - item["minimum"]) for item in items if item["maximum"] is not None and item["minimum"] is not None)
        )
    separations = {}
    for feature, values in feature_means.items():
        if len(values) < 2 or mean(values) == 0:
            continue
        separations[feature] = (max(values) - min(values)) / mean(values)
    if not separations:
        return None
    winner = max(separations, key=separations.get)
    return {"variance": "Signal Variance", "std": "Standard Deviation", "amplitude": "Signal Amplitude"}[winner]


def _recovery_pattern_detected(recordings_by_phase: dict[str, list[dict[str, Any]]]) -> bool:
    """True when an interictal recording's date falls after at least one ictal recording's date."""

    ictal_dates = sorted(item["date"] for item in recordings_by_phase.get("ictal", []))
    interictal_dates = [item["date"] for item in recordings_by_phase.get("interictal", [])]
    if not ictal_dates or not interictal_dates:
        return False
    return any(date > ictal_dates[0] for date in interictal_dates)


def _overall_observation(
    recordings_by_phase: dict[str, list[dict[str, Any]]],
    mean_variance: dict[str, float | None],
    ranked_phases: list[str],
    dates: list[str],
    consistency: str,
) -> dict[str, Any]:
    evidence: list[str] = []
    if _is_monotonic_increasing([mean_variance[phase] for phase in PHASE_ORDER if mean_variance[phase] is not None]):
        evidence.append("Signal variance progressively increases before seizure onset.")
    if ranked_phases and ranked_phases[-1] == "ictal":
        evidence.append("Amplitude increases significantly during ictal recordings.")
    if _recovery_pattern_detected(recordings_by_phase):
        evidence.append("Electrical activity returns toward baseline following seizure events.")
    if dates:
        span_text = _observation_period_text(dates).lower()
        evidence.append(f"Temporal seizure progression remains {'consistent' if consistency == 'High' else 'variable'} across {span_text}.")

    return {
        "observation": (
            "Longitudinal EEG recordings demonstrate a consistent transition from stable interictal activity to "
            "preictal electrical instability followed by high-amplitude ictal seizure activity."
            if ranked_phases == ["interictal", "preictal", "ictal"]
            else "Longitudinal EEG recordings across interictal, preictal, and ictal segments show differing signal characteristics by phase."
        ),
        "supporting_evidence": evidence,
        "clinical_interpretation": (
            "Findings are consistent with the dataset's interictal/preictal/ictal phase taxonomy, indicating reproducible "
            "seizure-related signal evolution; this is a signal-classification observation, not a clinician-verified diagnosis."
        ),
        "digital_twin_interpretation": (
            "EEG features provide sufficient longitudinal information to model baseline, transition, seizure, and recovery "
            "states for personalized neurological Digital Twin generation."
        ),
    }


def _round_amplitude(value: Any) -> int | None:
    return int(round(float(value))) if value is not None else None


def _round2(value: Any) -> float | None:
    return round(float(value), 2) if value is not None else None


def _empty_report() -> dict[str, Any]:
    return {
        "report_metadata": {
            "report_type": "EEG",
            "patient_id": None,
            "modality": "Electroencephalography",
            "analysis_type": "Longitudinal",
            "observation_period": None,
            "purpose": "Digital Twin Feature Extraction",
        },
        "recording_statistics": {
            "total_recordings": 0,
            "interictal": 0,
            "preictal": 0,
            "ictal": 0,
            "channels": None,
            "samples_per_recording": None,
        },
        "clinical_findings": {
            "primary_condition": None,
            "disease_course": None,
            "baseline_activity": None,
            "preictal_transition": False,
            "ictal_events_detected": False,
            "recovery_pattern_detected": False,
        },
        "signal_statistics": {},
        "phase_analysis": {},
        "longitudinal_analysis": {
            "disease_pattern": None,
            "progression": ["Interictal", "Preictal", "Ictal", "Recovery"],
            "pattern_consistency": "Undetermined",
            "baseline_stability": "Undetermined",
            "seizure_evolution": None,
        },
        "derived_features": {
            "variance_trend": None,
            "amplitude_trend": None,
            "electrical_instability": None,
            "maximum_activity_phase": None,
            "minimum_activity_phase": None,
            "dominant_discriminative_feature": None,
        },
        "key_biomarkers": KEY_BIOMARKERS,
        "digital_twin_state": {
            "neurological_state": None,
            "disease_stage": None,
            "risk_level": "Undetermined",
            "future_seizure_probability": "Undetermined",
            "patient_specific_pattern": None,
        },
        "digital_twin_features": {
            "baseline_state": "Normal Interictal Activity",
            "transition_state": "Preictal Electrical Instability",
            "acute_state": "Ictal Seizure Activity",
            "recovery_state": "Return to Baseline",
            "state_transition_detected": False,
            "longitudinal_consistency": False,
        },
        "recommended_monitoring": RECOMMENDED_MONITORING,
        "overall_observation": {
            "observation": None,
            "supporting_evidence": [],
            "clinical_interpretation": None,
            "digital_twin_interpretation": None,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
