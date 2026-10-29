"""Readable billing: totals per deployment and per day, and monthly statements.

The ledger holds one row per metered minute, so a busy account has thousands
a day. An external API user (Oct 2026, requests #20 and #37) had to add them
up by hand; these read the same rows through one grouped query
(BillingRepository.spending_rows) and give them back as

- `spending_summary`: for a date range, what was charged per day and per
  deployment (with its name, the card it ran on, hours and state), and what
  was topped up;
- `monthly_statement`: one calendar month as statement lines (one per
  deployment per day per kind of charge, shared-model requests per day, and
  each top-up, refund or adjustment), with opening and closing balance;
- `statement_csv`: the same lines as CSV, for accounts and tax.

Hours come from the rate on each charge's ledger text, which the meter writes
(control-plane domain.metering.rental_charge_description and the private-model
description); tests pin the two formats together. Dates are UTC days.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

#: "GPU usage 2× GPU @ $0.40/hr" (rentals) and
#: "Private model Llama · 2 GPUs at $0.40/hr each" (private models).
_RENTAL_RATE = re.compile(r"(\d+)× GPU @ \$(\d+(?:\.\d+)?)/hr")
_MODEL_RATE = re.compile(r"(\d+) GPUs? at \$(\d+(?:\.\d+)?)/hr")
_STORAGE_RATE = re.compile(r"\$(\d+(?:\.\d+)?)/day")

#: How each non-metered ledger kind reads on a statement.
ENTRY_TYPES = {
    "topup": "Top-up",
    "refund": "Refund",
    "adjustment": "Adjustment",
    "chargeback": "Chargeback",
    "bonus": "Bonus",
}
CREDIT_KINDS = ("topup", "bonus")

MAX_RANGE_DAYS = 400


def cents_per_hour(description: str) -> int | None:
    """What a rental or private model was charged per hour, from its charge text."""
    m = _RENTAL_RATE.search(description) or _MODEL_RATE.search(description)
    if not m:
        return None
    return int(m.group(1)) * round(float(m.group(2)) * 100)


def _storage_cents_per_day(description: str) -> int | None:
    m = _STORAGE_RATE.search(description)
    return round(float(m.group(1)) * 100) if m else None


def _usd(cents: int) -> float:
    return round(cents / 100, 2)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return str(value)


class _Labels:
    """What each deployment was, for the customer: name, kind, the card it ran
    on, and its state now (running, paused, ended, deleted). From the
    deployment row while it exists, else from the snapshot kept when its
    workload was deleted, else just the id."""

    def __init__(self, control_plane) -> None:
        self.cp = control_plane
        self.cache: dict[str, dict[str, Any]] = {}
        self._gpu_by_node: dict[str, str] | None = None

    def _node_gpu(self, node_id: str | None) -> str | None:
        if self._gpu_by_node is None:
            self._gpu_by_node = {n.node_id: n.gpu_model for n in self.cp.repository.list_nodes()}
        return self._gpu_by_node.get(node_id) if node_id else None

    def get(self, deployment_id: str, description: str) -> dict[str, Any]:
        if deployment_id in self.cache:
            return self.cache[deployment_id]
        info: dict[str, Any] = {"name": f"Deployment {deployment_id[:8]}", "kind": "rental", "state": "deleted",
                                "gpu_model": None, "gpu_count": None}
        dep = self.cp.repository.get_deployment(deployment_id)
        if dep is not None:
            state = getattr(dep.state, "value", str(dep.state))
            info["state"] = {"suspended": "paused", "terminated": "ended", "failed": "ended"}.get(state, "running")
            wl = self.cp.repository.get_workload(dep.workload_id)
            if wl is not None:
                allowed = list(wl.requirements.supported_gpu_models or []) if wl.requirements else []
                info.update(
                    name=dep.name or wl.display_name or wl.name,
                    kind="model" if getattr(wl.kind, "value", str(wl.kind)) == "inference" else "rental",
                    gpu_model=self._node_gpu(dep.node_id) or (allowed[0] if allowed else None),
                    gpu_count=wl.requirements.gpu_count if wl.requirements else None,
                )
        else:
            snapshot = next(
                (e for e in reversed(self.cp.repository.list_deployment_events(deployment_id))
                 if e.get("event") == "deleted_with_workload"),
                None,
            )
            if snapshot:
                info.update(
                    name=snapshot.get("name") or snapshot.get("workload_name") or info["name"],
                    kind="model" if snapshot.get("workload_kind") == "inference" else "rental",
                    gpu_model=snapshot.get("gpu_model"),
                    gpu_count=snapshot.get("gpu_count"),
                )
            elif _MODEL_RATE.search(description):
                info["kind"] = "model"
        self.cache[deployment_id] = info
        return info


def _category(kind: str, label_kind: str) -> str:
    if kind == "storage":
        return "storage"
    if kind == "inference":
        return "shared_models"
    return "private_models" if label_kind == "model" else "gpu_rentals"


TYPE_NAMES = {
    "gpu_rentals": "GPU rental",
    "private_models": "Private model",
    "storage": "Saved-pod storage",
    "shared_models": "Shared models",
}


def parse_range(start: str | None, end: str | None, *, today: date | None = None) -> tuple[date, date]:
    """[start, end] as inclusive UTC days; the last 30 days by default."""
    today = today or datetime.now(UTC).date()
    try:
        last = date.fromisoformat(end) if end else today
        first = date.fromisoformat(start) if start else last - timedelta(days=29)
    except ValueError as exc:
        raise ValueError("dates must be YYYY-MM-DD") from exc
    if first > last:
        raise ValueError("`from` must not be after `to`")
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"at most {MAX_RANGE_DAYS} days per request")
    return first, last


def parse_month(month: str | None, *, today: date | None = None) -> tuple[date, date]:
    """A calendar month 'YYYY-MM' as (first day, first day of the next)."""
    today = today or datetime.now(UTC).date()
    if not month:
        first = today.replace(day=1)
    else:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("month must be YYYY-MM")
        try:
            first = date(int(month[:4]), int(month[5:]), 1)
        except ValueError as exc:
            raise ValueError("month must be YYYY-MM") from exc
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return first, nxt


def _bounds(first: date, end_exclusive: date) -> tuple[datetime, datetime]:
    return (datetime(first.year, first.month, first.day, tzinfo=UTC),
            datetime(end_exclusive.year, end_exclusive.month, end_exclusive.day, tzinfo=UTC))


def _groups(rows: dict, labels: _Labels) -> list[dict[str, Any]]:
    """The metered rows as one group per (deployment, kind, day): cents
    charged (positive), rows, hours or days, first and last charge."""
    out: dict[tuple, dict[str, Any]] = {}
    for ref, kind, day, desc, cents, n, first_at, last_at in rows["metered"]:
        charged = -cents
        key = (ref, kind, day)
        g = out.get(key)
        if g is None:
            info = labels.get(ref, desc) if ref else {"name": "Shared models", "kind": "shared", "state": None,
                                                     "gpu_model": None, "gpu_count": None}
            g = out[key] = {"ref": ref, "kind": kind, "day": day, "info": info,
                            "category": _category(kind, info["kind"]), "cents": 0, "rows": 0,
                            "hours": 0.0, "hours_known": True, "days": 0.0,
                            "first_at": first_at, "last_at": last_at}
        g["cents"] += charged
        g["rows"] += n
        g["first_at"] = min(g["first_at"], first_at)
        g["last_at"] = max(g["last_at"], last_at)
        if kind == "usage":
            rate = cents_per_hour(desc)
            if rate:
                g["hours"] += charged / rate
            else:
                g["hours_known"] = False
        elif kind == "storage":
            per_day = _storage_cents_per_day(desc)
            if per_day:
                g["days"] += charged / per_day
    return list(out.values())


def _quantity(g: dict[str, Any]) -> str | None:
    if g["kind"] == "usage" and g["hours_known"]:
        return f"{g['hours']:.1f} h"
    if g["kind"] == "storage" and g["days"]:
        return f"{g['days']:.0f} day{'s' if round(g['days']) != 1 else ''}"
    if g["kind"] == "inference":
        return f"{g['rows']} request{'s' if g['rows'] != 1 else ''}"
    return None


def spending_summary(billing_repo, control_plane, user_id: str, first: date, last: date) -> dict[str, Any]:
    since, until = _bounds(first, last + timedelta(days=1))
    rows = billing_repo.spending_rows(user_id, since, until)
    groups = _groups(rows, _Labels(control_plane))

    def blank():
        return {"charged": 0, "topped_up": 0, "adjustments": 0, "gpu_rentals": 0, "private_models": 0,
                "storage": 0, "shared_models": 0}

    days: dict[str, dict[str, int]] = {}
    for e in rows["entries"]:
        at = e.created_at if e.created_at.tzinfo else e.created_at.replace(tzinfo=UTC)
        d = days.setdefault(at.astimezone(UTC).date().isoformat(), blank())
        d["topped_up" if e.kind in CREDIT_KINDS else "adjustments"] += e.amount_cents
    deployments: dict[str, dict[str, Any]] = {}
    shared = {"cents": 0, "requests": 0}
    for g in groups:
        d = days.setdefault(g["day"], blank())
        d[g["category"]] += g["cents"]
        d["charged"] += g["cents"]
        if g["kind"] == "inference":
            shared["cents"] += g["cents"]
            shared["requests"] += g["rows"]
            continue
        info = g["info"]
        e = deployments.setdefault(g["ref"], {
            "deployment_id": g["ref"], "name": info["name"], "kind": info["kind"], "state": info["state"],
            "gpu_model": info.get("gpu_model"), "gpu_count": info.get("gpu_count"),
            "gpu_cents": 0, "storage_cents": 0, "hours": 0.0,
            "first_at": g["first_at"], "last_at": g["last_at"],
        })
        if g["kind"] == "storage":
            e["storage_cents"] += g["cents"]
        else:
            e["gpu_cents"] += g["cents"]
            e["hours"] += g["hours"]
        e["first_at"] = min(e["first_at"], g["first_at"])
        e["last_at"] = max(e["last_at"], g["last_at"])

    dep_list = []
    for e in deployments.values():
        total = e["gpu_cents"] + e["storage_cents"]
        dep_list.append({
            **{k: e[k] for k in ("deployment_id", "name", "kind", "state", "gpu_model", "gpu_count")},
            "hours": round(e["hours"], 2),
            "gpu_usd": _usd(e["gpu_cents"]),
            "storage_usd": _usd(e["storage_cents"]),
            "total_usd": _usd(total),
            "total_cents": total,
            "first_charge_at": _iso(e["first_at"]),
            "last_charge_at": _iso(e["last_at"]),
        })
    dep_list.sort(key=lambda x: (-x["total_cents"], x["name"]))
    return {
        "from": first.isoformat(),
        "to": last.isoformat(),
        "opening_balance_usd": _usd(rows["opening_cents"]),
        "closing_balance_usd": _usd(rows["closing_cents"]),
        "charged_usd": _usd(sum(v["charged"] for v in days.values())),
        "topped_up_usd": _usd(sum(v["topped_up"] for v in days.values())),
        "adjustments_usd": _usd(sum(v["adjustments"] for v in days.values())),
        "shared_models_usd": _usd(shared["cents"]),
        "shared_model_requests": shared["requests"],
        "days": [
            {"date": k, "charged_usd": _usd(v["charged"]), "topped_up_usd": _usd(v["topped_up"]),
             "adjustments_usd": _usd(v["adjustments"]), "gpu_rentals_usd": _usd(v["gpu_rentals"]),
             "private_models_usd": _usd(v["private_models"]), "storage_usd": _usd(v["storage"]),
             "shared_models_usd": _usd(v["shared_models"])}
            for k, v in sorted(days.items())
        ],
        "deployments": dep_list,
    }


def monthly_statement(billing_repo, control_plane, user_id: str, month: str | None) -> dict[str, Any]:
    first, nxt = parse_month(month)
    since, until = _bounds(first, nxt)
    rows = billing_repo.spending_rows(user_id, since, until)
    lines: list[dict[str, Any]] = []
    for g in _groups(rows, _Labels(control_plane)):
        info = g["info"]
        if g["kind"] == "inference":
            what = "Requests to shared models"
        else:
            gpus = f", {info['gpu_count']}× {info['gpu_model']}" if info.get("gpu_model") and info.get("gpu_count") else ""
            ended = " (deleted)" if info.get("state") == "deleted" else ""
            what = f"{info['name']}{gpus}{ended}"
        lines.append({
            "date": g["day"], "type": TYPE_NAMES[g["category"]], "description": what, "quantity": _quantity(g),
            "deployment_id": g["ref"], "amount_cents": -g["cents"],
            "_sort": (g["day"], 1, TYPE_NAMES[g["category"]], what),
        })
    for e in rows["entries"]:
        at = e.created_at if e.created_at.tzinfo else e.created_at.replace(tzinfo=UTC)
        lines.append({
            "date": at.astimezone(UTC).date().isoformat(),
            "type": ENTRY_TYPES.get(e.kind, e.kind.capitalize()),
            "description": e.description or "",
            "quantity": None,
            "deployment_id": None,
            "amount_cents": e.amount_cents,
            "_sort": (at.astimezone(UTC).date().isoformat(), 0, at.isoformat(), ""),
        })
    lines.sort(key=lambda x: x["_sort"])
    for line in lines:
        line.pop("_sort")
        line["amount_usd"] = _usd(line["amount_cents"])
    credits = sum(x["amount_cents"] for x in lines if x["amount_cents"] > 0)
    charges = -sum(x["amount_cents"] for x in lines if x["amount_cents"] < 0)
    by_type: dict[str, int] = {}
    for x in lines:
        if x["amount_cents"] < 0:
            by_type[x["type"]] = by_type.get(x["type"], 0) - x["amount_cents"]
    return {
        "month": first.strftime("%Y-%m"),
        "from": first.isoformat(),
        "to": (nxt - timedelta(days=1)).isoformat(),
        "opening_balance_cents": rows["opening_cents"],
        "closing_balance_cents": rows["closing_cents"],
        "credits_cents": credits,
        "charges_cents": charges,
        "charges_by_type_cents": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "lines": lines,
    }


def _cell(text: str) -> str:
    """Text for a CSV cell that a spreadsheet won't run as a formula:
    names and descriptions come from customers and admins."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def statement_csv(statement: dict[str, Any]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Green Compute statement", statement["month"]])
    w.writerow(["Period", f"{statement['from']} to {statement['to']} (UTC)"])
    w.writerow([])
    w.writerow(["date", "type", "description", "quantity", "deployment_id", "amount_usd"])
    w.writerow([statement["from"], "Opening balance", "", "", "", f"{statement['opening_balance_cents'] / 100:.2f}"])
    for x in statement["lines"]:
        w.writerow([x["date"], _cell(x["type"]), _cell(x["description"]), x["quantity"] or "",
                    x["deployment_id"] or "", f"{x['amount_cents'] / 100:.2f}"])
    w.writerow([statement["to"], "Closing balance", "", "", "", f"{statement['closing_balance_cents'] / 100:.2f}"])
    w.writerow([])
    w.writerow(["Total added", "", "", "", "", f"{statement['credits_cents'] / 100:.2f}"])
    w.writerow(["Total charged", "", "", "", "", f"{-statement['charges_cents'] / 100:.2f}"])
    return out.getvalue()
