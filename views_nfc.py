"""NFC identity (Ring-Login) endpoints for the ZapBox.

NTAG 424 DNA chips (Bolt Card / Bolt Ring) identify themselves via the SUN
mechanism (AES-CMAC). Verification is delegated server-to-server to the
tagid_extension, which holds the card keys and increments the replay-protection
counter. A successful, known card triggers the relay exactly like LNURL-auth.

Active mode:
  GET /api/v1/nfc/auth/{zapbox_id}?external_id=&p=&c=[&pin=]

Teach mode (requires open teach session — same session as LNURL-auth):
  GET /api/v1/nfc/teach/{zapbox_id}?external_id=&p=&c=

Both are device calls and need the X-Device-Token header (v2.7.0).
"""

import json
import re
from http import HTTPStatus
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from lnbits.core.db import core_app_extra
from lnbits.core.models import User
from lnbits.decorators import check_user_exists
from lnbits.settings import settings
from lnbits.utils.safe_http import SafeHttpClient
from loguru import logger

from .crud import (
    delete_nfc_identity,
    get_nfc_identities,
    get_nfc_identity,
    get_nfc_identity_by_card_id,
    update_nfc_identity,
    upsert_nfc_identity,
)
from .device_auth import get_authenticated_zapbox
from .device_channel import push_to_device
from .models import CreateNfcIdentity, NfcIdentity, UpdateNfcIdentity, ZapBox
from .views_auth import _assert_device_owner, _teach_open

zapbox_nfc_router = APIRouter(prefix="/api/v1")

DEFAULT_AUTH_DURATION = 3000
MAX_AUTH_DURATION = 600_000  # ms (10 min)
_TAGID_TIMEOUT = 10  # seconds for server-to-server HTTP call
# Fixed path of the tagid verify endpoint on this instance (local dispatch).
_TAGID_LOCAL_PATH = "/tagid/api/v1/scan/verify/"
_MAX_DETAIL = 100

_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


# --------------------------------------------------------------------------- #
# Internal helper
# --------------------------------------------------------------------------- #


def _validate_tap(external_id: str, p: str, c: str) -> tuple[str, str, str]:
    """external_id goes into a URL path, p/c into the query: only accept the
    shapes an NTAG 424 SUN message can have."""
    if (
        not _EXTERNAL_ID_RE.match(external_id)
        or len(p) != 32
        or len(c) != 16
        or not _HEX_RE.match(p)
        or not _HEX_RE.match(c)
    ):
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="Invalid NFC tap data.")
    return external_id.lower(), p.upper(), c.upper()


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, (parts.hostname or "").lower(), port


def _is_local_tagid(tagid_base_url: str) -> bool:
    if not settings.lnbits_baseurl or core_app_extra.local_lnurl_app is None:
        return False
    return _origin(tagid_base_url) == _origin(settings.lnbits_baseurl)


async def _tagid_request(
    tagid_base_url: str, external_id: str, params: dict, api_key: str
) -> httpx.Response:
    headers = {"X-Api-Key": api_key}
    if _is_local_tagid(tagid_base_url):
        # TagID on this instance: call the LNbits app in-process on a fixed
        # path. Only external_id (validated) is caller-controlled; the API key
        # never leaves the process, no TLS round trip through the proxy.
        base = settings.lnbits_baseurl.rstrip("/")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=core_app_extra.local_lnurl_app),
            base_url=base,
            follow_redirects=False,
            timeout=_TAGID_TIMEOUT,
            trust_env=False,
        ) as client:
            return await client.get(
                f"{_TAGID_LOCAL_PATH}{external_id}", params=params, headers=headers
            )
    # TagID on another server: SSRF-protected client (public destinations
    # only, no redirects).
    url = f"{tagid_base_url.rstrip('/')}/api/v1/scan/verify/{external_id}"
    async with SafeHttpClient(timeout=_TAGID_TIMEOUT, follow_redirects=False) as client:
        return await client.get(url, params=params, headers=headers)


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
    params: dict = {"p": p, "c": c}
    if pin:
        params["pin"] = pin
    try:
        resp = await _tagid_request(tagid_base_url, external_id, params, tagid_api_key)
    except (httpx.RequestError, ValueError) as exc:
        # ValueError covers UnsafeHttpDestinationError (private/internal target)
        logger.error(f"Ring-Login: tagid unreachable or not allowed: {exc}")
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail="tagid_extension unreachable.",
        ) from exc
    if resp.status_code == HTTPStatus.CONFLICT:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail="Replay detected.")
    if resp.status_code == HTTPStatus.FORBIDDEN:
        try:
            detail = str(resp.json().get("detail", ""))[:_MAX_DETAIL]
        except Exception:
            detail = ""
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN, detail=detail or "Access denied by tagid."
        )
    if resp.status_code != HTTPStatus.OK:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=f"tagid returned {resp.status_code}.",
        )
    try:
        data = resp.json()
    except Exception as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY, detail="tagid returned invalid data."
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY, detail="tagid returned invalid data."
        )
    return data


