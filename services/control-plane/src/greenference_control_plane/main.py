import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, status

from greenference_control_plane.application.services import service
from greenference_persistence import database_ready, load_runtime_settings
from greenference_control_plane.transport.routes import router

settings = load_runtime_settings("greenference-control-plane")


async def _control_plane_worker_loop() -> None:
    while True:
        service.process_pending_events()
        service.process_timeouts()
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
def healthcheck() -> dict[str, str | bool]:
    return {"status": "ok", "service": settings.service_name, "workers_enabled": settings.enable_background_workers}


@app.get("/readyz")
def readiness() -> dict[str, str]:
    ready, error = database_ready(settings.database_url)
    if not ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "error", "service": settings.service_name, "database_error": error},
        )
    return {"status": "ok", "service": settings.service_name, "database": "ok"}
