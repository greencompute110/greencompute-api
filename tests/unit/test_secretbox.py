"""AES-256-GCM sealing of custodied miner mnemonics.

A mnemonic here is a bearer credential for a provider's TAO. The properties
under test are the ones that decide whether a stolen database dump is inert or
catastrophic.
"""
import base64
import os

import pytest

from greencompute_validator.infrastructure.secretbox import (
    MASTER_KEY_ENV,
    AesGcmSecretBox,
    SecretBoxError,
    generate_master_key,
)

MNEMONIC = "abandon ability able about above absent absorb abstract absurd abuse access accident"


@pytest.fixture
def box():
    return AesGcmSecretBox(generate_master_key())


# --- Round trip --------------------------------------------------------------


def test_round_trip(box):
    sealed = box.encrypt(MNEMONIC, context="w1:coldkey")
    assert box.decrypt(sealed, context="w1:coldkey") == MNEMONIC


def test_ciphertext_does_not_contain_the_plaintext(box):
    sealed = box.encrypt(MNEMONIC, context="w1:coldkey")
    assert MNEMONIC not in sealed
    for word in MNEMONIC.split():
        assert word not in sealed


def test_same_plaintext_seals_differently_every_time(box):
    # A fresh nonce per call. Without this, identical ciphertexts would reveal
    # that two providers hold the same key material.
    a = box.encrypt(MNEMONIC, context="w1:coldkey")
    b = box.encrypt(MNEMONIC, context="w1:coldkey")
    assert a != b
    assert box.decrypt(a, context="w1:coldkey") == box.decrypt(b, context="w1:coldkey")


# --- The AAD binding: ciphertext is not transplantable ------------------------


def test_ciphertext_cannot_be_moved_to_another_wallet(box):
    # THE property that stops provider A's coldkey being pasted into provider
    # B's row and quietly decrypting into the wrong account.
    sealed = box.encrypt(MNEMONIC, context="w1:coldkey")
    with pytest.raises(SecretBoxError):
        box.decrypt(sealed, context="w2:coldkey")


def test_coldkey_ciphertext_cannot_be_read_as_a_hotkey(box):
    sealed = box.encrypt(MNEMONIC, context="w1:coldkey")
    with pytest.raises(SecretBoxError):
        box.decrypt(sealed, context="w1:hotkey")


def test_empty_context_is_refused_on_both_sides(box):
    # An empty AAD would silently drop the row-binding property.
    with pytest.raises(SecretBoxError, match="context is required"):
        box.encrypt(MNEMONIC, context="")
    with pytest.raises(SecretBoxError, match="context is required"):
        box.decrypt("v1:AAAA:AAAA", context="")


# --- Wrong key / tampering ---------------------------------------------------


def test_another_key_cannot_decrypt(box):
    sealed = box.encrypt(MNEMONIC, context="w1:coldkey")
    with pytest.raises(SecretBoxError):
        AesGcmSecretBox(generate_master_key()).decrypt(sealed, context="w1:coldkey")


def test_tampered_ciphertext_raises_rather_than_returning_garbage(box):
    # GCM is authenticated. Unauthenticated modes would hand back corrupted
    # bytes that we might then submit to a chain.
    scheme, nonce, payload = box.encrypt(MNEMONIC, context="w1:coldkey").split(":", 2)
    raw = bytearray(base64.b64decode(payload))
    raw[0] ^= 0x01
    tampered = f"{scheme}:{nonce}:{base64.b64encode(bytes(raw)).decode()}"
    with pytest.raises(SecretBoxError):
        box.decrypt(tampered, context="w1:coldkey")


def test_swapped_nonce_raises(box):
    a = box.encrypt(MNEMONIC, context="w1:coldkey")
    b = box.encrypt(MNEMONIC, context="w1:coldkey")
    frankenstein = f"v1:{a.split(':')[1]}:{b.split(':', 2)[2]}"
    with pytest.raises(SecretBoxError):
        box.decrypt(frankenstein, context="w1:coldkey")


def test_decrypt_error_does_not_reveal_which_check_failed(box):
    # Opaque by design, so this can't be used as an oracle.
    sealed = box.encrypt(MNEMONIC, context="w1:coldkey")
    wrong_key, wrong_ctx = None, None
    try:
        AesGcmSecretBox(generate_master_key()).decrypt(sealed, context="w1:coldkey")
    except SecretBoxError as exc:
        wrong_key = str(exc)
    try:
        box.decrypt(sealed, context="w2:coldkey")
    except SecretBoxError as exc:
        wrong_ctx = str(exc)
    assert wrong_key == wrong_ctx


@pytest.mark.parametrize(
    "bad",
    ["", "not-a-ciphertext", "v1:onlytwo", "v2:AAAA:AAAA", "v1:!!!:AAAA", "v1:AAAA:!!!"],
)
def test_malformed_ciphertext_is_rejected_cleanly(bad, box):
    with pytest.raises(SecretBoxError):
        box.decrypt(bad, context="w1:coldkey")


def test_short_nonce_is_rejected(box):
    payload = base64.b64encode(b"x" * 32).decode()
    with pytest.raises(SecretBoxError, match="nonce"):
        box.decrypt(f"v1:{base64.b64encode(b'short').decode()}:{payload}", context="w1:coldkey")


# --- Key handling ------------------------------------------------------------


def test_generated_key_is_256_bit_and_random():
    a, b = generate_master_key(), generate_master_key()
    assert len(base64.b64decode(a)) == 32
    assert a != b


def test_missing_master_key_refuses_to_start(monkeypatch):
    # Failing closed matters: silently running unencrypted would put plaintext
    # mnemonics in Postgres, and nothing downstream would notice.
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)
    with pytest.raises(SecretBoxError, match=MASTER_KEY_ENV):
        AesGcmSecretBox()


def test_key_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(MASTER_KEY_ENV, generate_master_key())
    sealed = AesGcmSecretBox().encrypt(MNEMONIC, context="w1:coldkey")
    assert sealed.startswith("v1:")


@pytest.mark.parametrize(
    "bad,match",
    [
        ("not base64!!", "base64"),
        (base64.b64encode(os.urandom(16)).decode(), "32 bytes"),
        (base64.b64encode(os.urandom(64)).decode(), "32 bytes"),
    ],
)
def test_malformed_master_key_is_rejected(bad, match):
    with pytest.raises(SecretBoxError, match=match):
        AesGcmSecretBox(bad)
