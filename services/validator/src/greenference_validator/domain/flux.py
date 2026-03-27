"""Flux orchestrator — dynamic GPU allocation engine.

Decides per-miner how GPUs should be split between inference and rental,
respecting reserve floors and never preempting active workloads.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from math import ceil, floor

from greenference_protocol import (
    FluxRebalanceEvent,
    FluxState,
    GpuAllocationMode,
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

    def rebalance(self, state: FluxState) -> tuple[FluxState, list[FluxRebalanceEvent]]:
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

        new_state = state.model_copy(update={
            "inference_gpus": new_inference,
            "rental_gpus": new_rental,
            "idle_gpus": idle,
            "last_rebalanced_at": now,
        })

        if events:
            logger.info(
                "flux rebalance %s: inf=%d→%d rental=%d→%d idle=%d→%d (%d events)",
                state.hotkey,
                state.inference_gpus, new_inference,
                state.rental_gpus, new_rental,
                state.idle_gpus, idle,
                len(events),
            )

        return new_state, events
