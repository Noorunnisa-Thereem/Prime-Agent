from __future__ import annotations

import argparse
import json
import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import ensure_dir, normalize_whitespace

DEFAULT_INPUT_DIR = Path("patient_data") / "Questionnaire"
DEFAULT_OUTPUT_PATH = Path("reports") / "questionnaire" / "Questionnaire_consolidated_summary.json"

REPORT_LABEL = "Merged Prime Agent Questionnaire Summary"

# Fixed, documented mapping from a domain's full name to the short key used for its
# latest-visit summary and its recurring-findings aggregate. Matches the reference report's
# key set for this questionnaire's known 7 domains; an unrecognized domain (a new one added
# to the source form) falls back to its slugified full name rather than being dropped.
DOMAIN_SHORT_KEY = {
    "Seizure Frequency & Types": "seizure_summary",
    "Aura & Prodromal Symptoms": "aura_summary",
    "Medication Side Effects": "medication_summary",
    "Sleep Quality & Architecture": "sleep_summary",
    "Epileptic Triggers": "trigger_summary",
    "Cognitive & Executive Function": "cognitive_summary",
    "Lab Biomarkers & Lifestyle": "laboratory_lifestyle_summary",
}

BOILERPLATE_PATTERNS = [
    re.compile(r"^SYNTHETIC TEST DATA.*$"),
    re.compile(r"^Questionnaire \d+/\d+ \| Generated \d{4}$"),
    re.compile(r"^Page \d+$"),
]
DOMAIN_SCORE_PATTERN = re.compile(r"^([\d.]+)\s*/\s*100$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a consolidated longitudinal questionnaire summary from questionnaire PDFs")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)

    manifest, reports = load_reports(args.input_dir)
    report = build_report(reports, manifest, args.input_dir)
    ensure_dir(args.output.parent)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote questionnaire consolidated summary to {args.output}")
    return 0


# ----------------------------------------------------------------------
# PDF parsing
# ----------------------------------------------------------------------
def load_reports(input_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    detected = sorted(input_dir.glob("*.pdf")) if input_dir.exists() else []
    processed: list[Path] = []
    failed: list[Path] = []
    reports: list[dict[str, Any]] = []

    for path in detected:
        try:
            text = _read_pdf_text(path)
        except Exception:
            failed.append(path)
            continue
        report = _parse_questionnaire_pdf(text)
        if report is None:
            failed.append(path)
            continue
        reports.append(report)
        processed.append(path)

    reports.sort(key=lambda item: item["report_date"] or "")
    _reconcile_domain_questions(reports)
    manifest = {
        "source_type": "pdf",
        "detected_count": len(detected),
        "processed_count": len(processed),
        "failed_count": len(failed),
        "skipped_count": 0,
        "status": "complete" if not failed and detected else ("partial" if processed else "no_input"),
        "detected_files": [str(path) for path in detected],
        "processed_files": [str(path) for path in processed],
        "failed_files": [str(path) for path in failed],
        "skipped_files": [],
        "confidence": round(len(processed) / len(detected), 2) if detected else 0.0,
    }
    return manifest, reports


def _read_pdf_text(path: Path) -> str:
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name, fromlist=["PdfReader"])
        except ImportError:
            continue
        reader = module.PdfReader(str(path))
        return normalize_whitespace("\n".join(page.extract_text() or "" for page in reader.pages))
    raise RuntimeError("PDF text extraction requires pypdf or PyPDF2")


def _parse_questionnaire_pdf(text: str) -> dict[str, Any] | None:
    if not text.strip():
        return None

    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not any(pattern.match(line) for pattern in BOILERPLATE_PATTERNS)]

    assessment_date = _first_date(_regex("\n".join(lines), r"Assessment Date:\s*([0-9]{2}-[A-Za-z]{3}-[0-9]{4})"))
    name = _regex("\n".join(lines), r"Name:\s*(.+)")
    patient_id = _regex("\n".join(lines), r"Patient ID:\s*(.+)")
    age_sex = re.search(r"Age/Sex:\s*(\d+)\s*/\s*(\w+)", "\n".join(lines))
    age = int(age_sex.group(1)) if age_sex else None
    sex = age_sex.group(2) if age_sex else None
    diagnosis_date = _first_date(_regex("\n".join(lines), r"Diagnosis Date:\s*([0-9]{2}-[A-Za-z]{3}-[0-9]{4})"))
    current_meds = _split_list(_regex("\n".join(lines), r"Current Meds:\s*(.+)"))
    seizure_history = _split_list(_regex("\n".join(lines), r"Seizure History:\s*(.+)"))

    domain_scores = _parse_domain_scores(lines)
    domain_names = list(domain_scores)
    domains = {name: _parse_domain_qa(lines, name, domain_names) for name in domain_names}

    return {
        "report_date": assessment_date,
        "patient_name": name,
        "patient_id": patient_id,
        "age": age,
        "sex": sex,
        "diagnosis_date": diagnosis_date,
        "current_medications": current_meds,
        "seizure_history": seizure_history,
        "domain_scores": domain_scores,
        "domains": domains,
        # kept for the second, anchor-based re-parse pass in _reconcile_domain_questions.
        "_lines": lines,
        "_domain_names": domain_names,
    }


