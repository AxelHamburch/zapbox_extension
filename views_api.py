import asyncio
import json
import math
import uuid
from http import HTTPStatus

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from lnbits.core.crud import get_wallet
from lnbits.core.models import User, WalletTypeInfo
from lnbits.core.services import create_invoice
from lnbits.core.services.lnurl import request_lnurl_json
from lnbits.decorators import check_user_exists, require_invoice_key
from lnbits.utils.exchange_rates import fiat_amount_as_satoshis
from loguru import logger
from lnurl import LnurlResponseException
from lnurl.types import LnurlPayMetadata
from pydantic import BaseModel

from .crud import (
    disable_signed_triggers,
    enable_signed_triggers,
    get_trigger_state,
    create_minipos_payment,
    create_switch_payment,
    create_zapbox,
    delete_zapbox,
    get_last_paid_minipos_payment,
    get_minipos_payment,
    get_zapbox,
    get_zapboxes,
    new_device_token,
    update_zapbox,
)
from .device_auth import device_token_ok, get_authenticated_zapbox
from .device_channel import close_device_channel, push_to_device, spawn
from .models import (
    CreateZapBox,
    MiniPosInvoiceRequest,
    MiniPosPayment,
    ZapBox,
    ZapBoxPublic,
)
from .pricing import switch_price_msat


class NfcLnurlwRequest(BaseModel):
    lnurlw: str


zapbox_api_router = APIRouter(prefix="/api/v1")

MAX_PIN_ATTEMPTS = 3
LNURL_TIMEOUT = 10  # seconds per LNURLW request
MAX_LNURLW_LENGTH = 512
MAX_REASON_LENGTH = 100  # remote error text shown on the device display

# Short-lived in-memory store for PIN sessions (one per active card tap, max 60s each).
# pin_sessions maps session_id → asyncio.Event set by api_nfc_pin_submit().
# pin_results  maps session_id → PIN string submitted by the device.
# pin_session_device maps session_id → device id (a PIN may only be submitted
# by the device that owns the session).
pin_sessions: dict[str, asyncio.Event] = {}
pin_results: dict[str, str] = {}
pin_session_device: dict[str, str] = {}


def _reason(data: dict, default: str) -> str:
    reason = str(data.get("reason") or default)
    return reason[:MAX_REASON_LENGTH]


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
    if switch.wallet not in user.wallet_ids:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="You do not have permission to trigger this switch.",
        )
    _switch = next((s for s in switch.switches if s.pin == pin), None)
    if not _switch:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="Switch with this pin does not exist.",
        )
    await push_to_device(switch.id, f"{pin}-{_switch.duration}")


