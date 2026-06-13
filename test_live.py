"""
Diagnostic + live auth test for the Polymarket CLOB.

Checks (in order):
  1. On-chain USDC.e balance (direct RPC — ground truth, bypasses Polymarket API)
  2. On-chain CTF allowances (direct RPC)
  3. py-clob-client authentication (HTTP 200 with derived API key)
  4. Optional: post a 1-share FOK order at an absurdly low price (0.01)
     on the first available active market token → tests full auth flow with
     zero risk (FOK at 0.01 will never match).

Usage:
  python3 test_live.py           # steps 1-3 only
  python3 test_live.py --order   # steps 1-4 (posts a ghost FOK order)
"""
import os
import sys
import json
import requests
from pathlib import Path
from decimal import Decimal

# ─── Config ───────────────────────────────────────────────────────────────────
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_CONTRACTS = [
    ("CTF Exchange",     "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
    ("NegRisk Exchange", "0xE111180000d2663C0091e4f400237545B87B996B"),
    ("NegRisk Adapter",  "0xe2222d279d744050d28e00520010520000310F59"),
]


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _load_env():
    p = Path(".env")
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def rpc(method, params):
    r = requests.post(POLYGON_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params
    }, timeout=12)
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"]["message"])
    return data["result"]


def pad(addr: str) -> str:
    return addr.lower().replace("0x", "").zfill(64)


def usdc_balance(wallet: str) -> Decimal:
    result = rpc("eth_call", [{"to": USDC_E, "data": "0x70a08231" + pad(wallet)}, "latest"])
    return Decimal(int(result, 16)) / Decimal("1e6")


def usdc_allowance(wallet: str, spender: str) -> Decimal:
    result = rpc("eth_call", [{
        "to": USDC_E,
        "data": "0xdd62ed3e" + pad(wallet) + pad(spender),
    }, "latest"])
    return Decimal(int(result, 16)) / Decimal("1e6")


def sep(title=""):
    print(f"\n{'─' * 50}")
    if title:
        print(f"  {title}")
        print(f"{'─' * 50}")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    _load_env()
    do_order = "--order" in sys.argv

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not pk:
        print("❌  POLYMARKET_PRIVATE_KEY not set")
        sys.exit(1)

    from eth_account import Account
    account = Account.from_key(pk)
    wallet = account.address
    print(f"\nWallet : {wallet}")

    # ── Step 1: on-chain USDC.e balance ──────────────────────────────────────
    sep("1 — On-chain USDC.e balance")
    balance = usdc_balance(wallet)
    icon = "✅" if balance > 0 else "⚠️ "
    print(f"  {icon}  Balance : {balance:.6f} USDC.e")
    if balance == 0:
        print("  ⚠️  Wallet holds 0 USDC.e — fund it before going live.")

    # ── Step 2: on-chain allowances ───────────────────────────────────────────
    sep("2 — On-chain CTF allowances")
    all_approved = True
    for label, contract in CTF_CONTRACTS:
        allow = usdc_allowance(wallet, contract)
        approved = allow > Decimal("1_000_000")
        icon = "✅" if approved else "❌"
        print(f"  {icon}  {label:<22} {allow:.0f} USDC.e")
        if not approved:
            all_approved = False
    if not all_approved:
        print("\n  Run python3 approve_usdc.py to fix missing approvals.")

    # ── Step 3: py-clob-client authentication ─────────────────────────────────
    sep("3 — CLOB API authentication")
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds

        host = "https://clob.polymarket.com"
        chain_id = 137

        # Build a Level-1 client (signs with EOA, no API key needed for derive)
        client_l1 = ClobClient(host, key=pk, chain_id=chain_id)
        creds = client_l1.create_or_derive_api_key()
        print(f"  Derived API key : {creds.api_key[:12]}...")

        # Build a Level-2 client with the derived credentials
        client = ClobClient(
            host,
            key=pk,
            chain_id=chain_id,
            creds=ApiCreds(
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                api_passphrase=creds.api_passphrase,
            ),
        )

        # Check balance via CLOB API
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        api_balance = Decimal(str(bal.get("balance", 0)))
        api_allowance = Decimal(str(
            max((float(v) for v in bal.get("allowances", {}).values()), default=0)
            if bal.get("allowances") else 0
        ))
        print(f"  CLOB API balance   : {api_balance} USDC")
        print(f"  CLOB API allowance : {api_allowance} USDC")

        if api_balance == 0 and balance > 0:
            print("  ℹ️  API reports 0 but on-chain balance is non-zero — stale cache.")
            print("     Attempting update_balance_allowance...")
            try:
                from py_clob_client.clob_types import UpdateBalanceAllowanceParams
                client.update_balance_allowance(
                    UpdateBalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
                )
                bal2 = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                print(f"  After sync — balance : {bal2.get('balance')} | allowances : {bal2.get('allowances')}")
            except Exception as e:
                print(f"  update_balance_allowance error: {e}")

        print("  ✅  Authentication OK")

    except ImportError:
        print("  ❌  py_clob_client not installed — run: pip install -r requirements.txt")
        return
    except Exception as e:
        print(f"  ❌  Auth error: {e}")
        return

    # ── Step 4 (optional): ghost FOK order ────────────────────────────────────
    if not do_order:
        sep()
        print("\n  Tip: run with --order to also post a ghost FOK (0.01 price, won't fill).")
        print("  This confirms the full order-signing auth chain works end-to-end.\n")
        return

    sep("4 — Ghost FOK order (price=0.01, size=1, will NOT fill)")

    # Pick the first token from wc_pairs.json or friendlies_pairs.json
    token_id = None
    for fname in ("wc_pairs.json", "friendlies_pairs.json"):
        if Path(fname).exists():
            data = json.load(open(fname))
            pairs = data.get("pairs", [])
            if pairs and pairs[0].get("tokens"):
                token_id = pairs[0]["tokens"][0]
                print(f"  Using token : {token_id[:20]}... (from {fname})")
                break

    if not token_id:
        print("  ❌  No pairs file found — run getotken.py first or specify a token.")
        return

    try:
        from py_clob_client_v2.clob_types import OrderArgs, OrderType
        from py_clob_client_v2 import Side

        order_args = OrderArgs(
            token_id=token_id,
            price=0.01,   # absurdly low → will never match
            size=1,
            side=Side.BUY,
        )
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.FOK)

        status = resp.get("status", "unknown")
        print(f"  CLOB response : {json.dumps(resp, indent=4)}")

        if status in ("matched", "live"):
            print("  ✅  Order accepted (unexpected fill at 0.01 — check immediately!)")
        elif "error" in resp or resp.get("errorMsg"):
            err = resp.get("errorMsg") or resp.get("error")
            print(f"  ⚠️  Server error: {err}")
        else:
            print(f"  ✅  FOK rejected as expected (status={status}) — auth chain works!\n")

    except Exception as e:
        print(f"  ❌  Order error: {e}")


if __name__ == "__main__":
    main()
