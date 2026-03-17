import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, status

from greenference_builder.application.services import service
from greenference_persistence import database_ready, load_runtime_settings
from greenference_builder.transport.routes import router

settings = load_runtime_settings("greenference-builder")
_worker_state: dict[str, object | None] = {"running": False, "last_iteration": None}


async def _builder_worker_loop() -> None:
    _worker_state["running"] = True
    while True:
        service.process_pending_events()
        _worker_state["last_iteration"] = asyncio.get_running_loop().time()
        await asyncio.sleep(settings.worker_poll_interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = None
    if settings.enable_background_workers:
        task = asyncio.create_task(_builder_worker_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Greenference Builder", version="0.1.0", lifespan=lifespan)
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
def readiness() -> dict[str, str | bool | float | None]:
    ready, error = database_ready(settings.database_url)
    if not ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "error", "service": settings.service_name, "database_error": error},
        )
    payload: dict[str, str | bool | float | None] = {"status": "ok", "service": settings.service_name, "database": "ok"}
    payload["bus_transport"] = settings.bus_transport
    if settings.enable_background_workers:
        payload["workers_enabled"] = True
        payload["worker_running"] = bool(_worker_state["running"])
        payload["worker_last_iteration"] = _worker_state["last_iteration"]
    return payload
