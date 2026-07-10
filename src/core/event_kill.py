"""
Event-driven entry halt — blocks new positions around high-impact economic events.
Fetches ForexFactory calendar, caches weekly, gates entries 15min before/after.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("hermes.event_kill")

FF_URL = "https://www.forexfactory.com/calendar"
BLOCK_BEFORE = 15  # minutes before event
BLOCK_AFTER = 15   # minutes after event
CACHE_HOURS = 6    # refresh interval

HIGH_IMPACT_FILTERS = [
    "CPI", "FOMC", "Non-Farm", "Employment", "GDP", "Unemployment Rate",
    "Fed Interest Rate", "NFP", "PPI", "Retail Sales", "Industrial Production",
    "Consumer Confidence", "ISM", "PMI", "Housing",
    "Consumer Price Index", "Producer Price Index",
]


def _is_high_impact(event_name: str) -> bool:
    name = event_name.lower()
    keywords = [
        "cpi", "fomc", "non-farm", "nonfarm", "employment", "gdp",
        "unemployment", "fed interest rate", "nfp", "ppi",
        "retail sales", "industrial production",
        "consumer confidence", "ism manufact", "ism serv",
        "pmi", "housing", "consumer price", "producer price",
        "initial claims", "jobless claims",
        "philadelphia fed", "empire state",
    ]
    return any(k in name for k in keywords)


class EventKillSwitch:
    def __init__(self):
        self._events: list[dict] = []
        self._last_fetch = 0.0
        self._fetch_interval = CACHE_HOURS * 3600

    def _fetch_ff_calendar(self) -> list[dict]:
        """Fetch and parse ForexFactory calendar. Returns list of high-impact events."""
        try:
            import urllib.request
            req = urllib.request.Request(
                FF_URL,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning("FF fetch failed: %s", e)
            return []

        events = []
        # Parse date headers — FF uses <td class="calendar__date"> with data-date="YYYY-MM-DD"
        dates = {}
        for m in re.finditer(
            r'<td[^>]*class="calendar__date"[^>]*data-date="(\d{4}-\d{2}-\d{2})"',
            html,
        ):
            # Find the tbody following this date
            after = html[m.end():]
            tbody_match = re.search(r'<tbody[^>]*>(.*?)</tbody>', after, re.DOTALL)
            if tbody_match:
                date_str = m.group(1)
                dates[date_str] = tbody_match.group(1)

        for date_str, tbody in dates.items():
            # Parse each event row in this tbody
            for row_match in re.finditer(
                r'<tr[^>]*class="calendar__row"[^>]*data-event-id="(\d+)"(.*?)</tr>',
                tbody, re.DOTALL,
            ):
                row = row_match.group(2)

                # Impact level
                is_high = bool(re.search(r'impact--red|impact--orange', row))
                if not is_high:
                    continue

                # Time
                time_m = re.search(
                    r'<td[^>]*class="calendar__time"[^>]*>(.*?)</td>', row, re.DOTALL
                )
                time_str = ""
                if time_m:
                    time_str = re.sub(r'<[^>]+>', '', time_m.group(1)).strip()

                # Currency
                cur_m = re.search(
                    r'<td[^>]*class="calendar__currency"[^>]*>(.*?)</td>', row, re.DOTALL
                )
                currency = cur_m.group(1).strip() if cur_m else ""

                # Event name
                ev_m = re.search(
                    r'<td[^>]*class="calendar__event"[^>]*>(.*?)</td>', row, re.DOTALL
                )
                event_name = ""
                if ev_m:
                    event_name = re.sub(r'<[^>]+>', '', ev_m.group(1)).strip()
                    event_name = event_name.replace("&amp;", "&").replace("&#039;", "'")

                if not event_name or not time_str:
                    continue

                # Parse time
                try:
                    now = datetime.now(timezone.utc)
                    event_dt = datetime.strptime(
                        f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=timezone.utc)

                    # Events not yet passed or still within block window
                    if event_dt < now - timedelta(minutes=BLOCK_AFTER + 60):
                        continue
                except ValueError:
                    continue

                events.append({
                    "datetime": event_dt,
                    "currency": currency,
                    "event": event_name,
                    "impact": "high",
                    "date": date_str,
                    "time": time_str,
                })

                _event_str = event_name[:80]
                logger.debug("FF event: %s %s %s %s", date_str, time_str, currency, _event_str)

        return events

    def refresh(self) -> int:
        """Fetch calendar and cache high-impact events. Returns count."""
        raw = self._fetch_ff_calendar()
        # Filter to only high-impact events
        self._events = [
            e for e in raw
            if _is_high_impact(e["event"])
        ]
        self._last_fetch = time.time()
        active = sum(1 for e in self._events if e["datetime"] > datetime.now(timezone.utc))
        logger.info("EventKill: %d high-impact events cached (%d upcoming)", len(self._events), active)
        return len(self._events)

    def should_block(self, now: Optional[datetime] = None) -> Optional[dict]:
        """Check if we're inside a block window. Returns blocking event or None."""
        if time.time() - self._last_fetch > self._fetch_interval:
            try:
                self.refresh()
            except Exception as e:
                logger.warning("EventKill refresh failed: %s", e)

        if now is None:
            now = datetime.now(timezone.utc)

        for event in self._events:
            evt_dt = event["datetime"]
            delta_min = (now - evt_dt).total_seconds() / 60
            if -BLOCK_BEFORE <= delta_min <= BLOCK_AFTER:
                remaining = BLOCK_AFTER - delta_min if delta_min >= 0 else abs(delta_min)
                logger.info(
                    "EventKill BLOCK: %s (%s) — %.0f min %s",
                    event["event"], event["currency"],
                    remaining, "before" if delta_min < 0 else "after",
                )
                return event
        return None

    def next_event(self) -> Optional[dict]:
        """Return the next upcoming high-impact event."""
        now = datetime.now(timezone.utc)
        upcoming = [e for e in self._events if e["datetime"] > now]
        if not upcoming:
            return None
        return min(upcoming, key=lambda e: e["datetime"])
