"""
Approve Polymarket CTF Exchange contracts to spend native USDC on Polygon.

py-clob-client's update_balance_allowance is hardcoded for USDC.e (0x2791...).
This script approves native USDC (0x3c499c...) directly on-chain.

Usage: python3 approve_usdc.py
"""
import os
import requests
from pathlib import Path
from eth_account import Account

POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
POLYGON_CHAIN_ID = 137

# Polymarket uses USDC.e (bridged from Ethereum) — NOT native USDC
USDC_NATIVE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e

# CTF Exchange contracts returned by get_balance_allowance — approve all three
CTF_CONTRACTS = [
    ("CTF Exchange",         "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
    ("NegRisk Exchange",     "0xE111180000d2663C0091e4f400237545B87B996B"),
    ("NegRisk Adapter",      "0xe2222d279d744050d28e00520010520000310F59"),
]

MAX_UINT256 = 2 ** 256 - 1


def _load_env():
    p = Path(".env")
    if not p.exists():
        return
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


def get_allowance(owner: str, spender: str) -> float:
    # allowance(address owner, address spender) → uint256
    padded_owner   = owner.lower().replace("0x", "").zfill(64)
    padded_spender = spender.lower().replace("0x", "").zfill(64)
    result = rpc("eth_call", [{
        "to": USDC_NATIVE,
        "data": "0xdd62ed3e" + padded_owner + padded_spender
    }, "latest"])
    return int(result, 16) / 1e6


def build_approve_calldata(spender: str) -> str:
    # approve(address spender, uint256 amount) → selector 0x095ea7b3
    padded_spender = spender.lower().replace("0x", "").zfill(64)
    padded_amount  = hex(MAX_UINT256)[2:].zfill(64)
    return "0x095ea7b3" + padded_spender + padded_amount


def send_approve(account, spender: str, nonce: int, gas_price: int) -> str:
    tx = {
        "nonce":    nonce,
        "gasPrice": gas_price,
        "gas":      65_000,
        "to":       USDC_NATIVE,
        "value":    0,
        "data":     build_approve_calldata(spender),
        "chainId":  POLYGON_CHAIN_ID,
    }
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
    tx_hash = rpc("eth_sendRawTransaction", ["0x" + raw.hex()])
    return tx_hash


def main():
    _load_env()

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not pk:
        print("❌  POLYMARKET_PRIVATE_KEY not set in .env")
        return

    account = Account.from_key(pk)
    wallet  = account.address

    print(f"\n─── Native USDC Approval ───")
    print(f"  Wallet : {wallet}")
    print(f"  Token  : {USDC_NATIVE} (native USDC)\n")

    # Current nonce
    nonce = int(rpc("eth_getTransactionCount", [wallet, "latest"]), 16)

    # Gas price — 2× current for fast inclusion on Polygon
    base_gas = int(rpc("eth_gasPrice", []), 16)
    gas_price = base_gas * 2
    print(f"  Gas price : {base_gas/1e9:.1f} gwei (using {gas_price/1e9:.1f} gwei)\n")

    sent = 0
    for label, contract in CTF_CONTRACTS:
        current = get_allowance(wallet, contract)
        if current > 1_000_000:
            print(f"  ✅  {label:<22} already approved ({current:.0f} USDC)")
            continue

        print(f"  ⏳  {label:<22} approving...", end="", flush=True)
        try:
            tx_hash = send_approve(account, contract, nonce + sent, gas_price)
            print(f" ✅  {tx_hash}")
            sent += 1
        except Exception as e:
            print(f" ❌  {e}")

    if sent == 0:
        print("\n  All contracts already approved — nothing to do.")
    else:
        print(f"\n  {sent} approval(s) submitted.")
        print("  Polygon confirms in ~5s. Run setup_credentials.py to verify.\n")


if __name__ == "__main__":
    main()
