from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from greenference_persistence.db import create_db_engine, create_session_factory, init_database, needs_bootstrap, session_scope
from greenference_persistence.orm import WorkflowEventORM


def utcnow() -> datetime:
    return datetime.now(UTC)


class WorkflowEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    subject: str
    payload: dict[str, Any]
    status: str = "pending"
    attempts: int = 0
    available_at: datetime = Field(default_factory=utcnow)
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class WorkflowEventRepository:
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

    def publish(self, subject: str, payload: dict[str, Any]) -> WorkflowEvent:
        event = WorkflowEvent(subject=subject, payload=payload)
        with session_scope(self.session_factory) as session:
            session.add(
                WorkflowEventORM(
                    event_id=event.event_id,
                    subject=event.subject,
                    payload=event.payload,
                    status=event.status,
                    attempts=event.attempts,
                    available_at=event.available_at,
                    last_error=event.last_error,
                    created_at=event.created_at,
                    updated_at=event.updated_at,
                )
            )
        return event

    def claim_pending(self, subjects: list[str], limit: int = 10) -> list[WorkflowEvent]:
        now = utcnow()
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(WorkflowEventORM)
                .where(
                    WorkflowEventORM.subject.in_(subjects),
                    WorkflowEventORM.status == "pending",
                    WorkflowEventORM.available_at <= now,
                )
                .order_by(WorkflowEventORM.created_at.asc())
                .limit(limit)
            ).all()
            events: list[WorkflowEvent] = []
            for row in rows:
                row.status = "processing"
                row.attempts += 1
                row.updated_at = now
                session.add(row)
                events.append(self._to_event(row))
            return events

    def mark_completed(self, event_id: str) -> WorkflowEvent | None:
        return self._update_status(event_id, status="completed")

    def mark_failed(self, event_id: str, error: str, retryable: bool = False) -> WorkflowEvent | None:
        status = "pending" if retryable else "failed"
        return self._update_status(event_id, status=status, error=error)

    def list_events(
        self,
        subjects: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> list[WorkflowEvent]:
        with session_scope(self.session_factory) as session:
            stmt = select(WorkflowEventORM)
            if subjects:
                stmt = stmt.where(WorkflowEventORM.subject.in_(subjects))
            if statuses:
                stmt = stmt.where(WorkflowEventORM.status.in_(statuses))
            rows = session.scalars(stmt.order_by(WorkflowEventORM.created_at.asc())).all()
            return [self._to_event(row) for row in rows]

    def _update_status(self, event_id: str, status: str, error: str | None = None) -> WorkflowEvent | None:
        with session_scope(self.session_factory) as session:
            row = session.get(WorkflowEventORM, event_id)
            if row is None:
                return None
            row.status = status
            row.last_error = error
            row.updated_at = utcnow()
            session.add(row)
            session.flush()
            session.refresh(row)
            return self._to_event(row)

    @staticmethod
    def _to_event(row: WorkflowEventORM) -> WorkflowEvent:
        return WorkflowEvent(
            event_id=row.event_id,
            subject=row.subject,
            payload=row.payload,
            status=row.status,
            attempts=row.attempts,
            available_at=row.available_at,
            last_error=row.last_error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
