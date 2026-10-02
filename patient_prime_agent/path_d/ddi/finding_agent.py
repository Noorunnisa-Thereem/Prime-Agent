"""Evidence/Finding Agent for the pair-level DDI pipeline -- LLM-backed.

``ddi_flag_evidence.py`` already decides, deterministically, which source
resolves a pair and what that source's own evidence text is (see that
module's ``_pair_finding``). This module's only job is the step after that:
given an already-formed pair and that already-retrieved evidence text,
determine what it actually says about this specific pair and compose the
Finding text SKILL.md Part 6 requires.

The rules live in SKILL.md, not here
------------------------------------
SKILL.md's Part 6 section is read live from disk (re-read whenever the file
changes) and given to the model verbatim as its system prompt. This module
adds only pipeline plumbing around it -- the pair, the source label, and the
resource text as the user message -- and never restates a Part 6 rule in
Python. The one thing Python must know is the literal ``NO_FINDING`` token,
because it has to detect it; SKILL.md itself defines that token.

Backends (``PRIME_AGENT_LLM_PROVIDER``)
---------------------------------------
- ``openai``: :class:`~patient_prime_agent.agentic.llm.OpenAIBackend`, a
  hosted model (``PRIME_AGENT_OPENAI_MODEL``, default ``gpt-5.5`` at
  ``PRIME_AGENT_OPENAI_REASONING_EFFORT=low``), keyed by ``OPENAI_API_KEY``
  from the environment or the project's gitignored ``.env``. The key is
  never logged or echoed. It answers through a forced ``submit_finding``
  tool call (``status`` = ``finding``/``no_finding`` enum), so "no finding"
  is a structured value rather than free text. It is deliberately given no
  search tool -- SKILL.md Step 6 restricts a Finding to the resource text the
  pipeline already retrieved.
- ``gguf`` (default when unset): a local GGUF-quantized
  ``Qwen2.5-0.5B-Instruct`` run on CPU via ``gpt4all`` -- the only local
  model confirmed to load on this project's reference CPU-only, 7.69 GB-RAM
  hardware (~0.48 GB peak RSS). Raw ``transformers`` float32 loading does
  not fit there (``Qwen2.5-3B-Instruct`` segfaulted from OOM).

Both implement the same ``generate(prompt, system=None, **kwargs) -> str``
interface ``agentic.llm.TransformersBackend`` does, so nothing downstream of
``generate_finding`` depends on which one is active.

Enablement
----------
``PRIME_AGENT_DDI_FINDING_AGENT`` (``1``/``true``/``yes``/``on``) turns the
agent on; unset means fully disabled, and disabled returns the caller's own
deterministic text unchanged -- the pipeline's default run stays
byte-identical. Independent of ``PRIME_AGENT_ENABLE_LLM`` (Path A's switch).

``reason`` strings
------------------
``_pair_finding`` branches on exactly one: ``"llm_reported_insufficient_
evidence"`` (the model answered ``NO_FINDING``) makes the candidate fall
through to the next one in priority order. Every other reason -- disabled,
empty input, backend unavailable, API/model error, or a guardrail rejection
-- publishes the candidate's original deterministic text unchanged, so a
model problem can never silently drop a pair's evidence.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ...agentic.settings import parse_env_file
from ...external_lookup.guardrails import sanitize_response_field

_ENV_FLAG = "PRIME_AGENT_DDI_FINDING_AGENT"
_PROVIDER_ENV = "PRIME_AGENT_LLM_PROVIDER"
_OPENAI_MODEL_ENV = "PRIME_AGENT_OPENAI_MODEL"
_OPENAI_KEY_ENV = "OPENAI_API_KEY"
_TRUE_TOKENS = {"1", "true", "yes", "on"}

_OPENAI_EFFORT_ENV = "PRIME_AGENT_OPENAI_REASONING_EFFORT"

_DEFAULT_PROVIDER = "gguf"
_DEFAULT_OPENAI_MODEL = "gpt-5.5"
_DEFAULT_OPENAI_EFFORT = "low"

# Structured answer channel for tool-capable backends: the model must pick a status from a
# fixed enum instead of typing a sentinel, so "no finding" can never come back malformed.
FINDING_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "submit_finding",
    "description": (
        "Submit this resource's Finding and its Step 5 Impact for the drug pair, or status=no_finding "
        "where the specification calls for NO_FINDING (finding must then be an empty string and impact 'none')."
    ),
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "finding", "impact"],
        "properties": {
            "status": {"type": "string", "enum": ["finding", "no_finding"]},
            "finding": {"type": "string"},
            "impact": {"type": "string", "enum": ["positive", "negative", "none"]},
        },
    },
}

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE_PATH = _PROJECT_ROOT / ".env"
SKILL_PATH = _PROJECT_ROOT / "skills" / "patient_prime_agent" / "ddi-therapeutic-response" / "SKILL.md"
_PART6_HEADING = "## Part 6"

MAX_FINDING_LENGTH = 800  # room for SKILL.md Step 4's 2-3 sentence Finding
_CORRECTION_ATTEMPTS = 1
_CORRECTABLE_REASONS = frozenset(
    {"output_dropped_hedge", "output_too_long", "output_contains_truncation_artifact", "output_malformed"}
)
_MAX_LLM_INPUT_CHARS = 6000  # a full abstract or a label's multi-mention context fits
_MAX_NEW_TOKENS = 150

NO_FINDING_TOKEN = "NO_FINDING"
_TRUNCATION_ARTIFACT_MARKERS: tuple[str, ...] = ("...", "…")

# Post-hoc hedge check only (never used to generate text).
_HEDGE_MARKERS: tuple[str, ...] = (
    "may", "might", "could", "can", "possible", "possibly", "potential", "potentially", "likely", "apparently", "consider", "considering",
)
_HEDGE_PHRASES: tuple[str, ...] = (
    "is associated with", "was associated with", "are associated with", "were associated with",
    "appears to", "appear to", "is reported to", "are reported to", "has been reported to",
    "have been reported to", "is thought to", "are thought to",
)

_GGUF_REPO_ID = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
_GGUF_FILENAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
_GGUF_CACHE_DIR = Path("reports") / "external_lookup" / ".gguf_models"


@dataclass(slots=True)
class FindingAgentOutcome:
    """``text`` is always usable: the model's Finding (``used_llm=True``) or
    the caller's fallback text unchanged (``used_llm=False``, with
    ``reason`` naming why). ``impact`` ("positive"/"negative") is the
    agent's own SKILL.md Step 5 verdict, decided from the same evidence as
    the Finding; ``None`` when no Finding was produced or the backend can't
    return one (the caller then keeps its keyword classification)."""

    text: str
    used_llm: bool
    reason: str | None = None
    impact: str | None = None


def _env_value(name: str) -> str | None:
    """Process environment first, then the project's gitignored ``.env`` --
    same lookup order ``external_lookup.search_tools`` uses for its key."""
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    value = parse_env_file(_ENV_FILE_PATH).get(name)
    return value.strip() if value and value.strip() else None


def _ddi_finding_agent_enabled() -> bool:
    return os.environ.get(_ENV_FLAG, "").strip().lower() in _TRUE_TOKENS


def agent_enabled() -> bool:
    """Whether ``PRIME_AGENT_DDI_FINDING_AGENT`` turns the agent on -- lets the
    pipeline skip building rich evidence contexts nobody will read."""
    return _ddi_finding_agent_enabled()


def _provider() -> str:
    return (_env_value(_PROVIDER_ENV) or _DEFAULT_PROVIDER).lower()


@lru_cache(maxsize=4)
def _part6_text_cached(path: str, mtime_ns: int) -> str:
    text = Path(path).read_text(encoding="utf-8")
    start = text.find(_PART6_HEADING)
    if start == -1:
        raise ValueError("SKILL.md has no Part 6 section")
    end = text.find("\n## ", start + len(_PART6_HEADING))
    return text[start:end if end != -1 else None].strip()


def skill_part6_text(path: Path = SKILL_PATH) -> str:
    """SKILL.md's Part 6, keyed on the file's mtime so an edit is picked up
    on the next call without re-reading the file for every pair."""
    return _part6_text_cached(str(path), path.stat().st_mtime_ns)


def build_system_prompt(part6: str, structured: bool = False) -> str:
    if structured:
        answer = (
            "Answer only through the submit_finding tool: status=finding with the Finding text in "
            "Step 4's single-Finding form (not Step 4a's two-line form) and its Step 5 impact "
            "(positive or negative), or status=no_finding wherever the specification calls for "
            f"{NO_FINDING_TOKEN}."
        )
    else:
        answer = (
            "Return only the Finding text in Step 4's single-Finding form (not Step 4a's two-line form), "
            f"or exactly {NO_FINDING_TOKEN} as the specification directs."
        )
    return f"You are the Evidence/Finding Agent specified below. Follow that specification exactly. {answer}\n\n{part6}"


def _parse_tool_answer(raw: str) -> tuple[str, str | None] | None:
    """Map a submit_finding call to (text, impact): the Finding text and its
    impact, or the literal NO_FINDING token; ``None`` when unparseable."""
    try:
        args = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(args, dict):
        return None
    if args.get("status") == "no_finding":
        return NO_FINDING_TOKEN, None
    if args.get("status") == "finding" and isinstance(args.get("finding"), str):
        impact = args.get("impact")
        return args["finding"], impact if isinstance(impact, str) else None
    return None


def build_user_prompt(drug_a: str, drug_b: str, source_label: str, resource_text: str) -> str:
    return (
        f"Drug pair: {drug_a} + {drug_b}\n"
        f"Source: {source_label}\n"
        f'Retrieved evidence from this source:\n"""\n{resource_text}\n"""'
    )


def _contains_truncation_artifact(text: str) -> bool:
    return any(marker in text for marker in _TRUNCATION_ARTIFACT_MARKERS)


def _is_structurally_complete(text: str) -> bool:
    return text.count("(") == text.count(")") and text.rstrip()[-1:] in {".", "!", "?"}


def _hedge_terms_in(text: str) -> set[str]:
    lowered = text.lower()
    found = {phrase for phrase in _HEDGE_PHRASES if phrase in lowered}
    found |= {marker for marker in _HEDGE_MARKERS if re.search(r"\b" + re.escape(marker) + r"\b", lowered)}
    return found


_COMBINATION_WORDS = (
    "combination", "combined", "coadminist", "co-administ", "concomitant", "together", "added to", "add-on", "interaction",
)


def _pair_relevant_text(resource_text: str, drug_a: str, drug_b: str) -> str:
    """Only the source sentences about this pair: those naming either drug or
    the combination itself. A label or abstract is full of hedged sentences
    about other things (real case: a perampanel label's "may decrease the
    effectiveness of hormonal contraceptives" next to its flat, unhedged
    statement about CYP3A4 inducers) -- checking the whole text demanded a hedge
    the pair's own statement never had. Falls back to the whole text when no
    sentence qualifies."""
    names = [n.strip().lower() for n in (drug_a, drug_b) if n and n.strip()]
    sentences = re.split(r"(?<=[.!?])\s+", resource_text)
    relevant = [
        s for s in sentences
        if any(re.search(r"\b" + re.escape(n) + r"\b", s.lower()) for n in names)
        or any(w in s.lower() for w in _COMBINATION_WORDS)
    ]
    return " ".join(relevant) if relevant else resource_text


def _hedge_preserved(resource_text: str, output_text: str, drug_a: str = "", drug_b: str = "") -> bool:
    """A source whose statements about this pair are hedged must yield output
    that still carries a hedge (SKILL.md Part 6 Step 6, checked after
    generation)."""
    source_hedges = _hedge_terms_in(_pair_relevant_text(resource_text, drug_a, drug_b))
    if not source_hedges:
        return True
    output_hedges = _hedge_terms_in(output_text)
    if source_hedges & output_hedges:
        return True
    return any(h in o or o in h for h in source_hedges for o in output_hedges)


class GGUFBackend:
    """Local CPU backend (``gpt4all`` + a GGUF file), same interface as
    ``agentic.llm.TransformersBackend``."""

    name = "gguf"

    def __init__(self) -> None:
        self.last_usage: dict[str, int] | None = None
        self.last_error: str | None = None

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        model = _load_gguf_model()
        with model.chat_session(system_prompt=system):
            return str(model.generate(prompt, max_tokens=kwargs.get("max_tokens", _MAX_NEW_TOKENS), temp=0.1)).strip()

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "model": _GGUF_FILENAME}


@lru_cache(maxsize=1)
def _load_gguf_model() -> Any:
    from gpt4all import GPT4All
    from huggingface_hub import hf_hub_download

    _GGUF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    hf_hub_download(_GGUF_REPO_ID, _GGUF_FILENAME, local_dir=str(_GGUF_CACHE_DIR))
    return GPT4All(model_name=_GGUF_FILENAME, model_path=str(_GGUF_CACHE_DIR), allow_download=False, device="cpu", n_ctx=2048)


def build_backend() -> Any:
    """The backend ``PRIME_AGENT_LLM_PROVIDER`` selects. Raises (caught by
    the caller) when it can't be built -- unknown provider, missing key."""
    provider = _provider()
    if provider == "openai":
        api_key = _env_value(_OPENAI_KEY_ENV)
        if not api_key:
            raise RuntimeError(f"{_OPENAI_KEY_ENV} is not set")
        from ...agentic.llm import OpenAIBackend

        return OpenAIBackend(
            api_key,
            model=_env_value(_OPENAI_MODEL_ENV) or _DEFAULT_OPENAI_MODEL,
            reasoning_effort=_env_value(_OPENAI_EFFORT_ENV) or _DEFAULT_OPENAI_EFFORT,
        )
    if provider == "gguf":
        return GGUFBackend()
    raise ValueError(f"unknown {_PROVIDER_ENV}: {provider!r}")


