"""Language-model backends used by the agent layer.

The extraction pipeline is deterministic and rule-based; the model is *advisory*
only.  It narrates plans, proposes fix hints and drafts non-clinical commentary.
It is never allowed to supply a patient value -- everything that reaches the
report comes from the existing extractors and is schema-validated.

Two backends are provided:

``TransformersBackend``
    Loads an open-source Hugging Face model directly with ``transformers``.
``EchoBackend``
    A deterministic, dependency-free stand-in used when the LLM is disabled or
    when loading fails.  It keeps the whole pipeline runnable offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .model_loader import ModelLoader
from .settings import AgentSettings

ADVISORY_SYSTEM_PROMPT = (
    "You are a clinical data engineering assistant inside a validated pipeline. "
    "You never invent, guess or infer patient values. "
    "If a value is unavailable you say null. "
    "You only comment on process, structure, schema conformance and next actions."
)


class LLMBackend(Protocol):
    name: str

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str: ...

    def describe(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class EchoBackend:
    """Deterministic offline backend.

    Returns a short structured acknowledgement of the prompt.  Deterministic
    output keeps runs reproducible and keeps tests free of model downloads.
    """

    name: str = "echo"
    reason: str = "llm_disabled"

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        first_line = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
        if len(first_line) > 200:
            first_line = first_line[:199].rstrip() + "…"
        return f"[deterministic:{self.reason}] {first_line}"

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "reason": self.reason, "loaded": False}


class TransformersBackend:
    """Generates with a locally loaded Hugging Face causal LM."""

    name = "transformers"

    def __init__(self, loader: ModelLoader, settings: AgentSettings):
        self.loader = loader
        self.settings = settings

    def generate(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        import torch  # local import: torch is only needed on this path

        bundle = self.loader.load()
        tokenizer, model = bundle.tokenizer, bundle.model

        messages = [
            {"role": "system", "content": system or ADVISORY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        if getattr(tokenizer, "chat_template", None):
            text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        else:
            text = f"{messages[0]['content']}\n\n{messages[1]['content']}\n\nAnswer:"

        inputs = tokenizer(text, return_tensors="pt")
        device = getattr(model, "device", None)
        if device is not None:
            inputs = {key: value.to(device) for key, value in inputs.items()}

        generation_kwargs = self.settings.generation_kwargs()
        generation_kwargs.update(kwargs)
        if getattr(tokenizer, "pad_token_id", None) is not None:
            generation_kwargs.setdefault("pad_token_id", tokenizer.pad_token_id)

        with torch.no_grad():
            output_ids = model.generate(**inputs, **generation_kwargs)

        prompt_length = inputs["input_ids"].shape[-1]
        completion_ids = output_ids[0][prompt_length:]
        return tokenizer.decode(completion_ids, skip_special_tokens=True).strip()

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, **self.loader.describe()}


@dataclass(slots=True)
class LanguageModel:
    """Facade the agents talk to.

    Falls back to :class:`EchoBackend` when the LLM is disabled or when the
    transformers backend raises, so a model problem can never break a report.
    """

    backend: LLMBackend
    settings: AgentSettings
    fallback: EchoBackend = field(default_factory=lambda: EchoBackend(reason="backend_error"))
    call_count: int = 0
    error_count: int = 0
    last_error: str | None = None

    @classmethod
    def build(cls, settings: AgentSettings, loader: ModelLoader | None = None) -> "LanguageModel":
        if not settings.enable_llm:
            return cls(backend=EchoBackend(reason="llm_disabled"), settings=settings)
        loader = loader or ModelLoader(settings)
        return cls(backend=TransformersBackend(loader, settings), settings=settings)

    @property
    def is_live(self) -> bool:
        return getattr(self.backend, "name", "") == "transformers"

    def advise(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        """Ask for advisory (non-clinical) text.  Never raises."""

        self.call_count += 1
        try:
            return self.backend.generate(prompt, system=system, **kwargs)
        except Exception as exc:  # pragma: no cover - depends on local hardware
            self.error_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return self.fallback.generate(prompt, system=system)

    def describe(self) -> dict[str, Any]:
        info = dict(self.backend.describe())
        info.update(
            {
                "enabled": self.settings.enable_llm,
                "calls": self.call_count,
                "errors": self.error_count,
                "last_error": self.last_error,
            }
        )
        return info
