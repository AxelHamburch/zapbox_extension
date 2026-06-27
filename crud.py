from datetime import datetime, timezone

from lnbits.db import Database
from lnbits.helpers import urlsafe_short_hash

from .models import (
    AuthKey,
    CreateAuthKey,
    CreateNfcIdentity,
    MiniPosPayment,
    NfcIdentity,
    ZapBox,
    ZapBoxPayment,
    CreateZapBox,
)

db = Database("ext_zapbox")


async def create_zapbox(
    data: CreateZapBox,
) -> ZapBox:
    zapbox_id = urlsafe_short_hash()
    device = ZapBox(
        id=zapbox_id,
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


async def update_zapbox(device: ZapBox) -> ZapBox:
    device.updated_at = datetime.now(timezone.utc)
    await db.update("zapbox.switch", device)
    return device


async def get_zapbox(zapbox_id: str) -> ZapBox | None:
    return await db.fetchone(
        "SELECT * FROM zapbox.switch WHERE id = :id",
        {"id": zapbox_id},
        ZapBox,
    )


async def get_zapboxes(wallet_ids: list[str]) -> list[ZapBox]:
    q = ",".join([f"'{w}'" for w in wallet_ids])
    return await db.fetchall(
        f"""
        SELECT * FROM zapbox.switch WHERE wallet IN ({q})
        ORDER BY id
        """,
        model=ZapBox,
    )


async def delete_zapbox(zapbox_id: str) -> None:
    await db.execute(
        "DELETE FROM zapbox.switch WHERE id = :id",
        {"id": zapbox_id},
    )


async def create_switch_payment(
    payment_hash: str,
    switch_id: str,
    pin: int,
    amount_msat: int = 0,
) -> ZapBoxPayment:
    payment_id = urlsafe_short_hash()
    payment = ZapBoxPayment(
        id=payment_id,
        payment_hash=payment_hash,
        zapbox_id=switch_id,
        pin=pin,
        sats=amount_msat,
    )
    await db.insert("zapbox.payment", payment)
    return payment


async def update_switch_payment(
    switch_payment: ZapBoxPayment,
) -> ZapBoxPayment:
    switch_payment.updated_at = datetime.now(timezone.utc)
    await db.update("zapbox.payment", switch_payment)
    return switch_payment


async def delete_switch_payment(switch_payment_id: str) -> None:
    await db.execute(
        "DELETE FROM zapbox.payment WHERE id = :id",
        {"id": switch_payment_id},
    )


async def get_switch_payment(
    zapbox_payment_id: str,
) -> ZapBoxPayment | None:
    return await db.fetchone(
        "SELECT * FROM zapbox.payment WHERE id = :id",
        {"id": zapbox_payment_id},
        ZapBoxPayment,
    )


async def get_switch_payment_by_payment_hash(
    payment_hash: str,
) -> ZapBoxPayment | None:
    return await db.fetchone(
        "SELECT * FROM zapbox.payment WHERE payment_hash = :h",
        {"h": payment_hash},
        ZapBoxPayment,
    )


async def create_minipos_payment(payment: MiniPosPayment) -> MiniPosPayment:
    await db.insert("zapbox.minipos_payment", payment)
    return payment


async def get_minipos_payment(payment_hash: str) -> MiniPosPayment | None:
    return await db.fetchone(
        "SELECT * FROM zapbox.minipos_payment WHERE id = :id",
        {"id": payment_hash},
        MiniPosPayment,
    )


async def update_minipos_payment(payment: MiniPosPayment) -> MiniPosPayment:
    payment.updated_at = datetime.now(timezone.utc)
    await db.update("zapbox.minipos_payment", payment)
    return payment


async def get_last_paid_minipos_payment(
    zapbox_id: str, wallet_id: str
) -> MiniPosPayment | None:
    return await db.fetchone(
        """
        SELECT * FROM zapbox.minipos_payment
        WHERE zapbox_id = :zapbox_id AND wallet = :wallet AND paid = TRUE
        ORDER BY created_at DESC LIMIT 1
        """,
        {"zapbox_id": zapbox_id, "wallet": wallet_id},
        MiniPosPayment,
    )


async def create_auth_key(data: CreateAuthKey) -> AuthKey:
    auth_key = AuthKey(
        id=urlsafe_short_hash(),
        zapbox_id=data.zapbox_id,
        pubkey=data.pubkey,
        label=data.label,
        enabled=data.enabled,
    )
    await db.insert("zapbox.auth_key", auth_key)
    return auth_key


async def get_auth_keys(zapbox_id: str) -> list[AuthKey]:
    return await db.fetchall(
        """
        SELECT * FROM zapbox.auth_key WHERE zapbox_id = :zapbox_id
        ORDER BY created_at DESC
        """,
        {"zapbox_id": zapbox_id},
        AuthKey,
    )


async def get_auth_key(auth_key_id: str) -> AuthKey | None:
    return await db.fetchone(
        "SELECT * FROM zapbox.auth_key WHERE id = :id",
        {"id": auth_key_id},
        AuthKey,
    )


async def get_auth_key_by_pubkey(zapbox_id: str, pubkey: str) -> AuthKey | None:
    return await db.fetchone(
        """
        SELECT * FROM zapbox.auth_key
        WHERE zapbox_id = :zapbox_id AND pubkey = :pubkey
        """,
        {"zapbox_id": zapbox_id, "pubkey": pubkey},
        AuthKey,
    )


async def update_auth_key(auth_key: AuthKey) -> AuthKey:
    await db.update("zapbox.auth_key", auth_key)
    return auth_key


async def delete_auth_key(auth_key_id: str) -> None:
    await db.execute(
        "DELETE FROM zapbox.auth_key WHERE id = :id",
        {"id": auth_key_id},
    )


async def create_nfc_identity(data: CreateNfcIdentity) -> NfcIdentity:
    nfc_id = NfcIdentity(
        id=urlsafe_short_hash(),
        zapbox_id=data.zapbox_id,
        card_id=data.card_id,
        label=data.label,
        enabled=data.enabled,
    )
    await db.insert("zapbox.nfc_identity", nfc_id)
    return nfc_id


async def upsert_nfc_identity(data: CreateNfcIdentity) -> NfcIdentity:
    existing = await get_nfc_identity_by_card_id(data.zapbox_id, data.card_id)
    if existing:
        existing.enabled = True
        if data.label is not None:
            existing.label = data.label
        return await update_nfc_identity(existing)
    return await create_nfc_identity(data)


async def get_nfc_identities(zapbox_id: str) -> list[NfcIdentity]:
    return await db.fetchall(
        """
        SELECT * FROM zapbox.nfc_identity WHERE zapbox_id = :zapbox_id
        ORDER BY created_at DESC
        """,
        {"zapbox_id": zapbox_id},
        NfcIdentity,
    )


async def get_nfc_identity(nfc_id: str) -> NfcIdentity | None:
    return await db.fetchone(
        "SELECT * FROM zapbox.nfc_identity WHERE id = :id",
        {"id": nfc_id},
        NfcIdentity,
    )


async def get_nfc_identity_by_card_id(zapbox_id: str, card_id: str) -> NfcIdentity | None:
    return await db.fetchone(
        """
        SELECT * FROM zapbox.nfc_identity
        WHERE zapbox_id = :zapbox_id AND card_id = :card_id
        """,
        {"zapbox_id": zapbox_id, "card_id": card_id},
        NfcIdentity,
    )


async def update_nfc_identity(nfc: NfcIdentity) -> NfcIdentity:
    await db.update("zapbox.nfc_identity", nfc)
    return nfc


async def delete_nfc_identity(nfc_id: str) -> None:
    await db.execute(
        "DELETE FROM zapbox.nfc_identity WHERE id = :id",
        {"id": nfc_id},
    )


async def get_switch_payments(
    zapbox_ids: list[str],
) -> list[ZapBoxPayment]:
    if len(zapbox_ids) == 0:
        return []
    q = ",".join([f"'{w}'" for w in zapbox_ids])
    return await db.fetchall(
        f"""
        SELECT * FROM zapbox.payment WHERE deviceid IN ({q})
        ORDER BY id
        """,
        model=ZapBoxPayment,
    )
