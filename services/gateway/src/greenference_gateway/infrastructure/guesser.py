"""HF model config guesser for VLLM GPU requirements."""

from __future__ import annotations

import math
from typing import Any
from urllib import request
import json


class GPURequirements:
    def __init__(
        self,
        total_model_size: int,
        required_gpus: int,
        min_vram_per_gpu: int,
        model_type: str,
        quantization: str | None,
        num_attention_heads: int,
        num_key_value_heads: int | None,
        hidden_size: int,
        num_layers: int,
    ):
        self.total_model_size = total_model_size
        self.required_gpus = required_gpus
        self.min_vram_per_gpu = min_vram_per_gpu
        self.model_type = model_type
        self.quantization = quantization
        self.num_attention_heads = num_attention_heads
        self.num_key_value_heads = num_key_value_heads
        self.hidden_size = hidden_size
        self.num_layers = num_layers

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_model_size": self.total_model_size,
            "required_gpus": self.required_gpus,
            "min_vram_per_gpu": self.min_vram_per_gpu,
            "model_type": self.model_type,
            "quantization": self.quantization,
            "num_attention_heads": self.num_attention_heads,
            "num_key_value_heads": self.num_key_value_heads,
            "hidden_size": self.hidden_size,
            "num_layers": self.num_layers,
        }


AVAILABLE_VRAM = [8, 12, 16, 24, 40, 48, 80, 80, 120]
VRAM_OVERHEAD = {"llama": 1.4, "mistral": 1.4, "qwen": 1.4, "deepseek": 1.5, "default": 1.4}
QUANT_MULTIPLIERS = {"4bit": 0.25, "8bit": 0.5, "fp8": 0.5, "none": 1.0}


def _cfg(config: dict, key: str, default: int = 0) -> int:
    val = config.get(key)
    if val is None and "text_config" in config:
        val = config["text_config"].get(key)
    return val if val is not None else default


def _detect_model_type(config: dict) -> str:
    model_type = config.get("model_type", "").lower()
    for arch in VRAM_OVERHEAD:
        if arch in model_type:
            return arch
    return "default"


def _detect_quantization(config: dict) -> str | None:
    if "quantization_config" in config:
        qc = config["quantization_config"]
        bits = qc.get("bits")
        if bits == 4:
            return "4bit"
        if bits == 8:
            return "8bit"
    return "none"


def analyze_model(model_name: str) -> GPURequirements:
    """Fetch config from HuggingFace and compute GPU requirements."""
    if "/" not in model_name:
        raise ValueError("model must be org/model format")
    url = f"https://huggingface.co/{model_name}/raw/main/config.json"
    try:
        with request.urlopen(url, timeout=10) as resp:  # noqa: S310
            config = json.loads(resp.read().decode())
    except Exception as e:
        raise ValueError(f"failed to fetch config: {e}") from e

    model_type = _detect_model_type(config)
    quantization = _detect_quantization(config)
    num_attention_heads = _cfg(config, "num_attention_heads", 32)
    num_key_value_heads = _cfg(config, "num_key_value_heads")
    hidden_size = _cfg(config, "hidden_size", 4096)
    num_layers = _cfg(config, "num_hidden_layers", 32)

    vocab_size = _cfg(config, "vocab_size", 32000)
    param_size = (
        num_layers
        * (
            4 * hidden_size * hidden_size
            + 8 * hidden_size * (num_attention_heads + (num_key_value_heads or num_attention_heads))
        )
        + 2 * hidden_size * vocab_size
    )
    bytes_per_param = 2
    total_size_gb = (param_size * bytes_per_param) / (1024**3)
    overhead = VRAM_OVERHEAD.get(model_type, VRAM_OVERHEAD["default"])
    quant_mult = QUANT_MULTIPLIERS.get(quantization or "none", 1.0)
    total_vram_gb = total_size_gb * overhead * quant_mult

    best_gpus = 8
    best_vram = 80
    for gpu_count in range(1, 9):
        if num_attention_heads % gpu_count != 0 or hidden_size % gpu_count != 0:
            continue
        vram_per_gpu = math.ceil(total_vram_gb / gpu_count)
        for vram in AVAILABLE_VRAM:
            if vram >= vram_per_gpu:
                if gpu_count < best_gpus or (gpu_count == best_gpus and vram < best_vram):
                    best_gpus = gpu_count
                    best_vram = vram
                break

    return GPURequirements(
        total_model_size=int(total_size_gb * 1024 * 1024 * 1024),
        required_gpus=best_gpus,
        min_vram_per_gpu=best_vram,
        model_type=model_type,
        quantization=quantization,
        num_attention_heads=num_attention_heads,
        num_key_value_heads=num_key_value_heads if num_key_value_heads else None,
        hidden_size=hidden_size,
        num_layers=num_layers,
    )
