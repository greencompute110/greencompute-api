from collections import defaultdict

from greenference_protocol import UsageRecord


class UsageAggregator:
    def aggregate(self, records: list[UsageRecord]) -> dict[str, dict[str, float]]:
        totals: dict[str, dict[str, float]] = defaultdict(
            lambda: {
                "requests": 0.0,
                "streamed_requests": 0.0,
                "stream_chunks": 0.0,
                "compute_seconds": 0.0,
                "occupancy_seconds": 0.0,
                "p95_max": 0.0,
            }
        )
        for record in records:
            current = totals[record.deployment_id]
            current["requests"] += record.request_count
            current["streamed_requests"] += record.streamed_request_count
            current["stream_chunks"] += record.stream_chunk_count
            current["compute_seconds"] += record.compute_seconds
            current["occupancy_seconds"] += record.occupancy_seconds
            current["p95_max"] = max(current["p95_max"], record.latency_ms_p95)
        return dict(totals)
