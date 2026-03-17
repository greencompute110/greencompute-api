from fastapi import FastAPI

from greenference_builder.transport.routes import router

app = FastAPI(title="Greenference Builder", version="0.1.0")
app.include_router(router)


@app.get("/healthz")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}

