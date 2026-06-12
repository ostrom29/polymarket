"""
Connectivity test — run before going live to verify the Polymarket CLOB API
is reachable from this server's IP (geo-restriction check).

Usage:
    python3 test_connectivity.py

Tests (no credentials required, all read-only):
  1. Gamma API      — market data (used by getotken.py)
  2. CLOB WebSocket — order book stream (used by websocket.py)
  3. CLOB REST API  — order placement endpoint (blocked in France/US)
  4. CLOB auth ping — verifies API key is recognized (requires .env creds)

Exit code 0 = all clear, exit code 1 = geo-block or connectivity issue.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path


def _load_env(path: str = ".env") -> None:
    if not Path(path).exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _check(label: str, ok: bool, detail: str = "") -> bool:
    status = "✅" if ok else "❌"
    print(f"  {status}  {label:<40} {detail}")
    return ok


def test_gamma_api() -> bool:
    import requests
    try:
        t0 = time.perf_counter()
        r = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"active": "true", "limit": 1},
            timeout=8,
        )
        ms = (time.perf_counter() - t0) * 1000
        ok = r.status_code == 200
        return _check("Gamma API (market data)", ok, f"HTTP {r.status_code} | {ms:.0f}ms")
    except Exception as e:
        return _check("Gamma API (market data)", False, str(e))


def test_clob_rest() -> bool:
    import requests
    try:
        t0 = time.perf_counter()
        # /markets is a public read-only endpoint — blocked in France if geo-restricted
        r = requests.get("https://clob.polymarket.com/markets", timeout=8)
        ms = (time.perf_counter() - t0) * 1000
        ok = r.status_code in (200, 400)  # 400 = reached but params wrong, still accessible
        detail = f"HTTP {r.status_code} | {ms:.0f}ms"
        if r.status_code == 403:
            detail += " ← GEO-BLOCKED"
        return _check("CLOB REST API (order endpoint)", ok, detail)
    except Exception as e:
        return _check("CLOB REST API (order endpoint)", False, str(e))


def test_clob_auth() -> bool:
    api_key = os.environ.get("POLYMARKET_API_KEY", "").strip()
    if not api_key:
        _check("CLOB auth ping (API key)", True, "skipped — no API key in .env")
        return True  # not a failure, just not configured yet

    import requests
    try:
        t0 = time.perf_counter()
        r = requests.get(
            "https://clob.polymarket.com/auth/api-key",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8,
        )
        ms = (time.perf_counter() - t0) * 1000
        ok = r.status_code == 200
        detail = f"HTTP {r.status_code} | {ms:.0f}ms"
        if r.status_code == 401:
            detail += " ← key not recognized"
        elif r.status_code == 403:
            detail += " ← GEO-BLOCKED"
        return _check("CLOB auth ping (API key)", ok, detail)
    except Exception as e:
        return _check("CLOB auth ping (API key)", False, str(e))


async def test_websocket() -> bool:
    try:
        import websockets
        t0 = time.perf_counter()
        async with websockets.connect(
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            ping_interval=None,
            open_timeout=8,
        ) as ws:
            ms = (time.perf_counter() - t0) * 1000
            return _check("CLOB WebSocket (order book stream)", True, f"connected | {ms:.0f}ms")
    except Exception as e:
        return _check("CLOB WebSocket (order book stream)", False, str(e))


def main() -> None:
    _load_env()

    print("\n─── Polymarket Connectivity Test ───\n")

    results = []
    results.append(test_gamma_api())
    results.append(test_clob_rest())
    results.append(asyncio.run(test_websocket()))
    results.append(test_clob_auth())

    print()
    if all(results):
        print("✅  All checks passed — server can reach Polymarket APIs")
        print("   No VPN or proxy required from this IP.\n")
        sys.exit(0)
    else:
        failed = sum(1 for r in results if not r)
        print(f"❌  {failed} check(s) failed")
        if not results[1]:  # CLOB REST failed
            print("""
   The CLOB REST API is unreachable — likely geo-blocked.
   Options:
     A) Migrate server to a non-restricted region (Amsterdam, Warsaw)
     B) Set HTTPS_PROXY=http://your-proxy:port in .env
     C) Use WireGuard VPN on this server
""")
        sys.exit(1)


if __name__ == "__main__":
    main()
