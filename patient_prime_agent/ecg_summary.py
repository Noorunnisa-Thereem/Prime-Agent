from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import ensure_dir

DEFAULT_INPUT_PATH = Path("patient_data") / "ECG" / "ECG_summary.json"
DEFAULT_OUTPUT_PATH = Path("reports") / "ecg" / "ECG_Clinical_Summary.json"

REPORT_TITLE = "ECG Digital Twin Dataset - Signal-Level Statistical Report"
AMPLITUDE_UNIT_LABELS = {"uV": "microvolts (uV)"}
NEAR_IDENTICAL_PERCENTILE_TOLERANCE = 0.5

SCOPE_AND_LIMITATIONS = (
    "This report is derived strictly from the statistical fields present in the source dataset. "
    "It does not represent a clinical or diagnostic ECG interpretation. Any assessment of cardiac "
    "rhythm, heart rate, or abnormality requires review of the underlying waveform by a qualified "
    "clinician or an appropriately validated diagnostic algorithm, neither of which is reflected "
    "in this report."
)

FILENAME_PATTERN = re.compile(r"^(?P<stem>.+)_(?P<date>\d{8})_(?P<time>\d{6})\.edf$")
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate ECG signal-level statistical summary JSON from ECG_summary.json")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    recordings = load_recordings(args.input)
    report = build_report(recordings)
    ensure_dir(args.output.parent)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote ECG clinical summary to {args.output}")
    return 0


def load_recordings(input_path: Path) -> list[dict[str, Any]]:
    if not input_path.exists():
        return []
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []

    recordings: list[dict[str, Any]] = []
    for entry in payload:
        file_name = entry.get("file_name", "")
        match = FILENAME_PATTERN.match(file_name)
        channels = entry.get("channels") or []
        if not match or not channels:
            continue
        channel = channels[0]
        date_text, time_text = match.group("date"), match.group("time")
        recordings.append(
            {
                "date": datetime.strptime(date_text, "%Y%m%d").strftime("%Y-%m-%d"),
                "time": f"{time_text[0:2]}:{time_text[2:4]}:{time_text[4:6]}",
                "duration_seconds": float(channel.get("duration_seconds") or 0.0),
                "samples": int(channel.get("samples") or 0),
                "sampling_rate_hz": float(channel.get("sampling_rate_hz") or 0.0),
                "channel_name": channel.get("channel_name"),
                "number_of_channels": entry.get("number_of_channels"),
                "physical_dimension": channel.get("physical_dimension"),
                "statistics": channel.get("statistics") or {},
                "percentiles": channel.get("percentiles") or {},
            }
        )
    recordings.sort(key=lambda item: item["date"])
    return recordings


def build_report(recordings: list[dict[str, Any]]) -> dict[str, Any]:
    if not recordings:
        return _empty_report()

    dates = [item["date"] for item in recordings]
    total_seconds = sum(item["duration_seconds"] for item in recordings)
    sampling_rates = {item["sampling_rate_hz"] for item in recordings}
    channel_names = {item["channel_name"] for item in recordings if item["channel_name"]}
    channel_counts = {item["number_of_channels"] for item in recordings}
    units = {item["physical_dimension"] for item in recordings if item["physical_dimension"]}

    return {
        "title": REPORT_TITLE,
        "reporting_period": {"start_date": dates[0], "end_date": dates[-1]},
        "generated": datetime.now().strftime("%Y-%m-%d"),
        "overview": [_overview_text(recordings)],
        "dataset_summary": {
            "number_of_recordings": len(recordings),
            "date_range": f"{dates[0]} to {dates[-1]}",
            "total_recorded_duration": _format_duration(total_seconds),
            "channel_configuration": _channel_configuration_text(channel_names, channel_counts),
            "sampling_rate_hz": _single_or_none(sampling_rates),
            "amplitude_units": _amplitude_units_text(units),
        },
        "recording_log": [_recording_log_entry(item) for item in recordings],
        "amplitude_statistics_uV": [_amplitude_statistics_entry(item) for item in recordings],
        "percentile_distribution_uV": [_percentile_entry(item) for item in recordings],
        "notable_observations": _notable_observations(recordings, channel_names, channel_counts),
        "scope_and_limitations": SCOPE_AND_LIMITATIONS,
    }


def _recording_log_entry(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": item["date"],
        "time": item["time"],
        "duration": _format_duration(item["duration_seconds"]),
        "samples": item["samples"],
        "sampling_rate_hz": item["sampling_rate_hz"],
    }


def _amplitude_statistics_entry(item: dict[str, Any]) -> dict[str, Any]:
    stats = item["statistics"]
    return {
        "date": item["date"],
        "min": _round(stats.get("minimum"), 1),
        "max": _round(stats.get("maximum"), 1),
        "mean": _round(stats.get("mean"), 2),
        "median": _round(stats.get("median"), 2),
        "std_dev": _round(stats.get("std"), 2),
    }


def _percentile_entry(item: dict[str, Any]) -> dict[str, Any]:
    percentiles = item["percentiles"]
    return {
        "date": item["date"],
        "p5": _round(percentiles.get("5"), 0),
        "p25": _round(percentiles.get("25"), 0),
        "p75": _round(percentiles.get("75"), 0),
        "p95": _round(percentiles.get("95"), 0),
    }


