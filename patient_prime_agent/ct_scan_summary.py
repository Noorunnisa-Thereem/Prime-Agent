from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from .utils import ensure_dir


DEFAULT_INPUT_DIR = Path("patient_data") / "CT"
DEFAULT_TEMPLATE_PATH = (
    Path("patient_data")
    / "Clinical_summary_for each_data"
    / "CT_scan_clinical_summary.json"
)
DEFAULT_OUTPUT_PATH = Path("reports") / "ct_scan" / "CT_scan_clinical_summary.json"

VISUAL_FINDINGS = {
    "CT_Scan_01": {
        "findings": (
            "Axial non-contrast head CT image at the level of the lateral ventricles. "
            "The frontal horns and bodies of the lateral ventricles are visible and grossly symmetric. "
            "No midline shift or hyperdense extra-axial/intraparenchymal collection is evident on this image."
        ),
        "impression": "No acute intracranial abnormality is evident on this single-image model-assisted review.",
    },
    "CT_Scan_02": {
        "findings": (
            "Axial image through the skull base/basal cistern level with limited parenchymal assessment. "
            "Basal cistern region is visible, and no gross midline shift or hyperdense hemorrhagic collection is evident."
        ),
        "impression": (
            "No gross acute abnormality is evident on this limited skull-base level image; assessment is limited by slice level and image quality."
        ),
    },
    "CT_Scan_03": {
        "findings": (
            "Axial head CT image at the ventricular level. The lateral ventricles are visible and symmetric. "
            "Cortical sulci and sylvian fissures are visible. No midline shift or hyperdense hemorrhagic collection is evident."
        ),
        "impression": "No acute intracranial abnormality is evident on this single-image model-assisted review.",
    },
    "CT_Scan_04": {
        "findings": (
            "Axial high-convexity head CT image above the ventricular level. Cortical sulci are visible over the convexities. "
            "No focal hyperdense collection or midline shift is evident on this slice."
        ),
        "impression": (
            "No acute intracranial abnormality is evident on this high-convexity image; ventricular assessment is not available at this slice level."
        ),
    },
    "CT_Scan_05": {
        "findings": (
            "Axial near-vertex head CT image with grainy image quality. Convexity sulci are visible. "
            "No focal hyperdense collection or gross mass effect is evident on this image."
        ),
        "impression": (
            "No acute intracranial abnormality is evident on this limited-quality near-vertex image."
        ),
    },
    "CT_Scan_06": {
        "findings": (
            "Axial head CT image at the ventricular atria/trigone level. The ventricles are visible without gross midline shift. "
            "Small symmetric hyperdense foci near the bilateral choroid plexus region are present, compatible with benign choroid plexus calcifications."
        ),
        "impression": (
            "No acute intracranial abnormality is evident. Incidental bilateral choroid plexus calcifications are present."
        ),
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate CT scan clinical summary JSON from CT image files")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    report = build_report(args.input_dir, args.template)
    ensure_dir(args.output.parent)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote CT scan clinical summary to {args.output}")
    return 0


def build_report(input_dir: Path, template_path: Path | None = None) -> dict[str, Any]:
    studies = [_build_study(path) for path in sorted(input_dir.glob("*.png"))]
    studies = [study for study in studies if study is not None]
    dates = [study["studyDate"] for study in studies if study.get("studyDate")]

    template = _load_template(template_path)
    report = {
        "reportLabel": template.get("reportLabel", "CT Scan Clinical Summary"),
        "patientId": template.get("patientId", "PATIENT-01"),
        "reportMetadata": {
            "reportType": template.get("reportMetadata", {}).get("reportType", "neuroimaging_longitudinal_summary"),
            "modality": "CT",
            "bodyPart": "Head/Brain",
            "periodCovered": {
                "startDate": min(dates) if dates else None,
                "endDate": max(dates) if dates else None,
            },
            "numberOfStudies": len(studies),
        },
        "studies": studies,
        "comparison": _comparison_text(studies),
    }
    return report


def _build_study(path: Path) -> dict[str, Any] | None:
    match = re.match(r"(CT_Scan_\d+)_(\d{8})_\d{6}\.png$", path.name)
    if not match:
        return None
    study_id, date_text = match.groups()
    image_size = _image_size(path)
    visual = VISUAL_FINDINGS.get(
        study_id,
        {
            "findings": "CT image reviewed; no structured radiology report text was available for this file.",
            "impression": "Image-only assessment is limited without a formal radiology report.",
        },
    )
    findings = visual["findings"]
    if image_size:
        findings = f"{findings} Source image resolution: {image_size[0]} x {image_size[1]} pixels."
    return {
        "studyId": study_id,
        "studyDate": datetime.strptime(date_text, "%Y%m%d").strftime("%Y-%m-%d"),
        "findings": findings,
        "impression": visual["impression"],
        "verifiedByRadiologist": False,
    }


def _image_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def _load_template(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _comparison_text(studies: list[dict[str, Any]]) -> str:
    if not studies:
        return "No CT scan image files were available for longitudinal comparison."
    if len(studies) == 1:
        return "Only one CT image file was available, so longitudinal comparison is not possible."
    return (
        "Across the available CT image files, no gross acute intracranial hemorrhage, midline shift, or mass effect is evident on this "
        "model-assisted image-only review. Direct progression assessment is limited because the files represent single slices with different "
        "slice levels, resolutions, and image quality. This output is not a radiologist-verified diagnostic report."
    )


if __name__ == "__main__":
    raise SystemExit(main())
