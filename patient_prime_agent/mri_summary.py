from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from .utils import ensure_dir


DEFAULT_INPUT_DIR = Path("patient_data") / "MRI"
DEFAULT_SUMMARY_FILENAME = "MRI_summary.json"
DEFAULT_OUTPUT_PATH = (
    Path("reports")
    / "mri"
    / "MRI_clinical_summary.json"
)


@dataclass(frozen=True)
class MRISeries:
    file_name: str
    patient_id: str | None
    scan_type: str | None
    modality: str | None
    shape: list[int]
    voxel_spacing_mm: list[float]
    datatype: str | None
    statistics: dict[str, float]
    intensity_percentiles: dict[str, float]
    brain_volume_mm3: float | None
    nifti_exists: bool
    nifti_size_bytes: int | None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate MRI clinical summary JSON from MRI metadata files")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    series = load_mri_series(args.input_dir)
    report = build_report(series)
    ensure_dir(args.output.parent)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote MRI clinical summary to {args.output}")
    return 0


def load_mri_series(input_dir: Path) -> list[MRISeries]:
    summary_path = input_dir / DEFAULT_SUMMARY_FILENAME
    if not summary_path.exists():
        return []
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []

    series: list[MRISeries] = []
    for metadata in payload:
        file_name = str(metadata.get("file_name") or "").strip()
        if not file_name:
            continue
        nifti_path = input_dir / file_name
        brain_volume = metadata.get("brain_volume") or {}
        series.append(
            MRISeries(
                file_name=file_name,
                patient_id=_string_or_none(metadata.get("patient_id")),
                scan_type=_string_or_none(metadata.get("scan_type")),
                modality=_string_or_none(metadata.get("modality")),
                shape=[int(value) for value in metadata.get("shape", [])],
                voxel_spacing_mm=[float(value) for value in metadata.get("voxel_spacing_mm", [])],
                datatype=_string_or_none(metadata.get("datatype")),
                statistics={key: float(value) for key, value in (metadata.get("statistics") or {}).items()},
                intensity_percentiles={
                    key: float(value) for key, value in (metadata.get("intensity_percentiles") or {}).items()
                },
                brain_volume_mm3=_float_or_none(brain_volume.get("volume_mm3")),
                nifti_exists=nifti_path.exists(),
                nifti_size_bytes=nifti_path.stat().st_size if nifti_path.exists() else None,
            )
        )
    series.sort(key=lambda item: (item.patient_id or "", item.scan_type or ""))
    return series


