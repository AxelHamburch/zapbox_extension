import json
import math

from fastapi import APIRouter, Query, Request
from lnbits.core.services import create_invoice, websocket_manager
from lnurl import (
    CallbackUrl,
    InvalidLnurl,
    LightningInvoice,
    LnurlErrorResponse,
    LnurlPayActionResponse,
    LnurlPayMetadata,
    LnurlPayResponse,
    Max144Str,
    MessageAction,
    MilliSatoshi,
)
from pydantic import TypeAdapter, ValidationError

from .crud import create_switch_payment, get_trigger_state, get_zapbox
from .device_channel import has_device_channel
from .pricing import amount_accepted, max_sendable_msat, switch_price_msat

zapbox_lnurl_router = APIRouter(prefix="/api/v1/lnurl")


async def device_can_deliver(zapbox_id: str) -> bool:
    """True if a paid trigger will reach a device that accepts it.

    - Open device channel: the device proved its token when it connected.
    - Core WebSocket only (fallback while the channel is down, or single
      connection mode): only if the device proved the *current* token. The
      trigger there is signed with it, and a device with another token (after
      "New token", before the new device string is flashed) silently drops it:
      the payer would pay and get nothing. Devices that never proved a token
      cannot be told apart from stale ones and are not sold to either.
    """
    if has_device_channel(zapbox_id):
        return True
    if not websocket_manager.has_connection(zapbox_id):
        return False
    _, proved_token = await get_trigger_state(zapbox_id)
    return proved_token

# Both routes are called by Lightning wallets. The opt-in lets wallets on the
# same LNbits instance pay a ZapBox (LNbits >= 1.7.0 blocks self-requests
# otherwise and dispatches opted-in GET routes in-process).
LOCAL_LNURL = {"x-lnbits-local-lnurl": True}


def _parse_pin(pin: str) -> int | None:
    try:
        return int(pin)
    except (TypeError, ValueError):
        return None


@zapbox_lnurl_router.get("/{zapbox_id}", openapi_extra=LOCAL_LNURL)
async def lnurl_params(
    request: Request, zapbox_id: str, pin: str
) -> LnurlPayResponse | LnurlErrorResponse:
    switch = await get_zapbox(zapbox_id)
    if not switch:
        return LnurlErrorResponse(
            reason=f"ZapBox {zapbox_id} not found on this server"
        )
    if switch.disabled:
        return LnurlErrorResponse(
            reason=f"ZapBox {zapbox_id} is disabled"
        )

    pin_int = _parse_pin(pin)
    _switch = next((_s for _s in switch.switches if _s.pin == pin_int), None)
    if not _switch:
        return LnurlErrorResponse(reason=f"Switch with pin {pin} not found.")

    price_msat = await switch_price_msat(switch, _switch)
    if price_msat < 1000:
        return LnurlErrorResponse(
            reason=(
                f"Configured amount ({_switch.amount} {switch.currency}) is less than "
                f"1 satoshi — please use whole numbers for satoshis or fiat currency."
            )
        )
    url = request.url_for("zapbox.lnurl_cb", switch_id=zapbox_id, pin=pin_int)
    try:
        callback_url = TypeAdapter(CallbackUrl).validate_python(str(url))
    except (InvalidLnurl, ValidationError):
        return LnurlErrorResponse(reason=f"Invalid LNURL callback URL: {url!s}")
    res = LnurlPayResponse(
        callback=callback_url,
        minSendable=MilliSatoshi(price_msat),
        maxSendable=MilliSatoshi(max_sendable_msat(price_msat, _switch)),
        metadata=LnurlPayMetadata(json.dumps([["text/plain", switch.title]])),
    )
    if _switch.comment is True:
        res.commentAllowed = 255
    return res


@zapbox_lnurl_router.get(
    "/cb/{switch_id}/{pin}", name="zapbox.lnurl_cb", openapi_extra=LOCAL_LNURL
)
async def lnurl_callback(
    switch_id: str,
    pin: int,
    amount: int | None = Query(None),
    comment: str | None = Query(None),
) -> LnurlPayActionResponse | LnurlErrorResponse:
    if comment and len(comment) > 255:
        return LnurlErrorResponse(reason="Comment too long, max 255 characters.")
    if not amount:
        return LnurlErrorResponse(reason="No amount specified.")

    switch = await get_zapbox(switch_id)
    if not switch:
        return LnurlErrorResponse(reason="Switch not found.")
    if switch.disabled:
        return LnurlErrorResponse(reason=f"ZapBox {switch_id} is disabled")
    _switch = next((_s for _s in switch.switches if _s.pin == int(pin)), None)
    if not _switch:
        return LnurlErrorResponse(reason=f"Switch with pin {pin} not found.")

    # The price advertised as minSendable/maxSendable is only a hint for the
    # wallet. Enforce it here, otherwise a direct callback with amount=1000
    # triggers a fixed-price switch for 1 sat.
    price_msat = await switch_price_msat(switch, _switch)
    if price_msat < 1000 or not amount_accepted(switch, _switch, price_msat, amount):
        max_msat = max_sendable_msat(price_msat, _switch)
        return LnurlErrorResponse(
            reason=f"Amount must be between {price_msat} and {max_msat} msat."
        )

    # Never take a payment the device cannot act on (v2.7.1).
    if not await device_can_deliver(switch_id):
        return LnurlErrorResponse(reason="No active ZapBox connections.")

    memo = f"{switch.title} (pin: {pin})"
    if comment:
        memo += f" - {comment}"

    metadata = LnurlPayMetadata(json.dumps([["text/plain", switch.title]]))

    payment = await create_invoice(
        wallet_id=switch.wallet,
        amount=math.ceil(amount / 1000),
        unhashed_description=metadata.encode(),
        memo=memo,
        extra={
            "tag": "ZapBox",
            "pin": pin,
            "comment": comment,
            "zapbox_id": switch_id,
        },
    )

    await create_switch_payment(
        payment_hash=payment.payment_hash,
        switch_id=switch.id,
        pin=pin,
        amount_msat=amount,
    )

    message = f"{int(amount / 1000)}sats sent"
    if _switch.label:
        message = f"{_switch.label} - GPIO {pin} - {message}"
    else:
        message = f"GPIO {pin} - {message}"
    if switch.password and switch.password != comment:
        message = f"{message}, but password was incorrect! :("

    return LnurlPayActionResponse(
        pr=TypeAdapter(LightningInvoice).validate_python(payment.bolt11),
        successAction=MessageAction(
            message=TypeAdapter(Max144Str).validate_python(message[:144])
        ),
        disposable=switch.disposable,
    )
