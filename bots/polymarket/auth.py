"""
Polymarket CLOB authentication.

Authentication flow:
  1. Provide Ethereum private key via POLYMARKET_PRIVATE_KEY env var
  2. Either provide pre-derived L2 API credentials (preferred):
       POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE
  3. Or let the client derive them automatically from the private key.

Run once to derive and save credentials:
  python -c "from bots.polymarket.auth import derive_and_print_creds; derive_and_print_creds()"

No private key → read-only mode (paper trading only).
"""
import logging
import os
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

CLOB_API = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

logger = logging.getLogger(__name__)


def get_client(private_key: Optional[str] = None) -> Optional[ClobClient]:
    """
    Initialize an authenticated CLOB client.

    Returns:
        Authenticated ClobClient, or None if no private key is available
        (read-only / paper trading mode).
    """
    pk = private_key or os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not pk:
        logger.info("No POLYMARKET_PRIVATE_KEY set — read-only / paper trading mode")
        return None

    try:
        api_key = os.environ.get("POLYMARKET_API_KEY")
        api_secret = os.environ.get("POLYMARKET_API_SECRET")
        api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE")

        if api_key and api_secret and api_passphrase:
            # Use pre-derived credentials (faster, no on-chain tx)
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )
            client = ClobClient(
                CLOB_API,
                key=pk,
                chain_id=POLYGON_CHAIN_ID,
                creds=creds,
            )
            logger.info(f"CLOB client initialized with pre-derived L2 key: {api_key[:8]}...")
        else:
            # Derive L2 credentials from private key (first-time setup)
            logger.info("Deriving L2 API key from private key (first-time setup)...")
            base_client = ClobClient(CLOB_API, key=pk, chain_id=POLYGON_CHAIN_ID)
            creds = base_client.derive_api_key()
            logger.info(f"L2 key derived: {creds.api_key[:8]}...")
            logger.info(
                "Save these to your environment to avoid re-deriving on every start:\n"
                f"  POLYMARKET_API_KEY={creds.api_key}\n"
                f"  POLYMARKET_API_SECRET={creds.api_secret}\n"
                f"  POLYMARKET_API_PASSPHRASE={creds.api_passphrase}"
            )
            client = ClobClient(
                CLOB_API,
                key=pk,
                chain_id=POLYGON_CHAIN_ID,
                creds=creds,
            )

        # Verify auth works
        try:
            balance = client.get_balance()
            logger.info(f"Auth verified — USDC balance: {balance:.2f}")
        except Exception as e:
            logger.warning(f"Could not fetch balance (auth may still work): {e}")

        return client

    except Exception as e:
        logger.error(f"Failed to initialize CLOB client: {e}")
        return None


def derive_and_print_creds(private_key: Optional[str] = None) -> None:
    """
    Derive and print L2 API credentials to stdout.

    Run this once, then save the output to your environment variables.
    Usage:
        python -c "from bots.polymarket.auth import derive_and_print_creds; derive_and_print_creds('0x...')"
    """
    pk = private_key or os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not pk:
        print("Error: provide private_key argument or set POLYMARKET_PRIVATE_KEY env var")
        return

    client = ClobClient(CLOB_API, key=pk, chain_id=POLYGON_CHAIN_ID)
    creds = client.derive_api_key()
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
