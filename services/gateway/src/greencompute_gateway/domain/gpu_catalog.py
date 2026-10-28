"""The GPUs a customer can rent, and how to compare GPU names.

Shared by the routes (what we advertise) and the service (what we accept), so
the two can't drift apart again.
"""
from __future__ import annotations

import os


def normalize_gpu_model(raw: str | None) -> str:
    """Lower-case, separators stripped: "RTX 4090" == "rtx-4090" == "rtx4090".

    Must agree with `greencompute_protocol.billing_rates._normalize_gpu_model`
    and the control-plane scheduler, or a GPU can be priced but never placed.
    """
    return "".join(ch for ch in (raw or "").lower() if ch.isalnum())


def public_gpu_models() -> list[str]:
    """GPU models a customer can actually rent, in the canonical spelling nodes
    report ("rtx4090", not "rtx-4090").

    Derived from the billing rate table so the list can never advertise a card
    we don't price, and filtered by the same public-family allowlist the
    control-plane uses, so internal-only hardware (the A4000 test box) stays
    hidden. The hard-coded list this replaces advertised A100/H100/T4/K80 we
    have never had, omitted the RTX 5090 we do have, and spelled the 4090 with a
    hyphen the scheduler then failed to match.
    """
    from greencompute_protocol import GPU_RATE_CENTS_PER_HOUR

    families = [
        f.strip().lower()
        for f in os.getenv("GREENCOMPUTE_PUBLIC_RENTAL_GPU_FAMILIES", "4090,5090").split(",")
        if f.strip()
    ]
    models = sorted(GPU_RATE_CENTS_PER_HOUR)
    if families:
        models = [m for m in models if any(f in m for f in families)]
    return models


def matches_public_gpu(requested: list[str]) -> bool:
    """True if at least one requested model is rentable (any spelling)."""
    wanted = {normalize_gpu_model(m) for m in requested}
    return bool(wanted & {normalize_gpu_model(m) for m in public_gpu_models()})
