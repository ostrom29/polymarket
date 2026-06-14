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
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .auth import build_client
from .order_builder import build_buy, OrderSpec
from .position_guard import PositionGuard, FilledLeg
import notify

log = logging.getLogger(__name__)

# Default taker fee rate — overridden per-pair by the actual feeRate from market data
TAKER_FEE_RATE = Decimal("0.02")
BREAKEVEN_GROSS = Decimal("1") / (Decimal("1") + TAKER_FEE_RATE)  # ≈ 0.9804

# How long to wait for a fill confirmation from the CLOB
FILL_TIMEOUT_S = 5.0

# Safety margin subtracted from breakeven before execution check
EXECUTION_SAFETY_MARGIN = Decimal("0.005")

# Position sizing: fraction of available balance to risk per opportunity.
# target_shares = clamp(floor(balance × fraction / gross), MIN, MAX)
CAPITAL_FRACTION = Decimal(os.environ.get("CAPITAL_FRACTION", "0.20"))
MIN_SHARES_PER_LEG = int(os.environ.get("MIN_SHARES_PER_LEG", "5"))
MAX_SHARES_PER_LEG = int(os.environ.get("MAX_SHARES_PER_LEG", "50"))

# Minimum net profit per execution. Filters out marginal trades where the
# emergency-exit loss (if a leg fails) would exceed the expected gain.
# We learned the hard way that emergency exits cost real money (spread bleed),
# so this floor stays conservative.
MIN_NET_PUSD = Decimal(os.environ.get("MIN_NET_PUSD", "0.10"))

# Sanity floor on the detected gross. A legitimate inclusion arb sums to just
# below 1.00; anything far under means the book is dead/thin (losing outcome
# parked at 0.001) and the "arb" is a phantom we cannot fill. Skip before the API.
SANITY_MIN_GROSS = Decimal(os.environ.get("SANITY_MIN_GROSS", "0.85"))

# Stop hammering a pair that keeps failing to execute (partial fills = real money
# lost to spread). After this many partial-fill failures, blacklist it this session.
MAX_PAIR_FAILURES = int(os.environ.get("MAX_PAIR_FAILURES", "2"))

