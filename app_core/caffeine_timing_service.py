from __future__ import annotations

from datetime import date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from app_core.diary_service import get_diary_entry_path, load_diary_entry
from app_core.analysis_dates import parse_analysis_datetime
from app_core.study_log_service import load_study_sessions_by_date
from config import PROJECT_ROOT


def _number(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if isfinite(number) else 0.0


def _measurement(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def parse_record_datetime(value: Any) -> datetime | None:
    return parse_analysis_datetime(value)


def timing_band(minutes_since: float | None, has_untimed: bool) -> str:
    if minutes_since is None:
        return "時刻不明のみ" if has_untimed else "摂取前・記録なし"
    if minutes_since <= 30:
        return "0-30分"
    if minutes_since <= 90:
        return "31-90分"
    if minutes_since <= 180:
        return "91-180分"
    if minutes_since <= 360:
        return "181-360分"
    return "361分以上"


def caffeine_before_session(
    records: Iterable[dict[str, Any]],
    session_start: datetime,
    *,
    has_untimed: bool = False,
) -> dict[str, Any]:
    prior: list[tuple[datetime, float]] = []
    for record in records:
        taken_at = parse_record_datetime(record.get("taken_at"))
        amount = max(_number(record.get("amount_mg")), 0.0)
        if taken_at is not None and taken_at <= session_start and amount > 0:
            prior.append((taken_at, amount))
    prior.sort(key=lambda item: item[0])
    last_at, last_amount = prior[-1] if prior else (None, None)
    elapsed = (session_start - last_at).total_seconds() / 60 if last_at else None
    cutoff = session_start - timedelta(hours=6)
    return {
        "timing_band": timing_band(elapsed, has_untimed),
        "minutes_since_last_intake": elapsed,
        "last_intake_at": last_at,
        "last_intake_mg": last_amount,
        "caffeine_6h_mg": round(sum(amount for taken_at, amount in prior if taken_at >= cutoff), 6),
    }


def build_session_timing_frame(
    start_date: date,
    end_date: date,
    root: Path | None = None,
    *,
    preloaded_diaries: dict[date, dict[str, Any]] | None = None,
    preloaded_study_by_date: dict[date, list[dict[str, Any]]] | None = None,
) -> pd.DataFrame:
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    records: list[dict[str, Any]] = []
    untimed_by_date: dict[date, float] = {}
    # Include the preceding day so the first displayed day's six-hour window is complete.
    target = start_date - timedelta(days=1)
    while target <= end_date:
        if preloaded_diaries is not None or get_diary_entry_path(target).exists():
            diary = preloaded_diaries.get(target, {}) if preloaded_diaries is not None else load_diary_entry(target)
            records.extend(item for item in diary.get("caffeine_records", []) if isinstance(item, dict))
            untimed_by_date[target] = max(_number(diary.get("caffeine_untimed_mg")), 0.0)
        target += timedelta(days=1)

    rows: list[dict[str, Any]] = []
    target = start_date
    while target <= end_date:
        sessions = (
            preloaded_study_by_date.get(target, []) if preloaded_study_by_date is not None
            else load_study_sessions_by_date(target, root or PROJECT_ROOT)
        )
        for session in sessions:
            if not isinstance(session, dict):
                continue
            session_start = parse_record_datetime(session.get("start_at"))
            if session_start is None:
                continue
            condition = session.get("condition") if isinstance(session.get("condition"), dict) else {}
            duration_sec = _measurement(session.get("duration_sec"))
            rows.append(
                {
                    "session_start": session_start,
                    **caffeine_before_session(
                        records, session_start, has_untimed=untimed_by_date.get(target, 0.0) > 0,
                    ),
                    "duration_min": max(duration_sec, 0.0) / 60 if duration_sec is not None else None,
                    "effort": _measurement(condition.get("effort")),
                    "category": str(condition.get("category") or session.get("category") or "未設定"),
                    "io_type": str(condition.get("io_type") or session.get("io_type") or "未設定"),
                }
            )
        target += timedelta(days=1)
    return pd.DataFrame(rows)


def with_caffeine_recording_status(frame: pd.DataFrame, *, missing_as_zero: bool = False) -> pd.DataFrame:
    """Separate absent diary days from stored zero values without inferring new records."""
    result = frame.copy()
    recorded = result["has_diary_log"].fillna(False).astype(bool)
    if "caffeine_field_recorded" in result:
        recorded &= result["caffeine_field_recorded"].fillna(False).astype(bool)
    observed = result["is_observed_day"].fillna(False).astype(bool) if "is_observed_day" in result else pd.Series(True, index=result.index)
    recorded &= observed
    imputed = observed & ~recorded & bool(missing_as_zero)
    result["caffeine_recorded"] = recorded
    result["caffeine_imputed_zero"] = imputed
    # Compatibility name used by comparison tables; raw flags above stay intact.
    result["has_caffeine_log"] = recorded | imputed
    for column in (
        "caffeine_mg", "timed_caffeine_mg", "caffeine_untimed_mg",
        "caffeine_after_18_mg", "caffeine_record_count",
    ):
        if column in result:
            result[column] = pd.to_numeric(result[column], errors="coerce").where(recorded)
    result.loc[imputed, "caffeine_mg"] = 0.0
    result["has_caffeine"] = result["caffeine_mg"].gt(0)
    result["has_no_caffeine_record"] = (recorded | imputed) & result["caffeine_mg"].eq(0)
    return result
