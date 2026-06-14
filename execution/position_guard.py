"""
Position Guard — emergency exit when a multi-leg execution fails midway.

If leg N fails after legs 0..N-1 were filled, we're exposed:
we hold tokens on the filled legs with no hedge on the others.
This module immediately places market SELL orders for all filled legs.

A market SELL is implemented as a limit SELL at best_bid (or 0.01 as absolute floor).
If the sell fails, we log CRITICAL and require manual intervention.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal

log = logging.getLogger(__name__)


@dataclass
class FilledLeg:
    token_id: str
    filled_size: int
    avg_fill_price: Decimal
    order_id: str


class PositionGuard:
    def __init__(self, client) -> None:
        self._client = client
        self.stuck_count = 0  # emergency sells that failed → unhedged positions held

    async def emergency_exit(
        self,
        filled_legs: list[FilledLeg],
        pair_id: str,
        books: dict,  # token_id → LiveOrderBook (for best_bid lookup)
    ) -> None:
        """
        Sell back all filled legs as fast as possible.
        Runs all sells in parallel (asyncio.gather).
        """
        if not filled_legs:
            return

        log.error(
            "🚨 EMERGENCY EXIT | pair=%s | selling %d filled legs",
            pair_id,
            len(filled_legs),
        )

        tasks = [
            self._sell_leg(leg, books)
            for leg in filled_legs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for leg, result in zip(filled_legs, results):
            if isinstance(result, Exception):
                # This is the worst possible failure — stuck position with no exit.
                self.stuck_count += 1
                log.critical(
                    "❌ EMERGENCY SELL FAILED — MANUAL INTERVENTION REQUIRED\n"
                    "  token   : %s\n"
                    "  size    : %d shares\n"
                    "  bought @: %s USDC/sh\n"
                    "  error   : %s",
                    leg.token_id,
                    leg.filled_size,
                    leg.avg_fill_price,
                    result,
                )

    async def _sell_leg(self, leg: FilledLeg, books: dict) -> None:
        from py_clob_client_v2.clob_types import OrderArgs, OrderType
        from py_clob_client_v2 import Side

        # Cross a few ticks below best bid so the SELL fills as a taker immediately,
        # even if the bid moved since the snapshot. Escaping an unhedged position
        # fast is worth a little slippage; resting at the exact bid is how legs got
        # stuck before (order sat 'live', never matched).
        book = books.get(leg.token_id)
        best_bid = max(book.bids.keys()) if (book and book.bids) else None
        sell_price = max(0.01, round(float(best_bid) - 0.03, 2)) if best_bid else 0.01

        order_args = OrderArgs(
            token_id=leg.token_id,
            price=sell_price,
            size=float(leg.filled_size),
            side=Side.SELL,
        )
        signed = await asyncio.to_thread(self._client.create_order, order_args)
        resp = await asyncio.to_thread(self._client.post_order, signed, OrderType.GTC)

        log.warning(
            "  ↳ emergency sell submitted | token=%s... | size=%d | price=%.4f | resp=%s",
            leg.token_id[:20],
            leg.filled_size,
            sell_price,
            resp,
        )
