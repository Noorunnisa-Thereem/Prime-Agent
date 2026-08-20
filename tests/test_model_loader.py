"""Model loading: configuration resolution, hardware detection, dtype/quant choice."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patient_prime_agent.agentic.model_loader import (
    HardwareProfile,
    ModelLoader,
    build_model_kwargs,
    build_quantization_config,
    estimate_weight_gb,
    quantization_supported,
    resolve_model_plan,
    select_device,
    select_dtype,
    select_model_id,
    select_quantization,
)
from patient_prime_agent.agentic.settings import (
    AgentSettings,
    load_settings,
    parse_env_file,
    write_example_env,
)


def cpu_profile(ram_gb: float = 8.0) -> HardwareProfile:
    return HardwareProfile(
        torch_available=True,
        transformers_available=True,
        has_cuda=False,
        total_ram_gb=ram_gb,
        has_bitsandbytes=False,
        has_accelerate=True,
    )


def gpu_profile(vram_gb: float = 24.0, bf16: bool = True, bnb: bool = True) -> HardwareProfile:
    return HardwareProfile(
        torch_available=True,
        transformers_available=True,
        has_cuda=True,
        cuda_device_count=1,
        cuda_device_name="Test GPU",
        total_vram_gb=vram_gb,
        total_ram_gb=64.0,
        supports_bfloat16=bf16,
        has_bitsandbytes=bnb,
        has_accelerate=True,
    )


# ----------------------------------------------------------------------
# settings resolution
# ----------------------------------------------------------------------
def test_env_file_is_parsed_with_comments_quotes_and_export(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "PRIME_AGENT_MODEL_ID=Qwen/Qwen2.5-7B-Instruct",
                'export PRIME_AGENT_DEVICE="cuda"',
                "PRIME_AGENT_DTYPE='bfloat16'",
                "malformed line without equals",
            ]
        ),
        encoding="utf-8",
    )
    values = parse_env_file(env)
    assert values["PRIME_AGENT_MODEL_ID"] == "Qwen/Qwen2.5-7B-Instruct"
    assert values["PRIME_AGENT_DEVICE"] == "cuda"
    assert values["PRIME_AGENT_DTYPE"] == "bfloat16"
    assert len(values) == 3


def test_model_is_configurable_from_env_file_without_touching_source(tmp_path: Path):
    (tmp_path / ".env").write_text(
        "PRIME_AGENT_MODEL_ID=mistralai/Mistral-7B-Instruct-v0.3\nPRIME_AGENT_ENABLE_LLM=1\n",
        encoding="utf-8",
    )
    settings = load_settings(project_root=tmp_path, environ={})
    assert settings.model_id == "mistralai/Mistral-7B-Instruct-v0.3"
    assert settings.enable_llm is True
    assert settings.source["model_id"] == "env_file"


def test_process_environment_overrides_env_file(tmp_path: Path):
    (tmp_path / ".env").write_text("PRIME_AGENT_MODEL_ID=Qwen/Qwen2.5-3B-Instruct\n", encoding="utf-8")
    settings = load_settings(
        project_root=tmp_path,
        environ={"PRIME_AGENT_MODEL_ID": "Qwen/Qwen2.5-7B-Instruct"},
    )
    assert settings.model_id == "Qwen/Qwen2.5-7B-Instruct"
    assert settings.source["model_id"] == "env"


def test_json_config_file_is_honoured(tmp_path: Path):
    (tmp_path / "agent_config.json").write_text(
        json.dumps({"model_id": "Qwen/Qwen2.5-7B-Instruct", "max_new_tokens": 64}), encoding="utf-8"
    )
    settings = load_settings(project_root=tmp_path, environ={})
    assert settings.model_id == "Qwen/Qwen2.5-7B-Instruct"
    assert settings.max_new_tokens == 64
    assert settings.source["model_id"] == "config_file"


def test_auto_values_resolve_to_none_so_detection_can_run(tmp_path: Path):
    (tmp_path / ".env").write_text(
        "PRIME_AGENT_DEVICE=auto\nPRIME_AGENT_DTYPE=auto\nPRIME_AGENT_LOAD_IN_4BIT=auto\n", encoding="utf-8"
    )
    settings = load_settings(project_root=tmp_path, environ={})
    assert settings.device is None
    assert settings.dtype is None
    assert settings.load_in_4bit is None


def test_write_example_env_documents_every_model_knob(tmp_path: Path):
    path = write_example_env(tmp_path / ".env.example")
    text = path.read_text(encoding="utf-8")
    for key in ("PRIME_AGENT_MODEL_ID", "PRIME_AGENT_DEVICE", "PRIME_AGENT_DTYPE", "PRIME_AGENT_LOAD_IN_4BIT"):
        assert key in text


# ----------------------------------------------------------------------
# hardware detection
# ----------------------------------------------------------------------
def test_hardware_detection_runs_and_is_json_safe():
    profile = HardwareProfile.detect()
    assert isinstance(profile.to_dict(), dict)
    json.dumps(profile.to_dict())
    assert profile.total_ram_gb >= 0.0
    if not profile.has_cuda:
        assert profile.total_vram_gb == 0.0


def test_accelerator_memory_prefers_vram_when_cuda_present():
    assert gpu_profile(vram_gb=12.0).accelerator_memory_gb == 12.0
    assert cpu_profile(ram_gb=6.0).accelerator_memory_gb == 6.0


# ----------------------------------------------------------------------
# device / dtype selection
# ----------------------------------------------------------------------
def test_device_auto_detects_cpu_and_gpu():
    assert select_device(AgentSettings(), cpu_profile(), []) == "cpu"
    assert select_device(AgentSettings(), gpu_profile(), []) == "cuda"


def test_requesting_cuda_on_a_cpu_machine_falls_back_to_cpu():
    reasons: list[str] = []
    assert select_device(AgentSettings(device="cuda"), cpu_profile(), reasons) == "cpu"
    assert any("unavailable" in reason for reason in reasons)


def test_cpu_gets_float32_and_never_float16():
    assert select_dtype(AgentSettings(), cpu_profile(), "cpu", []) == "float32"
    assert select_dtype(AgentSettings(dtype="float16"), cpu_profile(), "cpu", []) == "float32"


def test_gpu_dtype_follows_bfloat16_support():
    assert select_dtype(AgentSettings(), gpu_profile(bf16=True), "cuda", []) == "bfloat16"
    assert select_dtype(AgentSettings(), gpu_profile(bf16=False), "cuda", []) == "float16"
    assert select_dtype(AgentSettings(dtype="bfloat16"), gpu_profile(bf16=False), "cuda", []) == "float16"


# ----------------------------------------------------------------------
# quantization
# ----------------------------------------------------------------------
def test_4bit_only_when_cuda_and_bitsandbytes_are_both_available():
    assert quantization_supported(gpu_profile(bnb=True), "cuda") is True
    assert quantization_supported(gpu_profile(bnb=False), "cuda") is False
    assert quantization_supported(cpu_profile(), "cpu") is False


def test_4bit_request_on_unsupported_machine_is_refused_not_crashed():
    reasons: list[str] = []
    enabled = select_quantization(AgentSettings(load_in_4bit=True), cpu_profile(), "cpu", reasons)
    assert enabled is False
    assert any("unavailable" in reason for reason in reasons)


def test_4bit_auto_enables_only_where_supported():
    assert select_quantization(AgentSettings(), gpu_profile(bnb=True), "cuda", []) is True
    assert select_quantization(AgentSettings(), gpu_profile(bnb=False), "cuda", []) is False
    assert select_quantization(AgentSettings(load_in_4bit=False), gpu_profile(bnb=True), "cuda", []) is False


def test_quantization_config_is_none_unless_plan_requests_4bit():
    plan = resolve_model_plan(AgentSettings(), cpu_profile())
    assert plan.load_in_4bit is False
    assert build_quantization_config(plan) is None


# ----------------------------------------------------------------------
# model selection
# ----------------------------------------------------------------------
def test_low_memory_system_prefers_the_3b_model():
    plan = resolve_model_plan(AgentSettings(), cpu_profile(ram_gb=8.0))
    assert plan.model_id == "Qwen/Qwen2.5-3B-Instruct"
    assert plan.device == "cpu"
    assert plan.dtype == "float32"


def test_16gb_accelerator_moves_up_to_the_7b_model():
    model_id = select_model_id(AgentSettings(), gpu_profile(vram_gb=16.0, bnb=False), False, "bfloat16", [])
    assert model_id == "Qwen/Qwen2.5-7B-Instruct"


def test_large_gpu_selects_mistral_tier():
    model_id = select_model_id(AgentSettings(), gpu_profile(vram_gb=24.0, bnb=False), False, "bfloat16", [])
    assert model_id == "mistralai/Mistral-7B-Instruct-v0.3"


def test_pinned_model_id_always_wins_over_hardware_detection():
    settings = AgentSettings(model_id="Qwen/Qwen2.5-3B-Instruct")
    plan = resolve_model_plan(settings, gpu_profile(vram_gb=80.0))
    assert plan.model_id == "Qwen/Qwen2.5-3B-Instruct"
    assert any("pinned by configuration" in reason for reason in plan.reasons)


def test_estimate_weight_gb_shrinks_with_4bit():
    assert estimate_weight_gb(7.0, "bfloat16", False) > estimate_weight_gb(7.0, "bfloat16", True)


# ----------------------------------------------------------------------
# plan + loader
# ----------------------------------------------------------------------
def test_plan_is_json_serialisable_and_explains_itself():
    plan = resolve_model_plan(AgentSettings(), gpu_profile())
    json.dumps(plan.to_dict())
    assert plan.reasons
    assert plan.device == "cuda"
    assert plan.load_in_4bit is True
    # 4-bit compute dtype must stay a half type.
    assert plan.dtype in {"bfloat16", "float16"}


def test_4bit_plan_never_keeps_float32_compute_dtype():
    settings = AgentSettings(dtype="float32", load_in_4bit=True)
    plan = resolve_model_plan(settings, gpu_profile(bf16=False))
    assert plan.load_in_4bit is True
    assert plan.dtype == "float16"


def test_model_kwargs_carry_cache_and_offline_flags():
    settings = AgentSettings(hf_cache_dir="/tmp/models", local_files_only=True)
    plan = resolve_model_plan(settings, cpu_profile())
    kwargs = build_model_kwargs(plan, settings)
    assert kwargs["cache_dir"] == "/tmp/models"
    assert kwargs["local_files_only"] is True
    assert kwargs["low_cpu_mem_usage"] is True


def test_loader_is_lazy_and_downloads_nothing_until_load_is_called():
    loader = ModelLoader(AgentSettings(), cpu_profile())
    assert loader.is_loaded is False
    described = loader.describe()
    assert described["loaded"] is False
    assert described["plan"]["model_id"] == "Qwen/Qwen2.5-3B-Instruct"
    assert loader.is_loaded is False


def test_loader_plan_is_computed_once_and_cached():
    loader = ModelLoader(AgentSettings(), gpu_profile())
    assert loader.plan is loader.plan


@pytest.mark.skipif(
    not HardwareProfile.detect().transformers_available, reason="transformers is not installed"
)
def test_transformers_is_importable_so_the_loader_path_is_real():
    import transformers  # noqa: F401

    from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: F401
