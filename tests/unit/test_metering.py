from greenference_protocol import UsageRecord
from greenference_control_plane.domain.metering import UsageAggregator


def test_metering_aggregates_usage_by_deployment():
    aggregator = UsageAggregator()
    summary = aggregator.aggregate(
        [
            UsageRecord(
                deployment_id="dep-1",
                workload_id="wl-1",
                hotkey="miner-a",
                request_count=2,
                compute_seconds=1.5,
                latency_ms_p95=100,
                occupancy_seconds=2.0,
            ),
            UsageRecord(
                deployment_id="dep-1",
                workload_id="wl-1",
                hotkey="miner-a",
                request_count=3,
                compute_seconds=2.5,
                latency_ms_p95=120,
                occupancy_seconds=3.0,
            ),
        ]
    )
    assert summary["dep-1"]["requests"] == 5.0
    assert summary["dep-1"]["compute_seconds"] == 4.0
    assert summary["dep-1"]["p95_max"] == 120

