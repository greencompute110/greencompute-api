from greencompute_protocol import NodeCapability, WorkloadCreateRequest, WorkloadSpec
from greencompute_control_plane.domain.scheduler import PlacementPolicy


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

