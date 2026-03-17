from pydantic import BaseModel, Field


class Settings(BaseModel):
    service_name: str = "greenference-validator"
    score_alpha: float = Field(default=1.0, ge=0.0)
    score_beta: float = Field(default=1.3, ge=0.0)
    score_gamma: float = Field(default=1.1, ge=0.0)


settings = Settings()

