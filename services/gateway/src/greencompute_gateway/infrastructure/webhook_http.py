"""Sending webhook requests to customers' URLs without letting a URL reach
our own network, or hold a sender for long.

The URL is the customer's choice, and the gateway sits next to the control
plane, the database and the machines' LAN. So:
- HTTPS only, and no user:password@ in it;
- the host must resolve to public addresses only, checked at every send (DNS
  can change after the URL was saved), and the connection goes to the very
  address that was checked, with the certificate verified for the host name;
- our own servers' public addresses are refused too (GREENCOMPUTE_WEBHOOK_DENY_CIDRS):
  from inside, a connection to them could reach ports the firewall closes;
- no redirects are followed;
- only the status line of the reply is read, and the whole exchange has a
  hard deadline, so a slow or silent server holds one sender for at most
  about TIMEOUT_SECONDS;
- why a connection failed (refused, reset, TLS, timeout) is reported only as
  "couldn't reach it over HTTPS": the test endpoint must not work as a port
  scanner. The customer still sees any HTTP status their server returned.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import time
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 10.0
MAX_URL_CHARS = 2048
USER_AGENT = "GreenCompute-Webhooks/1"
UNREACHABLE = "could not reach the endpoint over HTTPS (connection, TLS certificate or timeout)"
# The validator/API server and the GPU machines (.88, .24, .12, .205) and the 5090 cluster.
_DEFAULT_DENY = "217.138.104.0/24,45.143.122.0/24"


def _denied_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Our servers, plus any GREENCOMPUTE_WEBHOOK_DENY_CIDRS adds (never fewer)."""
    raw = _DEFAULT_DENY + "," + os.environ.get("GREENCOMPUTE_WEBHOOK_DENY_CIDRS", "")
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                continue
    return out


class WebhookURLError(ValueError):
    """The URL can't be used for a webhook; the message says why."""


def check_url(url: object) -> tuple[str, int, str]:
    """(host, port, path with query) of a usable webhook URL."""
    if not isinstance(url, str) or not url.strip():
        raise WebhookURLError("url is required, e.g. https://example.com/green-compute")
    url = url.strip()
    if len(url) > MAX_URL_CHARS:
        raise WebhookURLError(f"url must be at most {MAX_URL_CHARS} characters")
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        raise WebhookURLError("url must not contain spaces or control characters")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise WebhookURLError("url must start with https://")
    if parts.username is not None or parts.password is not None:
        raise WebhookURLError("url must not contain a user name or password")
    host = parts.hostname
    if not host:
        raise WebhookURLError("url has no host")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise WebhookURLError("url has an invalid port") from exc
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return host, port, path


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        return False
    return not any(ip.version == net.version and ip in net for net in _denied_networks())


def resolve_public(host: str, port: int) -> list[str]:
    """The addresses of `host` to connect to, in order. Every address it
    resolves to must be public, else WebhookURLError."""
    # One message whether the name doesn't exist or points somewhere private,
    # and never the address: the API must not tell which internal names exist.
    refused = WebhookURLError(f"{host} doesn't resolve to a public address; use a public HTTPS endpoint")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise refused from exc
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or not all(_is_public(a) for a in addresses):
        raise refused
    return addresses


def _host_header(host: str, port: int) -> str:
    name = f"[{host}]" if ":" in host else host
    return name if port == 443 else f"{name}:{port}"


def post(url: str, body: bytes, headers: dict[str, str], *, resolve=resolve_public,
         timeout: float = TIMEOUT_SECONDS, context: ssl.SSLContext | None = None) -> tuple[int | None, str | None]:
    """POST `body` as JSON. (status, None) when the endpoint answered 2xx,
    otherwise (status or None, what went wrong)."""
    deadline = time.monotonic() + timeout

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no answer within {int(timeout)} s")
        return remaining

    try:
        host, port, path = check_url(url)
        addresses = resolve(host, port)
    except WebhookURLError as exc:
        return None, str(exc)
    if isinstance(addresses, str):
        addresses = [addresses]
    lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {_host_header(host, port)}",
        f"User-Agent: {USER_AGENT}",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        "Connection: close",
        *(f"{k}: {v}" for k, v in headers.items()),
    ]
    request = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
    context = context or ssl.create_default_context()  # tests pass one that trusts their own server
    reply = None
    sent = False
    for address in addresses:  # the next one only if this one couldn't be reached
        try:
            with socket.create_connection((address, port), timeout=left()) as raw:
                raw.settimeout(left())
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    tls.settimeout(left())
                    sent = True  # from here on, another address would be a second delivery
                    tls.sendall(request)
                    reply = b""
                    while b"\r\n" not in reply and len(reply) < 8192:
                        tls.settimeout(left())
                        chunk = tls.recv(4096)
                        if not chunk:
                            break
                        reply += chunk
            break
        except (OSError, ssl.SSLError, TimeoutError):
            reply = None
            if sent or deadline - time.monotonic() <= 0:
                break
    if reply is None:
        return None, UNREACHABLE
    status_line = reply.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    words = status_line.split()
    if len(words) < 2 or not words[0].startswith("HTTP/") or not words[1].isdigit():
        return None, f"not an HTTP reply: {status_line[:80]!r}"
    status = int(words[1])
    if 200 <= status < 300:
        return status, None
    return status, f"HTTP {status}"
