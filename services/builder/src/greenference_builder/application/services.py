from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse

from greenference_persistence import WorkflowEventRepository, load_runtime_settings
from greenference_protocol import BuildRecord, BuildRequest
from greenference_builder.infrastructure.repository import BuilderRepository


class BuilderService:
    def __init__(
        self,
        repository: BuilderRepository | None = None,
        workflow_repository: WorkflowEventRepository | None = None,
    ) -> None:
        self.repository = repository or BuilderRepository()
        self.workflow_repository = workflow_repository or WorkflowEventRepository(
            engine=self.repository.engine,
            session_factory=self.repository.session_factory,
        )
        self.settings = load_runtime_settings("greenference-builder")

    def start_build(self, request: BuildRequest) -> BuildRecord:
        build = BuildRecord(
            image=request.image,
            context_uri=request.context_uri,
            dockerfile_path=request.dockerfile_path,
            public=request.public,
            status="accepted",
            artifact_uri=None,
        )
        build.updated_at = datetime.now(UTC)
        saved = self.repository.save_build(build)
        self.workflow_repository.publish(
            "build.accepted",
            {
                "build_id": saved.build_id,
                "image": saved.image,
            },
        )
        return saved

    def list_builds(self) -> list[BuildRecord]:
        return self.repository.list_builds()

    def process_pending_events(self, limit: int = 10) -> list[BuildRecord]:
        events = self.workflow_repository.claim_pending(["build.accepted"], limit=limit)
        processed: list[BuildRecord] = []
        registry_ref = urlparse(self.settings.registry_url).netloc or self.settings.registry_url.replace(
            "http://", ""
        ).replace("https://", "")

        for event in events:
            build = self.repository.get_build(str(event.payload["build_id"]))
            if build is None:
                self.workflow_repository.mark_failed(event.event_id, "build not found")
                continue
            build.status = "published"
            build.artifact_uri = f"oci://{registry_ref.rstrip('/')}/{build.image}"
            build.updated_at = datetime.now(UTC)
            self.repository.save_build(build)
            self.workflow_repository.publish(
                "build.published",
                {
                    "build_id": build.build_id,
                    "artifact_uri": build.artifact_uri,
                },
            )
            self.workflow_repository.mark_completed(event.event_id)
            processed.append(build)
        return processed


service = BuilderService()
