"""/openapi.json as clients should read it (external review, Nov 2026: it
declared no auth scheme, so every route looked public, and documented only
200/201/422).

On top of what FastAPI generates:

- Two security schemes, `bearerAuth` (Authorization: Bearer <key>) and
  `apiKeyHeader` (X-API-Key), required on every route that takes a key, and
  `btauth` (a request signed with a Bittensor hotkey that paid in gamma) as
  a third way on the routes a signed request may call; the
  public ones (prices, GPU list, health, wallet sign-in, sales forms) say
  `security: []`. The per-route `authorization` / `X-API-Key` header
  parameters are dropped: the schemes say it.
- The error responses each route can give, every one with the coded error
  body (`ErrorResponse`: `detail` plus `error` {code, message, retryable,
  retry_after_seconds}), each status listing its codes from errors.CODES.
- 429 everywhere a key is taken (the per-key rate limit), and the
  X-RateLimit-* headers on success.

Internal and turned-off routes are kept out with include_in_schema=False on
the routes themselves.
"""
from __future__ import annotations

import inspect
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.params import Header as HeaderParam

from greencompute_gateway.transport.errors import CODES

SECURITY_SCHEMES = {
    "bearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "description": ("Your API key: `Authorization: Bearer <key>`. Create one at "
                        "https://www.green-compute.com/settings, with POST /platform/api-keys, or by signing in "
                        "with a wallet (POST /platform/auth/wallet/challenge)."),
    },
    "apiKeyHeader": {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "The same key in an `X-API-Key` header instead.",
    },
}
KEY_REQUIRED = [{"bearerAuth": []}, {"apiKeyHeader": []}]
#: Routes a hotkey-signed request may also call: those whose scope is one a
#: signed request has (domain/gamma.SIGNED_REQUEST_SCOPES).
KEY_OR_SIGNED = KEY_REQUIRED + [{"btauth": []}]


def _btauth_scheme() -> dict:
    from greencompute_gateway.domain import gamma

    receiver = gamma.service_hotkey()
    return {
        "type": "apiKey",
        "in": "header",
        "name": "X-Bittensor-Signature",
        "description": (
            "Bittensor signed requests, `btauth/1` (https://www.bittensor.com/docs/guides/signed-requests). "
            f"The sender hotkey is the account: spend SN110 gamma at the receiver hotkey `{receiver}` "
            f"(`btcli gamma spend --netuid {gamma.netuid()} --service-hotkey {receiver} --amount-gamma 5 -H API_KEY`) "
            "and the hotkey's account is credited $1 per gamma within about a minute; no sign-up or API key. "
            "Send all of `X-Bittensor-Version: 1`, `X-Bittensor-Hotkey` (sender ss58), `X-Bittensor-Receiver` "
            f"(`{receiver}`), `X-Bittensor-Nonce` (unix time in nanoseconds; accepted from 10 s old to 2 s ahead, "
            "each once) and `X-Bittensor-Signature` (0x-hex sr25519 signature over the payload: the "
            "newline-joined lines `btauth/1`, `sr25519`, METHOD, request target as sent, hex sha256 of the raw "
            "body, nonce, sender, receiver). Signed requests may rent, run inference and read the account "
            "(scopes " + ", ".join(gamma.SIGNED_REQUEST_SCOPES) + "). When these headers are present any "
            "Authorization header is ignored, so OpenAI-style SDKs may send a placeholder key."
        ),
        "x-btauth": {
            "version": "btauth/1",
            "netuid": gamma.netuid(),
            "receivers": gamma.service_hotkeys(),
            "crypto": ["sr25519"],
            "scopes": list(gamma.SIGNED_REQUEST_SCOPES),
            "balance": f"{gamma.API_BASE}/gamma/balance",
            "services": gamma.services_url(),
        },
    }

#: Routes that take an API key but don't need one: wallet sign-in reads it
#: only to tell the website's server apart.
OPTIONAL_KEY = {("post", "/platform/auth/wallet/challenge"), ("post", "/platform/auth/wallet/verify")}