def _notable_observations(
    recordings: list[dict[str, Any]],
    channel_names: set[str],
    channel_counts: set[Any],
) -> list[str]:
    observations: list[str] = []

    sampling_rates = {item["sampling_rate_hz"] for item in recordings}
    if len(channel_names) == 1 and channel_counts == {1} and len(sampling_rates) == 1:
        rate = _display_number(next(iter(sampling_rates)))
        name = next(iter(channel_names))
        observations.append(
            f'All {len(recordings)} recordings use a single channel sampled at {rate} Hz, '
            f'so statistics are directly comparable across sessions.'
        )

    shortest = min(recordings, key=lambda item: item["duration_seconds"])
    longest = max(recordings, key=lambda item: item["duration_seconds"])
    if shortest is not longest:
        observations.append(
            f'Recording duration varies considerably, from about {_format_duration(shortest["duration_seconds"])} '
            f'({shortest["date"]}) to about {_format_duration(longest["duration_seconds"])} ({longest["date"]}).'
        )

    duplicate_pairs = _find_near_identical_percentile_pairs(recordings)
    for first, second in duplicate_pairs:
        observations.append(
            f'The {first["date"]} and {second["date"]} recordings have matching percentile values and '
            f'near-identical histogram shapes, suggesting very similar signal characteristics or a data '
            f'duplication between the two sessions.'
        )

    std_values = [(item["statistics"].get("std"), item["date"]) for item in recordings if item["statistics"].get("std") is not None]
    if len(std_values) >= 2:
        low_std, low_date = min(std_values, key=lambda pair: pair[0])
        high_std, high_date = max(std_values, key=lambda pair: pair[0])
        observations.append(
            f"Standard deviation of amplitude ranges from about {_round(low_std, 1)} uV ({low_date}) to about "
            f"{_round(high_std, 1)} uV ({high_date}), indicating substantially different signal variability/noise "
            f"levels between sessions."
        )

    peak_to_peak = [
        (item["statistics"].get("maximum", 0) - item["statistics"].get("minimum", 0), item["date"])
        for item in recordings
        if item["statistics"].get("maximum") is not None and item["statistics"].get("minimum") is not None
    ]
    if len(peak_to_peak) >= 2:
        low_ptp, low_date = min(peak_to_peak, key=lambda pair: pair[0])
        high_ptp, high_date = max(peak_to_peak, key=lambda pair: pair[0])
        observations.append(
            f"Peak-to-peak amplitude (max minus min) ranges from roughly {_round(low_ptp, 0):,.0f} uV ({low_date}) "
            f"to about {_round(high_ptp, 0):,.0f} uV ({high_date})."
        )

    return observations


def _find_near_identical_percentile_pairs(recordings: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, first in enumerate(recordings):
        for second in recordings[index + 1 :]:
            if _percentiles_match(first["percentiles"], second["percentiles"]):
                pairs.append((first, second))
    return pairs


def _percentiles_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("5", "25", "75", "95")
    if not all(key in left and key in right for key in keys):
        return False
    return all(abs(float(left[key]) - float(right[key])) <= NEAR_IDENTICAL_PERCENTILE_TOLERANCE for key in keys)


def _overview_text(recordings: list[dict[str, Any]]) -> str:
    start_month = _month_name(recordings[0]["date"])
    end_month = _month_name(recordings[-1]["date"])
    span = start_month if start_month == end_month else f"{start_month} and {end_month}"
    return (
        f"This report summarizes the signal-level statistical characteristics of the ECG digital twin dataset, "
        f"comprising {len(recordings)} recording sessions captured between {span} {recordings[0]['date'][:4]}. "
        f"Each entry in the source dataset represents a single-channel ECG waveform recorded from the digital "
        f"twin data stream, described only in terms of amplitude distribution (minimum, maximum, mean, median, "
        f"standard deviation, and percentiles)."
    )


def _channel_configuration_text(channel_names: set[str], channel_counts: set[Any]) -> str:
    if len(channel_names) == 1 and channel_counts == {1}:
        return f'single channel ("{next(iter(channel_names))}") in every recording'
    return "channel configuration varies across recordings; see recording_log for per-session detail"


def _amplitude_units_text(units: set[str]) -> str | None:
    if len(units) != 1:
        return None
    unit = next(iter(units))
    return AMPLITUDE_UNIT_LABELS.get(unit, unit)


def _single_or_none(values: set[float]) -> float | None:
    if len(values) != 1:
        return None
    value = next(iter(values))
    return _display_number(value)


def _display_number(value: float) -> float | int:
    return int(value) if float(value).is_integer() else value


def _month_name(date_text: str) -> str:
    month_index = int(date_text.split("-")[1]) - 1
    return MONTH_NAMES[month_index]


def _format_duration(total_seconds: float) -> str:
    total = int(total_seconds)
    hours, minutes = divmod(total // 60, 60)
    return f"{hours}h {minutes}m"


def _round(value: Any, digits: int) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _empty_report() -> dict[str, Any]:
    return {
        "title": REPORT_TITLE,
        "reporting_period": {"start_date": None, "end_date": None},
        "generated": datetime.now().strftime("%Y-%m-%d"),
        "overview": [],
        "dataset_summary": {
            "number_of_recordings": 0,
            "date_range": None,
            "total_recorded_duration": None,
            "channel_configuration": None,
            "sampling_rate_hz": None,
            "amplitude_units": None,
        },
        "recording_log": [],
        "amplitude_statistics_uV": [],
        "percentile_distribution_uV": [],
        "notable_observations": [],
        "scope_and_limitations": SCOPE_AND_LIMITATIONS,
    }


if __name__ == "__main__":
    raise SystemExit(main())
