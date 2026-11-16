"""Price of a switch and the accepted payment range.

Used by the LNURL-pay endpoints, the NFC payment path and the settlement
listener, so all three agree on what "paid in full" means.
"""

from lnbits.utils.exchange_rates import fiat_amount_as_satoshis

from .models import Switch, ZapBox

# Fiat prices are converted at request time. The rate can move between the
# LNURL params request, the callback and the settlement, so fiat amounts get
# a small tolerance. Sat prices are exact.
FIAT_TOLERANCE = 0.98
# Variable-price switches accept up to this multiple of the base price.
VARIABLE_MAX_FACTOR = 100


async def switch_price_msat(zapbox: ZapBox, switch: Switch) -> int:
    if zapbox.currency == "sat":
        return int(float(switch.amount) * 1000)
    sats = await fiat_amount_as_satoshis(float(switch.amount), zapbox.currency)
    return int(sats * 1000)


def max_sendable_msat(price_msat: int, switch: Switch) -> int:
    return price_msat * VARIABLE_MAX_FACTOR if switch.variable else price_msat


def tolerance(zapbox: ZapBox) -> float:
    return 1.0 if zapbox.currency == "sat" else FIAT_TOLERANCE


def amount_accepted(
    zapbox: ZapBox, switch: Switch, price_msat: int, amount_msat: int
) -> bool:
    """True if amount_msat is within [price, max_sendable] (fiat: ±2 %)."""
    tol = tolerance(zapbox)
    low = int(price_msat * tol)
    high = int(max_sendable_msat(price_msat, switch) / tol)
    return low <= amount_msat <= high


def paid_enough(zapbox: ZapBox, price_msat: int, paid_msat: int) -> bool:
    """Lower bound only (settlement side)."""
    return paid_msat >= int(price_msat * tolerance(zapbox))