async def _run_pin_loop(
    switch_id: str,
    device_id: str,
    session_id: str,
    callback: str,
    params: dict,
    sats: int,
) -> None:
    """Background task: wait for PIN submissions and retry the LNURLW callback.

    Runs after api_nfc_lnurlw() has already returned HTTP 200 to the device.
    Sends 'pin_error' WS events on wrong PIN; the invoice settlement (relay
    trigger) is handled by tasks.py once the callback succeeds.
    """
    try:
        for attempt in range(1, MAX_PIN_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(pin_sessions[session_id].wait(), timeout=180)
            except asyncio.TimeoutError:
                logger.warning(f"PIN session timed out: device={device_id} session={session_id}")
                await push_to_device(switch_id, json.dumps({
                    "event": "pin_error",
                    "session_id": session_id,
                    "reason": "PIN entry timed out.",
                    "attempts": MAX_PIN_ATTEMPTS,
                    "max_attempts": MAX_PIN_ATTEMPTS,
                }))
                return

            user_pin = pin_results.pop(session_id, "")
            pin_sessions[session_id].clear()

            if not user_pin:
                logger.error(f"PIN loop received empty PIN: session={session_id}")
                return

            try:
                cb_data = await request_lnurl_json(
                    callback, params={**params, "pin": user_pin}, timeout=LNURL_TIMEOUT
                )
            except LnurlResponseException as exc:
                logger.error(f"NFC: PIN callback failed: {exc}")
                await push_to_device(switch_id, json.dumps({
                    "event": "pin_error",
                    "session_id": session_id,
                    "reason": str(exc)[:MAX_REASON_LENGTH],
                    "attempts": MAX_PIN_ATTEMPTS,
                    "max_attempts": MAX_PIN_ATTEMPTS,
                }))
                return

            if cb_data.get("status") != "ERROR":
                logger.info(
                    f"NFC PIN accepted: device={device_id} sats={sats} attempt={attempt}/{MAX_PIN_ATTEMPTS}"
                )
                return  # Invoice submitted — tasks.py handles settlement

            reason = _reason(cb_data, "Invalid PIN")
            server_blocked = "blocked" in reason.lower()
            blocked = attempt >= MAX_PIN_ATTEMPTS or server_blocked
            logger.warning(f"NFC PIN error: {reason} (attempt {attempt}/{MAX_PIN_ATTEMPTS}, server_blocked={server_blocked})")
            await push_to_device(switch_id, json.dumps({
                "event": "pin_error",
                "session_id": session_id,
                "reason": reason,
                "attempts": MAX_PIN_ATTEMPTS if server_blocked else attempt,
                "max_attempts": MAX_PIN_ATTEMPTS,
            }))
            if blocked:
                return
            # Loop: device shows error 5s, user enters new PIN → next pin_submit
    finally:
        pin_sessions.pop(session_id, None)
        pin_results.pop(session_id, None)
        pin_session_device.pop(session_id, None)


@zapbox_api_router.post("/pos/invoice")
async def api_minipos_create_invoice(
    data: MiniPosInvoiceRequest,
    wallet: WalletTypeInfo = Depends(require_invoice_key),
    x_device_token: str | None = Header(None),
) -> dict:
    """Mini-PoS invoice creation.

    Called by the ZapBox device after the user entered an amount on the touch
    display and pressed "Invoice". Settlement is detected by the invoice
    listener in tasks.py which pushes the relay trigger over WebSocket.

    The invoice key may belong to any wallet of the ZapBox owner's account
    (the receiving wallet can differ from the ZapBox wallet), but not to a
    different account.
    """
    zapbox = await get_authenticated_zapbox(data.device_id, x_device_token)
    zapbox_wallet = await get_wallet(zapbox.wallet)
    if not zapbox_wallet or zapbox_wallet.user != wallet.wallet.user:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="Invoice key does not belong to the ZapBox owner.",
        )
    if data.amount <= 0:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Amount must be greater than zero.",
        )

    currency = data.currency.strip()
    if currency.lower() in ("sat", "sats"):
        sats = int(data.amount)
    else:
        sats = await fiat_amount_as_satoshis(data.amount, currency.upper())
    if sats < 1:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=f"Amount ({data.amount} {currency}) is less than 1 satoshi.",
        )

    payment = await create_invoice(
        wallet_id=wallet.wallet.id,
        amount=sats,
        memo=f"Mini-PoS {data.amount:.2f} {currency}",
        extra={
            "tag": "ZapBox",
            "minipos": True,
            "pin": data.pin,
            "zapbox_id": zapbox.id,
        },
    )
    await create_minipos_payment(
        MiniPosPayment(
            id=payment.payment_hash,
            zapbox_id=zapbox.id,
            wallet=wallet.wallet.id,
            sats=sats,
            amount=data.amount,
            currency=currency,
            bolt11=payment.bolt11,
        )
    )
    logger.info(
        f"Mini-PoS invoice created: device={zapbox.id} "
        f"amount={data.amount} {currency} sats={sats}"
    )
    return {
        "payment_hash": payment.payment_hash,
        "payment_request": payment.bolt11,
    }


@zapbox_api_router.get("/pos/invoice/last")
async def api_minipos_last_paid(
    device_id: str = Query(...),
    wallet: WalletTypeInfo = Depends(require_invoice_key),
    x_device_token: str | None = Header(None),
) -> dict:
    """Returns amount and currency of the last settled Mini-PoS payment
    for this device ("Last Pay" button on the touch display)."""
    await get_authenticated_zapbox(device_id, x_device_token)
    payment = await get_last_paid_minipos_payment(device_id, wallet.wallet.id)
    if not payment:
        return {"amount": None}
    return {"amount": payment.amount, "currency": payment.currency}


def submit_pin(session_id: str, pin: str, device_id: str) -> str | None:
    """Hand a PIN to a waiting PIN loop. Returns an error text or None.

    Shared by the HTTPS endpoint and the device channel (views_ws.py).
    """
    if session_id not in pin_sessions or pin_session_device.get(session_id) != device_id:
        return "Unknown or expired PIN session."
    if not pin.isdigit() or len(pin) != 4:
        return "PIN must be exactly 4 digits."
    pin_results[session_id] = pin
    pin_sessions[session_id].set()
    return None


