import asyncio

from lnbits.core.models import Payment
from lnbits.core.services import websocket_updater
from lnbits.tasks import register_invoice_listener
from loguru import logger

from .crud import (
    get_minipos_payment,
    get_zapbox,
    get_switch_payment_by_payment_hash,
    update_minipos_payment,
)

MINIPOS_DEFAULT_DURATION = 3000  # ms, fallback when the device's pin has no switch config


async def wait_for_paid_invoices():
    invoice_queue = asyncio.Queue()
    register_invoice_listener(invoice_queue, "ext_zapbox")

    while True:
        payment = await invoice_queue.get()
        # Never let a single bad payment kill the listener — otherwise the device
        # stops being notified about *every* subsequent settlement until restart.
        try:
            await on_invoice_paid(payment)
        except Exception as exc:
            logger.error(
                f"ZapBox: error handling paid invoice {payment.payment_hash}: {exc}"
            )


async def on_invoice_paid(payment: Payment) -> None:
    if payment.extra.get("tag") != "ZapBox":
        return

    if payment.extra.get("minipos"):
        return await on_minipos_invoice_paid(payment)

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
        return await websocket_updater(zapbox.id, payload)

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

    return await websocket_updater(zapbox.id, payload)


async def on_minipos_invoice_paid(payment: Payment) -> None:
    """Mini-PoS settlement: mark the payment as paid and push the relay
    trigger to the device. The relay pin is supplied by the device when it
    requests the invoice (stored in extra['pin']) — the extension does not know
    the GPIO layout. Duration comes from that pin's switch config if present,
    otherwise MINIPOS_DEFAULT_DURATION."""
    zapbox_id = payment.extra.get("zapbox_id")
    pin = payment.extra.get("pin")
    if not zapbox_id or pin is None:
        logger.warning(
            f"Mini-PoS payment without zapbox_id/pin: {payment.payment_hash}"
        )
        return

    minipos_payment = await get_minipos_payment(payment.payment_hash)
    if minipos_payment:
        # Same race guard as switch payments: settlement can beat the insert
        if not minipos_payment.paid:
            minipos_payment.paid = True
            await update_minipos_payment(minipos_payment)
    else:
        for delay in (0.1, 0.3, 0.6):
            await asyncio.sleep(delay)
            minipos_payment = await get_minipos_payment(payment.payment_hash)
            if minipos_payment:
                minipos_payment.paid = True
                await update_minipos_payment(minipos_payment)
                break
        if not minipos_payment:
            logger.warning(
                f"Mini-PoS payment not found in DB: {payment.payment_hash}"
            )

    duration = MINIPOS_DEFAULT_DURATION
    zapbox = await get_zapbox(zapbox_id)
    if zapbox:
        _switch = next(
            (s for s in zapbox.switches if s.pin == int(pin)), None
        )
        if _switch and _switch.duration > 0:
            duration = _switch.duration

    logger.info(
        f"Mini-PoS paid: device={zapbox_id} hash={payment.payment_hash} "
        f"pin={pin} duration={duration}"
    )
    return await websocket_updater(zapbox_id, f"{pin}-{duration}")
