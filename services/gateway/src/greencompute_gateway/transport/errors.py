"""Every error the API returns carries a machine-readable `error` object next
to the existing `detail` (which is unchanged, so nothing that reads it breaks):

    {"detail": ..., "error": {"code": "no_capacity", "message": "...",
                              "retryable": true, "retry_after_seconds": 60}}

- `code` is stable and separates the cases a client acts on differently:
  capacity, credit, authentication, permission, quota, validation, conflict.
  An external API user (Oct 2026, request #25) had to match message text such
  as "no compatible miner capacity" to decide whether to retry.
- `message` says in plain English what happened and what to do next (#33).
- `retryable` says whether the same request may succeed later, and
  `retry_after_seconds` when (from the Retry-After header when there is one).

Routes raise `coded(status, code, detail)` where the code matters (or
HTTPException as before, whose code is then inferred from the status);
`install_error_handlers` adds the object on the way out, also for request
validation errors, and `CrashToJSON` (added inside CORS, so browsers can read
it) turns an unexpected crash, which used to be plain-text "Internal Server
Error", into the same JSON.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

SUPPORT = "support@green-compute.com"

#: Codes whose request may succeed if sent again later, unchanged.
RETRYABLE = frozenset({
    "no_capacity", "rate_limited", "idempotency_in_progress", "model_not_ready", "unavailable", "server_error",
})

#: Every code, its usual status, and what it means: the table in the docs.
CODES: dict[str, tuple[int, str]] = {
    "invalid_request": (400, "The request can't be done as sent; `message` says why."),
    "gpu_unavailable": (400, "None of the GPU models asked for is offered; `detail.available` lists those that are."),
    "unauthenticated": (401, "The API key is missing or not valid."),
    "insufficient_balance": (402, "The balance doesn't cover an hour of what you're starting."),
    "forbidden": (403, "This key or account may not do this."),
    "scope_required": (403, "The key lacks a scope; `detail.required_scope` names it."),
    "key_limit": (403, "The key is at one of its limits (rentals at once, or spend per 24 hours)."),
    "not_found": (404, "No such resource on this account."),
    "unknown_endpoint": (404, "No such endpoint; check the path against /openapi.json."),
    "method_not_allowed": (405, "That HTTP method isn't used on this path."),
    "conflict": (409, "The resource's current state doesn't allow this."),
    "active_deployments": (409, "Stop the running rentals and models first."),
    "idempotency_in_progress": (409, "A request with this Idempotency-Key is still running."),
    "model_not_ready": (409, "The model has no replica ready yet (starting or moving); retry shortly."),
    "validation_error": (422, "Fields are missing or invalid; `detail` lists each one."),
    "idempotency_key_reused": (422, "This Idempotency-Key was used with a different body."),
    "rate_limited": (429, "Too many requests; wait `retry_after_seconds`."),
    "too_many_invoices": (429, "Too many unpaid crypto invoices are open."),
    "auth_throttled": (429, "Too many failed API-key checks from this address; fix the key, then wait."),
    "server_error": (500, "Something failed on our side."),
    "not_implemented": (501, "This endpoint is turned off."),
    "no_capacity": (503, "Every GPU that fits is in use; nothing was created. Retry after `retry_after_seconds`."),
    "unavailable": (503, "Temporarily unavailable."),
    "exec_unknown": (504, "A command was sent but no answer came back in time: it may or may not have run."),
}


def coded(status_code: int, code: str, detail: Any, headers: dict | None = None) -> HTTPException:
    """An HTTPException whose `error.code` is `code`, set where it's raised
    rather than inferred from its text. `detail` is unchanged on the wire."""
    exc = HTTPException(status_code=status_code, detail=detail, headers=headers)
    exc.error_code = code  # type: ignore[attr-defined]
    return exc


def _sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    first = text.split(" ", 1)[0]
    if first.isalpha() and first.islower():  # not "rtx4090", "metadata.env", …
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def _retry_after(headers: dict | None, detail: dict) -> int | None:
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            try:
                return int(v)
            except (TypeError, ValueError):
                break  # an HTTP-date or a fraction: fall back to the detail
    value = detail.get("retry_after_seconds")
    return int(value) if isinstance(value, (int, float)) else None


def _code(status: int, detail: Any, text: str, explicit: str | None = None) -> str:
    """The code set at the raise site, else one inferred from the status.
    Text matching is only a fallback for raise sites not yet coded."""
    if explicit:
        return explicit
    d = detail if isinstance(detail, dict) else {}
    if isinstance(d.get("code"), str) and d["code"]:
        return d["code"]
    lower = text.lower()
    if status == 400:
        return "gpu_unavailable" if "none of the requested gpu models" in lower else "invalid_request"
    if status == 401:
        return "unauthenticated"
    if status == 402:
        return "insufficient_balance"
    if status == 403:
        return "scope_required" if d.get("required_scope") else "forbidden"
    if status == 404:
        # Starlette's own reply for a path no route matches.
        return "unknown_endpoint" if text == "Not Found" else "not_found"
    if status == 405:
        return "method_not_allowed"
    if status == 409:
        return "idempotency_in_progress" if "idempotency-key" in lower else "conflict"
    if status == 422:
        return "idempotency_key_reused" if "idempotency-key" in lower else "validation_error"
    if status == 429:
        return "too_many_invoices" if "invoice" in lower else "rate_limited"
    if status == 501:
        return "not_implemented"
    if status == 503:
        return "unavailable"
    if status >= 500:
        return "server_error"
    return "invalid_request" if 400 <= status < 500 else "error"


def _message(code: str, detail: Any, text: str, retry_after: int | None) -> str:
    d = detail if isinstance(detail, dict) else {}
    if code == "unauthenticated":
        return ("Your API key is missing or not valid. Send it as `Authorization: Bearer <key>`; "
                "you can create one at https://www.green-compute.com/settings.")
    if code == "insufficient_balance":
        short = None
        if isinstance(d.get("required_cents"), int) and isinstance(d.get("current_cents"), int):
            short = max(0, d["required_cents"] - d["current_cents"])
        base = _sentence(text) or "Your balance is too low for this."
        add = f" Add at least ${short / 100:.2f}" if short else " Add credit"
        return f"{base}{add} at https://www.green-compute.com/billing, then send the request again."
    if code == "scope_required":
        return (f"This API key doesn't have the `{d.get('required_scope')}` scope. Use a key that has it, "
                "or create one at https://www.green-compute.com/settings.")
    if code == "gpu_unavailable":
        available = ", ".join(d.get("available") or []) or "none right now"
        return f"None of the GPUs you asked for is offered right now. You can rent: {available}."
    if code == "rate_limited":
        wait = f"{retry_after} seconds" if retry_after else "a minute"
        generic = not text or text.lower() == "rate limit exceeded"
        return f"Too many requests. Wait {wait}, then send it again." if generic else _sentence(text)
    if code == "auth_throttled":
        return ("Too many requests with a missing or wrong API key came from your address. Check the key "
                "(`Authorization: Bearer <key>`), then try again in a few minutes.")
    if code == "unknown_endpoint":
        return "There's no such endpoint. Check the path against https://api.green-compute.com/openapi.json."
    if code == "not_implemented":
        return _sentence(text) or "This endpoint is turned off."
    if code == "validation_error":
        first = None
        if isinstance(detail, list) and detail and isinstance(detail[0], dict):
            parts = list(detail[0].get("loc", []))
            if parts and parts[0] in ("body", "query", "path", "header", "cookie"):
                parts = parts[1:]  # where it came from, not a field name
            loc = ".".join(str(p) for p in parts)
            first = f"{loc}: {detail[0].get('msg')}" if loc else detail[0].get("msg")
        return (f"Some fields are missing or invalid ({first}). `detail` lists each one."
                if first else "Some fields are missing or invalid. `detail` lists each one.")
    if code == "server_error":
        return f"Something went wrong on our side. Try again in a minute; if it keeps happening, write to {SUPPORT}."
    if code == "not_found":
        return _sentence(text or "Not found") + " Check the id; it may belong to another account."
    if code == "unavailable" and not text:
        return "The service is busy. Try again in a minute."
    return _sentence(text) or _sentence(CODES.get(code, (0, "The request failed."))[1])


def describe_error(status: int, detail: Any, headers: dict | None = None, code: str | None = None) -> dict[str, Any]:
    d = detail if isinstance(detail, dict) else {}
    text = detail if isinstance(detail, str) else next(
        (d[k] for k in ("message", "reason", "error", "hint") if isinstance(d.get(k), str) and d[k]), "")
    code = _code(status, detail, text, code)
    retry_after = _retry_after(headers, d)
    out: dict[str, Any] = {
        "code": code,
        "message": _message(code, detail, text, retry_after),
        "retryable": code in RETRYABLE,
    }
    if retry_after is not None:
        out["retry_after_seconds"] = retry_after
    return out


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code in (204, 304):
            return Response(status_code=exc.status_code, headers=exc.headers)
        body = {"detail": exc.detail, "error": describe_error(
            exc.status_code, exc.detail, exc.headers, getattr(exc, "error_code", None))}
        return JSONResponse(jsonable_encoder(body), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        detail = jsonable_encoder(exc.errors())
        return JSONResponse({"detail": detail, "error": describe_error(422, detail)}, status_code=422)



class CrashToJSON:
    """ASGI middleware: an exception no handler took becomes the JSON 500
    above. Add it BEFORE CORSMiddleware (so CORS wraps it and a browser can
    read the reply); a crash after the response started is re-raised."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            if started:
                raise
            log.exception("unhandled error on %s %s", scope.get("method"), scope.get("path"))
            response = JSONResponse(
                {"detail": "internal server error", "error": describe_error(500, "internal server error")},
                status_code=500,
            )
            await response(scope, receive, send)
