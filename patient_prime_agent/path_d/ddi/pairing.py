"""Medication pair generation.

Generates every unique current-current pair (and, structurally, current-
proposed / proposed-proposed pairs for when a proposed-medication list
exists in the source data -- today's dataset has none, so those lists are
always empty). A proposed drug is never paired against itself as "current",
and a pair's ``pair_context`` travels with it everywhere downstream so a
proposed-drug pair can never be reported as an active interaction.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

from .normalizer import Medication

PairContext = str  # "current_current" | "current_proposed" | "proposed_proposed"


def generate_pairs(
    current: list[Medication],
    proposed: list[Medication] | None = None,
) -> list[dict[str, Any]]:
    proposed = proposed or []
    pairs: list[dict[str, Any]] = []

    for drug_a, drug_b in combinations(sorted(current, key=lambda m: m.normalized_name), 2):
        pairs.append({"drug_a": drug_a, "drug_b": drug_b, "pair_context": "current_current"})

    for drug_a in current:
        for drug_b in proposed:
            pairs.append({"drug_a": drug_a, "drug_b": drug_b, "pair_context": "current_proposed"})

    for drug_a, drug_b in combinations(sorted(proposed, key=lambda m: m.normalized_name), 2):
        pairs.append({"drug_a": drug_a, "drug_b": drug_b, "pair_context": "proposed_proposed"})

    return pairs