# pUSD has 6 decimal places; the CLOB API returns raw on-chain units.
_PUSD_DECIMALS = Decimal("1_000_000")


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
    title: str = ""
    game_start_time: str = ""


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
        self._pair_failures: dict[str, int] = {}  # pair_id → consecutive partial-fill count

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
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            usdc = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            # API returns raw on-chain units (6 decimal places for pUSD)
            self._usdc_balance = Decimal(str(usdc.get("balance", 0))) / _PUSD_DECIMALS
            log.info("pUSD balance: %.4f pUSD", self._usdc_balance)

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
        estimated_gross: float = 1.0,  # sum of per-leg VWAPs (cost per 1-share set)
        fee_rate: Optional[Decimal] = None,  # actual rate from market data; falls back to TAKER_FEE_RATE
        title: str = "",
        game_start_time: str = "",
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

        if self._pair_failures.get(pair_id, 0) >= MAX_PAIR_FAILURES:
            log.debug("Skipping %s — blacklisted after %d partial-fill failures",
                      pair_id, self._pair_failures[pair_id])
            return ExecutionResult(
                success=False, pair_id=pair_id, strategy=strategy,
                shadow=self.shadow_mode, error="blacklisted",
            )

        self._active.add(pair_id)
        t0 = time.perf_counter()
        try:
            result = await self._run(
                pair_id, strategy, tokens, books, estimated_gross, fee_rate
            )
            result.title = title
            result.game_start_time = game_start_time
        except Exception as exc:
            log.exception("Unexpected error executing %s", pair_id)
            result = ExecutionResult(
                success=False, pair_id=pair_id, strategy=strategy,
                shadow=self.shadow_mode, error=str(exc),
            )
        finally:
            self._active.discard(pair_id)
            result.duration_ms = (time.perf_counter() - t0) * 1000

        # Track partial-fill failures (real money moved to spread) and blacklist
        # repeat offenders so we stop hammering a structurally broken pair.
        if result.error and result.error.startswith("partial_fill"):
            self._pair_failures[pair_id] = self._pair_failures.get(pair_id, 0) + 1
            if self._pair_failures[pair_id] >= MAX_PAIR_FAILURES and not self.shadow_mode:
                notify.send(
                    f"⛔ Paire blacklistée (session) après {MAX_PAIR_FAILURES} échecs partiels\n{pair_id}"
                )
        elif result.success:
            self._pair_failures.pop(pair_id, None)

        self._log_result(result)
        return result

    # ─── Internal ──────────────────────────────────────────────────────────────

    async def _run(
        self,
        pair_id: str,
        strategy: str,
        tokens: list[str],
        books: dict,
        estimated_gross: float = 1.0,
        fee_rate: Optional[Decimal] = None,
    ) -> ExecutionResult:
        effective_fee = fee_rate if fee_rate is not None else TAKER_FEE_RATE
        effective_breakeven = Decimal("1") / (Decimal("1") + effective_fee)
        effective_max_gross = effective_breakeven - EXECUTION_SAFETY_MARGIN

        result = ExecutionResult(
            success=False, pair_id=pair_id, strategy=strategy, shadow=self.shadow_mode
        )

        # Sanity gate: reject implausibly-low gross (dead/thin-book phantom arb)
        # before spending an API round-trip on it. A real inclusion arb sums to
        # just below 1.00 — a gross of 0.60 means the book is broken/resolving.
        gross_est = Decimal(str(estimated_gross)) if estimated_gross > 0 else Decimal("1")
        if gross_est < SANITY_MIN_GROSS:
            result.error = f"implausible_gross:{gross_est:.4f}<{SANITY_MIN_GROSS}"
            log.warning("Skipping %s — implausible gross %.4f (dead/thin book?)",
                        pair_id, gross_est)
            return result

        # Step 0: Fetch balance and compute dynamic target_shares
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

        if not self.shadow_mode:
            usdc = await asyncio.to_thread(
                self._client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
            # API returns raw on-chain units (6 decimals for pUSD)
            available_pUSD = Decimal(str(usdc.get("balance", 0))) / _PUSD_DECIMALS
        else:
            available_pUSD = self._usdc_balance or Decimal("48")  # shadow: use startup value

        shares_from_fraction = int(available_pUSD * CAPITAL_FRACTION / gross_est)
        target_shares = max(MIN_SHARES_PER_LEG, min(MAX_SHARES_PER_LEG, shares_from_fraction))

        min_cost = Decimal(str(MIN_SHARES_PER_LEG)) * gross_est
        if available_pUSD < min_cost:
            result.error = f"insufficient_balance:{available_pUSD:.2f}<{min_cost:.2f}"
            log.warning("Skipping %s — need %.2f pUSD for min %d shares, have %.2f",
                        pair_id, min_cost, MIN_SHARES_PER_LEG, available_pUSD)
            return result

        estimated_net_per_share = Decimal("1") - gross_est * (Decimal("1") + effective_fee)
        estimated_net_total = estimated_net_per_share * Decimal(str(target_shares))
        if estimated_net_total < MIN_NET_PUSD:
            result.error = f"below_min_profit:{estimated_net_total:.3f}<{MIN_NET_PUSD}"
            log.debug("Skipping %s — net %.3f pUSD < min %.3f (gross=%.4f, %d shares)",
                      pair_id, estimated_net_total, MIN_NET_PUSD, estimated_gross, target_shares)
            return result

        log.info("Executing %s — gross=%.4f | %d shares/leg | est. net=+%.3f pUSD",
                 pair_id, estimated_gross, target_shares, estimated_net_total)

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

        # Step 3: Execute all legs in parallel — same book snapshot for all legs,
        # ~3× faster than sequential (max latency instead of sum of latencies).
        if self.shadow_mode:
            leg_results = [
                LegResult(token_id=tid, spec=spec, filled=FilledLeg(
                    token_id=tid, filled_size=spec.size,
                    avg_fill_price=spec.expected_vwap, order_id="shadow",
                ))
                for tid, spec in zip(tokens, specs)
            ]
            filled_legs = [lr.filled for lr in leg_results]  # type: ignore[misc]
        else:
            outcomes = await asyncio.gather(
                *[self._submit_leg(spec, pair_id, i) for i, spec in enumerate(specs)],
                return_exceptions=True,
            )
            leg_results = []
            filled_legs = []
            for tid, spec, outcome in zip(tokens, specs, outcomes):
                leg_r = LegResult(token_id=tid, spec=spec)
                if isinstance(outcome, FilledLeg):
                    leg_r.filled = outcome
                    filled_legs.append(outcome)
                else:
                    leg_r.error = str(outcome) if isinstance(outcome, Exception) else "fill_failed"
                leg_results.append(leg_r)

            if len(filled_legs) < len(specs):
                # At least one leg didn't fill — sell back what did
                await self._abort(filled_legs, pair_id, books)
                n_ok = len(filled_legs)
                result.legs = leg_results
                result.error = f"partial_fill:{n_ok}/{len(specs)}_legs"
                return result

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
        from py_clob_client_v2.clob_types import OrderArgs, OrderType
        from py_clob_client_v2 import Side

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

        if result.shadow:
            self._write_shadow_record(result)
        elif result.success:
            self._write_position(result)

        if result.success:
            notify.trades_fired += 1
            mode = "🔵 SHADOW" if result.shadow else "🟢 LIVE"
            shares_per_leg = result.legs[0].filled.filled_size if result.legs and result.legs[0].filled else 0
            total_cost = float(result.actual_gross) * shares_per_leg
            net_total = float(result.actual_net_per_share) * shares_per_leg

            # Format match kick-off time (strip date, keep HH:MM UTC)
            kickoff = ""
            if result.game_start_time:
                try:
                    from datetime import datetime, timezone
                    gst_str = result.game_start_time
                    gst = datetime.fromisoformat(gst_str.replace("+00", "+00:00"))
                    kickoff = gst.astimezone(timezone.utc).strftime("%H:%M UTC")
                except Exception:
                    kickoff = result.game_start_time

            match_line = result.title or result.pair_id.split("::")[0]
            time_line = f" — {kickoff}" if kickoff else ""

            notify.send(
                f"{mode} Trade exécuté ✅\n"
                f"Match : {match_line}{time_line}\n"
                f"Stratégie : {result.strategy}\n"
                f"Parts : {shares_per_leg} × {len(result.legs)} legs\n"
                f"Coût total : {total_cost:.2f} pUSD\n"
                f"Gross : {float(result.actual_gross):.4f}\n"
                f"Net/part : +{float(result.actual_net_per_share):.4f} pUSD\n"
                f"Net total : +{net_total:.3f} pUSD\n"
                f"Durée : {result.duration_ms:.0f}ms"
            )

        elif result.error and result.error.startswith("partial_fill") and not result.shadow:
            match_line = result.title or result.pair_id.split("::")[0]
            notify.send(
                f"⚠️ Exécution partielle — hedge incomplet\n"
                f"Match : {match_line}\n"
                f"Stratégie : {result.strategy}\n"
                f"Détail : {result.error}\n"
                f"→ Emergency exit déclenché (revente des legs remplis)"
            )

    def _write_position(self, result: ExecutionResult) -> None:
        """Append a live trade to positions.jsonl for P&L tracking."""
        shares_per_leg = result.legs[0].filled.filled_size if result.legs and result.legs[0].filled else 0
        cost_pusd = float(result.actual_gross) * shares_per_leg
        est_net_pusd = float(result.actual_net_per_share) * shares_per_leg

        # Heuristic: oracle typically resolves within 6h of kick-off
        resolved_after = ""
        if result.game_start_time:
            try:
                gst = datetime.fromisoformat(
                    result.game_start_time.replace("+00", "+00:00")
                )
                resolved_after = (gst + timedelta(hours=6)).isoformat()
            except Exception:
                pass

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "pair_id": result.pair_id,
            "title": result.title or result.pair_id.split("::")[0],
            "game_start_time": result.game_start_time,
            "strategy": result.strategy,
            "shares_per_leg": shares_per_leg,
            "n_legs": len(result.legs),
            "cost_pusd": round(cost_pusd, 4),
            "est_net_pusd": round(est_net_pusd, 4),
            "resolved_after": resolved_after,
        }
        try:
            with Path("positions.jsonl").open("a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            log.warning("Could not write position record: %s", e)

    def _write_shadow_record(self, result: ExecutionResult) -> None:
        legs_data = []
        for lr in result.legs:
            leg = {
                "token_id": lr.token_id,
                "limit_price": str(lr.spec.limit_price),
                "expected_vwap": str(lr.spec.expected_vwap),
                "expected_cost": str(lr.spec.expected_cost),
                "size": lr.spec.size,
            }
            if lr.filled:
                leg["fill_price"] = str(lr.filled.avg_fill_price)
            if lr.error:
                leg["error"] = lr.error
            legs_data.append(leg)

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "pair_id": result.pair_id,
            "strategy": result.strategy,
            "success": result.success,
            "error": result.error,
            "gross": str(result.actual_gross) if result.actual_gross else None,
            "net_per_share": str(result.actual_net_per_share) if result.actual_net_per_share else None,
            "duration_ms": round(result.duration_ms, 2) if result.duration_ms else None,
            "legs": legs_data,
        }

        try:
            with Path("shadow_trades.jsonl").open("a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            log.warning("Could not write shadow record: %s", e)
