from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CATEGORY_ORDER
from .models import PlanStep


@dataclass(slots=True)
class PlannedCategory:
    category: str
    files: list[Path]


class TaskPlanner:
    def plan(self, files_by_category: dict[str, list[Path]]) -> list[PlanStep]:
        plan: list[PlanStep] = []
        for order, category in enumerate(CATEGORY_ORDER, start=1):
            plan.append(
                PlanStep(
                    order=order,
                    category=category,
                    action="extract_and_validate",
                    file_count=len(files_by_category.get(category, [])),
                )
            )
        return plan

