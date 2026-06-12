"""
Multi-leg execution engine.

Execution flow per opportunity:
  1. Dedup guard: skip if this pair_id is already being executed
  2. Shadow mode: log the order specs without hitting the CLOB
  3. For each leg (sequential, order matters for leg risk):
       a. Snapshot the current ask book for this token
       b. Build OrderSpec (worst_price, expected_vwap)
       c. Re-check profitability with actual order prices (price may have moved)
       d. Submit FOK order → wait for fill confirmation
       e. On failure → emergency_exit(already_filled_legs) and abort
  4. Log the full execution result

Shadow mode (SHADOW_MODE=true in .env):
  Signs orders but never calls post_order. Safe for integration testing.
  Set SHADOW_MODE=false only when live credentials are in place and tested.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .auth import build_client
from .order_builder import build_buy, OrderSpec
from .position_guard import PositionGuard, FilledLeg

log = logging.getLogger(__name__)

# Default taker fee rate — overridden per-pair by the actual feeRate from market data
TAKER_FEE_RATE = Decimal("0.02")
BREAKEVEN_GROSS = Decimal("1") / (Decimal("1") + TAKER_FEE_RATE)  # ≈ 0.9804

# How long to wait for a fill confirmation from the CLOB
FILL_TIMEOUT_S = 5.0

# Safety margin subtracted from breakeven before execution check
EXECUTION_SAFETY_MARGIN = Decimal("0.005")


@dataclass
class LegResult:
    token_id: str
    spec: OrderSpec
    filled: Optional[FilledLeg] = None
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.filled is not None


@dataclass
class ExecutionResult:
    success: bool
    pair_id: str
    strategy: str
    shadow: bool
    legs: list[LegResult] = field(default_factory=list)
    actual_gross: Optional[Decimal] = None
    actual_net_per_share: Optional[Decimal] = None
    error: Optional[str] = None
    duration_ms: Optional[float] = None


class ExecutionEngine:
    """
    Orchestrates multi-leg inclusion arbitrage execution.

    Thread safety: each pair_id can have at most one concurrent execution
    (enforced by _active set). The WebSocket event loop calls
    asyncio.create_task(engine.execute(...)) — the engine runs inside the
    same asyncio event loop, with sync CLOB SDK calls dispatched to a
    thread pool via asyncio.to_thread().
    """

    def __init__(self, shadow_mode: bool = True) -> None:
        self.shadow_mode = shadow_mode
        self._client = None
        self._guard: Optional[PositionGuard] = None
        self._active: set[str] = set()
        self._usdc_balance: Optional[Decimal] = None  # refreshed before each session

        if not shadow_mode:
            self._client = build_client(require_level2=True)
            self._guard = PositionGuard(self._client)
            log.info("ExecutionEngine — LIVE MODE (Level 2 auth)")
            self._check_balance_and_approval()
        else:
            log.info("ExecutionEngine — SHADOW MODE (no credentials required)")

    def _check_balance_and_approval(self) -> None:
        """
        Fetch USDC balance and verify the CTF Exchange allowance on startup.
        Logs a warning if balance is low; raises if allowance is zero
        (orders would revert on-chain).
        """
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            usdc = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            self._usdc_balance = Decimal(str(usdc.get("balance", 0)))
            log.info("USDC balance on Polygon: %s USDC", self._usdc_balance)

            if self._usdc_balance < Decimal("10"):
                log.warning(
                    "⚠️  Low USDC balance (%s). Consider depositing more before going live.",
                    self._usdc_balance,
                )

            # "allowances" is a dict of {contract: amount} — check the max across all CTF contracts
            allowances_dict = usdc.get("allowances", {})
            max_allowance = max((float(v) for v in allowances_dict.values()), default=0) if allowances_dict else 0
            allowance = Decimal(str(max_allowance))
            if allowance < Decimal("1"):
                raise RuntimeError(
                    f"USDC allowance is {allowance} — Polymarket CTF Exchange cannot spend "
                    "your USDC. Run setup_credentials.py to approve."
                )
            log.info("USDC allowance: %s USDC ✅", allowance)

        except RuntimeError:
            raise
        except Exception as e:
            log.warning("Could not verify USDC balance/allowance: %s", e)

    # ─── Public API ────────────────────────────────────────────────────────────

    async def execute(
        self,
        pair_id: str,
        strategy: str,
        tokens: list[str],
        books: dict,          # token_id → LiveOrderBook (live reference)
        target_shares: int,
        fee_rate: Optional[Decimal] = None,  # actual rate from market data; falls back to TAKER_FEE_RATE
    ) -> ExecutionResult:
        """
        Entry point called from the WebSocket event loop via asyncio.create_task().
        Returns immediately if the pair_id is already being executed.
        """
        if pair_id in self._active:
            log.debug("Skipping %s — execution already in progress", pair_id)
            return ExecutionResult(
                success=False, pair_id=pair_id, strategy=strategy,
                shadow=self.shadow_mode, error="already_active",
            )

        self._active.add(pair_id)
        t0 = time.perf_counter()
        try:
            result = await self._run(pair_id, strategy, tokens, books, target_shares, fee_rate)
        except Exception as exc:
            log.exception("Unexpected error executing %s", pair_id)
            result = ExecutionResult(
                success=False, pair_id=pair_id, strategy=strategy,
                shadow=self.shadow_mode, error=str(exc),
            )
        finally:
            self._active.discard(pair_id)
            result.duration_ms = (time.perf_counter() - t0) * 1000

        self._log_result(result)
        return result

    # ─── Internal ──────────────────────────────────────────────────────────────

    async def _run(
        self,
        pair_id: str,
        strategy: str,
        tokens: list[str],
        books: dict,
        target_shares: int,
        fee_rate: Optional[Decimal] = None,
    ) -> ExecutionResult:
        effective_fee = fee_rate if fee_rate is not None else TAKER_FEE_RATE
        effective_breakeven = Decimal("1") / (Decimal("1") + effective_fee)
        effective_max_gross = effective_breakeven - EXECUTION_SAFETY_MARGIN

        result = ExecutionResult(
            success=False, pair_id=pair_id, strategy=strategy, shadow=self.shadow_mode
        )

        # Step 0: Verify we have enough USDC for all legs (live mode only)
        if not self.shadow_mode:
            specs_preview = []
            for token_id in tokens:
                book = books.get(token_id)
                if book:
                    spec = build_buy(token_id, target_shares, dict(book.asks))
                    if spec:
                        specs_preview.append(spec)
            if specs_preview:
                required = sum(s.expected_cost for s in specs_preview)
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                usdc = await asyncio.to_thread(
                    self._client.get_balance_allowance,
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
                )
                available = Decimal(str(usdc.get("balance", 0)))
                if available < required:
                    result.error = f"insufficient_balance:{available:.2f}<{required:.2f}"
                    log.warning("Skipping %s — need %s USDC, have %s", pair_id, required, available)
                    return result

        # Step 1: Build all order specs from current book snapshots
        specs: list[OrderSpec] = []
        for token_id in tokens:
            book = books.get(token_id)
            if book is None:
                result.error = f"no_book:{token_id[:20]}"
                return result

            asks_snapshot = dict(book.asks)  # snapshot — avoid races with live updates
            spec = build_buy(token_id, target_shares, asks_snapshot)
            if spec is None:
                result.error = f"no_depth:{token_id[:20]}"
                return result

            specs.append(spec)

        # Step 2: Re-verify profitability at actual execution prices
        actual_gross = sum(s.expected_vwap for s in specs)
        if actual_gross >= effective_max_gross:
            result.error = f"price_moved:{actual_gross:.4f}>={effective_max_gross:.4f}"
            log.debug("Aborting %s — price moved to %.4f (threshold %.4f)",
                      pair_id, actual_gross, effective_max_gross)
            return result

        result.actual_gross = actual_gross

        # Step 3: Execute legs sequentially
        filled_legs: list[FilledLeg] = []
        leg_results: list[LegResult] = []

        for i, (token_id, spec) in enumerate(zip(tokens, specs)):
            leg_r = LegResult(token_id=token_id, spec=spec)

            if self.shadow_mode:
                leg_r.filled = FilledLeg(
                    token_id=token_id,
                    filled_size=spec.size,
                    avg_fill_price=spec.expected_vwap,
                    order_id="shadow",
                )
            else:
                filled = await self._submit_leg(spec, pair_id, leg_index=i)
                if filled is None:
                    leg_r.error = "fill_failed"
                    leg_results.append(leg_r)
                    # Emergency exit: sell back everything already filled
                    await self._abort(filled_legs, pair_id, books)
                    result.legs = leg_results
                    result.error = f"leg_{i}_failed"
                    return result
                leg_r.filled = filled
                filled_legs.append(filled)

            leg_results.append(leg_r)

        # All legs filled
        actual_fills = [lr.filled for lr in leg_results]
        actual_gross_filled = sum(f.avg_fill_price for f in actual_fills)  # type: ignore
        fee_per_share = actual_gross_filled * effective_fee
        net_per_share = Decimal("1") - actual_gross_filled - fee_per_share

        result.success = True
        result.legs = leg_results
        result.actual_gross = actual_gross_filled
        result.actual_net_per_share = net_per_share
        return result

    async def _submit_leg(
        self, spec: OrderSpec, pair_id: str, leg_index: int
    ) -> Optional[FilledLeg]:
        from py_clob_client.clob_types import OrderArgs, OrderType, Side

        for attempt in range(2):  # one retry on transient network error
            try:
                order_args = OrderArgs(
                    token_id=spec.token_id,
                    price=float(spec.limit_price),
                    size=float(spec.size),
                    side=Side.BUY,
                )
                # Both calls are synchronous in py-clob-client.
                # Run in thread pool to avoid blocking the event loop.
                signed = await asyncio.to_thread(
                    self._client.create_order, order_args
                )
                resp = await asyncio.to_thread(
                    self._client.post_order, signed, OrderType.FOK
                )

                # FOK: either fully matched or not at all
                status = resp.get("status", "")
                size_matched = float(resp.get("size_matched") or 0)

                if status == "matched" or size_matched > 0:
                    avg_price_raw = resp.get("average_price") or resp.get("price") or spec.limit_price
                    avg_price = Decimal(str(avg_price_raw))
                    return FilledLeg(
                        token_id=spec.token_id,
                        filled_size=int(size_matched) or spec.size,
                        avg_fill_price=avg_price,
                        order_id=resp.get("id", ""),
                    )

                log.warning(
                    "Leg %d FOK not filled | pair=%s | resp=%s",
                    leg_index, pair_id, resp,
                )
                return None  # FOK rejected, no retry (market moved)

            except Exception as exc:
                log.error(
                    "Leg %d submission error (attempt %d) | pair=%s | err=%s",
                    leg_index, attempt + 1, pair_id, exc,
                )
                if attempt == 0:
                    await asyncio.sleep(0.3)  # brief pause before retry
                    continue
                return None

        return None

    async def _abort(
        self,
        filled_legs: list[FilledLeg],
        pair_id: str,
        books: dict,
    ) -> None:
        if filled_legs and self._guard:
            await self._guard.emergency_exit(filled_legs, pair_id, books)

    def _log_result(self, result: ExecutionResult) -> None:
        prefix = "[SHADOW]" if result.shadow else "[LIVE]"
        if result.success:
            log.info(
                "%s ✅ %s | strategy=%s | gross=%.4f | net/sh=+%.4f | %dms",
                prefix,
                result.pair_id,
                result.strategy,
                result.actual_gross,
                result.actual_net_per_share,
                result.duration_ms,
            )
            for i, lr in enumerate(result.legs):
                if lr.filled:
                    log.info(
                        "  Leg %d | token=%s... | size=%d | fill_price=%.4f",
                        i,
                        lr.token_id[:20],
                        lr.filled.filled_size,
                        lr.filled.avg_fill_price,
                    )
        else:
            log.warning(
                "%s ❌ %s | strategy=%s | error=%s | %sms",
                prefix,
                result.pair_id,
                result.strategy,
                result.error,
                f"{result.duration_ms:.0f}" if result.duration_ms else "?",
            )
