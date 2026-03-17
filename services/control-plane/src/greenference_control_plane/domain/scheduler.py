from __future__ import annotations

from dataclasses import dataclass

from greenference_protocol import LeaseAssignment, NodeCapability, WorkloadSpec


@dataclass(frozen=True)
class RankedNode:
    node: NodeCapability
    score: float


class PlacementPolicy:
    def rank_nodes(self, workload: WorkloadSpec, nodes: list[NodeCapability]) -> list[RankedNode]:
        candidates: list[RankedNode] = []
        for node in nodes:
            if workload.kind.value not in node.labels.get("workload_kinds", workload.kind.value):
                continue
            if node.available_gpus < workload.requirements.gpu_count:
                continue
            if node.vram_gb_per_gpu < workload.requirements.min_vram_gb_per_gpu:
                continue
            if node.cpu_cores < workload.requirements.cpu_cores:
                continue
            if node.memory_gb < workload.requirements.memory_gb:
                continue
            if (
                workload.requirements.supported_gpu_models
                and node.gpu_model not in workload.requirements.supported_gpu_models
            ):
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

    def assign_lease(self, workload: WorkloadSpec, deployment_id: str, nodes: list[NodeCapability]) -> LeaseAssignment | None:
        ranked = self.rank_nodes(workload, nodes)
        if not ranked:
            return None
        selected = ranked[0].node
        return LeaseAssignment(
            deployment_id=deployment_id,
            workload_id=workload.workload_id,
            hotkey=selected.hotkey,
            node_id=selected.node_id,
        )