def _parse_domain_scores(lines: list[str]) -> "OrderedDict[str, float]":
    scores: "OrderedDict[str, float]" = OrderedDict()
    try:
        start = lines.index("Domain")
    except ValueError:
        return scores
    index = start + 1
    if index < len(lines) and lines[index] == "Score":
        index += 1
    while index + 1 < len(lines):
        name_line, score_line = lines[index], lines[index + 1]
        match = DOMAIN_SCORE_PATTERN.match(score_line)
        if not match:
            break
        scores[name_line] = float(match.group(1))
        index += 2
    return scores


def _parse_domain_qa(lines: list[str], domain_name: str, all_domain_names: list[str]) -> list[dict[str, str]]:
    marker = [domain_name, "Question", "Response"]
    start_index = _find_sequence(lines, marker)
    if start_index is None:
        return []
    index = start_index + len(marker)

    end_index = len(lines)
    for other_name in all_domain_names:
        candidate = _find_sequence(lines, [other_name, "Question", "Response"], from_index=index)
        if candidate is not None:
            end_index = min(end_index, candidate)

    pairs: list[dict[str, str]] = []
    while index < end_index:
        # accumulate the question, which may wrap across lines, until one ends in "?"/":".
        question_parts: list[str] = []
        while index < end_index:
            question_parts.append(lines[index])
            terminated = lines[index].endswith("?") or lines[index].endswith(":")
            index += 1
            if terminated:
                break
        if not question_parts:
            break
        question = " ".join(question_parts).rstrip("?:").strip()
        if index >= end_index:
            break  # no response left for a trailing, incomplete question

        # accumulate the response, which may itself wrap across a line break. A wrapped
        # continuation is a short fragment starting lowercase (e.g. "twitch"); every real
        # question in this form starts with a capital letter, so that distinguishes "more
        # of this answer" from "the next question".
        response_parts = [lines[index]]
        index += 1
        while (
            index < end_index
            and not lines[index].endswith("?")
            and not lines[index].endswith(":")
            and lines[index][:1].islower()
        ):
            response_parts.append(lines[index])
            index += 1

        pairs.append({"item": question, "reported_response": " ".join(response_parts).strip()})
    return pairs


def _find_sequence(lines: list[str], sequence: list[str], from_index: int = 0) -> int | None:
    n = len(sequence)
    for index in range(from_index, len(lines) - n + 1):
        if lines[index : index + n] == sequence:
            return index
    return None


# ----------------------------------------------------------------------
# second pass: anchor Q&A pairs against a canonical, dataset-wide question list
# ----------------------------------------------------------------------
def _reconcile_domain_questions(reports: list[dict[str, Any]]) -> None:
    """Fix line-wrap ambiguity the first pass cannot always resolve on its own.

    The line-based parser in `_parse_domain_qa` occasionally cannot tell a wrapped
    response continuation from the start of the next question (e.g. a continuation
    like "Thrombocytopenia (Low)" that happens to start with a capital letter). Since
    the same ~9 questions recur verbatim across every visit of this form, the fix is
    to first harvest the questions that a MAJORITY of reports parsed identically for
    each domain -- those are trustworthy -- and then re-derive every report's pairs by
    anchoring on that known text directly against the joined page text, which sidesteps
    the line-wrap ambiguity entirely.
    """

    if not reports:
        return
    domain_names = list(dict.fromkeys(name for report in reports for name in report["_domain_names"]))

    canonical: dict[str, list[str]] = {}
    for domain in domain_names:
        counts: "OrderedDict[str, int]" = OrderedDict()
        for report in reports:
            for pair in report["domains"].get(domain, []):
                counts[pair["item"]] = counts.get(pair["item"], 0) + 1
        threshold = len(reports) / 2
        canonical[domain] = [item for item, count in counts.items() if count >= threshold]

    for report in reports:
        lines = report["_lines"]
        domain_names_here = report["_domain_names"]
        for domain in domain_names_here:
            known_questions = canonical.get(domain) or []
            if not known_questions:
                continue
            start_index = _find_sequence(lines, [domain, "Question", "Response"])
            if start_index is None:
                continue
            start_index += 3
            end_index = len(lines)
            for other in domain_names_here:
                candidate = _find_sequence(lines, [other, "Question", "Response"], from_index=start_index)
                if candidate is not None:
                    end_index = min(end_index, candidate)
            reparsed = _anchor_domain_qa(lines[start_index:end_index], known_questions)
            if reparsed is not None:
                report["domains"][domain] = reparsed

    for report in reports:
        report.pop("_lines", None)
        report.pop("_domain_names", None)


