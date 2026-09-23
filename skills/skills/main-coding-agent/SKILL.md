---
name: main-coding-agent
description: Operate and maintain the AI Coding Agent codebase, including CLI workflow, tool usage, debugging, code editing, testing, validation, rollback, and maintenance rules.
---

# Main Coding Agent

## Actual Main Python File

`src\ai_coding_agent\cli.py` is the implemented CLI entry point. The installed command is `ai-coding-agent` from `pyproject.toml`.

## Workflow

Work from `C:\AI coding agent`. Inspect relevant code before editing. Prefer existing modules and helper APIs. Keep changes scoped to the requested module or workflow. Do not create a new coding-agent implementation while `src\ai_coding_agent\cli.py` exists.

## Coding Agent Loop

1. Inspect: read the relevant source modules, tests, skill contracts, schemas, and current generated artifacts before making changes.
2. Plan: identify the module, expected input files, expected schema, likely failure points, and focused tests.
3. Act: make controlled code or skill edits with small diffs.
4. Test: run focused tests for touched modules and run the CLI path that exercises the requested workflow when feasible.
5. Feedback: inspect failures from the first project stack frame, compare actual JSON shape to the loaded `SKILL.md` schema, and identify whether the bug is extraction, consolidation, validation, path detection, or output writing.
6. Fix: patch the smallest relevant code path and rerun the failing test or command.
7. Validate: confirm generated JSON parses, schema keys match, all detected files are processed or explicitly skipped/failed, and no patient values were copied from reference/example JSON.
8. Rollback: if a change makes the workflow worse and cannot be fixed in the current pass, revert only the files changed by the current agent edit using a targeted patch. Never use destructive git reset/checkout without explicit user approval.

## Tool Usage

Use `rg` for searches, PowerShell for local commands, and focused pytest targets after code changes. Use the installed command `.\.venv\Scripts\ai-coding-agent.exe` or fallback `.\.venv\Scripts\python.exe -m ai_coding_agent.cli`.

## Error Detection And Debugging

Read stack traces from the first project frame. Check path quoting when folders contain spaces. Verify imports after package changes. Confirm generated JSON with schema validation before trusting report output.

## Code Editing Rules

Do not delete or rewrite working code unnecessarily. Preserve user changes in the git worktree. Keep report values sourced from actual patient files, extraction output, or deterministic calculations. Do not copy mentor/reference values.

## Testing And Validation

Run focused tests for changed modules, then run `.\.venv\Scripts\python.exe -m pytest tests --basetemp .pytest_tmp\<name>` when behavior or shared loading changes. Verify `ai-coding-agent --help` after CLI or packaging changes.

For folder automation, also run `process-folder` against a representative source root when feasible and inspect the final report plus module output reports. Confirm each module was selected by input files, loaded its module `SKILL.md`, generated values from actual patient files/model output, validated the payload, and wrote one consolidated JSON report.

## Rollback And Maintenance

Do not use destructive git commands unless explicitly requested. For risky changes, keep edits small and separable. Keep `skills\*\SKILL.md` files modular: agent workflow belongs here, category schemas and extraction rules belong in category skills.

## No-Copy Rule

Reference JSON files under `Clinical_summary_for each_data` may be used only for key names, schema shape, and ordering. Never copy their patient IDs, dates, observations, findings, impressions, recommendations, or conclusions into generated reports. Any generated value must come from detected patient files, deterministic calculations, OCR/VLM/model output grounded in those files, or validated module outputs from the same run.
