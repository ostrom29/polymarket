"""
Shadow trade log analyser.

Usage:
    python3 analyze_shadow.py [shadow_trades.jsonl]

Reads shadow_trades.jsonl and prints:
  - Session overview (trades, success rate, duration stats)
  - P&L summary (simulated, per-strategy)
  - Aborted trades breakdown (price_moved, no_depth, etc.)
  - Per-strategy detail table
"""
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


def load(path: str) -> list[dict]:
    records = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    return records


def analyse(records: list[dict]) -> None:
    total       = len(records)
    successes   = [r for r in records if r.get("success")]
    failures    = [r for r in records if not r.get("success")]

    if total == 0:
        print("No records found.")
        return

    # ── Overview ────────────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"  SHADOW TRADE ANALYSIS  ({total} execution attempts)")
    print(f"{'='*62}")
    print(f"  Successful fills   : {len(successes)}  ({len(successes)/total*100:.1f}%)")
    print(f"  Aborted            : {len(failures)}  ({len(failures)/total*100:.1f}%)")

    # ── Timing ──────────────────────────────────────────────────────────────
    durations = [r["duration_ms"] for r in records if r.get("duration_ms")]
    if durations:
        avg_ms = sum(durations) / len(durations)
        print(f"  Avg duration       : {avg_ms:.1f}ms "
              f"(min {min(durations):.1f} / max {max(durations):.1f})")

    # ── P&L (successful only) ───────────────────────────────────────────────
    if successes:
        print(f"\n  {'─'*56}")
        print(f"  P&L on successful fills")
        print(f"  {'─'*56}")

        by_strat: dict[str, list[Decimal]] = defaultdict(list)
        for r in successes:
            net = r.get("net_per_share")
            if net:
                by_strat[r["strategy"]].append(Decimal(net))

        total_net = Decimal("0")
        for strat, nets in sorted(by_strat.items()):
            count   = len(nets)
            avg_net = sum(nets) / count
            # Assume target_shares = 10 to compute $ P&L per trade
            # Read actual size from first leg if available
            sample_r = next((r for r in successes if r["strategy"] == strat), None)
            size = sample_r["legs"][0]["size"] if sample_r and sample_r.get("legs") else 10
            total_strat = sum(nets) * size
            total_net += total_strat
            print(f"  {strat:<20} | {count:>3} trades | "
                  f"avg net/sh +{avg_net:.4f} | sim P&L +{total_strat:.2f} USDC")

        print(f"\n  {'─'*56}")
        print(f"  Total simulated P&L  : +{total_net:.2f} USDC")

        # Gross distribution
        grosses = [Decimal(r["gross"]) for r in successes if r.get("gross")]
        if grosses:
            avg_gross = sum(grosses) / len(grosses)
            print(f"  Avg gross cost       : {avg_gross:.4f}  "
                  f"(best {min(grosses):.4f} / worst {max(grosses):.4f})")

    # ── Abort reasons ────────────────────────────────────────────────────────
    if failures:
        print(f"\n  {'─'*56}")
        print(f"  Abort reasons")
        print(f"  {'─'*56}")
        reasons: dict[str, int] = defaultdict(int)
        for r in failures:
            err = r.get("error") or "unknown"
            # Normalise price_moved:0.9823>=0.9754 → price_moved
            key = err.split(":")[0]
            reasons[key] += 1
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason:<30} : {count}")

    # ── Time distribution ─────────────────────────────────────────────────
    by_hour: dict[int, int] = defaultdict(int)
    for r in successes:
        ts = r.get("ts", "")
        if "T" in ts:
            try:
                hour = int(ts.split("T")[1][:2])
                by_hour[hour] += 1
            except Exception:
                pass
    if by_hour:
        print(f"\n  {'─'*56}")
        print(f"  Successful fills by hour (UTC)")
        print(f"  {'─'*56}")
        for h in sorted(by_hour):
            bar = "█" * by_hour[h]
            print(f"  {h:02d}h  {bar}  ({by_hour[h]})")

    print(f"\n{'='*62}\n")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "shadow_trades.jsonl"
    if not Path(path).exists():
        print(f"File not found: {path}")
        sys.exit(1)
    records = load(path)
    analyse(records)
