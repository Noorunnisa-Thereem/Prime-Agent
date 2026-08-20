from __future__ import annotations

import argparse
from pathlib import Path

from .config import ProjectPaths
from .orchestrator import PrimeAgentHarness


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prime Agent-style patient data harness")
    parser.add_argument("--data-root", type=Path, default=None, help="Root folder containing patient category folders")
    parser.add_argument("--reports-root", type=Path, default=None, help="Folder for the consolidated report")
    parser.add_argument("--memory-root", type=Path, default=None, help="Folder for persistent memory")
    parser.add_argument("--skills-root", type=Path, default=None, help="Folder for SKILL.md instructions")
    parser.add_argument("--schemas-root", type=Path, default=None, help="Folder for JSON Schemas")
    parser.add_argument(
        "--agentic",
        action="store_true",
        help="Run the Prime Agent runtime (persistent agents, A2A, continual harness) instead of the legacy harness",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = ProjectPaths.discover()
    if args.data_root is not None:
        paths = ProjectPaths(
            root=paths.root,
            data_root=args.data_root,
            reports_root=args.reports_root or paths.reports_root,
            memory_root=args.memory_root or paths.memory_root,
            skills_root=args.skills_root or paths.skills_root,
            schemas_root=args.schemas_root or paths.schemas_root,
        )
    else:
        paths = ProjectPaths(
            root=paths.root,
            data_root=paths.data_root,
            reports_root=args.reports_root or paths.reports_root,
            memory_root=args.memory_root or paths.memory_root,
            skills_root=args.skills_root or paths.skills_root,
            schemas_root=args.schemas_root or paths.schemas_root,
        )

    if args.agentic:
        from .agentic.main_agent import run_agentic_pipeline

        agentic_outcome = run_agentic_pipeline(paths=paths)
        print(f"Session {agentic_outcome.session.session_id}")
        print(f"Wrote validated report to {agentic_outcome.report_path}")
        print(f"Wrote agent run manifest to {agentic_outcome.manifest_path}")
        return 0 if agentic_outcome.verified else 2

    harness = PrimeAgentHarness(paths)
    outcome = harness.run()
    print(f"Wrote validated report to {outcome.report_path}")
    return 0
