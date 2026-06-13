"""
Polymarket CLOB authentication.

Two auth levels:
  Level 1 — private key only    → read markets, derive API creds
  Level 2 — private key + creds → place/cancel orders

Required env vars for Level 2 (order placement):
  POLYMARKET_PRIVATE_KEY        — EOA wallet private key (0x-prefixed)
  POLYMARKET_API_KEY            — derived via setup_credentials.py
  POLYMARKET_API_SECRET
  POLYMARKET_PASSPHRASE
  POLYMARKET_DEPOSIT_WALLET     — V2 deposit wallet address (signature_type=3)

Run setup_credentials.py once to generate and persist Level 2 credentials.
"""
import os
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# Signature type 3 = POLY_1271 (ERC-1271 deposit wallet flow, required for V2 CLOB)
_SIG_POLY_1271 = 3


def build_client(require_level2: bool = True) -> ClobClient:
    """
    Build an authenticated ClobClient from environment variables.

    Raises EnvironmentError if required vars are missing.
    When require_level2=True (default), raises if API creds are absent —
    this prevents accidentally running live without proper auth.
    """
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not pk:
        raise EnvironmentError(
            "POLYMARKET_PRIVATE_KEY is not set. "
            "Add it to your .env file (0x-prefixed EOA private key)."
        )

    api_key = os.environ.get("POLYMARKET_API_KEY", "").strip()
    api_secret = os.environ.get("POLYMARKET_API_SECRET", "").strip()
    passphrase = os.environ.get("POLYMARKET_PASSPHRASE", "").strip()
    deposit_wallet = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "").strip()

    has_creds = bool(api_key and api_secret and passphrase)

    if require_level2 and not has_creds:
        raise EnvironmentError(
            "POLYMARKET_API_KEY / API_SECRET / PASSPHRASE are not set. "
            "Run setup_credentials.py to generate them."
        )

    if has_creds:
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=passphrase,
        )
        if deposit_wallet:
            # V2 CLOB requires POLY_1271 (signature_type=3) with a per-user deposit wallet.
            # The deposit wallet is deployed by the Polymarket factory and holds pUSD.
            return ClobClient(
                host=CLOB_HOST,
                key=pk,
                chain_id=POLYGON_CHAIN_ID,
                creds=creds,
                signature_type=_SIG_POLY_1271,
                funder=deposit_wallet,
            )
        return ClobClient(
            host=CLOB_HOST,
            key=pk,
            chain_id=POLYGON_CHAIN_ID,
            creds=creds,
            signature_type=0,
        )

    # Level 1 only — enough for credential derivation
    return ClobClient(host=CLOB_HOST, key=pk, chain_id=POLYGON_CHAIN_ID)
