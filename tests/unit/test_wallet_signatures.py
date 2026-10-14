"""Wallet proofs: a signature over our message by the wallet's own key, in the
formats real wallets produce."""
import pytest
from eth_keys import keys
from eth_utils import keccak
from substrateinterface import Keypair, KeypairType

from greencompute_gateway.infrastructure.wallet_signatures import (
    InvalidAddress,
    normalize_address,
    verify_signature,
)

MESSAGE = "Green Compute: link this wallet to my account\nAccount: alice (u1)"


def _evm_sign(private_key: keys.PrivateKey, message: str, metamask_v: bool = True) -> str:
    data = message.encode()
    digest = keccak(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)
    sig = bytearray(private_key.sign_msg_hash(digest).to_bytes())
    if metamask_v:
        sig[64] += 27  # MetaMask and most wallets report v as 27/28
    return "0x" + sig.hex()


def test_sr25519_signature_from_btcli_or_substrate_interface():
    kp = Keypair.create_from_uri("//Alice")
    sig = "0x" + kp.sign(MESSAGE).hex()
    assert verify_signature("bittensor", kp.ss58_address, MESSAGE, sig)
    assert not verify_signature("bittensor", kp.ss58_address, MESSAGE + " (edited)", sig)
    assert not verify_signature("bittensor", Keypair.create_from_uri("//Bob").ss58_address, MESSAGE, sig)


def test_signature_without_0x_prefix_is_accepted():
    kp = Keypair.create_from_uri("//Alice")
    assert verify_signature("bittensor", kp.ss58_address, MESSAGE, kp.sign(MESSAGE).hex())


def test_ed25519_wallet():
    kp = Keypair.create_from_seed("0x" + "42" * 32, crypto_type=KeypairType.ED25519)
    assert verify_signature("bittensor", kp.ss58_address, MESSAGE, "0x" + kp.sign(MESSAGE).hex())


def test_polkadot_js_wraps_the_message_in_bytes_tags():
    kp = Keypair.create_from_uri("//Alice")
    wrapped = b"<Bytes>" + MESSAGE.encode() + b"</Bytes>"
    assert verify_signature("bittensor", kp.ss58_address, MESSAGE, "0x" + kp.sign(wrapped).hex())


@pytest.mark.parametrize("metamask_v", [True, False])
def test_evm_personal_sign(metamask_v):
    pk = keys.PrivateKey(bytes.fromhex("11" * 32))
    address = pk.public_key.to_checksum_address()
    sig = _evm_sign(pk, MESSAGE, metamask_v)
    assert verify_signature("evm", address, MESSAGE, sig)
    assert verify_signature("evm", address.lower(), MESSAGE, sig)
    other = keys.PrivateKey(bytes.fromhex("22" * 32)).public_key.to_checksum_address()
    assert not verify_signature("evm", other, MESSAGE, sig)
    assert not verify_signature("evm", address, MESSAGE + "!", sig)


@pytest.mark.parametrize("sig", ["", "zz", "0x1234", "0x" + "00" * 65])
def test_malformed_signatures_are_rejected_not_raised(sig):
    kp = Keypair.create_from_uri("//Alice")
    assert not verify_signature("bittensor", kp.ss58_address, MESSAGE, sig)
    assert not verify_signature("evm", "0x" + "ab" * 20, MESSAGE, sig)


def test_address_normalization():
    assert normalize_address("evm", "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01") == "0xabcdef0123456789abcdef0123456789abcdef01"
    alice = Keypair.create_from_uri("//Alice")
    # The same key in Polkadot's prefix-0 format maps to the generic prefix 42.
    polkadot_form = Keypair(public_key=alice.public_key, ss58_format=0).ss58_address
    assert normalize_address("bittensor", polkadot_form) == alice.ss58_address
    for chain, bad in [("evm", "0x123"), ("evm", "5F..."), ("bittensor", "0xabc"), ("solana", "x")]:
        with pytest.raises(InvalidAddress):
            normalize_address(chain, bad)
