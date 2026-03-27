"""Demand signal collectors for Flux rebalancing decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class InferenceDemandSignal:
    """Aggregated inference demand for a miner node."""
    hotkey: str
    pending_requests: int = 0
    avg_queue_depth: float = 0.0
    sampled_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def score(self) -> float:
        return self.pending_requests + self.avg_queue_depth


@dataclass
class RentalDemandSignal:
    """Aggregated rental demand for a miner node."""
    hotkey: str
    pending_deployments: int = 0
    queued_users: int = 0
    sampled_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def score(self) -> float:
        return float(self.pending_deployments + self.queued_users)


class DemandCollector:
    """Collects and stores the latest demand signals per hotkey."""

    def __init__(self) -> None:
        self.inference: dict[str, InferenceDemandSignal] = {}
        self.rental: dict[str, RentalDemandSignal] = {}

    def update_inference(self, signal: InferenceDemandSignal) -> None:
        self.inference[signal.hotkey] = signal

    def update_rental(self, signal: RentalDemandSignal) -> None:
        self.rental[signal.hotkey] = signal

    def inference_score(self, hotkey: str) -> float:
        sig = self.inference.get(hotkey)
        return sig.score if sig else 0.0

    def rental_score(self, hotkey: str) -> float:
        sig = self.rental.get(hotkey)
        return sig.score if sig else 0.0
