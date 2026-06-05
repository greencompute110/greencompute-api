"""Flux catalog assignment — pin existing replicas + respect the fleet-wide
replica target.

Regression guard for the phantom over-count: ``assign_catalog_models`` runs
per node, and each node used to fill *every* inference GPU toward the same
fleet-wide target, so N nodes each spun up a replica of a target-1 model
(dashboard showed running=3 for a single real replica). The fix pins the
models a node already serves (no churn) and stops once the remaining deficit
is met.
"""
from greencompute_protocol import ModelCatalogEntry
from greencompute_validator.domain.flux import FluxOrchestrator

VL = "qwen2-vl-7b-instruct"
TXT = "qwen2.5-7b-instruct"


def _cat():
    return [
        ModelCatalogEntry(model_id=VL, min_vram_gb_per_gpu=24, gpu_count=1),
        ModelCatalogEntry(model_id=TXT, min_vram_gb_per_gpu=24, gpu_count=1),
    ]


def test_pinned_model_stays_even_with_zero_deficit():
    # A node already serving VL keeps it (no churn) even though the fleet-wide
    # remaining target is 0.
    out = FluxOrchestrator.assign_catalog_models(
        inference_gpu_count=2,
        vram_gb_per_gpu=24,
        catalog=_cat(),
        replica_targets={VL: 0, TXT: 0},
        pinned_model_ids={VL},
    )
    assert list(out) == [VL]


def test_stops_at_target_instead_of_filling_every_gpu():
    # 8 inference GPUs, target 1 each, nothing pinned -> exactly 2 GPUs used,
    # NOT all 8 (the old behaviour packed a replica onto every GPU).
    out = FluxOrchestrator.assign_catalog_models(
        inference_gpu_count=8,
        vram_gb_per_gpu=24,
        catalog=_cat(),
        replica_targets={VL: 1, TXT: 1},
        pinned_model_ids=set(),
    )
    assert sum(len(v) for v in out.values()) == 2


def test_no_deficit_no_pin_assigns_nothing():
    # Siblings already meet the fleet target (remaining 0) and this node runs
    # nothing -> it must not place a duplicate replica.
    out = FluxOrchestrator.assign_catalog_models(
        inference_gpu_count=8,
        vram_gb_per_gpu=24,
        catalog=_cat(),
        replica_targets={VL: 0, TXT: 0},
        pinned_model_ids=set(),
    )
    assert out == {}


def test_legacy_fill_without_targets():
    # Back-compat: no replica_targets -> round-robin fill of all GPUs.
    out = FluxOrchestrator.assign_catalog_models(
        inference_gpu_count=4,
        vram_gb_per_gpu=24,
        catalog=_cat(),
        replica_targets=None,
        pinned_model_ids=set(),
    )
    assert sum(len(v) for v in out.values()) == 4


def test_vram_filter_excludes_models():
    out = FluxOrchestrator.assign_catalog_models(
        inference_gpu_count=1,
        vram_gb_per_gpu=16,
        catalog=_cat(),
        replica_targets={VL: 1},
        pinned_model_ids=set(),
    )
    assert out == {}
