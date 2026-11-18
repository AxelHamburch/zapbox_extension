"""Registry and push helper for the persistent device channel.

Lives in its own module so both views_ws.py (which registers connections) and
every push site (tasks.py, views_api.py, views_auth.py, views_nfc.py) can use
it without circular imports.

Why pushes prefer the device channel: the device's protocol pings on that
socket prove to the server every ~20 s that the connection is alive, while the
core WebSocket (/api/v1/ws/{id}) can sit half-open on NAT-challenged routers —
events pushed into it are silently lost (observed: invoice paid, relay never
triggered). If the device has no channel (older firmware) or the send fails,
push_to_device() falls back to the core WebSocket, so nothing existing breaks.
"""

import asyncio
import hashlib
import hmac
import json
import re

from fastapi import WebSocket
from lnbits.core.services import websocket_updater
from loguru import logger

from .device_auth import TOKEN_INVALID_EVENT, WS_CLOSE_INVALID_TOKEN

# device_id → (websocket, per-connection send lock)
_channels: dict[str, tuple[WebSocket, asyncio.Lock]] = {}


def register_channel(device_id: str, websocket: WebSocket) -> asyncio.Lock:
    lock = asyncio.Lock()
    _channels[device_id] = (websocket, lock)
    return lock


def unregister_channel(device_id: str, websocket: WebSocket) -> None:
    # Only remove if this socket is still the registered one — a reconnect may
    # already have replaced the entry before the old handler's cleanup runs.
    entry = _channels.get(device_id)
    if entry and entry[0] is websocket:
        _channels.pop(device_id, None)


async def close_device_channel(device_id: str) -> bool:
    """Drop the open device channel because its token is no longer valid.

    The token is only checked when a channel is opened, so after "New token" an
    already connected device would keep working with the old one. The device
    gets the invalid-token event and close code 4401 (same as a rejected
    handshake) and then shows "Token invalid". Returns True if a channel was
    open.
    """
    entry = _channels.get(device_id)
    if not entry:
        return False
    websocket, lock = entry
    unregister_channel(device_id, websocket)
    try:
        async with lock:
            await websocket.send_text(json.dumps({"event": TOKEN_INVALID_EVENT}))
            await websocket.close(
                code=WS_CLOSE_INVALID_TOKEN, reason="Device token invalid"
            )
    except Exception as exc:
        logger.warning(f"ZapBox: closing device channel {device_id} failed: {exc}")
    return True


def has_device_channel(device_id: str) -> bool:
    return device_id in _channels


async def push_to_device(device_id: str, text: str) -> None:
    """Send an event to the device: device channel first, core WS as fallback.

    Either/or, never both — no duplicate relay triggers by construction.
    """
    entry = _channels.get(device_id)
    if entry:
        websocket, lock = entry
        try:
            async with lock:
                await websocket.send_text(text)
            return
        except Exception as exc:
            logger.warning(
                f"ZapBox: device channel send failed for {device_id} "
                f"({exc}) — falling back to core WebSocket"
            )
            unregister_channel(device_id, websocket)
    await websocket_updater(device_id, await _core_ws_payload(device_id, text))


# Relay trigger as sent to the firmware: "<pin>-<duration>[-<comment>]".
_TRIGGER_RE = re.compile(r"^\d+-\d+(-.*)?$", re.DOTALL)


def sign_trigger(token: str, device_id: str, counter: int, data: str) -> str:
    message = f"{device_id}|{counter}|{data}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


async def _core_ws_payload(device_id: str, text: str) -> str:
    """Wrap relay triggers for the core WebSocket in a signed envelope (Z6).

    LNbits core lets anyone push text into /api/v1/ws/<id> without
    authentication (GHSA-wqxg-3j7v-2v3c), and the device ID is public. Firmware
    with device token support therefore only accepts
        {"event": "trigger", "data": "4-2000", "ctr": 17, "sig": "<hmac>"}
    on the core WebSocket: sig = HMAC-SHA256(device token,
    "<device id>|<ctr>|<data>"), ctr strictly increasing (anyone can listen on
    the core WebSocket, so replays must fail). Older firmware never proved it
    has a token and keeps getting the plain trigger. Non-trigger events (PIN,
    teach, ...) stay plain: forging them cannot switch the relay.
    """
    if not _TRIGGER_RE.match(text):
        return text
    from .crud import get_trigger_state, get_zapbox, next_trigger_counter

    _, signed = await get_trigger_state(device_id)
    if not signed:
        return text
    zapbox = await get_zapbox(device_id)
    counter = await next_trigger_counter(device_id)
    if not zapbox or not zapbox.device_token or counter is None:
        return text
    return json.dumps(
        {
            "event": "trigger",
            "data": text,
            "ctr": counter,
            "sig": sign_trigger(zapbox.device_token, device_id, counter, text),
        }
    )


# Strong references for fire-and-forget tasks (PIN loops, tap handlers, teach
# timeouts). asyncio only keeps weak references, so an unreferenced task can
# be garbage-collected while it is still running.
_background_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
