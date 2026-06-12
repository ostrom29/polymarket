import asyncio
import websockets
import json
import logging
import os
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from arbitrage import PricingEngine, NLegDetector
from paper_trader import PaperTrader

log = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Only watch matches starting within this window (or already ongoing).
# 12h covers a full CdM match day without locking capital on tomorrow's games.
TRADE_WINDOW_HOURS = 12
TARGET_SHARES = 10

# Re-scan Polymarket every N hours for new markets / closed matches
PAIR_REFRESH_HOURS = 4

# Signals the stream to reconnect with freshly fetched pairs
_refresh_event = asyncio.Event()

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
    def __init__(
        self,
        paper_trader: PaperTrader | None = None,
        executor=None,  # execution.executor.ExecutionEngine | None
    ):
        self.books: dict[str, LiveOrderBook] = {}
        self.pricing_engine = PricingEngine(target_shares=float(TARGET_SHARES))
        self.detector = NLegDetector(self.pricing_engine)
        self.paper_trader = paper_trader
        self.executor = executor
        self._token_index: TokenIndex = {}  # token_id → {pair_id, ...}
        self._fee_rates: dict[str, Decimal] = {}  # pair_id → actual fee rate from market data

    def register_pair(self, pair: dict) -> None:
        """Add a pair to the detector and build the reverse token index."""
        self.detector.add_pair(
            pair_id=pair["pair_id"],
            tokens=pair["tokens"],
            label=pair["label"],
            strategy=pair["strategy"],
        )
        self._fee_rates[pair["pair_id"]] = Decimal(str(pair.get("fee_rate", "0.02")))
        for token in pair["tokens"]:
            self._token_index.setdefault(token, set()).add(pair["pair_id"])

    def _evaluate_pairs(self, asset_id: str) -> None:
        for pair_id in self._token_index.get(asset_id, set()):
            vwaps, total_cost = self.detector.check(pair_id, self.books)
            pair = self.detector.pairs[pair_id]

            fee_rate = self._fee_rates.get(pair_id, Decimal("0.02"))
            breakeven = Decimal("1") / (Decimal("1") + fee_rate)

            if self.paper_trader:
                self.paper_trader.on_tick(
                    pair_id=pair_id,
                    match_id=pair_id.split("::")[0],
                    strategy=pair["strategy"],
                    label=pair["label"],
                    vwaps=vwaps,
                    total_cost=total_cost,
                    fee_rate=fee_rate,
                )

            # Fire execution only when the signal is net-profitable after actual fees
            if (
                self.executor is not None
                and total_cost is not None
                and Decimal(str(round(total_cost, 8))) < breakeven
            ):
                asyncio.create_task(
                    self.executor.execute(
                        pair_id=pair_id,
                        strategy=pair["strategy"],
                        tokens=pair["tokens"],
                        books=self.books,
                        target_shares=TARGET_SHARES,
                        fee_rate=fee_rate,
                    )
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


async def _refresh_loop(pairs_file: str = "wc_pairs.json") -> None:
    """
    Background task: re-fetch market pairs every PAIR_REFRESH_HOURS hours.
    Sets _refresh_event to trigger a WebSocket reconnect with updated pairs.
    """
    while True:
        await asyncio.sleep(PAIR_REFRESH_HOURS * 3600)
        log.info("Scheduled pair refresh — re-scanning Polymarket (every %dh)...", PAIR_REFRESH_HOURS)
        try:
            from getotken import fetch_all_pairs
            await asyncio.to_thread(fetch_all_pairs, pairs_file)
            _refresh_event.set()
            log.info("Pairs refreshed — stream will reconnect with updated tokens")
        except Exception as e:
            log.warning("Pair refresh failed: %s", e)


async def stream_market_data(
    executor,
    paper_trader: PaperTrader | None,
    pairs_file: str = "wc_pairs.json",
) -> None:
    """
    Main stream loop. Reloads pairs from disk on every reconnect so that a scheduled
    pair refresh (via _refresh_loop) automatically takes effect on the next connection.
    """
    while True:
        _refresh_event.clear()
        pairs, tokens = _load_pairs(pairs_file)

        if not tokens:
            log.warning("No tokens to subscribe to — retrying in 30s")
            await asyncio.sleep(30)
            continue

        # Fresh manager per connection so pair/book state is always consistent
        manager = OrderBookManager(paper_trader=paper_trader, executor=executor)
        for p in pairs:
            manager.register_pair(p)

        subscribe_msg = {
            "type": "subscribe",
            "channels": ["market"],
            "assets_ids": tokens,
        }

        log.info("Connecting to CLOB Stream (%d tokens, %d pairs)...", len(tokens), len(pairs))
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps(subscribe_msg))
                log.info("Subscribed. Awaiting stream...")

                for t_id in tokens:
                    manager.books[t_id] = LiveOrderBook(t_id)

                while not _refresh_event.is_set():
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        manager.process_message(message)
                    except asyncio.TimeoutError:
                        pass  # check _refresh_event and loop

                log.info("Pair refresh detected — reconnecting with updated pairs")

        except websockets.exceptions.ConnectionClosed:
            log.warning("Connection closed — reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            log.warning("Network error: %s — reconnecting in 5s...", e)
            await asyncio.sleep(5)


def _is_in_trade_window(game_start_time: str | None) -> bool:
    """Return True if the match is ongoing or starts within TRADE_WINDOW_HOURS."""
    if not game_start_time:
        return True  # no time info → don't filter
    try:
        gst = datetime.fromisoformat(game_start_time.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        # Keep if: match started less than 2h ago (ongoing) OR starts within window
        return (now - timedelta(hours=2)) <= gst <= (now + timedelta(hours=TRADE_WINDOW_HOURS))
    except Exception:
        return True  # can't parse → don't filter


def _load_pairs(preferred: str = "wc_pairs.json", fallback: str = "friendlies_pairs.json"):
    """
    Load pairs config, apply temporal filter (TRADE_WINDOW_HOURS).
    Returns (pairs_list, tokens_list).
    """
    for filename in (preferred, fallback):
        if not os.path.exists(filename):
            continue
        with open(filename, "r") as f:
            config = json.load(f)

        raw_pairs = config.get("pairs", [])
        if not raw_pairs:
            continue

        # New format: pairs have a 'pair_id' and 'tokens' list
        if "tokens" in raw_pairs[0]:
            pairs = [p for p in raw_pairs if _is_in_trade_window(p.get("game_start_time"))]
            tokens = list({t for p in pairs for t in p["tokens"]})
            skipped = len(raw_pairs) - len(pairs)
            print(f"📂 {filename}: {len(raw_pairs)} pairs → {len(pairs)} in window "
                  f"({skipped} skipped, outside ±{TRADE_WINDOW_HOURS}h)")
            return pairs, tokens

        # Legacy format: pairs have 'strict_no' / 'broad_yes'
        legacy_pairs = []
        for p in raw_pairs:
            legacy_pairs.append({
                "pair_id": f"{p['match_id']}::btts_vs_o15",
                "match_id": p["match_id"],
                "strategy": "btts_vs_o15",
                "tokens": [p["strict_no"], p["broad_yes"]],
                "label": p.get("label", "NO BTTS + YES O1.5"),
                "game_start_time": None,
            })
        pairs = [p for p in legacy_pairs if _is_in_trade_window(p.get("game_start_time"))]
        tokens = list({t for p in pairs for t in p["tokens"]})
        print(f"📂 {filename} (legacy): {len(pairs)} pairs in window")
        return pairs, tokens

    return [], []


def _setup_logging() -> None:
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("execution.log"),
        ],
    )


