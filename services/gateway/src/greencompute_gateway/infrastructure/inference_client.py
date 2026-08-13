from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from urllib import request
from urllib.error import HTTPError, URLError

from greencompute_protocol import ChatCompletionRequest, ChatCompletionResponse, DeploymentRecord


class InferenceUpstreamError(RuntimeError):
    pass


class InferenceTimeoutError(InferenceUpstreamError):
    pass


class InferenceConnectionError(InferenceUpstreamError):
    pass


class InferenceBadResponseError(InferenceUpstreamError):
    pass


#: Models that legitimately need longer than the 120s default.
#:
#: 120s suits 7B-class models. Reasoning models emit chain-of-thought before the
#: answer, so one Kimi K3 completion runs 1-3 minutes and was being cut off at
#: exactly 120.2s with a 502 no matter what timeout the caller set. Streaming hid
#: it -- the socket keeps reading -- so only non-streaming calls, the shape most
#: agent frameworks send, ever failed.
#:
#: Deliberately per-model rather than a global bump: /v1/chat/completions is a sync
#: endpoint served from the anyio threadpool (40 threads), and K3 admits 32
#: concurrent requests. A blanket 600s would let K3 alone pin 32 of those threads
#: for ten minutes and starve every other model, so the long timeout is scoped to
#: the models that need it. Keys are matched with the same normalisation as
#: billing, so "moonshotai/Kimi-K3" and "kimi-k3" behave identically.
#: 1800s, not 600s, because K3 now serves up to 115k-token prompts. Prefill on a
#: ~104k prompt alone ran past 340s across 9 pipeline stages before generation
#: starts, so 600s would cut off exactly the long-context requests the larger
#: max_model_len exists to serve. Bounded blast radius: K3 admits 8 concurrent
#: requests, so at worst 8 of the ~40 threadpool threads are held.
MODEL_UPSTREAM_TIMEOUT_SECONDS: dict[str, float] = {
    "kimi-k3": 1800.0,
}


def _normalize_model_id(model: str | None) -> str:
    if not model:
        return ""
    return model.rsplit("/", 1)[-1].strip().lower()


class HttpInferenceClient:
    def __init__(
        self,
        upstream_timeout_seconds: float | None = None,
        health_timeout_seconds: float | None = None,
        miner_auth_secret: str | None = None,
    ) -> None:
        self.upstream_timeout_seconds = upstream_timeout_seconds or float(
            os.getenv("GREENCOMPUTE_UPSTREAM_TIMEOUT_SECONDS", "120.0")
        )
        self.health_timeout_seconds = health_timeout_seconds or float(
            os.getenv("GREENCOMPUTE_HEALTH_TIMEOUT_SECONDS", "2.0")
        )
        self.miner_auth_secret = miner_auth_secret or os.getenv("GREENCOMPUTE_INFERENCE_AUTH_SECRET") or None

    def _timeout_for(self, payload: ChatCompletionRequest) -> float:
        """Upstream timeout for this request, widened for slow reasoning models."""
        override = MODEL_UPSTREAM_TIMEOUT_SECONDS.get(_normalize_model_id(getattr(payload, "model", None)))
        # An explicitly-configured timeout still wins, so operators keep the final say.
        return max(override, self.upstream_timeout_seconds) if override else self.upstream_timeout_seconds

    def _base_headers(self, request_id: str | None) -> dict[str, str]:
        h: dict[str, str] = {"content-type": "application/json"}
        if request_id is not None:
            h["x-request-id"] = request_id
        if self.miner_auth_secret:
            # X-Agent-Auth is what node-agent's `validate_optional_auth`
            # actually reads. X-Gateway-Auth is kept for backwards-compat
            # in case any operator's reverse proxy filters on it.
            h["X-Agent-Auth"] = self.miner_auth_secret
            h["X-Gateway-Auth"] = self.miner_auth_secret
        return h

    def check_deployment_health(self, deployment: DeploymentRecord) -> bool:
        if not deployment.endpoint:
            return False
        headers: dict[str, str] = {}
        if self.miner_auth_secret:
            headers["X-Agent-Auth"] = self.miner_auth_secret
            headers["X-Gateway-Auth"] = self.miner_auth_secret
        upstream = request.Request(
            url=f"{deployment.endpoint.rstrip('/')}/healthz",
            headers=headers,
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
        *,
        request_id: str | None = None,
    ) -> ChatCompletionResponse:
        if not deployment.endpoint:
            raise InferenceUpstreamError(f"deployment endpoint missing: {deployment.deployment_id}")

        upstream = request.Request(
            url=f"{deployment.endpoint.rstrip('/')}/v1/chat/completions",
            data=payload.model_dump_json().encode(),
            headers=self._base_headers(request_id),
            method="POST",
        )
        try:
            with request.urlopen(upstream, timeout=self._timeout_for(payload)) as response:  # noqa: S310
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
            if isinstance(exc, URLError):
                raise InferenceConnectionError(
                    f"upstream connection failed for deployment={deployment.deployment_id}"
                ) from exc
            raise InferenceUpstreamError(
                f"upstream invocation failed for deployment={deployment.deployment_id}"
            ) from exc
        try:
            return ChatCompletionResponse(**body)
        except Exception as exc:  # noqa: BLE001
            raise InferenceBadResponseError(
                f"upstream returned invalid response for deployment={deployment.deployment_id}"
            ) from exc

    def stream_chat_completion(
        self,
        deployment: DeploymentRecord,
        payload: ChatCompletionRequest,
        *,
        request_id: str | None = None,
    ) -> Iterator[str]:
        if not deployment.endpoint:
            raise InferenceUpstreamError(f"deployment endpoint missing: {deployment.deployment_id}")

        # Force stream_options.include_usage=true so vLLM emits a final
        # chunk with {usage: {...}} — the gateway uses it to debit the user
        # after the stream ends. Without this, streaming calls are free.
        existing_opts = getattr(payload, "stream_options", None) or {}
        if not isinstance(existing_opts, dict):
            existing_opts = {}
        existing_opts.setdefault("include_usage", True)
        streamed = payload.model_copy(update={
            "stream": True,
            "stream_options": existing_opts,
        })
        upstream = request.Request(
            url=f"{deployment.endpoint.rstrip('/')}/v1/chat/completions",
            data=streamed.model_dump_json().encode(),
            headers=self._base_headers(request_id),
            method="POST",
        )
        try:
            with request.urlopen(upstream, timeout=self._timeout_for(payload)) as response:  # noqa: S310
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
            if isinstance(exc, URLError):
                raise InferenceConnectionError(
                    f"upstream connection failed for deployment={deployment.deployment_id}"
                ) from exc
            raise InferenceUpstreamError(
                f"upstream invocation failed for deployment={deployment.deployment_id}"
            ) from exc
