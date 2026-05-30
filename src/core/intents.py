"""Trade intent validation for the Freqtrade -> Hermes SAE boundary."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.core.types import Side


@dataclass
class TradeIntent:
    id: int
    idempotency_key: str
    source: str
    strategy: str
    asset: str
    side: Side
    confidence: float
    intended_entry_price: float
    requested_stop_price: float | None
    requested_leverage: float
    components: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_row(cls, row: dict) -> "TradeIntent":
        payload = json.loads(row.get("payload") or "{}")
        components = json.loads(row.get("components") or "[]")
        return cls(
            id=int(row["id"]),
            idempotency_key=row["idempotency_key"],
            source=row.get("source") or payload.get("source", "unknown"),
            strategy=row.get("strategy") or payload.get("strategy", ""),
            asset=row["asset"],
            side=Side(row["side"]),
            confidence=float(row.get("confidence") or 0),
            intended_entry_price=float(row.get("intended_entry_price") or 0),
            requested_stop_price=(
                float(row["requested_stop_price"])
                if row.get("requested_stop_price") is not None
                else None
            ),
            requested_leverage=float(row.get("requested_leverage") or 1),
            components=list(components),
            payload=payload,
            created_at=parse_utc(row["created_at"]),
            expires_at=parse_utc(row["expires_at"]),
        )


def parse_utc(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