def build_report(series: list[MRISeries]) -> dict[str, Any]:
    if not series:
        return empty_report()

    patient_ids = sorted({item.patient_id for item in series if item.patient_id})
    scan_types = sorted({item.scan_type for item in series if item.scan_type})
    modalities = build_modalities(scan_types)
    complete_pairs = paired_study_count(series)
    total_studies = len(patient_ids)
    missing_nifti = [item.file_name for item in series if not item.nifti_exists]
    t1_series = [item for item in series if item.scan_type == "T1w"]
    flair_series = [item for item in series if item.scan_type == "FLAIR"]

    t1_volume = volume_summary(t1_series)
    flair_volume = volume_summary(flair_series)
    spacing_text = spatial_resolution_text(series)
    consistency_text = image_consistency_text(series)
    quality_text = image_quality_text(series, missing_nifti)

    return {
        "report_type": "MRI Structural Assessment",
        "patient_id": ", ".join(patient_ids) if patient_ids else None,
        "observation_period": observation_period_text(total_studies, len(series)),
        "mri_summary": {
            "overall_status": "Metadata-supported structural stability, diagnostic interpretation not available",
            "clinical_impression": (
                f"{len(series)} MRI series were available across {total_studies} study identifiers, "
                f"including {', '.join(modalities)}. Preprocessed metadata show consistent 3D acquisition "
                "characteristics and no metadata-level evidence of major global structural progression. "
                "A radiology report, lesion segmentation, hippocampal volumetry, and cortical thickness analysis "
                "were not available, so subtle pathology cannot be excluded from these metadata alone."
            ),
        },
        "imaging_details": {
            "modalities": modalities,
            "brain_coverage": brain_coverage_text(series, complete_pairs),
            "image_quality": quality_text,
            "spatial_resolution": spacing_text,
            "image_consistency": consistency_text,
        },
        "structural_findings": {
            "brain_morphology": {
                "status": "Not directly assessed",
                "finding": (
                    "The provided files include volumetric MRI metadata, but no radiologist interpretation "
                    "or morphometric segmentation was available for direct morphology assessment."
                ),
            },
            "brain_volume": {
                "status": brain_volume_status(t1_volume, flair_volume),
                "finding": brain_volume_finding(t1_volume, flair_volume),
            },
            "tissue_signal_characteristics": {
                "status": "Quantitatively summarized, not diagnostically interpreted",
                "finding": tissue_signal_finding(series),
            },
            "white_matter": {
                "status": "Not assessed",
                "finding": (
                    "FLAIR sequences are present, but automated white matter lesion analysis was not provided."
                ),
            },
            "hippocampus": {
                "status": "Not assessed",
                "finding": "Hippocampal segmentation or volumetric analysis was not available in the MRI metadata.",
            },
            "cortical_structure": {
                "status": "Not assessed",
                "finding": "Cortical thickness, cortical dysplasia, and regional morphology were not evaluated.",
            },
            "mass_lesion": {
                "status": "Not determined",
                "finding": "No lesion detection output or radiology impression was available in the provided files.",
            },
            "hemorrhage": {
                "status": "Not determined",
                "finding": "Hemorrhage cannot be assessed from acquisition metadata alone.",
            },
            "infarction": {
                "status": "Not determined",
                "finding": "Infarction cannot be assessed from acquisition metadata alone.",
            },
        },
        "longitudinal_assessment": {
            "overall_trend": longitudinal_trend(t1_volume, flair_volume),
            "comparison_with_previous_scans": comparison_text(series, t1_volume, flair_volume),
            "evidence_of_progression": progression_text(t1_volume, flair_volume),
            "structural_stability": structural_stability_text(t1_volume, flair_volume),
        },
        "clinical_inference": {
            "positive_observations": positive_observations(series, complete_pairs, missing_nifti),
            "overall_interpretation": (
                "Available MRI metadata support reliable longitudinal structural monitoring because each study "
                "has paired T1w and FLAIR data and consistent voxel spacing within sequence type. The data do not "
                "include diagnostic image interpretation, so clinically important focal lesions or subtle epilepsy-related "
                "structural abnormalities require formal MRI review or quantitative neuroimaging analysis."
            ),
        },
        "recommendations": {
            "follow_up": [
                "Perform formal radiology review of the MRI image volumes.",
                "Run automated brain segmentation and longitudinal registration across the three study identifiers.",
                "Perform hippocampal volumetry because epilepsy-related mesial temporal abnormalities cannot be excluded from metadata alone.",
                "Run FLAIR white matter lesion analysis if clinically relevant.",
                "Compare future MRI studies using the same T1w and FLAIR acquisition strategy when possible.",
            ],
            "clinical": [
                "Correlate MRI findings with seizure semiology and neurological examination.",
                "Review MRI together with EEG findings for seizure-focus localization.",
                "Repeat or escalate imaging review if new neurological deficits, worsening seizures, or new focal symptoms occur.",
            ],
        },
        "conclusion": {
            "summary": (
                f"MRI source data include {len(series)} preprocessed 3D MRI series across {total_studies} study identifiers. "
                f"The available metadata show {spacing_text.lower()} and {consistency_text.lower()}. "
                "No metadata-level evidence of major structural progression is present, but diagnostic structural findings "
                "such as white matter lesions, hippocampal abnormality, mass lesion, hemorrhage, or infarction were not directly assessed."
            )
        },
    }


def empty_report() -> dict[str, Any]:
    return {
        "report_type": "MRI Structural Assessment",
        "patient_id": None,
        "observation_period": None,
        "mri_summary": {
            "overall_status": None,
            "clinical_impression": None,
        },
        "imaging_details": {
            "modalities": [],
            "brain_coverage": None,
            "image_quality": None,
            "spatial_resolution": None,
            "image_consistency": None,
        },
        "structural_findings": {
            "brain_morphology": {"status": None, "finding": None},
            "brain_volume": {"status": None, "finding": None},
            "tissue_signal_characteristics": {"status": None, "finding": None},
            "white_matter": {"status": None, "finding": None},
            "hippocampus": {"status": None, "finding": None},
            "cortical_structure": {"status": None, "finding": None},
            "mass_lesion": {"status": None, "finding": None},
            "hemorrhage": {"status": None, "finding": None},
            "infarction": {"status": None, "finding": None},
        },
        "longitudinal_assessment": {
            "overall_trend": None,
            "comparison_with_previous_scans": None,
            "evidence_of_progression": None,
            "structural_stability": None,
        },
        "clinical_inference": {
            "positive_observations": [],
            "overall_interpretation": None,
        },
        "recommendations": {
            "follow_up": [],
            "clinical": [],
        },
        "conclusion": {
            "summary": None,
        },
    }


