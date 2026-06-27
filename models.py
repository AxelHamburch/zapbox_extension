from datetime import datetime, timezone

from pydantic import BaseModel, Field


class Switch(BaseModel):
    amount: float = 0.0
    duration: int = 0
    pin: int = 0
    comment: bool = False
    variable: bool = False
    label: str | None = None


class CreateZapBox(BaseModel):
    title: str
    wallet: str
    currency: str
    switches: list[Switch]
    password: str | None = None
    disabled: bool = False
    disposable: bool = True
    teach_pin: str | None = None
    touch_enabled: bool = True
    auth_enabled: bool = True
    tagid_base_url: str | None = None
    tagid_api_key: str | None = None


class ZapBox(BaseModel):
    id: str
    title: str
    wallet: str
    currency: str
    switches: list[Switch]
    password: str | None = None
    disabled: bool = False
    disposable: bool = True
    teach_pin: str | None = None
    touch_enabled: bool = True
    auth_enabled: bool = True
    tagid_base_url: str | None = None
    tagid_api_key: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # obsolete field, do not use anymore
    # should be deleted from the database in the future
    key: str = ""


class ZapBoxPublic(BaseModel):
    title: str
    switches: list[Switch]


class MiniPosInvoiceRequest(BaseModel):
    amount: float
    currency: str
    device_id: str


class MiniPosPayment(BaseModel):
    id: str  # payment_hash
    zapbox_id: str
    wallet: str
    sats: int
    amount: float
    currency: str
    bolt11: str = ""  # kept so a Bolt Card tap can pay this pending invoice
    paid: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuthKey(BaseModel):
    id: str
    zapbox_id: str
    pubkey: str  # domain-specific linking key, hex (33-byte compressed)
    label: str | None = None
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CreateAuthKey(BaseModel):
    zapbox_id: str
    pubkey: str
    label: str | None = None
    enabled: bool = True


class UpdateAuthKey(BaseModel):
    label: str | None = None
    enabled: bool | None = None


class NfcIdentity(BaseModel):
    id: str
    zapbox_id: str
    card_id: str
    label: str | None = None
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CreateNfcIdentity(BaseModel):
    zapbox_id: str
    card_id: str
    label: str | None = None
    enabled: bool = True


class UpdateNfcIdentity(BaseModel):
    label: str | None = None
    enabled: bool | None = None


class ZapBoxPayment(BaseModel):
    id: str
    zapbox_id: str
    payment_hash: str
    pin: int
    sats: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # TODO: deprecated do not use this field anymore
    # should be deleted from the database in the future
    payload: str = ""