@zapbox_api_router.post("/nfc/pin_submit")
async def api_nfc_pin_submit(
    session_id: str = Query(...),
    pin: str = Query(...),
    x_device_token: str | None = Header(None),
) -> dict:
    """Receives the 4-digit PIN entered by the user on the ZapBox touch display.

    Called by the device after it receives a 'pin_required' WebSocket event.
    The session_id ties this POST to the suspended api_nfc_lnurlw coroutine,
    which is waiting on pin_sessions[session_id].
    """
    device_id = pin_session_device.get(session_id)
    if not device_id or session_id not in pin_sessions:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="Unknown or expired PIN session.",
        )
    await get_authenticated_zapbox(device_id, x_device_token)
    error = submit_pin(session_id, pin, device_id)
    if error:
        status = (
            HTTPStatus.NOT_FOUND
            if error.startswith("Unknown")
            else HTTPStatus.UNPROCESSABLE_ENTITY
        )
        raise HTTPException(status_code=status, detail=error)
    return {"status": "OK"}


async def process_nfc_lnurlw(
    device_id: str,
    pin: int,
    lnurlw: str,
    minipos_hash: str | None = None,
) -> dict:
    """Core NFC Bolt Card payment logic.

    Shared by the HTTP endpoint below and the device WebSocket channel
    (views_ws.py) — the WS handler converts raised HTTPExceptions into error
    events for the device. Callers must have authenticated the device.

    1. Creates a Lightning invoice for the switch/pin amount — or, when
       minipos_hash is given, reuses the pending Mini-PoS invoice.
    2. Resolves the LNURLW withdraw request (k1 + callback URL).
    3. Submits the invoice to the LNURLW callback so the Bolt Card wallet pays.
    4. Invoice settled event is handled by tasks.py which triggers the relay.

    Both LNURLW requests go through LNbits' request_lnurl_json: it blocks
    private/internal destinations (SSRF) and runs Bolt Cards of the same
    instance (boltcards / tagid with local LNURL opt-in) in-process.
    """
    switch = await get_zapbox(device_id)
    if not switch:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Device not found.")

    # Validate the card URL before creating any invoice.
    if len(lnurlw) > MAX_LNURLW_LENGTH or not lnurlw.lower().startswith("lnurlw://"):
        raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="Invalid LNURLW format.")
    resolve_url = "https://" + lnurlw[9:]

    if minipos_hash:
        # Mini-PoS: pay the invoice the device already created via /pos/invoice
        minipos_payment = await get_minipos_payment(minipos_hash)
        if not minipos_payment or minipos_payment.zapbox_id != device_id:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND, detail="Mini-PoS invoice not found."
            )
        if minipos_payment.paid:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail="Mini-PoS invoice already paid.",
            )
        if not minipos_payment.bolt11:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail="Mini-PoS invoice has no bolt11.",
            )
        sats = minipos_payment.sats
        price_msat = sats * 1000
        bolt11 = minipos_payment.bolt11
        payment_hash = minipos_hash
    else:
        _switch = next((s for s in switch.switches if s.pin == pin), None)
        if not _switch:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=f"Pin {pin} not found.")
        price_msat = await switch_price_msat(switch, _switch)
        sats = math.ceil(price_msat / 1000)
        if sats < 1:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=f"Configured amount ({_switch.amount} {switch.currency}) is less than 1 satoshi.",
            )
        bolt11 = ""
        payment_hash = ""

    # Step 2: Resolve LNURLW → k1 + callback
    try:
        lnurl_data = await request_lnurl_json(resolve_url, timeout=LNURL_TIMEOUT)
    except LnurlResponseException as exc:
        logger.error(f"NFC: LNURLW resolve failed: {exc}")
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=f"LNURLW resolve failed: {str(exc)[:MAX_REASON_LENGTH]}",
        ) from exc

    if lnurl_data.get("status") == "ERROR":
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=f"LNURLW error: {_reason(lnurl_data, 'unknown')}",
        )

    k1 = lnurl_data.get("k1")
    callback = lnurl_data.get("callback")
    if not k1 or not callback or not isinstance(callback, str):
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail="Missing k1 or callback in LNURLW response.",
        )

    # Bolt Cards (boltcards >= 1.2, tagid >= 2.2) advertise the remaining
    # budget as maxWithdrawable. Fail early with a clear message instead of a
    # callback error after the invoice exists.
    try:
        max_withdrawable = int(lnurl_data.get("maxWithdrawable", price_msat))
    except (TypeError, ValueError):
        max_withdrawable = price_msat
    if price_msat > max_withdrawable:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=f"Card limit reached: max {max_withdrawable // 1000} sat",
        )

    if not minipos_hash:
        # Step 1 (after the card answered): invoice for the switch amount
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
        bolt11 = payment.bolt11
        payment_hash = payment.payment_hash

    # Step 3: Submit invoice to LNURLW callback (with optional PIN)
    params = {"k1": str(k1), "pr": bolt11}

    pin_limit_msat = lnurl_data.get("pinLimit")
    if pin_limit_msat is not None and price_msat >= int(pin_limit_msat):
        # PIN protection active — send pin_required to device, return 200 immediately.
        # The PIN retry loop runs as a background task so the device's HTTP client
        # (15s timeout) is not blocked for the full PIN entry duration (up to 3×180s).
        session_id = str(uuid.uuid4())
        pin_sessions[session_id] = asyncio.Event()
        pin_session_device[session_id] = switch.id
        await push_to_device(switch.id, json.dumps({
            "event": "pin_required",
            "amount_sat": sats,
            "session_id": session_id,
            "max_attempts": MAX_PIN_ATTEMPTS,
        }))
        spawn(_run_pin_loop(
            switch_id=switch.id,
            device_id=device_id,
            session_id=session_id,
            callback=callback,
            params=params,
            sats=sats,
        ))
        return {"status": "OK", "payment_hash": payment_hash}

    # No PIN required — submit callback directly
    try:
        cb_data = await request_lnurl_json(callback, params=params, timeout=LNURL_TIMEOUT)
    except LnurlResponseException as exc:
        logger.error(f"NFC: LNURLW callback failed: {exc}")
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=f"LNURLW callback failed: {str(exc)[:MAX_REASON_LENGTH]}",
        ) from exc

    if cb_data.get("status") == "ERROR":
        reason = _reason(cb_data, "unknown")
        logger.error(f"NFC: LNURLW callback error: {reason}")
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=f"LNURLW callback error: {reason}",
        )

    logger.info(
        f"NFC Bolt Card payment initiated: device={device_id} pin={pin} sats={sats}"
        + (" (Mini-PoS)" if minipos_hash else "")
    )
    return {"status": "OK", "payment_hash": payment_hash}


