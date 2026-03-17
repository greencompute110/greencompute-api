from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from urllib import request
from urllib.error import HTTPError, URLError

from greenference_protocol import ChatCompletionRequest, ChatCompletionResponse, DeploymentRecord


class InferenceUpstreamError(RuntimeError):
    pass


class InferenceTimeoutError(InferenceUpstreamError):
    pass


class HttpInferenceClient:
    def __init__(
        self,
        upstream_timeout_seconds: float | None = None,
        health_timeout_seconds: float | None = None,
    ) -> None:
        self.upstream_timeout_seconds = upstream_timeout_seconds or float(
            os.getenv("GREENFERENCE_UPSTREAM_TIMEOUT_SECONDS", "10.0")
        )
        self.health_timeout_seconds = health_timeout_seconds or float(
            os.getenv("GREENFERENCE_HEALTH_TIMEOUT_SECONDS", "2.0")
        )

    def check_deployment_health(self, deployment: DeploymentRecord) -> bool:
        if not deployment.endpoint:
            return False
        upstream = request.Request(
            url=f"{deployment.endpoint.rstrip('/')}/healthz",
            method="GET",
        )
        try:
            with request.urlopen(upstream, timeout=self.health_timeout_seconds) as response:  # noqa: S310
                return 200 <= getattr(response, "status", 200) < 300
        except (HTTPError, URLError, TimeoutError, socket.timeout):
            return False

    def invoke_chat_completion(
        self,
        deployment: DeploymentRecord,
        payload: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        if not deployment.endpoint:
            raise InferenceUpstreamError(f"deployment endpoint missing: {deployment.deployment_id}")

        upstream = request.Request(
            url=f"{deployment.endpoint.rstrip('/')}/v1/chat/completions",
            data=payload.model_dump_json().encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(upstream, timeout=self.upstream_timeout_seconds) as response:  # noqa: S310
                body = json.loads(response.read().decode())
        except (TimeoutError, socket.timeout) as exc:
            raise InferenceTimeoutError(
                f"upstream timed out for deployment={deployment.deployment_id}"
            ) from exc
        except (HTTPError, URLError) as exc:
            if isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError | socket.timeout):
                raise InferenceTimeoutError(
                    f"upstream timed out for deployment={deployment.deployment_id}"
                ) from exc
            raise InferenceUpstreamError(
                f"upstream invocation failed for deployment={deployment.deployment_id}"
            ) from exc
        return ChatCompletionResponse(**body)

    def stream_chat_completion(
        self,
        deployment: DeploymentRecord,
        payload: ChatCompletionRequest,
    ) -> Iterator[str]:
        if not deployment.endpoint:
            raise InferenceUpstreamError(f"deployment endpoint missing: {deployment.deployment_id}")

        upstream = request.Request(
            url=f"{deployment.endpoint.rstrip('/')}/v1/chat/completions",
            data=payload.model_copy(update={"stream": True}).model_dump_json().encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(upstream, timeout=self.upstream_timeout_seconds) as response:  # noqa: S310
                while True:
                    line = response.readline()
                    if not line:
                        break
                    yield line.decode()
        except (TimeoutError, socket.timeout) as exc:
            raise InferenceTimeoutError(
                f"upstream timed out for deployment={deployment.deployment_id}"
            ) from exc
        except (HTTPError, URLError) as exc:
            if isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError | socket.timeout):
                raise InferenceTimeoutError(
                    f"upstream timed out for deployment={deployment.deployment_id}"
                ) from exc
            raise InferenceUpstreamError(
                f"upstream invocation failed for deployment={deployment.deployment_id}"
            ) from exc
