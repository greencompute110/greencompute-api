from pydantic import BaseModel, Field


class Settings(BaseModel):
    service_name: str = "greenference-control-plane"
    netuid: int = Field(default=64, ge=0)
    default_lease_ttl_seconds: int = Field(default=300, ge=1)


settings = Settings()

