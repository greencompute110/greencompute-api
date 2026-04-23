from __future__ import annotations

import os

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from greencompute_persistence.config import get_database_url, should_bootstrap_schema
from greencompute_persistence.db import create_db_engine, create_session_factory, session_scope


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


class RuntimeSettings(BaseModel):
    service_name: str
    database_url: str
    redis_url: str = "redis://127.0.0.1:6379/0"
    nats_url: str = "nats://127.0.0.1:4222"
    bus_transport: str = "auto"
    build_execution_mode: str = "live"
    object_store_endpoint: str = "http://127.0.0.1:9000"
    object_store_access_key: str = "greencompute"
    object_store_secret_key: str = "greencompute"
    object_store_bucket: str = "greencompute-build-artifacts"
    registry_url: str = "http://127.0.0.1:5000"
    registry_password: str = "greencompute-registry"
    build_executor_endpoint: str = "http://127.0.0.1:8081"
    build_executor_auth_token: str = "greencompute-builder"
    enable_background_workers: bool = False
    worker_poll_interval_seconds: float = Field(default=1.0, ge=0.1)
    bootstrap_schema: bool = False


def load_runtime_settings(service_name: str) -> RuntimeSettings:
    return RuntimeSettings(
        service_name=service_name,
        database_url=get_database_url(),
        redis_url=os.getenv("GREENCOMPUTE_REDIS_URL", "redis://127.0.0.1:6379/0"),
        nats_url=os.getenv("GREENCOMPUTE_NATS_URL", "nats://127.0.0.1:4222"),
        bus_transport=os.getenv("GREENCOMPUTE_BUS_TRANSPORT", "auto"),
        build_execution_mode=os.getenv("GREENCOMPUTE_BUILD_EXECUTION_MODE", "live"),
        object_store_endpoint=os.getenv("GREENCOMPUTE_OBJECT_STORE_ENDPOINT", "http://127.0.0.1:9000"),
        object_store_access_key=os.getenv("GREENCOMPUTE_OBJECT_STORE_ACCESS_KEY", "greencompute"),
        object_store_secret_key=os.getenv("GREENCOMPUTE_OBJECT_STORE_SECRET_KEY", "greencompute"),
        object_store_bucket=os.getenv("GREENCOMPUTE_OBJECT_STORE_BUCKET", "greencompute-build-artifacts"),
        registry_url=os.getenv("GREENCOMPUTE_REGISTRY_URL", "http://127.0.0.1:5000"),
        registry_password=os.getenv("GREENCOMPUTE_REGISTRY_PASSWORD", "greencompute-registry"),
        build_executor_endpoint=os.getenv("GREENCOMPUTE_BUILD_EXECUTOR_ENDPOINT", "http://127.0.0.1:8081"),
        build_executor_auth_token=os.getenv("GREENCOMPUTE_BUILD_EXECUTOR_AUTH_TOKEN", "greencompute-builder"),
        enable_background_workers=_env_bool("GREENCOMPUTE_ENABLE_BACKGROUND_WORKERS", False),
        worker_poll_interval_seconds=float(os.getenv("GREENCOMPUTE_WORKER_POLL_INTERVAL_SECONDS", "1.0")),
        bootstrap_schema=should_bootstrap_schema(),
    )


def database_ready(database_url: str | None = None) -> tuple[bool, str | None]:
    engine = create_db_engine(database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_scope(session_factory) as session:
            session.execute(text("SELECT 1"))
        return True, None
    except SQLAlchemyError as exc:
        return False, str(exc)
    finally:
        engine.dispose()
