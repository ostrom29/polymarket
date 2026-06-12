"""
Order construction — pure financial logic, no I/O.

Converts a live order book + target size into a concrete buy order spec.

Key concept:
  worst_price = the highest ask level we must accept to fill our full quantity.
  We set limit = worst_price to guarantee the order fills in one shot (taker).
  Anything cheaper than worst_price on the book will fill at its own price,
  giving us better avg fill than worst_price.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_UP

TICK_SIZE = Decimal("0.01")
MAX_PRICE = Decimal("0.99")


@dataclass(frozen=True)
class OrderSpec:
    token_id: str
    side: str           # "BUY"
    size: int           # number of shares (integer contracts)
    limit_price: Decimal  # highest price we accept — guarantees fill
    expected_vwap: Decimal  # expected avg fill cost per share
    expected_cost: Decimal  # limit_price × size (worst-case total spend)


def build_buy(
    token_id: str,
    target_shares: int,
    asks: dict[float, float],
) -> OrderSpec | None:
    """
    Build a BUY OrderSpec from the current ask side of a live order book.

    Returns None if the book doesn't have enough depth to fill target_shares.
    The caller should treat None as "no trade" (fail-safe).
    """
    if not asks or target_shares <= 0:
        return None

    levels = sorted(asks.items())  # [(price_float, size_float), ...] ascending

    total_cost = Decimal("0")
    accumulated = Decimal("0")
    worst_price: Decimal | None = None
    target = Decimal(str(target_shares))

    for price_f, size_f in levels:
        price = Decimal(str(price_f))
        size = Decimal(str(size_f))
        worst_price = price

        needed = target - accumulated
        if size >= needed:
            total_cost += price * needed
            accumulated = target
            break
        else:
            total_cost += price * size
            accumulated += size

    if accumulated < target:
        return None  # insufficient depth

    assert worst_price is not None
    expected_vwap = (total_cost / target).quantize(Decimal("0.000001"))

    # Limit price = worst_price exactly.
    # The CLOB will fill us at each maker's actual price up to this cap,
    # so our avg fill will be ≤ worst_price. No tick buffer needed:
    # we already verified this exact price level exists in the book.
    limit_price = min(worst_price, MAX_PRICE)

    return OrderSpec(
        token_id=token_id,
        side="BUY",
        size=target_shares,
        limit_price=limit_price,
        expected_vwap=expected_vwap,
        expected_cost=limit_price * target,
    )