def _require_tagid(zapbox: ZapBox) -> None:
    if not zapbox.tagid_base_url or not zapbox.tagid_api_key:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="tagid not configured on this device.",
        )


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
    auth_pin: int = Query(..., ge=0, le=99),  # relay GPIO supplied by the device
    auth_duration: int = Query(DEFAULT_AUTH_DURATION, ge=1, le=MAX_AUTH_DURATION),
    x_device_token: str | None = Header(None),
) -> dict:
    """Active identification: verify NTAG 424 tap and trigger relay if card is known.

    The device reads the LNURLW URL from the card (contains external_id, p, c),
    optionally collects a PIN on the touch display, then calls this endpoint.
    The zapbox_extension forwards to tagid for cryptographic verification and —
    on success — pushes the relay trigger via WebSocket.
    """
    zapbox = await get_authenticated_zapbox(zapbox_id, x_device_token)
    if not zapbox.auth_enabled:
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail="Identities disabled.")
    _require_tagid(zapbox)
    external_id, p, c = _validate_tap(external_id, p, c)
    if zapbox.ring_login_pin_required and not pin:
        return {"status": "ERROR", "reason": "PIN required"}

    verify = await _tagid_verify(
        zapbox.tagid_base_url or "",
        zapbox.tagid_api_key or "",
        external_id,
        p,
        c,
        pin,
    )

    card_id = verify.get("card_id")
    if not card_id:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY, detail="tagid returned no card_id."
        )
    if zapbox.ring_login_pin_required and verify.get("pin_verified") is not True:
        # Server-side PIN enforcement: tagid must have checked the card PIN.
        # A card without PIN cannot pass a ZapBox that requires one.
        logger.info(f"Ring-Login: PIN not verified for card {card_id} on {zapbox_id}")
        return {"status": "ERROR", "reason": "PIN required"}

    nfc = await get_nfc_identity_by_card_id(zapbox_id, str(card_id))
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
    x_device_token: str | None = Header(None),
) -> dict:
    """Teach: enrol an NTAG 424 card as a known identity for this device.

    Requires an open teach session (started by POST /auth/teach/start with the
    6-digit teach PIN) and the device token: only the device itself can enrol
    a card. The card must already be registered in the tagid_extension.
    """
    zapbox = await get_authenticated_zapbox(zapbox_id, x_device_token)
    if not _teach_open(zapbox_id):
        return {"status": "ERROR", "reason": "No open teach session"}
    if not zapbox.auth_enabled:
        return {"status": "ERROR", "reason": "Identities disabled"}
    if not zapbox.tagid_base_url or not zapbox.tagid_api_key:
        return {"status": "ERROR", "reason": "tagid not configured"}
    external_id, p, c = _validate_tap(external_id, p, c)

    verify = await _tagid_verify(
        zapbox.tagid_base_url,
        zapbox.tagid_api_key,
        external_id,
        p,
        c,
    )

    card_id = verify.get("card_id")
    if not card_id:
        return {"status": "ERROR", "reason": "tagid returned no card_id"}

    nfc = await upsert_nfc_identity(
        CreateNfcIdentity(zapbox_id=zapbox_id, card_id=str(card_id))
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
