"""Distributed-replica placement: which nodes may be combined into one engine.

Phase 1 of the multi-node inference build (see docs/adr/multi-node-inference.md).
The rules under test exist because breaking them yields a replica that starts
but serves terribly: mixing operators breaks payouts, mixing hardware creates
stragglers, and a slow fabric makes cross-node serving pointless.
"""
from greencompute_protocol import MultiNodeConfig
from greencompute_validator.domain.multinode import (
    NodeCandidate,
    build_replica_rows,
    head_address,
    plan_multi_node_placement,
    replica_is_ready,
    teardown_order,
    validate_topology,
)


def node(node_id, *, hotkey="5MINER_A", gpus=8, vram=32, model="rtx5090",
         gbps=100.0, domain="rack-1"):
    return NodeCandidate(
        hotkey=hotkey,
        node_id=node_id,
        available_gpus=gpus,
        vram_gb_per_gpu=vram,
        gpu_model=model,
        labels={"interconnect_gbps": str(gbps), "interconnect_domain": domain},
    )


def cfg(**over):
    base = dict(node_count=4, gpus_per_node=8, min_interconnect_gbps=100.0)
    base.update(over)
    return MultiNodeConfig(**base)


def plan(candidates, config=None, **kw):
    return plan_multi_node_placement(
        model_id="kimi-k3", config=config or cfg(), candidates=candidates, **kw
    )


# --- happy path --------------------------------------------------------------


def test_plans_a_replica_across_matching_nodes():
    p = plan([node(f"n{i}") for i in range(4)])
    assert p is not None
    assert len(p.assignments) == 4
    assert p.total_gpus == 32
    assert [a.rank for a in p.assignments] == [0, 1, 2, 3]
    assert p.head.rank == 0 and p.head.is_head


def test_derives_tp_within_node_and_pp_across_nodes():
    p = plan([node(f"n{i}") for i in range(4)])
    assert p.tensor_parallel_size == 8  # inside each chassis
    assert p.pipeline_parallel_size == 4  # one stage per node


def test_uses_only_the_nodes_it_needs():
    p = plan([node(f"n{i}") for i in range(10)])
    assert len(p.assignments) == 4


def test_planning_is_deterministic():
    nodes = [node(f"n{i}") for i in range(6)]
    assert plan(nodes).model_dump() == plan(list(reversed(nodes))).model_dump()


# --- the three grouping rules ------------------------------------------------


def test_never_spans_two_operators():
    # Four nodes, but split across two miners — a replica may not straddle them.
    nodes = [node("a1"), node("a2"), node("b1", hotkey="5MINER_B"), node("b2", hotkey="5MINER_B")]
    assert plan(nodes) is None


def test_never_spans_two_fabric_domains():
    # One operator, but boxes in two datacenters — pipeline stages can't cross a WAN.
    nodes = [node("a1"), node("a2"), node("b1", domain="rack-2"), node("b2", domain="rack-2")]
    assert plan(nodes) is None


def test_never_mixes_gpu_models():
    nodes = [node("a1"), node("a2"), node("b1", model="rtx4090"), node("b2", model="rtx4090")]
    assert plan(nodes) is None


def test_never_mixes_vram():
    nodes = [node("a1"), node("a2"), node("b1", vram=24), node("b2", vram=24)]
    assert plan(nodes) is None


def test_rejects_nodes_below_the_interconnect_floor():
    # Commodity ethernet — exactly the case that makes cross-node serving useless.
    assert plan([node(f"n{i}", gbps=10.0) for i in range(4)]) is None


def test_rejects_nodes_without_enough_free_gpus():
    assert plan([node(f"n{i}", gpus=4) for i in range(4)]) is None


def test_rejects_nodes_below_min_vram():
    assert plan([node(f"n{i}") for i in range(4)], min_vram_gb=80) is None


def test_none_when_too_few_nodes():
    assert plan([node("n0"), node("n1")]) is None


def test_unlabelled_nodes_group_by_hotkey():
    # A miner that hasn't labelled its fabric is still one operator — treat its
    # boxes as one domain rather than silently unplaceable.
    bare = [
        NodeCandidate(hotkey="5A", node_id=f"n{i}", available_gpus=8,
                      vram_gb_per_gpu=32, gpu_model="rtx5090", labels={})
        for i in range(4)
    ]
    p = plan(bare, config=cfg(min_interconnect_gbps=0.0))
    assert p is not None and len(p.assignments) == 4


