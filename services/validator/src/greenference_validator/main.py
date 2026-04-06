import asyncio
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse
from starlette.middleware.cors import CORSMiddleware

from greenference_persistence import (
    database_ready,
    get_metrics_store,
    load_runtime_settings,
    render_prometheus_text,
)
from greenference_validator.application.services import service
from greenference_validator.config import settings as service_settings
from greenference_validator.transport.routes import router

settings = load_runtime_settings("greenference-validator")
_worker_state: dict[str, object | None] = {
    "running": False,
    "last_iteration": None,
    "last_successful_iteration": None,
    "last_failed_iteration": None,
    "last_error": None,
}
metrics = get_metrics_store("greenference-validator")


async def _validator_worker_loop() -> None:
    _worker_state["running"] = True
    _flux_counter = 0
    _metagraph_counter = 0
    flux_every = max(1, int(
        service_settings.flux_rebalance_interval_seconds / settings.worker_poll_interval_seconds
    ))
    metagraph_every = max(1, int(
        service_settings.metagraph_sync_interval_seconds / settings.worker_poll_interval_seconds
    ))
    while True:
        try:
            service.process_pending_events()
            _flux_counter += 1
            if _flux_counter >= flux_every:
                service.rebalance_all_miners()
                _flux_counter = 0
            _metagraph_counter += 1
            if service_settings.bittensor_enabled and _metagraph_counter >= metagraph_every:
                service.sync_metagraph()
                _metagraph_counter = 0
            _worker_state["last_successful_iteration"] = asyncio.get_running_loop().time()
            _worker_state["last_error"] = None
        except Exception as exc:
            _worker_state["last_failed_iteration"] = asyncio.get_running_loop().time()
            _worker_state["last_error"] = str(exc)
        finally:
            _worker_state["last_iteration"] = asyncio.get_running_loop().time()
        await asyncio.sleep(settings.worker_poll_interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = None
    if settings.enable_background_workers:
        task = asyncio.create_task(_validator_worker_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Greenference Validator", version="0.1.0", lifespan=lifespan)

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
def healthcheck() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "service": settings.service_name,
        "workers_enabled": settings.enable_background_workers,
        "bus_transport": settings.bus_transport,
    }


@app.get("/readyz")
def readiness() -> dict[str, object]:
    ready, error = database_ready(settings.database_url)
    if not ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "error", "service": settings.service_name, "database_error": error},
        )
    payload: dict[str, object] = {"status": "ok", "service": settings.service_name, "database": "ok"}
    payload["bus_transport"] = settings.bus_transport
    if settings.enable_background_workers:
        payload["workers_enabled"] = True
        payload["worker_running"] = bool(_worker_state["running"])
        payload["worker_last_iteration"] = _worker_state["last_iteration"]
        payload["worker_last_successful_iteration"] = _worker_state["last_successful_iteration"]
        payload["worker_last_failed_iteration"] = _worker_state["last_failed_iteration"]
        payload["worker_last_error"] = _worker_state["last_error"]
    return payload


@app.get("/_metrics")
def prometheus_metrics() -> PlainTextResponse:
    metrics.set_gauge(
        "workers.validator.running",
        1.0 if bool(_worker_state["running"]) else 0.0,
    )
    metrics.set_gauge(
        "event_deliveries.validator.pending",
        float(len(service.bus.list_deliveries(consumer="validator-worker", statuses=["pending"]))),
    )
    metrics.set_gauge(
        "event_deliveries.validator.failed",
        float(len(service.bus.list_deliveries(consumer="validator-worker", statuses=["failed"]))),
    )
    return PlainTextResponse(
        render_prometheus_text(settings.service_name, metrics),
        media_type="text/plain; version=0.0.4",
    )
