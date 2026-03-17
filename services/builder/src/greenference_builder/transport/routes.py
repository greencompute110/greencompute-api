from fastapi import APIRouter

from greenference_protocol import BuildRequest
from greenference_builder.application.services import service

router = APIRouter()


@router.post("/builder/v1/builds")
def start_build(payload: BuildRequest) -> dict:
    return service.start_build(payload).model_dump(mode="json")


@router.get("/builder/v1/builds")
def list_builds() -> list[dict]:
    return [build.model_dump(mode="json") for build in service.list_builds()]

