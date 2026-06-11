from greencompute_control_plane.domain.scheduler import PlacementPolicy
from greencompute_protocol import NodeCapability, WorkloadCreateRequest, WorkloadSpec


def test_scheduler_prefers_health_reliability_and_fit():
    workload = WorkloadSpec(
        **WorkloadCreateRequest(
            name="llm",
            image="greencompute/llm:latest",
            requirements={
                "gpu_count": 1,
                "min_vram_gb_per_gpu": 48,
                "cpu_cores": 16,
                "memory_gb": 64,
            },
        ).model_dump()
    )
    policy = PlacementPolicy()
    ranked = policy.rank_nodes(
        workload,
        [
            NodeCapability(
                hotkey="miner-a",
                node_id="a-1",
                gpu_model="a100",
                gpu_count=1,
                available_gpus=1,
                vram_gb_per_gpu=80,
                cpu_cores=32,
                memory_gb=128,
                hourly_cost_usd=2.0,
                health_score=0.95,
                reliability_score=0.99,
                performance_score=1.2,
            ),
            NodeCapability(
                hotkey="miner-b",
                node_id="b-1",
                gpu_model="h100",
                gpu_count=1,
                available_gpus=1,
                vram_gb_per_gpu=80,
                cpu_cores=32,
                memory_gb=128,
                hourly_cost_usd=1.0,
                health_score=0.7,
                reliability_score=0.8,
                performance_score=1.0,
            ),
        ],
    )
    assert ranked[0].node.hotkey == "miner-a"



def _node(node_id: str, labels: dict | None = None) -> NodeCapability:
    return NodeCapability(
        hotkey="miner-a",
        node_id=node_id,
        gpu_model="rtx4090",
        gpu_count=8,
        available_gpus=8,
        vram_gb_per_gpu=24,
        cpu_cores=32,
        memory_gb=128,
        labels=labels or {},
    )


def _pod_workload() -> WorkloadSpec:
    return WorkloadSpec(
        **WorkloadCreateRequest(
            name="rental",
            image="greencompute/pod:latest",
            kind="pod",
            requirements={
                "gpu_count": 1, "min_vram_gb_per_gpu": 16, "cpu_cores": 1, "memory_gb": 1,
            },
        ).model_dump()
    )


def test_workload_kinds_label_excludes_unsupported_kind():
    # Regression: the old check was `kind in label-string` with the kind
    # itself as the .get default — a double no-op that let pod rentals land
    # on inference-only nodes.
    ranked = PlacementPolicy().rank_nodes(
        _pod_workload(), [_node("inference-only", {"workload_kinds": "inference"})]
    )
    assert ranked == []


def test_workload_kinds_label_allows_listed_kind():
    ranked = PlacementPolicy().rank_nodes(
        _pod_workload(), [_node("full", {"workload_kinds": "inference,pod,vm"})]
    )
    assert len(ranked) == 1


def test_workload_kinds_absent_label_allows_everything():
    # Legacy node-agents don't report the label — absent must not exclude.
    ranked = PlacementPolicy().rank_nodes(_pod_workload(), [_node("legacy")])
    assert len(ranked) == 1


def test_workload_kinds_is_not_a_substring_match():
    # 'pod' must not match inside 'tripod'.
    ranked = PlacementPolicy().rank_nodes(
        _pod_workload(), [_node("tripod", {"workload_kinds": "tripod"})]
    )
    assert ranked == []
