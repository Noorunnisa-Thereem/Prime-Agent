from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Iterable

from ..models import Evidence, LoadedDocument
from ..utils import compact_excerpt, dedupe_preserve_order, normalize_whitespace, slugify


DATE_PATTERNS = (
    re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+(?:19|20)?\d{2,4}\b"),
)


def normalize_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def textify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def flatten_structured(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            items.extend(flatten_structured(child, child_prefix))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_prefix = f"{prefix}[{index}]"
            items.extend(flatten_structured(child, child_prefix))
    else:
        items.append((prefix, value))
    return items


def search_structured_value(structured: Any, aliases: Iterable[str]) -> tuple[str, Any] | None:
    alias_norms = [normalize_key(alias) for alias in aliases]
    for path, value in flatten_structured(structured):
        normalized_path = normalize_key(path)
        if any(alias in normalized_path for alias in alias_norms):
            return path, value
    return None


def line_items(document: LoadedDocument) -> list[tuple[int, str]]:
    return [(index + 1, line.strip()) for index, line in enumerate(document.lines) if line.strip()]


def capture_section(document: LoadedDocument, aliases: Iterable[str]) -> tuple[str | None, list[int]]:
    if not document.lines:
        return None, []
    alias_norms = [normalize_key(alias) for alias in aliases]
    for index, line in enumerate(document.lines):
        stripped = line.strip()
        if not stripped:
            continue
        heading_part = stripped.split(":", 1)[0]
        normalized_heading = normalize_key(heading_part)
        if not any(normalized_heading == alias or normalized_heading.startswith(alias) for alias in alias_norms):
            continue
        block: list[str] = []
        if ":" in stripped:
            tail = stripped.split(":", 1)[1].strip()
            if tail:
                block.append(tail)
        for following in document.lines[index + 1 :]:
            following = following.strip()
            if not following:
                if block:
                    break
                continue
            if looks_like_heading(following):
                break
            block.append(following)
        if block:
            return normalize_whitespace("\n".join(block)), [index + 1]
    return None, []


def looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 120:
        return False
    if stripped.endswith(":") and len(stripped.split()) <= 8:
        return True
    if stripped.isupper() and len(stripped.split()) <= 12:
        return True
    heading_words = (
        "history of present illness",
        "chief complaint",
        "assessment",
        "plan",
        "impression",
        "findings",
        "diagnosis",
        "diagnoses",
        "medications",
        "allergies",
        "symptoms",
        "background",
        "interpretation",
        "conclusion",
    )
    normalized = normalize_key(stripped.split(":", 1)[0])
    return any(normalized == normalize_key(word) or normalized.startswith(normalize_key(word)) for word in heading_words)


def split_list_items(text: str) -> list[str]:
    if not text:
        return []
    pieces = re.split(r"[\n•·;|]+", text)
    items: list[str] = []
    for piece in pieces:
        candidate = piece.strip().strip("-*").strip()
        if not candidate:
            continue
        candidate = re.sub(r"^\d+[.)]\s*", "", candidate)
        if ":" in candidate and len(candidate.split(":", 1)[0].split()) <= 4:
            left, right = candidate.split(":", 1)
            if right.strip():
                candidate = right.strip()
        if candidate.lower() in {"n/a", "na", "none", "not applicable"}:
            continue
        items.append(candidate)
    return dedupe_preserve_order(items)


def split_sentences(text: str) -> list[str]:
    if not text:
        return []
    normalized = normalize_whitespace(text)
    chunks = re.split(r"(?<=[.!?])\s+|\n+", normalized)
    sentences = [chunk.strip() for chunk in chunks if chunk.strip()]
    return dedupe_preserve_order(sentences)


def extract_sentences_with_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    normalized_keywords = [keyword.lower() for keyword in keywords]
    matches: list[str] = []
    for sentence in split_sentences(text):
        lowered = sentence.lower()
        if any(keyword in lowered for keyword in normalized_keywords):
            matches.append(sentence)
    return dedupe_preserve_order(matches)


def find_date_string(text: str) -> str | None:
    if not text:
        return None
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        candidate = match.group(0)
        normalized = _normalize_date_candidate(candidate)
        if normalized:
            return normalized
    return None


def _normalize_date_candidate(candidate: str) -> str | None:
    candidate = candidate.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
            return candidate
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", candidate):
            month, day, year = candidate.split("/")
            year = year if len(year) == 4 else f"20{year.zfill(2)}"
            return f"{year}-{int(month):02d}-{int(day):02d}"
        parsed = datetime.strptime(candidate, "%d %B %Y")
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        try:
            parsed = datetime.strptime(candidate, "%d %b %Y")
            return parsed.strftime("%Y-%m-%d")
        except Exception:
            return None


def extract_value_after_alias(line: str, aliases: Iterable[str]) -> str | None:
    normalized_aliases = [re.escape(alias) for alias in aliases]
    if not normalized_aliases:
        return None
    pattern = re.compile(rf"(?i)\b(?:{'|'.join(normalized_aliases)})\b\s*[:=\-]?\s*(?P<value>.+)$")
    match = pattern.search(line.strip())
    if not match:
        return None
    value = match.group("value").strip()
    value = value.lstrip(":=-").strip()
    return value or None


def extract_first_number(text: str) -> float | int | None:
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None
    value = float(match.group(0))
    return int(value) if value.is_integer() else value


def find_metric_in_document(document: LoadedDocument, aliases: Iterable[str]) -> tuple[float | int | None, Evidence | None]:
    if document.structured is not None:
        structured_match = search_structured_value(document.structured, aliases)
        if structured_match is not None:
            path, value = structured_match
            number = extract_first_number(textify(value))
            if number is not None:
                return number, Evidence(
                    section="structured",
                    field=normalize_key(path),
                    source_file=str(document.path),
                    source_lines=[],
                    source_excerpt=compact_excerpt(f"{path}: {textify(value)}"),
                )

    alias_pattern = [normalize_key(alias) for alias in aliases]
    for line_no, line in line_items(document):
        if not any(alias in normalize_key(line) for alias in alias_pattern):
            continue
        value_text = extract_value_after_alias(line, aliases)
        candidate = value_text or line
        number = extract_first_number(candidate)
        if number is None:
            continue
        return number, Evidence(
            section="text",
            field=normalize_key(next(iter(aliases), "metric")),
            source_file=str(document.path),
            source_lines=[line_no],
            source_excerpt=compact_excerpt(line),
        )
    return None, None


def find_text_value_in_document(document: LoadedDocument, aliases: Iterable[str]) -> tuple[str | None, Evidence | None]:
    if document.structured is not None:
        structured_match = search_structured_value(document.structured, aliases)
        if structured_match is not None:
            path, value = structured_match
            value_text = textify(value).strip()
            if value_text:
                return value_text, Evidence(
                    section="structured",
                    field=normalize_key(path),
                    source_file=str(document.path),
                    source_lines=[],
                    source_excerpt=compact_excerpt(f"{path}: {value_text}"),
                )

    alias_pattern = [normalize_key(alias) for alias in aliases]
    for line_no, line in line_items(document):
        if not any(alias in normalize_key(line) for alias in alias_pattern):
            continue
        value_text = extract_value_after_alias(line, aliases)
        if value_text:
            return value_text, Evidence(
                section="text",
                field=normalize_key(next(iter(aliases), "field")),
                source_file=str(document.path),
                source_lines=[line_no],
                source_excerpt=compact_excerpt(line),
            )
    return None, None


def maybe_label_from_filename(document: LoadedDocument, keywords: Iterable[str]) -> str | None:
    normalized = normalize_key(document.path.stem)
    for keyword in keywords:
        if normalize_key(keyword) in normalized:
            return keyword
    return None

