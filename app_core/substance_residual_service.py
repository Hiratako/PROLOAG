from __future__ import annotations

from datetime import date, datetime, timedelta
import math
from typing import Any, Iterable

from app_core.diary_service import get_diary_entry_path, load_diary_entry
from app_core.substance_service import (
    CAFFEINE_SUBSTANCE_ID,
    default_substance_profiles,
    model_snapshot_for_profile,
    parse_substance_datetime,
    substance_profile_map,
    unified_substance_records,
)


DEFAULT_INTERVAL_MINUTES = 5
MAX_RESIDUAL_CUTOFF_HOURS = 720.0


def residual_amount_mg(
    amount_mg: Any,
    elapsed_hours: float,
    *,
    half_life_hours: float,
    peak_delay_minutes: int,
    residual_cutoff_hours: float,
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
    if not math.isfinite(amount) or amount <= 0 or elapsed_hours < 0:
        return 0.0
    if elapsed_hours >= residual_cutoff_hours:
        return 0.0

    peak_delay_hours = peak_delay_minutes / 60
    if peak_delay_hours > 0 and elapsed_hours < peak_delay_hours:
        return amount * elapsed_hours / peak_delay_hours
    decay_hours = max(elapsed_hours - peak_delay_hours, 0.0)
    return amount * (0.5 ** (decay_hours / half_life_hours))


def _record_model(record: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = record.get("model_snapshot")
    if isinstance(snapshot, dict):
        try:
            half_life = float(snapshot.get("half_life_hours"))
            peak_delay = int(snapshot.get("peak_delay_minutes"))
            cutoff = float(snapshot.get("residual_cutoff_hours"))
            if half_life > 0 and peak_delay >= 0 and cutoff > 0:
                return {
                    "half_life_hours": half_life,
                    "peak_delay_minutes": peak_delay,
                    "residual_cutoff_hours": cutoff,
                }
        except (TypeError, ValueError):
            pass
    fallback = profile or {
        "half_life_hours": 5.0,
        "peak_delay_minutes": 30,
        "residual_cutoff_hours": 36.0,
        "model_revision": 1,
    }
    return model_snapshot_for_profile(fallback)


def substance_residual_total_at(
    records: Iterable[dict[str, Any]],
    target_time: datetime,
    *,
    profile: dict[str, Any] | None = None,
) -> float:
    total = 0.0
    for record in records:
        taken_at = parse_substance_datetime(record.get("taken_at"))
        if taken_at is None or taken_at > target_time:
            continue
        model = _record_model(record, profile)
        elapsed_hours = (target_time - taken_at).total_seconds() / 3600
        total += residual_amount_mg(
            record.get("amount_mg"),
            elapsed_hours,
            half_life_hours=float(model["half_life_hours"]),
            peak_delay_minutes=int(model["peak_delay_minutes"]),
            residual_cutoff_hours=float(model["residual_cutoff_hours"]),
        )
    return total


def build_substance_residual_curve(
    records: Iterable[dict[str, Any]],
    start_time: datetime,
    end_time: datetime,
    *,
    profile: dict[str, Any] | None = None,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
) -> list[tuple[datetime, float]]:
    if end_time < start_time:
        start_time, end_time = end_time, start_time
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be greater than zero")

    rows = list(records)
    points = {start_time, end_time}
    interval = timedelta(minutes=interval_minutes)
    cursor = start_time
    while cursor <= end_time:
        points.add(cursor)
        cursor += interval

    for record in rows:
        taken_at = parse_substance_datetime(record.get("taken_at"))
        if taken_at is None:
            continue
        model = _record_model(record, profile)
        peak_at = taken_at + timedelta(minutes=int(model["peak_delay_minutes"]))
        cutoff_at = taken_at + timedelta(hours=float(model["residual_cutoff_hours"]))
        for point in (taken_at, peak_at, cutoff_at - timedelta(microseconds=1), cutoff_at):
            if start_time <= point <= end_time:
                points.add(point)

    return [
        (point, substance_residual_total_at(rows, point, profile=profile))
        for point in sorted(points)
    ]


def _date_range(start_date: date, end_date: date) -> Iterable[date]:
    cursor = start_date
    while cursor <= end_date:
        yield cursor
        cursor += timedelta(days=1)


def required_lookback_days(
    profiles: Iterable[dict[str, Any]],
    *,
    past_view_days: int = 6,
    max_display_half_span_hours: float = 36.0,
    snapshot_cutoff_ceiling_hours: float = MAX_RESIDUAL_CUTOFF_HOURS,
) -> int:
    current_maximum_cutoff = max(
        (float(item.get("residual_cutoff_hours", 0) or 0) for item in profiles),
        default=36.0,
    )
    maximum_cutoff = max(current_maximum_cutoff, float(snapshot_cutoff_ceiling_hours))
    required_hours = (
        maximum_cutoff
        + max(float(past_view_days), 0.0) * 24.0
        + max(float(max_display_half_span_hours), 0.0)
    )
    return max(1, int(math.ceil(required_hours / 24.0)))


def load_recent_substance_records(
    reference_time: datetime,
    *,
    lookback_days: int,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    profile_lookup = dict(substance_profile_map(include_inactive=True) if profiles is None else profiles)
    if CAFFEINE_SUBSTANCE_ID not in profile_lookup:
        profile_lookup[CAFFEINE_SUBSTANCE_ID] = next(
            item for item in default_substance_profiles() if item["substance_id"] == CAFFEINE_SUBSTANCE_ID
        )
    start_date = (reference_time - timedelta(days=max(lookback_days, 0))).date()
    records: list[dict[str, Any]] = []
    untimed_by_substance: dict[str, float] = {}

    for target_date in _date_range(start_date, reference_time.date()):
        if not get_diary_entry_path(target_date).exists():
            continue
        entry = load_diary_entry(target_date, profiles=profile_lookup)
        try:
            untimed = max(float(entry.get("caffeine_untimed_mg") or 0), 0.0)
        except (TypeError, ValueError):
            untimed = 0.0
        if untimed > 0:
            untimed_by_substance[CAFFEINE_SUBSTANCE_ID] = (
                untimed_by_substance.get(CAFFEINE_SUBSTANCE_ID, 0.0) + untimed
            )
        for record in unified_substance_records(entry, profiles=profile_lookup):
            taken_at = parse_substance_datetime(record.get("taken_at"))
            if taken_at is None or taken_at > reference_time:
                continue
            try:
                amount_mg = max(float(record.get("amount_mg") or 0), 0.0)
            except (TypeError, ValueError):
                continue
            if amount_mg <= 0:
                continue
            records.append({**record, "taken_at": taken_at, "amount_mg": amount_mg})

    records.sort(key=lambda record: (record["taken_at"], str(record.get("substance_record_id") or "")))
    return records, untimed_by_substance
