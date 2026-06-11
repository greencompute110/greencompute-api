from __future__ import annotations

import logging
from dataclasses import dataclass

from greencompute_protocol import LeaseAssignment, NodeCapability, WorkloadSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RankedNode:
    node: NodeCapability
    score: float


def effective_available_gpus(node: NodeCapability, reserved_by_node: dict[str, int] | None) -> int:
    """ORCH-C2: GPUs the scheduler may actually hand out on a node.

    The miner-reported `available_gpus` is treated only as a PHYSICAL ceiling.
    The authoritative floor is `gpu_count − sum(active reservations)` so a stale
    or clobbering capacity heartbeat can never resurrect GPUs the control-plane
    has already committed. We take the stricter (min) of the two views so the
    scheduler is always conservative and never double-spends a physical GPU,
    regardless of heartbeat timing. Reservation accounting starts at assign and
    is reconstructed on every read; it is never erased by a heartbeat.
    """
    reserved = (reserved_by_node or {}).get(node.node_id, 0)
    capacity_floor = node.gpu_count - reserved
    return min(node.available_gpus, capacity_floor)


class PlacementPolicy:
    def rank_nodes(
        self,
        workload: WorkloadSpec,
        nodes: list[NodeCapability],
        reserved_by_node: dict[str, int] | None = None,
    ) -> list[RankedNode]:
        candidates: list[RankedNode] = []
        for node in nodes:
            # Exact-token membership against the comma-separated label
            # (labels values are strings). The old check was a no-op twice
            # over: nodes never set the label, so the .get default made the
            # test `kind in kind` (always true), and `in` on a string is a
            # SUBSTRING test ('pod' matches inside 'tripod'). A node that
            # doesn't declare workload_kinds accepts everything — legacy
            # node-agents don't report the label, so absent ≠ inference-only.
            kinds_label = node.labels.get("workload_kinds", "")
            allowed_kinds = {k.strip().lower() for k in kinds_label.split(",") if k.strip()}
            if allowed_kinds and workload.kind.value not in allowed_kinds:
                logger.debug("node %s: skip workload_kinds mismatch (need %s, has %s)",
                             node.node_id, workload.kind.value, kinds_label)
                continue
            available = effective_available_gpus(node, reserved_by_node)
            if available < workload.requirements.gpu_count:
                logger.debug("node %s: skip gpu_count (need %d, effective %d, reported %d, reserved %d)",
                             node.node_id, workload.requirements.gpu_count, available,
                             node.available_gpus, (reserved_by_node or {}).get(node.node_id, 0))
                continue
            if node.vram_gb_per_gpu < workload.requirements.min_vram_gb_per_gpu:
                logger.debug("node %s: skip vram (need %d, has %d)",
                             node.node_id, workload.requirements.min_vram_gb_per_gpu, node.vram_gb_per_gpu)
                continue
            if node.cpu_cores < workload.requirements.cpu_cores:
                logger.debug("node %s: skip cpu (need %d, has %d)",
                             node.node_id, workload.requirements.cpu_cores, node.cpu_cores)
                continue
            if node.memory_gb < workload.requirements.memory_gb:
                logger.debug("node %s: skip memory (need %d, has %d)",
                             node.node_id, workload.requirements.memory_gb, node.memory_gb)
                continue
            if (
                workload.requirements.supported_gpu_models
                and node.gpu_model not in workload.requirements.supported_gpu_models
            ):
                logger.debug("node %s: skip gpu_model (need %s, has %s)",
                             node.node_id, workload.requirements.supported_gpu_models, node.gpu_model)
                continue
            cost_component = 1.0 / (1.0 + node.hourly_cost_usd)
            score = (
                node.health_score * 0.4
                + node.reliability_score * 0.3
                + node.performance_score * 0.2
                + cost_component * 0.1
            )
            candidates.append(RankedNode(node=node, score=score))
        return sorted(candidates, key=lambda item: item.score, reverse=True)

    def assign_lease(
        self,
        workload: WorkloadSpec,
        deployment_id: str,
        nodes: list[NodeCapability],
        reserved_by_node: dict[str, int] | None = None,
    ) -> LeaseAssignment | None:
        ranked = self.rank_nodes(workload, nodes, reserved_by_node=reserved_by_node)
        if not ranked:
            return None
        selected = ranked[0].node
        return LeaseAssignment(
            deployment_id=deployment_id,
            workload_id=workload.workload_id,
            hotkey=selected.hotkey,
            node_id=selected.node_id,
        )
