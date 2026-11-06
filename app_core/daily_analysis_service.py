from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from app_core.analysis_dates import analysis_datetime_series


def build_calendar_study_totals(
    sessions: pd.DataFrame,
    start_date: date,
    end_date: date,
    *,
    moving_average_days: int = 7,
) -> pd.DataFrame:
    """Return calendar-day totals without creating or changing any stored records."""
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    calendar = pd.date_range(start_date, end_date, freq="D", name="date")
    columns = ["duration_sec", "money", "sessions"]
    if sessions.empty:
        daily = pd.DataFrame(0.0, index=calendar, columns=columns)
    else:
        work = sessions.copy()
        work["date"] = analysis_datetime_series(work["date"]).dt.normalize()
        daily = work.groupby("date").agg(
            duration_sec=("duration_sec", "sum"),
            money=("money", "sum"),
            sessions=("duration_sec", "count"),
        ).reindex(calendar, fill_value=0)
    daily["study_hours"] = daily["duration_sec"] / 3600
    window = max(int(moving_average_days), 1)
    daily["hours_ma"] = daily["study_hours"].rolling(window, min_periods=1).mean()
    daily["money_ma"] = daily["money"].rolling(window, min_periods=1).mean()
    daily["cum_hours"] = daily["study_hours"].cumsum()
    daily["cum_money"] = daily["money"].cumsum()
    return daily.reset_index()


def analysis_context_window(
    start_date: date,
    end_date: date,
    *,
    lookback_days: int = 1,
    lookahead_days: int = 1,
) -> tuple[date, date, list[date]]:
    """Return normalized display bounds and contiguous calculation dates.

    Callers calculate shifts/rolling values on all returned dates, then slice
    to the first two bounds. Context is not itself a recorded observation.
    """
    start_date, end_date = sorted((start_date, end_date))
    first = start_date - timedelta(days=max(int(lookback_days), 0))
    last = end_date + timedelta(days=max(int(lookahead_days), 0))
    dates = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    return start_date, end_date, dates
