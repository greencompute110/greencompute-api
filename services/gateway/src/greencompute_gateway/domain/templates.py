"""Workload templates (VLLM, VLLM-Vision, Diffusion) for GreenCompute."""

from __future__ import annotations

from greencompute_protocol import (
    InferenceRuntimeConfig,
    WorkloadCreateRequest,
    WorkloadRequirements,
    WorkloadLifecyclePolicy,
)
from greencompute_protocol.enums import WorkloadKind, SecurityTier


def _metadata_with_hf_token(kwargs: dict[str, object]) -> dict[str, object]:
    """Pull `hf_token` out of the request and stash it under workload.metadata
    so the miner can pick it up during artifact dispatch. Empty/missing token
    → empty dict (miner falls back to its own env)."""
    token = kwargs.get("hf_token")
    if isinstance(token, str) and token.strip():
        return {"hf_token": token.strip()}
    return {}


def build_vllm_workload(
    model: str,
    name: str | None = None,
    image: str = "vllm/vllm-openai:v0.19.1-cu130-ubuntu2404",
    concurrency: int = 8,
    max_model_len: int = 16384,
    **kwargs: object,
) -> WorkloadCreateRequest:
    """Build a VLLM workload spec from HuggingFace model id."""
    if "/" not in model:
        raise ValueError("model must be org/model format")
    workload_name = name or model.replace("/", "-")
    return WorkloadCreateRequest(
        name=workload_name,
        image=image,
        kind=WorkloadKind.INFERENCE,
        security_tier=SecurityTier.STANDARD,
        requirements=WorkloadRequirements(
            gpu_count=1,
            min_vram_gb_per_gpu=24,
            cpu_cores=8,
            memory_gb=32,
            max_instances=8,
            concurrency=concurrency,
        ),
        runtime=InferenceRuntimeConfig(
            runtime_kind="vllm",
            model_identifier=model,
            model_revision=kwargs.get("revision"),
            tokenizer_identifier=kwargs.get("tokenizer"),
        ),
        lifecycle=WorkloadLifecyclePolicy(
            scaling_threshold=0.75,
            shutdown_after_seconds=300,
            warmup_enabled=True,
            warmup_path="/health",
        ),
        readme=kwargs.get("readme", ""),
        public=kwargs.get("public", False),
        metadata=_metadata_with_hf_token(kwargs),
    )


def build_diffusion_workload(
    model: str,
    name: str | None = None,
    image: str = "ghcr.io/greencompute110/diffusion:latest",
    concurrency: int = 1,
    **kwargs: object,
) -> WorkloadCreateRequest:
    """Build a diffusion workload spec for image generation models."""
    if "/" not in model:
        raise ValueError("model must be org/model format")
    workload_name = name or model.replace("/", "-")
    return WorkloadCreateRequest(
        name=workload_name,
        image=image,
        kind=WorkloadKind.INFERENCE,
        security_tier=SecurityTier.STANDARD,
        requirements=WorkloadRequirements(
            gpu_count=kwargs.get("gpu_count", 1),
            min_vram_gb_per_gpu=kwargs.get("min_vram_gb_per_gpu", 16),
            cpu_cores=4,
            memory_gb=16,
            max_instances=4,
            concurrency=concurrency,
        ),
        runtime=InferenceRuntimeConfig(
            runtime_kind="diffusion",
            model_identifier=model,
            model_revision=kwargs.get("revision"),
        ),
        lifecycle=WorkloadLifecyclePolicy(
            scaling_threshold=0.75,
            shutdown_after_seconds=600,
            warmup_enabled=True,
            warmup_path="/health",
        ),
        readme=kwargs.get("readme", ""),
        public=kwargs.get("public", False),
        metadata=_metadata_with_hf_token(kwargs),
    )

def build_vllm_vision_workload(
    model: str,
    name: str | None = None,
    image: str = "vllm/vllm-openai:v0.19.1-cu130-ubuntu2404",
    concurrency: int = 4,
    max_model_len: int = 4096,
    **kwargs: object,
) -> WorkloadCreateRequest:
    """Build a VLLM Vision workload spec for multimodal models."""
    if "/" not in model:
        raise ValueError("model must be org/model format")
    workload_name = name or model.replace("/", "-")
    return WorkloadCreateRequest(
        name=workload_name,
        image=image,
        kind=WorkloadKind.INFERENCE,
        security_tier=SecurityTier.STANDARD,
        requirements=WorkloadRequirements(
            gpu_count=kwargs.get("gpu_count", 1),
            min_vram_gb_per_gpu=kwargs.get("min_vram_gb_per_gpu", 24),
            cpu_cores=8,
            memory_gb=32,
            max_instances=4,
            concurrency=concurrency,
        ),
        runtime=InferenceRuntimeConfig(
            runtime_kind="vllm",
            model_identifier=model,
            model_revision=kwargs.get("revision"),
            tokenizer_identifier=kwargs.get("tokenizer"),
        ),
        lifecycle=WorkloadLifecyclePolicy(
            scaling_threshold=0.75,
            shutdown_after_seconds=300,
            warmup_enabled=True,
            warmup_path="/health",
        ),
        readme=kwargs.get("readme", ""),
        public=kwargs.get("public", False),
        metadata=_metadata_with_hf_token(kwargs),
    )
