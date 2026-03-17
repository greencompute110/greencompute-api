from fastapi import FastAPI, HTTPException, status

from greenference_persistence import database_ready, load_runtime_settings
from greenference_validator.transport.routes import router

settings = load_runtime_settings("greenference-validator")

app = FastAPI(title="Greenference Validator", version="0.1.0")
app.include_router(router)


@app.get("/healthz")
def healthcheck() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/readyz")
def readiness() -> dict[str, str]:
    ready, error = database_ready(settings.database_url)
    if not ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "error", "service": settings.service_name, "database_error": error},
        )
    return {"status": "ok", "service": settings.service_name, "database": "ok"}
