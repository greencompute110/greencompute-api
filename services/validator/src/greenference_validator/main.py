from fastapi import FastAPI

from greenference_validator.transport.routes import router

app = FastAPI(title="Greenference Validator", version="0.1.0")
app.include_router(router)


@app.get("/healthz")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
