# ZapBox Extension - <small>[LNbits](https://github.com/lnbits/lnbits) extension</small>

> **Forked from [lnbits/bitcoinswitch_extension](https://github.com/lnbits/bitcoinswitch_extension)**  
> Extended with NFC Bolt Card (NTAG424 DNA / LNURLW) support for the [ZapBox](https://github.com/AxelHamburch/ZapBox) device.<br>
> **Basic NFC Bolt Card functions were taken from [pieterjm/partytap_extension](https://github.com/pieterjm/partytap_extension)**

Turn things on with bitcoin – now with NFC Bolt Card support!

Check out [lnbits.com](https://lnbits.com) & join the Telegram support group [Makerbits](https://t.me/makerbits)

`Original authors: Ben Arc, DNI, pieterjm` / `ZapBox extension: AxelHamburch`

Extension manifest source for LNbits: [https://raw.githubusercontent.com/AxelHamburch/zapbox_extension/main/extensions.json](https://raw.githubusercontent.com/AxelHamburch/zapbox_extension/main/extensions.json)

---

## v2.7.1 – "New token" now disconnects the device at once

The device token was only checked when the device channel was opened. After **New token** an already connected ZapBox therefore kept working with the old token until it reconnected (for example after a restart). The open channel is now closed immediately with the event `device_token_invalid` (close code 4401); the firmware shows "Token invalid: update device string". No migration, no firmware change.

---

## v2.7.0 – Security update for LNbits 1.7.0 (breaking)

Requires **LNbits 1.7.0** or newer and **ZapBox firmware with device token support**.

> ⚠️ **Breaking change for security reasons:** every ZapBox now has a secret **device token**. Devices with older firmware stop working after this update until they get the new firmware **and** the new device string (copy button in the ZapBox list, paste it into the Web Installer). LNbits 1.7.0 updates extensions automatically, so plan the firmware update together with the LNbits upgrade.

**Device token**

- Created automatically for every ZapBox (existing ones get one during the update). It is part of the device string: `wss://<host>/api/v1/ws/<id>?token=<token>`.
- The firmware sends it as `X-Device-Token` header on every device call: device channel, NFC payments, PIN entry, Mini-PoS, Ring-Login, LNURL-auth and teach mode. It is never sent in a URL.
- "New token" in the ZapBox editor creates a new token; the old one stops working immediately.

**Security fixes**

- Relay could be triggered without paying the switch: invoices with a forged `extra` field in another wallet, Mini-PoS invoices for foreign devices, and direct LNURL callbacks with 1 sat for a fixed-price switch. Settlement now requires the payment to land in the ZapBox wallet with the full amount, Mini-PoS needs the invoice key of the ZapBox owner's account, and the LNURL callback enforces the price.
- A ZapBox could be taken over by another user via `PUT /zapbox/api/v1/{id}`.
- NFC payments and Ring-Login requests to TagID could be pointed at internal addresses (SSRF). Card requests now go through LNbits' protected LNURL client; TagID on the same instance is called in-process.
- The device channel, teach mode and Ring-Login could be used by anyone who knew the (public) device ID.
- New option **"Ring-Login only with PIN"**: the card only opens if TagID (v2.2.0+) confirmed its PIN.
- Bolt Cards with a used-up daily budget are rejected with "Card limit reached" before an invoice is created.

**Other changes**

- Pydantic 2 / lnurl 2 (LNbits 1.7.0), payments from wallets on the same instance work again (local LNURL opt-in).
