"""
Paper Trading Engine — records simulated arbitrage executions for viability analysis.

Fee model  : 2% taker fee on each leg (verify with Polymarket CLOB docs)
Break-even : gross_cost < 1.00 / 1.02 ≈ 0.9804 USDC

Key metric : window duration in ms — tells us if execution is physically possible.
             < 200ms   → requires co-location
             200ms–2s  → tight but feasible with async HTTP
             > 2s      → comfortable execution window
"""
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Optional
import json
import time
from datetime import datetime, timezone
from pathlib import Path

TAKER_FEE_RATE = Decimal("0.02")
BREAKEVEN_GROSS = Decimal("1") / (Decimal("1") + TAKER_FEE_RATE)  # ≈ 0.9804


@dataclass
class OpportunityWindow:
    pair_id: str
    match_id: str
    strategy: str
    label: str
    gross_cost_open: Decimal
    net_profit_per_share: Decimal
    net_profit_total: Decimal
    target_shares: Decimal
    opened_at_iso: str
    _perf_open: float
    fee_rate: Decimal = field(default_factory=lambda: TAKER_FEE_RATE)
    ticks: int = 1
    peak_net_profit: Decimal = field(default_factory=lambda: Decimal("0"))
    duration_ms: Optional[float] = None
    vwaps_open: list[str] = field(default_factory=list)  # str representations for JSON


