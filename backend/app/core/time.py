from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    """Return naive UTC for the existing timezone-naive database schema."""
    return datetime.now(UTC).replace(tzinfo=None)


def end_of_local_day_utc(day: date, timezone_name: str) -> datetime:
    """Return naive UTC at the exclusive end of a merchant-local date."""
    local_deadline = datetime.combine(day + timedelta(days=1), time.min, ZoneInfo(timezone_name))
    return local_deadline.astimezone(UTC).replace(tzinfo=None)


def utc_to_local(instant: datetime, timezone_name: str) -> datetime:
    """Convert a naive-UTC database instant to naive merchant-local time."""
    return instant.replace(tzinfo=UTC).astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
