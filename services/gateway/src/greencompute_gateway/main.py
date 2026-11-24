import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse
from starlette.middleware.cors import CORSMiddleware

from greencompute_persistence import database_ready, load_runtime_settings, render_prometheus_text
from greencompute_gateway.transport.security import (
    current_client_ip,
    current_rate_limit,
    current_request_route,
    current_signed_request,
)
from greencompute_gateway.transport.security import metrics as gateway_metrics
from greencompute_gateway.transport import signed_requests
from greencompute_gateway.transport.gamma_routes import router as gamma_router
from greencompute_gateway.transport.mcp import router as mcp_router
from greencompute_gateway.transport.routes import router
from greencompute_gateway.transport.errors import CrashToJSON, install_error_handlers

settings = load_runtime_settings("greencompute-gateway")

# Trust the proxy's view of the client only when explicitly enabled: without
# a proxy a client could set these headers itself. In prod nginx sets
# X-Real-IP to $remote_addr (overwriting whatever the client sent) and appends
# $remote_addr to X-Forwarded-For, so the trustworthy hop is X-Real-IP, or the
# LAST X-Forwarded-For entry; the first is whatever the client wrote there.
# Without it every request looks like the docker bridge (one address for the
# whole internet), so per-IP limits would be global.
_TRUST_PROXY = (os.getenv("GREENCOMPUTE_TRUSTED_PROXY", "") or "").strip() in ("1", "true", "yes", "on")


def _client_ip_from_request(request) -> str | None:
    if _TRUST_PROXY:
        real = (request.headers.get("x-real-ip") or "").strip()
        if real:
            return real[:64]
        xff = request.headers.get("x-forwarded-for")
        if xff:
            last = xff.rsplit(",", 1)[-1].strip()
            if last:
                return last[:64]
    client = request.client
    return client.host if client else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Spawn long-running background tasks at startup; cancel them on
    shutdown: the on-chain crypto deposit watcher, the billing monitor,
    the gamma watcher (payments spent at our service hotkey), the credit
    watch (auto-resume after top-up, low-credit emails), fleets,
    webhook delivery, the daily sanctions-list refresh and the TLS
    certificate watch."""
    from greencompute_gateway.application.billing_monitor import billing_monitor_loop
    from greencompute_gateway.application.credit_watch import credit_watch_loop
    from greencompute_gateway.application.fleets import fleet_loop
    from greencompute_gateway.application.webhooks import webhook_loop
    from greencompute_gateway.application.capacity_alerts import capacity_alert_loop
    from greencompute_gateway.application.stopped_alerts import stopped_alert_loop
    from greencompute_gateway.application.sanctions import sanctions_refresh_loop
    from greencompute_gateway.application.tls_watch import tls_watch_loop
    from greencompute_gateway.infrastructure.deposit_watcher import (
        deposit_watcher_loop,
    )
    from greencompute_gateway.infrastructure.gamma_watcher import gamma_watcher_loop

    # More threads for the sync routes (AnyIO's default is 40); /v1
    # completions have their own (transport/v1_capacity).
    from greencompute_gateway.transport.v1_capacity import raise_default_pool

    raise_default_pool()
    tasks = [
        asyncio.create_task(deposit_watcher_loop(), name="deposit-watcher"),
        asyncio.create_task(gamma_watcher_loop(), name="gamma-watcher"),
        asyncio.create_task(billing_monitor_loop(), name="billing-monitor"),
        asyncio.create_task(credit_watch_loop(), name="credit-watch"),
        asyncio.create_task(fleet_loop(), name="fleets"),
        asyncio.create_task(webhook_loop(), name="webhooks"),
        asyncio.create_task(capacity_alert_loop(), name="capacity-alerts"),
        asyncio.create_task(stopped_alert_loop(), name="stopped-emails"),
        asyncio.create_task(sanctions_refresh_loop(), name="sanctions-lists"),
        asyncio.create_task(tls_watch_loop(), name="tls-watch"),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass



# Terminal tickets never reach uvicorn's log lines (its WebSocket handshake
# lines carry the query string).
from greencompute_gateway.application.terminal import install_log_redaction  # noqa: E402

install_log_redaction()

# The audit trail: routes.py logs `secret_access event=...` at INFO (who
# revealed an SSH key, ran exec, opened a terminal: never what), and nothing
# else at INFO. Python's default level is WARNING, so without this none of it
# was ever written. Keep INFO in that module for audit lines only. Its own
# handler prints them while nothing else would: the root logger has no
# handler at first, and a library adds one later at runtime (then that one
# prints them). Warnings and errors are left to the usual path either way.
_audit_log = logging.getLogger("greencompute_gateway.transport.routes")
_audit_log.setLevel(logging.INFO)


def _audit_unprinted(record: logging.LogRecord) -> bool:
    if record.levelno >= logging.WARNING:
        return False
    return not any(h.level <= record.levelno for h in logging.getLogger().handlers)


if not any(getattr(h, "greencompute_audit", False) for h in _audit_log.handlers):
    _audit_handler = logging.StreamHandler()
    _audit_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    _audit_handler.addFilter(_audit_unprinted)
    _audit_handler.greencompute_audit = True  # type: ignore[attr-defined]
    _audit_log.addHandler(_audit_handler)

app = FastAPI(
    title="Green Compute API",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "GPU rentals (RTX 4090 / 5090) and OpenAI-compatible inference on Bittensor "
        "subnet 110. Auth: `Authorization: Bearer <key>` (create one at "
        "https://www.green-compute.com/settings). Guides for humans and agents: "
        "https://www.green-compute.com/docs  ·  machine index: "
        "https://www.green-compute.com/llms.txt  ·  prices: GET /platform/pricing"
    ),
)

# Browser clients (Next.js, etc.) need CORS. Comma-separated origins in GREENCOMPUTE_CORS_ALLOW_ORIGINS.
# If unset, default to local Next.js dev URLs so the gateway never runs without CORS (avoids silent browser blocks).
# Use "*" only for local experiments (credentials disabled per spec). Production: set your public UI origin(s).
_cors_raw = os.getenv("GREENCOMPUTE_CORS_ALLOW_ORIGINS", "").strip()
_cors_fallback = "http://localhost:3000,http://127.0.0.1:3000"
_origins_line = _cors_raw if _cors_raw else _cors_fallback
_origins = [o.strip() for o in _origins_line.split(",") if o.strip()]
_wildcard = len(_origins) == 1 and _origins[0] == "*"
# Inside CORS (added before it), so a browser can read the JSON 500.
app.add_middleware(CrashToJSON)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _wildcard else _origins,
    allow_credentials=not _wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
    # "*" exposes every header only to requests without credentials; with
    # credentials browsers read it as a header named "*". So the ones clients
    # read are also named: paging (X-Next-Cursor, Link), Retry-After,
    # Idempotent-Replayed, the per-key rate limit and Deprecation.
    expose_headers=[
        "*", "X-Next-Cursor", "Link", "Retry-After", "Idempotent-Replayed",
        "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Deprecation",
    ],
)

class _RequestContext:
    """Records the per-request client IP in a ContextVar so the auth helpers
    can apply a per-IP auth-failure rate limit without threading `request`
    through every handler; likewise the route (for scopes) and a slot for
    the key's rate limit, whose X-RateLimit-* headers are added to the
    response as it starts.

    Plain ASGI, not @app.middleware("http"): Starlette's BaseHTTPMiddleware
    runs the app in a task of its own and relays the body through a stream,
    and a caller hanging up mid-stream left the inner answer stream (with
    its in-flight slot, its node connection and its billing) suspended in a
    reference cycle until the garbage collector ran, minutes to hours later.
    Nothing in this app's stack may be a BaseHTTPMiddleware (review H1)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.datastructures import MutableHeaders
        from starlette.requests import Request

        signed = None
        found = signed_requests.headers_of(scope)
        if found is not None:
            # A hotkey-signed request (btauth/1): its signature covers the
            # exact body, so read it first, then hand the app the same bytes.
            limit = signed_requests._max_body()
            body = await signed_requests.read_body(receive, limit)
            if body is None:
                await signed_requests.too_large(send, limit)
                return
            signed = signed_requests.check(scope, found, body)
            receive = signed_requests.replay(body, receive)
        signed_token = current_signed_request.set(signed)
        token = current_client_ip.set(_client_ip_from_request(Request(scope)))
        route_token = current_request_route.set((scope.get("method"), scope.get("path")))
        rate_state = {"path": scope.get("path"), "counted": False, "headers": None}
        rate_token = current_rate_limit.set(rate_state)

        async def send_with_rate_headers(message):
            if message["type"] == "http.response.start" and rate_state.get("headers"):
                headers = MutableHeaders(scope=message)
                for name, value in rate_state["headers"].items():
                    headers[name] = value
            await send(message)

        try:
            await self.app(scope, receive, send_with_rate_headers)
        finally:
            current_rate_limit.reset(rate_token)
            current_request_route.reset(route_token)
            current_client_ip.reset(token)
            current_signed_request.reset(signed_token)


