"""Deterministic conformance check for a provider application's DECLARED node
specs against the enterprise requirement:

    8x RTX 4090 OR 8x RTX 5090 per node, dual enterprise CPUs, >=512 GB RAM,
    >=8 TB storage per node, 10 Gb symmetric connectivity.

This is Phase 1 of automated onboarding: a CHEAP, deterministic PRE-FILTER over
the free-text form fields. It is intentionally CONSERVATIVE — it only returns
"fail" when it can confidently parse an out-of-spec value, and "unclear" when
the free text can't be parsed, so a legitimate applicant who phrases things
oddly is NEVER auto-rejected (those route to a human). It does NOT prove the
hardware exists — that's the job of the live hardware attestation (later
phase). Here it just flags declared specs that plainly don't meet the bar and
gives the admin review queue a deterministic in-spec/flagged signal.
"""
from __future__ import annotations

import re
from typing import Any

REQUIRED_GPU_MODELS = ("4090", "5090")
REQUIRED_GPUS_PER_NODE = 8
REQUIRED_RAM_GB = 512
REQUIRED_STORAGE_TB = 8
REQUIRED_NET_GBPS = 10.0

SPEC_SUMMARY = (
    "8x RTX 4090 or 8x RTX 5090 per node, dual enterprise CPUs, "
    ">=512GB RAM, >=8TB storage/node, 10Gb symmetric connectivity"
)

PASS, FAIL, UNCLEAR = "pass", "fail", "unclear"

_NUM = re.compile(r"(\d+(?:\.\d+)?)")


def _first_number(text: str) -> float | None:
    m = _NUM.search(text or "")
    return float(m.group(1)) if m else None


def _to_gb(text: str) -> float | None:
    """Parse a memory/storage string to GiB-ish gigabytes. Handles TB/TiB, GB,
    and bare numbers assumed GB. Returns None if unparseable."""
    if not text:
        return None
    t = text.lower()
    n = _first_number(t)
    if n is None:
        return None
    if "tb" in t or "tib" in t:
        return n * 1024
    return n  # "gb", "gib", or bare → treat as GB


def _to_gbps(text: str) -> float | None:
    """Parse a link-speed string to Gbps. '10 Gbps' -> 10, '1000 Mbps' -> 1.
    Bare number → unclear (unit unknown) → None."""
    if not text:
        return None
    t = text.lower()
    n = _first_number(t)
    if n is None:
        return None
    if "gb" in t or "gbps" in t or "gbit" in t or "gig" in t:
        return n
    if "mb" in t or "mbps" in t or "mbit" in t:
        return n / 1000.0
    return None  # unit unknown — don't guess


def _gpu_check(node: dict) -> tuple[str, str | None]:
    blob = " ".join(str(node.get(k, "")) for k in ("gpu", "chassis", "cpu")).lower()
    model = next((m for m in REQUIRED_GPU_MODELS if m in blob), None)
    # Count: prefer an explicit "8x"/"x8" in the GPU text, else the quantity
    # field if it's a plain integer.
    count: int | None = None
    m = re.search(r"(\d+)\s*[x×]", blob) or re.search(r"[x×]\s*(\d+)", blob)
    if m:
        count = int(m.group(1))
    elif str(node.get("quantity", "")).strip().isdigit():
        count = int(str(node.get("quantity")).strip())

    if model is None:
        # A recognizable-but-wrong GPU (e.g. 3090/A100/H100) is a confident fail;
        # otherwise we can't tell.
        if re.search(r"\b(3090|3080|2080|1080|a100|h100|v100|a40|a6000|l40|t4|p100|titan)\b", blob):
            return FAIL, "GPU is not an RTX 4090/5090"
        return UNCLEAR, "GPU model not recognized"
    if count is not None and count != REQUIRED_GPUS_PER_NODE:
        return FAIL, f"declares {count} GPUs/node, need {REQUIRED_GPUS_PER_NODE}"
    if count is None:
        return UNCLEAR, f"RTX {model} found but GPU count/node unclear"
    return PASS, None


