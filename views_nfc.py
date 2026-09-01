"""NFC identity (Ring-Login) endpoints for the ZapBox.

NTAG 424 DNA chips (Bolt Card / Bolt Ring) identify themselves via the SUN
mechanism (AES-CMAC). Verification is delegated server-to-server to the
tagid_extension, which holds the card keys and increments the replay-protection
counter. A successful, known card triggers the relay exactly like LNURL-auth.

Active mode:
  GET /api/v1/nfc/auth/{zapbox_id}?external_id=&p=&c=[&pin=]

Teach mode (requires open teach session — same session as LNURL-auth):
  GET /api/v1/nfc/teach/{zapbox_id}?external_id=&p=&c=
"""

import json
from http import HTTPStatus

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from lnbits.core.models import User
from .device_channel import push_to_device
from lnbits.decorators import check_user_exists
from loguru import logger

from .crud import (
    delete_nfc_identity,
    get_nfc_identities,
    get_nfc_identity,
    get_nfc_identity_by_card_id,
    get_zapbox,
    update_nfc_identity,
    upsert_nfc_identity,
)
from .models import CreateNfcIdentity, NfcIdentity, UpdateNfcIdentity
from .views_auth import _assert_device_owner, _teach_open

zapbox_nfc_router = APIRouter(prefix="/api/v1")

DEFAULT_AUTH_DURATION = 3000
_TAGID_TIMEOUT = 10  # seconds for server-to-server HTTP call


# --------------------------------------------------------------------------- #
# Internal helper
# --------------------------------------------------------------------------- #


async def _tagid_verify(
    tagid_base_url: str,
    tagid_api_key: str,
    external_id: str,
    p: str,
    c: str,
    pin: str | None = None,
) -> dict:
    """Call the tagid verify-only endpoint and return its JSON response.
    Raises HTTPException on network error or non-2xx response."""
    url = f"{tagid_base_url.rstrip('/')}/api/v1/scan/verify/{external_id}"
    params: dict = {"p": p, "c": c}
    if pin:
        params["pin"] = pin
    try:
        async with httpx.AsyncClient(timeout=_TAGID_TIMEOUT) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"X-Api-Key": tagid_api_key},
            )
    except httpx.RequestError as exc:
        logger.error(f"Ring-Login: tagid unreachable: {exc}")
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail="tagid_extension unreachable.",
        )
    if resp.status_code == HTTPStatus.CONFLICT:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail="Replay detected.")
    if resp.status_code == HTTPStatus.FORBIDDEN:
        detail = resp.json().get("detail", "Access denied by tagid.")
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=detail)
    if resp.status_code != HTTPStatus.OK:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=f"tagid returned {resp.status_code}.",
        )
    return resp.json()


# --------------------------------------------------------------------------- #
# Active mode — device calls this on every NFC tap
# --------------------------------------------------------------------------- #


@zapbox_nfc_router.get("/nfc/auth/{zapbox_id}")
async def api_nfc_auth(
    zapbox_id: str,
    external_id: str = Query(...),
    p: str = Query(...),
    c: str = Query(...),
    pin: str | None = Query(None),
    auth_pin: int = Query(...),  # relay GPIO supplied by the device (its primary channel)
    auth_duration: int = Query(DEFAULT_AUTH_DURATION),
) -> dict:
    """Active identification: verify NTAG 424 tap and trigger relay if card is known.

    The device reads the LNURLW URL from the card (contains external_id, p, c),
    optionally collects a PIN on the touch display, then calls this endpoint.
    The zapbox_extension forwards to tagid for cryptographic verification and —
    on success — pushes the relay trigger via WebSocket.
    """
    zapbox = await get_zapbox(zapbox_id)
    if not zapbox:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Device not found.")
    if not zapbox.auth_enabled:
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail="Identities disabled.")
    if not zapbox.tagid_base_url or not zapbox.tagid_api_key:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="tagid not configured on this device.",
        )

    verify = await _tagid_verify(
        zapbox.tagid_base_url,
        zapbox.tagid_api_key,
        external_id,
        p.upper(),
        c.upper(),
        pin,
    )

    card_id = verify.get("card_id")
    if not card_id:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY, detail="tagid returned no card_id."
        )

    nfc = await get_nfc_identity_by_card_id(zapbox_id, card_id)
    if not nfc or not nfc.enabled:
        logger.info(f"Ring-Login: unknown/disabled card {card_id} on device {zapbox_id}")
        return {"status": "ERROR", "reason": "Unknown identity"}

    await push_to_device(zapbox_id, f"{auth_pin}-{auth_duration}")
    logger.info(
        f"Ring-Login: NFC auth ok: device={zapbox_id} card={card_id} pin={auth_pin}"
    )
    return {"status": "OK"}


