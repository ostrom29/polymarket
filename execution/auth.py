"""
Polymarket CLOB authentication.

Two auth levels:
  Level 1 — private key only    → read markets, derive API creds
  Level 2 — private key + creds → place/cancel orders

Required env vars for Level 2 (order placement):
  POLYMARKET_PRIVATE_KEY   — EOA wallet private key (0x-prefixed)
  POLYMARKET_API_KEY       — derived via setup_credentials.py
  POLYMARKET_API_SECRET
  POLYMARKET_PASSPHRASE

Run setup_credentials.py once to generate and persist Level 2 credentials.
"""
import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


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
        return ClobClient(
            host=CLOB_HOST,
            key=pk,
            chain_id=POLYGON_CHAIN_ID,
            creds=creds,
            signature_type=0,  # EOA (non-hardware wallet)
        )

    # Level 1 only — enough for credential derivation
    return ClobClient(host=CLOB_HOST, key=pk, chain_id=POLYGON_CHAIN_ID)
