import json
import math
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

    # Step 3: Submit invoice to LNURLW callback
    sep = "&" if "?" in callback else "?"
    cb_url = f"{callback}{sep}k1={k1}&pr={payment.bolt11}"
    async with httpx.AsyncClient() as client:
        try:
            cb_resp = await client.get(cb_url, timeout=10)
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
