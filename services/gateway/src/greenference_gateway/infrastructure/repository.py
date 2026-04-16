from __future__ import annotations

from collections import deque
from typing import Any

from sqlalchemy import select

from greenference_persistence import create_db_engine, create_session_factory, init_database, session_scope
from greenference_persistence.db import needs_bootstrap
from greenference_persistence.orm import APIKeyORM, UserORM, UserSecretORM, WorkloadShareORM
from greenference_protocol import APIKeyRecord, UserRecord, UserSecretRecord, WorkloadShareRecord


class GatewayRepository:
    def __init__(self, database_url: str | None = None, bootstrap: bool | None = None) -> None:
        self.engine = create_db_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        self.routing_decisions: deque[dict[str, Any]] = deque(maxlen=200)
        if needs_bootstrap(str(self.engine.url), bootstrap):
            init_database(self.engine)

    def save_user(self, user: UserRecord) -> UserRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(UserORM, user.user_id) or UserORM(user_id=user.user_id)
            row.username = user.username
            row.email = user.email
            row.display_name = user.display_name
            row.bio = user.bio
            row.website = user.website
            row.profile_metadata = user.metadata
            row.balance_credits = getattr(user, "balance_credits", 0)
            row.created_at = user.created_at
            session.add(row)
        return user

    def get_user(self, user_id: str) -> UserRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(UserORM, user_id)
            return self._to_user(row) if row else None

    def list_users(self) -> list[UserRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(UserORM)).all()
            return [self._to_user(row) for row in rows]

    def save_api_key(self, api_key: APIKeyRecord) -> APIKeyRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(APIKeyORM, api_key.key_id) or APIKeyORM(key_id=api_key.key_id)
            row.user_id = api_key.user_id
            row.name = api_key.name
            row.admin = api_key.admin
            row.scopes = api_key.scopes
            row.secret = api_key.secret
            row.created_at = api_key.created_at
            session.add(row)
        return api_key

    def list_api_keys(self, user_id: str | None = None) -> list[APIKeyRecord]:
        with session_scope(self.session_factory) as session:
            stmt = select(APIKeyORM)
            if user_id:
                stmt = stmt.where(APIKeyORM.user_id == user_id)
            rows = session.scalars(stmt).all()
            return [self._to_api_key(row) for row in rows]

    def get_api_key(self, key_id: str) -> APIKeyRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(APIKeyORM, key_id)
            return self._to_api_key(row) if row else None

    def delete_api_key(self, key_id: str) -> APIKeyRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(APIKeyORM, key_id)
            if row is None:
                return None
            record = self._to_api_key(row)
            session.delete(row)
            return record

    def save_secret(self, secret: UserSecretRecord) -> UserSecretRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(UserSecretORM, secret.secret_id) or UserSecretORM(secret_id=secret.secret_id)
            row.user_id = secret.user_id
            row.name = secret.name
            row.value = secret.value
            row.created_at = secret.created_at
            row.updated_at = secret.updated_at
            session.add(row)
        return secret

    def list_secrets(self, user_id: str) -> list[UserSecretRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(UserSecretORM).where(UserSecretORM.user_id == user_id)).all()
            return [self._to_secret(row) for row in rows]

    def get_secret(self, secret_id: str) -> UserSecretRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(UserSecretORM, secret_id)
            return self._to_secret(row) if row else None

    def delete_secret(self, secret_id: str) -> UserSecretRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(UserSecretORM, secret_id)
            if row is None:
                return None
            record = self._to_secret(row)
            session.delete(row)
            return record

    def save_workload_share(self, share: WorkloadShareRecord) -> WorkloadShareRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(WorkloadShareORM, share.share_id) or WorkloadShareORM(share_id=share.share_id)
            row.workload_id = share.workload_id
            row.owner_user_id = share.owner_user_id
            row.shared_with_user_id = share.shared_with_user_id
            row.permission = share.permission
            row.created_at = share.created_at
            session.add(row)
        return share

    def list_workload_shares(self, workload_id: str) -> list[WorkloadShareRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(select(WorkloadShareORM).where(WorkloadShareORM.workload_id == workload_id)).all()
            return [self._to_workload_share(row) for row in rows]

    def list_shared_workloads_for_user(self, user_id: str) -> list[WorkloadShareRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(WorkloadShareORM).where(WorkloadShareORM.shared_with_user_id == user_id)
            ).all()
            return [self._to_workload_share(row) for row in rows]

    def record_routing_decision(self, decision: dict[str, Any]) -> None:
        self.routing_decisions.appendleft(decision)

    def list_routing_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.routing_decisions)[:limit]

    @staticmethod
    def _to_user(row: UserORM) -> UserRecord:
        return UserRecord(
            user_id=row.user_id,
            username=row.username,
            email=row.email,
            display_name=row.display_name,
            bio=row.bio,
            website=row.website,
            metadata=row.profile_metadata or {},
            balance_credits=getattr(row, "balance_credits", 0),
            created_at=row.created_at,
        )

    @staticmethod
    def _to_api_key(row: APIKeyORM) -> APIKeyRecord:
        return APIKeyRecord(
            key_id=row.key_id,
            user_id=row.user_id,
            name=row.name,
            admin=row.admin,
            scopes=row.scopes,
            secret=row.secret,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_secret(row: UserSecretORM) -> UserSecretRecord:
        return UserSecretRecord(
            secret_id=row.secret_id,
            user_id=row.user_id,
            name=row.name,
            value=row.value,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_workload_share(row: WorkloadShareORM) -> WorkloadShareRecord:
        return WorkloadShareRecord(
            share_id=row.share_id,
            workload_id=row.workload_id,
            owner_user_id=row.owner_user_id,
            shared_with_user_id=row.shared_with_user_id,
            permission=row.permission,
            created_at=row.created_at,
        )
