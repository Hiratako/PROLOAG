"""One recoverable operation for the diary/health/exercise form submissions."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Mapping

from app_core.diary_service import get_diary_entry_path, save_diary_entry
from app_core.exercise_service import (
    delete_daily_hub_exercise_record,
    upsert_daily_hub_exercise_record,
    upsert_diary_running_record,
)
from app_core.health_service import get_health_log_path, save_health_log
from app_core.json_operations import atomic_json_operation


def save_daily_records(
    target: date,
    diary_data: dict[str, Any],
    *,
    root: Path,
    operation_id: str,
    expected_revisions: Mapping[Path, Any],
    health_data: dict[str, Any] | None = None,
    exercise_mode: str = "unchanged",
    exercise_data: dict[str, Any] | None = None,
) -> None:
    if exercise_mode not in {"unchanged", "hub", "delete_hub", "diary_running"}:
        raise ValueError("運動記録の保存方法が無効です。")
    with atomic_json_operation(
        root=root, kind="daily_save", operation_id=operation_id,
        expected_revisions=dict(expected_revisions),
    ) as operation:
        if operation.already_committed:
            return
        save_diary_entry(target, diary_data, expected_revision=expected_revisions[get_diary_entry_path(target)])
        if health_data is not None:
            save_health_log(target, health_data, expected_revision=expected_revisions[get_health_log_path(target)])
        if exercise_mode == "hub":
            upsert_daily_hub_exercise_record(target, exercise_data or {})
        elif exercise_mode == "delete_hub":
            delete_daily_hub_exercise_record(target)
        elif exercise_mode == "diary_running":
            values = exercise_data or {}
            upsert_diary_running_record(
                target, values.get("distance_km", 0), values.get("duration_min", 0),
                memo=str(values.get("memo") or ""),
            )
