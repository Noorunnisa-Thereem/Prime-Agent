"""Builds the Green Flags / Red Flags drug-drug-interaction table.

Inputs (read-only, never modified):
  - reports/genetics/genetics_clinical_summary.json  -- the NeuroTwin drug
    extraction report (44 distinct drug names across 5 therapeutic classes,
    itself derived from the patient's PGx panel).
  - The NeuroPrecisionDx PGx report (Sample ID 1325006529, pages 2-3) --
    used to cross-check the extracted drug names against the same
    class-grouped, color-coded drug list.

Every pair below was independently confirmed against open pharmacology
sources (drug labels, PubMed/peer-reviewed literature, and open
interaction-checker summaries) during authoring -- see the chat record for
the specific sources checked per pair. This script only renders that
already-confirmed content into a spreadsheet; it does not compute or infer
any interaction itself.

Does not touch neurotwin_cpic_db or any CPIC data, per instruction.
"""

from __future__ import annotations

import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# Project root (D:\Prime Agent) on sys.path so this script runs standalone
# (python build_flags_table.py) without requiring patient_prime_agent to be
# pip-installed first.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from patient_prime_agent.ddi_flag_data import GREEN_FLAGS, RED_FLAGS  # noqa: E402

OUTPUT_PATH = Path(__file__).parent / "Drug_Interaction_Green_Red_Flags.xlsx"

FONT_NAME = "Arial"

GREEN_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
RED_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
HEADER_FONT = Font(name=FONT_NAME, bold=True, size=13, color="FFFFFF")
TITLE_FONT = Font(name=FONT_NAME, bold=True, size=14)
SUB_FONT = Font(name=FONT_NAME, italic=True, size=9, color="555555")
_PAIR_INLINE_FONT = InlineFont(rFont=FONT_NAME, sz="11", b=True)
_BODY_INLINE_FONT = InlineFont(rFont=FONT_NAME, sz="10")


def _cell_rich_text(drug_a: str, drug_b: str, bullets: list[str]) -> CellRichText:
    """Bold drug-pair name on its own line, followed by plain-font bullets --
    one cell, two visual weights, via openpyxl's rich-text run support."""
    body = "\n" + "\n".join(f"• {b}" for b in bullets)
    return CellRichText(
        TextBlock(_PAIR_INLINE_FONT, f"{drug_a} + {drug_b}"),
        TextBlock(_BODY_INLINE_FONT, body),
    )


def build() -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "DDI Green-Red Flags"

    ws.merge_cells("A1:B1")
    ws["A1"] = "Drug-Drug Interaction Flags — NeuroTwin Extracted Drug List"
    ws["A1"].font = TITLE_FONT

    ws.merge_cells("A2:B2")
    ws["A2"] = (
        "Source: reports/genetics/genetics_clinical_summary.json (44 drugs, NeuroTwin PGx extraction) "
        "cross-checked against the NeuroPrecisionDx PGx report (Sample ID 1325006529, pp. 2–3). "
        "Each pair confirmed against open pharmacology sources (drug labels, peer-reviewed literature). "
        "Decision-support reference only — not a prescribing instruction."
    )
    ws["A2"].font = SUB_FONT
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[2].height = 42

    header_row = 3
    ws.cell(row=header_row, column=1, value="Green Flags (no significant interaction)").font = HEADER_FONT
    ws.cell(row=header_row, column=2, value="Red Flags (clinically significant interaction)").font = HEADER_FONT
    for col in (1, 2):
        cell = ws.cell(row=header_row, column=col)
        cell.fill = PatternFill(start_color="10243C", end_color="10243C", fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[header_row].height = 24

    max_rows = max(len(GREEN_FLAGS), len(RED_FLAGS))
    start_row = header_row + 1
    for i in range(max_rows):
        row = start_row + i
        if i < len(GREEN_FLAGS):
            drug_a, drug_b, bullets = GREEN_FLAGS[i]
            cell = ws.cell(row=row, column=1, value=_cell_rich_text(drug_a, drug_b, bullets))
            cell.fill = GREEN_FILL
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        if i < len(RED_FLAGS):
            drug_a, drug_b, bullets = RED_FLAGS[i]
            cell = ws.cell(row=row, column=2, value=_cell_rich_text(drug_a, drug_b, bullets))
            cell.fill = RED_FILL
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[row].height = 60

    ws.column_dimensions[get_column_letter(1)].width = 62
    ws.column_dimensions[get_column_letter(2)].width = 62

    ws.freeze_panes = f"A{start_row}"

    wb.save(OUTPUT_PATH)
    return OUTPUT_PATH


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path}")