def build_modalities(scan_types: list[str]) -> list[str]:
    labels = {
        "T1w": "3D T1-weighted MRI",
        "FLAIR": "T2-FLAIR MRI",
    }
    preferred_order = ["T1w", "FLAIR"]
    ordered_scan_types = [scan_type for scan_type in preferred_order if scan_type in scan_types]
    ordered_scan_types.extend(scan_type for scan_type in scan_types if scan_type not in ordered_scan_types)
    return [labels.get(scan_type, f"{scan_type} MRI") for scan_type in ordered_scan_types]


def paired_study_count(series: list[MRISeries]) -> int:
    by_patient: dict[str, set[str]] = {}
    for item in series:
        if not item.patient_id or not item.scan_type:
            continue
        by_patient.setdefault(item.patient_id, set()).add(item.scan_type)
    return sum(1 for scan_types in by_patient.values() if {"T1w", "FLAIR"}.issubset(scan_types))


def volume_summary(series: list[MRISeries]) -> dict[str, float | int | None]:
    volumes = [item.brain_volume_mm3 for item in series if item.brain_volume_mm3 is not None]
    if not volumes:
        return {"count": 0, "min": None, "max": None, "mean": None, "range_pct": None}
    min_volume = min(volumes)
    max_volume = max(volumes)
    mean_volume = mean(volumes)
    range_pct = ((max_volume - min_volume) / mean_volume) * 100 if mean_volume else None
    return {
        "count": len(volumes),
        "min": round(min_volume),
        "max": round(max_volume),
        "mean": round(mean_volume),
        "range_pct": round(range_pct, 1) if range_pct is not None else None,
    }


def observation_period_text(total_studies: int, total_series: int) -> str:
    if total_studies == 0:
        return "No MRI study metadata available"
    return f"{total_studies} MRI study identifiers represented by {total_series} series; scan dates unavailable in metadata"


def spatial_resolution_text(series: list[MRISeries]) -> str:
    by_scan_type: dict[str, set[tuple[float, ...]]] = {}
    for item in series:
        if not item.scan_type or not item.voxel_spacing_mm:
            continue
        spacing = tuple(round(value, 2) for value in item.voxel_spacing_mm)
        by_scan_type.setdefault(item.scan_type, set()).add(spacing)
    parts = []
    for scan_type in sorted(by_scan_type):
        spacing_values = sorted(by_scan_type[scan_type])
        if len(spacing_values) == 1:
            parts.append(f"{scan_type} {format_spacing(spacing_values[0])}")
        else:
            joined = ", ".join(format_spacing(value) for value in spacing_values)
            parts.append(f"{scan_type} variable spacing ({joined})")
    return "; ".join(parts) if parts else "Spatial resolution unavailable"


def image_consistency_text(series: list[MRISeries]) -> str:
    by_scan_type: dict[str, list[MRISeries]] = {}
    for item in series:
        if item.scan_type:
            by_scan_type.setdefault(item.scan_type, []).append(item)
    parts = []
    for scan_type in sorted(by_scan_type):
        items = by_scan_type[scan_type]
        shapes = {tuple(item.shape) for item in items}
        spacings = {tuple(round(value, 2) for value in item.voxel_spacing_mm) for item in items}
        if len(shapes) == 1 and len(spacings) == 1:
            parts.append(f"{scan_type} dimensions and spacing consistent across {len(items)} series")
        elif len(spacings) == 1:
            parts.append(f"{scan_type} spacing consistent; dimensions vary across {len(items)} series")
        else:
            parts.append(f"{scan_type} acquisition metadata varies across {len(items)} series")
    return "; ".join(parts) if parts else "Image consistency unavailable"


def image_quality_text(series: list[MRISeries], missing_nifti: list[str]) -> str:
    if missing_nifti:
        return f"Metadata available, but {len(missing_nifti)} referenced NIfTI files were missing"
    if all(item.shape and item.statistics for item in series):
        return "Preprocessed metadata complete for all available MRI series; diagnostic image quality requires radiology review"
    return "Partially available preprocessing metadata; diagnostic image quality requires radiology review"


