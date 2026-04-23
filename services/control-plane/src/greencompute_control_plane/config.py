import os

from pydantic import BaseModel, Field


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


class Settings(BaseModel):
    service_name: str = "greencompute-control-plane"
    # Green Compute netuid: 16 on testnet, 110 on mainnet. Override via
    # GREENCOMPUTE_NETUID env var.
    netuid: int = Field(default=16, ge=0)
    default_lease_ttl_seconds: int = Field(default=3600, ge=1)
    miner_heartbeat_timeout_seconds: int = Field(default=120, ge=1)
    node_inventory_timeout_seconds: int = Field(default=300, ge=1)
    server_observed_timeout_seconds: int = Field(default=300, ge=1)
    deployment_request_retry_limit: int = Field(default=10, ge=1)
    deployment_request_retry_delay_seconds: int = Field(default=10, ge=1)
    deployment_health_failure_threshold: int = Field(default=3, ge=1)
    placement_failure_cooldown_seconds: int = Field(default=120, ge=1)
    placement_failure_threshold: int = Field(default=3, ge=1)
    # Private-endpoint inference: auto-suspend if zero invocations within this
    # window. Catalog (Flux-managed) replicas are not subject to this timer;
    # they're rebalanced based on demand signals instead.
    idle_private_endpoint_timeout_seconds: int = Field(default=1800, ge=60)


settings = Settings(
    netuid=_int("GREENCOMPUTE_NETUID", 16),
    default_lease_ttl_seconds=_int("GREENCOMPUTE_DEFAULT_LEASE_TTL_SECONDS", 3600),
    miner_heartbeat_timeout_seconds=_int("GREENCOMPUTE_MINER_HEARTBEAT_TIMEOUT_SECONDS", 120),
    node_inventory_timeout_seconds=_int("GREENCOMPUTE_NODE_INVENTORY_TIMEOUT_SECONDS", 300),
    server_observed_timeout_seconds=_int("GREENCOMPUTE_SERVER_OBSERVED_TIMEOUT_SECONDS", 300),
    deployment_request_retry_limit=_int("GREENCOMPUTE_DEPLOYMENT_REQUEST_RETRY_LIMIT", 10),
    deployment_request_retry_delay_seconds=_int("GREENCOMPUTE_DEPLOYMENT_REQUEST_RETRY_DELAY_SECONDS", 10),
    deployment_health_failure_threshold=_int("GREENCOMPUTE_DEPLOYMENT_HEALTH_FAILURE_THRESHOLD", 3),
    placement_failure_cooldown_seconds=_int("GREENCOMPUTE_PLACEMENT_FAILURE_COOLDOWN_SECONDS", 120),
    placement_failure_threshold=_int("GREENCOMPUTE_PLACEMENT_FAILURE_THRESHOLD", 3),
    idle_private_endpoint_timeout_seconds=_int("GREENCOMPUTE_IDLE_PRIVATE_ENDPOINT_TIMEOUT_SECONDS", 1800),
)
