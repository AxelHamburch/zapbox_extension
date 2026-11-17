"""Device WebSocket channel (device → server).

The ZapBox connects to LNbits core's /api/v1/ws/{device_id} to RECEIVE relay
triggers — that channel, the server pings on it, and the core
websocket_manager.has_connection() check in views_lnurl.py all stay untouched.

This endpoint adds the missing direction: a persistent connection the DEVICE
can send events over, so a Bolt Card tap does not need a fresh HTTPS
connection. Some consumer routers were observed to fail NEW TLS connections in
phases (any destination) while established connections kept working in both
directions — the tap now rides an established channel that was opened once at
boot.

Protocol (JSON text frames):

  device → server:
    {"event": "lnurlw", "request_id": "42", "lnurlw": "lnurlw://...",
     "pin": 403, "minipos_hash": null}

  server → device:
    {"event": "lnurlw_result", "request_id": "42", "status": "OK",
     "payment_hash": "..."}
    {"event": "lnurlw_result", "request_id": "42", "status": "ERROR",
     "detail": "..."}

  device → server (v2.6.2+):
    {"event": "pin_submit", "session_id": "...", "pin": "1234"}
  server → device:
    {"event": "pin_submit_result", "session_id": "...", "status": "OK"}
    {"event": "pin_submit_result", "session_id": "...", "status": "ERROR",
     "detail": "..."}

Relay triggers on settlement and the pin_required / pin_error events go
through push_to_device() (device_channel.py): over this channel when it is
connected, core WebSocket otherwise. The HTTPS pin_submit endpoint remains as
fallback for older firmware.

Authentication (v2.7.0): the handshake must carry the header
  X-Device-Token: <device token of this ZapBox>
otherwise the server accepts, sends {"event": "device_token_invalid"} and
immediately closes with code 4401 ("Device token invalid"). The firmware then
stops reconnecting the channel and shows a notice. The token reaches the
device inside the device settings string
(wss://<host>/api/v1/ws/<id>?token=<token>); the firmware strips it from
the URL before connecting.

With a valid token the server sends {"event": "device_channel_ready"} once the
channel is registered. The firmware switches to single-connection mode (drops
the core WebSocket) only on that event, never on the bare connect.

Keepalive is protocol-level: the device sends WebSocket pings
(enableHeartbeat), the server answers pongs automatically — no application
code needed here.

The HTTPS POST /api/v1/nfc/{device_id} remains as the device's fallback when
this channel is down, and for older firmware that never connects here.
"""

import asyncio
import json

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from loguru import logger

from .crud import enable_signed_triggers, get_zapbox
from .device_auth import (
    DEVICE_TOKEN_HEADER,
    CHANNEL_READY_EVENT,
    TOKEN_INVALID_EVENT,
    WS_CLOSE_INVALID_TOKEN,
    device_token_ok,
)
from .device_channel import register_channel, spawn, unregister_channel
from .views_api import process_nfc_lnurlw, submit_pin

zapbox_ws_router = APIRouter(prefix="/api/v1")


async def _handle_lnurlw_event(
    websocket: WebSocket, send_lock: asyncio.Lock, device_id: str, msg: dict
) -> None:
    """Run one lnurlw payment request and send the result back on the socket."""
    request_id = str(msg.get("request_id", ""))
    reply: dict = {"event": "lnurlw_result", "request_id": request_id}
    try:
        lnurlw = str(msg.get("lnurlw", ""))
        pin = int(msg.get("pin", -1))
        minipos_hash = msg.get("minipos_hash") or None
        result = await process_nfc_lnurlw(device_id, pin, lnurlw, minipos_hash)
        reply.update(result)  # {"status": "OK", "payment_hash": ...}
    except HTTPException as exc:
        reply["status"] = "ERROR"
        reply["detail"] = str(exc.detail)
    except Exception as exc:  # never let one bad tap kill the channel
        logger.exception(f"ZapBox WS: lnurlw handling failed for {device_id}: {exc}")
        reply["status"] = "ERROR"
        # Name the failing exception instead of a bare "Internal error." so the
        # device error screen shows what actually went wrong.
        # The device splits on the first ": " and gives the label its own line,
        # then word-wraps the rest (max. 4 lines of ~25 chars on the touch display).
        reason = str(exc).strip().splitlines()[0][:60] if str(exc).strip() else ""
        reply["detail"] = f"Internal error: {type(exc).__name__}"
        if reason:
            reply["detail"] += f" - {reason}"
    try:
        async with send_lock:
            await websocket.send_text(json.dumps(reply))
    except Exception:
        # Device disconnected while the payment ran. Not fatal: on success the
        # relay trigger still arrives via the core WebSocket; on failure the
        # device's own pending timeout shows NO LUCK.
        logger.info(
            f"ZapBox WS: could not deliver lnurlw_result to {device_id} "
            "(channel closed)"
        )


