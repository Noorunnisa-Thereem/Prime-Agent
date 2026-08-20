from __future__ import annotations

import csv
import html
import io
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from typing import Any

from .config import CATEGORY_ALIASES, CATEGORY_ORDER
from .models import LoadedDocument
from .utils import normalize_whitespace, read_text


SUPPORTED_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".json", ".xml", ".html", ".htm"}
SUPPORTED_BINARY_EXTENSIONS = {".docx", ".pdf"}


def normalize_category_name(name: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "", name.lower())
    return CATEGORY_ALIASES.get(normalized)


def infer_category_from_path(path: Path) -> str | None:
    for part in reversed(path.parts):
        category = normalize_category_name(part)
        if category:
            return category
    stem_category = normalize_category_name(path.stem)
    if stem_category:
        return stem_category
    return None


def collect_files(data_root: Path) -> dict[str, list[Path]]:
    files_by_category: dict[str, list[Path]] = {category: [] for category in CATEGORY_ORDER}
    if not data_root.exists():
        return files_by_category
    for path in data_root.rglob("*"):
        if not path.is_file():
            continue
        category = infer_category_from_path(path)
        if category is None:
            continue
        files_by_category.setdefault(category, []).append(path)
    for category in files_by_category:
        files_by_category[category] = sorted(files_by_category[category])
    return files_by_category


def load_document(path: Path, category: str | None = None) -> LoadedDocument:
    category = category or infer_category_from_path(path) or "unknown"
    suffix = path.suffix.lower()
    notes: list[str] = []
    structured: Any | None = None

    if suffix in SUPPORTED_TEXT_EXTENSIONS:
        text = _load_text_document(path)
        if suffix == ".json":
            try:
                structured = json.loads(text)
                text = json.dumps(structured, indent=2, ensure_ascii=False, sort_keys=True)
            except Exception as exc:
                notes.append(f"json_parse_failed: {exc}")
        elif suffix == ".csv" or suffix == ".tsv":
            try:
                structured = _load_tabular_document(text, suffix)
            except Exception as exc:
                notes.append(f"tabular_parse_failed: {exc}")
        elif suffix == ".xml":
            try:
                structured = _load_xml_document(text)
                text = structured.get("_text", text)
            except Exception as exc:
                notes.append(f"xml_parse_failed: {exc}")
        elif suffix in {".html", ".htm"}:
            structured = {"_text": _strip_html(text)}
            text = structured["_text"]
    elif suffix == ".docx":
        try:
            text = _load_docx_document(path)
            structured = {"_text": text}
        except Exception as exc:
            notes.append(f"docx_parse_failed: {exc}")
            text = ""
    elif suffix == ".pdf":
        try:
            text = _load_pdf_document(path)
            structured = {"_text": text}
        except Exception as exc:
            notes.append(f"pdf_parse_failed: {exc}")
            text = ""
    else:
        notes.append(f"unsupported_extension:{suffix}")
        text = ""

    text = normalize_whitespace(text)
    lines = text.splitlines() if text else []
    return LoadedDocument(path=path, category=category, text=text, lines=lines, structured=structured, notes=notes)


def _load_text_document(path: Path) -> str:
    return read_text(path)


def _load_tabular_document(text: str, suffix: str) -> list[dict[str, Any]]:
    delimiter = "\t" if suffix == ".tsv" else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return [dict(row) for row in reader]


def _load_xml_document(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    joined_text = " ".join(part.strip() for part in root.itertext() if part and part.strip())
    return {"_text": normalize_whitespace(joined_text), "_root_tag": root.tag}


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return normalize_whitespace(text)


def _load_docx_document(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        document_xml = archive.read("word/document.xml")
    root = ET.fromstring(document_xml)
    namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespaces):
        text_fragments = [node.text or "" for node in paragraph.findall(".//w:t", namespaces)]
        paragraph_text = "".join(text_fragments).strip()
        if paragraph_text:
            paragraphs.append(paragraph_text)
    return normalize_whitespace("\n".join(paragraphs))


def _load_pdf_document(path: Path) -> str:
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name, fromlist=["PdfReader"])
            reader = module.PdfReader(str(path))
            pages = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
            return normalize_whitespace("\n".join(pages))
        except Exception:
            continue
    raise RuntimeError("PDF text extraction requires pypdf or PyPDF2")

