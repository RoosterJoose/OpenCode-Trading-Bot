"""
Entry point — loads config, sets up logging, starts the trading loop.

Usage:
  export HERMES_COINBASE__API_KEY_ID="organizations/.../apiKeys/..."
  export HERMES_COINBASE__PRIVATE_KEY="-----BEGIN EC PRIVATE KEY-----..."
  python -m src.main

Or with env vars for paper-only (no keys needed):
  python -m src.main  # paper mode, no auth required
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo root is on sys.path (works when run via python src/main.py
# or python -m src.main from the project root)
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from src.core.loop import TradingLoop

DEFAULT_CONFIG = {
    "exchange": {"initial_balance": 10_000.0},
    "store": {"path": "hermes.db"},
    "signal_tracker": {"path": "signals.json"},
    "coinbase": {
        "api_key_id": "",
        "private_key": "",
        "portfolio_uuid": "",
    },
    "strategies": {
        "mean_reversion": {
            "assets": [
                "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE",
                "ADA", "AVAX", "LINK", "DOT", "AAVE",
                "LTC", "NEAR", "SUI", "XLM", "HBAR",
                "BCH", "ZEC", "PEPE", "SHIB",
                "HYPE", "ONDO", "ENA",
            ],
        },
    },
}


def merge_env_config(base: dict) -> dict:
    prefix = "HERMES_"
    for key, val in os.environ.items():
        if key.startswith(prefix):
            parts = key[len(prefix):].lower().split("__")
            target = base
            for p in parts[:-1]:
                target = target.setdefault(p, {})
            target[parts[-1]] = _coerce(val)
    return base


def _coerce(val: str):
    if val.lower() in ("true", "yes", "1"):
        return True
    if val.lower() in ("false", "no", "0"):
        return False
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)

    # Quiet noisy libs
    for lib in ("httpx", "websockets", "httpcore"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description="Hermes v2 — Perp trading bot")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument(
        "--data-dir", type=Path, default=Path("data"),
        help="Data directory",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)
    config = merge_env_config(dict(DEFAULT_CONFIG))

    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    api_key_id = config.get("coinbase", {}).get("api_key_id", "")
    mode = "paper" if not api_key_id else "live"
    logger = logging.getLogger("hermes")

    logger.info("=" * 50)
    logger.info("Hermes v2 — Coinbase Perpetual Futures Bot")
    logger.info("Mode: %s | Data: %s", mode, data_dir)
    logger.info("Assets: %s", config["strategies"]["mean_reversion"]["assets"])
    logger.info("Initial balance: $%.0f", config["exchange"]["initial_balance"])
    logger.info("=" * 50)

    loop = TradingLoop(config, data_dir)
    try:
        asyncio.run(loop.start())
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    finally:
        logger.info("Exited cleanly")


if __name__ == "__main__":
    main()