_AUTH_PARAMS = {"authorization", "x-api-key"}

_STATUS_TEXT = {
    400: "The request can't be done as sent",
    401: "The API key is missing, not valid, or expired",
    402: "The balance doesn't cover an hour of what you're starting",
    403: "This key or account may not do this",
    404: "No such resource on this account",
    409: "The resource's current state doesn't allow this",
    422: "Fields are missing or invalid; `detail` lists each one",
    429: "Too many requests; wait `Retry-After` seconds",
    503: "Temporarily unavailable, or no GPU that fits is free; nothing was created",
    504: "No answer in time; it may or may not have run",
}

#: Statuses a route can answer with beyond the general rules below.
EXTRA_STATUSES: dict[tuple[str, str], tuple[int, ...]] = {
    ("post", "/platform/deployments"): (402, 503),
    ("post", "/platform/deployments/{deployment_id}/resume"): (402, 503),
    ("get", "/platform/deployments/{deployment_id}/stats"): (409, 503),
    ("get", "/platform/deployments/{deployment_id}/logs"): (409, 503),
    ("post", "/platform/deployments/{deployment_id}/exec"): (503, 504),
    ("post", "/platform/deployments/{deployment_id}/terminal"): (503,),
    ("post", "/platform/billing/topup/crypto"): (503,),
    ("post", "/v1/chat/completions"): (402, 404, 409, 503),
    ("post", "/v1/completions"): (402, 404, 409, 503),
    ("post", "/v1/embeddings"): (402, 404, 409, 503),
    ("post", "/platform/auth/wallet/challenge"): (400, 403, 429, 503),
    ("post", "/platform/auth/wallet/verify"): (400, 401, 403, 429, 503),
    ("post", "/platform/sales/inquiries"): (429,),
    ("post", "/platform/sales/bare-metal-inquiries"): (429,),
}

_RATE_HEADERS = {
    "X-RateLimit-Limit": {"description": "Requests this key may send at once (the burst).",
                          "schema": {"type": "integer"}},
    "X-RateLimit-Remaining": {"description": "Requests it may send right now.", "schema": {"type": "integer"}},
    "X-RateLimit-Reset": {"description": "Seconds until its allowance is full again.", "schema": {"type": "integer"}},
}
_RETRY_AFTER = {"Retry-After": {"description": "Seconds to wait before sending it again.",
                                "schema": {"type": "integer"}}}


def _components(spec: dict) -> dict:
    schemas = spec.setdefault("components", {}).setdefault("schemas", {})
    schemas["ApiError"] = {
        "title": "ApiError",
        "type": "object",
        "required": ["code", "message", "retryable"],
        "properties": {
            "code": {"type": "string", "enum": sorted(CODES), "description": "Stable: branch on this, never on text."},
            "message": {"type": "string", "description": "What happened and what to do next, fit to show a person."},
            "retryable": {"type": "boolean", "description": "Whether the same request may succeed later."},
            "retry_after_seconds": {"type": "integer", "description": "When to retry, if retryable."},
        },
    }
    schemas["ErrorResponse"] = {
        "title": "ErrorResponse",
        "type": "object",
        "required": ["detail", "error"],
        "properties": {
            "detail": {"description": "The specifics of this error: a string, or an object with more to act on."},
            "error": {"$ref": "#/components/schemas/ApiError"},
        },
    }
    schemas["ValidationErrorResponse"] = {
        "title": "ValidationErrorResponse",
        "type": "object",
        "required": ["detail", "error"],
        "properties": {
            "detail": {"type": "array", "items": {"$ref": "#/components/schemas/ValidationError"}},
            "error": {"$ref": "#/components/schemas/ApiError"},
        },
    }
    from greencompute_gateway.transport.schemas import APIKeyCreatePublic

    # POST /platform/api-keys documents the customer's body; the staff-only
    # fields of the model it parses (user_id, admin) stay out of the spec.
    schemas["APIKeyCreatePublic"] = APIKeyCreatePublic.model_json_schema(ref_template="#/components/schemas/{model}")
    spec["components"]["securitySchemes"] = {**SECURITY_SCHEMES, "btauth": _btauth_scheme()}
    return schemas


