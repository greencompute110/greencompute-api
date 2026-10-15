"""workloads: give every existing model its own address

New model workloads get ingress_host <name>-<id>.<zone> when they are
created (GatewayService._assign_ingress_host); this gives the models that
already exist one too, the same way. Pods are untouched.

Data only. Downgrade clears exactly the addresses this assigned.

Revision ID: 20261014_0065
Revises: 20261013_0064
Create Date: 2026-10-14
"""
import os
import re

import sqlalchemy as sa

from alembic import op

revision = "20261014_0065"
down_revision = "20261013_0064"
branch_labels = None
depends_on = None


def _zone() -> str:
    zone = os.environ.get("GREENCOMPUTE_MODEL_INGRESS_DOMAIN") or "inference.green-compute.com"
    return zone.strip().lower().strip(".")


def _label(name: str, workload_id: str, length: int) -> str:
    # Same rule as greencompute_gateway.application.services.default_ingress_label.
    base = (name or "").rsplit("/", 1)[-1].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:30].strip("-") or "model"
    return f"{slug}-{workload_id.replace('-', '')[:length]}"


def upgrade() -> None:
    bind = op.get_bind()
    zone = _zone()
    taken = {h for (h,) in bind.execute(sa.text("SELECT ingress_host FROM workloads WHERE ingress_host IS NOT NULL"))}
    rows = bind.execute(sa.text(
        "SELECT workload_id, name FROM workloads WHERE kind = 'inference' AND ingress_host IS NULL"
    )).all()
    for workload_id, name in rows:
        for length in (6, 9):
            host = f"{_label(name, workload_id, length)}.{zone}"
            if host not in taken:
                break
        else:
            continue
        taken.add(host)
        bind.execute(
            sa.text("UPDATE workloads SET ingress_host = :host WHERE workload_id = :id"),
            {"host": host, "id": workload_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    zone = _zone()
    rows = bind.execute(sa.text(
        "SELECT workload_id, name, ingress_host FROM workloads WHERE kind = 'inference' AND ingress_host IS NOT NULL"
    )).all()
    for workload_id, name, host in rows:
        if host in {f"{_label(name, workload_id, n)}.{zone}" for n in (6, 9)}:
            bind.execute(
                sa.text("UPDATE workloads SET ingress_host = NULL WHERE workload_id = :id"), {"id": workload_id}
            )
