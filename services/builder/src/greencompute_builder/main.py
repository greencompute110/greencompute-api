import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse

from greencompute_builder.application.services import service
from greencompute_persistence import (
    database_ready,
    get_metrics_store,
    load_runtime_settings,
    render_prometheus_text,
)
from greencompute_builder.transport.routes import router

settings = load_runtime_settings("greencompute-builder")
_worker_state: dict[str, object | None] = {
    "running": False,
    "last_iteration": None,
    "last_successful_iteration": None,
    "last_failed_iteration": None,
    "last_error": None,
    "last_recovery_at": None,
    "last_recovery_error": None,
    "last_recovery_requeued_deliveries": 0,
    "last_recovery_republished_jobs": 0,
}
metrics = get_metrics_store("greencompute-builder")


async def _builder_worker_loop() -> None:
    _worker_state["running"] = True
    while True:
        try:
            service.process_pending_events()
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
            recovery = service.recover_inflight_jobs()
            _worker_state["last_recovery_at"] = recovery["last_recovery_at"]
            _worker_state["last_recovery_error"] = None
            _worker_state["last_recovery_requeued_deliveries"] = recovery["requeued_deliveries"]
            _worker_state["last_recovery_republished_jobs"] = recovery["republished_jobs"]
        except Exception as exc:  # noqa: BLE001
            _worker_state["last_recovery_error"] = str(exc)
        task = asyncio.create_task(_builder_worker_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="GreenCompute Builder", version="0.1.0", lifespan=lifespan)
app.include_router(router)


@app.get("/healthz")
def healthcheck() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "service": settings.service_name,
        "workers_enabled": settings.enable_background_workers,
        "bus_transport": settings.bus_transport,
        "build_execution_mode": settings.build_execution_mode,
    }


@app.get("/readyz")
def readiness() -> dict[str, str | bool | float | None]:
    ready, error = database_ready(settings.database_url)
    if not ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "error", "service": settings.service_name, "database_error": error},
        )
    payload: dict[str, str | bool | float | None] = {"status": "ok", "service": settings.service_name, "database": "ok"}
    payload["bus_transport"] = settings.bus_transport
    payload["build_execution_mode"] = settings.build_execution_mode
    execution = service.execution_status()
    payload["builder_runner"] = str(execution["runner"])
    payload["builder_object_store_adapter"] = str(execution["object_store_adapter"])
    payload["builder_registry_adapter"] = str(execution["registry_adapter"])
    payload["pending_delivery_count"] = float(execution["pending_delivery_count"])
    payload["failed_delivery_count"] = float(execution["failed_delivery_count"])
    if settings.enable_background_workers:
        payload["workers_enabled"] = True
        payload["worker_running"] = bool(_worker_state["running"])
        payload["worker_last_iteration"] = _worker_state["last_iteration"]
        payload["worker_last_successful_iteration"] = _worker_state["last_successful_iteration"]
        payload["worker_last_failed_iteration"] = _worker_state["last_failed_iteration"]
        payload["worker_last_error"] = _worker_state["last_error"]
        payload["worker_last_recovery_at"] = (
            _worker_state["last_recovery_at"].isoformat()
            if hasattr(_worker_state["last_recovery_at"], "isoformat")
            else _worker_state["last_recovery_at"]
        )
        payload["worker_last_recovery_error"] = _worker_state["last_recovery_error"]
        payload["worker_last_recovery_requeued_deliveries"] = _worker_state["last_recovery_requeued_deliveries"]
        payload["worker_last_recovery_republished_jobs"] = _worker_state["last_recovery_republished_jobs"]
    return payload


@app.get("/_metrics")
def prometheus_metrics() -> PlainTextResponse:
    metrics.set_gauge(
        "workers.builder.running",
        1.0 if bool(_worker_state["running"]) else 0.0,
    )
    metrics.set_gauge(
        "event_deliveries.builder.pending",
        float(len(service.bus.list_deliveries(consumer="builder-worker", statuses=["pending"]))),
    )
    metrics.set_gauge(
        "event_deliveries.builder.failed",
        float(len(service.bus.list_deliveries(consumer="builder-worker", statuses=["failed"]))),
    )
    return PlainTextResponse(
        render_prometheus_text(settings.service_name, metrics),
        media_type="text/plain; version=0.0.4",
    )
