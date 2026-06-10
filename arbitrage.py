"""
Arbitrage detection engine — supports n-leg inclusion arbitrage.

Mathematical invariant for ALL strategies:
  sum(ask_VWAP for each leg) < 1.00  →  guaranteed payout ≥ 1.00 per share

Strategy catalogue:
  btts_vs_o15  | NO BTTS  + YES O1.5  | goals ≤ 1 or goals ≥ 2  covers all
  o15_vs_o25   | YES O1.5 + NO O2.5   | goals ≤ 1 or goals = 2 (double) or goals ≥ 3
  o25_vs_o35   | YES O2.5 + NO O3.5   | same logic shifted one goal up
  1x2_surebet  | YES Home + YES Draw + YES Away | exhaustive partition, always pays 1.00
"""


class PricingEngine:
    def __init__(self, target_shares: float = 50.0):
        self.target_shares = target_shares

    def get_vwap_ask(self, asks_dict: dict) -> float | None:
        if not asks_dict:
            return None
        total_cost = 0.0
        shares_accumulated = 0.0
        for price, size in sorted(asks_dict.items()):
            shares_needed = self.target_shares - shares_accumulated
            if size >= shares_needed:
                total_cost += price * shares_needed
                shares_accumulated += shares_needed
                break
            else:
                total_cost += price * size
                shares_accumulated += size
        if shares_accumulated < self.target_shares:
            return None
        return total_cost / self.target_shares


# Only print tracker lines for pairs approaching the threshold (reduces noise)
_TRACKER_PRINT_THRESHOLD = 1.05


class NLegDetector:
    """
    Unified n-leg arbitrage detector.
    Each pair is a list of token IDs (ask prices summed against 1.00).
    """

    def __init__(self, pricing_engine: PricingEngine):
        self.pricing = pricing_engine
        self.pairs: dict[str, dict] = {}

    def add_pair(
        self,
        pair_id: str,
        tokens: list[str],
        label: str,
        strategy: str,
    ) -> None:
        self.pairs[pair_id] = {
            "tokens": tokens,
            "label": label,
            "strategy": strategy,
        }

    def check(
        self, pair_id: str, books: dict
    ) -> tuple[list[float | None], float | None]:
        """
        Returns (vwaps, total_cost).
        vwaps mirrors the tokens list; None means insufficient liquidity on that leg.
        total_cost is None if any leg is illiquid.
        """
        pair = self.pairs.get(pair_id)
        if not pair:
            return [], None

        vwaps: list[float | None] = [
            self.pricing.get_vwap_ask(books[t].asks) if t in books else None
            for t in pair["tokens"]
        ]

        if any(v is None for v in vwaps):
            return vwaps, None

        total_cost: float = sum(vwaps)  # type: ignore[arg-type]

        if total_cost < _TRACKER_PRINT_THRESHOLD:
            strat = pair["strategy"]
            mid = pair_id[:28]
            marker = "🔔" if total_cost < 1.00 else "📊"
            print(f"{marker} [{strat:15}] {mid:<28} | {total_cost:.4f} USDC")

            if total_cost < 1.00:
                gross_profit = 1.00 - total_cost
                leg_str = " + ".join(f"{v:.4f}" for v in vwaps)  # type: ignore
                print(f"   └─ [{pair['label']}] legs=[{leg_str}]  Δ=+{gross_profit:.4f}/share")

        return vwaps, total_cost
