"""Submit a trade intent into Hermes' SQLite queue.

Usage: python scripts/submit_intent.py data/hermes.db intent.json
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.store.sqlite import Store


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: submit_intent.py <db-path> <intent-json>")
    store = Store(Path(sys.argv[1]))
    try:
        intent = json.loads(Path(sys.argv[2]).read_text())
        if not store.save_intent(intent):
            raise SystemExit("duplicate idempotency_key")
    finally:
        store.close()


if __name__ == "__main__":
    main()
