import asyncio
import json
import math
import uuid
from http import HTTPStatus

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from lnbits.core.models import User
from lnbits.core.services import create_invoice, websocket_updater
from lnbits.decorators import check_user_exists
from lnbits.utils.exchange_rates import fiat_amount_as_satoshis
from loguru import logger
from lnurl.types import LnurlPayMetadata
from pydantic import BaseModel

from .crud import (
    create_zapbox,
    create_switch_payment,
    delete_zapbox,
    get_zapbox,
    get_zapboxes,
    update_zapbox,
)
from .models import ZapBox, ZapBoxPublic, CreateZapBox


class NfcLnurlwRequest(BaseModel):
    lnurlw: str

zapbox_api_router = APIRouter(prefix="/api/v1")

MAX_PIN_ATTEMPTS = 3

# Short-lived in-memory store for PIN sessions (one per active card tap, max 60s each).
# pin_sessions maps session_id → asyncio.Event set by api_nfc_pin_submit().
# pin_results  maps session_id → PIN string submitted by the device.
pin_sessions: dict[str, asyncio.Event] = {}
pin_results:  dict[str, str] = {}


@zapbox_api_router.post("")
async def api_zapbox_create(
    data: CreateZapBox, user: User = Depends(check_user_exists)
) -> ZapBox:
    if data.wallet not in user.wallet_ids:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail=(
                "You do not have permission to create a ZapBox for this wallet."
            ),
        )
    return await create_zapbox(data)


@zapbox_api_router.put("/trigger/{switch_id}/{pin}")
async def api_zapbox_trigger(
    switch_id: str,
    pin: int,
    user: User = Depends(check_user_exists),
) -> None:
    switch = await get_zapbox(switch_id)
    if not switch:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="ZapBox does not exist."
        )
    _switch = next((s for s in switch.switches if s.pin == pin), None)
    if not _switch:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="Switch with this pin does not exist.",
        )
    if switch.wallet not in user.wallet_ids:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="You do not have permission to trigger this switch.",
        )
    await websocket_updater(switch.id, f"{pin}-{_switch.duration}")


@zapbox_api_router.post("/nfc/pin_submit")
async def api_nfc_pin_submit(
    session_id: str = Query(...),
    pin: str = Query(...),
) -> dict:
    """Receives the 4-digit PIN entered by the user on the ZapBox touch display.

    Called by the device after it receives a 'pin_required' WebSocket event.
    The session_id ties this POST to the suspended api_nfc_lnurlw coroutine,
    which is waiting on pin_sessions[session_id].
    """
    if session_id not in pin_sessions:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="Unknown or expired PIN session.",
        )
    if not pin.isdigit() or len(pin) != 4:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="PIN must be exactly 4 digits.",
        )
    pin_results[session_id] = pin
    pin_sessions[session_id].set()
    return {"status": "OK"}


