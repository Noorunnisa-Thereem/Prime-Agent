"""The Continual Harness: Prompt + Skills + Memory + Sub-agent configuration.

The harness is the thing that is *continually* refined.  It is a single
persisted document with four components:

``prompt``     the system prompt, objective and standing policies
``skills``     which ``SKILL.md`` file backs each category, plus applied refinements
``memory``     memory policy (scopes, retention, refinement threshold)
``subagents``  per-category sub-agent configuration (instruction, schema, retries)

Every mutation can be committed as a revision under
``memory/harness/revisions/``, which is what makes :meth:`ContinualHarness.rollback`
possible after a bad refinement.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from ..config import CATEGORY_LABELS, CATEGORY_ORDER, DEFAULT_OBJECTIVE, ProjectPaths
from ..utils import atomic_write_json, ensure_dir, read_json, utc_now_iso

HARNESS_FILENAME = "harness.json"
REVISIONS_DIRNAME = "revisions"
REVISION_INDEX_FILENAME = "revision_index.json"

COMPONENT_PROMPT = "prompt"
COMPONENT_SKILLS = "skills"
COMPONENT_MEMORY = "memory"
COMPONENT_SUBAGENTS = "subagents"
COMPONENTS = (COMPONENT_PROMPT, COMPONENT_SKILLS, COMPONENT_MEMORY, COMPONENT_SUBAGENTS)

BASE_SYSTEM_PROMPT = (
    "You are the main orchestrator of a patient Digital Twin pipeline. "
    "Plan the work, delegate each data category to its persistent sub-agent, "
    "validate every result against its JSON schema before integration, "
    "never invent a patient value, use null when a value is unavailable, "
    "and preserve source-file traceability for everything you emit."
)

BASE_POLICIES = (
    "Follow Plan -> Delegate -> Execute -> Validate -> Fix/Retry -> Verify -> Integrate.",
    "Never fabricate patient values; unavailable scalars are null and unavailable lists are [].",
    "Every extracted field keeps the source file it came from.",
    "A category result may only be integrated after it validates against its schema.",
    "When an issue repeats, refine the narrowest artifact: memory record, skill file, or agent instruction.",
)


@dataclass(slots=True)
class SubAgentConfig:
    """Configuration for one persistent category sub-agent."""

    category: str
    agent_id: str
    label: str
    skill_path: str
    schema_name: str
    instruction: str
    enabled: bool = True
    max_retries: int = 2
    memory_scope: str = ""
    extractor: str = ""

    def __post_init__(self) -> None:
        if not self.memory_scope:
            self.memory_scope = f"agent-{self.category}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubAgentConfig":
        return cls(
            category=data["category"],
            agent_id=data.get("agent_id") or f"agent-{data['category']}",
            label=data.get("label") or data["category"],
            skill_path=data.get("skill_path", ""),
            schema_name=data.get("schema_name") or data["category"],
            instruction=data.get("instruction", ""),
            enabled=bool(data.get("enabled", True)),
            max_retries=int(data.get("max_retries", 2)),
            memory_scope=data.get("memory_scope") or f"agent-{data['category']}",
            extractor=data.get("extractor", ""),
        )


@dataclass(slots=True)
class HarnessComponent:
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HarnessComponent":
        return cls(
            name=data["name"],
            data=data.get("data") or {},
            version=int(data.get("version", 1)),
            updated_at=data.get("updated_at") or utc_now_iso(),
        )


def default_instruction(category: str, skill_path: str, schema_name: str) -> str:
    label = CATEGORY_LABELS.get(category, category)
    return (
        f"You are the persistent {label} sub-agent. "
        f"Read {skill_path} before every run and follow it exactly. "
        f"Extract only values explicitly present in the assigned source files. "
        f"Emit an object that validates against schemas/{schema_name}.schema.json, "
        f"using null for unavailable scalars and [] for unavailable lists, "
        f"and attach the source file to every field you populate."
    )


class ContinualHarness:
    """Persisted, versioned harness state with CRUD and rollback."""

    def __init__(self, root: Path, paths: ProjectPaths | None = None):
        self.root = ensure_dir(Path(root))
        self.paths = paths
        self.state_path = self.root / HARNESS_FILENAME
        self.revisions_dir = ensure_dir(self.root / REVISIONS_DIRNAME)
        self.revision_index_path = self.root / REVISION_INDEX_FILENAME
        self._lock = threading.RLock()
        self.components: dict[str, HarnessComponent] = {}
        self.created_at = utc_now_iso()
        self.updated_at = self.created_at
        self.load()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def load(self) -> "ContinualHarness":
        payload = read_json(self.state_path, None)
        if not isinstance(payload, dict) or "components" not in payload:
            self.components = self._build_defaults()
            self.created_at = utc_now_iso()
            self.updated_at = self.created_at
            self.save()
            return self
        self.components = {
            name: HarnessComponent.from_dict(component)
            for name, component in payload.get("components", {}).items()
        }
        for name in COMPONENTS:
            if name not in self.components:
                self.components[name] = self._build_defaults()[name]
        self.created_at = payload.get("created_at") or utc_now_iso()
        self.updated_at = payload.get("updated_at") or self.created_at
        return self

    def save(self) -> Path:
        with self._lock:
            self.updated_at = utc_now_iso()
            atomic_write_json(self.state_path, self.to_dict())
        return self.state_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "components": {name: component.to_dict() for name, component in self.components.items()},
        }

    def _build_defaults(self) -> dict[str, HarnessComponent]:
        paths = self.paths or ProjectPaths.discover()
        subagents: dict[str, Any] = {}
        skills: dict[str, Any] = {
            "root": {"path": str(paths.skills_root / "SKILL.md"), "refinements": []},
        }
        for category in CATEGORY_ORDER:
            skill_path = str(paths.category_skill_path(category))
            skills[category] = {"path": skill_path, "refinements": []}
            config = SubAgentConfig(
                category=category,
                agent_id=f"agent-{category}",
                label=CATEGORY_LABELS.get(category, category),
                skill_path=skill_path,
                schema_name=category,
                instruction=default_instruction(category, skill_path, category),
                extractor=f"patient_prime_agent.extractors.{category}",
            )
            subagents[category] = config.to_dict()

        return {
            COMPONENT_PROMPT: HarnessComponent(
                name=COMPONENT_PROMPT,
                data={
                    "system": BASE_SYSTEM_PROMPT,
                    "objective": DEFAULT_OBJECTIVE,
                    "policies": list(BASE_POLICIES),
                    "workflow": [
                        "Plan",
                        "Delegate",
                        "Execute",
                        "Validate",
                        "Fix/Retry",
                        "Verify",
                        "Integrate",
                    ],
                },
            ),
            COMPONENT_SKILLS: HarnessComponent(name=COMPONENT_SKILLS, data=skills),
            COMPONENT_MEMORY: HarnessComponent(
                name=COMPONENT_MEMORY,
                data={
                    "scopes": ["global"] + [f"agent-{category}" for category in CATEGORY_ORDER],
                    "refinement_threshold": 2,
                    "retain_sessions": 50,
                    "record_kinds": ["semantic", "episodic", "procedural"],
                },
            ),
            COMPONENT_SUBAGENTS: HarnessComponent(name=COMPONENT_SUBAGENTS, data=subagents),
        }

    # ------------------------------------------------------------------
    # component CRUD
    # ------------------------------------------------------------------
    def create_component(self, name: str, data: dict[str, Any]) -> HarnessComponent:
        with self._lock:
            if name in self.components:
                raise KeyError(f"Harness component already exists: {name}")
            component = HarnessComponent(name=name, data=dict(data))
            self.components[name] = component
            self.save()
            return component

    def get_component(self, name: str) -> HarnessComponent:
        component = self.components.get(name)
        if component is None:
            raise KeyError(f"Unknown harness component: {name}")
        return component

    def read_component(self, name: str) -> dict[str, Any]:
        return dict(self.get_component(name).data)

    def update_component(self, name: str, data: dict[str, Any], merge: bool = True) -> HarnessComponent:
        with self._lock:
            component = self.get_component(name)
            component.data = {**component.data, **data} if merge else dict(data)
            component.version += 1
            component.updated_at = utc_now_iso()
            self.save()
            return component

    def delete_component(self, name: str) -> bool:
        with self._lock:
            if name not in self.components:
                return False
            del self.components[name]
            self.save()
            return True

    def list_components(self) -> list[str]:
        return sorted(self.components)

    # ------------------------------------------------------------------
    # prompt
    # ------------------------------------------------------------------
    @property
    def system_prompt(self) -> str:
        return str(self.read_component(COMPONENT_PROMPT).get("system", BASE_SYSTEM_PROMPT))

    @property
    def objective(self) -> str:
        return str(self.read_component(COMPONENT_PROMPT).get("objective", DEFAULT_OBJECTIVE))

    def policies(self) -> list[str]:
        return list(self.read_component(COMPONENT_PROMPT).get("policies", []))

    def add_policy(self, policy: str) -> HarnessComponent:
        policies = self.policies()
        if policy not in policies:
            policies.append(policy)
        return self.update_component(COMPONENT_PROMPT, {"policies": policies})

    def remove_policy(self, policy: str) -> HarnessComponent:
        policies = [item for item in self.policies() if item != policy]
        return self.update_component(COMPONENT_PROMPT, {"policies": policies})

    def render_prompt(self, extra: list[str] | None = None) -> str:
        data = self.read_component(COMPONENT_PROMPT)
        lines = [str(data.get("system", BASE_SYSTEM_PROMPT)), "", f"Objective: {data.get('objective', '')}", "", "Policies:"]
        lines.extend(f"- {policy}" for policy in data.get("policies", []))
        workflow = data.get("workflow") or []
        if workflow:
            lines.extend(["", "Workflow: " + " -> ".join(str(step) for step in workflow)])
        if extra:
            lines.extend(["", "Session notes:"])
            lines.extend(f"- {item}" for item in extra)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # skills
    # ------------------------------------------------------------------
    def list_skills(self) -> dict[str, Any]:
        return self.read_component(COMPONENT_SKILLS)

    def get_skill(self, name: str) -> dict[str, Any] | None:
        return self.list_skills().get(name)

    def upsert_skill(self, name: str, path: str, refinements: list[str] | None = None) -> HarnessComponent:
        skills = self.list_skills()
        entry = skills.get(name) or {"path": path, "refinements": []}
        entry["path"] = path
        if refinements is not None:
            entry["refinements"] = list(refinements)
        skills[name] = entry
        return self.update_component(COMPONENT_SKILLS, skills, merge=False)

    def record_skill_refinement(self, name: str, note: str) -> HarnessComponent:
        skills = self.list_skills()
        entry = skills.setdefault(name, {"path": "", "refinements": []})
        entry.setdefault("refinements", []).append({"note": note, "at": utc_now_iso()})
        return self.update_component(COMPONENT_SKILLS, skills, merge=False)

    def delete_skill(self, name: str) -> bool:
        skills = self.list_skills()
        if name not in skills:
            return False
        del skills[name]
        self.update_component(COMPONENT_SKILLS, skills, merge=False)
        return True

    # ------------------------------------------------------------------
    # memory policy
    # ------------------------------------------------------------------
    def memory_policy(self) -> dict[str, Any]:
        return self.read_component(COMPONENT_MEMORY)

    def set_memory_policy(self, **changes: Any) -> HarnessComponent:
        return self.update_component(COMPONENT_MEMORY, changes)

    # ------------------------------------------------------------------
    # sub-agents
    # ------------------------------------------------------------------
    def list_subagents(self, enabled_only: bool = False) -> list[SubAgentConfig]:
        data = self.read_component(COMPONENT_SUBAGENTS)
        configs = [SubAgentConfig.from_dict(item) for item in data.values()]
        if enabled_only:
            configs = [config for config in configs if config.enabled]
        order = {category: index for index, category in enumerate(CATEGORY_ORDER)}
        return sorted(configs, key=lambda config: order.get(config.category, len(order)))

    def get_subagent(self, category: str) -> SubAgentConfig | None:
        item = self.read_component(COMPONENT_SUBAGENTS).get(category)
        return SubAgentConfig.from_dict(item) if item else None

    def create_subagent(self, config: SubAgentConfig) -> SubAgentConfig:
        data = self.read_component(COMPONENT_SUBAGENTS)
        if config.category in data:
            raise KeyError(f"Sub-agent already registered: {config.category}")
        data[config.category] = config.to_dict()
        self.update_component(COMPONENT_SUBAGENTS, data, merge=False)
        return config

    def update_subagent(self, category: str, **changes: Any) -> SubAgentConfig | None:
        data = self.read_component(COMPONENT_SUBAGENTS)
        item = data.get(category)
        if item is None:
            return None
        item.update(changes)
        data[category] = item
        self.update_component(COMPONENT_SUBAGENTS, data, merge=False)
        return SubAgentConfig.from_dict(item)

    def delete_subagent(self, category: str) -> bool:
        data = self.read_component(COMPONENT_SUBAGENTS)
        if category not in data:
            return False
        del data[category]
        self.update_component(COMPONENT_SUBAGENTS, data, merge=False)
        return True

    # ------------------------------------------------------------------
    # revisions / rollback
    # ------------------------------------------------------------------
    def commit(self, reason: str, actor: str = "system") -> dict[str, Any]:
        """Snapshot the current harness state as an immutable revision."""

        with self._lock:
            index = read_json(self.revision_index_path, {"revisions": []})
            revisions = index.get("revisions", [])
            number = len(revisions) + 1
            revision_id = f"rev-{number:04d}"
            snapshot = {
                "revision_id": revision_id,
                "number": number,
                "reason": reason,
                "actor": actor,
                "created_at": utc_now_iso(),
                "state": self.to_dict(),
            }
            atomic_write_json(self.revisions_dir / f"{revision_id}.json", snapshot)
            revisions.append(
                {
                    "revision_id": revision_id,
                    "number": number,
                    "reason": reason,
                    "actor": actor,
                    "created_at": snapshot["created_at"],
                }
            )
            atomic_write_json(self.revision_index_path, {"revisions": revisions})
            return snapshot

    def list_revisions(self) -> list[dict[str, Any]]:
        return list(read_json(self.revision_index_path, {"revisions": []}).get("revisions", []))

    def read_revision(self, revision_id: str) -> dict[str, Any] | None:
        payload = read_json(self.revisions_dir / f"{revision_id}.json", None)
        return payload if isinstance(payload, dict) else None

    def rollback(self, revision_id: str, commit_current: bool = True) -> bool:
        """Restore the harness to a previous revision."""

        snapshot = self.read_revision(revision_id)
        if snapshot is None:
            return False
        with self._lock:
            if commit_current:
                self.commit(reason=f"pre-rollback snapshot before {revision_id}", actor="rollback")
            state = snapshot.get("state") or {}
            self.components = {
                name: HarnessComponent.from_dict(component)
                for name, component in state.get("components", {}).items()
            }
            self.created_at = state.get("created_at") or self.created_at
            self.save()
        return True

    # ------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        return {
            "state_path": str(self.state_path),
            "components": {name: component.version for name, component in self.components.items()},
            "subagents": [config.category for config in self.list_subagents()],
            "enabled_subagents": [config.category for config in self.list_subagents(enabled_only=True)],
            "revisions": len(self.list_revisions()),
            "updated_at": self.updated_at,
        }
