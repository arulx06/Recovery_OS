from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return naive UTC for the existing timezone-naive database schema."""
    return datetime.now(UTC).replace(tzinfo=None)
