import asyncio
import websockets
import json
import os
from decimal import Decimal

from arbitrage import PricingEngine, NLegDetector
from paper_trader import PaperTrader

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Token → set of pair_ids that include it (reverse index for fast lookup on price update)
TokenIndex = dict[str, set[str]]


class LiveOrderBook:
    def __init__(self, asset_id: str):
        self.asset_id = asset_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def update_snapshot(self, event: dict) -> None:
        self.bids = {float(b["price"]): float(b["size"]) for b in event.get("bids", [])}
        self.asks = {float(a["price"]): float(a["size"]) for a in event.get("asks", [])}

    def update_delta(self, change: dict) -> None:
        side = (change.get("side") or "").upper()
        price = float(change.get("price", 0))
        size = float(change.get("size", 0))
        target = self.bids if side == "BUY" else self.asks
        if size == 0:
            target.pop(price, None)
        else:
            target[price] = size


class OrderBookManager:
    def __init__(self, paper_trader: PaperTrader | None = None):
        self.books: dict[str, LiveOrderBook] = {}
        self.pricing_engine = PricingEngine(target_shares=50.0)
        self.detector = NLegDetector(self.pricing_engine)
        self.paper_trader = paper_trader
        self._token_index: TokenIndex = {}  # token_id → {pair_id, ...}

    def register_pair(self, pair: dict) -> None:
        """Add a pair to the detector and build the reverse token index."""
        self.detector.add_pair(
            pair_id=pair["pair_id"],
            tokens=pair["tokens"],
            label=pair["label"],
            strategy=pair["strategy"],
        )
        for token in pair["tokens"]:
            self._token_index.setdefault(token, set()).add(pair["pair_id"])

    def _evaluate_pairs(self, asset_id: str) -> None:
        for pair_id in self._token_index.get(asset_id, set()):
            vwaps, total_cost = self.detector.check(pair_id, self.books)
            if self.paper_trader:
                pair = self.detector.pairs[pair_id]
                self.paper_trader.on_tick(
                    pair_id=pair_id,
                    match_id=pair_id.split("::")[0],
                    strategy=pair["strategy"],
                    label=pair["label"],
                    vwaps=vwaps,
                    total_cost=total_cost,
                )

    def process_message(self, message: str) -> None:
        try:
            data = json.loads(message)
        except Exception:
            return

        # Book snapshots arrive as a JSON array, one event per subscribed token
        if isinstance(data, list):
            for event in data:
                asset_id = event.get("asset_id")
                if not asset_id or asset_id not in self.books:
                    continue
                if event.get("event_type") == "book":
                    self.books[asset_id].update_snapshot(event)
                    self._evaluate_pairs(asset_id)
            return

        # Price-change updates arrive as a dict:
        # {"event_type": "price_change", "price_changes": [{asset_id, price, size, side}, ...]}
        if isinstance(data, dict) and data.get("event_type") == "price_change":
            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id")
                if not asset_id or asset_id not in self.books:
                    continue
                self.books[asset_id].update_delta(change)
                self._evaluate_pairs(asset_id)


async def stream_market_data(token_ids: list[str], manager: OrderBookManager) -> None:
    subscribe_msg = {
        "type": "subscribe",
        "channels": ["market"],
        "assets_ids": token_ids,
    }

    while True:
        print(f"\n🔌 Connecting to Polymarket CLOB Stream ({len(token_ids)} tokens)...")
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(subscribe_msg))
                print("✅ Subscribed. Awaiting stream...\n")

                for t_id in token_ids:
                    if t_id not in manager.books:
                        manager.books[t_id] = LiveOrderBook(t_id)

                while True:
                    message = await ws.recv()
                    manager.process_message(message)

        except websockets.exceptions.ConnectionClosed:
            print("❌ Connection closed. Reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"⚠️  Network error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


def _load_pairs(preferred: str = "wc_pairs.json", fallback: str = "friendlies_pairs.json"):
    """
    Load pairs config from wc_pairs.json (new format) or friendlies_pairs.json (legacy).
    Returns (pairs_list, tokens_list).
    """
    # Try new format first
    for filename in (preferred, fallback):
        if not os.path.exists(filename):
            continue
        with open(filename, "r") as f:
            config = json.load(f)

        # New format: pairs have a 'pair_id' and 'tokens' list
        if config.get("pairs") and "tokens" in config["pairs"][0]:
            tokens = config.get("all_tokens", [])
            print(f"📂 Loaded {len(config['pairs'])} pairs from {filename} (new format)")
            return config["pairs"], tokens

        # Legacy format: pairs have 'strict_no' / 'broad_yes'
        legacy_pairs = []
        for p in config.get("pairs", []):
            legacy_pairs.append({
                "pair_id": f"{p['match_id']}::btts_vs_o15",
                "match_id": p["match_id"],
                "strategy": "btts_vs_o15",
                "tokens": [p["strict_no"], p["broad_yes"]],
                "label": p.get("label", "NO BTTS + YES O1.5"),
            })
        tokens = config.get("all_tokens", [])
        print(f"📂 Loaded {len(legacy_pairs)} pairs from {filename} (legacy format)")
        return legacy_pairs, tokens

    return [], []


if __name__ == "__main__":
    pairs, tokens = _load_pairs()

    if not pairs:
        print("[!] No pairs file found. Run getotken.py first.")
        exit(1)

    # Strategy breakdown
    by_strat: dict[str, int] = {}
    for p in pairs:
        s = p.get("strategy", "unknown")
        by_strat[s] = by_strat.get(s, 0) + 1
    for s, n in sorted(by_strat.items()):
        print(f"   {s:20} : {n} pairs")

    paper = PaperTrader(target_shares=Decimal("50"), log_file="paper_trades.jsonl")
    manager = OrderBookManager(paper_trader=paper)

    for p in pairs:
        manager.register_pair(p)

    print(f"\n📋 Paper trading ON — {len(pairs)} pairs | {len(tokens)} tokens")
    print(f"📁 Log: paper_trades.jsonl\n")

    try:
        asyncio.run(stream_market_data(tokens, manager))
    except KeyboardInterrupt:
        print("\n⏹  Stream stopped.")
        paper.print_summary()
