import asyncio

from lnbits.core.models import Payment
from lnbits.core.services import websocket_manager
from lnbits.tasks import register_invoice_listener
from loguru import logger

from .crud import (
    get_bitcoinswitch,
    get_switch_payment_by_payment_hash,
)


async def wait_for_paid_invoices():
    invoice_queue = asyncio.Queue()
    register_invoice_listener(invoice_queue, "ext_zapbox")

    while True:
        payment = await invoice_queue.get()
        await on_invoice_paid(payment)


async def on_invoice_paid(payment: Payment) -> None:
    if payment.extra.get("tag") != "Switch":
        return

    switch_payment = await get_switch_payment_by_payment_hash(payment.payment_hash)

    # Race condition guard: on fast Bolt Card payments the invoice can settle before
    # the DB transaction from create_switch_payment commits. Fall back to the
    # bitcoinswitch_id and pin stored directly in the invoice extra fields.
    if not switch_payment:
        bitcoinswitch_id = payment.extra.get("bitcoinswitch_id")
        pin = payment.extra.get("pin")
        if not bitcoinswitch_id or pin is None:
            logger.warning(
                f"Switch payment not found for payment hash: {payment.payment_hash}"
            )
            return
        logger.info(
            f"Switch payment not in DB yet – using extra fields "
            f"(bitcoinswitch_id={bitcoinswitch_id}, pin={pin})"
        )
        bitcoinswitch = await get_bitcoinswitch(bitcoinswitch_id)
        if not bitcoinswitch:
            logger.error("no bitcoinswitch found for payment.")
            return
        _switch = next(
            (s for s in bitcoinswitch.switches if s.pin == int(pin)),
            None,
        )
        if not _switch:
            logger.error(f"Switch with pin {pin} not found.")
            return
        payload = f"{_switch.pin}-{_switch.duration}"
        comment = payment.extra.get("comment")
        if comment:
            payload = f"{payload}-{comment}"
        if bitcoinswitch.password and bitcoinswitch.password != comment:
            logger.info(f"Wrong password entered for bitcoin switch: {bitcoinswitch.id}")
            return
        return await websocket_manager.send(bitcoinswitch.id, payload)

    bitcoinswitch = await get_bitcoinswitch(switch_payment.bitcoinswitch_id)
    if not bitcoinswitch:
        logger.error("no bitcoinswitch found for payment.")
        return

    _switch = next(
        (s for s in bitcoinswitch.switches if s.pin == switch_payment.pin),
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
    if bitcoinswitch.password and bitcoinswitch.password != comment:
        logger.info(f"Wrong password entered for bitcoin switch: {bitcoinswitch.id}")
        return

    return await websocket_manager.send(bitcoinswitch.id, payload)
