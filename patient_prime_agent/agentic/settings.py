"""Configuration for the agentic layer.

Every knob is resolved in this order:

1. explicit keyword argument passed to :func:`load_settings`
2. process environment variable
3. key in a ``.env`` file (project root by default)
4. key in an optional JSON config file (``PRIME_AGENT_CONFIG``)
5. built-in default

That ordering is what keeps the model configurable without editing source: set
``PRIME_AGENT_MODEL_ID`` in ``.env`` (or in ``agent_config.json``) and the loader
picks it up.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from ..utils import read_json

ENV_PREFIX = "PRIME_AGENT_"
DEFAULT_ENV_FILENAME = ".env"
DEFAULT_CONFIG_FILENAME = "agent_config.json"

# Model catalogue used when the operator has not pinned an explicit model id.
# ``min_total_gb`` is memory (VRAM on GPU, system RAM on CPU) needed to make the
# tier a sensible choice.
MODEL_TIERS: tuple[dict[str, Any], ...] = (
    {"model_id": "mistralai/Mistral-7B-Instruct-v0.3", "min_total_gb": 24.0, "params_b": 7.0},
    {"model_id": "Qwen/Qwen2.5-7B-Instruct", "min_total_gb": 16.0, "params_b": 7.0},
    {"model_id": "Qwen/Qwen2.5-3B-Instruct", "min_total_gb": 0.0, "params_b": 3.0},
)

FALLBACK_MODEL_ID = "Qwen/Qwen2.5-3B-Instruct"

TRUE_TOKENS = {"1", "true", "yes", "on", "y", "t"}
FALSE_TOKENS = {"0", "false", "no", "off", "n", "f"}


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` ``.env`` file.

    Comments (``#``), blank lines, ``export`` prefixes and quoted values are
    handled.  Implemented locally so the package does not require python-dotenv.
    """

    values: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _coerce_bool(value: Any, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_TOKENS:
        return True
    if text in FALSE_TOKENS:
        return False
    if text in {"", "auto", "none", "null"}:
        return default
    return default


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"auto", "none", "null"}:
        return None
    return text


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class AgentSettings:
    """Resolved runtime configuration for the agentic layer."""

    # --- model -----------------------------------------------------------
    model_id: str | None = None
    """Explicit Hugging Face repo id.  ``None`` means auto-select by hardware."""
    device: str | None = None
    """``cuda`` / ``cpu`` / ``mps``.  ``None`` means auto-detect."""
    dtype: str | None = None
    """``float16`` / ``bfloat16`` / ``float32``.  ``None`` means auto-select."""
    load_in_4bit: bool | None = None
    """Force 4-bit on/off.  ``None`` means "use it only when supported"."""
    max_new_tokens: int = 320
    temperature: float = 0.2
    top_p: float = 0.9
    trust_remote_code: bool = False
    hf_cache_dir: str | None = None
    local_files_only: bool = False
    enable_llm: bool = False
    """When false the runtime uses the deterministic Echo backend.

    The extraction pipeline is rule-based on purpose, so an LLM is never
    required to produce a report.  Turn it on with ``PRIME_AGENT_ENABLE_LLM=1``.
    """

    # --- agent behaviour --------------------------------------------------
    max_retries: int = 2
    refinement_threshold: int = 2
    max_workers: int = 8
    session_id: str | None = None

    # --- provenance -------------------------------------------------------
    source: dict[str, str] = field(default_factory=dict)
    """Maps setting name -> where the value came from (env/env_file/config/default)."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def generation_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"max_new_tokens": self.max_new_tokens}
        if self.temperature and self.temperature > 0:
            kwargs["do_sample"] = True
            kwargs["temperature"] = self.temperature
            kwargs["top_p"] = self.top_p
        else:
            kwargs["do_sample"] = False
        return kwargs


_SETTING_KEYS: dict[str, str] = {
    "model_id": "MODEL_ID",
    "device": "DEVICE",
    "dtype": "DTYPE",
    "load_in_4bit": "LOAD_IN_4BIT",
    "max_new_tokens": "MAX_NEW_TOKENS",
    "temperature": "TEMPERATURE",
    "top_p": "TOP_P",
    "trust_remote_code": "TRUST_REMOTE_CODE",
    "hf_cache_dir": "HF_CACHE_DIR",
    "local_files_only": "LOCAL_FILES_ONLY",
    "enable_llm": "ENABLE_LLM",
    "max_retries": "MAX_RETRIES",
    "refinement_threshold": "REFINEMENT_THRESHOLD",
    "max_workers": "MAX_WORKERS",
    "session_id": "SESSION_ID",
}


class _Resolver:
    """Looks a key up across the override / env / .env / config layers."""

    def __init__(
        self,
        overrides: dict[str, Any],
        environ: dict[str, str],
        env_file: dict[str, str],
        config: dict[str, Any],
    ) -> None:
        self.overrides = overrides
        self.environ = environ
        self.env_file = env_file
        self.config = config
        self.source: dict[str, str] = {}

    def get(self, name: str) -> Any:
        env_key = ENV_PREFIX + _SETTING_KEYS[name]
        if self.overrides.get(name) is not None:
            self.source[name] = "override"
            return self.overrides[name]
        if env_key in self.environ and str(self.environ[env_key]).strip() != "":
            self.source[name] = "env"
            return self.environ[env_key]
        if env_key in self.env_file and str(self.env_file[env_key]).strip() != "":
            self.source[name] = "env_file"
            return self.env_file[env_key]
        if name in self.config and self.config[name] is not None:
            self.source[name] = "config_file"
            return self.config[name]
        if env_key in self.config and self.config[env_key] is not None:
            self.source[name] = "config_file"
            return self.config[env_key]
        self.source[name] = "default"
        return None


def load_settings(
    project_root: Path | None = None,
    env_file: Path | None = None,
    config_file: Path | None = None,
    environ: dict[str, str] | None = None,
    **overrides: Any,
) -> AgentSettings:
    """Build :class:`AgentSettings` from overrides, env, ``.env`` and config file."""

    environ = dict(os.environ if environ is None else environ)
    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parents[2]

    env_path = Path(env_file) if env_file else root / DEFAULT_ENV_FILENAME
    env_values = parse_env_file(env_path)

    config_path_raw = (
        config_file
        or environ.get(f"{ENV_PREFIX}CONFIG")
        or env_values.get(f"{ENV_PREFIX}CONFIG")
        or root / DEFAULT_CONFIG_FILENAME
    )
    config_path = Path(config_path_raw)
    if not config_path.is_absolute():
        config_path = root / config_path
    config_values = read_json(config_path, {})
    if not isinstance(config_values, dict):
        config_values = {}

    resolver = _Resolver(overrides, environ, env_values, config_values)
    defaults = AgentSettings()

    settings = AgentSettings(
        model_id=_coerce_optional_str(resolver.get("model_id")),
        device=_coerce_optional_str(resolver.get("device")),
        dtype=_coerce_optional_str(resolver.get("dtype")),
        load_in_4bit=_coerce_bool(resolver.get("load_in_4bit"), None),
        max_new_tokens=_coerce_int(resolver.get("max_new_tokens"), defaults.max_new_tokens),
        temperature=_coerce_float(resolver.get("temperature"), defaults.temperature),
        top_p=_coerce_float(resolver.get("top_p"), defaults.top_p),
        trust_remote_code=bool(_coerce_bool(resolver.get("trust_remote_code"), defaults.trust_remote_code)),
        hf_cache_dir=_coerce_optional_str(resolver.get("hf_cache_dir")),
        local_files_only=bool(_coerce_bool(resolver.get("local_files_only"), defaults.local_files_only)),
        enable_llm=bool(_coerce_bool(resolver.get("enable_llm"), defaults.enable_llm)),
        max_retries=_coerce_int(resolver.get("max_retries"), defaults.max_retries),
        refinement_threshold=_coerce_int(resolver.get("refinement_threshold"), defaults.refinement_threshold),
        max_workers=_coerce_int(resolver.get("max_workers"), defaults.max_workers),
        session_id=_coerce_optional_str(resolver.get("session_id")),
    )
    settings.source = dict(resolver.source)
    return settings


def write_example_env(path: Path) -> Path:
    """Write a documented ``.env.example`` next to the project root."""

    lines = [
        "# Prime Agent runtime configuration.",
        "# Copy to .env and edit; no source changes are required to swap models.",
        "",
        "# Leave unset to auto-select by hardware:",
        "#   >=24 GB -> mistralai/Mistral-7B-Instruct-v0.3",
        "#   >=16 GB -> Qwen/Qwen2.5-7B-Instruct",
        "#   otherwise -> Qwen/Qwen2.5-3B-Instruct",
        "PRIME_AGENT_MODEL_ID=Qwen/Qwen2.5-3B-Instruct",
        "",
        "# auto | cpu | cuda | mps",
        "PRIME_AGENT_DEVICE=auto",
        "# auto | float16 | bfloat16 | float32",
        "PRIME_AGENT_DTYPE=auto",
        "# auto (only when CUDA + bitsandbytes are present) | 1 | 0",
        "PRIME_AGENT_LOAD_IN_4BIT=auto",
        "",
        "# The extraction pipeline is deterministic; the model is advisory only.",
        "PRIME_AGENT_ENABLE_LLM=0",
        "PRIME_AGENT_MAX_NEW_TOKENS=320",
        "PRIME_AGENT_TEMPERATURE=0.2",
        "PRIME_AGENT_TOP_P=0.9",
        "PRIME_AGENT_LOCAL_FILES_ONLY=0",
        "# PRIME_AGENT_HF_CACHE_DIR=D:/models",
        "",
        "PRIME_AGENT_MAX_RETRIES=2",
        "PRIME_AGENT_REFINEMENT_THRESHOLD=2",
        "PRIME_AGENT_MAX_WORKERS=8",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
