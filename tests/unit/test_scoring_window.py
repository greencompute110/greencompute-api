"""The probe-based scores (reliability/performance/fraud) are computed over a
trailing window, not all-time, so a miner with a rough bring-up that has been
healthy recently isn't penalized forever.

Repro of the 2026-06-19 ops report: a hotkey sat at reliability 0.12 because
~5.5k lifetime readiness failures + 60% lifetime success floored the score,
even though the last 7 days were 99.9% clean.
"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from greencompute_protocol import ProbeResult
from greencompute_validator.infrastructure.repository import ValidatorRepository


def _result(hotkey: str, *, success: bool, readiness: int, age_days: float) -> ProbeResult:
    return ProbeResult(
        challenge_id=str(uuid4()),
        hotkey=hotkey,
        node_id="n1",
        latency_ms=360.0,
        throughput=45.0,
        success=success,
        readiness_failures=readiness,
        observed_at=datetime.now(UTC) - timedelta(days=age_days),
    )


def test_list_results_since_excludes_old_probes():
    repo = ValidatorRepository(database_url="sqlite:///:memory:", bootstrap=True)
    repo.add_result(_result("hk", success=False, readiness=5, age_days=30))   # old/bad
    repo.add_result(_result("hk", success=True, readiness=0, age_days=1))     # recent/good

    cutoff = datetime.now(UTC) - timedelta(days=7)
    windowed = repo.list_results("hk", since=cutoff)
    assert len(windowed) == 1
    assert windowed[0].success is True
    # No window → both rows.
    assert len(repo.list_results("hk")) == 2


def test_window_recovers_reliability_after_bad_history():
    from greencompute_validator.domain.scoring import ScoreEngine
    from greencompute_protocol import NodeCapability

    repo = ValidatorRepository(database_url="sqlite:///:memory:", bootstrap=True)
    # Rough bring-up 30 days ago: many failures + readiness hits.
    for _ in range(80):
        repo.add_result(_result("hk", success=False, readiness=3, age_days=30))
    # Healthy last week.
    for _ in range(40):
        repo.add_result(_result("hk", success=True, readiness=0, age_days=1))

    cap = NodeCapability(
        hotkey="hk", node_id="n1", gpu_model="rtx4090",
        gpu_count=8, available_gpus=0, vram_gb_per_gpu=24, cpu_cores=32, memory_gb=128,
    )
    engine = ScoreEngine()
    all_time = engine.compute_scorecard(cap, repo.list_results("hk"))
    cutoff = datetime.now(UTC) - timedelta(days=7)
    windowed = engine.compute_scorecard(cap, repo.list_results("hk", since=cutoff))

    # All-time is dragged down by the bad history; windowed reflects the
    # healthy recent week and is materially higher.
    assert all_time.reliability_score < 0.5
    assert windowed.reliability_score > 0.9
    assert windowed.reliability_score > all_time.reliability_score
