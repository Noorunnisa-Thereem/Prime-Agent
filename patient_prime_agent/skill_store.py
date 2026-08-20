from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CATEGORY_ORDER
from .utils import atomic_write_text, ensure_dir, read_text, utc_now_iso


@dataclass(slots=True)
class SkillDoc:
    name: str
    path: Path
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": str(self.path), "content": self.content}


class SkillRegistry:
    def __init__(self, skills_root: Path):
        self.skills_root = ensure_dir(skills_root)
        self.root_skill_path = self.skills_root / "SKILL.md"
        self._cache: dict[str, SkillDoc] = {}

    def load(self, category: str) -> SkillDoc:
        path = self.skills_root / category / "SKILL.md"
        if not path.exists():
            raise FileNotFoundError(f"Missing skill document: {path}")
        if category in self._cache:
            return self._cache[category]
        content = read_text(path)
        doc = SkillDoc(name=category, path=path, content=content)
        self._cache[category] = doc
        return doc

    def load_all(self) -> list[SkillDoc]:
        docs: list[SkillDoc] = []
        if self.root_skill_path.exists():
            docs.append(SkillDoc(name="root", path=self.root_skill_path, content=read_text(self.root_skill_path)))
        for category in CATEGORY_ORDER:
            try:
                docs.append(self.load(category))
            except FileNotFoundError:
                continue
        return docs

    def append_refinement(self, category: str, note: str) -> None:
        path = self.skills_root / category / "SKILL.md"
        ensure_dir(path.parent)
        if not path.exists():
            raise FileNotFoundError(f"Missing skill document: {path}")
        content = read_text(path)
        marker = "## Learned Refinements"
        timestamp = utc_now_iso()
        note_line = f"- {timestamp}: {note.strip()}"
        if marker not in content:
            content = content.rstrip() + f"\n\n{marker}\n{note_line}\n"
        elif note_line not in content:
            content = content.rstrip() + f"\n{note_line}\n"
        atomic_write_text(path, content)
        self._cache.pop(category, None)

