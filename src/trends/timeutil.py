"""Time conventions: the database stores naive UTC; business dates and display use Asia/Taipei."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, tzinfo
from zoneinfo import ZoneInfo

import pandas as pd


TAIPEI = ZoneInfo("Asia/Taipei")
PACIFIC = ZoneInfo("America/Los_Angeles")
DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_DB_DATETIME_FORMATS = {19: DB_DATETIME_FORMAT, 26: "%Y-%m-%d %H:%M:%S.%f"}


def parse_db_datetime(value: object) -> datetime | None:
    """Parse a stored timestamp without guessing: only the two known string layouts are accepted."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().replace(tzinfo=None)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    layout = _DB_DATETIME_FORMATS.get(len(text))
    if layout is None:
        raise ValueError(f"Unsupported timestamp layout: {text!r}")
    return datetime.strptime(text, layout)


def format_db_datetime(value: datetime) -> str:
    """Format a naive UTC (or aware) datetime as the canonical DB string."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.strftime(DB_DATETIME_FORMAT)


def local_to_utc(value: datetime, zone: tzinfo, strict: bool = True) -> datetime:
    """Interpret a naive wall-clock time in ``zone`` and return naive UTC.

    With ``strict`` the call refuses wall-clock times that are ambiguous (DST fall-back)
    or nonexistent (DST spring-forward), because no single UTC instant can be chosen safely.
    """
    if value.tzinfo is not None:
        raise ValueError("local_to_utc expects a naive datetime")
    earlier = value.replace(tzinfo=zone, fold=0)
    later = value.replace(tzinfo=zone, fold=1)
    if strict:
        if earlier.utcoffset() != later.utcoffset():
            raise ValueError(f"Ambiguous or nonexistent local time {value} in {zone}")
        if earlier.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != value:
            raise ValueError(f"Nonexistent local time {value} in {zone}")
    return earlier.astimezone(UTC).replace(tzinfo=None)


def utc_now() -> datetime:
    """Current time as naive UTC, matching SQLite CURRENT_TIMESTAMP."""
    return datetime.now(UTC).replace(tzinfo=None)


def utc_to_taipei(value: datetime) -> datetime:
    """Convert a naive UTC datetime to an aware Asia/Taipei datetime."""
    return value.replace(tzinfo=UTC).astimezone(TAIPEI)


def taipei_date(value: datetime) -> date:
    return utc_to_taipei(value).date()


def taipei_today() -> date:
    return datetime.now(TAIPEI).date()


def taipei_day_start_utc(day: date) -> datetime:
    """Naive UTC instant at which ``day`` starts in Asia/Taipei."""
    return datetime.combine(day, time.min, tzinfo=TAIPEI).astimezone(UTC).replace(tzinfo=None)


def utc_series_to_taipei_date(values: pd.Series) -> pd.Series:
    """Map naive-UTC timestamps (datetime or either DB string layout) to Taipei calendar days.

    Returns naive midnight timestamps so results can be compared with other day-level columns.
    """
    parsed = pd.to_datetime(values, errors="coerce", format="mixed")
    return parsed.dt.tz_localize(UTC).dt.tz_convert(TAIPEI).dt.tz_localize(None).dt.normalize()