class FindingAgent:
    """Reads one already-retrieved resource for one already-formed pair and
    returns the Finding text for it, per SKILL.md Part 6.

    Flow: disabled/empty -> fallback unchanged; otherwise send Part 6 (live)
    + the pair/resource to the backend; ``NO_FINDING`` -> fall-through
    reason; any other output is validated (non-empty, length cap, no
    truncation artifact, balanced parens + terminal punctuation, hedge
    preserved, no stray ``NO_FINDING``) before it is accepted.
    """

    def __init__(self, max_output_length: int = MAX_FINDING_LENGTH, backend: Any | None = None) -> None:
        self.max_output_length = max_output_length
        self.backend = backend
        self.last_error_type: str | None = None

    def _check(self, raw_output: Any, structured: bool, resource_text: str, drug_a: str, drug_b: str) -> tuple[str | None, str, str | None]:
        """(reason, finding, impact): reason is None when the answer is accepted,
        ``NO_FINDING_TOKEN`` for an explicit insufficient-evidence answer, or the
        name of the guardrail that rejected it."""
        impact: str | None = None
        if structured:
            parsed = _parse_tool_answer(raw_output)
            if parsed is None:
                return "output_malformed", "", None
            raw_output, impact = parsed
        cleaned = (raw_output or "").strip()
        if cleaned == NO_FINDING_TOKEN:
            return NO_FINDING_TOKEN, "", None
        if NO_FINDING_TOKEN in cleaned:
            # Token present but not as the whole answer -- ambiguous; never publish it.
            return "output_malformed_no_finding", cleaned, None
        if structured and impact not in ("positive", "negative"):
            return "output_malformed", cleaned, None

        finding = sanitize_response_field(cleaned, max_length=self.max_output_length + 1)
        if not finding:
            return "output_empty", "", None
        if len(finding) > self.max_output_length:
            return "output_too_long", finding, impact
        if _contains_truncation_artifact(finding):
            return "output_contains_truncation_artifact", finding, impact
        if not _is_structurally_complete(finding):
            return "output_malformed", finding, impact
        if not _hedge_preserved(resource_text, finding, drug_a, drug_b):
            return "output_dropped_hedge", finding, impact
        return None, finding, impact

    def _correction_note(self, reason: str, previous: str, resource_text: str, drug_a: str, drug_b: str) -> str:
        if reason == "output_dropped_hedge":
            relevant = _pair_relevant_text(resource_text, drug_a, drug_b)
            hedged = [s for s in re.split(r"(?<=[.!?])\s+", relevant) if _hedge_terms_in(s)][:3]
            problem = (
                "It states the result with more certainty than the source. These source sentences about this pair are "
                "hedged:\n" + "\n".join(f"- {s}" for s in hedged) + "\nIf they describe this pair's result, keep their "
                "hedging word-for-word in degree (Step 6); state only what they support."
            )
        elif reason == "output_too_long":
            problem = f"It is {len(previous)} characters; the limit is {self.max_output_length}. Keep the same facts in 2-3 shorter sentences."
        else:
            problem = "It is not complete, well-formed sentences (balanced parentheses, a final full stop, no '...')."
        return (
            f"\n\nYour previous answer was rejected:\n\"\"\"\n{previous}\n\"\"\"\n{problem}\n"
            "Answer again, following the specification."
        )

    def generate_finding(
        self,
        drug_a: str,
        drug_b: str,
        source_label: str,
        resource_text: str | None,
        fallback_text: str,
    ) -> FindingAgentOutcome:
        if not isinstance(resource_text, str) or not resource_text.strip():
            return FindingAgentOutcome(fallback_text, False, "empty_resource_text")
        if not _ddi_finding_agent_enabled():
            return FindingAgentOutcome(fallback_text, False, "pattern_agent_disabled")

        try:
            if self.backend is None:
                self.backend = build_backend()
            structured = bool(getattr(self.backend, "supports_tools", False))
            system_prompt = build_system_prompt(skill_part6_text(), structured=structured)
        except Exception as exc:  # noqa: BLE001 - never crash the DDI pipeline
            self.last_error_type = type(exc).__name__
            return FindingAgentOutcome(fallback_text, False, "skill_unavailable")

        base_prompt = build_user_prompt(drug_a, drug_b, source_label, resource_text[:_MAX_LLM_INPUT_CHARS])
        prompt = base_prompt
        reason: str | None = None
        for attempt in range(1 + _CORRECTION_ATTEMPTS):
            try:
                if structured:
                    raw_output = self.backend.generate(prompt, system=system_prompt, tool=FINDING_TOOL)
                else:
                    raw_output = self.backend.generate(prompt, system=system_prompt)
            except Exception as exc:  # noqa: BLE001 - API/model failure: keep the original text
                # Type name only -- SDK error messages can echo part of an API key.
                self.last_error_type = type(exc).__name__
                return FindingAgentOutcome(fallback_text, False, "llm_error")

            reason, finding, impact = self._check(raw_output, structured, resource_text, drug_a, drug_b)
            if reason is None:
                return FindingAgentOutcome(finding, True, None, impact)
            if reason == NO_FINDING_TOKEN:
                return FindingAgentOutcome(fallback_text, False, "llm_reported_insufficient_evidence")
            if reason not in _CORRECTABLE_REASONS or attempt == _CORRECTION_ATTEMPTS:
                break
            # One guided retry: a rejected Finding otherwise falls back to the raw snippet,
            # which is usually worse than a corrected Finding (and carries only a keyword Impact).
            prompt = base_prompt + self._correction_note(reason, finding, resource_text, drug_a, drug_b)
        return FindingAgentOutcome(fallback_text, False, reason)
