import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse

from greenference_control_plane.application.services import service
from greenference_persistence import database_ready, get_metrics_store, load_runtime_settings, render_prometheus_text
from greenference_control_plane.transport.routes import router

settings = load_runtime_settings("greenference-control-plane")
_worker_state: dict[str, object | None] = {
    "running": False,
    "last_iteration": None,
    "last_successful_iteration": None,
    "last_failed_iteration": None,
    "last_error": None,
}
metrics = get_metrics_store("greenference-control-plane")


async def _control_plane_worker_loop() -> None:
    _worker_state["running"] = True
    while True:
        try:
            service.process_pending_events()
            service.process_timeouts()
            service.process_unhealthy_miners()
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
        task = asyncio.create_task(_control_plane_worker_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Greenference Control Plane", version="0.1.0", lifespan=lifespan)
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
