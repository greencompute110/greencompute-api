"""Placement planning for distributed (multi-node) inference replicas.

A model too large for one chassis is served as ONE engine spanning several
nodes. This module decides WHICH nodes form a replica and what rank each takes.
It is pure — no chain, DB, or network — so the topology rules are unit-testable
without a cluster.

Three rules do the heavy lifting, and each exists because violating it produces
a replica that starts but serves terribly (or not at all):

1. **Same operator (hotkey).** Every node in a replica must belong to one
   miner. Two reasons: a usable inter-node link realistically only exists inside
   one operator's rack, and a replica spanning several hotkeys would break the
   per-hotkey scoring/emission model (who gets paid for a model that is 1/8th
   yours?). Keeping a replica within a hotkey keeps payouts unchanged.

2. **Homogeneous hardware.** All nodes must expose the same GPU model and VRAM.
   Pipeline parallelism runs at the speed of its slowest stage, and mismatched
   kernels/VRAM cause load failures or brutal stragglers.

3. **Interconnect floor.** Nodes must advertise at least the configured
   inter-node bandwidth (label `interconnect_gbps`). Distributed serving over a
   commodity link is worse than not serving at all, so we refuse rather than
   place a replica that will crawl.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from greencompute_protocol import (
    MultiNodeConfig,
    MultiNodeNodeAssignment,
    MultiNodePlan,
)

# Node label carrying the advertised inter-node link speed, in Gbps.
INTERCONNECT_LABEL = "interconnect_gbps"
# Node label grouping nodes that share a physical fabric (rack/DC). Nodes with
# different domains are never grouped, even under one hotkey — a miner may own
# boxes in several datacenters, and pipeline stages must not cross the WAN.
DOMAIN_LABEL = "interconnect_domain"


@dataclass(frozen=True)
class NodeCandidate:
    """A node Flux may use for a distributed replica."""

    hotkey: str
    node_id: str
    available_gpus: int
    vram_gb_per_gpu: int
    gpu_model: str
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def interconnect_gbps(self) -> float:
        try:
            return float(self.labels.get(INTERCONNECT_LABEL, 0.0))
        except (TypeError, ValueError):
            return 0.0

    @property
    def domain(self) -> str:
        """Fabric group. Falls back to the hotkey so an operator that hasn't
        labelled its boxes is treated as one domain (it is one operator) rather
        than silently unplaceable."""
        return str(self.labels.get(DOMAIN_LABEL) or self.hotkey)


def _eligible(node: NodeCandidate, config: MultiNodeConfig, min_vram_gb: int) -> bool:
    return (
        node.available_gpus >= config.gpus_per_node
        and node.vram_gb_per_gpu >= min_vram_gb
        and node.interconnect_gbps >= config.min_interconnect_gbps
    )


def _group_key(node: NodeCandidate) -> tuple[str, str, str, int]:
    """Nodes are only combinable when operator, fabric, and hardware all match."""
    return (node.hotkey, node.domain, node.gpu_model.lower(), node.vram_gb_per_gpu)


def plan_multi_node_placement(
    *,
    model_id: str,
    config: MultiNodeConfig,
    candidates: list[NodeCandidate],
    min_vram_gb: int = 1,
) -> MultiNodePlan | None:
    """Pick `config.node_count` nodes forming one distributed replica.

    Returns None when no group satisfies the rules — the caller must then leave
    the model unplaced rather than start a degraded partial replica.
    """
    if not config.is_distributed:
        return None  # single-node models go through the normal Flux path

    eligible = [n for n in candidates if _eligible(n, config, min_vram_gb)]

    groups: dict[tuple[str, str, str, int], list[NodeCandidate]] = {}
    for node in eligible:
        groups.setdefault(_group_key(node), []).append(node)

    viable = [g for g in groups.values() if len(g) >= config.node_count]
    if not viable:
        return None

    # Prefer the group with the most spare capacity (headroom for other work),
    # then the fastest fabric; ties broken deterministically by hotkey/domain so
    # repeated planning is stable.
    def group_rank(group: list[NodeCandidate]) -> tuple:
        return (
            -sum(n.available_gpus for n in group),
            -min(n.interconnect_gbps for n in group),
            group[0].hotkey,
            group[0].domain,
        )

    chosen_group = sorted(viable, key=group_rank)[0]

    # Within the group, take the nodes with the most headroom first; node_id
    # breaks ties so rank assignment is reproducible across rebalances.
    ordered = sorted(chosen_group, key=lambda n: (-n.available_gpus, n.node_id))
    selected = ordered[: config.node_count]

    assignments = [
        MultiNodeNodeAssignment(
            hotkey=node.hotkey,
            node_id=node.node_id,
            rank=rank,
            gpu_count=config.gpus_per_node,
        )
        for rank, node in enumerate(selected)
    ]

    return MultiNodePlan(
        model_id=model_id,
        assignments=assignments,
        tensor_parallel_size=config.effective_tensor_parallel_size,
        pipeline_parallel_size=config.effective_pipeline_parallel_size,
        backend=config.backend,
    )


# Node label carrying the address peers use to reach this box on the cluster
# fabric. Operators set it because the validator cannot infer a node's private
# cluster IP — the address it talks to a miner on is usually the public one,
# which is the wrong path for inter-rank traffic.
CLUSTER_ADDRESS_LABEL = "cluster_ip"


def head_address(node: NodeCandidate) -> str:
    """Address workers dial to join this node's Ray head."""
    return str(node.labels.get(CLUSTER_ADDRESS_LABEL) or "").strip()