# --------------------------------------------------------------------------- #
# Teach mode — device calls this after reading an NFC tap during teach session
# --------------------------------------------------------------------------- #


@zapbox_nfc_router.get("/nfc/teach/{zapbox_id}")
async def api_nfc_teach(
    zapbox_id: str,
    external_id: str = Query(...),
    p: str = Query(...),
    c: str = Query(...),
) -> dict:
    """Teach: enrol an NTAG 424 card as a known identity for this device.

    Requires an open teach session (started by POST /auth/teach/start with the
    6-digit teach PIN). The card must already be registered in the tagid_extension.
    No PIN is checked here — the teach session itself is the security gate.
    """
    if not _teach_open(zapbox_id):
        return {"status": "ERROR", "reason": "No open teach session"}

    zapbox = await get_zapbox(zapbox_id)
    if not zapbox:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Device not found.")
    if not zapbox.auth_enabled:
        return {"status": "ERROR", "reason": "Identities disabled"}
    if not zapbox.tagid_base_url or not zapbox.tagid_api_key:
        return {"status": "ERROR", "reason": "tagid not configured"}

    verify = await _tagid_verify(
        zapbox.tagid_base_url,
        zapbox.tagid_api_key,
        external_id,
        p.upper(),
        c.upper(),
    )

    card_id = verify.get("card_id")
    if not card_id:
        return {"status": "ERROR", "reason": "tagid returned no card_id"}

    nfc = await upsert_nfc_identity(
        CreateNfcIdentity(zapbox_id=zapbox_id, card_id=card_id)
    )
    await push_to_device(
        zapbox_id, json.dumps({"event": "nfc_enrolled", "card_id": card_id})
    )
    logger.info(f"Ring-Login: NFC enrolled: device={zapbox_id} card={card_id}")
    return {"status": "OK", "card_id": card_id, "nfc_id": nfc.id}


# --------------------------------------------------------------------------- #
# Management API (instance editor)
# --------------------------------------------------------------------------- #


@zapbox_nfc_router.get("/nfc/identities/{zapbox_id}")
async def api_nfc_identities_list(
    zapbox_id: str, user: User = Depends(check_user_exists)
) -> list[NfcIdentity]:
    await _assert_device_owner(zapbox_id, user)
    return await get_nfc_identities(zapbox_id)


@zapbox_nfc_router.put("/nfc/identities/{nfc_id}")
async def api_nfc_identity_update(
    nfc_id: str,
    data: UpdateNfcIdentity,
    user: User = Depends(check_user_exists),
) -> NfcIdentity:
    nfc = await get_nfc_identity(nfc_id)
    if not nfc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="NFC identity not found.")
    await _assert_device_owner(nfc.zapbox_id, user)
    if data.label is not None:
        nfc.label = data.label
    if data.enabled is not None:
        nfc.enabled = data.enabled
    return await update_nfc_identity(nfc)


@zapbox_nfc_router.delete("/nfc/identities/{nfc_id}")
async def api_nfc_identity_delete(
    nfc_id: str, user: User = Depends(check_user_exists)
) -> None:
    nfc = await get_nfc_identity(nfc_id)
    if not nfc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="NFC identity not found.")
    await _assert_device_owner(nfc.zapbox_id, user)
    await delete_nfc_identity(nfc_id)
