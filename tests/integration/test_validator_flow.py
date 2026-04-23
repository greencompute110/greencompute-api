import pytest

from greenference_protocol import NodeCapability, ProbeResult
from greenference_validator.application.services import (
    InvalidProbeResultError,
    UnknownProbeChallengeError,
    ValidatorService,
)
from greenference_validator.infrastructure.repository import ValidatorRepository


def test_probe_results_produce_weights_and_reject_invalid_submissions():
    repository = ValidatorRepository(database_url="sqlite+pysqlite:///:memory:", bootstrap=True)
    validator = ValidatorService(repository)
    capability = NodeCapability(
        hotkey="miner-a",
        node_id="node-a",
        gpu_model="a100",
        gpu_count=1,
        available_gpus=1,
        vram_gb_per_gpu=80,
        cpu_cores=32,
        memory_gb=128,
    )
    validator.register_capability(capability)

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
    with pytest.raises(UnknownProbeChallengeError):
        validator.submit_probe_result(
            ProbeResult(
                challenge_id="missing-challenge",
                hotkey="miner-a",
                node_id="node-a",
                latency_ms=90.0,
                throughput=200.0,
            )
        )

    with pytest.raises(InvalidProbeResultError):
        validator.submit_probe_result(
            ProbeResult(
                challenge_id=challenge.challenge_id,
                hotkey="miner-a",
                node_id="node-a",
                latency_ms=95.0,
                throughput=190.0,
                benchmark_signature="sig-1",
            )
        )

    # publish_weight_snapshot defaults to netuid 16 (testnet); mainnet is 110.
    snapshot_one = validator.publish_weight_snapshot()
    snapshot_two = validator.publish_weight_snapshot()
    snapshots = repository.list_snapshots(netuid=16)

    assert scorecard.final_score > 0
    assert snapshot_one.weights["miner-a"] == scorecard.final_score
    assert snapshot_two.weights == snapshot_one.weights
    assert len(snapshots) == 2
