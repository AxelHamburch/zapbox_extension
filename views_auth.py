"""LNURL-auth (LUD-04) — service side for the ZapBox.

A known wallet identifies itself per LNURL-auth and the device triggers its
relay — analogous to the payment flow, but nothing is paid. v1 is plain
LNURL-auth (phone wallets). See temp/lnurlauth/lnurlauth-plan.md.

This module is the LN SERVICE: it hands out auth challenges (k1), verifies the
wallet's signature with embit (already an LNbits dependency), and on success
pushes the existing "<pin>-<duration>" WebSocket trigger to the device.
"""

import asyncio
import json
import secrets
import time
from http import HTTPStatus

from embit import ec
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from lnbits.core.models import User
from lnbits.core.services import websocket_updater
from lnbits.decorators import check_user_exists
from lnurl import encode as lnurl_encode
from loguru import logger

from .crud import (
    create_auth_key,
    delete_auth_key,
    get_auth_key,
    get_auth_key_by_pubkey,
    get_auth_keys,
    get_zapbox,
    update_auth_key,
    update_zapbox,
)
from .models import AuthKey, CreateAuthKey, UpdateAuthKey

zapbox_auth_router = APIRouter(prefix="/api/v1")

K1_TTL = 120          # seconds — auth challenge lifetime (single use)
TEACH_TTL = 300       # seconds — open teach session lifetime (5 min)
MAX_TEACH_ATTEMPTS = 3
DEFAULT_AUTH_PIN = 5          # touch 3.5 CH01 relay GPIO
DEFAULT_AUTH_DURATION = 3000  # ms

# In-memory stores (analogous to pin_sessions in views_api.py).
# auth_k1_cache: k1 hex -> (device_id, pin, duration, expiry_ts). Single use.
auth_k1_cache: dict[str, tuple[str, int, int, float]] = {}
# teach_sessions: device_id -> expiry_ts. A register callback is only accepted
# while an open session exists for the device.
teach_sessions: dict[str, float] = {}
# teach_attempts: device_id -> consecutive wrong PIN count.
teach_attempts: dict[str, int] = {}
# Light per-device rate limit on the callback (replay / brute force).
_cb_hits: dict[str, list[float]] = {}
CB_RATE_WINDOW = 10   # seconds
CB_RATE_MAX = 12      # callback attempts per window per device


def _now() -> float:
    return time.time()


def _purge_k1() -> None:
    now = _now()
    for k1 in [k for k, v in auth_k1_cache.items() if v[3] < now]:
        auth_k1_cache.pop(k1, None)


def _teach_open(device_id: str) -> bool:
    expiry = teach_sessions.get(device_id)
    if expiry is None:
        return False
    if expiry < _now():
        teach_sessions.pop(device_id, None)
        return False
    return True


def _rate_limited(device_id: str) -> bool:
    now = _now()
    hits = [t for t in _cb_hits.get(device_id, []) if t > now - CB_RATE_WINDOW]
    hits.append(now)
    _cb_hits[device_id] = hits
    return len(hits) > CB_RATE_MAX


def verify_lud04(k1_hex: str, sig_hex: str, key_hex: str) -> bool:
    """Verify a LUD-04 signature: DER-ECDSA/secp256k1 over the raw 32 k1 bytes.

    The wallet signs the raw k1 (k1 itself acts as the message digest, no extra
    hashing — see BuhoGo src/utils/identityCrypto.js, prehash:false).
    """
    try:
        k1 = bytes.fromhex(k1_hex)
        if len(k1) != 32:
            return False
        pubkey = ec.PublicKey.parse(bytes.fromhex(key_hex))
        sig = ec.Signature.parse(bytes.fromhex(sig_hex))
        return bool(pubkey.verify(sig, k1))
    except Exception as exc:
        logger.debug(f"LNURL-auth verify failed: {exc}")
        return False


