"""Payment alerts for the team: a payment nobody was credited for, or a user
asking for a payment to be reviewed. Emailed to the support inbox (through
the website, infrastructure/notify_website.py), and posted to Slack and/or
Telegram when configured.

- GREENCOMPUTE_ALERT_SLACK_WEBHOOK_URL: a Slack incoming-webhook URL
- GREENCOMPUTE_ALERT_TELEGRAM_BOT_TOKEN + GREENCOMPUTE_ALERT_TELEGRAM_CHAT_ID

Sending happens on a background thread with a short timeout: an alert must
never slow down or break crediting.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0


def _requests(text: str) -> list[tuple[str, dict]]:
    """(url, JSON body) for each configured channel."""
    out: list[tuple[str, dict]] = []
    slack = (os.environ.get("GREENCOMPUTE_ALERT_SLACK_WEBHOOK_URL") or "").strip()
    if slack:
        out.append((slack, {"text": text}))
    token = (os.environ.get("GREENCOMPUTE_ALERT_TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("GREENCOMPUTE_ALERT_TELEGRAM_CHAT_ID") or "").strip()
    if token and chat:
        out.append((
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat, "text": text, "disable_web_page_preview": True},
        ))
    return out


def _post(url: str, body: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
            resp.read()
    except Exception as exc:  # never let an alert failure surface
        # Log the channel kind only: the URL carries the webhook or bot secret.
        log.warning("payment alert to %s failed: %s", "telegram" if "telegram" in url else "slack", exc)


def send_payment_alert(text: str, *, email_support: bool = True) -> None:
    """Log the alert, email it to the support inbox, and post it to every
    configured channel, in the background. `email_support=False` when the
    support inbox gets its own, fuller email about the same thing."""
    log.warning("payment alert: %s", text)
    if email_support:
        from greencompute_gateway.infrastructure.notify_website import notify_support

        notify_support(text)
    for url, body in _requests(text):
        threading.Thread(target=_post, args=(url, body), daemon=True, name="payment-alert").start()
