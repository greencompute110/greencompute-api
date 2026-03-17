from greenference_protocol import NodeCapability, ProbeResult
from greenference_validator.application.services import ValidatorService
from greenference_validator.infrastructure.repository import ValidatorRepository


def test_probe_results_produce_weights():
    validator = ValidatorService(ValidatorRepository(database_url="sqlite+pysqlite:///:memory:"))
    validator.register_capability(
        NodeCapability(
            hotkey="miner-a",
            node_id="node-a",
            gpu_model="a100",
            gpu_count=1,
            available_gpus=1,
            vram_gb_per_gpu=80,
            cpu_cores=32,
            memory_gb=128,
        )
    )
    challenge = validator.create_probe("miner-a", "node-a")
    scorecard = validator.submit_probe_result(
        ProbeResult(
            challenge_id=challenge.challenge_id,
            hotkey="miner-a",
            node_id="node-a",
            latency_ms=100.0,
            throughput=180.0,
            benchmark_signature="sig-1",
        )
    )
    snapshot = validator.publish_weight_snapshot()

    assert scorecard.final_score > 0
    assert snapshot.weights["miner-a"] == scorecard.final_score
