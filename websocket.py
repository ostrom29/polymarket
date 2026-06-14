import asyncio
import websockets
import json
import logging
import os
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from arbitrage import PricingEngine, NLegDetector
from paper_trader import PaperTrader
import notify

log = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Upcoming matches: watch up to TRADE_WINDOW_HOURS before kick-off.
TRADE_WINDOW_HOURS = 12
# Started matches: watch up to POST_MATCH_HOURS after kick-off (live play only).
# Deliberately short: the post-match oracle-resolution window is where losing
# outcomes collapse to 0.001 and create phantom arbs — we stay out of it.
POST_MATCH_HOURS = int(os.environ.get("POST_MATCH_HOURS", "2"))
TARGET_SHARES = 10

# Re-scan Polymarket every N hours for new markets / closed matches
PAIR_REFRESH_HOURS = 4

# Heartbeat interval in hours
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "1"))

# Global stats counters
_stats = {"ticks": 0, "opportunities": 0, "trades": 0}

# Avoid spamming Telegram on rapid reconnects
_last_connect_notify: float = 0.0
_CONNECT_NOTIFY_MIN_INTERVAL = 1800  # seconds

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
        self._last_executed: dict[str, float] = {}  # pair_id → timestamp of last execution

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
                import time as _t
                last = self._last_executed.get(pair_id, 0.0)
                if _t.time() - last < 300:  # 5 min cooldown per pair
                    continue
                self._last_executed[pair_id] = _t.time()
                _stats["opportunities"] += 1
                asyncio.create_task(
                    self.executor.execute(
                        pair_id=pair_id,
                        strategy=pair["strategy"],
                        tokens=pair["tokens"],
                        books=self.books,
                        estimated_gross=float(total_cost),
                        fee_rate=fee_rate,
                        title=pair.get("title", ""),
                        game_start_time=pair.get("game_start_time", ""),
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
                _stats["ticks"] += 1
                self._evaluate_pairs(asset_id)


def _build_connect_message(mode: str, balance_str: str, pairs: list) -> str:
    """Build the Telegram startup message listing watched matches."""
    # Deduplicate by match_id, keep title + kick-off
    seen: dict[str, dict] = {}
    for p in pairs:
        mid = p.get("match_id") or p["pair_id"].split("::")[0]
        if mid not in seen:
            seen[mid] = {
                "title": p.get("title", mid),
                "gst": p.get("game_start_time") or "",
            }

    # Sort by kick-off time
    def _sort_key(item):
        gst = item[1]["gst"]
        if not gst:
            return "9999"
        return gst

    matches = sorted(seen.items(), key=_sort_key)

    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    lines = [f"🤖 Bot connecté — {mode}", f"{balance_str}{len(matches)} matchs surveillés", ""]
    for _, info in matches:
        title = info["title"]
        gst = info["gst"]
        if gst:
            try:
                dt = datetime.fromisoformat(gst.replace("Z", "+00:00")).astimezone(timezone.utc)
                time_tag = dt.strftime("%H:%M")
                date_tag = dt.strftime("%Y-%m-%d")
                day_label = "" if date_tag == today_str else f" ({dt.strftime('%d/%m')})"
                icon = "🏁" if dt <= now_utc else "⏰"
                lines.append(f"  {icon} {time_tag}{day_label} — {title}")
            except Exception:
                lines.append(f"  ⏰ ? — {title}")
        else:
            lines.append(f"  ⏰ — {title}")

    return "\n".join(lines)


def _positions_summary() -> str:
    """Read positions.jsonl and return an open/closed P&L summary string."""
    import json as _json
    from pathlib import Path as _Path

    path = _Path("positions.jsonl")
    if not path.exists():
        return ""

    now = datetime.now(timezone.utc)
    open_pos, closed_net, closed_count = [], 0.0, 0

    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = _json.loads(line)
        except Exception:
            continue

        resolved = False
        ra = rec.get("resolved_after", "")
        if ra:
            try:
                resolved = now > datetime.fromisoformat(ra)
            except Exception:
                pass

        if resolved:
            closed_net += rec.get("est_net_pusd", 0)
            closed_count += 1
        else:
            open_pos.append(rec)

    if not open_pos and closed_count == 0:
        return ""

    lines = []
    if open_pos:
        locked = sum(r.get("cost_pusd", 0) for r in open_pos)
        expected = sum(r.get("est_net_pusd", 0) for r in open_pos)
        lines.append(f"Positions ouvertes : {len(open_pos)} ({locked:.2f} pUSD bloqués)")
        for r in open_pos[-3:]:
            title = r.get("title") or r.get("pair_id", "?").split("::")[0]
            lines.append(f"  ⏳ {title} — +{r.get('est_net_pusd', 0):.3f} pUSD attendu")
        lines.append(f"P&L en attente : +{expected:.3f} pUSD")

    if closed_count > 0:
        lines.append(f"P&L réalisé (estimé) : +{closed_net:.3f} pUSD sur {closed_count} trades")

    return "\n".join(lines)


async def _send_heartbeat(executor) -> None:
    """Build and send a rich status ping: live balance, P&L, execution breakdown."""
    mode = "SHADOW" if (executor and executor.shadow_mode) else "LIVE"

    # Live balance + P&L since startup (fetched fresh, not the stale startup cache)
    bal_line = ""
    if executor and not executor.shadow_mode:
        bal = await executor.fetch_balance()
        start = getattr(executor, "_start_balance", None)
        if bal is not None:
            if start is not None:
                pnl = float(bal) - float(start)
                sign = "+" if pnl >= 0 else ""
                bal_line = (
                    f"Solde : {float(bal):.2f} pUSD "
                    f"(départ {float(start):.2f} · P&L {sign}{pnl:.2f})\n"
                )
            else:
                bal_line = f"Solde : {float(bal):.2f} pUSD\n"

    s = getattr(executor, "stats", {}) if executor else {}
    stuck = getattr(executor._guard, "stuck_count", 0) if (executor and getattr(executor, "_guard", None)) else 0

    msg = (
        f"💓 Bot actif — {mode}\n"
        f"{bal_line}"
        f"Ticks : {_stats['ticks']:,} | Signaux : {_stats['opportunities']}\n"
        f"✅ Trades : {s.get('success', 0)}   ⚠️ Partiels : {s.get('partial_fill', 0)}\n"
        f"🚫 Fantômes filtrés : {s.get('skipped_phantom', 0)}   "
        f"⛔ Blacklist : {s.get('blacklisted_skips', 0)}"
    )
    if stuck:
        msg += f"\n🔴 Positions bloquées (revente ratée) : {stuck}"

    positions = _positions_summary()
    if positions:
        msg += f"\n\n{positions}"

    notify.send(msg)


async def _heartbeat_loop(executor) -> None:
    """Send a Telegram status ping every HEARTBEAT_HOURS hours (plus one shortly after boot)."""
    await asyncio.sleep(120)  # quick first heartbeat so the live balance shows up fast
    await _send_heartbeat(executor)
    while True:
        await asyncio.sleep(HEARTBEAT_HOURS * 3600)
        await _send_heartbeat(executor)


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
                global _last_connect_notify
                import time as _time
                if _time.time() - _last_connect_notify > _CONNECT_NOTIFY_MIN_INTERVAL:
                    mode = "SHADOW" if (executor and executor.shadow_mode) else "LIVE"
                    balance_str = ""
                    if executor and not executor.shadow_mode and executor._usdc_balance is not None:
                        balance_str = f"{float(executor._usdc_balance):.2f} pUSD — "
                    notify.send(_build_connect_message(mode, balance_str, pairs))
                    _last_connect_notify = _time.time()

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
    """
    True if the match is worth watching.

    Upcoming  → kick-off within the next TRADE_WINDOW_HOURS (12h).
    Started   → kick-off within the last POST_MATCH_HOURS (18h).
                Covers the oracle resolution window where the winning token
                can still trade at a discount before Polymarket settles.
    """
    if not game_start_time:
        return True  # no time info → don't filter
    try:
        gst = datetime.fromisoformat(game_start_time.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if gst <= now:
            # Match already started: keep for POST_MATCH_HOURS after kick-off
            return (now - gst) <= timedelta(hours=POST_MATCH_HOURS)
        else:
            # Upcoming match: keep if within TRADE_WINDOW_HOURS
            return (gst - now) <= timedelta(hours=TRADE_WINDOW_HOURS)
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

    mode_label = "SHADOW" if shadow_mode else "LIVE"

    async def _main() -> None:
        asyncio.create_task(_refresh_loop(PAIRS_FILE))
        asyncio.create_task(_heartbeat_loop(executor))
        await stream_market_data(executor, paper, PAIRS_FILE)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n⏹  Stream stopped.")
        paper.print_summary()
