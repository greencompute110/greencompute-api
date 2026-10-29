"""Every error carries `error.code` (machine-readable, #25) and
`error.message` (plain English, what to do next, #33) next to the unchanged
`detail`; Retry-After and the other headers are kept."""
import pytest
from fastapi import FastAPI, HTTPException, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel

from greencompute_gateway.transport.errors import (
    CODES,
    RETRYABLE,
    CrashToJSON,
    coded,
    describe_error,
    install_error_handlers,
)


class Body(BaseModel):
    workload_id: str
    gpu_count: int


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(CrashToJSON)
    install_error_handlers(app)

    @app.get("/raise/{status}")
    def raise_(status: int, kind: str = Query(default="")):
        details = {
            "no_capacity": ({"code": "no_capacity", "message": "every rtx4090 is rented right now; nothing was created"},
                            {"Retry-After": "60"}),
            "rate": ({"message": "rate limit exceeded", "retry_after_seconds": 12}, {"Retry-After": "12"}),
            "balance": ({"message": "insufficient balance to start this rental", "required_cents": 40,
                         "current_cents": 12}, None),
            "scope": ({"message": "scope required", "required_scope": "rentals"}, None),
            "gpu": ({"message": "none of the requested GPU models are available to rent", "requested": ["h100"],
                     "available": ["rtx4090"]}, None),
            "text": ("deployment not found", None),
        }
        detail, headers = details[kind]
        raise HTTPException(status_code=status, detail=detail, headers=headers)

    @app.post("/body")
    def body(payload: Body):
        return payload

    @app.get("/crash")
    def crash():
        raise RuntimeError("boom")

    @app.get("/coded")
    def coded_():
        raise coded(409, "model_not_ready", "no ready deployment for model qwen", {"Retry-After": "10"})

    @app.get("/disabled")
    def disabled():
        raise coded(501, "not_implemented", {"status": "disabled", "reason": "not implemented in greencompute-api"})

    return TestClient(app, raise_server_exceptions=False)


def test_no_capacity_keeps_its_code_detail_and_retry_after(client):
    res = client.get("/raise/503?kind=no_capacity")
    assert res.status_code == 503 and res.headers["retry-after"] == "60"
    body = res.json()
    assert body["detail"]["code"] == "no_capacity"  # detail unchanged
    assert body["error"] == {"code": "no_capacity", "retryable": True, "retry_after_seconds": 60,
                             "message": "Every rtx4090 is rented right now; nothing was created."}


@pytest.mark.parametrize("status, kind, code, retryable, says", [
    (429, "rate", "rate_limited", True, "Wait 12 seconds"),
    (402, "balance", "insufficient_balance", False, "Add at least $0.28 at https://www.green-compute.com/billing"),
    (403, "scope", "scope_required", False, "doesn't have the `rentals` scope"),
    (400, "gpu", "gpu_unavailable", False, "You can rent: rtx4090."),
    (404, "text", "not_found", False, "Deployment not found. Check the id"),
])
def test_each_case_gets_its_code_and_a_sentence_that_says_what_to_do(client, status, kind, code, retryable, says):
    err = client.get(f"/raise/{status}?kind={kind}").json()["error"]
    assert (err["code"], err["retryable"]) == (code, retryable)
    assert says in err["message"]


def test_a_missing_key_says_how_to_send_one(client):
    err = describe_error(401, "missing api key. Send `Authorization: Bearer <key>`.")
    assert err["code"] == "unauthenticated" and "Authorization: Bearer" in err["message"]


def test_validation_errors_name_the_first_bad_field(client):
    res = client.post("/body", json={"workload_id": "w", "gpu_count": "lots"})
    assert res.status_code == 422
    body = res.json()
    assert isinstance(body["detail"], list)  # FastAPI's list, unchanged
    assert body["error"]["code"] == "validation_error"
    assert "gpu_count" in body["error"]["message"]


def test_an_unexpected_crash_is_json_with_a_way_forward(client):
    res = client.get("/crash")
    assert res.status_code == 500
    err = res.json()["error"]
    assert err["code"] == "server_error" and err["retryable"] is True
    assert "support@green-compute.com" in err["message"]


def test_unknown_routes_and_methods_get_codes(client):
    err = client.get("/no-such-path").json()["error"]
    assert err["code"] == "unknown_endpoint" and "openapi.json" in err["message"]
    assert client.get("/body").json()["error"]["code"] == "method_not_allowed"


def test_codes_set_at_the_raise_site_win(client):
    res = client.get("/coded")
    err = res.json()["error"]
    assert res.json()["detail"] == "no ready deployment for model qwen"  # unchanged
    assert (err["code"], err["retryable"], err["retry_after_seconds"]) == ("model_not_ready", True, 10)


def test_a_disabled_feature_is_not_worth_retrying_and_says_why(client):
    err = client.get("/disabled").json()["error"]
    assert (err["code"], err["retryable"]) == ("not_implemented", False)
    assert err["message"] == "Not implemented in greencompute-api."


def test_identifiers_keep_their_case_and_fields_named_like_sources_are_kept():
    assert describe_error(503, {"message": "rtx4090 is fully rented"})["message"] == "rtx4090 is fully rented."
    err = describe_error(422, [{"loc": ["body", "path"], "msg": "Field required"}])
    assert "path: Field required" in err["message"]


def test_an_unparseable_retry_after_header_falls_back_to_the_detail():
    err = describe_error(429, {"message": "rate limit exceeded", "retry_after_seconds": 30},
                         {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert err["retry_after_seconds"] == 30 and "Wait 30 seconds" in err["message"]


def test_the_auth_throttle_says_to_fix_the_key_not_to_retry(monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from greencompute_gateway.transport import security

    monkeypatch.setattr(security, "_resolve_client_ip", lambda ip: "203.0.113.9")
    monkeypatch.setattr(security.rate_limiter, "check", lambda *a, **k: SimpleNamespace(allowed=False, reset_at=0))
    with pytest.raises(HTTPException) as exc:
        security._check_auth_failure_throttle("203.0.113.9")
    err = describe_error(exc.value.status_code, exc.value.detail, exc.value.headers, exc.value.error_code)
    assert (err["code"], err["retryable"]) == ("auth_throttled", False)
    assert "Check the key" in err["message"]


def test_every_retryable_code_is_documented():
    assert RETRYABLE <= set(CODES)


def test_the_real_gateway_app_uses_the_handlers():
    from greencompute_gateway.main import app

    res = TestClient(app).get("/platform/billing/balance")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthenticated"
    assert "detail" in res.json()


def test_a_crash_in_the_real_app_is_json_a_browser_can_read(monkeypatch):
    """CrashToJSON sits inside CORS, so the 500 carries the CORS headers."""
    from greencompute_gateway import main
    from greencompute_gateway.transport import routes

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(routes, "_public_gpu_models", boom)
    origin = "http://localhost:3000"  # allowed by the default CORS list in tests
    res = TestClient(main.app, raise_server_exceptions=False).get("/platform/pricing", headers={"Origin": origin})
    assert res.status_code == 500
    assert res.json()["error"]["code"] == "server_error"
    assert res.headers.get("access-control-allow-origin") == origin


def test_idempotency_reuse_and_in_progress_have_their_own_codes():
    reused = describe_error(422, "this Idempotency-Key was already used with a different request body; use a new key")
    assert (reused["code"], reused["retryable"]) == ("idempotency_key_reused", False)
    running = describe_error(409, "a request with this Idempotency-Key is still being processed", {"Retry-After": "2"})
    assert (running["code"], running["retryable"], running["retry_after_seconds"]) == ("idempotency_in_progress", True, 2)
