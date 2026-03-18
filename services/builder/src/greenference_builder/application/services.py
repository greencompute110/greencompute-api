from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from greenference_persistence import (
    SubjectBus,
    WorkflowEventRepository,
    create_subject_bus,
    get_metrics_store,
    load_runtime_settings,
)
from greenference_protocol import (
    BuildAttemptRecord,
    BuildContextRecord,
    BuildContextUploadRecord,
    BuildContextUploadRequest,
    BuildEventRecord,
    BuildJobCheckpointRecord,
    BuildJobRecord,
    BuildLogRecord,
    BuildRecord,
    BuildRequest,
)
from greenference_builder.infrastructure.execution import (
    AdapterBackedBuildRunner,
    BuildRunner,
    BuildStageResult,
    BuilderExecutionError,
    ObjectStoreAdapter,
    RegistryAdapter,
    create_execution_adapters,
)
from greenference_builder.infrastructure.repository import BuilderRepository


class BuilderService:
    def __init__(
        self,
        repository: BuilderRepository | None = None,
        workflow_repository: WorkflowEventRepository | None = None,
        bus: SubjectBus | None = None,
        object_store: ObjectStoreAdapter | None = None,
        registry: RegistryAdapter | None = None,
        runner: BuildRunner | None = None,
    ) -> None:
        self.repository = repository or BuilderRepository()
        self.workflow_repository = workflow_repository or WorkflowEventRepository(
            engine=self.repository.engine,
            session_factory=self.repository.session_factory,
        )
        self.settings = load_runtime_settings("greenference-builder")
        self.bus = bus or create_subject_bus(
            engine=self.workflow_repository.engine,
            session_factory=self.workflow_repository.session_factory,
            workflow_repository=self.workflow_repository,
            nats_url=self.settings.nats_url,
            transport=self.settings.bus_transport,
        )
        default_object_store, default_registry = create_execution_adapters(self.settings)
        self.object_store = object_store or default_object_store
        self.registry = registry or default_registry
        self.runner = runner or AdapterBackedBuildRunner(self.object_store, self.registry)
        self.metrics = get_metrics_store("greenference-builder")
        self._recovery_state: dict[str, object | None] = {
            "last_recovery_at": None,
            "last_recovery_error": None,
            "requeued_deliveries": 0,
            "republished_jobs": 0,
            "recovered_build_ids": [],
        }

    def start_build(self, request: BuildRequest, *, owner_user_id: str | None = None) -> BuildRecord:
        context_uri = self._materialize_context_source(request)
        build = BuildRecord(
            image=request.image,
            owner_user_id=owner_user_id,
            context_uri=context_uri,
            dockerfile_path=request.dockerfile_path,
            display_name=request.display_name,
            readme=request.readme,
            logo_uri=request.logo_uri,
            tags=request.tags,
            public=request.public,
            status="accepted",
            artifact_uri=None,
        )
        build.updated_at = datetime.now(UTC)
        saved = self.repository.save_build(build)
        self.repository.save_build_context(
            BuildContextRecord(
                build_id=saved.build_id,
                source_uri=context_uri,
                normalized_context_uri=self._normalized_context_uri(context_uri),
                dockerfile_path=request.dockerfile_path,
                dockerfile_object_uri=self._dockerfile_object_uri(context_uri, request.dockerfile_path),
                context_digest=self._context_digest(saved.build_id, context_uri, request.dockerfile_path),
            )
        )
        self.repository.add_build_event(
            BuildEventRecord(
                build_id=saved.build_id,
                stage="accepted",
                message="build request accepted and queued",
            )
        )
        self.bus.publish(
            "build.accepted",
            {
                "build_id": saved.build_id,
                "image": saved.image,
                "attempt": 1,
            },
        )
        self.metrics.increment("build.accepted")
        return saved

    def upload_build_context(self, request: BuildContextUploadRequest) -> BuildContextUploadRecord:
        context_uri, size_bytes = self._write_context_archive(
            request.context_archive_b64,
            request.context_archive_name,
        )
        return BuildContextUploadRecord(
            context_uri=context_uri,
            archive_name=request.context_archive_name,
            size_bytes=size_bytes,
        )

    def list_builds(self) -> list[BuildRecord]:
        return self.repository.list_builds()

    def get_build(self, build_id: str) -> BuildRecord | None:
        return self.repository.get_build(build_id)

    def get_build_context(self, build_id: str) -> BuildContextRecord | None:
        return self.repository.get_build_context(build_id)

    def list_build_events(self, build_id: str) -> list[BuildEventRecord]:
        return self.repository.list_build_events(build_id)

    def list_build_logs(self, build_id: str) -> list[BuildLogRecord]:
        return self.repository.list_build_logs(build_id)

    def stream_build_logs(self, build_id: str, *, follow: bool = False, poll_interval: float = 0.5) -> Iterator[str]:
        seen_ids: set[str] = set()
        while True:
            logs = self.repository.list_build_logs(build_id)
            for log in logs:
                if log.log_id in seen_ids:
                    continue
                seen_ids.add(log.log_id)
                yield f"data: {json.dumps(log.model_dump(mode='json'))}\n\n"
            build = self.repository.get_build(build_id)
            if build is None:
                yield "event: error\ndata: {\"detail\":\"build not found\"}\n\n"
                return
            if not follow or build.status in {"published", "failed", "cancelled"}:
                yield f"event: end\ndata: {json.dumps({'status': build.status, 'build_id': build_id})}\n\n"
                return
            time.sleep(poll_interval)

    def get_build_attempt(self, build_id: str, attempt: int) -> BuildAttemptRecord | None:
        return self.repository.get_build_attempt(build_id, attempt)

    def get_build_job(self, build_id: str, attempt: int | None = None) -> BuildJobRecord | None:
        return self.repository.get_build_job(build_id, attempt=attempt)

    def list_build_jobs(self, build_id: str) -> list[BuildJobRecord]:
        return self.repository.list_build_jobs(build_id)

    def latest_build_job_timeline(self, build_id: str) -> list[BuildJobCheckpointRecord]:
        latest_job = self.repository.get_build_job(build_id)
        if latest_job is None:
            return []
        return self.repository.list_build_job_checkpoints(
            build_id,
            attempt=latest_job.attempt,
            job_id=latest_job.job_id,
        )

    def latest_build_job_recovery_summary(self, build_id: str) -> dict[str, object]:
        job = self.repository.get_build_job(build_id)
        if job is None:
            raise KeyError(f"build job not found: {build_id}")
        resumed_from_state = bool(job.recovery_count or str(job.stage_state.get("recovered", "")).lower() == "true")
        return {
            "build_id": build_id,
            "job_id": job.job_id,
            "attempt": job.attempt,
            "status": job.status,
            "current_stage": job.current_stage,
            "last_completed_stage": job.last_completed_stage,
            "recovery_count": job.recovery_count,
            "last_recovered_at": job.last_recovered_at,
            "resumed_from_persisted_state": resumed_from_state,
            "resumed_stage": job.current_stage if resumed_from_state else None,
            "reused_stage_outputs": bool(job.stage_state),
            "stage_state": job.stage_state,
        }

    def recovery_status(self) -> dict[str, object | None]:
        return dict(self._recovery_state)

    def restart_latest_job(self, build_id: str) -> BuildRecord:
        latest_job = self.repository.get_build_job(build_id)
        if latest_job is None:
            raise KeyError(f"build job not found: {build_id}")
        if latest_job.finished_at is None and latest_job.status not in {"failed", "cancelled"}:
            raise ValueError("running job cannot be restarted")
        return self.retry_build(
            build_id,
            restarted_from_job=latest_job,
            restart_reason="latest_job_restart",
        )

    def cancel_latest_job(self, build_id: str) -> BuildRecord:
        latest_job = self.repository.get_build_job(build_id)
        if latest_job is None:
            raise KeyError(f"build job not found: {build_id}")
        return self.cancel_build(build_id)

    def recover_inflight_jobs(self) -> dict[str, object | None]:
        stale_after_seconds = max(self.settings.worker_poll_interval_seconds * 4, 2.0)
        requeued = self.bus.requeue_stale_processing(
            "builder-worker",
            ["build.accepted", "build.job.progress"],
            stale_after_seconds=stale_after_seconds,
        )
        republished_build_ids: list[str] = []
        for build in self.repository.list_builds():
            if build.status not in {"accepted", "staging", "building", "publishing"}:
                continue
            latest_job = self.repository.get_build_job(build.build_id)
            if latest_job is None:
                if build.status == "accepted" and not self._has_active_delivery(build.build_id, None, "build.accepted"):
                    self.bus.publish(
                        "build.accepted",
                        {"build_id": build.build_id, "image": build.image, "attempt": build.retry_count + 1},
                    )
                    republished_build_ids.append(build.build_id)
                continue
            if latest_job.finished_at is not None or latest_job.status not in {"queued", "running"}:
                continue
            if self._has_active_delivery(build.build_id, latest_job.attempt, "build.job.progress"):
                continue
            recovered_at = datetime.now(UTC)
            latest_job.status = "queued"
            latest_job.recovery_count += 1
            latest_job.last_recovered_at = recovered_at
            latest_job.progress_message = f"recovered build job at stage {latest_job.current_stage}"
            latest_job.stage_state = {
                **latest_job.stage_state,
                "recovered": "true",
                "recovered_at": recovered_at.isoformat(),
            }
            latest_job.updated_at = recovered_at
            self.repository.save_build_job(latest_job)
            self._record_job_checkpoint(
                latest_job,
                stage=latest_job.current_stage,
                status="recovered",
                message=latest_job.progress_message,
                recovered=True,
                created_at=recovered_at,
            )
            self.repository.add_build_event(
                BuildEventRecord(
                    build_id=build.build_id,
                    stage="recovered",
                    message=latest_job.progress_message,
                )
            )
            self.repository.add_build_log(
                BuildLogRecord(
                    build_id=build.build_id,
                    attempt=latest_job.attempt,
                    stage="recovered",
                    message=latest_job.progress_message,
                )
            )
            self.bus.publish(
                "build.job.progress",
                {
                    "build_id": build.build_id,
                    "attempt": latest_job.attempt,
                    "stage": latest_job.current_stage,
                    "recovered": True,
                },
            )
            republished_build_ids.append(build.build_id)

        self._recovery_state = {
            "last_recovery_at": datetime.now(UTC),
            "last_recovery_error": None,
            "requeued_deliveries": len(requeued),
            "republished_jobs": len(republished_build_ids),
            "recovered_build_ids": republished_build_ids,
        }
        return self.recovery_status()

    def list_image_history(self, image: str) -> list[BuildRecord]:
        return self.repository.list_builds(image=image)

    def build_attempts(self, build_id: str) -> list[dict]:
        attempts = self.repository.list_build_attempts(build_id)
        return [
            {
                **attempt.model_dump(mode="json"),
                "logs": [
                    log.model_dump(mode="json")
                    for log in self.repository.list_build_logs(build_id, attempt=attempt.attempt)
                ],
            }
            for attempt in attempts
        ]

    def retry_build(
        self,
        build_id: str,
        *,
        restarted_from_job: BuildJobRecord | None = None,
        restart_reason: str | None = None,
    ) -> BuildRecord:
        build = self.repository.get_build(build_id)
        if build is None:
            raise KeyError(f"build not found: {build_id}")
        if build.status == "published":
            return build
        if build.status == "cancelled":
            raise ValueError("cancelled build cannot be retried")
        context = self.repository.get_build_context(build_id)
        self.cleanup_build(build_id, persist=False)
        build.status = "accepted"
        build.retry_count += 1
        build.failure_reason = None
        build.failure_class = None
        build.last_operation = "retry_requested"
        build.cleanup_status = None
        build.retry_exhausted = False
        build.registry_repository = None
        build.image_tag = None
        build.artifact_uri = None
        build.artifact_digest = None
        build.registry_manifest_uri = None
        build.build_log_uri = self.runner.build_log_uri(build_id)
        build.executor_name = None
        build.updated_at = datetime.now(UTC)
        self.repository.save_build(build)
        if context is not None:
            context.staged_context_uri = None
            context.context_manifest_uri = None
            self.repository.save_build_context(context)
        self.repository.add_build_event(
            BuildEventRecord(
                build_id=build.build_id,
                stage="retry_requested",
                message=(
                    f"build restart requested from attempt {restarted_from_job.attempt}"
                    if restarted_from_job is not None
                    else "build retry requested"
                ),
            )
        )
        self.bus.publish(
            "build.accepted",
            {
                "build_id": build.build_id,
                "image": build.image,
                "attempt": build.retry_count + 1,
                "restarted_from_attempt": restarted_from_job.attempt if restarted_from_job is not None else None,
                "restarted_from_job_id": restarted_from_job.job_id if restarted_from_job is not None else None,
                "restart_reason": restart_reason,
            },
        )
        self.metrics.increment("build.retry_requested")
        return build

    def cancel_build(self, build_id: str) -> BuildRecord:
        build = self.repository.get_build(build_id)
        if build is None:
            raise KeyError(f"build not found: {build_id}")
        if build.status in {"published", "failed", "cancelled"}:
            return build
        build.status = "cancelled"
        build.failure_reason = "build cancelled by operator"
        build.failure_class = "cancelled"
        build.last_operation = "cancel"
        build.updated_at = datetime.now(UTC)
        self.repository.save_build(build)
        self.repository.add_build_event(
            BuildEventRecord(build_id=build.build_id, stage="cancelled", message="build cancelled by operator")
        )
        latest_attempt = self.repository.get_build_attempt(build_id, build.retry_count + 1)
        if latest_attempt is not None and latest_attempt.finished_at is None:
            latest_attempt.status = "cancelled"
            latest_attempt.failure_class = "cancelled"
            latest_attempt.last_operation = "cancel"
            latest_attempt.finished_at = build.updated_at
            self.repository.save_build_attempt(latest_attempt)
        latest_job = self.repository.get_build_job(build_id, attempt=build.retry_count + 1)
        if latest_job is not None and latest_job.finished_at is None:
            latest_job.status = "cancelled"
            latest_job.current_stage = "cancelled"
            latest_job.failure_class = "cancelled"
            latest_job.progress_message = "operator cancelled build"
            latest_job.finished_at = build.updated_at
            latest_job.updated_at = build.updated_at
            self.repository.save_build_job(latest_job)
            self._record_job_checkpoint(
                latest_job,
                stage="cancelled",
                status="cancelled",
                message="operator cancelled build",
                created_at=build.updated_at,
            )
        self.repository.add_build_log(
            BuildLogRecord(
                build_id=build.build_id,
                attempt=max(1, build.retry_count + 1),
                stage="cancelled",
                message="operator cancelled build",
            )
        )
        self.metrics.increment("build.cancelled")
        return build

    def cleanup_build(self, build_id: str, *, persist: bool = True) -> BuildRecord:
        build = self.repository.get_build(build_id)
        if build is None:
            raise KeyError(f"build not found: {build_id}")
        context = self.repository.get_build_context(build_id)
        messages: list[str] = []
        messages.append(self.object_store.cleanup(build, context))
        messages.append(self.registry.cleanup(build))
        build.cleanup_status = "completed"
        build.last_operation = "cleanup"
        build.updated_at = datetime.now(UTC)
        if persist:
            self.repository.save_build(build)
            self.repository.add_build_event(
                BuildEventRecord(
                    build_id=build.build_id,
                    stage="cleanup",
                    message="; ".join(messages),
                )
            )
        return build

    def process_pending_events(self, limit: int = 10) -> list[BuildRecord]:
        processed: list[BuildRecord] = []
        remaining = limit
        while remaining > 0:
            events = self.bus.claim_pending("builder-worker", ["build.accepted", "build.job.progress"], limit=remaining)
            if not events:
                break
            remaining -= len(events)
            for event in events:
                build = self.repository.get_build(str(event.payload["build_id"]))
                if build is None:
                    self.bus.mark_failed(event.delivery_id, "build not found")
                    self.metrics.increment("build.failed")
                    continue
                try:
                    if build.status == "cancelled":
                        self.bus.mark_completed(event.delivery_id)
                        continue
                    if build.status == "published":
                        self.bus.mark_completed(event.delivery_id)
                        continue
                    if event.subject == "build.accepted":
                        self._initialize_or_resume_job(build, event)
                        self.bus.mark_completed(event.delivery_id)
                        continue
                    processed_build = self._advance_job(build, event)
                    if processed_build is not None:
                        processed.append(processed_build)
                except BuilderExecutionError as exc:
                    self._handle_job_failure(build, event, exc)
                except ValueError as exc:
                    self._handle_job_failure(
                        build,
                        event,
                        BuilderExecutionError(
                            str(exc),
                            operation="validate_context",
                            failure_class="build_validation_failure",
                            retryable=False,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    self._handle_job_failure(
                        build,
                        event,
                        BuilderExecutionError(
                            str(exc),
                            operation=build.last_operation or "unexpected",
                            failure_class="builder_runtime_error",
                            retryable=False,
                        ),
                    )
        pending_count = len(
            self.bus.list_deliveries(
                consumer="builder-worker",
                subjects=["build.accepted"],
                statuses=["pending"],
            )
        )
        self.metrics.set_gauge("workflow.pending.build.accepted", float(pending_count))
        self.metrics.set_gauge(
            "workflow.pending.build.job.progress",
            float(
                len(
                    self.bus.list_deliveries(
                        consumer="builder-worker",
                        subjects=["build.job.progress"],
                        statuses=["pending"],
                    )
                )
            ),
        )
        return processed

    def _has_active_delivery(self, build_id: str, attempt: int | None, subject: str) -> bool:
        deliveries = self.bus.list_deliveries(
            consumer="builder-worker",
            subjects=[subject],
            statuses=["pending", "processing"],
        )
        for delivery in deliveries:
            if str(delivery.payload.get("build_id")) != build_id:
                continue
            if attempt is not None and int(delivery.payload.get("attempt", -1)) != attempt:
                continue
            return True
        return False

    def _initialize_or_resume_job(self, build: BuildRecord, event) -> None:
        self._validate_context_uri(build.context_uri)
        context = self.repository.get_build_context(build.build_id)
        if context is None:
            raise ValueError("build context not found")
        attempt_number = int(event.payload.get("attempt", build.retry_count + 1))
        restarted_from_attempt = event.payload.get("restarted_from_attempt")
        restarted_from_job_id = event.payload.get("restarted_from_job_id")
        restart_reason = event.payload.get("restart_reason")
        attempt = self.repository.get_build_attempt(build.build_id, attempt_number)
        if attempt is None:
            attempt = BuildAttemptRecord(
                build_id=build.build_id,
                attempt=attempt_number,
                status="queued",
                restarted_from_attempt=int(restarted_from_attempt) if restarted_from_attempt is not None else None,
                restarted_from_job_id=str(restarted_from_job_id) if restarted_from_job_id is not None else None,
                restart_reason=str(restart_reason) if restart_reason is not None else None,
            )
            self.repository.save_build_attempt(attempt)
        job = self.repository.get_build_job(build.build_id, attempt=attempt_number)
        if job is None:
            preparation = self.runner.prepare_job(build, context)
            now = datetime.now(UTC)
            build.status = "staging"
            build.failure_reason = None
            build.failure_class = None
            build.executor_name = preparation.executor_name
            build.build_log_uri = preparation.log_uri
            build.last_operation = "prepare_job"
            build.updated_at = now
            self.repository.save_build(build)
            attempt.status = "queued"
            attempt.last_operation = "prepare_job"
            self.repository.save_build_attempt(attempt)
            job = BuildJobRecord(
                build_id=build.build_id,
                attempt=attempt_number,
                status="queued",
                current_stage=preparation.initial_stage,
                last_completed_stage=None,
                stage_state={
                    "log_uri": preparation.log_uri,
                    "prepared_message": preparation.message,
                },
                restarted_from_attempt=attempt.restarted_from_attempt,
                restarted_from_job_id=attempt.restarted_from_job_id,
                restart_reason=attempt.restart_reason,
                executor_name=preparation.executor_name,
                progress_message=preparation.message,
                started_at=now,
                updated_at=now,
            )
            self.repository.save_build_job(job)
            self._record_job_checkpoint(
                job,
                stage=job.current_stage,
                status="queued",
                message=(
                    f"{preparation.message} (restarted from attempt {job.restarted_from_attempt})"
                    if job.restarted_from_attempt is not None
                    else preparation.message
                ),
                created_at=now,
            )
            self.repository.add_build_event(
                BuildEventRecord(build_id=build.build_id, stage="job_started", message=preparation.message)
            )
            self.repository.add_build_log(
                BuildLogRecord(
                    build_id=build.build_id,
                    attempt=attempt_number,
                    stage="job_started",
                    message=preparation.message,
                )
            )
            self.bus.publish(
                "build.job.started",
                {
                    "build_id": build.build_id,
                    "attempt": attempt_number,
                    "stage": job.current_stage,
                    "executor_name": preparation.executor_name,
                },
            )
            self.metrics.increment("build.started")
        self.bus.publish(
            "build.job.progress",
            {
                "build_id": build.build_id,
                "attempt": attempt_number,
                "stage": job.current_stage,
            },
        )

    def _advance_job(self, build: BuildRecord, event) -> BuildRecord | None:
        context = self.repository.get_build_context(build.build_id)
        if context is None:
            raise ValueError("build context not found")
        attempt_number = int(event.payload.get("attempt", max(1, event.attempts)))
        attempt = self.repository.get_build_attempt(build.build_id, attempt_number)
        if attempt is None:
            raise ValueError("build attempt not found")
        job = self.repository.get_build_job(build.build_id, attempt=attempt_number)
        if job is None:
            raise ValueError("build job not found")
        if job.finished_at is not None or job.status in {"succeeded", "failed", "cancelled"}:
            self.bus.mark_completed(event.delivery_id)
            return None
        now = datetime.now(UTC)
        job.status = "running"
        job.stage_state = {
            **job.stage_state,
            "active_stage": job.current_stage,
            "active_stage_started_at": now.isoformat(),
        }
        job.updated_at = now
        self.repository.save_build_job(job)
        self._record_job_checkpoint(
            job,
            stage=job.current_stage,
            status="running",
            message=f"running stage {job.current_stage}",
            created_at=now,
        )
        attempt.status = job.current_stage
        attempt.last_operation = job.current_stage
        self.repository.save_build_attempt(attempt)
        build.status = job.current_stage
        build.last_operation = job.current_stage
        build.updated_at = now
        self.repository.save_build(build)

        result = self.runner.run_stage(build, context, job.current_stage)
        return self._apply_stage_result(build, attempt, job, result, event.delivery_id)

    def _apply_stage_result(
        self,
        build: BuildRecord,
        attempt: BuildAttemptRecord,
        job: BuildJobRecord,
        result: BuildStageResult,
        delivery_id: str,
    ) -> BuildRecord | None:
        now = datetime.now(UTC)
        if result.context is not None:
            self.repository.save_build_context(result.context)
        next_stage_state = dict(job.stage_state)
        if result.stage_state:
            next_stage_state.update(result.stage_state)
        next_stage_state["last_stage_message"] = result.message
        next_stage_state["last_stage_completed_at"] = now.isoformat()
        job.progress_message = result.message
        job.last_completed_stage = result.stage
        job.stage_state = next_stage_state
        job.updated_at = now
        build.updated_at = now
        self._record_job_checkpoint(
            job,
            stage=result.stage,
            status="progress",
            message=result.message,
            created_at=now,
        )
        self.repository.add_build_event(
            BuildEventRecord(build_id=build.build_id, stage=result.stage, message=result.message)
        )
        self.repository.add_build_log(
            BuildLogRecord(build_id=build.build_id, attempt=attempt.attempt, stage=result.stage, message=result.message)
        )
        self.bus.publish(
            "build.job.progress",
            {
                "build_id": build.build_id,
                "attempt": attempt.attempt,
                "stage": result.stage,
                "message": result.message,
            },
        )
        if result.next_stage is not None:
            job.current_stage = result.next_stage
            job.stage_state = {
                **job.stage_state,
                "next_stage": result.next_stage,
            }
            self.repository.save_build_job(job)
            self._record_job_checkpoint(
                job,
                stage=result.next_stage,
                status="queued",
                message=f"queued next stage {result.next_stage}",
                created_at=now,
            )
            build.status = result.next_stage
            build.last_operation = result.stage
            self.repository.save_build(build)
            attempt.status = result.next_stage
            attempt.last_operation = result.stage
            self.repository.save_build_attempt(attempt)
            self.bus.publish(
                "build.job.progress",
                {
                    "build_id": build.build_id,
                    "attempt": attempt.attempt,
                    "stage": result.next_stage,
                },
            )
            self.bus.mark_completed(delivery_id)
            return None

        if result.published_image is None:
            raise ValueError("terminal build stage missing published image")
        build = self.runner.finalize_success(build, result.published_image)
        build.build_duration_seconds = max((now - self._ensure_utc(attempt.started_at)).total_seconds(), 0.001)
        build.retry_count = max(build.retry_count, max(0, attempt.attempt - 1))
        self.repository.save_build(build)
        attempt.status = "published"
        attempt.last_operation = "published"
        attempt.finished_at = now
        self.repository.save_build_attempt(attempt)
        job.status = "succeeded"
        job.current_stage = "succeeded"
        job.executor_name = result.published_image.executor_name
        job.stage_state = {
            **job.stage_state,
            "final_status": "succeeded",
        }
        job.finished_at = now
        job.updated_at = now
        self.repository.save_build_job(job)
        self._record_job_checkpoint(
            job,
            stage="succeeded",
            status="succeeded",
            message=result.message,
            created_at=now,
        )
        self.bus.publish(
            "build.job.succeeded",
            {
                "build_id": build.build_id,
                "attempt": attempt.attempt,
                "artifact_uri": build.artifact_uri,
                "artifact_digest": build.artifact_digest,
            },
        )
        self.bus.publish(
            "build.published",
            {
                "build_id": build.build_id,
                "artifact_uri": build.artifact_uri,
                "artifact_digest": build.artifact_digest,
                "registry_manifest_uri": build.registry_manifest_uri,
            },
        )
        self.metrics.increment("build.published")
        self.bus.mark_completed(delivery_id)
        return build

    def _handle_job_failure(self, build: BuildRecord, event, exc: BuilderExecutionError) -> None:
        attempt_number = int(event.payload.get("attempt", max(1, event.attempts)))
        attempt = self.repository.get_build_attempt(build.build_id, attempt_number)
        if attempt is None:
            attempt = BuildAttemptRecord(build_id=build.build_id, attempt=attempt_number)
        job = self.repository.get_build_job(build.build_id, attempt=attempt_number)
        if job is None:
            job = BuildJobRecord(build_id=build.build_id, attempt=attempt_number)
        now = datetime.now(UTC)
        build = self.runner.finalize_failure(build, exc)
        build.retry_count = max(build.retry_count, max(0, attempt_number - 1))
        build.build_log_uri = self.runner.build_log_uri(build.build_id)
        build.build_duration_seconds = max((now - self._ensure_utc(attempt.started_at)).total_seconds(), 0.001)
        build.updated_at = now
        self.repository.save_build(build)
        self.repository.add_build_event(
            BuildEventRecord(build_id=build.build_id, stage="failed", message=build.failure_reason or str(exc))
        )
        self.repository.add_build_log(
            BuildLogRecord(
                build_id=build.build_id,
                attempt=attempt_number,
                stage="failed",
                message=build.failure_reason or str(exc),
            )
        )
        attempt.status = "failed"
        attempt.failure_class = build.failure_class
        attempt.last_operation = exc.operation
        if not exc.retryable or build.retry_count >= 2:
            attempt.finished_at = now
        self.repository.save_build_attempt(attempt)
        job.status = "failed"
        job.current_stage = "failed"
        job.failure_class = build.failure_class
        job.progress_message = build.failure_reason
        job.stage_state = {
            **job.stage_state,
            "final_status": "failed",
            "failed_operation": exc.operation,
        }
        if not exc.retryable or build.retry_count >= 2:
            job.finished_at = now
        job.updated_at = now
        self.repository.save_build_job(job)
        self._record_job_checkpoint(
            job,
            stage=exc.operation or "failed",
            status="failed",
            message=build.failure_reason or str(exc),
            created_at=now,
        )
        self.bus.publish(
            "build.job.failed",
            {
                "build_id": build.build_id,
                "attempt": attempt_number,
                "reason": build.failure_reason,
                "failure_class": build.failure_class,
            },
        )
        self.bus.publish(
            "build.failed",
            {
                "build_id": build.build_id,
                "image": build.image,
                "reason": build.failure_reason,
                "failure_class": build.failure_class,
            },
        )
        self.metrics.increment("build.failed")
        if exc.retryable and build.retry_count < 2:
            build.status = job.current_stage = event.payload.get("stage", job.current_stage or "staging")
            build.failure_reason = None
            build.failure_class = None
            build.updated_at = now
            job.status = "queued"
            job.current_stage = str(event.payload.get("stage", "staging"))
            job.failure_class = None
            job.stage_state = {
                **job.stage_state,
                "retry_after_failure": "true",
                "next_stage": job.current_stage,
            }
            job.finished_at = None
            job.updated_at = now
            self.repository.save_build(build)
            self.repository.save_build_job(job)
            self._record_job_checkpoint(
                job,
                stage=job.current_stage,
                status="queued",
                message=f"retry queued for stage {job.current_stage}",
                created_at=now,
            )
            self.bus.mark_failed(
                event.delivery_id,
                str(exc),
                retryable=True,
                retry_after_seconds=1.0,
            )
            return
        build.retry_exhausted = True
        self.repository.save_build(build)
        self.bus.mark_failed(event.delivery_id, build.failure_reason or str(exc))

    @staticmethod
    def _validate_context_uri(context_uri: str) -> None:
        parsed = urlparse(context_uri)
        if parsed.scheme not in {"s3", "minio", "file", "http", "https"}:
            raise ValueError(f"unsupported build context scheme: {parsed.scheme or 'missing'}")
        if parsed.scheme != "file" and not parsed.netloc:
            raise ValueError("build context uri missing object store or host component")
        if not parsed.path and parsed.scheme == "file":
            raise ValueError("build context uri missing object path")
        if parsed.scheme in {"s3", "minio"} and not (parsed.netloc or parsed.path.lstrip("/")):
            raise ValueError("build context uri missing object path")

    @staticmethod
    def _normalized_context_uri(context_uri: str) -> str:
        parsed = urlparse(context_uri)
        path = parsed.path or ""
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    @staticmethod
    def _dockerfile_object_uri(context_uri: str, dockerfile_path: str) -> str:
        normalized = BuilderService._normalized_context_uri(context_uri).rstrip("/")
        return f"{normalized}.{dockerfile_path.replace('/', '_')}"

    @staticmethod
    def _context_digest(build_id: str, context_uri: str, dockerfile_path: str) -> str:
        return f"sha256:{hashlib.sha256(f'{build_id}:{context_uri}:{dockerfile_path}'.encode()).hexdigest()}"

    def _materialize_context_source(self, request: BuildRequest) -> str:
        if request.context_archive_b64:
            archive_name = request.context_archive_name or "build-context.zip"
            context_uri, _ = self._write_context_archive(request.context_archive_b64, archive_name)
            return context_uri
        if request.context_uri is None:
            raise ValueError("build request requires context_uri or context_archive_b64")
        return request.context_uri

    @staticmethod
    def _write_context_archive(context_archive_b64: str, archive_name: str) -> tuple[str, int]:
        suffix = Path(archive_name).suffix or ".zip"
        context_dir = Path(tempfile.gettempdir()) / "greenference-builder-contexts"
        context_dir.mkdir(parents=True, exist_ok=True)
        payload = base64.b64decode(context_archive_b64.encode())
        with tempfile.NamedTemporaryFile(
            prefix="greenference-build-",
            suffix=suffix,
            dir=context_dir,
            delete=False,
        ) as outfile:
            outfile.write(payload)
        return Path(outfile.name).resolve().as_uri(), len(payload)

    @staticmethod
    def _ensure_utc(timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=UTC)
        return timestamp

    def _record_job_checkpoint(
        self,
        job: BuildJobRecord,
        *,
        stage: str,
        status: str,
        message: str,
        recovered: bool = False,
        created_at: datetime | None = None,
    ) -> BuildJobCheckpointRecord:
        checkpoint = BuildJobCheckpointRecord(
            job_id=job.job_id,
            build_id=job.build_id,
            attempt=job.attempt,
            stage=stage,
            status=status,
            message=message,
            recovered=recovered,
            created_at=created_at or datetime.now(UTC),
        )
        self.repository.add_build_job_checkpoint(checkpoint)
        return checkpoint

service = BuilderService()