async def _teach_timeout(device_id: str, expiry: float) -> None:
    """Close the teach session at the server-side timeout and tell the device
    to return to normal operation (the device also has its own 5-min backup)."""
    await asyncio.sleep(max(0.0, expiry - _now()))
    if teach_sessions.get(device_id) == expiry:
        teach_sessions.pop(device_id, None)
        await websocket_updater(device_id, json.dumps({"event": "teach_ended"}))
        logger.info(f"LNURL-auth teach session expired: device={device_id}")


# --------------------------------------------------------------------------- #
# Teach session control (PIN protected, server-side)
# --------------------------------------------------------------------------- #


@zapbox_auth_router.post("/auth/teach/start")
async def api_auth_teach_start(
    device_id: str = Query(...),
    pin: str = Query(...),
) -> dict:
    zapbox = await get_zapbox(device_id)
    if not zapbox:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Device not found.")
    if not zapbox.auth_enabled:
        return {"status": "ERROR", "reason": "disabled"}
    if not zapbox.touch_enabled:
        return {"status": "ERROR", "reason": "locked"}
    if not zapbox.teach_pin:
        return {"status": "ERROR", "reason": "no_teach_pin"}

    if pin != zapbox.teach_pin:
        attempts = teach_attempts.get(device_id, 0) + 1
        teach_attempts[device_id] = attempts
        if attempts >= MAX_TEACH_ATTEMPTS:
            zapbox.touch_enabled = False
            await update_zapbox(zapbox)
            teach_attempts.pop(device_id, None)
            logger.warning(f"LNURL-auth teach locked after {attempts} wrong PINs: device={device_id}")
            return {"status": "ERROR", "reason": "locked", "remaining": 0}
        return {
            "status": "ERROR",
            "reason": "wrong_pin",
            "remaining": MAX_TEACH_ATTEMPTS - attempts,
        }

    # Correct PIN — open a time-boxed teach session.
    teach_attempts.pop(device_id, None)
    expiry = _now() + TEACH_TTL
    teach_sessions[device_id] = expiry
    asyncio.create_task(_teach_timeout(device_id, expiry))
    logger.info(f"LNURL-auth teach session opened: device={device_id}")
    return {"status": "OK", "ttl": TEACH_TTL}


@zapbox_auth_router.post("/auth/teach/stop")
async def api_auth_teach_stop(device_id: str = Query(...)) -> dict:
    teach_sessions.pop(device_id, None)
    return {"status": "OK"}


# --------------------------------------------------------------------------- #
# Auth challenge + callback (LUD-04 service side)
# --------------------------------------------------------------------------- #


@zapbox_auth_router.get("/auth/{device_id}")
async def api_auth_lnurl(
    request: Request,
    device_id: str,
    pin: int = Query(DEFAULT_AUTH_PIN),
    duration: int = Query(DEFAULT_AUTH_DURATION),
) -> dict:
    """Device asks for an auth LNURL. Creates+caches a fresh k1 and returns the
    bech32 LNURL. action=register while a teach session is open, else auth.
    The pin/duration the device passes here are cached with the k1 and used as
    the relay trigger on a successful auth callback."""
    zapbox = await get_zapbox(device_id)
    if not zapbox:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Device not found.")
    if zapbox.disabled:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN, detail=f"ZapBox {device_id} is disabled"
        )
    if not zapbox.auth_enabled:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN, detail="Identities (LNURL-auth) are disabled"
        )

    _purge_k1()
    k1 = secrets.token_hex(32)
    auth_k1_cache[k1] = (device_id, pin, duration, _now() + K1_TTL)

    action = "register" if _teach_open(device_id) else "auth"
    cb = str(request.url_for("zapbox.auth_cb", device_id=device_id))
    url = f"{cb}?tag=login&k1={k1}&action={action}"
    # NOTE: lnurl.encode(url) returns an Lnurl whose str() is the *plain URL*;
    # the bech32 carrier ("lnurl1…") lives in .bech32. Wallets only recognise
    # LNURL-auth from the bech32 (or keyauth://) form — sending the raw URL makes
    # them treat it as a payment. Lowercased to match the canonical QR form.
    lnurl = str(lnurl_encode(url).bech32).lower()
    return {"lnurl": lnurl, "k1": k1, "action": action}