app.add_middleware(_RequestContext)


app.include_router(router)
app.include_router(mcp_router)  # POST /mcp: the API as an MCP server
app.include_router(gamma_router)  # /gamma/*: paying with gamma (Subnet services standard)
install_error_handlers(app)


@app.get("/")
def api_index() -> dict:
    """What an agent sees when it opens the API host with no path.

    It used to be a bare 404, so the only way in was guessing /docs. Every link
    here is public and needs no key.
    """
    return {
        "name": "Green Compute API",
        "docs": "https://www.green-compute.com/docs",
        "llms_txt": "https://www.green-compute.com/llms.txt",
        "openapi": "https://api.green-compute.com/openapi.json",
        "interactive_docs": "https://api.green-compute.com/docs",
        "pricing": "https://api.green-compute.com/platform/pricing",
        "gpu_models": "https://api.green-compute.com/platform/nodes/supported",
        "status": "https://api.green-compute.com/platform/status",
        "inference_base_url": "https://api.green-compute.com/v1",
        "auth": "Authorization: Bearer <api key> -- create one at https://www.green-compute.com/settings",
        "wallet_sign_in": "No account yet? POST /platform/auth/wallet/challenge, sign the message with your "
                          "wallet, POST /platform/auth/wallet/verify: an API key, no email needed",
    }


@app.get("/healthz")
def healthcheck() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/readyz")
def readiness() -> dict[str, str]:
    ready, error = database_ready(settings.database_url)
    if not ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "error", "service": settings.service_name, "database_error": error},
        )
    return {"status": "ok", "service": settings.service_name, "database": "ok"}


@app.get("/_metrics", include_in_schema=False)
def prometheus_metrics() -> PlainTextResponse:
    return PlainTextResponse(
        render_prometheus_text(settings.service_name, gateway_metrics),
        media_type="text/plain; version=0.0.4",
    )


# The spec: auth schemes, coded error responses, rate-limit headers (after
# every route above is added; it is generated once, on first request).
from greencompute_gateway.transport import openapi as _openapi_doc  # noqa: E402

_openapi_doc.install(app)
