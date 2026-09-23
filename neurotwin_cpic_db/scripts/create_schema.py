"""Creates the three CPIC tables (cpic_source_documents, cpic_findings,
cpic_finding_variants) in the cpic_db PostgreSQL database, exactly as
proposed from the source-file inspection -- see schema.sql for the DDL and
its design rationale.

Credentials come from the environment only (db.py); PGPASSWORD must already
be set in the calling shell. This script does not touch the source xlsx
file and does not load any data -- see load_data.py for that.

Usage:
    python create_schema.py
"""

from __future__ import annotations

from pathlib import Path

from db import get_connection

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def main() -> int:
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
        print("Schema applied successfully: cpic_source_documents, cpic_findings, cpic_finding_variants.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
