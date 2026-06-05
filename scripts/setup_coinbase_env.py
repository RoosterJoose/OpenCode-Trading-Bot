#!/usr/bin/env python3
"""Writes a private key env file from raw JSON key export."""
import json, sys, os
from pathlib import Path

if len(sys.argv) > 1:
    key_file = Path(sys.argv[1])
    data = json.loads(key_file.read_text())
    key_name = data.get("name", os.environ.get("HERMES_COINBASE__API_KEY_ID", ""))
    key_secret = data.get("privateKey", "")
    # Write to dotenv-style file for sourcing
    env_path = Path("/opt/hermes-trading-bot/.env_key")
    env_path.write_text(f"""
export HERMES_COINBASE__API_KEY_ID="{key_name}"
export HERMES_COINBASE__PRIVATE_KEY='{key_secret}'
""")
    print(f"Wrote env key to {env_path}")
    print(f"Source with: source {env_path}")
else:
    print("Usage: python setup_coinbase_env.py /path/to/cdp_api_key.json")