class PaperTrader:
    def __init__(
        self,
        target_shares: Decimal = Decimal("50"),
        log_file: str = "paper_trades.jsonl",
    ):
        self.target_shares = target_shares
        self.log_file = Path(log_file)
        self._active: dict[str, OpportunityWindow] = {}
        self._closed: list[OpportunityWindow] = []

        self.n_gross_signals = 0
        self.n_net_signals = 0
        self.cumulative_profit = Decimal("0")

        # Per-strategy counters for the summary
        self._by_strategy: dict[str, dict] = {}

        self._write_session_header()

    # ─── Main entry point ────────────────────────────────────────────────────

    def on_tick(
        self,
        pair_id: str,
        match_id: str,
        strategy: str,
        label: str,
        vwaps: list[Optional[float]],
        total_cost: Optional[float],
        fee_rate: Optional[Decimal] = None,
    ) -> None:
        if total_cost is None:
            if pair_id in self._active:
                self._close_window(pair_id, reason="no_liquidity")
            return

        gross_cost = Decimal(str(round(total_cost, 8)))
        effective_fee = fee_rate if fee_rate is not None else TAKER_FEE_RATE

        if gross_cost < Decimal("1"):
            self.n_gross_signals += 1
            self._inc_strategy(strategy, "gross")

        fee_cost = gross_cost * effective_fee
        net_profit_per_share = Decimal("1") - gross_cost - fee_cost

        if net_profit_per_share > 0:
            net_profit_total = net_profit_per_share * self.target_shares
            vwaps_str = [str(round(v, 6)) if v is not None else "None" for v in vwaps]

            if pair_id not in self._active:
                self.n_net_signals += 1
                self._inc_strategy(strategy, "net")
                window = OpportunityWindow(
                    pair_id=pair_id,
                    match_id=match_id,
                    strategy=strategy,
                    label=label,
                    gross_cost_open=gross_cost,
                    net_profit_per_share=net_profit_per_share,
                    net_profit_total=net_profit_total,
                    target_shares=self.target_shares,
                    opened_at_iso=datetime.now(timezone.utc).isoformat(),
                    _perf_open=time.perf_counter(),
                    fee_rate=effective_fee,
                    peak_net_profit=net_profit_per_share,
                    vwaps_open=vwaps_str,
                )
                self._active[pair_id] = window
                self.cumulative_profit += net_profit_total
                self._log_event("OPEN", window, gross_cost, fee_cost)
                self._print_open(window, gross_cost, fee_cost)
            else:
                window = self._active[pair_id]
                window.ticks += 1
                if net_profit_per_share > window.peak_net_profit:
                    window.peak_net_profit = net_profit_per_share
        else:
            if pair_id in self._active:
                self._close_window(pair_id, reason="spread_narrowed")

    # ─── Internal ─────────────────────────────────────────────────────────────

    def _close_window(self, pair_id: str, reason: str) -> None:
        window = self._active.pop(pair_id)
        now = time.perf_counter()
        window.duration_ms = (now - window._perf_open) * 1000
        self._closed.append(window)
        gross_cost = window.gross_cost_open
        self._log_event("CLOSE", window, gross_cost, gross_cost * window.fee_rate, reason=reason)
        print(
            f"  ⏱  [{window.strategy:15}] {window.match_id[:30]:<30} | "
            f"{window.duration_ms:.0f}ms | {window.ticks} ticks | {reason}"
        )

    def _log_event(
        self,
        event: str,
        window: OpportunityWindow,
        gross_cost: Decimal,
        fee_cost: Decimal,
        reason: str = "",
    ) -> None:
        record = {
            "event": event,
            "ts": datetime.now(timezone.utc).isoformat(),
            "pair_id": window.pair_id,
            "match_id": window.match_id,
            "strategy": window.strategy,
            "label": window.label,
            "gross_cost": str(gross_cost),
            "fee_cost": str(fee_cost),
            "breakeven_gross": str(BREAKEVEN_GROSS),
            "net_profit_per_share": str(window.net_profit_per_share),
            "net_profit_total": str(window.net_profit_total),
            "peak_net_profit": str(window.peak_net_profit),
            "target_shares": str(window.target_shares),
            "duration_ms": window.duration_ms,
            "ticks": window.ticks,
            "vwaps_at_open": window.vwaps_open,
            "reason": reason,
            "cumulative_profit": str(self.cumulative_profit),
        }
        with self.log_file.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def _print_open(
        self, window: OpportunityWindow, gross_cost: Decimal, fee_cost: Decimal
    ) -> None:
        strat_badge = {
            "btts_vs_o15": "BTTS/O1.5",
            "o15_vs_o25":  "O1.5/O2.5",
            "o25_vs_o35":  "O2.5/O3.5",
            "1x2_surebet": "1X2 SURE ",
        }.get(window.strategy, window.strategy[:9])

        print(f"\n  ┌─ 💰 PAPER #{self.n_net_signals} [{strat_badge}] ─────────────")
        print(f"  │  Match    : {window.match_id}")
        print(f"  │  Legs     : {' + '.join(window.vwaps_open)}")
        print(f"  │  Gross    : {gross_cost:.4f} USDC  (threshold {BREAKEVEN_GROSS:.4f})")
        fee_pct = float(window.fee_rate) * 100
        print(f"  │  Fees {fee_pct:.0f}%  : {fee_cost:.4f} USDC")
        print(f"  │  Net/shr  : +{window.net_profit_per_share:.4f} USDC")
        print(f"  │  Net tot  : +{window.net_profit_total:.4f} USDC  ({window.target_shares} sh)")
        print(f"  └─ Cumul.   : +{self.cumulative_profit:.4f} USDC\n")

    def _inc_strategy(self, strategy: str, key: str) -> None:
        if strategy not in self._by_strategy:
            self._by_strategy[strategy] = {"gross": 0, "net": 0}
        self._by_strategy[strategy][key] = self._by_strategy[strategy].get(key, 0) + 1

    def _write_session_header(self) -> None:
        header = {
            "event": "SESSION_START",
            "ts": datetime.now(timezone.utc).isoformat(),
            "target_shares": str(self.target_shares),
            "taker_fee_rate": str(TAKER_FEE_RATE),
            "breakeven_gross": str(BREAKEVEN_GROSS),
        }
        with self.log_file.open("a") as f:
            f.write(json.dumps(header) + "\n")

    # ─── End-of-session summary ───────────────────────────────────────────────

    def print_summary(self) -> None:
        for pid in list(self._active.keys()):
            self._close_window(pid, reason="session_end")

        durations = [w.duration_ms for w in self._closed if w.duration_ms is not None]
        avg_d = sum(durations) / len(durations) if durations else 0
        max_d = max(durations, default=0)
        min_d = min(durations, default=0)

        conv = (
            f"{self.n_net_signals / self.n_gross_signals * 100:.1f}%"
            if self.n_gross_signals > 0 else "n/a"
        )

        print("\n" + "=" * 62)
        print("  PAPER TRADING SESSION SUMMARY")
        print("=" * 62)
        print(f"  Gross signals (cost < 1.00)      : {self.n_gross_signals}")
        print(f"  Net profitable (after 2% fees)   : {self.n_net_signals}  ({conv})")
        print(f"  Avg / Min / Max window duration  : {avg_d:.0f}ms / {min_d:.0f}ms / {max_d:.0f}ms")
        print(f"  Simulated profit ({self.target_shares} sh/trade) : +{self.cumulative_profit:.4f} USDC")

        if self._by_strategy:
            print()
            print("  Per-strategy breakdown:")
            for strat, counts in sorted(self._by_strategy.items()):
                g, n = counts.get("gross", 0), counts.get("net", 0)
                ratio = f"{n/g*100:.0f}%" if g > 0 else " —"
                print(f"    {strat:20} gross={g:4}  net={n:4}  ({ratio})")

        print(f"\n  Log: {self.log_file}  →  run: python analyze.py {self.log_file}")
        print("=" * 62)

        if avg_d > 0:
            print()
            if avg_d < 200:
                print("  ⚠️  Very short windows (<200ms) — execution requires co-location")
            elif avg_d < 1500:
                print("  🟡  Moderate windows (200ms–1.5s) — feasible with async HTTP")
            else:
                print("  ✅  Wide windows (>1.5s) — comfortable execution margin")
        print()
