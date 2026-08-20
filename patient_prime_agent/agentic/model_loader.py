"""Hardware detection and Hugging Face ``transformers`` model loading.

Design notes:

* The model is loaded **directly with transformers** -- no Ollama, no server.
* Hardware detection, dtype/device selection and quantization selection are pure
  functions over a :class:`HardwareProfile`, so they can be unit tested without a
  GPU and without downloading weights.
* :class:`ModelLoader` is lazy: importing this module, constructing a loader, or
  building a :class:`ModelPlan` never touches the network.  Weights are only
  fetched on the first :meth:`ModelLoader.load` call.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from dataclasses import dataclass, asdict, field
from typing import Any

from .settings import FALLBACK_MODEL_ID, MODEL_TIERS, AgentSettings

BYTES_PER_GB = 1024 ** 3

# Bytes per parameter for each dtype, used to size a model against memory.
DTYPE_BYTES = {
    "float32": 4.0,
    "float16": 2.0,
    "bfloat16": 2.0,
    "int8": 1.0,
    "int4": 0.5,
}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@dataclass(slots=True)
class HardwareProfile:
    """A snapshot of what the current machine can run."""

    torch_available: bool = False
    torch_version: str | None = None
    transformers_available: bool = False
    transformers_version: str | None = None
    has_cuda: bool = False
    cuda_device_count: int = 0
    cuda_device_name: str | None = None
    total_vram_gb: float = 0.0
    has_mps: bool = False
    total_ram_gb: float = 0.0
    supports_bfloat16: bool = False
    has_bitsandbytes: bool = False
    has_accelerate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def accelerator_memory_gb(self) -> float:
        """Memory the model will actually have to fit into."""

        if self.has_cuda and self.total_vram_gb > 0:
            return self.total_vram_gb
        return self.total_ram_gb

    @classmethod
    def detect(cls) -> "HardwareProfile":
        """Probe torch / transformers / bitsandbytes without raising."""

        profile = cls(
            transformers_available=_module_available("transformers"),
            has_bitsandbytes=_module_available("bitsandbytes"),
            has_accelerate=_module_available("accelerate"),
            total_ram_gb=_detect_system_ram_gb(),
        )
        if profile.transformers_available:
            try:
                profile.transformers_version = importlib.import_module("transformers").__version__
            except Exception:  # pragma: no cover - defensive
                profile.transformers_version = None

        try:
            torch = importlib.import_module("torch")
        except Exception:
            return profile

        profile.torch_available = True
        profile.torch_version = getattr(torch, "__version__", None)

        try:
            profile.has_cuda = bool(torch.cuda.is_available())
        except Exception:  # pragma: no cover - driver quirks
            profile.has_cuda = False

        if profile.has_cuda:
            try:
                profile.cuda_device_count = int(torch.cuda.device_count())
                properties = torch.cuda.get_device_properties(0)
                profile.cuda_device_name = getattr(properties, "name", None)
                profile.total_vram_gb = round(properties.total_memory / BYTES_PER_GB, 2)
            except Exception:  # pragma: no cover - driver quirks
                pass
            try:
                profile.supports_bfloat16 = bool(torch.cuda.is_bf16_supported())
            except Exception:  # pragma: no cover
                profile.supports_bfloat16 = False

        try:
            backend = getattr(torch.backends, "mps", None)
            profile.has_mps = bool(backend is not None and backend.is_available())
        except Exception:  # pragma: no cover
            profile.has_mps = False

        return profile


def _detect_system_ram_gb() -> float:
    try:
        psutil = importlib.import_module("psutil")
        return round(psutil.virtual_memory().total / BYTES_PER_GB, 2)
    except Exception:
        pass
    try:  # POSIX fallback
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round((pages * page_size) / BYTES_PER_GB, 2)
    except (AttributeError, ValueError, OSError):
        return 0.0


@dataclass(slots=True)
class ModelPlan:
    """Everything needed to call ``AutoModelForCausalLM.from_pretrained``."""

    model_id: str
    device: str
    dtype: str
    load_in_4bit: bool
    device_map: str | None
    reasons: list[str] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def select_device(settings: AgentSettings, profile: HardwareProfile, reasons: list[str]) -> str:
    requested = (settings.device or "").strip().lower()
    if requested in {"cuda", "gpu"}:
        if profile.has_cuda:
            reasons.append("device=cuda (requested and available)")
            return "cuda"
        reasons.append("device=cpu (cuda requested but unavailable)")
        return "cpu"
    if requested == "mps":
        if profile.has_mps:
            reasons.append("device=mps (requested and available)")
            return "mps"
        reasons.append("device=cpu (mps requested but unavailable)")
        return "cpu"
    if requested == "cpu":
        reasons.append("device=cpu (requested)")
        return "cpu"

    if profile.has_cuda:
        reasons.append(f"device=cuda (auto-detected, {profile.cuda_device_count} device(s))")
        return "cuda"
    if profile.has_mps:
        reasons.append("device=mps (auto-detected Apple Silicon)")
        return "mps"
    reasons.append("device=cpu (no accelerator detected)")
    return "cpu"


def select_dtype(settings: AgentSettings, profile: HardwareProfile, device: str, reasons: list[str]) -> str:
    requested = (settings.dtype or "").strip().lower()
    aliases = {"fp16": "float16", "half": "float16", "bf16": "bfloat16", "fp32": "float32", "full": "float32"}
    requested = aliases.get(requested, requested)

    if requested in DTYPE_BYTES and requested != "int4" and requested != "int8":
        if requested == "bfloat16" and device == "cuda" and not profile.supports_bfloat16:
            reasons.append("dtype=float16 (bfloat16 requested but not supported by this GPU)")
            return "float16"
        if requested in {"float16", "bfloat16"} and device == "cpu":
            # float16 math on CPU is unsupported or extremely slow in torch.
            reasons.append(f"dtype=float32 ({requested} requested but unsafe on CPU)")
            return "float32"
        reasons.append(f"dtype={requested} (requested)")
        return requested

    if device == "cuda":
        if profile.supports_bfloat16:
            reasons.append("dtype=bfloat16 (GPU reports bf16 support)")
            return "bfloat16"
        reasons.append("dtype=float16 (GPU without bf16 support)")
        return "float16"
    if device == "mps":
        reasons.append("dtype=float16 (Apple Metal)")
        return "float16"
    reasons.append("dtype=float32 (CPU inference)")
    return "float32"


def quantization_supported(profile: HardwareProfile, device: str) -> bool:
    """4-bit via bitsandbytes needs both a CUDA device and the bitsandbytes wheel."""

    return device == "cuda" and profile.has_cuda and profile.has_bitsandbytes


def select_quantization(
    settings: AgentSettings,
    profile: HardwareProfile,
    device: str,
    reasons: list[str],
) -> bool:
    supported = quantization_supported(profile, device)
    if settings.load_in_4bit is True:
        if supported:
            reasons.append("4-bit quantization enabled (requested, bitsandbytes+CUDA available)")
            return True
        missing = "CUDA" if device != "cuda" else "bitsandbytes"
        reasons.append(f"4-bit quantization disabled (requested but {missing} unavailable)")
        return False
    if settings.load_in_4bit is False:
        reasons.append("4-bit quantization disabled (explicitly turned off)")
        return False
    if supported:
        reasons.append("4-bit quantization enabled (auto: bitsandbytes+CUDA available)")
        return True
    reasons.append("4-bit quantization disabled (auto: not supported on this machine)")
    return False


def select_model_id(
    settings: AgentSettings,
    profile: HardwareProfile,
    load_in_4bit: bool,
    dtype: str,
    reasons: list[str],
) -> str:
    if settings.model_id:
        reasons.append(f"model={settings.model_id} (pinned by configuration)")
        return settings.model_id

    available_gb = profile.accelerator_memory_gb
    # 4-bit roughly quarters an fp16 footprint, so the same card fits a bigger tier.
    effective_gb = available_gb * (2.0 if load_in_4bit else 1.0)
    for tier in MODEL_TIERS:
        if effective_gb >= tier["min_total_gb"]:
            reasons.append(
                f"model={tier['model_id']} (auto: {available_gb:.1f} GB available, "
                f"{effective_gb:.1f} GB effective, tier needs {tier['min_total_gb']:.0f} GB)"
            )
            return str(tier["model_id"])
    reasons.append(f"model={FALLBACK_MODEL_ID} (auto fallback for low-memory system)")
    return FALLBACK_MODEL_ID


def estimate_weight_gb(params_b: float, dtype: str, load_in_4bit: bool) -> float:
    bytes_per_param = DTYPE_BYTES["int4"] if load_in_4bit else DTYPE_BYTES.get(dtype, 2.0)
    return round(params_b * 1e9 * bytes_per_param / BYTES_PER_GB, 2)


def resolve_model_plan(settings: AgentSettings, profile: HardwareProfile | None = None) -> ModelPlan:
    """Turn settings + hardware into a concrete, loggable loading plan.

    Pure and side-effect free -- safe to call in tests and to record in memory.
    """

    profile = profile or HardwareProfile.detect()
    reasons: list[str] = []

    device = select_device(settings, profile, reasons)
    dtype = select_dtype(settings, profile, device, reasons)
    load_in_4bit = select_quantization(settings, profile, device, reasons)
    if load_in_4bit and dtype == "float32":
        # bnb compute dtype must be a half type; keep them consistent.
        dtype = "bfloat16" if profile.supports_bfloat16 else "float16"
        reasons.append(f"dtype={dtype} (compute dtype aligned with 4-bit quantization)")
    model_id = select_model_id(settings, profile, load_in_4bit, dtype, reasons)

    device_map = "auto" if (device == "cuda" and profile.has_accelerate) else None
    if device_map:
        reasons.append("device_map=auto (accelerate available)")

    return ModelPlan(
        model_id=model_id,
        device=device,
        dtype=dtype,
        load_in_4bit=load_in_4bit,
        device_map=device_map,
        reasons=reasons,
        profile=profile.to_dict(),
    )


def _torch_dtype(dtype_name: str) -> Any:
    torch = importlib.import_module("torch")
    return getattr(torch, dtype_name)


def _dtype_kwarg_name() -> str:
    """transformers >= 4.56 renamed ``torch_dtype`` to ``dtype``."""

    try:
        version = importlib.import_module("transformers").__version__
        major_str, minor_str = (version.split(".") + ["0", "0"])[:2]
        major, minor = int(major_str), int("".join(ch for ch in minor_str if ch.isdigit()) or 0)
    except Exception:  # pragma: no cover - defensive
        return "dtype"
    if major > 4 or (major == 4 and minor >= 56):
        return "dtype"
    return "torch_dtype"


def build_quantization_config(plan: ModelPlan) -> Any | None:
    """Build a ``BitsAndBytesConfig`` for a 4-bit plan, or ``None``."""

    if not plan.load_in_4bit:
        return None
    transformers = importlib.import_module("transformers")
    return transformers.BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=_torch_dtype(plan.dtype),
    )


def build_model_kwargs(plan: ModelPlan, settings: AgentSettings) -> dict[str, Any]:
    """Assemble ``from_pretrained`` kwargs (dtype resolved lazily by the caller)."""

    kwargs: dict[str, Any] = {
        "low_cpu_mem_usage": True,
        "trust_remote_code": settings.trust_remote_code,
        "local_files_only": settings.local_files_only,
    }
    if settings.hf_cache_dir:
        kwargs["cache_dir"] = settings.hf_cache_dir
    if plan.device_map:
        kwargs["device_map"] = plan.device_map
    return kwargs


@dataclass(slots=True)
class LoadedModel:
    model: Any
    tokenizer: Any
    plan: ModelPlan


class ModelLoader:
    """Lazily loads a causal LM with ``transformers``.

    The loader caches the model in-process, so the persistent agents share a
    single set of weights across an entire session.
    """

    def __init__(self, settings: AgentSettings, profile: HardwareProfile | None = None):
        self.settings = settings
        self.profile = profile or HardwareProfile.detect()
        self._plan: ModelPlan | None = None
        self._loaded: LoadedModel | None = None
        self.last_error: str | None = None

    @property
    def plan(self) -> ModelPlan:
        if self._plan is None:
            self._plan = resolve_model_plan(self.settings, self.profile)
        return self._plan

    @property
    def is_loaded(self) -> bool:
        return self._loaded is not None

    def describe(self) -> dict[str, Any]:
        """A JSON-safe summary of what would be (or was) loaded."""

        return {
            "plan": self.plan.to_dict(),
            "loaded": self.is_loaded,
            "last_error": self.last_error,
            "estimated_weight_gb": estimate_weight_gb(
                _params_for_model(self.plan.model_id), self.plan.dtype, self.plan.load_in_4bit
            ),
        }

    def load(self) -> LoadedModel:
        """Load tokenizer + model.  Downloads weights on first call."""

        if self._loaded is not None:
            return self._loaded

        transformers = importlib.import_module("transformers")
        plan = self.plan
        kwargs = build_model_kwargs(plan, self.settings)

        tokenizer_kwargs = {
            "trust_remote_code": self.settings.trust_remote_code,
            "local_files_only": self.settings.local_files_only,
        }
        if self.settings.hf_cache_dir:
            tokenizer_kwargs["cache_dir"] = self.settings.hf_cache_dir
        tokenizer = transformers.AutoTokenizer.from_pretrained(plan.model_id, **tokenizer_kwargs)
        if tokenizer.pad_token is None and getattr(tokenizer, "eos_token", None) is not None:
            tokenizer.pad_token = tokenizer.eos_token

        quantization_config = build_quantization_config(plan)
        if quantization_config is not None:
            kwargs["quantization_config"] = quantization_config
        else:
            kwargs[_dtype_kwarg_name()] = _torch_dtype(plan.dtype)

        try:
            model = transformers.AutoModelForCausalLM.from_pretrained(plan.model_id, **kwargs)
        except TypeError as exc:
            # Tolerate the dtype/torch_dtype rename in either direction.
            if "dtype" not in str(exc) or quantization_config is not None:
                raise
            kwargs.pop("dtype", None)
            kwargs.pop("torch_dtype", None)
            alternate = "torch_dtype" if _dtype_kwarg_name() == "dtype" else "dtype"
            kwargs[alternate] = _torch_dtype(plan.dtype)
            model = transformers.AutoModelForCausalLM.from_pretrained(plan.model_id, **kwargs)

        if quantization_config is None and plan.device_map is None and plan.device != "cpu":
            model = model.to(plan.device)
        model.eval()

        self._loaded = LoadedModel(model=model, tokenizer=tokenizer, plan=plan)
        return self._loaded

    def unload(self) -> None:
        self._loaded = None
        try:
            torch = importlib.import_module("torch")
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # pragma: no cover
            pass


def _params_for_model(model_id: str) -> float:
    for tier in MODEL_TIERS:
        if tier["model_id"] == model_id:
            return float(tier["params_b"])
    lowered = model_id.lower()
    for token, value in (("70b", 70.0), ("14b", 14.0), ("13b", 13.0), ("8b", 8.0), ("7b", 7.0), ("3b", 3.0), ("1.5b", 1.5), ("0.5b", 0.5)):
        if token in lowered:
            return value
    return 7.0
