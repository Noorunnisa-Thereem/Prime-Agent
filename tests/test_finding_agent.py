"""Tests for the LLM-backed DDI Finding Agent and its OpenAI backend.

No real API calls: every OpenAI interaction goes through a fake client, and
the project's real ``.env`` is hidden from every test so a real key can never
be read here.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from patient_prime_agent.agentic.llm import OpenAIBackend
from patient_prime_agent.path_d.ddi import finding_agent as fa
from patient_prime_agent.path_d.ddi.finding_agent import FindingAgent, FindingAgentOutcome

FAKE_KEY = "sk-test-FAKEKEY-not-real-0000"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "_ENV_FILE_PATH", tmp_path / "no.env")
    for name in ("OPENAI_API_KEY", "PRIME_AGENT_LLM_PROVIDER", "PRIME_AGENT_OPENAI_MODEL", "PRIME_AGENT_OPENAI_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRIME_AGENT_DDI_FINDING_AGENT", "1")


def finding(text, impact="negative"):
    return {"status": "finding", "finding": text, "impact": impact}


NO_FINDING = {"status": "no_finding", "finding": "", "impact": "none"}


class _FakeResponses:
    """Stands in for ``client.responses``. A dict outcome becomes the forced tool
    call's JSON arguments; a str becomes raw arguments (for malformed cases) or,
    with no tool requested, the plain ``output_text``."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, output_tokens_details=SimpleNamespace(reasoning_tokens=5))
        if "tools" in kwargs:
            args = json.dumps(outcome) if isinstance(outcome, dict) else outcome
            return SimpleNamespace(output=[SimpleNamespace(type="function_call", arguments=args)], output_text="", usage=usage)
        return SimpleNamespace(output=[], output_text=outcome, usage=usage)


def _fake_backend(*outcomes, model="gpt-5.5", max_retries=3):
    responses = _FakeResponses(outcomes)
    sleeps: list[float] = []
    backend = OpenAIBackend(FAKE_KEY, model=model, client=SimpleNamespace(responses=responses), max_retries=max_retries, sleep=sleeps.append)
    return backend, responses, sleeps


class _PlainTextBackend:
    """A non-tool backend (like the local GGUF one): plain text in, plain text out."""

    def __init__(self, output):
        self.output = output

    def generate(self, prompt, system=None, **kwargs):
        assert "tool" not in kwargs
        return self.output


# Exception doubles named like the SDK's own -- retry logic keys on the type name.
class RateLimitError(Exception):
    pass


class APITimeoutError(Exception):
    pass


class AuthenticationError(Exception):
    pass


SOURCE = "Coadministration of Drug A may increase the plasma concentration of Drug B."


def _run(agent, text=SOURCE):
    return agent.generate_finding("Drug A", "Drug B", "External evidence (PubMed)", text, "FALLBACK")


# --- passthrough paths ------------------------------------------------------


def test_disabled_is_a_pure_passthrough(monkeypatch):
    monkeypatch.delenv("PRIME_AGENT_DDI_FINDING_AGENT")
    backend, responses, _ = _fake_backend(finding("unused."))
    assert _run(FindingAgent(backend=backend)) == FindingAgentOutcome("FALLBACK", False, "pattern_agent_disabled")
    assert responses.calls == []


def test_empty_resource_text_never_calls_the_api():
    backend, responses, _ = _fake_backend(finding("unused."))
    assert _run(FindingAgent(backend=backend), "   ") == FindingAgentOutcome("FALLBACK", False, "empty_resource_text")
    assert responses.calls == []


# --- NO_FINDING detection ----------------------------------------------------


def test_structured_no_finding_status_triggers_fall_through():
    backend, _, _ = _fake_backend(NO_FINDING)
    assert _run(FindingAgent(backend=backend)) == FindingAgentOutcome("FALLBACK", False, "llm_reported_insufficient_evidence")


@pytest.mark.parametrize("text", ["NO_FINDING.", "NO_FINDING - the pair was not studied together."])
def test_no_finding_token_inside_a_finding_is_never_published_and_never_falls_through(text):
    backend, _, _ = _fake_backend(finding(text))
    assert _run(FindingAgent(backend=backend)) == FindingAgentOutcome("FALLBACK", False, "output_malformed_no_finding")


@pytest.mark.parametrize("raw_args", ["not json", '{"status": "maybe", "finding": "x"}', '["finding"]', ""])
def test_malformed_tool_arguments_are_rejected(raw_args):
    backend, _, _ = _fake_backend(raw_args, raw_args)
    assert _run(FindingAgent(backend=backend)) == FindingAgentOutcome("FALLBACK", False, "output_malformed")


def test_plain_text_backend_still_uses_the_literal_no_finding_token():
    assert _run(FindingAgent(backend=_PlainTextBackend("NO_FINDING"))).reason == "llm_reported_insufficient_evidence"
    assert _run(FindingAgent(backend=_PlainTextBackend("NO_FINDING."))).reason == "output_malformed_no_finding"
    accepted = _run(FindingAgent(backend=_PlainTextBackend("Drug A may raise Drug B levels.")))
    assert accepted == FindingAgentOutcome("Drug A may raise Drug B levels.", True, None, None)  # no impact channel


