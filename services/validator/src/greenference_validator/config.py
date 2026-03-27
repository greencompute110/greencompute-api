from pydantic import BaseModel, Field


class Settings(BaseModel):
    service_name: str = "greenference-validator"
    score_alpha: float = Field(default=1.0, ge=0.0)
    score_beta: float = Field(default=1.3, ge=0.0)
    score_gamma: float = Field(default=1.1, ge=0.0)

    # Flux orchestrator
    flux_inference_floor_pct: float = Field(default=0.20, ge=0.0, le=1.0)
    flux_rental_floor_pct: float = Field(default=0.10, ge=0.0, le=1.0)
    flux_rebalance_interval_seconds: float = Field(default=30.0, ge=1.0)


settings = Settings()

