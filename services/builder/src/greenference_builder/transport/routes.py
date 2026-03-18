from fastapi import APIRouter

from greenference_protocol import BuildContextUploadRequest, BuildRequest
from greenference_builder.application.services import service

router = APIRouter()


@router.post("/builder/v1/builds")
def start_build(payload: BuildRequest) -> dict:
    return service.start_build(payload).model_dump(mode="json")


@router.post("/builder/v1/build-contexts")
def upload_build_context(payload: BuildContextUploadRequest) -> dict:
    return service.upload_build_context(payload).model_dump(mode="json")


@router.get("/builder/v1/builds")
def list_builds() -> list[dict]:
    return [build.model_dump(mode="json") for build in service.list_builds()]


@router.get("/builder/v1/builds/{build_id}")
def get_build(build_id: str) -> dict | None:
    build = service.get_build(build_id)
    return build.model_dump(mode="json") if build is not None else None


@router.get("/builder/v1/builds/{build_id}/jobs/latest/timeline")
def latest_build_job_timeline(build_id: str) -> list[dict]:
    return [entry.model_dump(mode="json") for entry in service.latest_build_job_timeline(build_id)]


@router.get("/builder/v1/builds/{build_id}/jobs/latest/recovery-summary")
def latest_build_job_recovery_summary(build_id: str) -> dict:
    return service.latest_build_job_recovery_summary(build_id)


@router.get("/builder/v1/images/{image:path}/history")
def image_history(image: str) -> list[dict]:
    return [build.model_dump(mode="json") for build in service.list_image_history(image)]


@router.post("/builder/v1/events/process")
def process_events(limit: int = 10) -> list[dict]:
    return [build.model_dump(mode="json") for build in service.process_pending_events(limit=limit)]


@router.post("/builder/v1/recovery")
def recover_jobs() -> dict:
    return service.recover_inflight_jobs()


@router.get("/builder/v1/recovery")
def recovery_status() -> dict:
    return service.recovery_status()
