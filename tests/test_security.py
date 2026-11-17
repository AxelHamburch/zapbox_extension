"""Security fixes of v2.7.0 (see 00/lnbits/v1.7.x_update.md, Z1-Z9)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lnbits.core.services.users import create_user_account_no_ckeck
from lnbits.decorators import check_user_exists
from pydantic import ValidationError

from .. import tasks, views_api, views_lnurl
from ..crud import (
    create_minipos_payment,
    create_switch_payment,
    create_zapbox,
    get_zapbox,
)
from ..device_auth import device_token_ok
from ..models import CreateZapBox, MiniPosPayment

pytestmark = pytest.mark.asyncio(loop_scope="session")

API = "/zapbox/api/v1"


def as_user(app, user):
    app.dependency_overrides[check_user_exists] = lambda: user


async def make_zapbox(user, **kwargs):
    data = {
        "title": "Test box",
        "wallet": user.wallets[0].id,
        "currency": "sat",
        "switches": [{"pin": 4, "amount": 1000, "duration": 2000}],
    }
    data.update(kwargs)
    return await create_zapbox(CreateZapBox(**data))


def token_header(zapbox):
    return {"X-Device-Token": zapbox.device_token}


# --------------------------------------------------------------------------- #
# Z2 - takeover of a foreign ZapBox
# --------------------------------------------------------------------------- #


async def test_update_foreign_zapbox_is_forbidden(app, client, owner, stranger):
    zapbox = await make_zapbox(owner)
    as_user(app, stranger)
    response = await client.put(
        f"{API}/{zapbox.id}",
        json={
            "title": "taken over",
            "wallet": stranger.wallets[0].id,
            "currency": "sat",
            "switches": [{"pin": 1, "amount": 1, "duration": 0}],
        },
    )
    assert response.status_code == 403
    assert (await get_zapbox(zapbox.id)).wallet == owner.wallets[0].id


async def test_update_cannot_change_device_token(app, client, owner):
    zapbox = await make_zapbox(owner)
    as_user(app, owner)
    response = await client.put(
        f"{API}/{zapbox.id}",
        json={
            "title": "renamed",
            "wallet": owner.wallets[0].id,
            "currency": "sat",
            "switches": [{"pin": 4, "amount": 1000, "duration": 2000}],
            "device_token": "attacker",
        },
    )
    assert response.status_code == 200
    assert (await get_zapbox(zapbox.id)).device_token == zapbox.device_token


async def test_regenerate_token_owner_only(app, client, owner, stranger):
    zapbox = await make_zapbox(owner)
    as_user(app, stranger)
    assert (await client.put(f"{API}/{zapbox.id}/device_token")).status_code == 403
    as_user(app, owner)
    response = await client.put(f"{API}/{zapbox.id}/device_token")
    assert response.status_code == 200
    new_token = response.json()["device_token"]
    assert len(new_token) == 32 and new_token != zapbox.device_token


# --------------------------------------------------------------------------- #
# Z8 - underpayment via direct LNURL callback
# --------------------------------------------------------------------------- #


async def test_callback_rejects_underpayment(client, owner, monkeypatch):
    monkeypatch.setattr(views_lnurl, "has_device_channel", lambda _id: True)
    zapbox = await make_zapbox(owner)
    cb = f"{API}/lnurl/cb/{zapbox.id}/4"
    response = await client.get(cb, params={"amount": 1000})
    assert response.json()["status"] == "ERROR"
    response = await client.get(cb, params={"amount": 2_000_000})
    assert response.json()["status"] == "ERROR"  # above max for fixed price
    response = await client.get(cb, params={"amount": 1_000_000})
    assert "pr" in response.json(), response.text


async def test_callback_variable_switch_bounds(client, owner, monkeypatch):
    monkeypatch.setattr(views_lnurl, "has_device_channel", lambda _id: True)
    zapbox = await make_zapbox(
        owner,
        switches=[{"pin": 4, "amount": 10, "duration": 1000, "variable": True}],
    )
    cb = f"{API}/lnurl/cb/{zapbox.id}/4"
    assert (await client.get(cb, params={"amount": 9_000})).json()["status"] == "ERROR"
    assert "pr" in (await client.get(cb, params={"amount": 500_000})).json()
    over = await client.get(cb, params={"amount": 1_001_000})
    assert over.json()["status"] == "ERROR"


async def test_lnurl_params_invalid_pin_is_error(client, owner):
    zapbox = await make_zapbox(owner)
    response = await client.get(f"{API}/lnurl/{zapbox.id}", params={"pin": "x"})
    assert response.status_code == 200
    assert response.json()["status"] == "ERROR"


# --------------------------------------------------------------------------- #
# Z1 - forged payment.extra / settlement checks
# --------------------------------------------------------------------------- #


def fake_payment(wallet_id, amount_msat, extra, payment_hash=None):
    return SimpleNamespace(
        wallet_id=wallet_id,
        amount=amount_msat,
        extra=extra,
        payment_hash=payment_hash or __import__("secrets").token_hex(32),
    )


@pytest.fixture
def pushed(monkeypatch):
    calls = []

    async def push(device_id, text):
        calls.append((device_id, text))

    monkeypatch.setattr(tasks, "push_to_device", push)
    monkeypatch.setattr(tasks, "RETRY_DELAYS", ())
    return calls


async def test_forged_extra_in_foreign_wallet_does_not_trigger(owner, stranger, pushed):
    zapbox = await make_zapbox(owner)
    payment = fake_payment(
        stranger.wallets[0].id,
        1_000_000,
        {"tag": "ZapBox", "zapbox_id": zapbox.id, "pin": 4},
    )
    await tasks.on_invoice_paid(payment)
    assert pushed == []


async def test_forged_minipos_extra_does_not_trigger(owner, stranger, pushed):
    zapbox = await make_zapbox(owner)
    payment = fake_payment(
        stranger.wallets[0].id,
        1000,
        {"tag": "ZapBox", "minipos": True, "zapbox_id": zapbox.id, "pin": 4},
    )
    await tasks.on_invoice_paid(payment)
    assert pushed == []


async def test_minipos_record_in_other_wallet_does_not_trigger(owner, stranger, pushed):
    zapbox = await make_zapbox(owner)
    payment = fake_payment(
        stranger.wallets[0].id,
        5000,
        {"tag": "ZapBox", "minipos": True, "zapbox_id": zapbox.id, "pin": 4},
    )
    await create_minipos_payment(
        MiniPosPayment(
            id=payment.payment_hash,
            zapbox_id=zapbox.id,
            wallet=owner.wallets[0].id,
            sats=5,
            amount=5,
            currency="sat",
        )
    )
    await tasks.on_invoice_paid(payment)
    assert pushed == []


async def test_minipos_valid_triggers(owner, pushed):
    zapbox = await make_zapbox(owner)
    payment = fake_payment(
        owner.wallets[0].id,
        5000,
        {"tag": "ZapBox", "minipos": True, "zapbox_id": zapbox.id, "pin": 4},
    )
    await create_minipos_payment(
        MiniPosPayment(
            id=payment.payment_hash,
            zapbox_id=zapbox.id,
            wallet=owner.wallets[0].id,
            sats=5,
            amount=5,
            currency="sat",
        )
    )
    await tasks.on_invoice_paid(payment)
    assert pushed == [(zapbox.id, "4-2000")]


async def test_fallback_needs_zapbox_wallet_and_full_price(owner, pushed):
    zapbox = await make_zapbox(owner)
    extra = {"tag": "ZapBox", "zapbox_id": zapbox.id, "pin": 4}
    await tasks.on_invoice_paid(fake_payment(owner.wallets[0].id, 1000, extra))
    assert pushed == []  # 1 sat for a 1000 sat switch
    await tasks.on_invoice_paid(fake_payment(owner.wallets[0].id, 1_000_000, extra))
    assert pushed == [(zapbox.id, "4-2000")]


async def test_recorded_payment_checks_wallet_and_amount(owner, stranger, pushed):
    zapbox = await make_zapbox(owner)
    payment = fake_payment(
        stranger.wallets[0].id, 1_000_000, {"tag": "ZapBox", "zapbox_id": zapbox.id}
    )
    await create_switch_payment(payment.payment_hash, zapbox.id, 4, 1_000_000)
    await tasks.on_invoice_paid(payment)
    assert pushed == []
    payment.wallet_id = owner.wallets[0].id
    payment.amount = 999_000
    await tasks.on_invoice_paid(payment)
    assert pushed == []
    payment.amount = 1_000_000
    await tasks.on_invoice_paid(payment)
    assert pushed == [(zapbox.id, "4-2000")]


# --------------------------------------------------------------------------- #
# Mini-PoS invoice creation: device token + same account
# --------------------------------------------------------------------------- #


async def test_minipos_invoice_requires_token_and_same_user(client, owner, stranger):
    zapbox = await make_zapbox(owner)
    body = {"amount": 5, "currency": "sat", "device_id": zapbox.id, "pin": 4}
    response = await client.post(
        f"{API}/pos/invoice", json=body, headers={"X-API-KEY": owner.wallets[0].inkey}
    )
    assert response.status_code == 401
    response = await client.post(
        f"{API}/pos/invoice",
        json=body,
        headers={"X-API-KEY": stranger.wallets[0].inkey, **token_header(zapbox)},
    )
    assert response.status_code == 403
    second_wallet_owner = await create_user_account_no_ckeck(default_exts=["zapbox"])
    assert second_wallet_owner  # unrelated account stays unrelated
    response = await client.post(
        f"{API}/pos/invoice",
        json=body,
        headers={"X-API-KEY": owner.wallets[0].inkey, **token_header(zapbox)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["payment_request"]


# --------------------------------------------------------------------------- #
# Z5 / Z9 - device token on device-side endpoints
# --------------------------------------------------------------------------- #


async def test_device_endpoints_require_token(client, owner):
    zapbox = await make_zapbox(owner, auth_enabled=True, teach_pin="123456")
    tap = {"external_id": "abc", "p": "0" * 32, "c": "0" * 16}
    requests = [
        ("post", f"{API}/nfc/{zapbox.id}?pin=4", {"json": {"lnurlw": "lnurlw://x"}}),
        ("get", f"{API}/nfc/auth/{zapbox.id}", {"params": {**tap, "auth_pin": 4}}),
        ("get", f"{API}/nfc/teach/{zapbox.id}", {"params": tap}),
        ("get", f"{API}/auth/{zapbox.id}", {"params": {"pin": 4}}),
        (
            "post",
            f"{API}/auth/teach/start",
            {"params": {"device_id": zapbox.id, "pin": "123456"}},
        ),
        ("post", f"{API}/auth/teach/stop", {"params": {"device_id": zapbox.id}}),
        (
            "get",
            f"{API}/pos/invoice/last",
            {
                "params": {"device_id": zapbox.id},
                "headers": {"X-API-KEY": owner.wallets[0].inkey},
            },
        ),
    ]
    for method, url, kwargs in requests:
        headers = kwargs.pop("headers", {})
        bad = await getattr(client, method)(
            url, headers={**headers, "X-Device-Token": "wrong"}, **kwargs
        )
        assert bad.status_code == 401, (url, bad.text)
        missing = await getattr(client, method)(url, headers=headers, **kwargs)
        assert missing.status_code == 401, (url, missing.text)


async def test_auth_lnurl_with_token(client, owner):
    zapbox = await make_zapbox(owner, auth_enabled=True)
    response = await client.get(
        f"{API}/auth/{zapbox.id}", params={"pin": 4}, headers=token_header(zapbox)
    )
    assert response.status_code == 200
    assert response.json()["lnurl"].startswith("lnurl1")


async def test_device_token_compare():
    box = SimpleNamespace(device_token="a" * 32)
    assert device_token_ok(box, "a" * 32)
    assert not device_token_ok(box, "b" * 32)
    assert not device_token_ok(box, None)
    assert not device_token_ok(SimpleNamespace(device_token=""), "")


async def test_pin_submit_bound_to_session_device(client, owner):
    zapbox = await make_zapbox(owner)
    other = await make_zapbox(owner)
    import asyncio

    views_api.pin_sessions["s1"] = asyncio.Event()
    views_api.pin_session_device["s1"] = zapbox.id
    try:
        assert views_api.submit_pin("s1", "1234", other.id) is not None
        response = await client.post(
            f"{API}/nfc/pin_submit",
            params={"session_id": "s1", "pin": "1234"},
            headers=token_header(other),
        )
        assert response.status_code == 401
        response = await client.post(
            f"{API}/nfc/pin_submit",
            params={"session_id": "s1", "pin": "1234"},
            headers=token_header(zapbox),
        )
        assert response.status_code == 200
    finally:
        views_api.pin_sessions.pop("s1", None)
        views_api.pin_session_device.pop("s1", None)
        views_api.pin_results.pop("s1", None)


# --------------------------------------------------------------------------- #
# Z3 - SSRF in the NFC payment path
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "lnurlw",
    [
        "lnurlw://127.0.0.1:5000/api/v1/wallet",
        "lnurlw://169.254.169.254/latest/meta-data",
        "lnurlw://10.0.0.1/x",
    ],
)
async def test_nfc_payment_blocks_internal_targets(client, owner, lnurlw, monkeypatch):
    create_invoice = AsyncMock()
    monkeypatch.setattr(views_api, "create_invoice", create_invoice)
    zapbox = await make_zapbox(owner)
    response = await client.post(
        f"{API}/nfc/{zapbox.id}",
        params={"pin": 4},
        json={"lnurlw": lnurlw},
        headers=token_header(zapbox),
    )
    assert response.status_code == 502, response.text
    assert create_invoice.await_count == 0


async def test_nfc_payment_respects_card_budget(client, owner, monkeypatch):
    async def fake_lnurl(url, params=None, timeout=5):
        return {
            "tag": "withdrawRequest",
            "k1": "k",
            "callback": "https://card.example/cb",
            "maxWithdrawable": 500_000,
        }

    monkeypatch.setattr(views_api, "request_lnurl_json", fake_lnurl)
    create_invoice = AsyncMock()
    monkeypatch.setattr(views_api, "create_invoice", create_invoice)
    zapbox = await make_zapbox(owner)
    response = await client.post(
        f"{API}/nfc/{zapbox.id}",
        params={"pin": 4},
        json={"lnurlw": "lnurlw://card.example/scan"},
        headers=token_header(zapbox),
    )
    assert response.status_code == 422
    assert "Card limit reached" in response.json()["detail"]
    assert create_invoice.await_count == 0


# --------------------------------------------------------------------------- #
# Z4 - tagid_base_url validation, T18 option C
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/tagid",
        "https://user:pw@example.com/tagid",
        "https://example.com/tagid?x=1",
        "https://example.com/tagid#frag",
        "https://" + "a" * 200 + ".com",
    ],
)
async def test_tagid_base_url_rejected(url, owner):
    with pytest.raises(ValidationError):
        CreateZapBox(
            title="t",
            wallet=owner.wallets[0].id,
            currency="sat",
            switches=[],
            tagid_base_url=url,
        )


async def test_tagid_base_url_normalized(owner):
    data = CreateZapBox(
        title="t",
        wallet=owner.wallets[0].id,
        currency="sat",
        switches=[],
        tagid_base_url=" https://example.com/tagid/ ",
    )
    assert data.tagid_base_url == "https://example.com/tagid"
    empty = CreateZapBox(
        title="t", wallet="w", currency="sat", switches=[], tagid_base_url=""
    )
    assert empty.tagid_base_url is None


async def test_ring_login_pin_required(client, owner, monkeypatch):
    from .. import views_nfc
    from ..crud import upsert_nfc_identity
    from ..models import CreateNfcIdentity

    verify_result = {"verified": True, "card_id": "card1", "pin_verified": False}

    async def fake_verify(*args, **kwargs):
        return verify_result

    pushed = []

    async def push(device_id, text):
        pushed.append(text)

    monkeypatch.setattr(views_nfc, "_tagid_verify", fake_verify)
    monkeypatch.setattr(views_nfc, "push_to_device", push)
    zapbox = await make_zapbox(
        owner,
        auth_enabled=True,
        tagid_base_url="https://tagid.example/tagid",
        tagid_api_key="key",
        ring_login_pin_required=True,
    )
    await upsert_nfc_identity(CreateNfcIdentity(zapbox_id=zapbox.id, card_id="card1"))
    params = {"external_id": "abc_DEF-1", "p": "A" * 32, "c": "B" * 16, "auth_pin": 4}
    url = f"{API}/nfc/auth/{zapbox.id}"
    no_pin = await client.get(url, params=params, headers=token_header(zapbox))
    assert no_pin.json() == {"status": "ERROR", "reason": "PIN required"}
    unverified = await client.get(
        url, params={**params, "pin": "1234"}, headers=token_header(zapbox)
    )
    assert unverified.json()["status"] == "ERROR"
    verify_result["pin_verified"] = True
    ok = await client.get(url, params={**params, "pin": "1234"}, headers=token_header(zapbox))
    assert ok.json() == {"status": "OK"}
    assert pushed == ["4-3000"]
    bad_tap = await client.get(
        url, params={**params, "external_id": "../x"}, headers=token_header(zapbox)
    )
    assert bad_tap.status_code == 400


# --------------------------------------------------------------------------- #
# Device channel: token rejection is signalled to the firmware
# --------------------------------------------------------------------------- #


class FakeSocket:
    def __init__(self, headers, incoming=()):
        from starlette.datastructures import Headers

        self.headers = Headers(headers)
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None
        self.accepted = False
        self._incoming = list(incoming)

    async def accept(self):
        self.accepted = True

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def receive_text(self):
        from fastapi import WebSocketDisconnect

        if self._incoming:
            return self._incoming.pop(0)
        raise WebSocketDisconnect()


async def test_device_channel_rejects_bad_token_with_event(owner):
    import json

    from .. import views_ws
    from ..device_channel import has_device_channel

    zapbox = await make_zapbox(owner)
    for headers in ({}, {"x-device-token": "wrong"}):
        socket = FakeSocket(headers)
        await views_ws.websocket_nfc_channel(socket, zapbox.id)
        assert socket.accepted
        assert [json.loads(m) for m in socket.sent] == [
            {"event": "device_token_invalid"}
        ]
        assert socket.closed and socket.closed[0] == 4401
        assert not has_device_channel(zapbox.id)


async def test_device_channel_accepts_valid_token(owner):
    from .. import views_ws

    zapbox = await make_zapbox(owner)
    socket = FakeSocket({"x-device-token": zapbox.device_token})
    await views_ws.websocket_nfc_channel(socket, zapbox.id)
    assert socket.accepted and socket.closed is None
    assert socket.sent == []
