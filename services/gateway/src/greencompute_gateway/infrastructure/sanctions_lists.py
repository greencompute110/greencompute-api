"""The free sanctions sources wallets are screened against, and how to read
them. application/sanctions.py keeps a copy in the database and decides.

- OFAC's SDN list (US Treasury). Digital currency addresses sit in each
  entry's remarks ("Digital Currency Address - ETH 0x..."); remarks longer
  than 1000 characters continue in SDN_COMMENTS.CSV, sometimes in the middle
  of an address, so the two files are joined per entry before reading.
- The UK Sanctions List (FCDO). It replaced OFSI's Consolidated List on
  28 January 2026; addresses appear in free text ("Other Information").
- The Chainalysis sanctions oracle on Ethereum mainnet: a contract whose
  isSanctioned(address) answers for one EVM address, live.

Addresses are read from the text generically rather than by the lists'
wording: any 0x + 40 hex digits is an EVM address (the same key on every EVM
chain, whatever ticker the list gives it), and any base58 word that decodes
as SS58 is a Bittensor key (any SS58 prefix: one key, one owner).

Env vars (defaults are the official downloads):
- GREENCOMPUTE_SANCTIONS_OFAC_SDN_URL, GREENCOMPUTE_SANCTIONS_OFAC_COMMENTS_URL
- GREENCOMPUTE_SANCTIONS_UK_URL
- ETH_RPC_URL (the deposit watcher's) for the oracle
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import urllib.request

OFAC_SDN_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV"
OFAC_COMMENTS_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_COMMENTS.CSV"
UK_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv"

ORACLE_CONTRACT = "0x40C57923924B5c5c5455c48D93317139ADDaC8fb"
# keccak256("isSanctioned(address)")[:4]
_IS_SANCTIONED = "df592f7d"
_DEFAULT_ETH_RPC = "https://ethereum-rpc.publicnode.com"

# The UK list is about 50 MB; anything far past that is not the list.
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
_USER_AGENT = "Mozilla/5.0 (compatible; GreenCompute-SanctionsScreening/1.0)"

_EVM = re.compile(r"0x[0-9a-fA-F]{40}")
# Not preceded or followed by another base58 character, so a longer word
# isn't read as an address. 46-48 characters covers SS58 public keys with a
# one- or two-byte prefix.
_BASE58_WORD = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{46,48}(?![1-9A-HJ-NP-Za-km-z])")


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def ofac_urls() -> tuple[str, str]:
    return (
        _env("GREENCOMPUTE_SANCTIONS_OFAC_SDN_URL", OFAC_SDN_URL),
        _env("GREENCOMPUTE_SANCTIONS_OFAC_COMMENTS_URL", OFAC_COMMENTS_URL),
    )


def uk_url() -> str:
    return _env("GREENCOMPUTE_SANCTIONS_UK_URL", UK_URL)


def download(url: str, timeout: float = 120.0) -> bytes:
    """GET `url` (redirects followed: OFAC hands out a signed storage URL).
    Raises on an HTTP error, a timeout or an oversized body."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed official URLs
        body = resp.read(MAX_DOWNLOAD_BYTES + 1)
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"{url}: larger than {MAX_DOWNLOAD_BYTES} bytes")
    return body


def addresses_in(text: str) -> set[tuple[str, str]]:
    """Every (chain, canonical address) written in `text`."""
    from greencompute_gateway.infrastructure.wallet_signatures import InvalidAddress, normalize_address

    found = {("evm", m.group(0).lower()) for m in _EVM.finditer(text)}
    for m in _BASE58_WORD.finditer(text):
        try:
            found.add(("bittensor", normalize_address("bittensor", m.group(0))))
        except InvalidAddress:
            continue  # a base58 word that isn't an SS58 key (its checksum fails)
    return found


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def parse_ofac_sdn(sdn_csv: bytes, comments_csv: bytes | None) -> dict[tuple[str, str], str]:
    """(chain, address) -> the SDN entry's name. SDN.CSV has no header:
    ent_num, name, type, program, ..., remarks (the 12th column);
    SDN_COMMENTS.CSV is ent_num, the rest of the remarks."""
    csv.field_size_limit(10 * 1024 * 1024)
    rest: dict[str, str] = {}
    if comments_csv:
        for row in csv.reader(io.StringIO(_text(comments_csv))):
            if len(row) >= 2:
                rest[row[0].strip()] = rest.get(row[0].strip(), "") + row[1]
    out: dict[tuple[str, str], str] = {}
    for row in csv.reader(io.StringIO(_text(sdn_csv))):
        if len(row) < 12:
            continue
        remarks = row[11] + rest.get(row[0].strip(), "")
        if "Digital Currency Address" not in remarks and "0x" not in remarks:
            continue
        for key in addresses_in(remarks):
            out.setdefault(key, row[1].strip()[:255])
    return out


def parse_uk_list(raw: bytes) -> dict[tuple[str, str], str]:
    """(chain, address) -> the designated person's or entity's name. The CSV
    starts with a "Report Date" line, then a header; one row per name."""
    csv.field_size_limit(10 * 1024 * 1024)
    lines = _text(raw).splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines[:20]) if "Unique ID" in line), None)
    out: dict[tuple[str, str], str] = {}
    if start is None:
        # Not the format we know: still find the addresses, without names.
        return {key: "" for key in addresses_in("".join(lines))}
    for row in csv.DictReader(io.StringIO("".join(lines[start:]))):
        values = " ".join(v for v in row.values() if isinstance(v, str))
        if "0x" not in values and not _BASE58_WORD.search(values):
            continue
        name = (row.get("Name 6") or " ".join(row.get(f"Name {i}") or "" for i in range(1, 6))).strip()
        for key in addresses_in(values):
            if not out.get(key):
                out[key] = name[:255]
    return out


def eth_rpc_url() -> str:
    return _env("ETH_RPC_URL", _DEFAULT_ETH_RPC)


def oracle_is_sanctioned(address: str, timeout: float = 8.0) -> bool:
    """Ask the Chainalysis oracle about one EVM address. Raises when the RPC
    can't answer: the caller decides what an unknown means."""
    data = "0x" + _IS_SANCTIONED + "0" * 24 + address.lower().removeprefix("0x")
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": ORACLE_CONTRACT, "data": data}, "latest"],
    }).encode()
    req = urllib.request.Request(
        eth_rpc_url(), data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        reply = json.loads(resp.read())
    if "error" in reply:
        raise RuntimeError(f"oracle eth_call failed: {reply['error']}")
    result = reply.get("result")
    if not isinstance(result, str) or not result.startswith("0x") or len(result) != 66:
        raise RuntimeError(f"oracle returned {result!r}")
    return int(result, 16) != 0
