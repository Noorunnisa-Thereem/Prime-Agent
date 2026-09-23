"""ETL loader: reads data/Final_merged_sheet.xlsx and populates the three
CPIC tables in cpic_db (cpic_source_documents, cpic_findings,
cpic_finding_variants). The source xlsx file itself is only read, never
modified.

Pipeline, in order:
  1. Drop the 5 fully-blank rows and the 1 stray annotation row
     (Drug Name == "21716271_1 to 21716271_107").
  2. Normalize literal "Null"/"null"/"NULL" strings (any casing) to real
     SQL NULL across every column.
  3. Drop exact full-row duplicates (the ~9 pairs / 18 rows found during
     inspection), keeping the first occurrence of each.
  4. Split "PDF Name" into source_pubmed_id / extract_seq via regex and
     populate cpic_source_documents (one row per distinct PDF Name).
  5. Collapse "variant-alias groups" -- rows identical in every column
     except "Genetic Variant(s) & RsID" -- into a single cpic_findings
     row, moving each row's variant value into cpic_finding_variants.
     Cells that pack multiple variants together (comma / semicolon /
     "and"-separated) are split into separate variant rows too.
  6. Compute a sha256 row_hash per finding (over its own field values,
     not including the variants that now live in the child table).

Credentials come from the environment only (db.py); PGPASSWORD must already
be set in the calling shell.

Usage:
    python load_data.py [path/to/Final_merged_sheet.xlsx]
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from psycopg2.extras import execute_values

from db import get_connection

DEFAULT_INPUT_PATH = Path(__file__).parent.parent / "Data" / "Final_merged_sheet.xlsx"

STRAY_DRUG_NAME_VALUE = "21716271_1 to 21716271_107"

# Excel column name -> cpic_findings column name (everything except
# 'PDF Name', which drives cpic_source_documents, and
# 'Genetic Variant(s) & RsID', which drives cpic_finding_variants).
COLUMN_MAP: dict[str, str] = {
    "Gene Studied": "gene_studied",
    "Genotype": "genotype",
    "Disease Studied": "disease_studied",
    "Drug Name": "drug_name",
    "Dosage Form": "dosage_form",
    "Drug Class": "drug_class",
    "Route of Medication": "route_of_medication",
    "Duration of Medication": "duration_of_medication",
    "Dosage Adjustments": "dosage_adjustments",
    "Combination Therapies": "combination_therapies",
    "Response Category": "response_category",
    "Therapeutic Interaction": "therapeutic_interaction",
    "Total Participants": "total_participants",
    "Cases/Control": "cases_control",
    "No. of SNP Selected": "no_of_snp_selected",
    "Measure of Response": "measure_of_response",
    "Dosage-Effect Relationship": "dosage_effect_relationship",
    "Frequency of Genetic Variants (%)": "frequency_of_genetic_variants_pct",
    "Association Stats": "association_stats",
    "Core Finding": "core_finding",
    "Alert Point": "alert_point",
    "Ethnicity": "ethnicity",
    "Page(s)": "page_ref",
}

VARIANT_COLUMN = "Genetic Variant(s) & RsID"
PDF_NAME_COLUMN = "PDF Name"

PDF_NAME_RE = re.compile(r"^\s*(\d+)_(\d+)\.pdf\s*$", re.IGNORECASE)
VARIANT_SPLIT_RE = re.compile(r"\s*(?:,|;|\band\b)\s*", re.IGNORECASE)

# Distinct from a real empty string, so a finding whose field is genuinely
# None never hashes the same as one whose field is "".
_HASH_NONE_MARKER = "\x00"
_HASH_SEP = "\x1f"

# All source columns that identify a distinct "finding" -- everything
# except the variant column itself, which is intentionally excluded so
# that rows differing only in their variant alias group together.
GROUP_COLUMNS = [PDF_NAME_COLUMN, *COLUMN_MAP.keys()]

# Sentinel used only to build a hashable/groupable per-row key; never
# stored -- real values are always read back from the original row.
_GROUP_KEY_NONE_MARKER = "\x00__NULL__\x00"


def _normalize_null_strings(df: pd.DataFrame) -> pd.DataFrame:
    """Replaces any literal 'Null'/'null'/'NULL' (any casing, with
    incidental surrounding whitespace) with real missing values, across
    every column, without altering any other text."""

    def _clean(value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() == "null":
            return None
        return value

    return df.map(_clean)


def _split_variants(raw: Any) -> list[str]:
    """Splits one 'Genetic Variant(s) & RsID' cell into its individual
    variant tokens (comma / semicolon / 'and'-separated), preserving each
    token's original text exactly. Returns [] for a missing cell."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    text = str(raw).strip()
    if not text:
        return []
    tokens = [t.strip() for t in VARIANT_SPLIT_RE.split(text)]
    return [t for t in tokens if t]


