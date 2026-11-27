"""The network report (application/network_report.py) over HTTP.

Public:
- GET /platform/report: the published report (the network's GPUs, revenue
  per period by stream, buybacks). 404 until staff publish it. CORS *.

Staff (admin key), kept out of the public spec:
- GET  /platform/admin/report: the report with exact counts, farmers' names
  and the period in progress, a preview of what the public gets, every
  machine, the farmers, the contracts, staff debits and the settings,
  published or not.
- POST /platform/admin/contracts, PATCH /platform/admin/contracts/{id}
- POST /platform/admin/contracts/{id}/payments,
  PATCH and DELETE /platform/admin/contract-payments/{id}
- PUT  /platform/admin/report/settings
- POST /platform/admin/farmers, PATCH /platform/admin/farmers/{id}
- PUT  /platform/admin/machines/{node_id}: whose machine it is / hide it
- GET  /platform/admin/buyback: what is owed and the commands for it
- GET  /platform/admin/farmers/{id}/statement?month=YYYY-MM
- POST /platform/admin/farmers/{id}/payments, DELETE /platform/admin/farmer-payments/{id}
"""
from __future__ import annotations

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from greencompute_gateway.application import network_report as report
from greencompute_gateway.transport.errors import coded

router = APIRouter()


def _sf():
    from greencompute_gateway.transport import security

    return security.credential_store.session_factory


def _admin(authorization, x_api_key):
    from greencompute_gateway.transport import security

    return security.require_api_key(authorization, x_api_key, admin_required=True)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except report.ReportError as exc:
        raise coded(400, "invalid_request", str(exc)) from exc


def _found(value, what: str):
    if value is None or value is False:
        raise coded(404, "not_found", f"no such {what}")
    return value


@router.get("/platform/report", tags=["report"], summary="Network report: GPUs, revenue and buybacks")
def network_report() -> JSONResponse:
    """Subnet 110's public figures: the GPUs the network has, by type and by
    provider (under anonymous labels; counts are rounded down to a band
    unless `gpus.detail` is "exact"), revenue for each completed period
    (`period`: quarter by default) by stream (gamma, contracts, card,
    crypto), and how much of it has been bought back as SN110 alpha and
    burned, with the on-chain total to check it against. Refreshed every
    minute."""
    value = report.public_report(_sf())
    if value is None:
        raise coded(404, "not_found", "the network report isn't published yet")
    return JSONResponse(value, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=60"})


@router.get("/platform/admin/report", include_in_schema=False)
def admin_report(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _admin(authorization, x_api_key)
    sf = _sf()
    return {
        "settings": report.get_settings(sf),
        "report": report.build(sf, admin=True),   # up to now, with names
        "preview": report.build(sf),              # exactly what /platform/report gives once published
        "machines": report.machines(sf),
        "farmers": report.list_farmers(sf),
        "contracts": report.list_contracts(sf),
        "adjustments": report.adjustments(sf),
        "bases": report.BASES,
    }


@router.put("/platform/admin/report/settings", include_in_schema=False)
def admin_report_settings(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _admin(authorization, x_api_key)
    return _call(report.update_settings, _sf(), payload)


@router.post("/platform/admin/farmers", include_in_schema=False)
def admin_create_farmer(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _admin(authorization, x_api_key)
    return _call(report.create_farmer, _sf(), name=payload.get("name"), public_label=payload.get("public_label"),
                 terms=payload.get("terms"), notes=payload.get("notes"))


@router.patch("/platform/admin/farmers/{farmer_id}", include_in_schema=False)
def admin_update_farmer(
    farmer_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _admin(authorization, x_api_key)
    return _found(_call(report.update_farmer, _sf(), farmer_id, payload), "farmer")


@router.put("/platform/admin/machines/{node_id}", include_in_schema=False)
def admin_assign_machine(
    node_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """{"farmer_id": "<id>" | null, "hidden": true | false}; either may be left out."""
    _admin(authorization, x_api_key)
    kwargs = {}
    if "farmer_id" in payload:
        kwargs["farmer_id"] = payload["farmer_id"] or None
    if "hidden" in payload:
        kwargs["hidden"] = payload["hidden"]
    return _found(_call(report.assign_machine, _sf(), node_id, **kwargs), "machine")


@router.get("/platform/admin/buyback", include_in_schema=False)
def admin_buyback(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _admin(authorization, x_api_key)
    return report.buyback(_sf())


@router.get("/platform/admin/farmers/{farmer_id}/statement", include_in_schema=False)
def admin_farmer_statement(
    farmer_id: str,
    month: str = Query(..., description="YYYY-MM"),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _admin(authorization, x_api_key)
    return _found(_call(report.statement, _sf(), farmer_id, month), "farmer")


@router.post("/platform/admin/farmers/{farmer_id}/payments", include_in_schema=False)
def admin_record_farmer_payment(
    farmer_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    api_key = _admin(authorization, x_api_key)
    return _found(_call(
        report.record_payment, _sf(), farmer_id, month=payload.get("month"), amount_usd=payload.get("amount_usd"),
        reference=payload.get("reference"), note=payload.get("note"), paid_at=payload.get("paid_at"),
        created_by=getattr(api_key, "key_id", None),
    ), "farmer")


@router.delete("/platform/admin/farmer-payments/{payment_id}", include_in_schema=False)
def admin_delete_farmer_payment(
    payment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _admin(authorization, x_api_key)
    _found(report.delete_payment(_sf(), payment_id), "payment")
    return {"deleted": True, "payment_id": payment_id}


@router.post("/platform/admin/contracts", include_in_schema=False)
def admin_create_contract(
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """{"name", "starts_on", "farmer_id"?, "gpu_model"?, "gpu_count"?,
    "monthly_usd"?, "term_months"? or "ends_on"?, "notes"?}: a contract the
    sales team made directly, e.g. for one month, two months or a quarter."""
    _admin(authorization, x_api_key)
    return _call(report.create_contract, _sf(), payload)


@router.patch("/platform/admin/contracts/{contract_id}", include_in_schema=False)
def admin_update_contract(
    contract_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _admin(authorization, x_api_key)
    return _found(_call(report.update_contract, _sf(), contract_id, payload), "contract")


@router.post("/platform/admin/contracts/{contract_id}/payments", include_in_schema=False)
def admin_record_contract_payment(
    contract_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """{"received_on": "YYYY-MM-DD", "amount_usd": 12000, "reference"?, "note"?}:
    money received on the contract; it is revenue on that day."""
    api_key = _admin(authorization, x_api_key)
    return _found(_call(
        report.record_contract_payment, _sf(), contract_id, received_on=payload.get("received_on"),
        amount_usd=payload.get("amount_usd"), reference=payload.get("reference"), note=payload.get("note"),
        created_by=getattr(api_key, "key_id", None),
    ), "contract")


@router.patch("/platform/admin/contract-payments/{payment_id}", include_in_schema=False)
def admin_update_contract_payment(
    payment_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Any of {"received_on", "amount_usd", "reference", "note"}: correct a
    recorded payment."""
    _admin(authorization, x_api_key)
    return _found(_call(report.update_contract_payment, _sf(), payment_id, payload), "payment")


@router.delete("/platform/admin/contract-payments/{payment_id}", include_in_schema=False)
def admin_delete_contract_payment(
    payment_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _admin(authorization, x_api_key)
    _found(report.delete_contract_payment(_sf(), payment_id), "payment")
    return {"deleted": True, "payment_id": payment_id}
