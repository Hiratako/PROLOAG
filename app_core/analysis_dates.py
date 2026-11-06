"""Read-only date interpretation shared by analysis and display paths.

Naive values denote the computer's local wall time. Offset-aware values are
converted to that same local time before their date/hour is used. This never
changes stored timestamps, source-file ownership, or edit snapshots.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any


def parse_analysis_datetime(value: Any) -> datetime | None:
    try:
        if isinstance(value, datetime):
            # pandas.NaT is a datetime subclass, but has a non-integer year.
            if not isinstance(value.year, int):
                return None
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, time.min)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            parsed = datetime.fromisoformat(text)
        else:
            # Numbers are not implicit Unix/nanosecond timestamps; booleans,
            # missing sentinels and containers are not dates either.
            return None
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_analysis_date(value: Any) -> date | None:
    parsed = parse_analysis_datetime(value)
    return parsed.date() if parsed is not None else None


def analysis_datetime_series(values: Any):
    """Parse heterogeneous ISO values individually, retaining index and NaT."""
    import pandas as pd

    source = values if isinstance(values, pd.Series) else pd.Series(values, dtype="object")
    parsed = pd.Series(
        [parse_analysis_datetime(value) for value in source],
        index=source.index, name=source.name, dtype="object",
    )
    return pd.to_datetime(parsed, errors="coerce")