def _row_hash(pdf_name: str | None, values: dict[str, Any]) -> str:
    """``pdf_name`` and every value in ``values`` are expected to already be
    ``_to_text``-normalized (None or str), matching exactly what gets
    written to cpic_findings, so the hash can never disagree with the
    stored row."""
    parts = [pdf_name if pdf_name is not None else _HASH_NONE_MARKER]
    for col in COLUMN_MAP.values():
        v = values.get(col)
        parts.append(_HASH_NONE_MARKER if v is None else v)
    digest_input = _HASH_SEP.join(parts).encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()


def _to_none(value: Any) -> Any:
    """NaN/NaT -> None; everything else passed through unchanged."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


def _to_text(value: Any) -> str | None:
    """Canonical TEXT representation for one cell, used identically for
    both the row_hash input and the value actually stored -- so the two
    can never silently disagree (e.g. a whole-number float like 5.0 must
    not hash as "5.0" while being stored as "5", or vice versa)."""
    value = _to_none(value)
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def load_and_transform(xlsx_path: Path) -> tuple[list[tuple], list[dict], dict[int, list[str]]]:
    """Reads and transforms the source sheet. Returns:
      - source_docs: list of (pdf_name, source_pubmed_id, extract_seq) tuples, one per distinct PDF Name
      - findings: list of dicts, one per collapsed finding (includes '_pdf_name' and '_variants' keys
        alongside every cpic_findings column, for the caller to resolve FK/child rows)
    The third return value is unused (kept for clarity) -- variants are attached per finding dict.
    """
    df = pd.read_excel(xlsx_path, sheet_name="Sheet1")
    total_raw = len(df)

    # 1. Drop the 5 fully-blank rows and the 1 stray annotation row.
    fully_blank = df.isna().all(axis=1)
    is_stray = df[PDF_NAME_COLUMN].isna() & (df["Drug Name"].astype(str).str.strip() == STRAY_DRUG_NAME_VALUE)
    junk_mask = fully_blank | is_stray
    print(f"Dropping {junk_mask.sum()} junk row(s) (blank or stray annotation) out of {total_raw} raw rows.")
    df = df[~junk_mask].reset_index(drop=True)

    # 2. Normalize literal "Null"/"null"/"NULL" strings to real missing values.
    df = _normalize_null_strings(df)

    # 3. Drop exact full-row duplicates, keeping the first occurrence.
    before = len(df)
    df = df.drop_duplicates(keep="first").reset_index(drop=True)
    print(f"Dropped {before - len(df)} exact-duplicate row(s), keeping the first occurrence of each.")

    # 4. Distinct source documents, in first-seen order.
    seen_pdf_names: dict[str, None] = {}
    for name in df[PDF_NAME_COLUMN]:
        name = _to_none(name)
        if name is not None and name not in seen_pdf_names:
            seen_pdf_names[name] = None
    source_docs: list[tuple] = []
    for name in seen_pdf_names:
        match = PDF_NAME_RE.match(name)
        if match:
            source_docs.append((name, int(match.group(1)), int(match.group(2))))
        else:
            print(f"  WARNING: PDF Name '{name}' does not match '<digits>_<digits>.pdf' -- storing without source_pubmed_id/extract_seq.")
            source_docs.append((name, None, None))

    # 5. Collapse variant-alias groups into one finding each.
    groups: dict[str, dict[str, Any]] = {}
    group_order: list[str] = []
    for _, row in df.iterrows():
        key_parts = []
        for col in GROUP_COLUMNS:
            v = _to_text(row[col])
            key_parts.append(_GROUP_KEY_NONE_MARKER if v is None else v)
        key = _HASH_SEP.join(key_parts)

        if key not in groups:
            group_order.append(key)
            values = {db_col: _to_text(row[excel_col]) for excel_col, db_col in COLUMN_MAP.items()}
            groups[key] = {
                "_pdf_name": _to_text(row[PDF_NAME_COLUMN]),
                "_variant_tokens": [],  # preserves first-seen order, de-duplicated below
                **values,
            }
        variant_tokens = _split_variants(row[VARIANT_COLUMN])
        existing = groups[key]["_variant_tokens"]
        for token in variant_tokens:
            if token not in existing:
                existing.append(token)

    findings = [groups[key] for key in group_order]
    multi_variant_groups = sum(1 for f in findings if len(f["_variant_tokens"]) > 1)
    total_variant_rows = sum(len(f["_variant_tokens"]) for f in findings)
    print(f"Collapsed {len(df)} surviving rows into {len(findings)} distinct finding(s).")
    print(f"  {multi_variant_groups} finding(s) carry more than one variant alias ({total_variant_rows} variant row(s) total).")

    for f in findings:
        f["row_hash"] = _row_hash(f["_pdf_name"], f)

    return source_docs, findings, {}


def load_into_database(source_docs: list[tuple], findings: list[dict]) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # Full-refresh load: safe to re-run without accumulating duplicates.
                print("Truncating existing table contents (full-refresh load)...")
                cur.execute("TRUNCATE cpic_finding_variants, cpic_findings, cpic_source_documents RESTART IDENTITY CASCADE;")

                print(f"Inserting {len(source_docs)} source document(s)...")
                execute_values(
                    cur,
                    "INSERT INTO cpic_source_documents (pdf_name, source_pubmed_id, extract_seq) VALUES %s "
                    "RETURNING source_document_id, pdf_name",
                    source_docs,
                    fetch=False,
                )
                cur.execute("SELECT source_document_id, pdf_name FROM cpic_source_documents;")
                doc_id_by_name = {name: doc_id for doc_id, name in cur.fetchall()}

                finding_db_columns = ["source_document_id", *COLUMN_MAP.values(), "row_hash"]
                finding_rows = []
                for f in findings:
                    row = [doc_id_by_name.get(f["_pdf_name"])]
                    row.extend(f[col] for col in COLUMN_MAP.values())
                    row.append(f["row_hash"])
                    finding_rows.append(tuple(row))

                print(f"Inserting {len(finding_rows)} finding(s)...")
                finding_ids = execute_values(
                    cur,
                    f"INSERT INTO cpic_findings ({', '.join(finding_db_columns)}) VALUES %s RETURNING finding_id",
                    finding_rows,
                    fetch=True,
                )
                finding_ids = [row[0] for row in finding_ids]

                variant_rows = []
                for finding_id, f in zip(finding_ids, findings):
                    for token in f["_variant_tokens"]:
                        variant_rows.append((finding_id, token))

                print(f"Inserting {len(variant_rows)} finding-variant row(s)...")
                if variant_rows:
                    execute_values(
                        cur,
                        "INSERT INTO cpic_finding_variants (finding_id, variant_raw) VALUES %s",
                        variant_rows,
                        fetch=False,
                    )
        print("Load committed successfully.")
    finally:
        conn.close()


def main() -> int:
    xlsx_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT_PATH
    if not xlsx_path.exists():
        print(f"ERROR: source file not found: {xlsx_path}", file=sys.stderr)
        return 1

    source_docs, findings, _ = load_and_transform(xlsx_path)
    load_into_database(source_docs, findings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
