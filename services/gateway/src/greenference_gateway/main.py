import os

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse
from starlette.middleware.cors import CORSMiddleware

from greenference_persistence import database_ready, load_runtime_settings, render_prometheus_text
from greenference_gateway.transport.security import metrics as gateway_metrics
from greenference_gateway.transport.routes import router

settings = load_runtime_settings("greenference-gateway")

app = FastAPI(title="Greenference Gateway", version="0.1.0")

# Browser clients (Next.js, etc.) need CORS. Comma-separated origins in GREENFERENCE_CORS_ALLOW_ORIGINS.
# If unset, default to local Next.js dev URLs so the gateway never runs without CORS (avoids silent browser blocks).
# Use "*" only for local experiments (credentials disabled per spec). Production: set your public UI origin(s).
_cors_raw = os.getenv("GREENFERENCE_CORS_ALLOW_ORIGINS", "").strip()
_cors_fallback = "http://localhost:3000,http://127.0.0.1:3000"
_origins_line = _cors_raw if _cors_raw else _cors_fallback
_origins = [o.strip() for o in _origins_line.split(",") if o.strip()]
_wildcard = len(_origins) == 1 and _origins[0] == "*"
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _wildcard else _origins,
    allow_credentials=not _wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

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


@app.get("/_metrics")
def prometheus_metrics() -> PlainTextResponse:
    return PlainTextResponse(
        render_prometheus_text(settings.service_name, gateway_metrics),
        media_type="text/plain; version=0.0.4",
    )
