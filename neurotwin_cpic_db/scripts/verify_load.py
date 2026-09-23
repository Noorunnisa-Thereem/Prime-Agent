"""Verifies the CPIC load: row counts on all three tables, plus sample
queries (all findings for one drug, all findings for one gene) so the
result can be compared against the source sheet.

Credentials come from the environment only (db.py); PGPASSWORD must already
be set in the calling shell. Read-only -- makes no changes to the database.

Usage:
    python verify_load.py
"""

from __future__ import annotations

from db import get_connection


def _print_table(headers: list[str], rows: list[tuple], max_width: int = 60) -> None:
    def _cell(v) -> str:
        s = "" if v is None else str(v)
        return s if len(s) <= max_width else s[: max_width - 3] + "..."

    widths = [len(h) for h in headers]
    str_rows = [[_cell(v) for v in row] for row in rows]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _fmt(row: list[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    print(_fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for row in str_rows:
        print(_fmt(row))


def main() -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            print("=== Row counts ===")
            for table in ("cpic_source_documents", "cpic_findings", "cpic_finding_variants"):
                cur.execute(f"SELECT count(*) FROM {table};")
                print(f"  {table}: {cur.fetchone()[0]}")

            print()
            print("=== Findings with more than one variant alias (sanity check) ===")
            cur.execute(
                """
                SELECT count(*) FROM (
                    SELECT finding_id FROM cpic_finding_variants
                    GROUP BY finding_id HAVING count(*) > 1
                ) sub;
                """
            )
            print(f"  findings with >1 variant row: {cur.fetchone()[0]}")

            print()
            print("=== Sample query: all findings for one drug ===")
            cur.execute(
                """
                SELECT drug_name, count(*) AS n
                FROM cpic_findings
                WHERE drug_name IS NOT NULL
                GROUP BY drug_name
                ORDER BY n DESC
                LIMIT 1;
                """
            )
            drug_name, drug_count = cur.fetchone()
            print(f"  Most frequent drug in the loaded data: '{drug_name}' ({drug_count} finding rows)")
            cur.execute(
                """
                SELECT f.finding_id, sd.pdf_name, f.gene_studied, f.disease_studied,
                       f.response_category, f.core_finding
                FROM cpic_findings f
                LEFT JOIN cpic_source_documents sd ON sd.source_document_id = f.source_document_id
                WHERE f.drug_name = %s
                ORDER BY f.finding_id
                LIMIT 5;
                """,
                (drug_name,),
            )
            rows = cur.fetchall()
            _print_table(["finding_id", "pdf_name", "gene_studied", "disease_studied", "response_category", "core_finding"], rows)
            print(f"  (showing 5 of {drug_count})")

            print()
            print("=== Sample query: all findings for one gene, with their variant aliases ===")
            cur.execute(
                """
                SELECT gene_studied, count(*) AS n
                FROM cpic_findings
                WHERE gene_studied IS NOT NULL
                GROUP BY gene_studied
                ORDER BY n DESC
                LIMIT 1;
                """
            )
            gene_name, gene_count = cur.fetchone()
            print(f"  Most frequent gene in the loaded data: '{gene_name}' ({gene_count} finding rows)")
            cur.execute(
                """
                SELECT f.finding_id, sd.pdf_name, f.drug_name, f.disease_studied,
                       COALESCE(array_agg(v.variant_raw ORDER BY v.finding_variant_id) FILTER (WHERE v.variant_raw IS NOT NULL), '{}') AS variants
                FROM cpic_findings f
                LEFT JOIN cpic_source_documents sd ON sd.source_document_id = f.source_document_id
                LEFT JOIN cpic_finding_variants v ON v.finding_id = f.finding_id
                WHERE f.gene_studied = %s
                GROUP BY f.finding_id, sd.pdf_name, f.drug_name, f.disease_studied
                ORDER BY f.finding_id
                LIMIT 5;
                """,
                (gene_name,),
            )
            rows = cur.fetchall()
            _print_table(["finding_id", "pdf_name", "drug_name", "disease_studied", "variants"], rows)
            print(f"  (showing 5 of {gene_count})")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
