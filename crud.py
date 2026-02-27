from datetime import datetime, timezone

from lnbits.db import Database
from lnbits.helpers import urlsafe_short_hash

from .models import (
    Bitcoinswitch,
    BitcoinswitchPayment,
    CreateBitcoinswitch,
)

db = Database("ext_zapbox")


async def create_bitcoinswitch(
    data: CreateBitcoinswitch,
) -> Bitcoinswitch:
    bitcoinswitch_id = urlsafe_short_hash()
    device = Bitcoinswitch(
        id=bitcoinswitch_id,
        title=data.title,
        wallet=data.wallet,
        currency=data.currency,
        switches=data.switches,
        password=data.password,
        disabled=data.disabled,
        disposable=data.disposable,
    )
    await db.insert("zapbox.switch", device)
    return device


async def update_bitcoinswitch(device: Bitcoinswitch) -> Bitcoinswitch:
    device.updated_at = datetime.now(timezone.utc)
    await db.update("zapbox.switch", device)
    return device


async def get_bitcoinswitch(bitcoinswitch_id: str) -> Bitcoinswitch | None:
    return await db.fetchone(
        "SELECT * FROM zapbox.switch WHERE id = :id",
        {"id": bitcoinswitch_id},
        Bitcoinswitch,
    )


async def get_bitcoinswitches(wallet_ids: list[str]) -> list[Bitcoinswitch]:
    q = ",".join([f"'{w}'" for w in wallet_ids])
    return await db.fetchall(
        f"""
        SELECT * FROM zapbox.switch WHERE wallet IN ({q})
        ORDER BY id
        """,
        model=Bitcoinswitch,
    )


async def delete_bitcoinswitch(bitcoinswitch_id: str) -> None:
    await db.execute(
        "DELETE FROM zapbox.switch WHERE id = :id",
        {"id": bitcoinswitch_id},
    )


async def create_switch_payment(
    payment_hash: str,
    switch_id: str,
    pin: int,
    amount_msat: int = 0,
) -> BitcoinswitchPayment:
    payment_id = urlsafe_short_hash()
    payment = BitcoinswitchPayment(
        id=payment_id,
        payment_hash=payment_hash,
        bitcoinswitch_id=switch_id,
        pin=pin,
        sats=amount_msat,
    )
    await db.insert("zapbox.payment", payment)
    return payment


async def update_switch_payment(
    switch_payment: BitcoinswitchPayment,
) -> BitcoinswitchPayment:
    switch_payment.updated_at = datetime.now(timezone.utc)
    await db.update("zapbox.payment", switch_payment)
    return switch_payment


async def delete_switch_payment(switch_payment_id: str) -> None:
    await db.execute(
        "DELETE FROM zapbox.payment WHERE id = :id",
        {"id": switch_payment_id},
    )


async def get_switch_payment(
    bitcoinswitchpayment_id: str,
) -> BitcoinswitchPayment | None:
    return await db.fetchone(
        "SELECT * FROM zapbox.payment WHERE id = :id",
        {"id": bitcoinswitchpayment_id},
        BitcoinswitchPayment,
    )


async def get_switch_payment_by_payment_hash(
    payment_hash: str,
) -> BitcoinswitchPayment | None:
    return await db.fetchone(
        "SELECT * FROM zapbox.payment WHERE payment_hash = :h",
        {"h": payment_hash},
        BitcoinswitchPayment,
    )


async def get_switch_payments(
    bitcoinswitch_ids: list[str],
) -> list[BitcoinswitchPayment]:
    if len(bitcoinswitch_ids) == 0:
        return []
    q = ",".join([f"'{w}'" for w in bitcoinswitch_ids])
    return await db.fetchall(
        f"""
        SELECT * FROM zapbox.payment WHERE deviceid IN ({q})
        ORDER BY id
        """,
        model=BitcoinswitchPayment,
    )
