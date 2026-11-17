"""Account secrets (/platform/secrets) and the workloads that use them.

A secret's value is written through the API and read back by nothing a
client can call: list, create, replace and delete answer with its name and
dates only. A workload refers to secrets by name,

    metadata.secret_env = {"HF_TOKEN": "hf-read", "POOL_PASS": "pool"}   (a pod)
    metadata.hf_token_secret = "hf-read"                                (a model)

and the values are filled in only when the control plane hands the workload
to the machine that runs one of its deployments (GET /miner/v1/workloads/{id}
from the miner holding its lease). They are never written into a workload or
deployment row, so no GET of those (nor events, logs or MCP) can carry one.

Names are unique per account for new secrets; rows made before that rule may
share a name, and then the most recently changed one is used.

A secret named `ssh-key:<label>` is the website's store for SSH *public* keys
(Rentals > SSH keys). Its value must be an SSH public key, which is not a
secret, so reads return it as `public_key`.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any

from sqlalchemy import select

from greencompute_persistence.db import session_scope
from greencompute_persistence.orm import UserSecretORM

log = logging.getLogger(__name__)

SECRET_ENV_KEY = "secret_env"
HF_TOKEN_SECRET_KEY = "hf_token_secret"
#: Workload metadata keys that name secrets (and hold no values).
REFERENCE_KEYS = (SECRET_ENV_KEY, HF_TOKEN_SECRET_KEY)

SSH_KEY_PREFIX = "ssh-key:"
_SSH_KEY_TYPES = (
    "ssh-ed25519", "ssh-rsa", "ssh-dss", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
)
_SSH_LINE = re.compile(r"^(\S+) ([A-Za-z0-9+/]+={0,3})(?: [^\r\n]*)?$")


def is_ssh_public_key(value: str | None) -> bool:
    """True for one OpenSSH public key line ("ssh-ed25519 AAAA… comment")
    whose base64 body names the same key type. A private key never is."""
    if not isinstance(value, str):
        return False
    match = _SSH_LINE.match(value.strip())
    if match is None or match.group(1) not in _SSH_KEY_TYPES:
        return False
    try:
        blob = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        return False
    if len(blob) < 4:
        return False
    size = int.from_bytes(blob[:4], "big")
    return blob[4:4 + size] == match.group(1).encode()


def public_key_of(name: str, value: str) -> str | None:
    """The SSH public key an `ssh-key:` secret holds, else None."""
    if name.startswith(SSH_KEY_PREFIX) and is_ssh_public_key(value):
        return value.strip()
    return None


def secret_references(metadata: dict | None) -> dict[str, str]:
    """Where a workload's metadata names secrets: {where: secret name},
    `where` being "secret_env.<VAR>" or "hf_token_secret". Malformed entries
    are left out (the gateway refuses them at create)."""
    meta = metadata or {}
    out: dict[str, str] = {}
    env = meta.get(SECRET_ENV_KEY)
    if isinstance(env, dict):
        for var, name in env.items():
            if isinstance(var, str) and isinstance(name, str) and name.strip():
                out[f"{SECRET_ENV_KEY}.{var}"] = name.strip()
    hf = meta.get(HF_TOKEN_SECRET_KEY)
    if isinstance(hf, str) and hf.strip():
        out[HF_TOKEN_SECRET_KEY] = hf.strip()
    return out


def secret_values(session_factory, user_id: str | None, names) -> dict[str, str]:
    """name -> value for those of `names` the account has; of several rows
    with one name (made before names were unique), the newest."""
    wanted = {n for n in names if n}
    if not user_id or not wanted:
        return {}
    with session_scope(session_factory) as session:
        rows = session.execute(
            select(UserSecretORM.name, UserSecretORM.value)
            .where(UserSecretORM.user_id == user_id, UserSecretORM.name.in_(sorted(wanted)))
            .order_by(UserSecretORM.updated_at.asc(), UserSecretORM.created_at.asc())
        ).all()
    return {name: value for name, value in rows}  # later (newer) rows win


def secret_names(session_factory, user_id: str | None) -> set[str]:
    if not user_id:
        return set()
    with session_scope(session_factory) as session:
        return set(session.scalars(select(UserSecretORM.name).where(UserSecretORM.user_id == user_id)).all())


def resolve_workload_secrets(workload, session_factory):
    """A copy of `workload` (a WorkloadSpec) with the secrets it names filled
    in: each secret_env entry added to metadata.env, hf_token_secret as
    metadata.hf_token. For the machine that runs it, never for a client. A
    name the account no longer has (deleted since) is left out, and logged."""
    meta: dict[str, Any] = dict(workload.metadata or {})
    refs = secret_references(meta)
    if not refs:
        return workload
    values = secret_values(session_factory, workload.owner_user_id, set(refs.values()))
    missing = sorted({name for name in refs.values() if name not in values})
    if missing:
        log.warning("workload %s: secrets %s no longer exist; started without them", workload.workload_id, missing)
    env = dict(meta.get("env") or {}) if isinstance(meta.get("env"), dict) else {}
    for where, name in refs.items():
        if name not in values:
            continue
        if where == HF_TOKEN_SECRET_KEY:
            meta["hf_token"] = values[name]
        else:
            env[where.split(".", 1)[1]] = values[name]
    if env:
        meta["env"] = env
    return workload.model_copy(update={"metadata": meta})
