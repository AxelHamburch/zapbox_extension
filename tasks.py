import asyncio

from lnbits.core.models import Payment
from lnbits.core.services import websocket_manager
from lnbits.tasks import register_invoice_listener
from loguru import logger

from .crud import (
    get_zapbox,
    get_switch_payment_by_payment_hash,
)


async def wait_for_paid_invoices():
    invoice_queue = asyncio.Queue()
    register_invoice_listener(invoice_queue, "ext_zapbox")

    while True:
        payment = await invoice_queue.get()
        await on_invoice_paid(payment)


async def on_invoice_paid(payment: Payment) -> None:
    if payment.extra.get("tag") != "ZapBox":
        return

    switch_payment = await get_switch_payment_by_payment_hash(payment.payment_hash)

    # Race condition guard: on fast Bolt Card payments the invoice can settle before
    # the DB transaction from create_switch_payment commits. Retry a few times before
    # falling back to the zapbox_id and pin stored in the invoice extra fields.
    if not switch_payment:
        for delay in (0.1, 0.3, 0.6):
            await asyncio.sleep(delay)
            switch_payment = await get_switch_payment_by_payment_hash(payment.payment_hash)
            if switch_payment:
                break

    if not switch_payment:
        zapbox_id = payment.extra.get("zapbox_id")
        pin = payment.extra.get("pin")
        if not zapbox_id or pin is None:
            logger.warning(
                f"Switch payment not found for payment hash: {payment.payment_hash}"
            )
            return
        logger.info(
            f"Switch payment not in DB yet – using extra fields "
            f"(zapbox_id={zapbox_id}, pin={pin})"
        )
        zapbox = await get_zapbox(zapbox_id)
        if not zapbox:
            logger.error("no ZapBox found for payment.")
            return
        _switch = next(
            (s for s in zapbox.switches if s.pin == int(pin)),
            None,
        )
        if not _switch:
            logger.error(f"Switch with pin {pin} not found.")
            return
        payload = f"{_switch.pin}-{_switch.duration}"
        comment = payment.extra.get("comment")
        if comment:
            payload = f"{payload}-{comment}"
        if zapbox.password and zapbox.password != comment:
            logger.info(f"Wrong password entered for ZapBox: {zapbox.id}")
            return
        return await websocket_manager.send(zapbox.id, payload)

    zapbox = await get_zapbox(switch_payment.zapbox_id)
    if not zapbox:
        logger.error("no ZapBox found for payment.")
        return

    _switch = next(
        (s for s in zapbox.switches if s.pin == switch_payment.pin),
        None,
    )

    if not _switch:
        logger.error(f"Switch with pin {switch_payment.pin} not found.")
        return

    duration = _switch.duration

    if _switch.variable is True:
        duration = round(
            (switch_payment.sats / 1000) / _switch.amount * _switch.duration
        )

    payload = f"{_switch.pin}-{duration}"

    comment = payment.extra.get("comment")
    if comment:
        payload = f"{payload}-{comment}"

    # Wrong password in comment
    if zapbox.password and zapbox.password != comment:
        logger.info(f"Wrong password entered for ZapBox: {zapbox.id}")
        return

    return await websocket_manager.send(zapbox.id, payload)
