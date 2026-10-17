"""workloads: move models to the shorter address form

Addresses were <whole name>-<id>, e.g. meta-llama-meta-llama-3-1-8b-i-7fb157;
new models now get a short form of the model's name, e.g. llama-3-1-8b-7fb157
(GatewayService._assign_ingress_host). This moves the models that still have
an address made the old way to the new form. An address its owner chose is
left alone. Pods are untouched.

Data only. Downgrade moves exactly these back.

Revision ID: 20261017_0068
Revises: 20261017_0067
Create Date: 2026-10-17
"""
import hashlib
import json
import os
import re

import sqlalchemy as sa

from alembic import op

revision = "20261017_0068"
down_revision = "20261017_0067"
branch_labels = None
depends_on = None

_DROP_PREFIX = ("meta",)
_DROP_ANYWHERE = ("instruct",)
_DROP_SUFFIX = ("chat", "it", "hf")
_SLUG_MAX = 24
_PLACEHOLDER_MODEL_ID = "sshleifer/tiny-gpt2"


def _zone() -> str:
    zone = os.environ.get("GREENCOMPUTE_MODEL_INGRESS_DOMAIN") or "inference.green-compute.com"
    return zone.strip().lower().strip(".")


def _old_label(name: str, workload_id: str, length: int) -> str:
    # The rule 20261014_0065 assigned with.
    base = (name or "").rsplit("/", 1)[-1].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:30].strip("-") or "model"
    return f"{slug}-{workload_id.replace('-', '')[:length]}"


def _label(name: str, workload_id: str, length: int, model_identifier: str | None = None) -> str:
    # Same rule as greencompute_gateway.application.services.default_ingress_label.
    words: list[str] = []
    for source in (model_identifier, name):
        if not source or source == _PLACEHOLDER_MODEL_ID or "://" in source:
            continue
        words = [w for w in re.split(r"[^a-z0-9]+", source.rsplit("/", 1)[-1].lower()) if w]
        if words:
            break
    if len(words) > 1 and words[0] in _DROP_PREFIX:
        words = words[1:]
    words = [w for w in words if w not in _DROP_ANYWHERE] or words
    while len(words) > 1 and words[-1] in _DROP_SUFFIX:
        words = words[:-1]
    slug = ""
    for word in words:
        longer = f"{slug}-{word}" if slug else word
        if len(longer) > _SLUG_MAX:
            break
        slug = longer
    slug = slug or (words[0][:_SLUG_MAX] if words else "model")
    hexed = workload_id.replace("-", "")
    short = hexed[:length] if re.fullmatch(r"[0-9a-f]+", hexed) else hashlib.sha1(workload_id.encode()).hexdigest()[:length]
    return f"{slug}-{short}"


def _model_identifier(runtime) -> str | None:
    if isinstance(runtime, str):
        try:
            runtime = json.loads(runtime)
        except ValueError:
            return None
    return runtime.get("model_identifier") if isinstance(runtime, dict) else None


def _move(old_rule, new_rule) -> None:
    """Give each model whose address follows old_rule the address new_rule
    makes, a longer id if that one is taken."""
    bind = op.get_bind()
    zone = _zone()
    taken = {h for (h,) in bind.execute(sa.text("SELECT ingress_host FROM workloads WHERE ingress_host IS NOT NULL"))}
    rows = bind.execute(sa.text(
        "SELECT workload_id, name, runtime, ingress_host FROM workloads "
        "WHERE kind = 'inference' AND ingress_host IS NOT NULL"
    )).all()
    for workload_id, name, runtime, host in rows:
        model_id = _model_identifier(runtime)
        if host not in {f"{old_rule(name, workload_id, n, model_id)}.{zone}" for n in (6, 9)}:
            continue
        for length in (6, 9):
            new_host = f"{new_rule(name, workload_id, length, model_id)}.{zone}"
            if new_host == host or new_host not in taken:
                break
        else:
            continue
        if new_host == host:
            continue
        taken.discard(host)
        taken.add(new_host)
        bind.execute(
            sa.text("UPDATE workloads SET ingress_host = :host WHERE workload_id = :id"),
            {"host": new_host, "id": workload_id},
        )


def upgrade() -> None:
    _move(lambda name, wid, n, _m: _old_label(name, wid, n), _label)


def downgrade() -> None:
    _move(_label, lambda name, wid, n, _m: _old_label(name, wid, n))
