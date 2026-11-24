"""Public gamma endpoints, as Bittensor's "Subnet services" standard asks:

- GET /gamma/services.json: the document the chain's service listing
  (`set_service(110, <service hotkey>, <this url>)`) points to. CORS *.
- GET /gamma/balance?hotkey=: what a hotkey's gamma was credited, used and
  has left, in USD. CORS *.
- GET /gamma/spent: what has been paid in gamma and bought back at our
  service, for anyone to check against the chain. CORS *.
- GET /platform/gamma: how to pay with gamma, and whether the chain side is
  switched on yet.
"""
from __future__ import annotations

from fastapi import APIRouter, Query
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
def gamma_balance(hotkey: str = Query(..., min_length=40, max_length=64,
                                      description="The hotkey (ss58) that spent the gamma")) -> JSONResponse:
    """What the hotkey's gamma spends at our service were credited, how much
    of that is used, and what is left, in USD as decimal strings; zeros for a
    hotkey with no account. Spends are credited within about a minute of
    finalizing (`as_of_block` is the last block read)."""
    try:
        return _json(gamma_app.balance(_session_factory(), hotkey.strip()))
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
    the watcher's last read (`chain`: gamma_enabled, service_url)."""
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
    }
    return _json(body)
