from __future__ import annotations

from datetime import UTC, datetime

from greenference_protocol import BuildRecord, BuildRequest
from greenference_builder.infrastructure.repository import BuilderRepository


class BuilderService:
    def __init__(self, repository: BuilderRepository | None = None) -> None:
        self.repository = repository or BuilderRepository()

    def start_build(self, request: BuildRequest) -> BuildRecord:
        build = BuildRecord(
            image=request.image,
            context_uri=request.context_uri,
            dockerfile_path=request.dockerfile_path,
            public=request.public,
            status="published",
            artifact_uri=f"oci://registry.greenference.local/{request.image}",
        )
        build.updated_at = datetime.now(UTC)
        return self.repository.save_build(build)

    def list_builds(self) -> list[BuildRecord]:
        return self.repository.list_builds()


service = BuilderService()
