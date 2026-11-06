from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from app_core.analysis_statistics import (
    classify_evidence_level,
    robust_scale,
    summarize_difference,
    summarize_rate_difference,
)
from app_core.app_clock import app_today
from app_core.analysis_dates import parse_analysis_datetime
from app_core.daily_analysis_service import analysis_context_window
from app_core.diary_service import LEGACY_CAFFEINE_PROFILE, normalize_caffeine_records
from app_core.substance_service import normalize_substance_records
from app_core.json_store import load_json_safe, record_json_files
from app_core.study_record_reader import unpack_study_document
from app_core.study_category_service import (load_study_category_catalog, default_category_catalog,
    study_category_names, resolve_study_category, other_category_name)
from config import PROJECT_ROOT


STUDY_CATEGORIES = ("医学", "英語", "語学", "資格", "IT", "読書", "仕事", "その他")
DATA_FOLDERS = ("study_logs", "daily_plans", "diary_entries", "health_logs", "exercise_logs")

FACTOR_LABELS = {
    "caffeine": "カフェイン記録",
    "running": "ランニング記録",
    "good_seat": "よい席を取れたか",
    "sleep": "前夜の睡眠時間",
    "plan_load": "予定学習時間",
    "morning_sleepiness": "朝の眠気",
}

MATCHING_LABELS = {
    "sleep_hours": "前夜睡眠時間",
    "morning_sleepiness": "朝の眠気",
    "previous_fatigue": "前日の疲労感",
    "planned_hours": "予定学習時間",
    "prior_7d_study_hours": "直近7日学習量",
    "previous_study_hours": "前日の学習時間",
}

DEFAULT_PROFILE_SPECS = (
    {"column": "sleep_hours", "label": "前夜睡眠時間", "unit": "時間"},
    {"column": "sleep_quality", "label": "睡眠の質", "unit": "/ 5"},
    {"column": "morning_sleepiness", "label": "朝の眠気", "unit": "/ 5"},
    {"column": "caffeine_mg", "label": "カフェイン", "unit": "mg"},
    {"column": "planned_hours", "label": "予定学習時間", "unit": "時間"},
    {"column": "previous_fatigue", "label": "前日の疲労感", "unit": "/ 5"},
    {"column": "previous_study_hours", "label": "前日の学習時間", "unit": "時間"},
    {"column": "avg_effort", "label": "平均頑張り度", "unit": "/ 5"},
    {"column": "output_ratio", "label": "アウトプット比率", "unit": "%"},
    {"column": "night_study_hours", "label": "夜間学習時間", "unit": "時間"},
    {"column": "running_recorded", "label": "ランニング記録率", "unit": "pt", "multiplier": 100.0, "statistic": "rate"},
)

PERFORMANCE_METRIC_COLUMNS = (
    "study_hours", "output_hours", "output_ratio", "plan_execution_rate",
    "task_completion_rate", "focus_score", "night_study_hours",
    *(f"study_category_{category}_hours" for category in STUDY_CATEGORIES),
)


def _root(root: Path | None = None) -> Path:
    return root or PROJECT_ROOT


def _daily_path(root: Path, folder: str, target: date) -> Path:
    return root / folder / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"


def _parse_file_date(path: Path) -> date | None:
    token = path.stem
    try:
        return date(int(token[:4]), int(token[4:6]), int(token[6:8]))
    except (TypeError, ValueError):
        return None