def test_single_node_config_is_not_our_business():
    assert plan([node("n0")], config=MultiNodeConfig(node_count=1, gpus_per_node=8)) is None


# --- topology validation -----------------------------------------------------


def test_valid_topology_has_no_problems():
    assert validate_topology(cfg()) == []


def test_tensor_parallel_may_not_span_nodes():
    problems = validate_topology(cfg(gpus_per_node=8, tensor_parallel_size=16))
    assert any("cannot span nodes" in p for p in problems)


def test_indivisible_tensor_parallel_is_flagged():
    problems = validate_topology(cfg(gpus_per_node=8, tensor_parallel_size=3))
    assert any("not divisible" in p for p in problems)


def test_pipeline_parallel_must_match_node_count():
    problems = validate_topology(cfg(node_count=4, pipeline_parallel_size=2))
    assert any("must equal node_count" in p for p in problems)


def test_missing_interconnect_floor_is_flagged():
    problems = validate_topology(cfg(min_interconnect_gbps=0.0))
    assert any("min_interconnect_gbps" in p for p in problems)


def test_unknown_backend_is_flagged():
    assert any("unknown backend" in p for p in validate_topology(cfg(backend="mpi")))


# --- the K3 shape ------------------------------------------------------------


# --- turning a plan into deployment rows -------------------------------------


def rows_for(n=4):
    p = plan([node(f"n{i}") for i in range(n)], config=cfg(node_count=n, pipeline_parallel_size=n))
    return build_replica_rows(plan=p, replica_id="r1", head_host="10.0.0.1", gpus_per_node=8)


def test_one_row_per_rank_head_first():
    rows = rows_for()
    assert len(rows) == 4
    # Head must be first: workers dial its address, so it has to exist first.
    assert rows[0]["multi_node"]["role"] == "head"
    assert [r["multi_node"]["rank"] for r in rows] == [0, 1, 2, 3]
    assert all(r["multi_node"]["role"] == "worker" for r in rows[1:])


def test_every_rank_carries_the_head_address_and_topology():
    for r in rows_for():
        mn = r["multi_node"]
        assert mn["head_host"] == "10.0.0.1"
        assert mn["node_count"] == 4
        assert mn["tensor_parallel_size"] == 8
        assert mn["pipeline_parallel_size"] == 4
        assert mn["replica_id"] == "r1"


def test_rows_name_their_node_and_operator():
    for r in rows_for():
        assert r["hotkey"] == "5MINER_A"
        assert r["node_id"].startswith("n")


def test_head_address_read_from_the_cluster_label():
    assert head_address(node("n0")) == ""  # not labelled
    labelled = NodeCandidate(hotkey="5A", node_id="n0", available_gpus=8,
                             vram_gb_per_gpu=32, gpu_model="rtx5090",
                             labels={"cluster_ip": "10.0.0.7"})
    assert head_address(labelled) == "10.0.0.7"


# --- readiness + teardown ----------------------------------------------------


def test_replica_ready_only_when_every_rank_is_ready():
    assert replica_is_ready(["ready"] * 4) is True
    # A partially-live distributed replica serves nothing — the head blocks on
    # the missing GPUs, so routing traffic there would hang.
    assert replica_is_ready(["ready", "ready", "starting", "ready"]) is False
    assert replica_is_ready(["ready", "failed"]) is False
    assert replica_is_ready([]) is False


def test_teardown_drains_workers_before_the_head():
    ordered = teardown_order(rows_for())
    assert [r["multi_node"]["role"] for r in ordered][-1] == "head"
    assert all(r["multi_node"]["role"] == "worker" for r in ordered[:-1])


def test_teardown_tolerates_single_node_rows():
    assert teardown_order([{"deployment_id": "d1"}]) == [{"deployment_id": "d1"}]


def test_kimi_k3_shape_needs_eight_nodes():
    """K3 (~1.4TB weights) = 8 nodes x 8x5090 = 64 GPUs, TP8 within, PP8 across."""
    k3 = MultiNodeConfig(node_count=8, gpus_per_node=8, min_interconnect_gbps=200.0)
    assert k3.total_gpus == 64
    assert validate_topology(k3) == []
    # Seven nodes is not enough — no partial replica.
    assert plan([node(f"n{i}", gbps=200.0) for i in range(7)], config=k3) is None
    p = plan([node(f"n{i}", gbps=200.0) for i in range(8)], config=k3)
    assert p is not None and p.total_gpus == 64
    assert p.tensor_parallel_size == 8 and p.pipeline_parallel_size == 8
