from collections import defaultdict

from greencompute_protocol import UsageRecord


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


def is_catalog_workload(workload_id: str, metadata: dict | None) -> bool:
    """One of the platform's shared catalog models: run by Flux, ids
    "catalog-<model>", paid for per token by whoever calls it."""
    return (metadata or {}).get("managed_by") == "flux" or workload_id.startswith("catalog-")


def is_private_model(owner_user_id: str | None, kind: str, workload_id: str, metadata: dict | None) -> bool:
    """A customer's own model: a deployment of an inference workload with an
    owner, that isn't a shared catalog model. Its owner pays for it by the
    hour while it's READY, like a GPU rental, and requests to it are not
    charged per token. Takes plain values so the billing monitor can use it
    on database rows."""
    return bool(owner_user_id) and kind == "inference" and not is_catalog_workload(workload_id, metadata)
