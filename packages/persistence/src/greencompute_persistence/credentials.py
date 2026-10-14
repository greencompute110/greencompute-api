from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from greencompute_persistence.db import create_db_engine, create_session_factory, init_database, needs_bootstrap, session_scope
from greencompute_persistence.orm import APIKeyORM
from greencompute_protocol import APIKeyRecord


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

    def get_api_key_by_secret(self, secret: str) -> APIKeyRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.scalar(select(APIKeyORM).where(APIKeyORM.secret == secret))
            if row is None:
                return None
            return APIKeyRecord(
                key_id=row.key_id,
                user_id=row.user_id,
                name=row.name,
                admin=row.admin,
                scopes=row.scopes,
                secret=row.secret,
                created_at=row.created_at,
                max_active_rentals=row.max_active_rentals,
                daily_spend_limit_usd=(row.daily_spend_limit_cents / 100 if row.daily_spend_limit_cents is not None else None),
            )
