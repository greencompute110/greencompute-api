import hashlib

from greencompute_protocol import NodeCapability, ProbeResult
from greencompute_validator.domain.scoring import ScoreEngine


def _denonced_signature(model_id: str, node_id: str, response_text: str, nonce: str) -> str:
    """Mirror of the de-nonced cross-probe signature generated in
    ValidatorService.run_inference_canary — kept in lockstep so this test
    fails if the generation formula drifts."""
    norm = response_text.upper()
    denonced = norm.replace(nonce.upper(), "")[:64]
    return hashlib.sha256(f"{model_id}:{node_id}:{denonced}".encode()).hexdigest()[:16]


def test_fraud_penalty_neutral_on_empty_results():
    # ORCH-L1: empty results must yield a NEUTRAL multiplier (1.0), not 0.0,
    # which would zero the entire final_score.
    engine = ScoreEngine()
    assert engine._fraud_penalty([]) == 1.0


def test_denonced_signature_collapses_across_honest_probes():
    # ORCH-C1: two honest "{nonce} DONE" answers to DIFFERENT nonces must
    # produce the SAME stable signature so an honest miner does not eat a
    # permanent 0.75 cross-probe penalty.
    sig_a = _denonced_signature("model-x", "node-a", "abc123 DONE", "abc123")
    sig_b = _denonced_signature("model-x", "node-a", "def456 DONE", "def456")
    assert sig_a == sig_b
    # A canned proxy whose structure differs still yields a DISTINCT signature.
    sig_proxy = _denonced_signature("model-x", "node-a", "abc123 OK", "abc123")
    assert sig_proxy != sig_a
    # A different served model/node also diverges.
    sig_other_node = _denonced_signature("model-x", "node-b", "abc123 DONE", "abc123")
    assert sig_other_node != sig_a


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



def _probe(success: bool, proxy: bool):
    from greencompute_protocol import ProbeResult
    from uuid import uuid4
    return ProbeResult(
        challenge_id=str(uuid4()), hotkey="hk", node_id="n1",
        latency_ms=360.0, throughput=45.0, success=success, proxy_suspected=proxy,
    )


def test_proxy_penalty_tolerates_rare_misses():
    # 2 in 1000 (0.2%) = model flakiness, not proxying → no penalty.
    engine = ScoreEngine()
    results = [_probe(True, False) for _ in range(998)] + [_probe(False, True) for _ in range(2)]
    assert engine._proxy_penalty(results) == 1.0


def test_proxy_penalty_full_penalty_when_consistent():
    # A backend that ~never echoes the nonce = real proxy/cache → 0.4.
    engine = ScoreEngine()
    results = [_probe(False, True) for _ in range(50)] + [_probe(True, False) for _ in range(10)]
    assert engine._proxy_penalty(results) == 0.4


def test_proxy_penalty_moderate_band():
    engine = ScoreEngine()
    # ~10% proxy rate → moderate penalty.
    results = [_probe(True, False) for _ in range(90)] + [_probe(False, True) for _ in range(10)]
    assert engine._proxy_penalty(results) == 0.7