@zapbox_auth_router.get("/auth/cb/{device_id}", name="zapbox.auth_cb")
async def api_auth_cb(
    device_id: str,
    k1: str = Query(...),
    sig: str = Query(...),
    key: str = Query(...),
    action: str = Query("auth"),
) -> dict:
    """LUD-04 callback hit by the wallet. Verifies the signature, then either
    enrols the key (register, only with an open teach session) or triggers the
    relay (auth, only for a known enabled key)."""
    if _rate_limited(device_id):
        logger.warning(f"LNURL-auth callback rate limited: device={device_id}")
        return {"status": "ERROR", "reason": "Too many requests"}

    zapbox = await get_zapbox(device_id)
    if not zapbox or not zapbox.auth_enabled:
        return {"status": "ERROR", "reason": "Identities disabled"}

    cached = auth_k1_cache.get(k1)
    if not cached or cached[0] != device_id or cached[3] < _now():
        return {"status": "ERROR", "reason": "Unknown or expired k1"}

    if not verify_lud04(k1, sig, key):
        return {"status": "ERROR", "reason": "Invalid signature"}

    if action == "register":
        if not _teach_open(device_id):
            return {"status": "ERROR", "reason": "No open teach session"}
        existing = await get_auth_key_by_pubkey(device_id, key)
        if existing:
            if not existing.enabled:
                existing.enabled = True
                await update_auth_key(existing)
        else:
            await create_auth_key(CreateAuthKey(zapbox_id=device_id, pubkey=key))
        auth_k1_cache.pop(k1, None)
        await websocket_updater(
            device_id, json.dumps({"event": "auth_enrolled", "pubkey": key})
        )
        logger.info(f"LNURL-auth enrolled key: device={device_id} key={key[:10]}…")
        return {"status": "OK"}

    # action == auth
    ak = await get_auth_key_by_pubkey(device_id, key)
    if not ak or not ak.enabled:
        auth_k1_cache.pop(k1, None)
        return {"status": "ERROR", "reason": "Unknown identity"}

    _device_id, pin, duration, _expiry = cached
    auth_k1_cache.pop(k1, None)
    await websocket_updater(device_id, f"{pin}-{duration}")
    logger.info(f"LNURL-auth ok: device={device_id} key={key[:10]}… pin={pin}")
    return {"status": "OK"}


# --------------------------------------------------------------------------- #
# Identities management API (instance editor)
# --------------------------------------------------------------------------- #


async def _assert_device_owner(device_id: str, user: User) -> None:
    zapbox = await get_zapbox(device_id)
    if not zapbox:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="ZapBox does not exist")
    if zapbox.wallet not in user.wallet_ids:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="You do not have permission to access this ZapBox.",
        )


@zapbox_auth_router.get("/auth/keys/{device_id}")
async def api_auth_keys_list(
    device_id: str, user: User = Depends(check_user_exists)
) -> list[AuthKey]:
    await _assert_device_owner(device_id, user)
    return await get_auth_keys(device_id)


@zapbox_auth_router.put("/auth/keys/{auth_key_id}")
async def api_auth_key_update(
    auth_key_id: str,
    data: UpdateAuthKey,
    user: User = Depends(check_user_exists),
) -> AuthKey:
    auth_key = await get_auth_key(auth_key_id)
    if not auth_key:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Identity not found.")
    await _assert_device_owner(auth_key.zapbox_id, user)
    if data.label is not None:
        auth_key.label = data.label
    if data.enabled is not None:
        auth_key.enabled = data.enabled
    return await update_auth_key(auth_key)


@zapbox_auth_router.delete("/auth/keys/{auth_key_id}")
async def api_auth_key_delete(
    auth_key_id: str,
    user: User = Depends(check_user_exists),
) -> None:
    auth_key = await get_auth_key(auth_key_id)
    if not auth_key:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Identity not found.")
    await _assert_device_owner(auth_key.zapbox_id, user)
    await delete_auth_key(auth_key_id)
