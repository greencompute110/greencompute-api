from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from greencompute_persistence.db import create_db_engine, create_session_factory, init_database, needs_bootstrap, session_scope
from greencompute_persistence.orm import APIKeyORM
from greencompute_protocol import APIKeyRecord


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class StoredAPIKey(APIKeyRecord):
    """An API key as the API side stores it: the shared protocol's record plus
    what only the API reads (so the node boxes' protocol package stays as it
    is). `expires_at` None: the key never expires."""

    expires_at: datetime | None = None


def key_expired(api_key, now: datetime | None = None) -> bool:
    """True for a key past its expiry. Any record shape: one without
    `expires_at` (the protocol's own, a test's stand-in) never expires."""
    expires = _utc(getattr(api_key, "expires_at", None))
    return expires is not None and (now or datetime.now(UTC)) >= expires


def stored_api_key(row: APIKeyORM) -> StoredAPIKey:
    return StoredAPIKey(
        key_id=row.key_id,
        user_id=row.user_id,
        name=row.name,
        admin=row.admin,
        scopes=row.scopes,
        secret=row.secret,
        created_at=row.created_at,
        max_active_rentals=row.max_active_rentals,
        daily_spend_limit_usd=(row.daily_spend_limit_cents / 100 if row.daily_spend_limit_cents is not None else None),
        expires_at=_utc(row.expires_at),
    )


class CredentialStore:
    def __init__(
        self,
        database_url: str | None = None,
        bootstrap: bool | None = None,
        engine: Engine | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.engine = engine or create_db_engine(database_url)
        self.session_factory = session_factory or create_session_factory(self.engine)
        if engine is None and session_factory is None and needs_bootstrap(str(self.engine.url), bootstrap):
            init_database(self.engine)

    def get_api_key_by_secret(self, secret: str) -> StoredAPIKey | None:
        """The key with this secret, expired or not: the caller decides what
        an expired key gets (the gateway: 401 api_key_expired)."""
        with session_scope(self.session_factory) as session:
            row = session.scalar(select(APIKeyORM).where(APIKeyORM.secret == secret))
            if row is None:
                return None
            return stored_api_key(row)
