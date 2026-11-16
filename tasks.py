import asyncio

from lnbits.core.crud import get_wallet
from lnbits.core.models import Payment
from lnbits.settings import settings
from lnbits.tasks import register_invoice_listener
from loguru import logger

from .crud import (
    get_minipos_payment,
    get_switch_payment_by_payment_hash,
    get_zapbox,
    update_minipos_payment,
)
from .device_channel import push_to_device
from .pricing import paid_enough, switch_price_msat

MINIPOS_DEFAULT_DURATION = 3000  # ms, fallback when the device's pin has no switch config

# Race guard: on fast Bolt Card payments the invoice can settle before the
# DB insert of the switch/Mini-PoS record has committed.
RETRY_DELAYS = (0.1, 0.3, 0.6, 1.0)


async def wait_for_paid_invoices():
    invoice_queue = asyncio.Queue()
    register_invoice_listener(invoice_queue, "ext_zapbox")

    while settings.lnbits_running:
        payment = await invoice_queue.get()
        # Never let a single bad payment kill the listener — otherwise the device
        # stops being notified about *every* subsequent settlement until restart.
        try:
            await on_invoice_paid(payment)
        except Exception as exc:
            logger.error(
                f"ZapBox: error handling paid invoice {payment.payment_hash}: {exc}"
            )


async def paid_into(payment: Payment, wallet_id: str) -> bool:
    """True if the payment was received by wallet_id.

    `payment.extra` is chosen by whoever creates the invoice: any LNbits user
    can create an invoice with tag "ZapBox" in their OWN wallet and pay it
    from a second own wallet. Only invoices that landed in the ZapBox wallet
    may trigger the relay. Shared wallets record payments on their source
    wallet in newer LNbits versions.
    """
    if payment.wallet_id == wallet_id:
        return True
    wallet = await get_wallet(wallet_id)
    return bool(wallet) and payment.wallet_id == getattr(
        wallet, "source_wallet_id", None
    )


async def on_invoice_paid(payment: Payment) -> None:
    if payment.extra.get("tag") != "ZapBox":
        return

    if payment.extra.get("minipos"):
        return await on_minipos_invoice_paid(payment)

    switch_payment = await get_switch_payment_by_payment_hash(payment.payment_hash)
    if not switch_payment:
        for delay in RETRY_DELAYS:
            await asyncio.sleep(delay)
            switch_payment = await get_switch_payment_by_payment_hash(
                payment.payment_hash
            )
            if switch_payment:
                break

    if not switch_payment:
        return await _on_unrecorded_switch_payment(payment)

    zapbox = await get_zapbox(switch_payment.zapbox_id)
    if not zapbox:
        logger.error("no ZapBox found for payment.")
        return
    if not await paid_into(payment, zapbox.wallet):
        logger.warning(
            f"ZapBox: ignoring payment {payment.payment_hash} not received by "
            f"the wallet of {zapbox.id}"
        )
        return
    # switch_payment.sats holds the requested amount in msat.
    if payment.amount < switch_payment.sats:
        logger.warning(
            f"ZapBox: payment {payment.payment_hash} below the requested amount "
            f"({payment.amount} < {switch_payment.sats} msat)"
        )
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

    return await push_to_device(zapbox.id, payload)


async def _on_unrecorded_switch_payment(payment: Payment) -> None:
    """Settlement without a switch_payment record (insert never committed).

    Kept so a customer who paid still gets the relay, but only for invoices
    that were received by the ZapBox wallet and cover the full switch price.
    Variable pricing needs the record, so those use the base duration.
    """
    zapbox_id = payment.extra.get("zapbox_id")
    pin = payment.extra.get("pin")
    if not zapbox_id or pin is None:
        logger.warning(
            f"Switch payment not found for payment hash: {payment.payment_hash}"
        )
        return
    zapbox = await get_zapbox(str(zapbox_id))
    if not zapbox:
        logger.error("no ZapBox found for payment.")
        return
    if not await paid_into(payment, zapbox.wallet):
        logger.warning(
            f"ZapBox: ignoring unrecorded payment {payment.payment_hash} not "
            f"received by the wallet of {zapbox.id}"
        )
        return
    try:
        pin_int = int(pin)
    except (TypeError, ValueError):
        return
    _switch = next((s for s in zapbox.switches if s.pin == pin_int), None)
    if not _switch:
        logger.error(f"Switch with pin {pin} not found.")
        return
    price_msat = await switch_price_msat(zapbox, _switch)
    if not paid_enough(zapbox, price_msat, payment.amount):
        logger.warning(
            f"ZapBox: unrecorded payment {payment.payment_hash} below the switch "
            f"price ({payment.amount} < {price_msat} msat)"
        )
        return
    logger.info(
        f"Switch payment not in DB – using verified extra fields "
        f"(zapbox_id={zapbox.id}, pin={pin_int})"
    )
    payload = f"{_switch.pin}-{_switch.duration}"
    comment = payment.extra.get("comment")
    if comment:
        payload = f"{payload}-{comment}"
    if zapbox.password and zapbox.password != comment:
        logger.info(f"Wrong password entered for ZapBox: {zapbox.id}")
        return
    return await push_to_device(zapbox.id, payload)


async def on_minipos_invoice_paid(payment: Payment) -> None:
    """Mini-PoS settlement: mark the payment as paid and push the relay
    trigger to the device. The relay pin is supplied by the device when it
    requests the invoice (stored in extra['pin']) — the extension does not know
    the GPIO layout. Duration comes from that pin's switch config if present,
    otherwise MINIPOS_DEFAULT_DURATION.

    Only triggers if the Mini-PoS record created by /pos/invoice exists for
    this payment hash and the payment landed in that record's wallet. The
    record is only created for devices of the same LNbits account.
    """
    pin = payment.extra.get("pin")
    minipos_payment = await get_minipos_payment(payment.payment_hash)
    if not minipos_payment:
        for delay in RETRY_DELAYS:
            await asyncio.sleep(delay)
            minipos_payment = await get_minipos_payment(payment.payment_hash)
            if minipos_payment:
                break
    if not minipos_payment:
        logger.warning(
            f"Mini-PoS payment not found in DB, not triggering: {payment.payment_hash}"
        )
        return
    if not await paid_into(payment, minipos_payment.wallet):
        logger.warning(
            f"Mini-PoS: ignoring payment {payment.payment_hash} not received by "
            "the Mini-PoS wallet"
        )
        return
    if payment.amount < minipos_payment.sats * 1000:
        logger.warning(f"Mini-PoS: payment {payment.payment_hash} below the amount")
        return
    if pin is None:
        logger.warning(f"Mini-PoS payment without pin: {payment.payment_hash}")
        return
    try:
        pin_int = int(pin)
    except (TypeError, ValueError):
        return

    if not minipos_payment.paid:
        minipos_payment.paid = True
        await update_minipos_payment(minipos_payment)

    zapbox_id = minipos_payment.zapbox_id
    duration = MINIPOS_DEFAULT_DURATION
    zapbox = await get_zapbox(zapbox_id)
    if zapbox:
        _switch = next((s for s in zapbox.switches if s.pin == pin_int), None)
        if _switch and _switch.duration > 0:
            duration = _switch.duration

    logger.info(
        f"Mini-PoS paid: device={zapbox_id} hash={payment.payment_hash} "
        f"pin={pin_int} duration={duration}"
    )
    return await push_to_device(zapbox_id, f"{pin_int}-{duration}")
