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

HIGH_IMPACT_KEYWORDS = [
    "cpi", "fomc", "non-farm", "nonfarm", "employment", "gdp",
    "unemployment rate", "fed interest", "nfp", "ppi",
    "retail sales", "industrial production",
    "consumer confidence", "ism manufact", "ism serv",
    "pmi", "housing starts", "consumer price", "producer price",
    "initial claims", "jobless claims",
    "philadelphia fed", "empire state",
    "average hourly earnings",
]


def _is_high_impact(event_name: str) -> bool:
    name = event_name.lower().strip()
    name = re.sub(r"[^a-z0-9\s]", "", name)
    return any(kw in name for kw in HIGH_IMPACT_KEYWORDS)


def _clean_html(html_text: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", html_text).strip()


def _parse_time(time_str: str) -> str:
    """Normalize FF time format to HH:MM (24-hour).
    Handles '9:00am', '1:00pm', '08:30', 'All Day', etc.
    """
    t = time_str.strip()
    t = re.sub(r"<[^>]+>", "", t).strip()
    if not t or "All Day" in t:
        return ""

    is_pm = "pm" in t.lower()
    is_am = "am" in t.lower()
    # Remove am/pm for parsing
    clean = re.sub(r"[ap]\.?m\.?", "", t, flags=re.IGNORECASE).strip()
    parts = clean.split(":")
    if len(parts) >= 2:
        try:
            h = int(parts[0])
            m = int(re.sub(r"[^\d]", "", parts[1])[:2])
            if is_pm and h < 12:
                h += 12
            if is_am and h == 12:
                h = 0
            return f"{h:02d}:{m:02d}"
        except ValueError:
            pass
    return clean


class EventKillSwitch:
    def __init__(self):
        self._events: list[dict] = []
        self._last_fetch = 0.0
        self._fetch_interval = CACHE_HOURS * 3600

    def _fetch_ff_calendar(self) -> list[dict]:
        """Fetch and parse ForexFactory calendar HTML for high-impact events."""
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
        current_date = ""

        # Find each event row by data-event-id
        for m in re.finditer(
            r'<tr[^>]*data-event-id="(\d+)"[^>]*class="calendar__row[^"]*"[^>]*>(.*?)</tr>',
            html, re.DOTALL,
        ):
            row_id = m.group(1)
            row_html = m.group(2)

            # Check for new date (row has calendar__row--new-day class)
            if 'calendar__row--new-day' in m.group(0):
                date_m = re.search(
                    r'<td[^>]*class="calendar__cell calendar__date"[^>]*>.*?<span[^>]*class="date"[^>]*>(.*?)</span>',
                    row_html, re.DOTALL,
                )
                if date_m:
                    current_date = date_m.group(1).strip()
                    current_date = re.sub(r"<[^>]+>", "", current_date).strip()
            else:
                # Check if there's a date cell anyway (first row of each date group)
                date_m = re.search(
                    r'<td[^>]*class="calendar__cell calendar__date"[^>]*>', row_html
                )
                if date_m:
                    # Extract just the date part
                    date_span = re.search(
                        r'<span[^>]*class="date"[^>]*>(.*?)</span>', row_html, re.DOTALL
                    )
                    if date_span:
                        current_date = re.sub(r"<[^>]+>", "", date_span.group(1)).strip()

            if not current_date:
                continue

            # Impact — check for red (high) or orange (medium-high)
            is_high = bool(re.search(r"icon--ff-impact-red", row_html))
            is_medium = bool(re.search(r"icon--ff-impact-ora", row_html))

            # Time
            time_m = re.search(
                r'<td[^>]*class="calendar__cell calendar__time"[^>]*>(.*?)</td>',
                row_html, re.DOTALL,
            )
            time_str = _parse_time(time_m.group(1)) if time_m else ""

            # Currency
            cur_m = re.search(
                r'<td[^>]*class="calendar__cell calendar__currency"[^>]*>(.*?)</td>',
                row_html, re.DOTALL,
            )
            currency = _clean_html(cur_m.group(1)) if cur_m else ""

            # Event name
            ev_m = re.search(
                r'<td[^>]*class="calendar__cell calendar__event"[^>]*>(.*?)</td>',
                row_html, re.DOTALL,
            )
            event_name = _clean_html(ev_m.group(1)) if ev_m else ""
            event_name = event_name.replace("&amp;", "&").replace("&#039;", "'").replace("&nbsp;", " ")

            if not event_name or not time_str or not is_high:
                continue

            # Only US events matter for our crypto portfolio
            if currency not in ("USD",):
                continue

            # Parse date — current_date is like "Fri Jul 10"
            try:
                now = datetime.now(timezone.utc)
                # Parse "Fri Jul 10" — we need to add the year
                date_parts = current_date.split()
                if len(date_parts) >= 3:
                    month = date_parts[-2]
                    day = date_parts[-1]
                    # Clean day (remove ordinal suffixes like 10th)
                    day = re.sub(r"[^\d]", "", day)
                    year = now.year
                    # Handle December -> January rollover
                    dt_str = f"{month} {day} {year} {time_str}"
                    event_dt = datetime.strptime(dt_str, "%b %d %Y %H:%M")
                    event_dt = event_dt.replace(tzinfo=timezone.utc)

                    # If event seems more than 2 weeks in the past, skip
                    if event_dt < now - timedelta(days=14):
                        continue
                    # If event seems more than 2 weeks in the future, skip (we reload weekly)
                    if event_dt > now + timedelta(days=14):
                        continue
                else:
                    continue
            except (ValueError, IndexError) as e:
                logger.debug("FF date parse error: %s %s", current_date, e)
                continue

            events.append({
                "datetime": event_dt,
                "currency": currency,
                "event": event_name,
                "impact": "high",
                "date": current_date,
                "time": time_str,
                "id": row_id,
            })

        logger.debug("FF: %d high-impact USD events found in HTML", len(events))
        return events

    def refresh(self) -> int:
        """Fetch calendar and cache high-impact events. Returns count."""
        raw = self._fetch_ff_calendar()
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
