from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from greencompute_persistence.config import get_database_url, should_bootstrap_schema
from greencompute_persistence.orm import Base


def create_db_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_database_url()
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_database(engine: Engine) -> None:
    try:
        Base.metadata.create_all(engine, checkfirst=True)
    except OperationalError as exc:
        if "already exists" not in str(exc).lower():
            raise
    # Best-effort additive migrations for existing deployments. create_all does
    # not add columns to pre-existing tables, so handle the handful of columns
    # we added after initial bootstrap. Safe to run on every start.
    _additive_migrations(engine)


def _additive_migrations(engine: Engine) -> None:
    dialect = engine.dialect.name
    statements: list[str] = []
    if dialect == "postgresql":
        statements.append(
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS port_mappings JSON DEFAULT '{}'::json"
        )
        statements.append("ALTER TABLE crypto_invoices ADD COLUMN IF NOT EXISTS amount_units BIGINT")
        statements.append("ALTER TABLE crypto_invoices ADD COLUMN IF NOT EXISTS review_status VARCHAR(16)")
        statements.append("ALTER TABLE crypto_invoices ADD COLUMN IF NOT EXISTS review_note TEXT")
        statements.append(
            "ALTER TABLE crypto_invoices ADD COLUMN IF NOT EXISTS review_requested_at TIMESTAMP WITH TIME ZONE"
        )
    elif dialect == "sqlite":
        # SQLite lacks IF NOT EXISTS on ADD COLUMN; we probe and ignore duplicate errors.
        statements.append(
            "ALTER TABLE deployments ADD COLUMN port_mappings JSON DEFAULT '{}'"
        )
        statements.append("ALTER TABLE crypto_invoices ADD COLUMN amount_units BIGINT")
        statements.append("ALTER TABLE crypto_invoices ADD COLUMN review_status VARCHAR(16)")
        statements.append("ALTER TABLE crypto_invoices ADD COLUMN review_note TEXT")
        statements.append("ALTER TABLE crypto_invoices ADD COLUMN review_requested_at TIMESTAMP")
    for stmt in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise


def needs_bootstrap(database_url: str, bootstrap: bool | None = None) -> bool:
    if bootstrap is not None:
        return bootstrap
    if ":memory:" in database_url:
        return True
    return should_bootstrap_schema()


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
