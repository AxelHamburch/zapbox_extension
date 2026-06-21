"""Unit tests for the LNURL-auth (LUD-04) service side.

The security core is verify_lud04(): a DER-ECDSA/secp256k1 signature over the
raw 32 k1 bytes. We round-trip a signature with embit (the same library the
verifier uses) and assert tampering is rejected. The in-memory teach/rate-limit
helpers are exercised directly.
"""

import secrets

import pytest
from embit import ec

from ..views_auth import (
    _rate_limited,
    _teach_open,
    auth_k1_cache,
    teach_sessions,
    verify_lud04,
    _cb_hits,
    _now,
    CB_RATE_MAX,
)


def _sign(k1: bytes, priv: ec.PrivateKey) -> tuple[str, str]:
    """Return (sig_der_hex, pubkey_compressed_hex) for a raw 32-byte k1."""
    sig = priv.sign(k1)
    return sig.serialize().hex(), priv.get_public_key().serialize().hex()


def test_verify_valid_signature():
    priv = ec.PrivateKey(secrets.token_bytes(32))
    k1 = secrets.token_bytes(32)
    sig_hex, key_hex = _sign(k1, priv)
    assert verify_lud04(k1.hex(), sig_hex, key_hex) is True


def test_verify_rejects_tampered_k1():
    priv = ec.PrivateKey(secrets.token_bytes(32))
    k1 = secrets.token_bytes(32)
    sig_hex, key_hex = _sign(k1, priv)
    other_k1 = secrets.token_bytes(32)
    assert verify_lud04(other_k1.hex(), sig_hex, key_hex) is False


def test_verify_rejects_wrong_key():
    priv = ec.PrivateKey(secrets.token_bytes(32))
    other = ec.PrivateKey(secrets.token_bytes(32))
    k1 = secrets.token_bytes(32)
    sig_hex, _ = _sign(k1, priv)
    wrong_key = other.get_public_key().serialize().hex()
    assert verify_lud04(k1.hex(), sig_hex, wrong_key) is False


def test_verify_rejects_garbage():
    assert verify_lud04("00" * 32, "deadbeef", "00" * 33) is False
    assert verify_lud04("zz", "zz", "zz") is False
    # k1 must be exactly 32 bytes
    priv = ec.PrivateKey(secrets.token_bytes(32))
    k1 = secrets.token_bytes(32)
    sig_hex, key_hex = _sign(k1, priv)
    assert verify_lud04((k1 + b"\x00").hex(), sig_hex, key_hex) is False


def test_teach_open_expiry():
    dev = "dev_teach"
    teach_sessions.pop(dev, None)
    assert _teach_open(dev) is False
    teach_sessions[dev] = _now() + 60
    assert _teach_open(dev) is True
    teach_sessions[dev] = _now() - 1  # expired
    assert _teach_open(dev) is False
    # expired session is purged
    assert dev not in teach_sessions


def test_rate_limit():
    dev = "dev_rate"
    _cb_hits.pop(dev, None)
    for _ in range(CB_RATE_MAX):
        assert _rate_limited(dev) is False
    # one beyond the window cap is limited
    assert _rate_limited(dev) is True


def test_k1_cache_single_use_shape():
    # The cache value shape is (device_id, pin, duration, expiry); the callback
    # pops it after use so a replay finds nothing.
    k1 = secrets.token_hex(32)
    auth_k1_cache[k1] = ("dev", 5, 3000, _now() + 60)
    assert auth_k1_cache.get(k1)[0] == "dev"
    auth_k1_cache.pop(k1, None)
    assert auth_k1_cache.get(k1) is None


@pytest.mark.asyncio
async def test_router_imports():
    from fastapi import APIRouter

    from ..views_auth import zapbox_auth_router

    router = APIRouter()
    router.include_router(zapbox_auth_router)
