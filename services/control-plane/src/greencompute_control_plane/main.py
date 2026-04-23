import asyncio
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse
from starlette.middleware.cors import CORSMiddleware

from greencompute_control_plane.application.services import service
from greencompute_persistence import database_ready, get_metrics_store, load_runtime_settings, render_prometheus_text
from greencompute_control_plane.transport.routes import router

settings = load_runtime_settings("greencompute-control-plane")
_worker_state: dict[str, object | None] = {
    "running": False,
    "last_iteration": None,
    "last_successful_iteration": None,
    "last_failed_iteration": None,
    "last_error": None,
    "last_recovery_at": None,
    "last_recovery_error": None,
    "last_recovery_requeued_deliveries": 0,
}
metrics = get_metrics_store("greencompute-control-plane")


async def _control_plane_worker_loop() -> None:
    _worker_state["running"] = True
    _metering_counter = 0
    _metering_interval = max(1, int(60 / max(1, settings.worker_poll_interval_seconds)))
    while True:
        try:
            service.process_pending_events()
            service.process_timeouts()
            service.process_unhealthy_miners()
            service.process_idle_inference_deployments()
            # Usage metering — runs every ~60 seconds
            _metering_counter += 1
            if _metering_counter >= _metering_interval:
                _metering_counter = 0
                try:
                    service.meter_usage()
                except Exception as meter_exc:
                    # Log loudly — the outer try/except wrapper clears
                    # `last_error` on every successful iteration, so without
                    # the log a metering regression would otherwise be
                    # invisible.
                    import logging
                    logging.getLogger(__name__).exception(
                        "metering cycle failed: %s", meter_exc
                    )
                    _worker_state["last_error"] = f"metering: {meter_exc}"
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
        try:
            recovery = service.recover_inflight_events()
            _worker_state["last_recovery_at"] = recovery["last_recovery_at"]
            _worker_state["last_recovery_error"] = None
            _worker_state["last_recovery_requeued_deliveries"] = recovery["requeued_deliveries"]
        except Exception as exc:  # noqa: BLE001
            _worker_state["last_recovery_error"] = str(exc)
        task = asyncio.create_task(_control_plane_worker_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="GreenCompute Control Plane", version="0.1.0", lifespan=lifespan)

_cors_raw = os.getenv("GREENCOMPUTE_CORS_ALLOW_ORIGINS", "").strip()
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
def healthcheck() -> dict[str, object]:
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
        payload["worker_last_recovery_at"] = _worker_state["last_recovery_at"]
        payload["worker_last_recovery_error"] = _worker_state["last_recovery_error"]
        payload["worker_last_recovery_requeued_deliveries"] = _worker_state["last_recovery_requeued_deliveries"]
    return payload


@app.get("/_metrics")
def prometheus_metrics() -> PlainTextResponse:
    metrics.set_gauge(
        "workers.control_plane.running",
        1.0 if bool(_worker_state["running"]) else 0.0,
    )
    metrics.set_gauge(
        "event_deliveries.pending",
        float(len(service.bus.list_deliveries(statuses=["pending"]))),
    )
    metrics.set_gauge(
        "event_deliveries.processing",
        float(len(service.bus.list_deliveries(statuses=["processing"]))),
    )
    metrics.set_gauge(
        "event_deliveries.failed",
        float(len(service.bus.list_deliveries(statuses=["failed"]))),
    )
    metrics.set_gauge(
        "miners.unhealthy",
        float(len([item for item in service.miner_health_report() if item["status"] != "healthy"])),
    )
    metrics.set_gauge(
        "deployments.stuck",
        float(len(service.stuck_deployments_report())),
    )
    return PlainTextResponse(
        render_prometheus_text(settings.service_name, metrics),
        media_type="text/plain; version=0.0.4",
    )
