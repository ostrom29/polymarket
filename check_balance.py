"""
Diagnostic: check USDC balances directly on Polygon via raw RPC call.
No extra dependencies — uses only requests.

Usage: python3 check_balance.py
"""
import os
import json
import requests
from pathlib import Path

# Load .env
def _load_env():
    for line in Path(".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

_load_env()

POLYGON_RPC = "https://polygon-rpc.com"

# USDC contracts on Polygon
CONTRACTS = {
    "USDC.e (bridged)": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "USDC   (native) ": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
}

def balanceof(contract: str, wallet: str) -> float:
    """Call ERC-20 balanceOf via eth_call."""
    # balanceOf(address) selector = 0x70a08231
    padded = wallet.lower().replace("0x", "").zfill(64)
    data = "0x70a08231" + padded
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": contract, "data": data}, "latest"],
    }
    r = requests.post(POLYGON_RPC, json=payload, timeout=10)
    result = r.json().get("result", "0x0")
    raw = int(result, 16) if result and result != "0x" else 0
    return raw / 1e6  # USDC has 6 decimals


def get_pol_balance(wallet: str) -> float:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
        "params": [wallet, "latest"],
    }
    r = requests.post(POLYGON_RPC, json=payload, timeout=10)
    result = r.json().get("result", "0x0")
    raw = int(result, 16) if result and result != "0x" else 0
    return raw / 1e18  # POL/MATIC has 18 decimals


pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
if not pk:
    print("❌ POLYMARKET_PRIVATE_KEY not set in .env")
    exit(1)

try:
    from eth_account import Account
    wallet = Account.from_key(pk).address
except Exception as e:
    print(f"❌ Could not derive address from private key: {e}")
    exit(1)

print(f"\n─── On-chain balance check ───")
print(f"  Wallet : {wallet}")
print(f"  RPC    : {POLYGON_RPC}\n")

pol = get_pol_balance(wallet)
print(f"  POL (native)       : {pol:.4f}")

for name, contract in CONTRACTS.items():
    balance = balanceof(contract, wallet)
    marker = "✅" if balance > 0 else "  "
    print(f"  {marker} {name}: {balance:.2f} USDC  (contract: {contract})")

print()
