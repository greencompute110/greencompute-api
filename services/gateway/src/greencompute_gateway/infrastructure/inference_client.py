from __future__ import annotations

import http.client
import json
import os
import re
import socket
from collections.abc import Iterator
from urllib import request
from urllib.error import HTTPError, URLError

from greencompute_protocol import ChatCompletionRequest, ChatCompletionResponse, DeploymentRecord


class InferenceUpstreamError(RuntimeError):
    """The model server, or the node in front of it, failed this request.

    `status_code` is the HTTP status it answered with (None when it never
    answered), `upstream_message` its own reason when it gave one. The
    gateway sets `model_healthy` after probing the deployment: True means the
    model is up and only this request failed, False that the model is down,
    None that it wasn't probed."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        upstream_message: str | None = None,
        model_unreachable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.upstream_message = upstream_message
        #: The node said it can't reach the model server at all: the model is down.
        self.model_unreachable = model_unreachable
        self.model_healthy: bool | None = None


class InferenceTimeoutError(InferenceUpstreamError):
    pass


class InferenceConnectionError(InferenceUpstreamError):
    pass


class InferenceBadResponseError(InferenceUpstreamError):
    pass


class InferenceRequestRejectedError(InferenceUpstreamError):
    """The model server refused the request itself, with a 4xx: empty
    `messages`, more tokens than its context, tools it wasn't started for.
    The caller's fault, so it goes back to them with the reason; it is never
    counted against the model (three of these used to mark a shared model
    FAILED for everyone) and never billed."""


#: 4xx answers that are NOT the request's fault: the gateway's own key to the
#: node (401/403), a route or runtime the node doesn't have (404/405), the
#: node or model being overloaded (408/429). Those go through the health
#: probe like a 5xx.
_NOT_THE_REQUESTS_FAULT = frozenset({401, 403, 404, 405, 407, 408, 429})

#: A node agent from before 2026-11-17 turned every answer from vLLM into a
#: 502 "upstream error: <what urllib said>"; read the real status back out.
_OLD_NODE_ERROR = "upstream error: "
_OLD_NODE_HTTP_STATUS = re.compile(r"^HTTP Error (\d{3})")


def _is_rejection(status: int) -> bool:
    return 400 <= status < 500 and status not in _NOT_THE_REQUESTS_FAULT


#: Longest upstream reason passed on to a caller.
MAX_UPSTREAM_MESSAGE_CHARS = 300


def _capped(text: str | None) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= MAX_UPSTREAM_MESSAGE_CHARS else text[: MAX_UPSTREAM_MESSAGE_CHARS - 1] + "…"


def is_model_error_body(raw: bytes) -> bool:
    """Whether an error body is the model server's own, in vLLM's or
    OpenAI's shape: `{"object": "error", "message": ...}` or `{"error":
    {"message": ...}}`. Only those make a 4xx the request's fault; an HTML
    page or a stray JSON from something else on the port is the server's."""
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("object") == "error" and isinstance(data.get("message"), str):
        return True
    err = data.get("error")
    return isinstance(err, dict) and isinstance(err.get("message"), str)


def upstream_message(raw: bytes) -> str | None:
    """The reason in an error body (at most MAX_UPSTREAM_MESSAGE_CHARS):
    vLLM's `{"message": ...}` (or the newer `{"error": {"message": ...}}`),
    a node agent's `{"detail": ...}`, or the text itself."""
    return _capped(_message_in(raw))


def _message_in(raw: bytes) -> str | None:
    try:
        data = json.loads(raw)
    except ValueError:
        text = raw.decode("utf-8", "replace").strip()
        return text[:1000] or None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(err, str) and err:
            return err
        for key in ("message", "detail"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, list) and value and isinstance(value[0], dict) and value[0].get("msg"):
                return str(value[0]["msg"])
    return None


def classify_http_error(exc: HTTPError, deployment_id: str | None) -> InferenceUpstreamError:
    """The right error for an HTTP error answer from a node (or an external
    OpenAI-compatible server)."""
    try:
        raw = exc.read(64 * 1024) or b""
    except Exception:  # noqa: BLE001 - the body is a nicety; the status decides
        raw = b""
    message = upstream_message(raw)
    status = exc.code
    node_error = exc.headers.get("X-GC-Node-Error") if exc.headers is not None else None
    where = f"deployment={deployment_id}"
    if node_error == "model_unreachable":
        return InferenceConnectionError(f"the model server is unreachable for {where}", status_code=status,
                                        upstream_message=message, model_unreachable=True)
    if node_error == "model_timeout":
        return InferenceTimeoutError(f"upstream timed out for {where}", status_code=status, upstream_message=message)
    if status == 502 and message and message.startswith(_OLD_NODE_ERROR):
        reason = message[len(_OLD_NODE_ERROR):]
        found = _OLD_NODE_HTTP_STATUS.match(reason)
        if found:
            inner = int(found.group(1))
            if _is_rejection(inner):
                return InferenceRequestRejectedError(
                    f"the model rejected the request for {where} (HTTP {inner})", status_code=inner,
                    upstream_message=f"the model rejected the request as invalid (HTTP {inner})")
            return InferenceUpstreamError(f"upstream invocation failed for {where} (HTTP {inner})", status_code=inner)
        if "timed out" in reason:
            return InferenceTimeoutError(f"upstream timed out for {where}", status_code=status)
        return InferenceConnectionError(f"the model server is unreachable for {where}", status_code=status,
                                        model_unreachable=True)
    if _is_rejection(status) and is_model_error_body(raw):
        return InferenceRequestRejectedError(f"the model rejected the request for {where}: {message or status}",
                                             status_code=status, upstream_message=message)
    return InferenceUpstreamError(f"upstream invocation failed for {where} (HTTP {status})",
                                  status_code=status, upstream_message=message)


def _transport_error(exc: BaseException, deployment_id: str | None) -> InferenceUpstreamError:
    """The right error for a request that got no HTTP answer at all."""
    where = f"deployment={deployment_id}"
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, TimeoutError | socket.timeout):
        return InferenceTimeoutError(f"upstream timed out for {where}")
    return InferenceConnectionError(f"upstream connection failed for {where}")


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
    # A 1M-token prefill measured ~1476s to first token, so the ceiling has to
    # clear that with room for generation on top.
    "k3-coder": 3600.0,
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
        except (OSError, http.client.HTTPException):  # an HTTP error, a timeout, a dropped connection
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
                raw = response.read()
        except HTTPError as exc:
            raise classify_http_error(exc, deployment.deployment_id) from exc
        except (OSError, http.client.HTTPException) as exc:
            # URLError, a timeout, or the connection dropping mid-answer
            # (RemoteDisconnected, a reset), which urllib raises unwrapped.
            raise _transport_error(exc, deployment.deployment_id) from exc
        try:
            return ChatCompletionResponse(**json.loads(raw.decode()))
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
        except HTTPError as exc:
            raise classify_http_error(exc, deployment.deployment_id) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise _transport_error(exc, deployment.deployment_id) from exc