@zapbox_api_router.post("/nfc/{device_id}")
async def api_nfc_lnurlw(
    device_id: str,
    pin: int = Query(...),
    minipos_hash: str | None = Query(None),
    data: NfcLnurlwRequest = ...,
    x_device_token: str | None = Header(None),
) -> dict:
    """NFC Bolt Card payment endpoint (HTTPS).

    Called by the ZapBox device when it reads a Bolt Card (NTAG424 LNURLW).
    Newer firmware prefers the persistent device WebSocket channel
    (views_ws.py) and uses this endpoint as fallback — some consumer routers
    fail NEW TLS connections in phases while established ones keep working,
    so a fresh HTTPS connection per tap is the least reliable transport.
    """
    await get_authenticated_zapbox(device_id, x_device_token)
    return await process_nfc_lnurlw(device_id, pin, data.lnurlw, minipos_hash)


@zapbox_api_router.put("/{zapbox_id}/device_token")
async def api_zapbox_regenerate_token(
    zapbox_id: str,
    user: User = Depends(check_user_exists),
) -> ZapBox:
    """Create a new device token. The device must get the new device string,
    the old token stops working immediately."""
    zapbox = await get_zapbox(zapbox_id)
    if not zapbox:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="ZapBox does not exist"
        )
    if zapbox.wallet not in user.wallet_ids:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="You do not have permission to update this ZapBox.",
        )
    zapbox.device_token = new_device_token()
    zapbox = await update_zapbox(zapbox)
    # The device has not proven the new token yet: until it does, nothing can
    # be sold to it (see device_can_deliver in views_lnurl.py).
    await disable_signed_triggers(zapbox.id)
    # The old token must stop working now, not at the next reconnect: close the
    # channel that was opened with it.
    await close_device_channel(zapbox.id)
    return zapbox


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
    # Both the current owner and the new wallet must belong to the caller,
    # otherwise anyone could take over a ZapBox by its (public) id.
    if zapbox.wallet not in user.wallet_ids or data.wallet not in user.wallet_ids:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="You do not have permission to update this ZapBox.",
        )

    for k, v in data.model_dump().items():
        if v is not None:
            setattr(zapbox, k, v)

    zapbox.switches = data.switches
    return await update_zapbox(zapbox)


@zapbox_api_router.get(
    "/public/{zapbox_id}", response_model=ZapBoxPublic
)
async def api_zapbox_get_public(
    zapbox_id: str, x_device_token: str | None = Header(None)
) -> ZapBoxPublic:
    """Public switch config (labels, durations). Also fetched by the device at
    boot: with a valid X-Device-Token the device proves it supports signed
    core-WebSocket triggers (Z6), and gets the current trigger counter."""
    zapbox = await get_zapbox(zapbox_id)
    if not zapbox:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="ZapBox does not exist."
        )
    if x_device_token and device_token_ok(zapbox, x_device_token):
        await enable_signed_triggers(zapbox.id)
    counter, _ = await get_trigger_state(zapbox.id)
    return ZapBoxPublic(
        title=zapbox.title, switches=zapbox.switches, trigger_counter=counter
    )


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