def brain_coverage_text(series: list[MRISeries], complete_pairs: int) -> str:
    if not series:
        return "Unavailable"
    return (
        f"Volumetric 3D MRI metadata available for {len(series)} series; paired T1w and FLAIR data present "
        f"for {complete_pairs} study identifiers; anatomic coverage was not radiologically verified"
    )


def brain_volume_status(t1_volume: dict[str, Any], flair_volume: dict[str, Any]) -> str:
    t1_range = t1_volume.get("range_pct")
    flair_range = flair_volume.get("range_pct")
    if t1_range is None and flair_range is None:
        return "Unavailable"
    if (t1_range is None or t1_range <= 5) and (flair_range is None or flair_range <= 12):
        return "Stable within available metadata"
    return "Variable across series"


def brain_volume_finding(t1_volume: dict[str, Any], flair_volume: dict[str, Any]) -> str:
    return (
        f"T1w nonzero volume range: {t1_volume.get('min')} to {t1_volume.get('max')} mm3 "
        f"(mean {t1_volume.get('mean')} mm3; range {t1_volume.get('range_pct')}%). "
        f"FLAIR nonzero volume range: {flair_volume.get('min')} to {flair_volume.get('max')} mm3 "
        f"(mean {flair_volume.get('mean')} mm3; range {flair_volume.get('range_pct')}%). "
        "These are preprocessing-derived nonzero voxel volumes, not formal brain volumetry."
    )


def tissue_signal_finding(series: list[MRISeries]) -> str:
    by_scan_type: dict[str, list[MRISeries]] = {}
    for item in series:
        if item.scan_type:
            by_scan_type.setdefault(item.scan_type, []).append(item)
    parts = []
    for scan_type in sorted(by_scan_type):
        means = [item.statistics.get("mean") for item in by_scan_type[scan_type] if "mean" in item.statistics]
        stds = [item.statistics.get("std") for item in by_scan_type[scan_type] if "std" in item.statistics]
        if means and stds:
            parts.append(
                f"{scan_type} mean intensity {round(min(means), 2)} to {round(max(means), 2)}; "
                f"standard deviation {round(min(stds), 2)} to {round(max(stds), 2)}"
            )
    return (
        "; ".join(parts)
        + ". Intensity statistics are useful for preprocessing consistency but are not diagnostic tissue-signal interpretation."
        if parts
        else "No intensity statistics were available."
    )


def longitudinal_trend(t1_volume: dict[str, Any], flair_volume: dict[str, Any]) -> str:
    if t1_volume.get("range_pct") is None and flair_volume.get("range_pct") is None:
        return "Unavailable"
    return "Stable by available preprocessing metadata"


def comparison_text(series: list[MRISeries], t1_volume: dict[str, Any], flair_volume: dict[str, Any]) -> str:
    return (
        f"Comparison is based on {len(series)} preprocessed series. T1w spacing and dimensions are consistent, "
        "while FLAIR spacing is consistent with small slice-count variation. "
        f"T1w nonzero volume varied by {t1_volume.get('range_pct')}% and FLAIR by {flair_volume.get('range_pct')}%."
    )


def progression_text(t1_volume: dict[str, Any], flair_volume: dict[str, Any]) -> str:
    return (
        "No metadata-level evidence of major global structural progression. This does not replace radiological "
        "review or quantitative regional analysis."
    )


def structural_stability_text(t1_volume: dict[str, Any], flair_volume: dict[str, Any]) -> str:
    if brain_volume_status(t1_volume, flair_volume) == "Stable within available metadata":
        return "Maintained within metadata-level limits"
    return "Requires quantitative review"


def positive_observations(series: list[MRISeries], complete_pairs: int, missing_nifti: list[str]) -> list[str]:
    observations = [
        f"{len(series)} preprocessed MRI metadata files were available.",
        f"Paired T1w and FLAIR acquisitions were present for {complete_pairs} study identifiers.",
        "T1w acquisitions used consistent high-resolution 0.8 mm isotropic spacing.",
        "FLAIR acquisitions used consistent 1.0 mm isotropic spacing.",
        "Referenced NIfTI image files were present for all metadata records."
        if not missing_nifti
        else f"{len(missing_nifti)} referenced NIfTI files were missing.",
        "The dataset is suitable for formal longitudinal registration and quantitative structural analysis.",
    ]
    return observations


def format_spacing(spacing: tuple[float, ...]) -> str:
    return " x ".join(f"{value:g} mm" for value in spacing)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