@zapbox_ws_router.websocket("/ws/nfc/{device_id}")
async def websocket_nfc_channel(websocket: WebSocket, device_id: str) -> None:
    zapbox = await get_zapbox(device_id)
    if not zapbox:
        # Accept, then close with a distinct code — the client then sees a clean
        # close instead of a failed HTTP upgrade it cannot tell from a proxy issue.
        await websocket.accept()
        await websocket.close(code=4404, reason="Device not found")
        return

    # v2.7.0: only the real device (knows the device token) may open the
    # channel. Without this anyone with the public device ID could replace the
    # channel, swallow relay triggers and see PIN sessions.
    if not device_token_ok(zapbox, websocket.headers.get(DEVICE_TOKEN_HEADER)):
        logger.warning(f"ZapBox WS: rejected device channel without valid token: {device_id}")
        await websocket.accept()
        await websocket.send_text(json.dumps({"event": TOKEN_INVALID_EVENT}))
        await websocket.close(code=WS_CLOSE_INVALID_TOKEN, reason="Device token invalid")
        return

    await websocket.accept()
    logger.info(f"ZapBox WS: device channel connected: {device_id}")
    # Register so push_to_device() routes relay triggers and events over this
    # socket instead of the (possibly half-open) core WebSocket. The returned
    # lock serializes writes: replies from spawned tasks and pushed events must
    # not interleave frames on the socket.
    # The device proved it knows its token: firmware with signed-trigger
    # support. Core-WebSocket fallback triggers are signed from now on (Z6).
    await enable_signed_triggers(device_id)
    send_lock = register_channel(device_id, websocket)
    try:
        # Confirm the channel only now (token checked, channel registered). The
        # firmware releases its core WebSocket on this event, not on the bare
        # connect: a rejected channel never takes the core WebSocket down.
        async with send_lock:
            await websocket.send_text(json.dumps({"event": CHANNEL_READY_EVENT}))
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue  # not JSON — ignore
            if not isinstance(msg, dict):
                continue
            event = msg.get("event")
            if event == "lnurlw":
                # Spawn a task so the receive loop stays responsive while the
                # payment (LNURLW resolve + callback, up to ~20 s) runs.
                spawn(
                    _handle_lnurlw_event(websocket, send_lock, device_id, msg)
                )
            elif event == "pin_submit":
                # PIN entered on the touch display — same validation as the
                # HTTPS endpoint api_nfc_pin_submit (which remains the fallback
                # for older firmware). Pure dict work, so handled inline.
                session_id = str(msg.get("session_id", ""))
                pin = str(msg.get("pin", ""))
                reply = {"event": "pin_submit_result", "session_id": session_id}
                error = submit_pin(session_id, pin, device_id)
                if error:
                    reply["status"] = "ERROR"
                    reply["detail"] = error
                else:
                    reply["status"] = "OK"
                async with send_lock:
                    await websocket.send_text(json.dumps(reply))
            # Unknown events are ignored — room for future message types
            # without breaking older servers.
    except WebSocketDisconnect:
        logger.info(f"ZapBox WS: device channel disconnected: {device_id}")
    finally:
        unregister_channel(device_id, websocket)
