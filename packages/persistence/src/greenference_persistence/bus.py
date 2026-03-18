from __future__ import annotations

import asyncio
import json
import importlib.util
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from greenference_persistence.db import (
    create_db_engine,
    create_session_factory,
    init_database,
    needs_bootstrap,
    session_scope,
)
from greenference_persistence.orm import BusDeliveryORM, WorkflowEventORM
from greenference_persistence.workflow import WorkflowEvent, WorkflowEventRepository


def utcnow() -> datetime:
    return datetime.now(UTC)


SUBJECT_CONSUMERS: dict[str, list[str]] = {
    "build.accepted": ["builder-worker"],
    "deployment.requested": ["control-plane-worker"],
    "invocation.recorded": ["control-plane-worker"],
    "usage.recorded": ["control-plane-worker"],
    "probe.result.recorded": ["validator-worker"],
    "validator.weights.published": ["validator-worker"],
}


class BusMessage(BaseModel):
    delivery_id: int
    event_id: str
    consumer: str
    subject: str
    payload: dict[str, Any]
    attempts: int = 0
    available_at: datetime = Field(default_factory=utcnow)
    last_error: str | None = None


class SubjectBus:
    def __init__(
        self,
        database_url: str | None = None,
        bootstrap: bool | None = None,
        engine: Engine | None = None,
        session_factory: sessionmaker[Session] | None = None,
        workflow_repository: WorkflowEventRepository | None = None,
    ) -> None:
        resolved_engine = engine
        resolved_session_factory = session_factory
        if workflow_repository is not None and resolved_engine is None and resolved_session_factory is None:
            resolved_engine = workflow_repository.engine
            resolved_session_factory = workflow_repository.session_factory
        self.engine = resolved_engine or create_db_engine(database_url)
        self.session_factory = resolved_session_factory or create_session_factory(self.engine)
        if engine is None and session_factory is None and needs_bootstrap(str(self.engine.url), bootstrap):
            init_database(self.engine)
        self.workflow_repository = workflow_repository or WorkflowEventRepository(
            engine=self.engine,
            session_factory=self.session_factory,
        )

    @property
    def active_transport(self) -> str:
        return "durable"

    def publish(self, subject: str, payload: dict[str, Any]) -> WorkflowEvent:
        event = self.workflow_repository.publish(subject, payload)
        consumers = SUBJECT_CONSUMERS.get(subject, [])
        if not consumers:
            return event
        with session_scope(self.session_factory) as session:
            for consumer in consumers:
                session.add(
                    BusDeliveryORM(
                        event_id=event.event_id,
                        consumer=consumer,
                        subject=subject,
                        status="pending",
                        attempts=0,
                        available_at=event.available_at,
                        last_error=None,
                        created_at=event.created_at,
                        updated_at=event.updated_at,
                    )
                )
        return event

    def claim_pending(self, consumer: str, subjects: list[str], limit: int = 10) -> list[BusMessage]:
        now = utcnow()
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(BusDeliveryORM)
                .where(
                    BusDeliveryORM.consumer == consumer,
                    BusDeliveryORM.subject.in_(subjects),
                    BusDeliveryORM.status == "pending",
                    BusDeliveryORM.available_at <= now,
                )
                .order_by(BusDeliveryORM.created_at.asc())
                .limit(limit)
            ).all()
            messages: list[BusMessage] = []
            for row in rows:
                row.status = "processing"
                row.attempts += 1
                row.updated_at = now
                session.add(row)
                event_row = session.scalar(select(WorkflowEventORM).where(WorkflowEventORM.event_id == row.event_id))
                if event_row is None:
                    continue
                messages.append(self._to_message(row, event_row))
            return messages

    def mark_completed(self, delivery_id: int) -> BusMessage | None:
        return self._update_status(delivery_id, status="completed")

    def mark_failed(
        self,
        delivery_id: int,
        error: str,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> BusMessage | None:
        status = "pending" if retryable else "failed"
        return self._update_status(delivery_id, status=status, error=error, retry_after_seconds=retry_after_seconds)

    def list_deliveries(
        self,
        consumer: str | None = None,
        subjects: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> list[BusMessage]:
        with session_scope(self.session_factory) as session:
            stmt = select(BusDeliveryORM)
            if consumer:
                stmt = stmt.where(BusDeliveryORM.consumer == consumer)
            if subjects:
                stmt = stmt.where(BusDeliveryORM.subject.in_(subjects))
            if statuses:
                stmt = stmt.where(BusDeliveryORM.status.in_(statuses))
            rows = session.scalars(stmt.order_by(BusDeliveryORM.created_at.asc())).all()
            messages: list[BusMessage] = []
            for row in rows:
                event_row = session.scalar(select(WorkflowEventORM).where(WorkflowEventORM.event_id == row.event_id))
                if event_row is None:
                    continue
                messages.append(self._to_message(row, event_row))
            return messages

    def requeue_stale_processing(
        self,
        consumer: str,
        subjects: list[str],
        *,
        stale_after_seconds: float,
    ) -> list[BusMessage]:
        threshold = utcnow() - timedelta(seconds=stale_after_seconds)
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(BusDeliveryORM).where(
                    BusDeliveryORM.consumer == consumer,
                    BusDeliveryORM.subject.in_(subjects),
                    BusDeliveryORM.status == "processing",
                    BusDeliveryORM.updated_at <= threshold,
                )
            ).all()
            messages: list[BusMessage] = []
            for row in rows:
                row.status = "pending"
                row.last_error = "requeued after stale processing delivery"
                row.available_at = utcnow()
                row.updated_at = utcnow()
                session.add(row)
                event_row = session.scalar(select(WorkflowEventORM).where(WorkflowEventORM.event_id == row.event_id))
                if event_row is None:
                    continue
                event_row.status = "pending"
                event_row.last_error = row.last_error
                event_row.available_at = row.available_at
                event_row.updated_at = row.updated_at
                session.add(event_row)
                messages.append(self._to_message(row, event_row))
            return messages

    def _update_status(
        self,
        delivery_id: int,
        status: str,
        error: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> BusMessage | None:
        with session_scope(self.session_factory) as session:
            row = session.get(BusDeliveryORM, delivery_id)
            if row is None:
                return None
            row.status = status
            row.last_error = error
            if retry_after_seconds is not None:
                row.available_at = utcnow() + timedelta(seconds=retry_after_seconds)
            row.updated_at = utcnow()
            session.add(row)
            session.flush()
            event_row = session.scalar(select(WorkflowEventORM).where(WorkflowEventORM.event_id == row.event_id))
            if event_row is None:
                return None
            event_row.status = status
            event_row.last_error = error
            if retry_after_seconds is not None:
                event_row.available_at = row.available_at
            event_row.updated_at = row.updated_at
            session.add(event_row)
            return self._to_message(row, event_row)

    @staticmethod
    def _to_message(row: BusDeliveryORM, event_row: WorkflowEventORM) -> BusMessage:
        return BusMessage(
            delivery_id=row.id,
            event_id=row.event_id,
            consumer=row.consumer,
            subject=row.subject,
            payload=event_row.payload,
            attempts=row.attempts,
            available_at=row.available_at,
            last_error=row.last_error,
        )


class NatsJetStreamBus:
    def __init__(
        self,
        durable_bus: SubjectBus,
        nats_url: str,
        enabled: bool = True,
    ) -> None:
        self.durable_bus = durable_bus
        self.nats_url = nats_url
        self.enabled = enabled
        self.client_available = importlib.util.find_spec("nats") is not None
        self._pending_messages: dict[int, Any] = {}

    @property
    def active_transport(self) -> str:
        return "nats" if self.enabled and self.client_available else "durable"

    def publish(self, subject: str, payload: dict[str, Any]) -> WorkflowEvent:
        event = self.durable_bus.publish(subject, payload)
        if self.active_transport == "nats":
            try:
                self._publish_to_nats(subject, event)
            except Exception:
                pass
        return event

    def claim_pending(self, consumer: str, subjects: list[str], limit: int = 10) -> list[BusMessage]:
        if self.active_transport != "nats":
            return self.durable_bus.claim_pending(consumer, subjects, limit=limit)
        try:
            messages = self._claim_pending_from_nats(consumer, subjects, limit=limit)
        except Exception:
            return self.durable_bus.claim_pending(consumer, subjects, limit=limit)
        if messages:
            return messages
        return self.durable_bus.claim_pending(consumer, subjects, limit=limit)

    def mark_completed(self, delivery_id: int) -> BusMessage | None:
        message = self._pending_messages.pop(delivery_id, None)
        if message is not None:
            self._ack_message(message)
        return self.durable_bus.mark_completed(delivery_id)

    def mark_failed(
        self,
        delivery_id: int,
        error: str,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> BusMessage | None:
        message = self._pending_messages.pop(delivery_id, None)
        if message is not None:
            self._fail_message(message, retryable=retryable)
        return self.durable_bus.mark_failed(
            delivery_id,
            error,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )

    def list_deliveries(
        self,
        consumer: str | None = None,
        subjects: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> list[BusMessage]:
        return self.durable_bus.list_deliveries(consumer=consumer, subjects=subjects, statuses=statuses)

    def requeue_stale_processing(
        self,
        consumer: str,
        subjects: list[str],
        *,
        stale_after_seconds: float,
    ) -> list[BusMessage]:
        return self.durable_bus.requeue_stale_processing(
            consumer,
            subjects,
            stale_after_seconds=stale_after_seconds,
        )

    def _publish_to_nats(self, subject: str, event: WorkflowEvent) -> None:
        self._run_async(self._publish_to_nats_async(subject, event))

    async def _publish_to_nats_async(self, subject: str, event: WorkflowEvent) -> None:
        client = await self._connect()
        try:
            jetstream = client.jetstream()
            await self._ensure_stream(jetstream)
            await jetstream.publish(
                subject,
                json.dumps({"event_id": event.event_id, "subject": subject}).encode("utf-8"),
                headers={"event_id": event.event_id, "subject": subject},
            )
        finally:
            await client.close()

    def _claim_pending_from_nats(self, consumer: str, subjects: list[str], limit: int = 10) -> list[BusMessage]:
        return self._run_async(self._claim_pending_from_nats_async(consumer, subjects, limit))

    async def _claim_pending_from_nats_async(
        self,
        consumer: str,
        subjects: list[str],
        limit: int = 10,
    ) -> list[BusMessage]:
        client = await self._connect()
        messages: list[BusMessage] = []
        try:
            jetstream = client.jetstream()
            await self._ensure_stream(jetstream)
            for subject in subjects:
                if len(messages) >= limit:
                    break
                subscription = await jetstream.pull_subscribe(
                    subject,
                    durable=self._durable_name(consumer, subject),
                    stream=self._stream_name(),
                )
                remaining = limit - len(messages)
                try:
                    raw_messages = await subscription.fetch(batch=remaining, timeout=0.05)
                except Exception:
                    continue
                for raw_message in raw_messages:
                    claimed = self._claim_delivery_for_message(consumer, subject, raw_message)
                    if claimed is None:
                        self._ack_message(raw_message)
                        continue
                    messages.append(claimed)
                    self._pending_messages[claimed.delivery_id] = raw_message
                    if len(messages) >= limit:
                        break
            return messages
        finally:
            await client.close()

    def _claim_delivery_for_message(self, consumer: str, subject: str, raw_message: Any) -> BusMessage | None:
        event_id = self._message_event_id(raw_message)
        if not event_id:
            return None
        now = utcnow()
        with session_scope(self.durable_bus.session_factory) as session:
            row = session.scalar(
                select(BusDeliveryORM).where(
                    BusDeliveryORM.event_id == event_id,
                    BusDeliveryORM.consumer == consumer,
                    BusDeliveryORM.subject == subject,
                    BusDeliveryORM.status == "pending",
                    BusDeliveryORM.available_at <= now,
                )
            )
            if row is None:
                return None
            row.status = "processing"
            row.attempts += 1
            row.updated_at = now
            session.add(row)
            event_row = session.scalar(select(WorkflowEventORM).where(WorkflowEventORM.event_id == row.event_id))
            if event_row is None:
                return None
            return self.durable_bus._to_message(row, event_row)

    @staticmethod
    def _message_event_id(raw_message: Any) -> str | None:
        headers = getattr(raw_message, "headers", None) or {}
        event_id = headers.get("event_id")
        if event_id:
            return str(event_id)
        try:
            payload = json.loads(getattr(raw_message, "data", b"{}").decode("utf-8"))
        except Exception:
            return None
        resolved = payload.get("event_id")
        return str(resolved) if resolved is not None else None

    @staticmethod
    def _stream_name() -> str:
        return "GREENFERENCE"

    @staticmethod
    def _durable_name(consumer: str, subject: str) -> str:
        normalized_subject = subject.replace(".", "-")
        return f"{consumer}-{normalized_subject}"

    async def _connect(self) -> Any:
        import nats

        return await nats.connect(servers=[self.nats_url], connect_timeout=1)

    async def _ensure_stream(self, jetstream: Any) -> None:
        try:
            await jetstream.add_stream(name=self._stream_name(), subjects=list(SUBJECT_CONSUMERS))
        except Exception:
            return

    @staticmethod
    def _run_async(coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result: dict[str, Any] = {}
        error: dict[str, BaseException] = {}

        def _runner() -> None:
            try:
                result["value"] = asyncio.run(coro)
            except BaseException as exc:  # noqa: BLE001
                error["value"] = exc

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        thread.join()
        if "value" in error:
            raise error["value"]
        return result.get("value")

    def _ack_message(self, raw_message: Any) -> None:
        if hasattr(raw_message, "ack"):
            self._run_async(raw_message.ack())

    def _fail_message(self, raw_message: Any, retryable: bool) -> None:
        if retryable and hasattr(raw_message, "nak"):
            self._run_async(raw_message.nak())
            return
        if hasattr(raw_message, "term"):
            self._run_async(raw_message.term())
            return
        self._ack_message(raw_message)


def create_subject_bus(
    *,
    database_url: str | None = None,
    bootstrap: bool | None = None,
    engine: Engine | None = None,
    session_factory: sessionmaker[Session] | None = None,
    workflow_repository: WorkflowEventRepository | None = None,
    nats_url: str = "nats://127.0.0.1:4222",
    transport: str = "auto",
):
    durable = SubjectBus(
        database_url=database_url,
        bootstrap=bootstrap,
        engine=engine,
        session_factory=session_factory,
        workflow_repository=workflow_repository,
    )
    if transport == "durable":
        return durable
    nats_bus = NatsJetStreamBus(durable_bus=durable, nats_url=nats_url, enabled=transport in {"auto", "nats"})
    if transport == "nats" and not nats_bus.client_available:
        return nats_bus
    if transport == "auto" and not nats_bus.client_available:
        return durable
    return nats_bus