def _anchor_domain_qa(block_lines: list[str], known_questions: list[str]) -> list[dict[str, str]] | None:
    """Re-derive Q&A pairs for one domain block using known question text as anchors."""

    block_text = " ".join(block_lines)
    block_text = re.sub(r"\s+", " ", block_text).strip()

    positions: list[tuple[int, int, str]] = []
    for question in known_questions:
        match = re.search(re.escape(question) + r"\s*[?:]", block_text)
        if match is None:
            return None  # a known question is missing from this report; keep the first-pass result
        positions.append((match.start(), match.end(), question))
    positions.sort(key=lambda item: item[0])

    pairs: list[dict[str, str]] = []
    for index, (_start, end, question) in enumerate(positions):
        next_start = positions[index + 1][0] if index + 1 < len(positions) else len(block_text)
        response = block_text[end:next_start].strip()
        pairs.append({"item": question, "reported_response": response})
    return pairs


# ----------------------------------------------------------------------
# report assembly
# ----------------------------------------------------------------------
def build_report(reports: list[dict[str, Any]], manifest: dict[str, Any], input_dir: Path) -> dict[str, Any]:
    if not reports:
        return _empty_report(manifest, input_dir)

    dates = [report["report_date"] for report in reports if report["report_date"]]
    first = reports[0]
    patient_profile = {
        "patient_name": first.get("patient_name"),
        "patient_id": first.get("patient_id"),
        "age": first.get("age"),
        "sex": first.get("sex"),
        "diagnosis_date": first.get("diagnosis_date"),
        "current_medications": first.get("current_medications") or [],
        "seizure_history": first.get("seizure_history") or [],
    }

    domain_names = _all_domain_names(reports)
    domain_score_trends = {_slugify_domain(name): _build_domain_trend(name, reports) for name in domain_names}
    latest_domain_summaries = _build_latest_domain_summaries(domain_names, reports)
    recurring_findings = _build_recurring_findings(domain_names, reports)

    low_domains = [
        key for key, trend in domain_score_trends.items() if trend["values"] and trend["values"][-1]["status"] == "low"
    ]
    changed_domains = [
        key for key, trend in domain_score_trends.items() if trend["first_score"] != trend["latest_score"]
    ]

    return {
        "report_label": REPORT_LABEL,
        "patient_profile": patient_profile,
        "observation_window": {
            "start_date": dates[0] if dates else None,
            "end_date": dates[-1] if dates else None,
            "number_of_questionnaires": len(reports),
            "source_folder": str(input_dir),
        },
        "domain_score_trends": domain_score_trends,
        "latest_domain_summaries": latest_domain_summaries,
        "recurring_patient_reported_findings": recurring_findings,
        "digital_twin_state": {
            "latest_low_scoring_domains": low_domains,
            "domains_with_score_change": changed_domains,
            "monitoring_value": "longitudinal_questionnaire_monitoring_available",
        },
        "executive_summary": {
            "summary": (
                f"{len(reports)} questionnaire report(s) from {dates[0]} to {dates[-1]} were processed. "
                f"Latest low-scoring domain(s): {', '.join(low_domains) if low_domains else 'none'}."
            ),
            "limitations": (
                "Generated from questionnaire PDF values only. This report summarizes patient-reported "
                "responses and score trends; it does not infer diagnoses or unsupported clinical risk."
            ),
        },
        "processing_manifest": manifest,
    }