@zapbox_api_router.post("/nfc/{device_id}")
async def api_nfc_lnurlw(
    device_id: str,
    pin: int = Query(...),
    data: NfcLnurlwRequest = ...,
) -> dict:
    """NFC Bolt Card payment endpoint.

    Called by the ZapBox device when it reads a Bolt Card (NTAG424 LNURLW).
    1. Creates a Lightning invoice for the switch/pin amount.
    2. Resolves the LNURLW withdraw request (k1 + callback URL).
    3. Submits the invoice to the LNURLW callback so the Bolt Card wallet pays.
    4. Invoice settled event is handled by tasks.py which triggers the relay.
    """
    switch = await get_zapbox(device_id)
    if not switch:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Device not found.")

    _switch = next((s for s in switch.switches if s.pin == pin), None)
    if not _switch:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=f"Pin {pin} not found.")

    # Step 1: Create Lightning invoice for the switch amount
    price_msat = int(
        (
            await fiat_amount_as_satoshis(float(_switch.amount), switch.currency)
            if switch.currency != "sat"
            else float(_switch.amount)
        )
        * 1000
    )
    sats = math.ceil(price_msat / 1000)
    if sats < 1:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=f"Configured amount ({_switch.amount} {switch.currency}) is less than 1 satoshi.",
        )

    metadata = LnurlPayMetadata(json.dumps([["text/plain", switch.title]]))
    payment = await create_invoice(
        wallet_id=switch.wallet,
        amount=sats,
        memo=f"{switch.title} (NFC pin: {pin})",
        unhashed_description=metadata.encode(),
        extra={"tag": "ZapBox", "pin": pin, "comment": None, "zapbox_id": switch.id},
    )
    await create_switch_payment(
        payment_hash=payment.payment_hash,
        switch_id=switch.id,
        pin=pin,
        amount_msat=price_msat,
    )

    # Step 2: Resolve LNURLW → k1 + callback
    lnurlw = data.lnurlw
    if not lnurlw.lower().startswith("lnurlw://"):
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="Invalid LNURLW format.")

    resolve_url = "https://" + lnurlw[9:]
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(resolve_url, timeout=10)
            lnurl_data = resp.json()
        except Exception as exc:
            logger.error(f"NFC: LNURLW resolve failed: {exc}")
            raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail="LNURLW resolve failed.")

    if lnurl_data.get("status") == "ERROR":
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=f"LNURLW error: {lnurl_data.get('reason')}",
        )

    k1 = lnurl_data.get("k1")
    callback = lnurl_data.get("callback")
    if not k1 or not callback:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail="Missing k1 or callback in LNURLW response.",
        )

    # Step 3: Submit invoice to LNURLW callback (with optional PIN)
    sep = "&" if "?" in callback else "?"
    cb_base = f"{callback}{sep}k1={k1}&pr={payment.bolt11}"

    pin_limit_msat = lnurl_data.get("pinLimit")
    if pin_limit_msat is not None and price_msat >= pin_limit_msat:
        # PIN protection active — ask the device for a PIN, then retry the callback
        session_id = str(uuid.uuid4())
        pin_sessions[session_id] = asyncio.Event()
        try:
            await websocket_updater(switch.id, json.dumps({
                "event": "pin_required",
                "amount_sat": sats,
                "session_id": session_id,
                "max_attempts": MAX_PIN_ATTEMPTS,
            }))
            for attempt in range(1, MAX_PIN_ATTEMPTS + 1):
                try:
                    await asyncio.wait_for(pin_sessions[session_id].wait(), timeout=60)
                except asyncio.TimeoutError:
                    raise HTTPException(
                        status_code=HTTPStatus.REQUEST_TIMEOUT,
                        detail="PIN entry timed out.",
                    )
                user_pin = pin_results.pop(session_id, "")
                pin_sessions[session_id].clear()
                if not user_pin:
                    raise HTTPException(
                        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                        detail="Empty PIN received.",
                    )

                try:
                    async with httpx.AsyncClient() as client:
                        cb_resp = await client.get(f"{cb_base}&pin={user_pin}", timeout=10)
                        cb_data = cb_resp.json()
                except Exception as exc:
                    logger.error(f"NFC: PIN callback failed: {exc}")
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_GATEWAY, detail="PIN callback failed."
                    )

                if cb_data.get("status") != "ERROR":
                    logger.info(
                        f"NFC PIN accepted: device={device_id} sats={sats} attempt={attempt}/{MAX_PIN_ATTEMPTS}"
                    )
                    return {"status": "OK", "payment_hash": payment.payment_hash}

                reason = cb_data.get("reason", "Invalid PIN")
                blocked = attempt >= MAX_PIN_ATTEMPTS
                logger.warning(f"NFC PIN error: {reason} (attempt {attempt}/{MAX_PIN_ATTEMPTS})")
                await websocket_updater(switch.id, json.dumps({
                    "event": "pin_error",
                    "reason": reason,
                    "attempts": attempt,
                    "max_attempts": MAX_PIN_ATTEMPTS,
                }))
                if blocked:
                    raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=reason)
                # Loop: device shows error 5s, then user enters new PIN → next pin_submit
        finally:
            pin_sessions.pop(session_id, None)
            pin_results.pop(session_id, None)

    # No PIN required — submit callback directly
    try:
        async with httpx.AsyncClient() as client:
            cb_resp = await client.get(cb_base, timeout=10)
            cb_data = cb_resp.json()
    except Exception as exc:
        logger.error(f"NFC: LNURLW callback failed: {exc}")
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail="LNURLW callback failed.")

    if cb_data.get("status") == "ERROR":
        logger.error(f"NFC: LNURLW callback error: {cb_data.get('reason')}")
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=f"LNURLW callback error: {cb_data.get('reason')}",
        )

    logger.info(f"NFC Bolt Card payment initiated: device={device_id} pin={pin} sats={sats}")
    return {"status": "OK", "payment_hash": payment.payment_hash}


@zapbox_api_router.put("/{zapbox_id}")
async def api_zapbox_update(
    data: CreateZapBox,
    zapbox_id: str,
    user: User = Depends(check_user_exists),
) -> ZapBox:
    zapbox = await get_zapbox(zapbox_id)
    if not zapbox:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="ZapBox does not exist"
        )
    if data.wallet not in user.wallet_ids:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="You do not have permission to update this ZapBox.",
        )

    for k, v in data.dict().items():
        if v is not None:
            setattr(zapbox, k, v)

    zapbox.switches = data.switches
    return await update_zapbox(zapbox)


@zapbox_api_router.get(
    "/public/{zapbox_id}", response_model=ZapBoxPublic
)
async def api_zapbox_get_public(zapbox_id: str) -> ZapBox:
    zapbox = await get_zapbox(zapbox_id)
    if not zapbox:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="ZapBox does not exist."
        )
    return zapbox


@zapbox_api_router.get("")
async def api_zapboxes_retrieve(
    user: User = Depends(check_user_exists),
) -> list[ZapBox]:
    return await get_zapboxes(user.wallet_ids)


@zapbox_api_router.get("/{zapbox_id}")
async def api_zapbox_retrieve(
    zapbox_id: str, user: User = Depends(check_user_exists)
) -> ZapBox:
    zapbox = await get_zapbox(zapbox_id)
    if not zapbox:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="ZapBox does not exist"
        )
    if zapbox.wallet not in user.wallet_ids:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="You do not have permission to access this ZapBox.",
        )
    return zapbox


@zapbox_api_router.delete("/{zapbox_id}")
async def api_zapbox_delete(
    zapbox_id: str,
    user: User = Depends(check_user_exists),
) -> None:
    zapbox = await get_zapbox(zapbox_id)
    if not zapbox:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="ZapBox does not exist."
        )
    if zapbox.wallet not in user.wallet_ids:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="You do not have permission to delete this ZapBox.",
        )
    await delete_zapbox(zapbox_id)