def _error_response(status: int) -> dict:
    codes = sorted(c for c, (s, _) in CODES.items() if s == status)
    text = _STATUS_TEXT.get(status, "Error")
    if codes:
        text += ". `error.code`: " + ", ".join(f"`{c}`" for c in codes)
    ref = "ValidationErrorResponse" if status == 422 else "ErrorResponse"
    out: dict[str, Any] = {"description": text + ".",
                           "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ref}"}}}}
    if status in (429, 503):
        out["headers"] = dict(_RETRY_AFTER)
    if status == 429:
        out["headers"].update(_RATE_HEADERS)
    return out


def _takes_key(route) -> bool:
    params = inspect.signature(route.endpoint).parameters.values()
    for p in params:
        default = p.default
        alias = getattr(default, "alias", None) or p.name
        if isinstance(default, HeaderParam) and alias.lower().replace("_", "-") in _AUTH_PARAMS:
            return True
    return False


def _signed_may(method: str, path: str) -> bool:
    from greencompute_gateway.domain.gamma import SIGNED_REQUEST_SCOPES
    from greencompute_gateway.domain.scopes import ANY_SCOPE, required_scope

    needed = required_scope(method, path)
    return needed == ANY_SCOPE or needed in SIGNED_REQUEST_SCOPES


def customize(app: FastAPI, spec: dict) -> dict:
    _components(spec)
    secured_ops: set[tuple[str, str]] = set()
    for route in app.routes:
        if getattr(route, "include_in_schema", False) and hasattr(route, "endpoint") and _takes_key(route):
            path = getattr(route, "path_format", route.path)  # "{image}", not "{image:path}"
            for method in getattr(route, "methods", ()) or ():
                if (method.lower(), path) not in OPTIONAL_KEY:
                    secured_ops.add((method.lower(), path))
    for path, ops in spec.get("paths", {}).items():
        for method, op in ops.items():
            if not isinstance(op, dict) or method not in ("get", "post", "put", "patch", "delete"):
                continue
            secured = (method, path) in secured_ops
            op["security"] = (KEY_OR_SIGNED if _signed_may(method, path) else KEY_REQUIRED) if secured else []
            params = [p for p in op.get("parameters", [])
                      if not (p.get("in") == "header" and p.get("name", "").lower() in _AUTH_PARAMS)]
            if params:
                op["parameters"] = params
            else:
                op.pop("parameters", None)
            statuses: set[int] = set(EXTRA_STATUSES.get((method, path), ()))
            if secured:
                statuses |= {401, 403, 429}
            if "{" in path:
                statuses.add(404)
            if method != "get":
                statuses |= {400, 409} if secured else {400}
            responses = op.setdefault("responses", {})
            if op.get("parameters") or op.get("requestBody"):
                statuses.add(422)
            else:
                responses.pop("422", None)  # it was for the key headers, now the schemes
            for status in sorted(statuses):
                responses[str(status)] = _error_response(status)
            if secured:
                for ok_status in ("200", "201"):
                    if ok_status in responses:
                        responses[ok_status].setdefault("headers", {}).update(_RATE_HEADERS)
    spec["security"] = KEY_REQUIRED
    import json

    if '"#/components/schemas/APIKeyCreate"' not in json.dumps(spec["paths"]):
        spec["components"]["schemas"].pop("APIKeyCreate", None)
    return spec


def install(app: FastAPI) -> None:
    """Replace app.openapi with the customised, cached spec."""

    def openapi() -> dict:
        if app.openapi_schema is None:
            spec = get_openapi(
                title=app.title, version=app.version, description=app.description, routes=app.routes,
            )
            app.openapi_schema = customize(app, spec)
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
