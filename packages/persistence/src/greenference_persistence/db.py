from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from greenference_persistence.config import get_database_url
from greenference_persistence.orm import Base


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
