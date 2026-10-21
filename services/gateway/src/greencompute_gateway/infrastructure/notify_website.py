"""Payment emails for users: a payment was added to their balance, or a
payment review was received or resolved. The gateway credits payments but the
email templates live in the website (greencompute-ui lib/billing-emails.ts),
so the gateway tells the website what happened and the website renders and
sends the email (POST /api/internal/notify).

- WEBSITE_NOTIFY_URL: where to post (default
  https://www.green-compute.com/api/internal/notify)
- GREENCOMPUTE_PROVISION_SECRET: the secret the website already uses to call
  the gateway, sent as X-Provision-Secret. Unset = no emails.
- GREENCOMPUTE_PAYMENT_EMAILS_ENABLED (default true): kill switch

Best-effort, like alerts.py: called only after a credit has committed, and
the user lookup and the POST run on a background thread with a short timeout
and one retry. A failure is logged (event type and HTTP status only, never
the address) and never reaches the credit. Each event carries an event_id the
website sends at most one email for, so the retry can't send it twice.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from uuid import uuid4

from greencompute_persistence import session_scope
from greencompute_persistence.orm import UserORM

log = logging.getLogger(__name__)

DEFAULT_NOTIFY_URL = "https://www.green-compute.com/api/internal/notify"
_TIMEOUT_SECONDS = 5.0
_RETRY_DELAY_SECONDS = 2.0


def _enabled() -> bool:
    flag = (os.environ.get("GREENCOMPUTE_PAYMENT_EMAILS_ENABLED") or "true").strip().lower()
    return flag not in ("0", "false", "no", "off")


def notify_user(session_factory, user_id: str, kind: str, fields: dict, *, event_id: str | None = None) -> None:
    """Email `user_id` about `kind` (payment_received, review_received,
    review_resolved) through the website, in the background. Never raises."""
    try:
        secret = (os.environ.get("GREENCOMPUTE_PROVISION_SECRET") or "").strip()
        if not secret or not _enabled() or not user_id:
            return
        url = (os.environ.get("WEBSITE_NOTIFY_URL") or "").strip() or DEFAULT_NOTIFY_URL
        body = {"type": kind, "event_id": event_id or f"{kind}:{uuid4().hex}", **fields}
        threading.Thread(
            target=_send, args=(session_factory, user_id, body, url, secret), daemon=True, name="payment-email"
        ).start()
    except Exception as exc:  # never let an email reach the credit
        log.warning("payment email (%s) not sent: %s", kind, type(exc).__name__)


def _recipient(session_factory, user_id: str) -> tuple[str, str | None] | None:
    """(email, name) to write to, or None for an account without an email
    address or one that was deleted (anonymised to "deleted-<id>")."""
    with session_scope(session_factory) as session:
        user = session.get(UserORM, user_id)
        if user is None:
            return None
        email = (user.email or "").strip()
        if not email or (user.username or "").startswith("deleted-"):
            return None
        return email, (user.display_name or user.username or "").strip() or None


def _send(session_factory, user_id: str, body: dict, url: str, secret: str) -> None:
    kind = body["type"]
    try:
        recipient = _recipient(session_factory, user_id)
    except Exception as exc:
        log.warning("payment email (%s) not sent: user lookup failed (%s)", kind, type(exc).__name__)
        return
    if recipient is None:
        return
    data = json.dumps({**body, "to": recipient[0], "name": recipient[1]}).encode()
    status = None
    for attempt in (1, 2):
        status = _post(url, secret, data)
        if status is not None and status < 500:
            if status >= 300:
                log.warning("payment email (%s) refused by the website: HTTP %s", kind, status)
            return
        if attempt == 1:
            time.sleep(_RETRY_DELAY_SECONDS)
    log.warning("payment email (%s) not sent: website unreachable (%s)", kind, status or "no response")


def _post(url: str, secret: str, data: bytes) -> int | None:
    """POST to the website; the HTTP status, or None when it can't be reached."""
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Provision-Secret": secret,
            # Hosts' firewalls often refuse urllib's default "Python-urllib/x.y".
            "User-Agent": "GreenCompute-Gateway/1.0 (payment emails)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
            resp.read()
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:
        # The body carries the user's address, so log the error kind only.
        log.info("payment email post failed: %s", type(exc).__name__)
        return None
