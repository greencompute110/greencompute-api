"""The network report (application/network_report.py) over HTTP.

Public:
- GET /platform/report: the published report (GPUs by type and by farmer,
  revenue, buybacks). 404 until staff publish it. CORS *.

Staff (admin key), kept out of the public spec:
- GET  /platform/admin/report: the report with farmers' names and the week
  in progress, a preview of what the public gets, every machine, the
  farmers, staff debits and the settings, published or not.
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
    """Subnet 110's public figures: GPUs by type and by farmer (under
    anonymous labels), what customers paid per week by card, crypto and
    gamma, and how much of it has been bought back as SN110 alpha and
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
