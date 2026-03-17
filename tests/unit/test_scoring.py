from greenference_protocol import NodeCapability, ProbeResult
from greenference_validator.domain.scoring import ScoreEngine


def test_scoring_penalizes_proxy_suspicion_and_signature_drift():
    engine = ScoreEngine()
    capability = NodeCapability(
        hotkey="miner-a",
        node_id="node-a",
        gpu_model="a100",
        gpu_count=2,
        available_gpus=2,
        vram_gb_per_gpu=80,
        cpu_cores=64,
        memory_gb=256,
        reliability_score=0.98,
        performance_score=1.1,
    )
    clean = engine.compute_scorecard(
        capability,
        [
            ProbeResult(
                challenge_id="1",
                hotkey="miner-a",
                node_id="node-a",
                latency_ms=120,
                throughput=220,
                benchmark_signature="sig-1",
            )
        ],
    )
    dirty = engine.compute_scorecard(
        capability,
        [
            ProbeResult(
                challenge_id="2",
                hotkey="miner-a",
                node_id="node-a",
                latency_ms=120,
                throughput=220,
                benchmark_signature="sig-1",
                proxy_suspected=True,
                readiness_failures=2,
            ),
            ProbeResult(
                challenge_id="3",
                hotkey="miner-a",
                node_id="node-a",
                latency_ms=130,
                throughput=210,
                benchmark_signature="sig-2",
            ),
        ],
    )
    assert dirty.fraud_penalty < clean.fraud_penalty
    assert dirty.final_score < clean.final_score

