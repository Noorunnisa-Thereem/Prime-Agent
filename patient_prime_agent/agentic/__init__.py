"""Prime Agent-style agentic layer wrapped around the existing extraction pipeline.

The modules in this package add a persistent agent runtime, a main orchestrator
agent, persistent category sub-agents, agent-to-agent messaging, and a continual
harness (Prompt + Skills + Memory + Sub-agent configuration).  None of them
replace the rule-based extractors, schemas, skills, or report builder that
already exist -- they wrap and drive them.
"""

from __future__ import annotations

from .settings import AgentSettings, load_settings
from .model_loader import HardwareProfile, ModelPlan, ModelLoader, resolve_model_plan
from .llm import LanguageModel, EchoBackend, TransformersBackend
from .a2a import A2ABus, A2AMessage, MessageType
from .session import Session, SessionStore, Trajectory, TrajectoryStep
from .memory import AgentMemory, MemoryRecord
from .harness import ContinualHarness, HarnessComponent, SubAgentConfig
from .refine import RefinementEngine, RefinementRecord
from .runtime import PersistentAgent, RLMRuntime, TaskEnvelope, TaskResult
from .subagents import CategorySubAgent, build_category_subagents
from .main_agent import MainOrchestratorAgent, OrchestrationOutcome

__all__ = [
    "AgentSettings",
    "load_settings",
    "HardwareProfile",
    "ModelPlan",
    "ModelLoader",
    "resolve_model_plan",
    "LanguageModel",
    "EchoBackend",
    "TransformersBackend",
    "A2ABus",
    "A2AMessage",
    "MessageType",
    "Session",
    "SessionStore",
    "Trajectory",
    "TrajectoryStep",
    "AgentMemory",
    "MemoryRecord",
    "ContinualHarness",
    "HarnessComponent",
    "SubAgentConfig",
    "RefinementEngine",
    "RefinementRecord",
    "PersistentAgent",
    "RLMRuntime",
    "TaskEnvelope",
    "TaskResult",
    "CategorySubAgent",
    "build_category_subagents",
    "MainOrchestratorAgent",
    "OrchestrationOutcome",
]
