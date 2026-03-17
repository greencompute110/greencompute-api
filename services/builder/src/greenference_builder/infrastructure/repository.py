from __future__ import annotations

from sqlalchemy import select

from greenference_persistence import create_db_engine, create_session_factory, init_database, session_scope
from greenference_persistence.db import needs_bootstrap
from greenference_persistence.orm import BuildAttemptORM, BuildContextORM, BuildEventORM, BuildLogORM, BuildORM
from greenference_protocol import (
    BuildAttemptRecord,
    BuildContextRecord,
    BuildEventRecord,
    BuildLogRecord,
    BuildRecord,
)


class BuilderRepository:
    def __init__(self, database_url: str | None = None, bootstrap: bool | None = None) -> None:
        self.engine = create_db_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        if needs_bootstrap(str(self.engine.url), bootstrap):
            init_database(self.engine)

    def save_build(self, build: BuildRecord) -> BuildRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(BuildORM, build.build_id) or BuildORM(build_id=build.build_id)
            row.image = build.image
            row.context_uri = build.context_uri
            row.dockerfile_path = build.dockerfile_path
            row.public = build.public
            row.status = build.status
            row.registry_repository = build.registry_repository
            row.image_tag = build.image_tag
            row.artifact_uri = build.artifact_uri
            row.artifact_digest = build.artifact_digest
            row.registry_manifest_uri = build.registry_manifest_uri
            row.build_log_uri = build.build_log_uri
            row.executor_name = build.executor_name
            row.build_duration_seconds = build.build_duration_seconds
            row.failure_reason = build.failure_reason
            row.failure_class = build.failure_class
            row.last_operation = build.last_operation
            row.cleanup_status = build.cleanup_status
            row.retry_count = build.retry_count
            row.retry_exhausted = build.retry_exhausted
            row.created_at = build.created_at
            row.updated_at = build.updated_at
            session.add(row)
        return build

    def save_build_context(self, context: BuildContextRecord) -> BuildContextRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(BuildContextORM, context.build_id) or BuildContextORM(build_id=context.build_id)
            row.source_uri = context.source_uri
            row.normalized_context_uri = context.normalized_context_uri
            row.dockerfile_path = context.dockerfile_path
            row.dockerfile_object_uri = context.dockerfile_object_uri
            row.context_digest = context.context_digest
            row.staged_context_uri = context.staged_context_uri
            row.context_manifest_uri = context.context_manifest_uri
            row.created_at = context.created_at
            session.add(row)
        return context

    def get_build_context(self, build_id: str) -> BuildContextRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(BuildContextORM, build_id)
            if row is None:
                return None
            return BuildContextRecord(
                build_id=row.build_id,
                source_uri=row.source_uri,
                normalized_context_uri=row.normalized_context_uri,
                dockerfile_path=row.dockerfile_path,
                dockerfile_object_uri=row.dockerfile_object_uri,
                context_digest=row.context_digest,
                staged_context_uri=row.staged_context_uri,
                context_manifest_uri=row.context_manifest_uri,
                created_at=row.created_at,
            )

    def add_build_event(self, event: BuildEventRecord) -> BuildEventRecord:
        with session_scope(self.session_factory) as session:
            session.add(
                BuildEventORM(
                    event_id=event.event_id,
                    build_id=event.build_id,
                    stage=event.stage,
                    message=event.message,
                    created_at=event.created_at,
                )
            )
        return event

    def save_build_attempt(self, attempt: BuildAttemptRecord) -> BuildAttemptRecord:
        with session_scope(self.session_factory) as session:
            row = session.get(BuildAttemptORM, attempt.attempt_id) or BuildAttemptORM(attempt_id=attempt.attempt_id)
            row.build_id = attempt.build_id
            row.attempt = attempt.attempt
            row.status = attempt.status
            row.failure_class = attempt.failure_class
            row.last_operation = attempt.last_operation
            row.started_at = attempt.started_at
            row.finished_at = attempt.finished_at
            session.add(row)
        return attempt

    def get_build_attempt(self, build_id: str, attempt: int) -> BuildAttemptRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.scalar(
                select(BuildAttemptORM)
                .where(BuildAttemptORM.build_id == build_id, BuildAttemptORM.attempt == attempt)
                .order_by(BuildAttemptORM.started_at.desc())
            )
            return self._to_build_attempt(row) if row is not None else None

    def list_build_attempts(self, build_id: str) -> list[BuildAttemptRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(BuildAttemptORM)
                .where(BuildAttemptORM.build_id == build_id)
                .order_by(BuildAttemptORM.attempt.asc(), BuildAttemptORM.started_at.asc())
            ).all()
            return [self._to_build_attempt(row) for row in rows]

    def add_build_log(self, log: BuildLogRecord) -> BuildLogRecord:
        with session_scope(self.session_factory) as session:
            session.add(
                BuildLogORM(
                    log_id=log.log_id,
                    build_id=log.build_id,
                    attempt=log.attempt,
                    stage=log.stage,
                    message=log.message,
                    created_at=log.created_at,
                )
            )
        return log

    def list_build_logs(self, build_id: str, attempt: int | None = None) -> list[BuildLogRecord]:
        with session_scope(self.session_factory) as session:
            stmt = select(BuildLogORM).where(BuildLogORM.build_id == build_id)
            if attempt is not None:
                stmt = stmt.where(BuildLogORM.attempt == attempt)
            rows = session.scalars(stmt.order_by(BuildLogORM.created_at.asc())).all()
            return [
                BuildLogRecord(
                    log_id=row.log_id,
                    build_id=row.build_id,
                    attempt=row.attempt,
                    stage=row.stage,
                    message=row.message,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    def list_build_events(self, build_id: str) -> list[BuildEventRecord]:
        with session_scope(self.session_factory) as session:
            rows = session.scalars(
                select(BuildEventORM).where(BuildEventORM.build_id == build_id).order_by(BuildEventORM.created_at.asc())
            ).all()
            return [
                BuildEventRecord(
                    event_id=row.event_id,
                    build_id=row.build_id,
                    stage=row.stage,
                    message=row.message,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    def get_build(self, build_id: str) -> BuildRecord | None:
        with session_scope(self.session_factory) as session:
            row = session.get(BuildORM, build_id)
            if row is None:
                return None
            return BuildRecord(
                build_id=row.build_id,
                image=row.image,
                context_uri=row.context_uri,
                dockerfile_path=row.dockerfile_path,
                public=row.public,
                status=row.status,
                registry_repository=row.registry_repository,
                image_tag=row.image_tag,
                artifact_uri=row.artifact_uri,
                artifact_digest=row.artifact_digest,
                registry_manifest_uri=row.registry_manifest_uri,
                build_log_uri=row.build_log_uri,
                executor_name=row.executor_name,
                build_duration_seconds=row.build_duration_seconds,
                failure_reason=row.failure_reason,
                failure_class=row.failure_class,
                last_operation=row.last_operation,
                cleanup_status=row.cleanup_status,
                retry_count=row.retry_count,
                retry_exhausted=row.retry_exhausted,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    def list_builds(self, image: str | None = None) -> list[BuildRecord]:
        with session_scope(self.session_factory) as session:
            stmt = select(BuildORM).order_by(BuildORM.created_at.desc())
            if image is not None:
                stmt = stmt.where(BuildORM.image == image)
            rows = session.scalars(stmt).all()
            return [
                BuildRecord(
                    build_id=row.build_id,
                    image=row.image,
                    context_uri=row.context_uri,
                    dockerfile_path=row.dockerfile_path,
                    public=row.public,
                    status=row.status,
                    registry_repository=row.registry_repository,
                    image_tag=row.image_tag,
                    artifact_uri=row.artifact_uri,
                    artifact_digest=row.artifact_digest,
                    registry_manifest_uri=row.registry_manifest_uri,
                    build_log_uri=row.build_log_uri,
                    executor_name=row.executor_name,
                    build_duration_seconds=row.build_duration_seconds,
                    failure_reason=row.failure_reason,
                    failure_class=row.failure_class,
                    last_operation=row.last_operation,
                    cleanup_status=row.cleanup_status,
                    retry_count=row.retry_count,
                    retry_exhausted=row.retry_exhausted,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ]

    @staticmethod
    def _to_build_attempt(row: BuildAttemptORM) -> BuildAttemptRecord:
        return BuildAttemptRecord(
            attempt_id=row.attempt_id,
            build_id=row.build_id,
            attempt=row.attempt,
            status=row.status,
            failure_class=row.failure_class,
            last_operation=row.last_operation,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )
