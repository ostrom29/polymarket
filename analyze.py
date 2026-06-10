"""
Post-session analysis tool.

Usage:
    python analyze.py [paper_trades.jsonl]

Reads the JSONL log produced by paper_trader.py and outputs:
  1. Strategy viability ranking
  2. Window duration distribution (is execution physically possible?)
  3. Fee impact (gross vs net conversion rate)
  4. Top opportunities by match
  5. Hourly frequency (when do windows cluster?)
  6. Verdict: which strategies to keep / drop / tune

Run after every paper-trading session to iteratively improve thresholds.
"""
import sys
import json
from collections import defaultdict
from decimal import Decimal
from datetime import datetime, timezone


# ─── Data loading ─────────────────────────────────────────────────────────────

def load_log(path: str) -> tuple[dict, list[dict]]:
    """Returns (session_header, list of OPEN/CLOSE records)."""
    header: dict = {}
    records: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") == "SESSION_START":
                header = row
            elif row.get("event") in ("OPEN", "CLOSE"):
                records.append(row)
    return header, records


# ─── Analysis helpers ─────────────────────────────────────────────────────────

def _bar(value: float, max_val: float, width: int = 30) -> str:
    if max_val == 0:
        return ""
    filled = int(round(value / max_val * width))
    return "█" * filled + "░" * (width - filled)


def _dur_bucket(ms: float) -> str:
    if ms < 200:
        return "<200ms (very short)"
    if ms < 500:
        return "200–500ms"
    if ms < 1500:
        return "500ms–1.5s"
    if ms < 5000:
        return "1.5s–5s"
    return ">5s (very long)"


def _hour(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%H:00")
    except Exception:
        return "??"


# ─── Report sections ──────────────────────────────────────────────────────────

def section_strategy_ranking(records: list[dict]) -> None:
    print("\n" + "═" * 64)
    print("  1. STRATEGY VIABILITY RANKING")
    print("═" * 64)

    opens = [r for r in records if r["event"] == "OPEN"]
    closes = [r for r in records if r["event"] == "CLOSE"]

    by_strat: dict[str, dict] = defaultdict(lambda: {
        "opens": 0, "total_net": Decimal("0"),
        "durations": [], "peaks": []
    })

    for r in opens:
        s = r.get("strategy", "unknown")
        by_strat[s]["opens"] += 1
        by_strat[s]["total_net"] += Decimal(r.get("net_profit_total", "0"))
        by_strat[s]["peaks"].append(float(r.get("net_profit_per_share", "0")))

    for r in closes:
        s = r.get("strategy", "unknown")
        if r.get("duration_ms") is not None:
            by_strat[s]["durations"].append(r["duration_ms"])

    # Sort by total net profit descending
    ranked = sorted(by_strat.items(), key=lambda x: x[1]["total_net"], reverse=True)
    max_profit = float(ranked[0][1]["total_net"]) if ranked else 1

    for strat, d in ranked:
        n = d["opens"]
        total = float(d["total_net"])
        avg_dur = sum(d["durations"]) / len(d["durations"]) if d["durations"] else 0
        avg_peak = sum(d["peaks"]) / len(d["peaks"]) if d["peaks"] else 0
        bar = _bar(total, max_profit)
        dur_flag = (
            "⚠️  co-loc" if avg_dur < 200 else
            "🟡 tight" if avg_dur < 1500 else
            "✅ ok"
        )
        print(f"\n  {strat}")
        print(f"    Opportunities  : {n}")
        print(f"    Simulated P&L  : +{total:.4f} USDC  {bar}")
        print(f"    Avg net/share  : +{avg_peak:.4f} USDC")
        print(f"    Avg window     : {avg_dur:.0f}ms  {dur_flag}")


def section_duration_distribution(records: list[dict]) -> None:
    print("\n" + "═" * 64)
    print("  2. WINDOW DURATION DISTRIBUTION  (can we execute in time?)")
    print("═" * 64)

    durations = [
        r["duration_ms"] for r in records
        if r["event"] == "CLOSE" and r.get("duration_ms") is not None
    ]
    if not durations:
        print("  No closed windows yet.")
        return

    buckets: dict[str, int] = defaultdict(int)
    for d in durations:
        buckets[_dur_bucket(d)] += 1

    bucket_order = ["<200ms (very short)", "200–500ms", "500ms–1.5s", "1.5s–5s", ">5s (very long)"]
    max_count = max(buckets.values(), default=1)
    total = len(durations)

    for bucket in bucket_order:
        count = buckets.get(bucket, 0)
        pct = count / total * 100
        bar = _bar(count, max_count)
        print(f"  {bucket:<22} {bar}  {count:4}  ({pct:.0f}%)")

    pct_executable = sum(
        buckets.get(b, 0) for b in bucket_order if "200" in b or "1.5" in b or ">5" in b
    ) / total * 100
    print(f"\n  Executable (≥200ms) : {pct_executable:.0f}% of windows")
    print(f"  Median duration     : {sorted(durations)[len(durations)//2]:.0f}ms")


def section_fee_impact(records: list[dict]) -> None:
    print("\n" + "═" * 64)
    print("  3. FEE IMPACT ANALYSIS  (how many gross signals survive fees?)")
    print("═" * 64)

    opens = [r for r in records if r["event"] == "OPEN"]
    by_strat: dict[str, dict] = defaultdict(lambda: {"gross_costs": [], "net_profits": []})

    for r in opens:
        s = r.get("strategy", "unknown")
        by_strat[s]["gross_costs"].append(float(r.get("gross_cost", 1)))
        by_strat[s]["net_profits"].append(float(r.get("net_profit_per_share", 0)))

    for strat, d in sorted(by_strat.items()):
        if not d["gross_costs"]:
            continue
        avg_gross = sum(d["gross_costs"]) / len(d["gross_costs"])
        avg_net = sum(d["net_profits"]) / len(d["net_profits"])
        # Theoretical fee = gross_cost * 0.02 (both legs)
        avg_fee = avg_gross * 0.02
        print(f"\n  {strat}")
        print(f"    Avg gross cost    : {avg_gross:.4f} USDC")
        print(f"    Avg fee (2%)      : {avg_fee:.4f} USDC")
        print(f"    Avg net/share     : +{avg_net:.4f} USDC")
        print(f"    Margin above fees : {avg_net / avg_fee * 100:.1f}% of fee cost")


def section_top_opportunities(records: list[dict], top_n: int = 8) -> None:
    print("\n" + "═" * 64)
    print(f"  4. TOP {top_n} OPPORTUNITIES (by net profit per window)")
    print("═" * 64)

    opens = [r for r in records if r["event"] == "OPEN"]
    opens_sorted = sorted(opens, key=lambda r: float(r.get("net_profit_total", "0")), reverse=True)

    for i, r in enumerate(opens_sorted[:top_n], 1):
        net = float(r.get("net_profit_total", 0))
        gross = float(r.get("gross_cost", 1))
        peak = float(r.get("peak_net_profit", r.get("net_profit_per_share", 0)))
        match = r.get("match_id", "?")[:35]
        strat = r.get("strategy", "?")
        ts = r.get("ts", "")[:16]
        print(f"\n  #{i:2}  {match}")
        print(f"       [{strat}]  gross={gross:.4f}  net/shr=+{peak:.4f}  total=+{net:.4f} USDC  @ {ts}")


def section_hourly_frequency(records: list[dict]) -> None:
    print("\n" + "═" * 64)
    print("  5. HOURLY FREQUENCY  (when do opportunities cluster?)")
    print("═" * 64)

    opens = [r for r in records if r["event"] == "OPEN"]
    by_hour: dict[str, int] = defaultdict(int)
    for r in opens:
        by_hour[_hour(r.get("ts", ""))] += 1

    if not by_hour:
        print("  No data.")
        return

    max_count = max(by_hour.values(), default=1)
    for hour in sorted(by_hour.keys()):
        count = by_hour[hour]
        bar = _bar(count, max_count, width=20)
        print(f"  {hour}  {bar}  {count}")


def section_verdict(header: dict, records: list[dict]) -> None:
    print("\n" + "═" * 64)
    print("  6. VERDICT & RECOMMENDATIONS")
    print("═" * 64)

    opens = [r for r in records if r["event"] == "OPEN"]
    closes = [r for r in records if r["event"] == "CLOSE" and r.get("duration_ms")]

    total_net = sum(float(r.get("net_profit_total", 0)) for r in opens)
    n = len(opens)

    if n == 0:
        print("\n  No profitable opportunities found in this session.")
        print("  → Possible causes: markets correctly priced, insufficient liquidity,")
        print("    or threshold too strict. Try reducing target_shares in PaperTrader.")
        return

    durations = [r["duration_ms"] for r in closes]
    pct_slow = sum(1 for d in durations if d >= 200) / len(durations) * 100 if durations else 0

    by_strat: dict[str, int] = defaultdict(int)
    for r in opens:
        by_strat[r.get("strategy", "?")] += 1
    best_strat = max(by_strat, key=lambda k: by_strat[k])

    print(f"\n  Sessions results:")
    print(f"    {n} profitable windows  →  +{total_net:.4f} USDC simulated")
    print(f"    {pct_slow:.0f}% of windows lasted ≥200ms (potentially executable)")
    print(f"    Best-performing strategy: {best_strat}")

    print("\n  Recommendations:")

    if pct_slow < 30:
        print("  ⚠️  Most windows are very short. Before going live:")
        print("     - Measure your actual network RTT to Polymarket endpoints")
        print("     - Consider co-locating near AWS us-east-1 (Polymarket's region)")
        print("     - Implement pre-signed order caching to shave signing latency")
    else:
        print("  ✅  Sufficient window duration for async execution")

    if total_net > 0 and n >= 3:
        print("  ✅  Strategy appears viable. Next step: shadow execution (sign but don't submit)")
    elif n < 3:
        print("  🔁  Too few opportunities in this session. Run a longer session (full match day)")

    # Per-strategy recommendations
    print()
    strat_profits: dict[str, Decimal] = defaultdict(Decimal)
    for r in opens:
        strat_profits[r.get("strategy", "?")] += Decimal(r.get("net_profit_total", "0"))

    for strat, profit in sorted(strat_profits.items(), key=lambda x: x[1], reverse=True):
        if profit <= 0:
            print(f"  ❌  {strat}: no profit — review threshold or liquidity requirements")
        elif float(profit) < 0.10:
            print(f"  🟡  {strat}: marginal (+{profit:.4f} USDC) — monitor more sessions")
        else:
            print(f"  ✅  {strat}: promising (+{profit:.4f} USDC) — candidate for live execution")


# ─── Entry point ──────────────────────────────────────────────────────────────

def run(log_path: str) -> None:
    print(f"\n{'═' * 64}")
    print(f"  POLYMARKET ARB — POST-SESSION ANALYSIS")
    print(f"  Log: {log_path}")
    print(f"{'═' * 64}")

    header, records = load_log(log_path)

    if header:
        ts = header.get("ts", "")[:16]
        fee = header.get("taker_fee_rate", "0.02")
        be = header.get("breakeven_gross", "0.9804")
        shares = header.get("target_shares", "50")
        print(f"\n  Session started : {ts}")
        print(f"  Fee rate        : {float(fee)*100:.0f}%  |  Break-even gross: {be}")
        print(f"  Target shares   : {shares}")

    if not records:
        print("\n  No trade records found.")
        return

    section_strategy_ranking(records)
    section_duration_distribution(records)
    section_fee_impact(records)
    section_top_opportunities(records)
    section_hourly_frequency(records)
    section_verdict(header, records)
    print()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "paper_trades.jsonl"
    run(path)