# --- accepted output and guardrails -----------------------------------------


def test_valid_hedged_output_is_accepted_with_the_agents_impact():
    text = "Coadministration of Drug A may increase Drug B plasma concentration."
    backend, _, _ = _fake_backend(finding(text, impact="negative"))
    assert _run(FindingAgent(backend=backend)) == FindingAgentOutcome(text, True, None, "negative")


def test_positive_impact_is_returned_for_an_explicit_no_adjustment_statement():
    source = "No dosage adjustment of Drug B is necessary when coadministered with Drug A."
    text = "The label states no dosage adjustment of Drug B is necessary with Drug A."
    backend, _, _ = _fake_backend(finding(text, impact="positive"))
    assert _run(FindingAgent(backend=backend), source).impact == "positive"


@pytest.mark.parametrize("impact", ["none", "maybe", ""])
def test_a_finding_without_a_positive_or_negative_impact_is_rejected(impact):
    backend, _, _ = _fake_backend(*[finding("Drug A may raise Drug B levels.", impact=impact)] * 2)
    assert _run(FindingAgent(backend=backend)).reason == "output_malformed"


def test_rejected_finding_gets_one_guided_correction_then_is_accepted():
    sharpened = finding("Drug A increases the plasma concentration of Drug B.")
    corrected = finding("Drug A may increase the plasma concentration of Drug B.")
    backend, responses, _ = _fake_backend(sharpened, corrected)
    outcome = _run(FindingAgent(backend=backend))
    assert outcome == FindingAgentOutcome("Drug A may increase the plasma concentration of Drug B.", True, None, "negative")
    assert len(responses.calls) == 2
    retry_prompt = responses.calls[1]["input"]
    assert "previous answer was rejected" in retry_prompt and "may increase" in retry_prompt


def test_correction_is_attempted_only_once():
    backend, responses, _ = _fake_backend(*[finding("Drug A increases Drug B levels.")] * 3)
    assert _run(FindingAgent(backend=backend)).reason == "output_dropped_hedge"
    assert len(responses.calls) == 2


def test_no_finding_is_never_retried():
    backend, responses, _ = _fake_backend(NO_FINDING, finding("unused."))
    assert _run(FindingAgent(backend=backend)).reason == "llm_reported_insufficient_evidence"
    assert len(responses.calls) == 1


def test_hedge_sharpened_into_certainty_is_rejected():
    backend, _, _ = _fake_backend(*[finding("Drug A increases Drug B plasma concentration.")] * 2)
    assert _run(FindingAgent(backend=backend)).reason == "output_dropped_hedge"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "output_empty"),
        ("Drug A may raise Drug B levels (e.g., in the elderly.", "output_malformed"),
        ("Drug A may raise Drug B levels and", "output_malformed"),
        ("Drug A may raise Drug B levels...", "output_contains_truncation_artifact"),
        ("Drug A may raise Drug B levels. " * 30, "output_too_long"),
    ],
)
def test_guardrails_reject_bad_output_without_falling_through(text, reason):
    backend, _, _ = _fake_backend(*[finding(text)] * 2)
    assert _run(FindingAgent(backend=backend)) == FindingAgentOutcome("FALLBACK", False, reason)


def test_hedge_elsewhere_in_the_source_does_not_force_a_hedge_on_the_pair_statement():
    source = (
        "Drug C may decrease the effectiveness of hormonal contraceptives. "
        "Strong inducers including Drug B increase clearance of Drug A and decrease Drug A concentrations."
    )
    backend, _, _ = _fake_backend(finding("Drug B increases Drug A clearance and decreases Drug A concentrations."))
    assert _run(FindingAgent(backend=backend), source).used_llm is True


@pytest.mark.parametrize(
    ("source", "output"),
    [
        ("Drug B can increase Drug A exposure.", "Drug B can increase Drug A exposure."),
        ("Drug B raises Drug A levels; a possible dose increase of Drug A is advised.", "Drug B raises Drug A levels, with a possible Drug A dose increase."),
        ("With Drug B, consider reducing the dose of Drug A.", "The label advises to consider reducing the Drug A dose with Drug B."),
    ],
)
def test_source_hedges_beyond_may_are_recognised(source, output):
    # Real cases: labels hedge with "can"/"possible"/"consider", not only "may" -- before these
    # were recognised, a faithful Finding keeping the label's own word was wrongly rejected.
    backend, _, _ = _fake_backend(finding(output))
    assert _run(FindingAgent(backend=backend), source).used_llm is True


def test_a_different_hedge_word_does_not_count_as_preserving_the_source_hedge():
    source = "Drug B may increase Drug A exposure."
    backend, _, _ = _fake_backend(*[finding("Drug B can increase Drug A exposure.")] * 2)
    assert _run(FindingAgent(backend=backend), source).reason == "output_dropped_hedge"