def _date_range(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _number(value: Any, default: float = np.nan) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if np.isfinite(number) else default


def _mean(values: Iterable[Any]) -> float:
    clean = pd.to_numeric(pd.Series(list(values), dtype="object"), errors="coerce").dropna()
    return float(clean.mean()) if len(clean) else np.nan


def available_date_bounds(root: Path | None = None) -> tuple[date | None, date | None]:
    base = _root(root)
    dates: list[date] = []
    for folder in DATA_FOLDERS:
        directory = base / folder
        if not directory.exists():
            continue
        dates.extend(parsed for path in record_json_files(directory) if (parsed := _parse_file_date(path)))
    return (min(dates), max(dates)) if dates else (None, None)


def _study_summary(path: Path, *, observed: bool = True) -> dict[str, Any]:
    recorded = observed and path.exists()
    raw = load_json_safe(path, []) if recorded else []
    document = unpack_study_document(raw)
    return summarize_study_records(
        [item for _, item in document.records],
        recorded=recorded and document.container_kind != "unsupported" and not document.ignored_items,
    )


def load_study_analysis_context(
    root: Path, targets: list[date], observed_through: date,
) -> tuple[dict[date, list[dict[str, Any]]], set[date]]:
    """Read adjacent owner files once, grouping immutable records by local start.

    Offset conversion may cross up to two calendar dates. An explicit empty
    list is observed zero; a missing/invalid document or timestamp is not.
    """
    by_date: dict[date, list[dict[str, Any]]] = {target: [] for target in targets}
    recorded: set[date] = set()
    if not targets:
        return by_date, recorded
    for owner in _date_range(min(targets) - timedelta(days=2), max(targets) + timedelta(days=2)):
        path = _daily_path(root, "study_logs", owner)
        if not path.exists():
            continue
        document = unpack_study_document(load_json_safe(path, []))
        if document.container_kind == "unsupported":
            continue
        if not document.records and not document.ignored_items and owner in by_date and owner <= observed_through:
            recorded.add(owner)
        for _, session in document.records:
            started = parse_analysis_datetime(session.get("start_at") or session.get("end_at"))
            if started is not None and started.date() in by_date and started.date() <= observed_through:
                by_date[started.date()].append(session)
                recorded.add(started.date())
    return by_date, recorded


def summarize_study_records(sessions: list[dict[str, Any]], *, recorded: bool, category_catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    """Summarize raw observations without filling absent measurements with zero."""
    category_catalog = default_category_catalog() if category_catalog is None else category_catalog
    other = other_category_name(category_catalog)
    categories = tuple(dict.fromkeys((*study_category_names(category_catalog, include_inactive=True), other)))
    if not recorded:
        return {
            "study_recorded": False,
            "study_hours": np.nan,
            "study_session_count": np.nan,
            "night_study_hours": np.nan,
            "avg_effort": np.nan,
            "input_hours": np.nan,
            "output_hours": np.nan,
            "unset_io_hours": np.nan,
            "output_ratio": np.nan,
            **{f"study_category_{category}_hours": np.nan for category in categories},
        }

    duration_sec = 0.0
    night_sec = 0.0
    efforts: list[float] = []
    input_sec = 0.0
    output_sec = 0.0
    unset_io_sec = 0.0
    category_sec = {category: 0.0 for category in categories}

    for session in sessions:
        duration = _number(session.get("duration_sec"))
        duration = max(duration, 0.0) if not np.isnan(duration) else np.nan
        duration_sec += duration
        start_at = parse_analysis_datetime(session.get("start_at"))
        if start_at and start_at.hour >= 18:
            night_sec += duration
        elif start_at is None:
            night_sec = np.nan

        condition = session.get("condition") if isinstance(session.get("condition"), dict) else {}
        effort = _number(condition.get("effort"))
        if not np.isnan(effort):
            efforts.append(effort)

        category = str(condition.get("category") or session.get("category") or "").strip()
        category = resolve_study_category(category, category_catalog, unknown=other)
        category_sec[category] += duration

        io_type = str(condition.get("io_type") or session.get("io_type") or "").strip().lower()
        if io_type in ("インプット", "input"):
            input_sec += duration
        elif io_type in ("アウトプット", "output"):
            output_sec += duration
        elif io_type in ("両方", "both"):
            input_sec += duration / 2
            output_sec += duration / 2
        else:
            unset_io_sec += duration

    io_total = input_sec + output_sec
    return {
        "study_recorded": True,
        "study_hours": duration_sec / 3600,
        "study_session_count": len(sessions),
        "night_study_hours": night_sec / 3600,
        "avg_effort": _mean(efforts),
        "input_hours": input_sec / 3600,
        "output_hours": output_sec / 3600,
        "unset_io_hours": unset_io_sec / 3600,
        "output_ratio": output_sec / io_total * 100 if io_total > 0 else np.nan,
        **{f"study_category_{category}_hours": seconds / 3600 for category, seconds in category_sec.items()},
    }


def _plan_summary(path: Path, *, observed: bool = True) -> dict[str, Any]:
    recorded = path.exists()
    raw = load_json_safe(path, {}) if recorded else {}
    raw = raw if isinstance(raw, dict) else {}
    tasks = [item for item in raw.get("tasks", []) if isinstance(item, dict)]
    task_minutes = sum(max(_number(item.get("planned_minutes"), 0.0), 0.0) for item in tasks)
    target_minutes = max(_number(raw.get("target_total_minutes"), 0.0), 0.0)
    planned_minutes = task_minutes if task_minutes > 0 else target_minutes
    completed = sum(1 for item in tasks if str(item.get("status") or "").lower() == "done")
    return {
        "plan_recorded": recorded,
        "planned_hours": planned_minutes / 60 if recorded else np.nan,
        "task_count": len(tasks) if recorded else np.nan,
        "completed_task_count": completed if recorded and observed else np.nan,
        "task_completion_rate": completed / len(tasks) * 100 if tasks and observed else np.nan,
    }


def caffeine_daily_measurement(raw: dict[str, Any], target: date) -> float:
    """Read legacy/current intake schemas without profile I/O or invented zero.

    Timed entries use the same normalization and alias IDs as the diary. The
    built-in profile is sufficient: pharmacokinetic parameters do not affect mg.
    """
    generic = normalize_substance_records(raw.get("substance_records", []), target,
        profiles={"caffeine": LEGACY_CAFFEINE_PROFILE})
    try:
        generic_schema = int(raw.get("substance_records_schema_version", 0) or 0) >= 1
    except (TypeError, ValueError):
        generic_schema = False
    records = [item for item in generic if item.get("substance_id") == "caffeine"]
    if not generic_schema:
        seen = {item["substance_record_id"] for item in generic}
        for item in normalize_caffeine_records(raw.get("caffeine_records", []), target,
                                              caffeine_profile=LEGACY_CAFFEINE_PROFILE):
            if item["caffeine_id"] not in seen:
                records.append(item)
                seen.add(item["caffeine_id"])
    timed = sum(item["amount_mg"] for item in records)
    untimed = _number(raw.get("caffeine_untimed_mg"))
    mirror = _number(raw.get("caffeine_mg"))
    if np.isnan(mirror):
        mirror = _number(raw.get("caffeine_cups")) * 100
    if np.isfinite(untimed) and untimed >= 0:
        return round(timed + untimed, 6)
    if np.isfinite(mirror) and mirror >= 0:
        return round(max(timed, mirror), 6)
    return round(timed, 6) if records else np.nan


def _diary_summary(path: Path, *, observed: bool = True, missing_as_zero: bool = False) -> dict[str, Any]:
    recorded = observed and path.exists()
    raw = load_json_safe(path, {}) if recorded else {}
    if isinstance(raw, list):
        raw = raw[-1] if raw else {}
    raw = raw if isinstance(raw, dict) else {}
    field_present = any(key in raw for key in ("caffeine_mg", "caffeine_cups", "caffeine_untimed_mg", "caffeine_records", "substance_records"))
    caffeine = caffeine_daily_measurement(raw, _parse_file_date(path) or date.min)

    caffeine_recorded = recorded and field_present and not np.isnan(caffeine)
    imputed = observed and not caffeine_recorded and missing_as_zero
    if imputed:
        caffeine = 0.0
        status = "未記録（0mg扱い）"
    elif not recorded:
        status = "日記記録なし"
    elif not field_present or np.isnan(caffeine):
        status = "カフェイン未明示"
    elif caffeine <= 0:
        status = "0mgまたは初期値"
    elif caffeine <= 100:
        status = "1-100mg"
    elif caffeine <= 200:
        status = "101-200mg"
    else:
        status = "201mg以上"
    if "good_seat_status" in raw:
        good_seat_raw = raw.get("good_seat_status")
        good_seat_status = (
            good_seat_raw
            if isinstance(good_seat_raw, str) and good_seat_raw in {"yes", "so_so", "no"}
            else None
        )
    else:
        # Backward compatibility for diary files created before the three-state field.
        legacy_seat_value = raw.get("good_seat_obtained")
        good_seat_status = (
            "yes" if legacy_seat_value is True else "no" if legacy_seat_value is False else None
        )
    good_seat_obtained = (
        True if good_seat_status == "yes" else False if good_seat_status == "no" else None
    )
    return {
        "diary_recorded": recorded,
        "caffeine_field_present": field_present,
        "caffeine_recorded": caffeine_recorded,
        "caffeine_imputed_zero": imputed,
        "caffeine_mg": caffeine,
        "caffeine_status": status,
        "good_seat_status": good_seat_status,
        "good_seat_obtained": good_seat_obtained,
    }


def _health_summary(path: Path, *, observed: bool = True) -> dict[str, Any]:
    recorded = observed and path.exists()
    raw = load_json_safe(path, {}) if recorded else {}
    raw = raw if isinstance(raw, dict) else {}
    sleep = raw.get("sleep") if isinstance(raw.get("sleep"), dict) else {}
    condition = raw.get("condition") if isinstance(raw.get("condition"), dict) else {}
    sleep_minutes = _number(sleep.get("sleep_duration_min")) if recorded else np.nan
    return {
        "health_recorded": recorded,
        "sleep_hours": sleep_minutes / 60 if not np.isnan(sleep_minutes) else np.nan,
        "sleep_quality": _number(sleep.get("sleep_quality")) if recorded else np.nan,
        "morning_sleepiness": _number(sleep.get("morning_sleepiness")) if recorded else np.nan,
        "midnight_awakenings": _number(sleep.get("midnight_awakenings")) if recorded else np.nan,
        "nap_minutes": _number(sleep.get("nap_minutes")) if recorded else np.nan,
        "physical_score": _number(condition.get("physical_score")) if recorded else np.nan,
        "fatigue": _number(condition.get("fatigue")) if recorded else np.nan,
        "stress": _number(condition.get("stress")) if recorded else np.nan,
        "focus_score": _number(condition.get("focus_score")) if recorded else np.nan,
        "mood_score": _number(condition.get("mood_score")) if recorded else np.nan,
    }


def _exercise_summary(path: Path, *, observed: bool = True) -> dict[str, Any]:
    log_exists = observed and path.exists()
    raw = load_json_safe(path, {}) if log_exists else {}
    raw = raw if isinstance(raw, dict) else {}
    source_records = raw.get("records")
    records = [item for item in source_records if isinstance(item, dict)] if isinstance(source_records, list) else []
    running = [item for item in records if str(item.get("type") or "").lower() == "running"]
    running_status_recorded = log_exists and isinstance(source_records, list) and all(
        isinstance(item, dict) and bool(str(item.get("type") or "").strip()) for item in source_records
    )
    if running:
        duration = sum(max(_number(item.get("duration_min")), 0.0) for item in running)
        distance = sum(max(_number(item.get("distance_km")), 0.0) for item in running)
        intensity = _mean(_number(item.get("intensity")) for item in running)
        fatigue_after = _mean(_number(item.get("fatigue_after")) for item in running)
    else:
        duration = distance = intensity = fatigue_after = np.nan
    return {
        "exercise_log_exists": log_exists,
        "running_status_recorded": running_status_recorded or bool(running),
        "exercise_record_count": len(records) if log_exists else np.nan,
        "running_recorded": bool(running),
        "running_count": len(running) if running_status_recorded else (len(running) if running else np.nan),
        "running_duration_min": duration,
        "running_distance_km": distance,
        "running_avg_intensity": intensity,
        "running_avg_fatigue_after": fatigue_after,
    }


def build_daily_feature_frame(
    start_date: date,
    end_date: date,
    root: Path | None = None,
    *,
    as_of_date: date | None = None,
    caffeine_missing_as_zero: bool = False,
) -> pd.DataFrame:
    """Build lagged features with context, then return only the requested display dates."""
    base = _root(root)
    category_catalog = load_study_category_catalog(base)
    start_date, end_date, targets = analysis_context_window(start_date, end_date, lookback_days=28, lookahead_days=1)
    observed_through = as_of_date or app_today()
    studies, study_recorded = load_study_analysis_context(base, targets, observed_through)
    rows: list[dict[str, Any]] = []
    for target in targets:
        observed = target <= observed_through
        row: dict[str, Any] = {
            "date": target,
            "weekday": target.weekday(),
            "is_weekend": target.weekday() >= 5,
        }
        row.update(summarize_study_records(studies[target], recorded=target in study_recorded, category_catalog=category_catalog))
        row.update(_plan_summary(_daily_path(base, "daily_plans", target), observed=observed))
        row.update(_diary_summary(_daily_path(base, "diary_entries", target), observed=observed, missing_as_zero=caffeine_missing_as_zero))
        row.update(_health_summary(_daily_path(base, "health_logs", target), observed=observed))
        row.update(_exercise_summary(_daily_path(base, "exercise_logs", target), observed=observed))
        if row["plan_recorded"] and row["planned_hours"] > 0 and row["study_recorded"]:
            row["plan_execution_rate"] = row["study_hours"] / row["planned_hours"] * 100
            row["study_plan_gap_hours"] = row["study_hours"] - row["planned_hours"]
        else:
            row["plan_execution_rate"] = np.nan
            row["study_plan_gap_hours"] = np.nan
        rows.append(row)

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.sort_values("date").reset_index(drop=True)

    next_columns = (
        "sleep_hours",
        "sleep_quality",
        "morning_sleepiness",
        "fatigue",
        "stress",
        "focus_score",
        "mood_score",
        "physical_score",
        "study_hours",
    )
    for column in next_columns:
        frame[f"next_{column}"] = frame[column].shift(-1)
    for column in ("fatigue", "stress", "focus_score", "study_hours", "caffeine_mg"):
        frame[f"previous_{column}"] = frame[column].shift(1)
    frame["prior_7d_study_hours"] = frame["study_hours"].shift(1).rolling(7, min_periods=3).mean()
    category_metrics = tuple(f"study_category_{category}_hours" for category in
                            dict.fromkeys((*study_category_names(category_catalog, include_inactive=True), other_category_name(category_catalog))))
    metrics = tuple(column for column in PERFORMANCE_METRIC_COLUMNS if not column.startswith("study_category_")) + category_metrics
    for column in metrics:
        frame[f"prior_28d_{column}_median"] = (
            frame[column].shift(1).rolling(28, min_periods=7).median()
        )
    display_dates = frame["date"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))
    return calculate_recovery_balance(frame.loc[display_dates].reset_index(drop=True))


def calculate_recovery_balance(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    components = {
        "next_focus_score": 1.0,
        "next_fatigue": -1.0,
        "next_morning_sleepiness": -1.0,
        "next_stress": -1.0,
    }
    standardized: dict[str, pd.Series] = {}
    for column, direction in components.items():
        if column not in result:
            continue
        values = pd.to_numeric(result[column], errors="coerce")
        deviation = values.std(ddof=0)
        if pd.isna(deviation) or deviation <= 0:
            z_score = values.where(values.isna(), 0.0)
        else:
            z_score = (values - values.mean()) / deviation
        standardized[column] = z_score * direction
    if not standardized:
        result["next_condition_balance"] = np.nan
        result["next_condition_component_count"] = 0
        return result
    score_frame = pd.DataFrame(standardized, index=result.index)
    counts = score_frame.notna().sum(axis=1)
    result["next_condition_balance"] = score_frame.mean(axis=1, skipna=True).where(counts >= 2)
    result["next_condition_component_count"] = counts
    return result


def calculate_performance_delta(frame: pd.DataFrame, metric_column: str, mode: str) -> pd.Series:
    values = pd.to_numeric(frame.get(metric_column), errors="coerce")
    if mode == "個人中央値差":
        return values - values.median()
    if mode == "直近28日中央値差":
        baseline_column = f"prior_28d_{metric_column}_median"
        baseline = (
            pd.to_numeric(frame[baseline_column], errors="coerce")
            if baseline_column in frame
            else values.shift(1).rolling(28, min_periods=7).median()
        )
        return values - baseline
    return values


def summarize_high_performance_profile(
    frame: pd.DataFrame,
    outcome_column: str,
    *,
    profile_specs: Iterable[dict[str, Any]] = DEFAULT_PROFILE_SPECS,
) -> tuple[pd.DataFrame, float | None]:
    outcome = pd.to_numeric(frame.get(outcome_column), errors="coerce")
    valid = frame.loc[outcome.notna()].copy()
    if len(valid) < 2:
        return pd.DataFrame(), None
    threshold = float(pd.to_numeric(valid[outcome_column], errors="coerce").quantile(0.75))
    high = valid.loc[pd.to_numeric(valid[outcome_column], errors="coerce") >= threshold]
    other = valid.loc[pd.to_numeric(valid[outcome_column], errors="coerce") < threshold]

    rows: list[dict[str, Any]] = []
    for position, spec in enumerate(profile_specs):
        column = str(spec.get("column") or "")
        if column not in valid:
            continue
        high_values = pd.to_numeric(high[column], errors="coerce").dropna()
        other_values = pd.to_numeric(other[column], errors="coerce").dropna()
        if not len(high_values) or not len(other_values):
            continue
        is_rate = spec.get("statistic") == "rate"
        summary = (
            summarize_rate_difference(high_values, other_values, seed=20260711 + position)
            if is_rate
            else summarize_difference(high_values, other_values, seed=20260711 + position)
        )
        scale = robust_scale(pd.concat([high_values, other_values], ignore_index=True))
        multiplier = float(spec.get("multiplier") or 1.0)
        difference = summary["rate_difference" if is_rate else "median_difference"]
        high_value = summary["group_a_rate" if is_rate else "group_a_median"]
        other_value = summary["group_b_rate" if is_rate else "group_b_median"]
        low = summary["interval_low"]
        high_interval = summary["interval_high"]
        rows.append(
            {
                "metric": column,
                "label": spec.get("label") or column,
                "unit": spec.get("unit") or "",
                "high_n": summary["group_a_n"],
                "other_n": summary["group_b_n"],
                "statistic": "rate" if is_rate else "median",
                "comparison_label": "記録率差" if is_rate else "中央値差",
                "high_value": high_value * multiplier,
                "other_value": other_value * multiplier,
                "difference": difference * multiplier if difference is not None else np.nan,
                "interval_low": low * multiplier if low is not None else np.nan,
                "interval_high": high_interval * multiplier if high_interval is not None else np.nan,
                "standardized_difference": difference / scale if difference is not None and scale else np.nan,
                "standardized_low": low / scale if low is not None and scale else np.nan,
                "standardized_high": high_interval / scale if high_interval is not None and scale else np.nan,
                "coverage_pct": (len(high_values) + len(other_values)) / len(valid) * 100,
                "evidence_level": summary["evidence_level"],
            }
        )
    return pd.DataFrame(rows), threshold


def factor_groups(frame: pd.DataFrame, factor_key: str) -> tuple[pd.Series, dict[str, Any]]:
    groups = pd.Series(pd.NA, index=frame.index, dtype="object")
    metadata: dict[str, Any] = {
        "factor_key": factor_key,
        "factor_label": FACTOR_LABELS.get(factor_key, factor_key),
        "group_a_label": "条件A",
        "group_b_label": "条件B",
        "matching_columns": [],
    }
    if factor_key == "caffeine":
        eligible = frame["diary_recorded"].fillna(False) | frame.get("caffeine_imputed_zero", pd.Series(False, index=frame.index)).fillna(False)
        valid = eligible & pd.to_numeric(frame["caffeine_mg"], errors="coerce").notna()
        groups.loc[valid & (pd.to_numeric(frame["caffeine_mg"], errors="coerce") > 0)] = "A"
        groups.loc[valid & (pd.to_numeric(frame["caffeine_mg"], errors="coerce") <= 0)] = "B"
        metadata.update(
            {
                "group_a_label": "0mg超の摂取記録あり",
                "group_b_label": "0mg（現在の欠測設定を適用）",
                "matching_columns": ["sleep_hours", "morning_sleepiness", "previous_fatigue", "planned_hours", "prior_7d_study_hours"],
            }
        )
    elif factor_key == "running":
        recorded = frame["running_recorded"].fillna(False)
        valid = frame.get("running_status_recorded", frame.get("exercise_log_exists", recorded.notna())).fillna(False)
        groups.loc[valid & recorded] = "A"
        groups.loc[valid & ~recorded] = "B"
        metadata.update(
            {
                "group_a_label": "ランニング記録あり",
                "group_b_label": "運動記録あり・ランニングなし",
                "exclusion_note": "運動未記録・ランニング有無不明の日は両群から除外",
                "matching_columns": ["sleep_hours", "morning_sleepiness", "previous_fatigue", "planned_hours", "prior_7d_study_hours"],
            }
        )
    elif factor_key == "good_seat":
        if "good_seat_status" in frame:
            values = frame["good_seat_status"]
        else:
            legacy_values = frame.get(
                "good_seat_obtained",
                pd.Series(pd.NA, index=frame.index, dtype="object"),
            )
            values = legacy_values.map(
                lambda value: (
                    "yes" if bool(value) else "no"
                ) if isinstance(value, (bool, np.bool_)) else pd.NA
            )
        groups.loc[values.eq("yes")] = "A"
        groups.loc[values.eq("no")] = "B"
        metadata.update(
            {
                "group_a_label": "よい席を取れた（Yes）",
                "group_b_label": "よい席を取れなかった（No）",
                "exclusion_note": "So-so・未記録は比較対象外",
                "matching_columns": ["sleep_hours", "morning_sleepiness", "previous_fatigue", "planned_hours", "prior_7d_study_hours"],
            }
        )
    elif factor_key == "sleep":
        values = pd.to_numeric(frame["sleep_hours"], errors="coerce")
        median = values.median()
        if pd.notna(median):
            groups.loc[values >= median] = "A"
            groups.loc[values < median] = "B"
        metadata.update(
            {
                "group_a_label": "個人中央値以上",
                "group_b_label": "個人中央値未満",
                "threshold": median,
                "matching_columns": ["previous_fatigue", "planned_hours", "prior_7d_study_hours", "previous_study_hours"],
            }
        )
    elif factor_key == "plan_load":
        values = pd.to_numeric(frame["planned_hours"], errors="coerce").where(lambda item: item > 0)
        median = values.median()
        if pd.notna(median):
            groups.loc[values >= median] = "A"
            groups.loc[values < median] = "B"
        metadata.update(
            {
                "group_a_label": "予定量が中央値以上",
                "group_b_label": "予定量が中央値未満",
                "threshold": median,
                "matching_columns": ["sleep_hours", "morning_sleepiness", "previous_fatigue", "prior_7d_study_hours"],
            }
        )
    elif factor_key == "morning_sleepiness":
        values = pd.to_numeric(frame["morning_sleepiness"], errors="coerce")
        median = values.median()
        if pd.notna(median):
            groups.loc[values <= median] = "A"
            groups.loc[values > median] = "B"
        metadata.update(
            {
                "group_a_label": "眠気が中央値以下",
                "group_b_label": "眠気が中央値超",
                "threshold": median,
                "matching_columns": ["sleep_hours", "previous_fatigue", "planned_hours", "prior_7d_study_hours"],
            }
        )
    return groups, metadata


def build_evidence_table(
    frame: pd.DataFrame,
    outcome_column: str,
    next_condition_column: str,
    *,
    factor_keys: Iterable[str] = ("caffeine", "running", "good_seat", "sleep", "plan_load", "morning_sleepiness"),
    minimum_days: int = 8,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for position, factor_key in enumerate(factor_keys):
        groups, metadata = factor_groups(frame, factor_key)
        outcome = pd.to_numeric(frame[outcome_column], errors="coerce")
        next_condition = pd.to_numeric(frame[next_condition_column], errors="coerce")
        outcome_summary = summarize_difference(
            outcome.loc[groups == "A"],
            outcome.loc[groups == "B"],
            seed=20260720 + position,
        )
        next_summary = summarize_difference(
            next_condition.loc[groups == "A"],
            next_condition.loc[groups == "B"],
            seed=20260820 + position,
        )
        effective_size = min(
            outcome_summary["group_a_n"],
            outcome_summary["group_b_n"],
            next_summary["group_a_n"],
            next_summary["group_b_n"],
        )
        outcome_scale = robust_scale(outcome.dropna())
        difference = outcome_summary["median_difference"]
        rows.append(
            {
                "factor_key": factor_key,
                "factor": metadata["factor_label"],
                "comparison": (
                    f"{metadata['group_a_label']} − {metadata['group_b_label']}"
                    + (f"（{metadata['exclusion_note']}）" if metadata.get("exclusion_note") else "")
                ),
                "group_a_n": outcome_summary["group_a_n"],
                "group_b_n": outcome_summary["group_b_n"],
                "outcome_difference": difference,
                "outcome_interval_low": outcome_summary["interval_low"],
                "outcome_interval_high": outcome_summary["interval_high"],
                "standardized_outcome_difference": difference / outcome_scale if difference is not None and outcome_scale else np.nan,
                "next_difference": next_summary["median_difference"],
                "next_group_a_n": next_summary["group_a_n"],
                "next_group_b_n": next_summary["group_b_n"],
                "effective_days": effective_size,
                "evidence_level": classify_evidence_level(effective_size),
                "eligible": effective_size >= max(int(minimum_days), 1),
            }
        )
    return pd.DataFrame(rows)


def find_comparable_days(
    frame: pd.DataFrame,
    factor_key: str,
    outcome_column: str,
    *,
    next_condition_column: str | None = None,
    maximum_distance: float = 1.5,
) -> dict[str, Any]:
    groups, metadata = factor_groups(frame, factor_key)
    work = frame.copy()
    work["_group"] = groups
    work["_outcome"] = pd.to_numeric(work[outcome_column], errors="coerce")
    work = work.loc[work["_group"].isin(["A", "B"]) & work["_outcome"].notna()].copy()
    empty_result = {
        "pairs": pd.DataFrame(),
        "summary": summarize_difference([], [], paired=True),
        "next_summary": summarize_difference([], [], paired=True),
        "metadata": metadata,
        "matching_columns": [],
    }
    if work.empty or not (work["_group"] == "A").any() or not (work["_group"] == "B").any():
        return empty_result

    matching_columns: list[str] = []
    for column in metadata.get("matching_columns", []):
        if column not in work:
            continue
        values = pd.to_numeric(work[column], errors="coerce")
        coverage = values.notna().mean()
        group_a_count = values.loc[work["_group"] == "A"].notna().sum()
        group_b_count = values.loc[work["_group"] == "B"].notna().sum()
        if coverage >= 0.45 and group_a_count >= 3 and group_b_count >= 3 and values.nunique(dropna=True) > 1:
            matching_columns.append(column)
    if not matching_columns:
        return empty_result

    standardized = pd.DataFrame(index=work.index)
    for column in matching_columns:
        values = pd.to_numeric(work[column], errors="coerce")
        median = values.median()
        scale = robust_scale(values.dropna()) or 1.0
        standardized[column] = (values.fillna(median) - median) / scale

    group_a_indices = list(work.index[work["_group"] == "A"])
    group_b_indices = list(work.index[work["_group"] == "B"])
    if len(group_a_indices) <= len(group_b_indices):
        source_group = "A"
        source_indices = group_a_indices
        available_indices = set(group_b_indices)
    else:
        source_group = "B"
        source_indices = group_b_indices
        available_indices = set(group_a_indices)
    # Keep the original index/set iteration order (including tie-breaking), but
    # convert the matching inputs only once rather than for every candidate.
    feature_matrix = standardized[matching_columns].to_numpy(dtype=float)
    feature_vectors = dict(zip(work.index, feature_matrix))
    dates = {index: pd.Timestamp(value) for index, value in work["date"].items()}
    weekends = {index: bool(value) for index, value in work["is_weekend"].items()}
    source_indices.sort(key=dates.__getitem__)

    pairs: list[dict[str, Any]] = []
    for source_index in source_indices:
        candidates = [
            index
            for index in available_indices
            if weekends[index] == weekends[source_index]
        ]
        if not candidates:
            continue
        best_index: int | None = None
        best_distance: float | None = None
        source_vector = feature_vectors[source_index]
        source_date = dates[source_index]
        for candidate_index in candidates:
            candidate_vector = feature_vectors[candidate_index]
            value_distance = float(np.mean(np.square(source_vector - candidate_vector)))
            date_gap = abs((source_date - dates[candidate_index]).days) / 90
            distance = float(np.sqrt(value_distance + (0.35 * date_gap) ** 2))
            if best_distance is None or distance < best_distance:
                best_index = candidate_index
                best_distance = distance
        if best_index is None or best_distance is None or best_distance > maximum_distance:
            continue
        available_indices.remove(best_index)
        if source_group == "A":
            index_a, index_b = source_index, best_index
        else:
            index_a, index_b = best_index, source_index
        pair = {
            "condition_a_date": pd.Timestamp(work.at[index_a, "date"]).date().isoformat(),
            "condition_b_date": pd.Timestamp(work.at[index_b, "date"]).date().isoformat(),
            "condition_a_value": float(work.at[index_a, "_outcome"]),
            "condition_b_value": float(work.at[index_b, "_outcome"]),
            "outcome_difference": float(work.at[index_a, "_outcome"] - work.at[index_b, "_outcome"]),
            "matching_distance": best_distance,
        }
        if next_condition_column and next_condition_column in work:
            next_a = _number(work.at[index_a, next_condition_column])
            next_b = _number(work.at[index_b, next_condition_column])
            pair["next_condition_a"] = next_a
            pair["next_condition_b"] = next_b
            pair["next_difference"] = next_a - next_b if not np.isnan(next_a) and not np.isnan(next_b) else np.nan
        pairs.append(pair)

    pair_frame = pd.DataFrame(pairs)
    if pair_frame.empty:
        return empty_result
    outcome_summary = summarize_difference(
        pair_frame["condition_a_value"],
        pair_frame["condition_b_value"],
        paired=True,
        seed=20260901,
    )
    if "next_condition_a" in pair_frame:
        next_pairs = pair_frame.dropna(subset=["next_condition_a", "next_condition_b"])
        next_summary = summarize_difference(
            next_pairs["next_condition_a"],
            next_pairs["next_condition_b"],
            paired=True,
            seed=20260902,
        )
    else:
        next_summary = summarize_difference([], [], paired=True)
    return {
        "pairs": pair_frame,
        "summary": outcome_summary,
        "next_summary": next_summary,
        "metadata": metadata,
        "matching_columns": matching_columns,
    }
