"""A client that disconnects mid-stream must still be billed for what was
served. Pre-fix, GeneratorExit at the `yield` skipped the entire post-loop
settlement block, yielding repeatable free inference."""

from __future__ import annotations

from greencompute_gateway.application.services import GatewayService
from greencompute_protocol import (
    ChatCompletionMessage,
    ChatCompletionRequest,
    DeploymentRecord,
)


def _service_with_stubs():
    svc = GatewayService.__new__(GatewayService)
    calls: dict[str, list] = {"charge": [], "invocation": [], "usage": []}

    class _Metrics:
        def observe(self, *a, **k):
            pass

        def increment(self, *a, **k):
            pass

    svc.metrics = _Metrics()
    svc._handle_upstream_success = lambda *a, **k: None
    svc._track_latency_ema = lambda *a, **k: None
    svc._record_usage = lambda *a, **k: calls["usage"].append(k)
    svc._charge_inference_tokens = lambda **k: calls["charge"].append(k)
    svc._record_invocation = lambda *a, **k: calls["invocation"].append(k)
    # Avoid the demand-tick DB import path firing during the unit test.
    svc._completion_token_bound = lambda req: 4096
    return svc, calls


def _request():
    return ChatCompletionRequest(
        model="m",
        messages=[ChatCompletionMessage(role="user", content="hello world " * 5)],
        stream=True,
    )


def _deployment():
    return DeploymentRecord(workload_id="w1", hotkey="hk1", deployment_id="dep123456789")


def test_disconnect_midstream_still_bills():
    svc, calls = _service_with_stubs()

    def fake_upstream(deployment, request, *, request_id):
        # Two content chunks, then we never reach the final usage chunk because
        # the consumer disconnects.
        yield 'data: {"choices":[{"delta":{"content":"a"}}]}'
        yield 'data: {"choices":[{"delta":{"content":"b"}}]}'
        yield 'data: {"choices":[{"delta":{"content":"c"}}]}'

    svc._invoke_upstream_stream = fake_upstream

    gen = svc._stream_from_candidates(
        _request(), [_deployment()], {"host": "h"},
        request_id="r1", started=0.0, api_key_id="k1", user_id="u1",
    )
    next(gen)  # consume the first chunk
    gen.close()  # client disconnects -> GeneratorExit at the yield

    assert len(calls["charge"]) == 1, "disconnect must trigger exactly one charge"
    charged = calls["charge"][0]
    assert charged["prompt_tokens"] > 0, "prompt is known from the request and must be billed"
    assert charged["completion_tokens"] >= 1, "content chunks served must be billed"
    assert calls["invocation"] and calls["invocation"][-1]["status"] == "client_disconnected"


def test_normal_completion_bills_once_from_usage_chunk():
    svc, calls = _service_with_stubs()

    def fake_upstream(deployment, request, *, request_id):
        yield 'data: {"choices":[{"delta":{"content":"a"}}]}'
        yield 'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":7}}'
        yield "data: [DONE]"

    svc._invoke_upstream_stream = fake_upstream

    gen = svc._stream_from_candidates(
        _request(), [_deployment()], {"host": "h"},
        request_id="r2", started=0.0, api_key_id="k1", user_id="u1",
    )
    list(gen)  # consume fully

    assert len(calls["charge"]) == 1
    assert calls["invocation"][-1]["status"] == "succeeded"
    # completion billed from the (clamped) reported usage, not the chunk count.
    assert calls["charge"][0]["completion_tokens"] == 7
