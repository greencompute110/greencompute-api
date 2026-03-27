"""Wait-time estimator for rental requests when GPUs are busy with inference."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from greenference_protocol import FluxState, RentalWaitEstimate

logger = logging.getLogger(__name__)

# Conservative defaults when no telemetry is available
DEFAULT_INFERENCE_DURATION_SECONDS = 120.0
MEDIAN_THROUGHPUT_TOKENS_PER_SEC = 50.0


class WaitEstimator:
    """Estimates how long a rental requester must wait for a GPU to become free."""

    def __init__(self) -> None:
        # Historical median completion times per hotkey
        self._completion_medians: dict[str, float] = {}
        # Queue positions per deployment
        self._queue: dict[str, int] = {}
        self._next_position: int = 1

    def record_completion(self, hotkey: str, duration_seconds: float) -> None:
        """Track inference completion times to improve estimates."""
        prev = self._completion_medians.get(hotkey, DEFAULT_INFERENCE_DURATION_SECONDS)
        # Exponential moving average (alpha=0.3) for responsiveness
        self._completion_medians[hotkey] = prev * 0.7 + duration_seconds * 0.3

    def enqueue(self, deployment_id: str) -> int:
        """Add a rental deployment to the wait queue, return its position."""
        if deployment_id not in self._queue:
            self._queue[deployment_id] = self._next_position
            self._next_position += 1
        return self._queue[deployment_id]

    def dequeue(self, deployment_id: str) -> None:
        """Remove a deployment from the wait queue (placed or cancelled)."""
        self._queue.pop(deployment_id, None)

    def estimate(
        self,
        deployment_id: str,
        flux_state: FluxState,
    ) -> RentalWaitEstimate:
        """Return estimated wait time in seconds until a GPU is free for rental."""
        position = self._queue.get(deployment_id, 0)
        median_duration = self._completion_medians.get(
            flux_state.hotkey, DEFAULT_INFERENCE_DURATION_SECONDS
        )

        # If there are idle GPUs, wait is zero
        if flux_state.idle_gpus > 0:
            return RentalWaitEstimate(
                deployment_id=deployment_id,
                estimated_wait_seconds=0.0,
                position_in_queue=position,
                created_at=datetime.now(UTC),
            )

        # Estimate: each inference GPU finishes in ~median_duration.
        # With N inference GPUs, one finishes every median_duration/N seconds.
        inference_gpus = max(flux_state.inference_gpus, 1)
        gpu_free_interval = median_duration / inference_gpus

        # Wait scales with queue position
        estimated = gpu_free_interval * max(position, 1)

        return RentalWaitEstimate(
            deployment_id=deployment_id,
            estimated_wait_seconds=round(estimated, 1),
            gpu_currently_serving=f"{inference_gpus} inference GPU(s)",
            position_in_queue=position,
            created_at=datetime.now(UTC),
        )
