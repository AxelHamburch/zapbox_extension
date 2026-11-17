"""Device authentication (v2.7.0).

Every ZapBox has a device_token: a shared secret known only to the extension
and the device firmware. It reaches the device inside the device settings
string (wss://<host>/api/v1/ws/<id>?token=<token>); the firmware stores it and
sends it as X-Device-Token header on every device-side call. It is never sent
in a URL.

Before v2.7.0 these endpoints only needed the device ID, which is public (it
is printed in the LNURL QR code). That allowed anyone to hijack the device
channel, start NFC payments, enrol identities during a teach window, or call
Ring-Login directly with a captured tap and no PIN.
"""

import secrets
from http import HTTPStatus

from fastapi import HTTPException
from loguru import logger

from .crud import get_zapbox
from .models import ZapBox

DEVICE_TOKEN_HEADER = "X-Device-Token"
# Close code for the device WebSocket channel when the token is missing/wrong.
WS_CLOSE_INVALID_TOKEN = 4401
# Shown on the device display (NFC error screen): the part before ": " gets its
# own line, the rest wraps. Keep it short.
INVALID_TOKEN_DETAIL = "Token invalid: update device string"
# Event sent over the device channel right before it is closed with 4401, so
# the firmware can tell a rejected token from a network problem, stop
# reconnecting and tell the user.
TOKEN_INVALID_EVENT = "device_token_invalid"


def device_token_ok(zapbox: ZapBox, token: str | None) -> bool:
    if not zapbox.device_token or not token:
        return False
    return secrets.compare_digest(zapbox.device_token, token)


def check_device_token(zapbox: ZapBox, token: str | None) -> None:
    if not device_token_ok(zapbox, token):
        logger.warning(f"ZapBox: rejected device call without valid token: {zapbox.id}")
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED, detail=INVALID_TOKEN_DETAIL
        )


async def get_authenticated_zapbox(zapbox_id: str, token: str | None) -> ZapBox:
    """Load a ZapBox and require a valid device token.

    Unknown IDs and wrong tokens both answer 404/401 without further detail.
    """
    zapbox = await get_zapbox(zapbox_id)
    if not zapbox:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Device not found.")
    check_device_token(zapbox, token)
    return zapbox