def _load_env(path: str = ".env") -> None:
    from pathlib import Path
    if not Path(path).exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


if __name__ == "__main__":
    _load_env()
    _setup_logging()

    PAIRS_FILE = "wc_pairs.json"

    # Always fetch fresh pairs at startup — ensures we trade on current data
    log.info("Fetching market pairs from Polymarket...")
    try:
        from getotken import fetch_all_pairs
        fetch_all_pairs(PAIRS_FILE)
    except Exception as e:
        log.warning("Could not fetch fresh pairs (%s) — using cached file if available", e)

    # Execution engine — shadow mode unless explicitly disabled in .env
    shadow_mode = os.environ.get("SHADOW_MODE", "true").lower() != "false"
    executor = None
    try:
        from execution.executor import ExecutionEngine
        executor = ExecutionEngine(shadow_mode=shadow_mode)
        mode_label = "SHADOW" if shadow_mode else "LIVE"
        log.info("Execution engine: %s", mode_label)
    except ImportError:
        log.warning("execution/ module not found — running paper trading only")
    except Exception as e:
        log.warning("Executor init failed (%s) — running paper trading only", e)

    paper = PaperTrader(target_shares=Decimal(str(TARGET_SHARES)), log_file="paper_trades.jsonl")

    async def _main() -> None:
        asyncio.create_task(_refresh_loop(PAIRS_FILE))
        await stream_market_data(executor, paper, PAIRS_FILE)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n⏹  Stream stopped.")
        paper.print_summary()
