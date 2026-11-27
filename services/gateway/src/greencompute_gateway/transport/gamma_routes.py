"""Public gamma endpoints, as Bittensor's "Subnet services" standard asks:

- GET /gamma/services.json: the document the chain's service listing
  (`set_service(110, <service hotkey>, <this url>)`) points to. CORS *.
- GET /gamma/balance?hotkey=: what a hotkey's gamma was credited, used and
  has left, in USD. CORS *.
- GET /gamma/spent: what has been paid in gamma and bought back at our
  service, for anyone to check against the chain. CORS *.
- GET /platform/gamma: how to pay with gamma, whether the chain side is
  switched on yet, and where this week's gamma limit stands (`limit`).

An account's own (API key or signed request, scope read):
- GET /platform/billing/gamma: what its hotkeys spent here, what of each
  spend is credited and what still waits for the weekly limit.

Staff (admin key), kept out of the public spec:
- GET /platform/admin/gamma: the weekly limit, this week, what is waiting.
- PUT /platform/admin/gamma/settings: {"weekly_limit_usd": 1000 | null}.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from greencompute_gateway.application import gamma as gamma_app
from greencompute_gateway.domain import gamma
from greencompute_gateway.transport.errors import coded

router = APIRouter()

_PUBLIC = {"Access-Control-Allow-Origin": "*"}


def _session_factory():
    from greencompute_gateway.transport import security

    return security.credential_store.session_factory


def _json(body: dict, *, max_age: int = 0) -> JSONResponse:
    headers = dict(_PUBLIC)
    headers["Cache-Control"] = f"public, max-age={max_age}" if max_age else "no-store"
    return JSONResponse(body, headers=headers)


@router.get("/gamma/services.json", tags=["gamma"], summary="Service document (Subnet services standard)")
def services_document() -> JSONResponse:
    """What we serve and how it's paid for: the document our on-chain service
    listing points to. Pay with gamma: spend SN110 gamma at the publisher
    hotkey, then sign requests with the hotkey that spent (btauth/1)."""
    return _json(gamma.services_document(), max_age=300)


@router.get("/gamma/service.json", include_in_schema=False)
def services_document_alias() -> JSONResponse:
    return services_document()


@router.get("/gamma/balance", tags=["gamma"], summary="A hotkey's gamma balance")
def gamma_balance(hotkey: str | None = Query(None, min_length=40, max_length=64,
                                             description="The hotkey (ss58) that spent the gamma; "
                                                         "optional on a request the hotkey signs")) -> JSONResponse:
    """What the hotkey's gamma spends at our service were credited
    (`credited_usd`, public: the chain shows the spends), and what of it is
    used and left, in USD as decimal strings. `used_usd` and `remaining_usd`
    are given when the request is signed by the hotkey itself (btauth/1) or
    the account is nothing but that hotkey's gamma; otherwise they are null,
    so nobody can read another account's balance from its wallet address.
    `waiting_usd` is gamma the hotkey spent that is waiting for our weekly
    gamma limit: it is credited in a later week, oldest spend first.
    Spends are credited within about a minute of finalizing (`as_of_block`
    is the last block read)."""
    from greencompute_gateway.transport import security

    signed = security.current_signed_request.get()
    signed_by = None
    if signed is not None:
        if signed.error is not None or not signed.hotkey:
            raise coded(401, "signed_request_invalid", {
                "message": f"signed request refused: {getattr(signed.error, 'message', 'not valid')}",
                "reason": getattr(signed.error, "reason", "malformed"),
            })
        signed_by = signed.hotkey
    target = (hotkey or signed_by or "").strip()
    if not target:
        raise coded(400, "invalid_request", "hotkey is required: ?hotkey=<ss58>, or sign the request with it")
    try:
        return _json(gamma_app.balance(_session_factory(), target, signed_by=signed_by))
    except ValueError:
        raise coded(400, "invalid_request", "hotkey must be an ss58 address, e.g. 5F…") from None


@router.get("/gamma/spent", tags=["gamma"], summary="Gamma paid and bought back at our service")
def gamma_spent() -> JSONResponse:
    """Everything spent at our service hotkeys as the watcher last read it:
    customers' gamma payments, buybacks spent here by our treasury hotkey,
    and spends that were blocked. The chain's GammaSpent at the service
    hotkeys is the source; this is a convenience."""
    return _json(gamma_app.spent_totals(_session_factory()), max_age=30)


@router.get("/platform/gamma", tags=["gamma"], summary="How to pay with gamma")
def gamma_info() -> JSONResponse:
    """The netuid, our service hotkey, the commands to mint and spend gamma,
    how to sign requests, and what the chain says about gamma on SN110 as of
    the watcher's last read (`chain`: gamma_enabled, service_url).

    `limit`: we credit gamma up to a weekly limit (`weekly_usd`, null when
    there is none; weeks run Monday to Sunday, UTC). Gamma spent above what
    is left (`remaining_this_week_usd`) is never lost: it waits and is
    credited in a later week, oldest spend first. `credits_now` says
    whether gamma spent now is credited at once, `next_credit_from` the day
    it would start to be credited otherwise."""
    status = gamma_app.chain_status(_session_factory()) or {}
    body = {
        **gamma.how_to_pay(),
        "services_document": gamma.services_url(),
        "balance": f"{gamma.API_BASE}/gamma/balance?hotkey=<hotkey>",
        "signed_requests": {
            "standard": "btauth/1 (https://www.bittensor.com/docs/guides/signed-requests)",
            "receiver": gamma.service_hotkey(),
            "scopes": gamma.SIGNED_REQUEST_SCOPES,
            "headers": ["X-Bittensor-Version: 1", "X-Bittensor-Hotkey", "X-Bittensor-Receiver",
                        "X-Bittensor-Nonce (unix ns)", "X-Bittensor-Signature (0x hex)"],
            "note": "With these headers any Authorization header is ignored: OpenAI SDKs may send a placeholder key.",
        },
        "chain": status,
        "accepting": bool(status.get("gamma_enabled") and status.get("service_url")),
        "limit": gamma_app.limit_status(_session_factory()),
    }
    return _json(body)


@router.get("/platform/billing/gamma", tags=["gamma"], summary="Your gamma payments, credited and waiting")
def my_gamma(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """What your hotkeys spent at our service: for each spend the gamma, what
    of it is credited (`credited_usd`) and what still waits for our weekly
    gamma limit (`waiting_usd`), newest first, with the totals and where the
    week stands (`limit`). Gamma that waits is never lost: it is credited in
    a later week, oldest spend first."""
    from greencompute_gateway.transport import security

    api_key = security.require_api_key(authorization, x_api_key)
    if api_key.user_id is None:
        raise coded(403, "forbidden", "api key must be bound to a user")
    return gamma_app.account_gamma(_session_factory(), api_key.user_id)


# --- staff ----------------------------------------------------------------------------


def _admin(authorization, x_api_key):
    from greencompute_gateway.transport import security

    return security.require_api_key(authorization, x_api_key, admin_required=True)


def _admin_view() -> dict:
    sf = _session_factory()
    status = gamma_app.chain_status(sf)
    return {
        "settings": gamma_app.get_settings(sf),
        "limit": gamma_app.limit_status(sf, fresh=True),
        "waiting": gamma_app.waiting_spends(sf),
        "chain": status,
        "accepting": bool(status and status.get("gamma_enabled") and status.get("service_url")),
    }


@router.get("/platform/admin/gamma", include_in_schema=False)
def admin_gamma(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _admin(authorization, x_api_key)
    return _admin_view()


@router.put("/platform/admin/gamma/settings", include_in_schema=False)
def admin_gamma_settings(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """{"weekly_limit_usd": 1000} sets the weekly limit, null removes it, 0
    makes every spend wait. Gamma already waiting is credited by the next
    watcher tick as far as the new limit allows."""
    _admin(authorization, x_api_key)
    if not isinstance(payload, dict) or set(payload) != {"weekly_limit_usd"}:
        raise coded(400, "invalid_request", 'send {"weekly_limit_usd": <number or null>}')
    try:
        gamma_app.set_weekly_limit(_session_factory(), payload["weekly_limit_usd"])
    except gamma_app.GammaSettingsError as exc:
        raise coded(400, "invalid_request", str(exc)) from exc
    return _admin_view()
