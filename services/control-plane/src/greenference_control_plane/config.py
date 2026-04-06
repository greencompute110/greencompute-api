from pydantic import BaseModel, Field


class Settings(BaseModel):
    service_name: str = "greenference-control-plane"
    netuid: int = Field(default=16, ge=0)
    default_lease_ttl_seconds: int = Field(default=3600, ge=1)  # 1h for image pulls
    miner_heartbeat_timeout_seconds: int = Field(default=30, ge=1)
    node_inventory_timeout_seconds: int = Field(default=60, ge=1)
    server_observed_timeout_seconds: int = Field(default=60, ge=1)
    deployment_request_retry_limit: int = Field(default=3, ge=1)
    deployment_request_retry_delay_seconds: int = Field(default=5, ge=1)
    deployment_health_failure_threshold: int = Field(default=2, ge=1)
    placement_failure_cooldown_seconds: int = Field(default=300, ge=1)
    placement_failure_threshold: int = Field(default=2, ge=1)


settings = Settings()
