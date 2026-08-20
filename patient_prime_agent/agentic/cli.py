"""Command line interface for the agentic layer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..config import ProjectPaths
from ..memory_store import MemoryStore
from ..skill_store import SkillRegistry
from .harness import ContinualHarness
from .main_agent import MainOrchestratorAgent, run_agentic_pipeline
from .memory import AgentMemory
from .model_loader import HardwareProfile, resolve_model_plan
from .refine import RefinementEngine
from .session import SessionStore
from .settings import load_settings, write_example_env


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prime-agent", description="Prime Agent runtime for the patient digital twin")
    parser.add_argument("--project-root", type=Path, default=None, help="Project root (defaults to the package parent)")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--reports-root", type=Path, default=None)
    parser.add_argument("--memory-root", type=Path, default=None)
    parser.add_argument("--skills-root", type=Path, default=None)
    parser.add_argument("--schemas-root", type=Path, default=None)

    sub = parser.add_subparsers(dest="command")

    run_cmd = sub.add_parser("run", help="Run Plan->Delegate->Execute->Validate->Fix/Retry->Verify->Integrate")
    run_cmd.add_argument("--session-id", default=None, help="Resume an existing session id")
    run_cmd.add_argument("--enable-llm", action="store_true", help="Load the Hugging Face model for advisory output")
    run_cmd.add_argument("--model-id", default=None, help="Override the Hugging Face model id for this run")

    sub.add_parser("status", help="Show runtime, harness and model status")

    model_cmd = sub.add_parser("model", help="Show detected hardware and the resolved model plan")
    model_cmd.add_argument("--load", action="store_true", help="Actually load the model (downloads weights)")

    harness_cmd = sub.add_parser("harness", help="Inspect or edit the continual harness")
    harness_cmd.add_argument(
        "action",
        choices=["show", "prompt", "skills", "memory", "subagents", "revisions", "commit", "rollback"],
    )
    harness_cmd.add_argument("--revision-id", default=None)
    harness_cmd.add_argument("--reason", default="manual commit")
    harness_cmd.add_argument("--category", default=None)
    harness_cmd.add_argument("--set-enabled", choices=["true", "false"], default=None)
    harness_cmd.add_argument("--set-instruction", default=None)

    sessions_cmd = sub.add_parser("sessions", help="List or inspect persisted sessions")
    sessions_cmd.add_argument("--session-id", default=None)

    refine_cmd = sub.add_parser("refinements", help="List refinement history or roll one back")
    refine_cmd.add_argument("--rollback", dest="rollback_id", default=None, help="Refinement id to roll back")
    refine_cmd.add_argument("--category", default=None)

    memory_cmd = sub.add_parser("memory", help="Inspect persisted agent memory")
    memory_cmd.add_argument("--scope", default=None)

    init_cmd = sub.add_parser("init-env", help="Write a documented .env.example")
    init_cmd.add_argument("--path", type=Path, default=None)

    return parser


def latest_session_id(paths: ProjectPaths) -> str | None:
    """Reuse the newest session so read-only commands do not spawn empty ones."""

    sessions = SessionStore(paths.memory_root / "agentic" / "sessions").list_sessions()
    return sessions[-1]["session_id"] if sessions else None


def build_refinement_engine(paths: ProjectPaths) -> RefinementEngine:
    """Build just the refinement engine, without starting a session."""

    agentic_root = paths.memory_root / "agentic"
    settings = load_settings(project_root=paths.root)
    return RefinementEngine(
        memory_store=MemoryStore(paths.memory_root),
        agent_memory=AgentMemory(agentic_root / "agent_memory"),
        skills=SkillRegistry(paths.skills_root),
        harness=ContinualHarness(agentic_root / "harness", paths=paths),
        root=agentic_root / "refinements",
        threshold=settings.refinement_threshold,
    )


def resolve_paths(args: argparse.Namespace) -> ProjectPaths:
    base = ProjectPaths.discover(args.project_root)
    return ProjectPaths(
        root=base.root,
        data_root=args.data_root or base.data_root,
        reports_root=args.reports_root or base.reports_root,
        memory_root=args.memory_root or base.memory_root,
        skills_root=args.skills_root or base.skills_root,
        schemas_root=args.schemas_root or base.schemas_root,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    paths = resolve_paths(args)

    if command == "init-env":
        target = args.path or (paths.root / ".env.example")
        _print({"written": str(write_example_env(target))})
        return 0

    if command == "model":
        settings = load_settings(project_root=paths.root)
        profile = HardwareProfile.detect()
        plan = resolve_model_plan(settings, profile)
        payload = {"hardware": profile.to_dict(), "plan": plan.to_dict(), "settings": settings.to_dict()}
        if args.load:
            from .model_loader import ModelLoader

            loader = ModelLoader(settings, profile)
            loader.load()
            payload["loaded"] = loader.describe()
        _print(payload)
        return 0

    if command == "harness":
        return _harness_command(args, paths)

    if command == "sessions":
        store = SessionStore(paths.memory_root / "agentic" / "sessions")
        if args.session_id:
            session = store.load(args.session_id)
            if session is None:
                _print({"error": f"unknown session {args.session_id}"})
                return 1
            _print(
                {
                    "session": session.to_dict(),
                    "trajectory": [step.to_dict() for step in store.trajectory(args.session_id).steps],
                }
            )
            return 0
        _print({"sessions": store.list_sessions()})
        return 0

    if command == "memory":
        memory = AgentMemory(paths.memory_root / "agentic" / "agent_memory")
        if args.scope:
            _print({"scope": args.scope, "records": [r.to_dict() for r in memory.list_records(args.scope)]})
        else:
            _print({"scopes": memory.scopes(), "snapshot": memory.snapshot()})
        return 0

    if command == "refinements":
        refiner = build_refinement_engine(paths)
        if args.rollback_id:
            ok = refiner.rollback(args.rollback_id)
            _print({"rolled_back": ok, "refinement_id": args.rollback_id})
            return 0 if ok else 1
        _print(
            {
                "stats": refiner.stats(),
                "history": [
                    record.to_dict() | {"before": "", "after": ""}
                    for record in refiner.history(category=args.category)
                ],
            }
        )
        return 0

    if command == "status":
        agent = MainOrchestratorAgent(paths=paths, session_id=latest_session_id(paths))
        _print(agent.status())
        return 0

    # default: run
    overrides: dict[str, Any] = {}
    if getattr(args, "enable_llm", False):
        overrides["enable_llm"] = True
    if getattr(args, "model_id", None):
        overrides["model_id"] = args.model_id
    settings = load_settings(project_root=paths.root, **overrides)

    outcome = run_agentic_pipeline(paths=paths, settings=settings, session_id=getattr(args, "session_id", None))
    _print(
        {
            "session_id": outcome.session.session_id,
            "report_path": str(outcome.report_path),
            "manifest_path": str(outcome.manifest_path),
            "categories": sorted(outcome.category_results),
            "verified": outcome.verified,
            "verification": outcome.verification,
            "refinements": [record.refinement_id for record in outcome.refinements],
        }
    )
    return 0 if outcome.verified else 2


def _harness_command(args: argparse.Namespace, paths: ProjectPaths) -> int:
    harness = ContinualHarness(paths.memory_root / "agentic" / "harness", paths=paths)
    action = args.action

    if action == "show":
        _print(harness.to_dict())
    elif action == "prompt":
        print(harness.render_prompt())
    elif action == "skills":
        _print(harness.list_skills())
    elif action == "memory":
        _print(harness.memory_policy())
    elif action == "subagents":
        if args.category and (args.set_enabled is not None or args.set_instruction is not None):
            changes: dict[str, Any] = {}
            if args.set_enabled is not None:
                changes["enabled"] = args.set_enabled == "true"
            if args.set_instruction is not None:
                changes["instruction"] = args.set_instruction
            updated = harness.update_subagent(args.category, **changes)
            _print({"updated": updated.to_dict() if updated else None})
        else:
            _print([config.to_dict() for config in harness.list_subagents()])
    elif action == "revisions":
        _print({"revisions": harness.list_revisions()})
    elif action == "commit":
        _print(harness.commit(reason=args.reason, actor="cli")["revision_id"])
    elif action == "rollback":
        if not args.revision_id:
            _print({"error": "--revision-id is required for rollback"})
            return 1
        ok = harness.rollback(args.revision_id)
        _print({"rolled_back": ok, "revision_id": args.revision_id})
        return 0 if ok else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
