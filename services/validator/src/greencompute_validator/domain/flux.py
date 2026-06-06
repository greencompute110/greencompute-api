"""Flux orchestrator — dynamic GPU allocation engine.

Decides per-miner how GPUs should be split between inference and rental,
respecting reserve floors and never preempting active workloads. Also
decides **which** catalog models the inference-allocated GPUs run (the
Chutes-style shared inference pool).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from math import ceil, floor

from greencompute_protocol import (
    FluxRebalanceEvent,
    FluxState,
    GpuAllocationMode,
    ModelCatalogEntry,
)

logger = logging.getLogger(__name__)


class FluxOrchestrator:
    """Stateless rebalance engine — call ``rebalance()`` with current state + demand."""

    def __init__(
        self,
        inference_floor_pct: float = 0.20,
        rental_floor_pct: float = 0.10,
    ) -> None:
        self.inference_floor_pct = inference_floor_pct
        self.rental_floor_pct = rental_floor_pct

    # ------------------------------------------------------------------
    # Catalog → per-GPU assignment
    # ------------------------------------------------------------------

    @staticmethod
    def assign_catalog_models(
        *,
        inference_gpu_count: int,
        vram_gb_per_gpu: int,
        catalog: list[ModelCatalogEntry],
        replica_targets: dict[str, int] | None = None,
        pinned_model_ids: set[str] | None = None,
    ) -> dict[str, list[int]]:
        """Map inference-allocated GPU indices to catalog model_ids.

        Strategy (round 1 — simple; demand-reactive scaling lands in 2I):
          1. Skip entries whose `min_vram_gb_per_gpu` exceeds the miner's VRAM.
          2. Skip entries whose `gpu_count` exceeds what's left.
          3. If `replica_targets` is supplied (Phase 2I), prefer models with
             the biggest unfilled deficit. Otherwise round-robin.
          4. Pack GPUs contiguously; one model can claim multiple adjacent GPUs
             if its `gpu_count` > 1 (tensor-parallel models).

        Returns ``{model_id: [gpu_index, ...]}``. GPU indices are 0-based and
        contiguous starting at 0 (Flux itself assigns indices 0..inference-1
        to inference; rental takes inference..inference+rental-1; idle gets
        the tail — see `rebalance` below).
        """
        if inference_gpu_count <= 0 or not catalog:
            return {}

        eligible = [
            e for e in catalog
            if e.min_vram_gb_per_gpu <= vram_gb_per_gpu
            and e.gpu_count <= inference_gpu_count
        ]
        if not eligible:
            return {}

        targets = dict(replica_targets or {})
        eligible_by_id = {e.model_id: e for e in eligible}
        assignments: dict[str, list[int]] = {e.model_id: [] for e in eligible}
        cursor = 0

        # 1. Pin replicas already running on THIS node first, so a rebalance
        #    keeps an existing replica in place instead of tearing it down and
        #    re-creating it on whichever node the loop reaches first (churn +
        #    downtime). The caller passes the catalog model_ids this node is
        #    currently serving.
        for model_id in pinned_model_ids or set():
            e = eligible_by_id.get(model_id)
            if e is None or cursor + e.gpu_count > inference_gpu_count:
                continue
            assignments[model_id] = list(range(cursor, cursor + e.gpu_count))
            cursor += e.gpu_count

        def _deficit(e: ModelCatalogEntry) -> int:
            have = len(assignments[e.model_id]) // max(1, e.gpu_count)
            return targets.get(e.model_id, 0) - have

        # 2. Fill remaining inference GPUs toward the (fleet-wide remaining)
        #    target. When targets are supplied we STOP once every model's
        #    deficit is met — previously this packed every inference GPU on
        #    every node, so N nodes each spun up a replica of a target-1 model
        #    (the phantom over-count). Without targets, keep the legacy fill.
        rotate_idx = 0
        while cursor < inference_gpu_count:
            # Choose next candidate: prefer highest remaining deficit if
            # replica_targets passed; otherwise rotate through eligible list.
            if targets:
                candidates = sorted(eligible, key=lambda e: (-_deficit(e), e.model_id))
                candidate = next(
                    (
                        c for c in candidates
                        if c.gpu_count <= inference_gpu_count - cursor and _deficit(c) > 0
                    ),
                    None,
                )
            else:
                candidate = None
                for _ in range(len(eligible)):
                    c = eligible[rotate_idx % len(eligible)]
                    rotate_idx += 1
                    if c.gpu_count <= inference_gpu_count - cursor:
                        candidate = c
                        break
            if candidate is None:
                break  # no eligible model fits in the remaining slots
            block = list(range(cursor, cursor + candidate.gpu_count))
            assignments[candidate.model_id].extend(block)
            cursor += candidate.gpu_count
        # Drop models that ended up with zero GPUs to keep the output tight.
        return {k: v for k, v in assignments.items() if v}

    def rebalance(
        self,
        state: FluxState,
        *,
        catalog: list[ModelCatalogEntry] | None = None,
        vram_gb_per_gpu: int | None = None,
        replica_targets: dict[str, int] | None = None,
        pinned_model_ids: set[str] | None = None,
    ) -> tuple[FluxState, list[FluxRebalanceEvent]]:
        """Compute optimal allocation and return updated state + audit events.

        Only idle GPUs may be reassigned — active inference/rental GPUs are untouched.
        """
        total = state.total_gpus
        if total == 0:
            return state, []

        # Hard floors
        inference_floor = max(1, ceil(total * self.inference_floor_pct))
        rental_floor = max(1, ceil(total * self.rental_floor_pct))
        flex_pool = max(0, total - inference_floor - rental_floor)

        # Current allocation is binding for non-idle GPUs
        locked_inference = min(state.inference_gpus, total)
        locked_rental = min(state.rental_gpus, total)

        # Allocate flex pool based on demand scores
        inference_demand = state.inference_demand_score
        rental_demand = state.rental_demand_score
        total_demand = inference_demand + rental_demand

        if total_demand > 0:
            flex_to_inference = floor(flex_pool * (inference_demand / total_demand))
        else:
            flex_to_inference = flex_pool // 2  # split evenly when no signal
        flex_to_rental = flex_pool - flex_to_inference

        target_inference = inference_floor + flex_to_inference
        target_rental = rental_floor + flex_to_rental

        # Only idle GPUs can transition — compute how many we CAN move
        idle = state.idle_gpus
        events: list[FluxRebalanceEvent] = []
        now = datetime.now(UTC)

        new_inference = locked_inference
        new_rental = locked_rental

        # Try to fill inference shortfall from idle
        inference_need = max(0, target_inference - locked_inference)
        inference_add = min(inference_need, idle)
        for i in range(inference_add):
            events.append(FluxRebalanceEvent(
                hotkey=state.hotkey,
                node_id=state.node_id,
                gpu_index=locked_inference + i,
                from_mode=GpuAllocationMode.IDLE,
                to_mode=GpuAllocationMode.INFERENCE,
                reason=f"flux_rebalance: demand={inference_demand:.2f}",
                created_at=now,
            ))
        new_inference += inference_add
        idle -= inference_add

        # Try to fill rental shortfall from remaining idle
        rental_need = max(0, target_rental - locked_rental)
        rental_add = min(rental_need, idle)
        for i in range(rental_add):
            events.append(FluxRebalanceEvent(
                hotkey=state.hotkey,
                node_id=state.node_id,
                gpu_index=locked_rental + i,
                from_mode=GpuAllocationMode.IDLE,
                to_mode=GpuAllocationMode.RENTAL,
                reason=f"flux_rebalance: demand={rental_demand:.2f}",
                created_at=now,
            ))
        new_rental += rental_add
        idle -= rental_add

        # Assign catalog models to the inference GPU block. Mirrors the
        # GPU index layout used above: indices 0 .. new_inference-1 are
        # inference; the assignment dict maps model_id → subset of those.
        new_assignments: dict[str, list[int]] = {}
        if catalog and new_inference > 0 and vram_gb_per_gpu is not None:
            new_assignments = self.assign_catalog_models(
                inference_gpu_count=new_inference,
                vram_gb_per_gpu=vram_gb_per_gpu,
                catalog=catalog,
                replica_targets=replica_targets,
                pinned_model_ids=pinned_model_ids,
            )

        # Backfill model_id on inference-transition events using the new
        # assignment map (GPU index → model_id).
        index_to_model: dict[int, str] = {}
        for m_id, idxs in new_assignments.items():
            for i in idxs:
                index_to_model[i] = m_id
        for evt in events:
            if evt.to_mode == GpuAllocationMode.INFERENCE and evt.gpu_index in index_to_model:
                evt.model_id = index_to_model[evt.gpu_index]

        new_state = state.model_copy(update={
            "inference_gpus": new_inference,
            "rental_gpus": new_rental,
            "idle_gpus": idle,
            "inference_assignments": new_assignments,
            "last_rebalanced_at": now,
        })

        if events:
            logger.info(
                "flux rebalance %s: inf=%d→%d rental=%d→%d idle=%d→%d (%d events, %d models)",
                state.hotkey,
                state.inference_gpus, new_inference,
                state.rental_gpus, new_rental,
                state.idle_gpus, idle,
                len(events),
                len(new_assignments),
            )

        return new_state, events