def _all_domain_names(reports: list[dict[str, Any]]) -> list[str]:
    names: "OrderedDict[str, None]" = OrderedDict()
    for report in reports:
        for name in report["domain_scores"]:
            names.setdefault(name, None)
    return list(names)


def _build_domain_trend(domain_name: str, reports: list[dict[str, Any]]) -> dict[str, Any]:
    values = []
    for report in reports:
        score = report["domain_scores"].get(domain_name)
        if score is None:
            continue
        values.append(
            {
                "date": report["report_date"],
                "score": score,
                "status": _status_for_score(score),
                "scale": "0-100 higher is better",
            }
        )
    scores = [item["score"] for item in values]
    return {
        "domain": domain_name,
        "values": values,
        "first_score": scores[0] if scores else None,
        "latest_score": scores[-1] if scores else None,
        "minimum_score": min(scores) if scores else None,
        "maximum_score": max(scores) if scores else None,
        "trend_direction": _trend_direction(scores),
    }


def _build_latest_domain_summaries(domain_names: list[str], reports: list[dict[str, Any]]) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for name in domain_names:
        latest = next((report for report in reversed(reports) if name in report["domain_scores"]), None)
        if latest is None:
            continue
        score = latest["domain_scores"][name]
        status = _status_for_score(score)
        responses = latest["domains"].get(name, [])
        summary_clauses = "; ".join(f"{item['item']}: {item['reported_response']}" for item in responses[:5])
        key = DOMAIN_SHORT_KEY.get(name, _slugify_domain(name))
        summaries[key] = {
            "score": score,
            "status": status,
            "clinically_relevant_responses": responses,
            "summary": f"{name} score was {score}/100 ({status}). {summary_clauses}.".strip(),
        }
    return summaries


def _build_recurring_findings(domain_names: list[str], reports: list[dict[str, Any]]) -> dict[str, Any]:
    findings: dict[str, Any] = {}
    for name in domain_names:
        key = DOMAIN_SHORT_KEY.get(name, _slugify_domain(name))
        by_item: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        for report in reports:
            for pair in report["domains"].get(name, []):
                item = pair["item"]
                entry = by_item.setdefault(item, {"item": item, "reported_responses": [], "dates": []})
                if pair["reported_response"] not in entry["reported_responses"]:
                    entry["reported_responses"].append(pair["reported_response"])
                entry["dates"].append(report["report_date"])
        entries = [
            {**entry, "occurrence_count": len(entry["dates"])}
            for entry in by_item.values()
        ]
        entries.sort(key=lambda entry: -entry["occurrence_count"])
        findings[key] = entries
    return findings


def _status_for_score(score: float) -> str:
    if score < 60:
        return "low"
    if score < 80:
        return "moderate"
    return "favorable"


def _trend_direction(scores: list[float]) -> str:
    if len(scores) < 2:
        return "insufficient_data"
    if scores[-1] == scores[0]:
        return "stable"
    return "increasing" if scores[-1] > scores[0] else "decreasing"


def _slugify_domain(name: str) -> str:
    text = name.lower().replace("&", " ")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _regex(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _first_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d-%b-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return value


def _empty_report(manifest: dict[str, Any], input_dir: Path) -> dict[str, Any]:
    return {
        "report_label": REPORT_LABEL,
        "patient_profile": {
            "patient_name": None,
            "patient_id": None,
            "age": None,
            "sex": None,
            "diagnosis_date": None,
            "current_medications": [],
            "seizure_history": [],
        },
        "observation_window": {
            "start_date": None,
            "end_date": None,
            "number_of_questionnaires": 0,
            "source_folder": str(input_dir),
        },
        "domain_score_trends": {},
        "latest_domain_summaries": {},
        "recurring_patient_reported_findings": {},
        "digital_twin_state": {
            "latest_low_scoring_domains": [],
            "domains_with_score_change": [],
            "monitoring_value": "longitudinal_questionnaire_monitoring_available",
        },
        "executive_summary": {
            "summary": "No questionnaire reports were available to process.",
            "limitations": (
                "Generated from questionnaire PDF values only. This report summarizes patient-reported "
                "responses and score trends; it does not infer diagnoses or unsupported clinical risk."
            ),
        },
        "processing_manifest": manifest,
    }


if __name__ == "__main__":
    raise SystemExit(main())
