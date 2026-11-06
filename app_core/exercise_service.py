from __future__ import annotations

from datetime import date, datetime, time, timedelta
from app_core.app_clock import app_today
from pathlib import Path
from typing import Any

from config import EXERCISE_LOGS_DIR, STUDY_LOGS_DIR
from app_core.ids import generate_daily_sequence_id
from app_core.json_store import _UNSET, json_transaction_for, load_json_safe, observed_json_revision, save_json_safe
from app_core.study_record_reader import unpack_study_document


def _parse_date(value: date | datetime | str | None = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return app_today()
    return app_today()


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date_range(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_time_text(value: Any) -> str:
    if isinstance(value, time):
        return value.strftime("%H:%M")
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text[:5], "%H:%M").time()
        return parsed.strftime("%H:%M")
    except ValueError:
        return ""


def get_exercise_log_path(target: date | datetime | str | None = None) -> Path:
    target_date = _parse_date(target)
    return EXERCISE_LOGS_DIR / f"{target_date:%Y}" / f"{target_date:%m}" / f"{target_date:%Y%m%d}.json"


def default_exercise_log(target: date | datetime | str | None = None) -> dict[str, Any]:
    target_date = _parse_date(target)
    timestamp = _now_text()
    return {
        "date": target_date.isoformat(),
        "records": [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def calculate_running_pace(distance_km: Any, duration_min: Any) -> float:
    distance = _num(distance_km)
    duration = _num(duration_min)
    if distance <= 0 or duration <= 0:
        return 0.0
    return round(duration / distance, 2)


def normalize_exercise_record(record: dict[str, Any], target: date, existing_ids: set[str] | None = None) -> dict[str, Any]:
    if existing_ids is None:
        existing_ids = set()
    timestamp = _now_text()
    exercise_id = str(record.get("exercise_id") or "")
    if not exercise_id:
        exercise_id = generate_daily_sequence_id("exercise", existing_ids, target)
    existing_ids.add(exercise_id)

    duration_min = int(max(0, _num(record.get("duration_min"))))
    distance_km = round(max(0.0, _num(record.get("distance_km"))), 3)
    pace = _num(record.get("pace_min_per_km")) or calculate_running_pace(distance_km, duration_min)
    return {
        "exercise_id": exercise_id,
        "type": str(record.get("type") or "other"),
        "title": str(record.get("title") or ""),
        "start_time": _to_time_text(record.get("start_time")),
        "duration_min": duration_min,
        "distance_km": distance_km,
        "pace_min_per_km": round(pace, 2) if pace else 0.0,
        "intensity": int(min(10, max(1, _num(record.get("intensity")) or 5))),
        "fatigue_after": int(min(5, max(1, _num(record.get("fatigue_after")) or 3))),
        "memo": str(record.get("memo") or ""),
        "source": str(record.get("source") or ""),
        "created_at": record.get("created_at") or timestamp,
        "updated_at": record.get("updated_at") or timestamp,
    }


def normalize_exercise_log(data: Any, target: date | datetime | str | None = None) -> dict[str, Any]:
    target_date = _parse_date(target)
    base = default_exercise_log(target_date)
    if not isinstance(data, dict):
        return base

    base["date"] = str(data.get("date") or target_date.isoformat())[:10]
    base["created_at"] = data.get("created_at") or base["created_at"]
    base["updated_at"] = data.get("updated_at") or base["created_at"]
    records = data.get("records", [])
    if isinstance(records, list):
        existing_ids = {
            str(record["exercise_id"])
            for record in records
            if isinstance(record, dict) and record.get("exercise_id")
        }
        base["records"] = [
            normalize_exercise_record(record, target_date, existing_ids)
            for record in records
            if isinstance(record, dict)
        ]
    return base


def load_exercise_log(target: date | datetime | str | None = None) -> dict[str, Any]:
    path = get_exercise_log_path(target)
    return normalize_exercise_log(load_json_safe(path, default_exercise_log(target)), target)


@json_transaction_for(lambda target, data, **_kwargs: get_exercise_log_path(target))
def save_exercise_log(target: date | datetime | str | None, data: dict[str, Any], *, expected_revision: Any = _UNSET) -> None:
    target_date = _parse_date(target)
    if expected_revision is _UNSET:
        expected_revision = observed_json_revision(get_exercise_log_path(target_date))
    existing = load_exercise_log(target_date) if get_exercise_log_path(target_date).exists() else default_exercise_log(target_date)
    payload = normalize_exercise_log({**existing, **data}, target_date)
    payload["date"] = target_date.isoformat()
    payload["created_at"] = existing.get("created_at") or _now_text()
    payload["updated_at"] = _now_text()
    save_json_safe(get_exercise_log_path(target_date), payload, expected_revision=expected_revision)


def ensure_exercise_log(target: date | datetime | str | None = None) -> dict[str, Any]:
    return load_exercise_log(target)


@json_transaction_for(lambda target, record: get_exercise_log_path(target))
def add_exercise_record(target: date | datetime | str | None, record: dict[str, Any]) -> dict[str, Any]:
    target_date = _parse_date(target)
    log = load_exercise_log(target_date)
    existing_ids = {item.get("exercise_id", "") for item in log.get("records", []) if isinstance(item, dict)}
    normalized = normalize_exercise_record(record, target_date, existing_ids)
    log.setdefault("records", []).append(normalized)
    save_exercise_log(target_date, log)
    return normalized


@json_transaction_for(lambda target, exercise_id: get_exercise_log_path(target))
def delete_exercise_record(target: date | datetime | str | None, exercise_id: str) -> bool:
    target_date = _parse_date(target)
    log = load_exercise_log(target_date)
    records = [item for item in log.get("records", []) if isinstance(item, dict)]
    kept = [item for item in records if item.get("exercise_id") != exercise_id]
    if len(kept) == len(records):
        return False
    log["records"] = kept
    save_exercise_log(target_date, log)
    return True


@json_transaction_for(lambda target, *_args, **_kwargs: get_exercise_log_path(target))
def upsert_diary_running_record(
    target: date | datetime | str | None,
    distance_km: Any,
    duration_min: Any,
    *,
    memo: str = "",
) -> dict[str, Any] | None:
    target_date = _parse_date(target)
    exercise_id = f"exercise_{target_date:%Y%m%d}_diary_running"
    log = load_exercise_log(target_date)
    records = [item for item in log.get("records", []) if isinstance(item, dict)]
    previous = next((item for item in records if item.get("exercise_id") == exercise_id), {})
    kept = [item for item in records if item.get("exercise_id") != exercise_id]

    distance = round(max(0.0, _num(distance_km)), 3)
    duration = int(max(0, _num(duration_min)))
    if distance <= 0 and duration <= 0:
        log["records"] = kept
        save_exercise_log(target_date, log)
        return None

    record = normalize_exercise_record(
        {
            "exercise_id": exercise_id,
            "type": "running",
            "title": "日記から同期",
            "start_time": "",
            "duration_min": duration,
            "distance_km": distance,
            "pace_min_per_km": calculate_running_pace(distance, duration),
            "intensity": 5,
            "fatigue_after": 3,
            "memo": memo,
            "source": "diary",
            "created_at": previous.get("created_at") or _now_text(),
            "updated_at": _now_text(),
        },
        target_date,
        {item.get("exercise_id", "") for item in kept if isinstance(item, dict)},
    )
    log["records"] = kept + [record]
    save_exercise_log(target_date, log)
    return record


@json_transaction_for(lambda target, record: get_exercise_log_path(target))
def upsert_daily_hub_exercise_record(
    target: date | datetime | str | None,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    target_date = _parse_date(target)
    exercise_id = f"exercise_{target_date:%Y%m%d}_daily_hub"
    log = load_exercise_log(target_date)
    records = [item for item in log.get("records", []) if isinstance(item, dict)]
    previous = next(
        (item for item in records if item.get("exercise_id") == exercise_id or item.get("source") == "daily_hub"),
        {},
    )
    kept = [
        item
        for item in records
        if item.get("exercise_id") != exercise_id and item.get("source") != "daily_hub"
    ]

    duration = int(max(0, _num(record.get("duration_min"))))
    distance = round(max(0.0, _num(record.get("distance_km"))), 3)
    title = str(record.get("title") or "").strip()
    if duration <= 0 and distance <= 0 and not title:
        delete_daily_hub_exercise_record(target_date)
        return None

    payload = dict(record)
    payload.update(
        {
            "exercise_id": exercise_id,
            "title": title or "今日の記録・計画",
            "duration_min": duration,
            "distance_km": distance,
            "source": "daily_hub",
            "created_at": previous.get("created_at") or _now_text(),
            "updated_at": _now_text(),
        }
    )
    normalized = normalize_exercise_record(
        payload,
        target_date,
        {item.get("exercise_id", "") for item in kept if isinstance(item, dict)},
    )
    log["records"] = kept + [normalized]
    save_exercise_log(target_date, log)
    return normalized


@json_transaction_for(lambda target: get_exercise_log_path(target))
def delete_daily_hub_exercise_record(target: date | datetime | str | None) -> bool:
    """Remove only the quick-entry exercise record for the target date."""
    target_date = _parse_date(target)
    exercise_id = f"exercise_{target_date:%Y%m%d}_daily_hub"
    log = load_exercise_log(target_date)
    records = [item for item in log.get("records", []) if isinstance(item, dict)]
    kept = [
        item
        for item in records
        if item.get("exercise_id") != exercise_id and item.get("source") != "daily_hub"
    ]
    if len(kept) == len(records):
        return False
    log["records"] = kept
    save_exercise_log(target_date, log)
    return True


def _study_log_path(target: date) -> Path:
    return STUDY_LOGS_DIR / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"


def _study_duration_sec(target: date) -> float:
    data = load_json_safe(_study_log_path(target), [])
    return sum(_num(item.get("duration_sec")) for _, item in unpack_study_document(data).records)


def load_exercise_logs_range(
    start_date: date, end_date: date, *, logs_by_date: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target in _date_range(start_date, end_date):
        path = get_exercise_log_path(target)
        if not path.exists():
            continue
        log = (logs_by_date[target.isoformat()]
               if logs_by_date is not None and target.isoformat() in logs_by_date
               else load_exercise_log(target))
        for record in log.get("records", []):
            item = dict(record)
            item["date"] = target.isoformat()
            rows.append(item)
    return rows


def build_exercise_summary(
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    exercise_records: list[dict[str, Any]] | None = None,
    study_duration_by_date: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate range-loader records; supplied snapshots are never reread or changed."""
    end_date = end_date or app_today()
    start_date = start_date or (end_date - timedelta(days=30))
    if exercise_records is None:
        exercise_records = load_exercise_logs_range(start_date, end_date)
    records_by_date: dict[str, list[dict[str, Any]]] = {}
    for record in exercise_records:
        day = str(record.get("date") or "")[:10]
        records_by_date.setdefault(day, []).append(record)
    rows: list[dict[str, Any]] = []
    for target in _date_range(start_date, end_date):
        records = records_by_date.get(target.isoformat(), [])
        duration_min = int(sum(_num(item.get("duration_min")) for item in records))
        distance_km = round(sum(_num(item.get("distance_km")) for item in records), 3)
        intensity_values = [_num(item.get("intensity")) for item in records if _num(item.get("intensity"))]
        fatigue_values = [_num(item.get("fatigue_after")) for item in records if _num(item.get("fatigue_after"))]
        study_sec = (
            _num(study_duration_by_date.get(target.isoformat()))
            if study_duration_by_date is not None
            else _study_duration_sec(target)
        )
        rows.append(
            {
                "date": target.isoformat(),
                "record_count": len(records),
                "exercise_duration_min": duration_min,
                "distance_km": distance_km,
                "avg_intensity": round(sum(intensity_values) / len(intensity_values), 2) if intensity_values else 0.0,
                "avg_fatigue_after": round(sum(fatigue_values) / len(fatigue_values), 2) if fatigue_values else 0.0,
                "study_duration_sec": study_sec,
                "study_hours": round(study_sec / 3600, 3),
                "has_exercise_log": bool(records),
            }
        )
    return rows


def build_exercise_study_joined_summary(
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    exercise_records: list[dict[str, Any]] | None = None,
    study_duration_by_date: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return build_exercise_summary(
        start_date, end_date,
        exercise_records=exercise_records,
        study_duration_by_date=study_duration_by_date,
    )