def test_hedged_combination_sentence_without_drug_names_still_requires_the_hedge():
    source = "Patients received Drug A and Drug B for 8 weeks. The combination may increase sedation."
    backend, _, _ = _fake_backend(*[finding("The combination of Drug A and Drug B increases sedation.")] * 2)
    assert _run(FindingAgent(backend=backend), source).reason == "output_dropped_hedge"


# --- request shape ----------------------------------------------------------


def test_request_forces_the_finding_tool_and_uses_live_skill_part6():
    backend, responses, _ = _fake_backend(finding("Drug A may raise Drug B levels."))
    _run(FindingAgent(backend=backend))
    sent = responses.calls[0]
    assert "## Part 6" in sent["instructions"] and "NO_FINDING" in sent["instructions"]
    assert "## Verification" not in sent["instructions"]  # only Part 6, not the rest of SKILL.md
    assert sent["tools"] == [fa.FINDING_TOOL]
    assert sent["tool_choice"] == {"type": "function", "name": "submit_finding"}
    assert FAKE_KEY not in str(sent)


def test_reasoning_model_sends_effort_not_temperature():
    backend, responses, _ = _fake_backend(finding("Drug A may raise Drug B levels."), model="gpt-5.5")
    _run(FindingAgent(backend=backend))
    assert responses.calls[0]["reasoning"] == {"effort": "low"}
    assert "temperature" not in responses.calls[0]


def test_non_reasoning_model_sends_temperature_not_effort():
    backend, responses, _ = _fake_backend(finding("Drug A may raise Drug B levels."), model="gpt-4o-mini")
    _run(FindingAgent(backend=backend))
    assert responses.calls[0]["temperature"] == 0.0
    assert "reasoning" not in responses.calls[0]


def test_usage_including_reasoning_tokens_is_recorded():
    backend, _, _ = _fake_backend(finding("Drug A may raise Drug B levels."))
    _run(FindingAgent(backend=backend))
    assert backend.last_usage == {"input_tokens": 100, "output_tokens": 20, "reasoning_tokens": 5}


def test_skill_edits_are_picked_up_without_a_code_change(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# x\n## Part 6 -- v1\nrule one\n## Verification\nend\n", encoding="utf-8")
    assert "rule one" in fa.skill_part6_text(skill)

    skill.write_text("# x\n## Part 6 -- v2\nrule two\n## Verification\nend\n", encoding="utf-8")
    stat = skill.stat()
    os.utime(skill, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    part6 = fa.skill_part6_text(skill)
    assert "rule two" in part6 and "rule one" not in part6 and "Verification" not in part6


# --- API failures, retry, and key safety ------------------------------------


def test_transient_errors_are_retried_with_backoff_then_succeed():
    backend, responses, sleeps = _fake_backend(RateLimitError("slow down"), APITimeoutError("timeout"), finding("Drug A may raise Drug B levels."))
    assert _run(FindingAgent(backend=backend)).used_llm is True
    assert len(responses.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_retries_are_bounded_then_fall_back_gracefully():
    backend, responses, sleeps = _fake_backend(*[RateLimitError("slow down")] * 4, max_retries=3)
    agent = FindingAgent(backend=backend)
    assert _run(agent) == FindingAgentOutcome("FALLBACK", False, "llm_error")
    assert len(responses.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert agent.last_error_type == "RateLimitError"


def test_invalid_key_is_not_retried_and_never_recorded():
    backend, responses, sleeps = _fake_backend(AuthenticationError(f"Incorrect API key provided: {FAKE_KEY}"))
    agent = FindingAgent(backend=backend)
    assert _run(agent) == FindingAgentOutcome("FALLBACK", False, "llm_error")
    assert len(responses.calls) == 1 and sleeps == []
    recorded = f"{agent.last_error_type} {backend.last_error} {backend.describe()}"
    assert FAKE_KEY not in recorded and "FAKEKEY" not in recorded


# --- provider selector -------------------------------------------------------


def test_openai_provider_without_a_key_falls_back_without_crashing(monkeypatch):
    monkeypatch.setenv("PRIME_AGENT_LLM_PROVIDER", "openai")
    assert _run(FindingAgent()) == FindingAgentOutcome("FALLBACK", False, "skill_unavailable")


def test_unknown_provider_falls_back_without_crashing(monkeypatch):
    monkeypatch.setenv("PRIME_AGENT_LLM_PROVIDER", "not-a-provider")
    assert _run(FindingAgent()) == FindingAgentOutcome("FALLBACK", False, "skill_unavailable")


def test_openai_provider_defaults_to_gpt_5_5_at_low_effort(monkeypatch):
    monkeypatch.setenv("PRIME_AGENT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    backend = fa.build_backend()
    assert isinstance(backend, OpenAIBackend)
    assert backend.describe()["model"] == "gpt-5.5"
    assert backend.describe()["reasoning_effort"] == "low"
    assert FAKE_KEY not in str(backend.describe())
