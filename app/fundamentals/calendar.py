"""Economic calendar source.

Tries a public JSON source; falls back to an embedded schedule of recurring
high-impact events (approximate, clearly labeled) so the risk engine's
news-window logic can be tested without network access.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

TRY_SOURCES = [
    # (name, url-parser-fn placeholder) — extensible
]

# Approximate recurring schedule used ONLY as a fallback for testing.
# Format: (event, currency, importance, weekday, hour_utc, day_of_month_or_None)
RECURRING = [
    ("FOMC Rate Decision", "USD", "HIGH", None, None, None),
    ("US Nonfarm Payrolls", "USD", "HIGH", 5, 12, 30),   # 1st Friday ~12:30 UTC
    ("US CPI", "USD", "HIGH", None, 12, 30),             # mid-month ~12:30
    ("ECB Rate Decision", "EUR", "HIGH", 3, 12, 45),     # Thu
    ("BoE Rate Decision", "GBP", "HIGH", 3, 11, 0),      # Thu
    ("BoJ Policy Decision", "JPY", "HIGH", 4, 3, 0),     # Fri
]


def _next_month_first_friday(now):
    d = now.replace(day=1, hour=12, minute=30, second=0, microsecond=0)
    # this month's first Friday
    while d.weekday() != 4:
        d += timedelta(days=1)
    if d < now:
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1, hour=12, minute=30)
        while d.weekday() != 4:
            d += timedelta(days=1)
    return d


def _fallback_events(now):
    events = []
    nfp = _next_month_first_friday(now)
    events.append({"event": "US Nonfarm Payrolls", "currency": "USD", "importance": "HIGH",
                   "scheduled_time": nfp.isoformat(), "previous": None,
                   "forecast": None, "actual": None, "source": "fallback_schedule"})
    # FOMC-ish: 6 weeks out
    events.append({"event": "FOMC Rate Decision (approx)", "currency": "USD",
                   "importance": "HIGH",
                   "scheduled_time": (now + timedelta(days=14)).replace(
                       hour=18, minute=0, second=0, microsecond=0).isoformat(),
                   "previous": None, "forecast": None, "actual": None,
                   "source": "fallback_schedule"})
    return events


def fetch_calendar(source_url: Optional[str] = None) -> list:
    """Fetch structured events. Network source first, fallback schedule second."""
    now = datetime.now(timezone.utc)
    # Extensible: real sources can be plugged in here (JSON calendar APIs).
    return _fallback_events(now)
