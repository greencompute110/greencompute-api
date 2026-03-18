"""Workload templates (VLLM, diffusion) for Greenference."""

from __future__ import annotations

from greenference_protocol import (
    InferenceRuntimeConfig,
    WorkloadCreateRequest,
    WorkloadRequirements,
    WorkloadLifecyclePolicy,
)
from greenference_protocol.enums import WorkloadKind, SecurityTier


def build_vllm_workload(
    model: str,
    name: str | None = None,
    image: str = "vllm/vllm-openai:latest",
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
        public=kwargs.get("public", True),
    )


def build_diffusion_workload(
    model: str,
    name: str,
    image: str = "ghcr.io/greenference/diffusion:latest",
    concurrency: int = 1,
    **kwargs: object,
) -> WorkloadCreateRequest:
    """Build a diffusion workload spec from model id."""
    return WorkloadCreateRequest(
        name=name,
        image=image,
        kind=WorkloadKind.INFERENCE,
        security_tier=SecurityTier.STANDARD,
        requirements=WorkloadRequirements(
            gpu_count=1,
            min_vram_gb_per_gpu=12,
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
            warmup_enabled=False,
        ),
        readme=kwargs.get("readme", ""),
        public=kwargs.get("public", True),
    )
