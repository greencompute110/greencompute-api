import os

from pydantic import BaseModel, Field


def _env(key: str, alt: str | None = None, default: str = "") -> str:
    return os.getenv(key) or (os.getenv(alt) if alt else None) or default


def _bool(key: str, alt: str | None = None, default: bool = False) -> bool:
    val = os.getenv(key) or (os.getenv(alt) if alt else None)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _float(key: str, alt: str | None = None, default: float = 0.0) -> float:
    val = os.getenv(key) or (os.getenv(alt) if alt else None)
    return float(val) if val else default


def _int(key: str, alt: str | None = None, default: int = 0) -> int:
    val = os.getenv(key) or (os.getenv(alt) if alt else None)
    return int(val) if val else default


class Settings(BaseModel):
    service_name: str = "greenference-validator"
    score_alpha: float = Field(default=1.0, ge=0.0)
    score_beta: float = Field(default=1.3, ge=0.0)
    score_gamma: float = Field(default=1.1, ge=0.0)
    score_delta: float = Field(default=0.8, ge=0.0)
    rental_revenue_bonus_cap: float = Field(default=0.1, ge=0.0)
    whitelist_enabled: bool = True
    flux_inference_floor_pct: float = Field(default=0.20, ge=0.0, le=1.0)
    flux_rental_floor_pct: float = Field(default=0.10, ge=0.0, le=1.0)
    flux_rebalance_interval_seconds: float = Field(default=30.0, ge=1.0)
    bittensor_enabled: bool = False
    bittensor_network: str = "test"
    bittensor_netuid: int = 16
    bittensor_wallet_path: str | None = None
    metagraph_sync_interval_seconds: float = Field(default=60.0, ge=5.0)
    # Phase 2I — demand-reactive Flux
    # Target invocations/minute served by one replica; above this Flux
    # provisions more replicas of the model.
    target_rpm_per_replica: float = Field(default=30.0, gt=0.0)
    # Scale-down hysteresis — a model must stay below its scale-up threshold
    # for this long before Flux will actually drop a replica. Protects
    # against flapping when demand oscillates.
    flux_cooldown_seconds: float = Field(default=300.0, ge=0.0)
    # Phase 2F — inference attestation probes
    # Gateway URL for the canary chat completion. Empty disables the probe.
    gateway_url: str = ""
    inference_canary_api_key: str = ""
    inference_canary_timeout_seconds: float = Field(default=30.0, ge=1.0)


settings = Settings(
    score_delta=_float("GREENFERENCE_SCORE_DELTA", "SCORE_DELTA", 0.8),
    rental_revenue_bonus_cap=_float("GREENFERENCE_RENTAL_REVENUE_BONUS_CAP", "RENTAL_REVENUE_BONUS_CAP", 0.1),
    whitelist_enabled=_bool("GREENFERENCE_WHITELIST_ENABLED", "WHITELIST_ENABLED", True),
    flux_inference_floor_pct=_float("GREENFERENCE_FLUX_INFERENCE_FLOOR_PCT", "FLUX_INFERENCE_FLOOR_PCT", 20.0) / 100.0,
    flux_rental_floor_pct=_float("GREENFERENCE_FLUX_RENTAL_FLOOR_PCT", "FLUX_RENTAL_FLOOR_PCT", 10.0) / 100.0,
    flux_rebalance_interval_seconds=_float("GREENFERENCE_FLUX_REBALANCE_INTERVAL_SECONDS", "FLUX_REBALANCE_INTERVAL_SECONDS", 30.0),
    bittensor_enabled=_bool("GREENFERENCE_BITTENSOR_ENABLED", "BITTENSOR_ENABLED", False),
    bittensor_network=_env("GREENFERENCE_BITTENSOR_NETWORK", "BITTENSOR_NETWORK", "test"),
    bittensor_netuid=_int("GREENFERENCE_BITTENSOR_NETUID", "BITTENSOR_NETUID", 16),
    bittensor_wallet_path=_env("GREENFERENCE_BITTENSOR_WALLET_PATH", "BITTENSOR_WALLET_PATH") or None,
    metagraph_sync_interval_seconds=_float("GREENFERENCE_BITTENSOR_METAGRAPH_SYNC_INTERVAL", "BITTENSOR_METAGRAPH_SYNC_INTERVAL", 60.0),
    target_rpm_per_replica=_float("GREENFERENCE_FLUX_TARGET_RPM_PER_REPLICA", "FLUX_TARGET_RPM_PER_REPLICA", 30.0),
    flux_cooldown_seconds=_float("GREENFERENCE_FLUX_COOLDOWN_SECONDS", "FLUX_COOLDOWN_SECONDS", 300.0),
    gateway_url=_env("GREENFERENCE_VALIDATOR_GATEWAY_URL", "VALIDATOR_GATEWAY_URL", ""),
    inference_canary_api_key=_env("GREENFERENCE_INFERENCE_CANARY_API_KEY", "INFERENCE_CANARY_API_KEY", ""),
    inference_canary_timeout_seconds=_float("GREENFERENCE_INFERENCE_CANARY_TIMEOUT_SECONDS", "INFERENCE_CANARY_TIMEOUT_SECONDS", 30.0),
)
