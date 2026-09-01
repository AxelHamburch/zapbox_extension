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

from fastapi import WebSocket
from lnbits.core.services import websocket_updater
from loguru import logger

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
    await websocket_updater(device_id, text)