def build_replica_rows(
    *,
    plan: MultiNodePlan,
    replica_id: str,
    head_host: str,
    gpus_per_node: int,
) -> list[dict]:
    """Turn a placement into the per-rank deployment payloads to persist.

    One row per rank, **head first** — workers need the head's address, so the
    head's row must exist (and its container be coming up) before the workers
    start dialling it. Each payload is what lands in `deployments.multi_node`
    and is read by the node-agent's launcher.
    """
    rows: list[dict] = []
    for assignment in sorted(plan.assignments, key=lambda a: a.rank):
        rows.append({
            "hotkey": assignment.hotkey,
            "node_id": assignment.node_id,
            "multi_node": {
                "replica_id": replica_id,
                "role": "head" if assignment.is_head else "worker",
                "rank": assignment.rank,
                "node_count": len(plan.assignments),
                "gpus_per_node": gpus_per_node,
                "tensor_parallel_size": plan.tensor_parallel_size,
                "pipeline_parallel_size": plan.pipeline_parallel_size,
                "head_host": head_host,
                "model_id": plan.model_id,
            },
        })
    return rows


def replica_is_ready(rank_states: list[str]) -> bool:
    """A distributed replica serves only when EVERY rank is up.

    Unlike a single-node replica, a partially-live distributed replica serves
    nothing — the head blocks waiting for the missing workers' GPUs. Treating it
    as ready would route traffic into a hang.
    """
    if not rank_states:
        return False
    return all(state == "ready" for state in rank_states)


def teardown_order(rows: list[dict]) -> list[dict]:
    """Workers first, head last.

    Killing the head first strands the workers in a cluster with no GCS; they
    linger holding GPUs until their own teardown lands. Draining workers before
    the head lets the Ray session close cleanly.
    """
    return sorted(
        rows,
        key=lambda r: 0 if (r.get("multi_node") or {}).get("role") == "worker" else 1,
    )


KEEP, REBUILD, CREATE = "keep", "rebuild", "create"
LIVE_STATES = {"ready", "starting", "scheduled", "provisioning", "pending"}


def replica_action(rank_rows: list[dict], node_count: int) -> str:
    """Decide what to do with a distributed replica's existing rank rows.

    * CREATE  — no ranks exist; plan and provision one.
    * KEEP    — every rank is present and live; leave it alone.
    * REBUILD — the replica is incomplete or has a dead rank. A distributed
      replica is all-or-nothing: one dead rank means the head is blocked
      forever waiting for GPUs that will never arrive, and the surviving ranks
      sit holding hardware. Tear the whole thing down so it can be re-planned
      (possibly onto different nodes) rather than trying to patch one rank back
      in — the Ray cluster can't absorb a replacement mid-flight anyway.
    """
    live = [r for r in rank_rows if r.get("state") in LIVE_STATES]
    if not live:
        return CREATE if not rank_rows else REBUILD
    if len(live) != len(rank_rows):
        return REBUILD  # some rank died while others live
    if len(live) != node_count:
        return REBUILD  # partial placement
    ranks = sorted((r.get("multi_node") or {}).get("rank") for r in live)
    if ranks != list(range(node_count)):
        return REBUILD  # duplicate or missing rank numbers
    return KEEP


def group_by_replica(rows: list[dict]) -> dict[str, list[dict]]:
    """Bucket rank rows by their replica_id."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        replica_id = (row.get("multi_node") or {}).get("replica_id")
        if replica_id:
            groups.setdefault(replica_id, []).append(row)
    return groups


def validate_topology(config: MultiNodeConfig) -> list[str]:
    """Return reasons the topology can't be served, empty if it's coherent.

    Catches the misconfigurations that would otherwise surface as an opaque
    vLLM/Ray crash minutes into a deployment.
    """
    problems: list[str] = []
    tp = config.effective_tensor_parallel_size
    pp = config.effective_pipeline_parallel_size

    if tp > config.gpus_per_node:
        problems.append(
            f"tensor_parallel_size {tp} exceeds gpus_per_node {config.gpus_per_node} — "
            "tensor parallelism cannot span nodes"
        )
    if config.gpus_per_node % tp != 0:
        problems.append(f"gpus_per_node {config.gpus_per_node} is not divisible by tensor_parallel_size {tp}")
    if pp != config.node_count:
        problems.append(
            f"pipeline_parallel_size {pp} must equal node_count {config.node_count} "
            "(one pipeline stage per node)"
        )
    if config.is_distributed and config.min_interconnect_gbps <= 0:
        problems.append(
            "min_interconnect_gbps is 0 for a distributed model — set a floor, "
            "cross-node serving over a commodity link will not perform"
        )
    if config.backend not in {"ray", "vllm-native"}:
        problems.append(f"unknown backend {config.backend!r}")
    return problems
