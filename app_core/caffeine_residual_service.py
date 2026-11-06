from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

from app_core.diary_service import get_diary_entry_path, load_diary_entry


DEFAULT_HALF_LIFE_HOURS = 5.0
DEFAULT_PEAK_DELAY_MINUTES = 30
DEFAULT_RESIDUAL_CUTOFF_HOURS = 36.0
DEFAULT_LOOKBACK_DAYS = 7


def parse_caffeine_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def caffeine_residual_mg(
    amount_mg: Any,
    elapsed_hours: float,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
    peak_delay_minutes: int = DEFAULT_PEAK_DELAY_MINUTES,
    residual_cutoff_hours: float = DEFAULT_RESIDUAL_CUTOFF_HOURS,
) -> float:
    if half_life_hours <= 0:
        raise ValueError("half_life_hours must be greater than zero")
    if peak_delay_minutes < 0:
        raise ValueError("peak_delay_minutes must not be negative")
    if residual_cutoff_hours <= 0:
        raise ValueError("residual_cutoff_hours must be greater than zero")
    try:
        amount = max(float(amount_mg or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0
    if amount <= 0 or elapsed_hours < 0:
        return 0.0
    if elapsed_hours >= residual_cutoff_hours:
        return 0.0

    peak_delay_hours = peak_delay_minutes / 60
    if peak_delay_hours > 0 and elapsed_hours < peak_delay_hours:
        return amount * elapsed_hours / peak_delay_hours
    decay_hours = max(elapsed_hours - peak_delay_hours, 0.0)
    return amount * (0.5 ** (decay_hours / half_life_hours))


def caffeine_residual_total_at(
    records: Iterable[dict[str, Any]],
    target_time: datetime,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
    peak_delay_minutes: int = DEFAULT_PEAK_DELAY_MINUTES,
    residual_cutoff_hours: float = DEFAULT_RESIDUAL_CUTOFF_HOURS,
) -> float:
    total = 0.0
    for record in records:
        taken_at = parse_caffeine_datetime(record.get("taken_at"))
        if taken_at is None or taken_at > target_time:
            continue
        elapsed_hours = (target_time - taken_at).total_seconds() / 3600
        total += caffeine_residual_mg(
            record.get("amount_mg"),
            elapsed_hours,
            half_life_hours,
            peak_delay_minutes,
            residual_cutoff_hours,
        )
    return total


def build_caffeine_residual_curve(
    records: Iterable[dict[str, Any]],
    start_time: datetime,
    end_time: datetime,
    *,
    interval_minutes: int = 5,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
    peak_delay_minutes: int = DEFAULT_PEAK_DELAY_MINUTES,
    residual_cutoff_hours: float = DEFAULT_RESIDUAL_CUTOFF_HOURS,
) -> list[tuple[datetime, float]]:
    if end_time < start_time:
        start_time, end_time = end_time, start_time
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be greater than zero")

    records = list(records)
    points = {start_time, end_time}
    cursor = start_time
    interval = timedelta(minutes=interval_minutes)
    while cursor <= end_time:
        points.add(cursor)
        cursor += interval

    # Include intake, peak, and cutoff exactly even when they fall between interval points.
    for record in records:
        taken_at = parse_caffeine_datetime(record.get("taken_at"))
        if taken_at is None:
            continue
        if start_time <= taken_at <= end_time:
            points.add(taken_at)
        peak_at = taken_at + timedelta(minutes=peak_delay_minutes)
        if start_time <= peak_at <= end_time:
            points.add(peak_at)
        cutoff_at = taken_at + timedelta(hours=residual_cutoff_hours)
        before_cutoff = cutoff_at - timedelta(microseconds=1)
        if start_time <= before_cutoff <= end_time:
            points.add(before_cutoff)
        if start_time <= cutoff_at <= end_time:
            points.add(cutoff_at)

    return [
        (
            point,
            caffeine_residual_total_at(
                records,
                point,
                half_life_hours,
                peak_delay_minutes,
                residual_cutoff_hours,
            ),
        )
        for point in sorted(points)
    ]


def _date_range(start_date: date, end_date: date) -> Iterable[date]:
    cursor = start_date
    while cursor <= end_date:
        yield cursor
        cursor += timedelta(days=1)


def load_recent_caffeine_records(
    reference_time: datetime,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> tuple[list[dict[str, Any]], float]:
    start_date = (reference_time - timedelta(days=max(lookback_days, 0))).date()
    records: list[dict[str, Any]] = []
    untimed_mg = 0.0

    for target_date in _date_range(start_date, reference_time.date()):
        if not get_diary_entry_path(target_date).exists():
            continue
        entry = load_diary_entry(target_date)
        try:
            untimed_mg += max(float(entry.get("caffeine_untimed_mg") or 0), 0.0)
        except (TypeError, ValueError):
            pass
        for record in entry.get("caffeine_records", []):
            if not isinstance(record, dict):
                continue
            taken_at = parse_caffeine_datetime(record.get("taken_at"))
            if taken_at is None or taken_at > reference_time:
                continue
            try:
                amount_mg = max(float(record.get("amount_mg") or 0), 0.0)
            except (TypeError, ValueError):
                continue
            if amount_mg <= 0:
                continue
            records.append({**record, "taken_at": taken_at, "amount_mg": amount_mg})

    records.sort(key=lambda record: record["taken_at"])
    return records, untimed_mg