def _cpu_check(node: dict) -> tuple[str, str | None]:
    t = str(node.get("cpu", "")).lower()
    if not t.strip():
        return UNCLEAR, "CPU not described"
    if re.search(r"\bdual\b|\b2\s*[x×]\b|[x×]\s*2\b|\btwo\b|2\s*socket|dual[- ]?socket", t):
        return PASS, None
    if re.search(r"\bsingle\b|\b1\s*[x×]\b|single[- ]?socket|1\s*socket", t):
        return FAIL, "single-CPU node, need dual enterprise CPUs"
    return UNCLEAR, "CPU count unclear (need dual)"


def _ram_check(node: dict) -> tuple[str, str | None]:
    gb = _to_gb(str(node.get("ram", "")))
    if gb is None:
        return UNCLEAR, "RAM not parseable"
    return (PASS, None) if gb >= REQUIRED_RAM_GB else (FAIL, f"{gb:g}GB RAM < {REQUIRED_RAM_GB}GB")


def _storage_check(node: dict) -> tuple[str, str | None]:
    gb = _to_gb(str(node.get("storage", "")))
    if gb is None:
        return UNCLEAR, "storage not parseable"
    tb = gb / 1024
    return (PASS, None) if tb >= REQUIRED_STORAGE_TB else (FAIL, f"{tb:g}TB storage < {REQUIRED_STORAGE_TB}TB")


def _network_check(details: dict) -> tuple[str, str | None]:
    infra = details.get("infrastructure") or {}
    up = _to_gbps(str(infra.get("upload_speed", "")))
    down = _to_gbps(str(infra.get("download_speed", "")))
    if up is None or down is None:
        return UNCLEAR, "up/down link speed not parseable"
    if up < REQUIRED_NET_GBPS or down < REQUIRED_NET_GBPS:
        return FAIL, f"{up:g}/{down:g} Gbps < {REQUIRED_NET_GBPS:g} Gbps symmetric"
    return PASS, None


def evaluate_provider_specs(details: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic spec-conformance assessment of a provider
    application's declared specs. Never raises — malformed input degrades to
    'unclear'. Shape:
      {"conformant": true|false|null, "checks": {gpu,cpu,ram,storage,network},
       "flags": [...], "spec": SPEC_SUMMARY}
    conformant: True = every check passed; False = at least one confident fail;
    None = nothing failed but something is unclear (route to human)."""
    nodes = details.get("nodes") if isinstance(details, dict) else None
    checks: dict[str, str] = {}
    flags: list[str] = []

    if not isinstance(nodes, list) or not nodes:
        checks = {k: UNCLEAR for k in ("gpu", "cpu", "ram", "storage")}
        flags.append("no node specs declared")
    else:
        # Aggregate per-node checks: the node dimension is FAIL if any node
        # fails, PASS only if all pass, else UNCLEAR.
        for dim, fn in (("gpu", _gpu_check), ("cpu", _cpu_check), ("ram", _ram_check), ("storage", _storage_check)):
            verdicts = []
            for i, node in enumerate(nodes):
                node = node if isinstance(node, dict) else {}
                verdict, reason = fn(node)
                verdicts.append(verdict)
                if reason and verdict == FAIL:
                    flags.append(f"node {i + 1}: {reason}")
                elif reason and verdict == UNCLEAR:
                    flags.append(f"node {i + 1}: {reason} (unclear)")
            checks[dim] = FAIL if FAIL in verdicts else (UNCLEAR if UNCLEAR in verdicts else PASS)

    net_verdict, net_reason = _network_check(details if isinstance(details, dict) else {})
    checks["network"] = net_verdict
    if net_reason:
        flags.append(f"network: {net_reason}{'' if net_verdict == FAIL else ' (unclear)'}")

    values = set(checks.values())
    conformant: bool | None = False if FAIL in values else (None if UNCLEAR in values else True)

    return {"conformant": conformant, "checks": checks, "flags": flags, "spec": SPEC_SUMMARY}
