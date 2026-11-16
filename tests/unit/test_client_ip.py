"""Per-IP limits key on the client's real address. Behind nginx every request
comes from the docker bridge, so without the proxy's header all of the
internet would share one limit; with it, only nginx's own value counts."""
import types

from greencompute_gateway import main


def _req(headers, host="172.18.0.1"):
    return types.SimpleNamespace(headers=headers, client=types.SimpleNamespace(host=host))


def test_without_a_trusted_proxy_the_headers_are_ignored(monkeypatch):
    monkeypatch.setattr(main, "_TRUST_PROXY", False)
    assert main._client_ip_from_request(_req({"x-real-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8"})) == "172.18.0.1"


def test_behind_nginx_the_real_ip_header_wins(monkeypatch):
    monkeypatch.setattr(main, "_TRUST_PROXY", True)
    assert main._client_ip_from_request(_req({"x-real-ip": "198.51.100.9", "x-forwarded-for": "6.6.6.6, 198.51.100.9"})) == "198.51.100.9"


def test_a_spoofed_first_forwarded_for_entry_is_never_used(monkeypatch):
    monkeypatch.setattr(main, "_TRUST_PROXY", True)
    # nginx appended the real address; the client wrote the first one.
    assert main._client_ip_from_request(_req({"x-forwarded-for": "6.6.6.6, 198.51.100.9"})) == "198.51.100.9"
    assert main._client_ip_from_request(_req({})) == "172.18.0.1"
