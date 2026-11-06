from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app_core.app_clock import app_today
from config import PROJECT_ROOT
from app_core.json_store import json_transaction_for


PRESET_INTERVALS: dict[str, list[int]] = {
    "light": [1, 7, 30],
    "standard": [1, 3, 7, 14, 30, 60],
    "intensive": [1, 2, 4, 7, 14, 30, 60, 90],
}

REVIEW_QUALITY: dict[str, int] = {
    "forgot": 1,
    "difficult": 3,
    "normal": 4,
    "easy": 5,
}


def _root(root: Path | None = None) -> Path:
    return root or PROJECT_ROOT


def _date(value: Any, fallback: date | None = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return fallback or app_today()


def _unique_id(prefix: str, target: date | str | None, existing_ids: set[str]) -> str:
    from data_utils import generate_id

    return generate_id(prefix, target, existing_ids)


def load_reminders(root: Path | None = None, *, write_back: bool = False) -> list[dict[str, Any]]:
    from data_utils import load_all_reminders

    return load_all_reminders(_root(root), write_back=write_back)


def save_reminders(reminders: list[dict[str, Any]], root: Path | None = None) -> None:
    from data_utils import save_reminders as _save

    _save(_root(root), reminders)


def ensure_reminder_ids(root: Path | None = None) -> list[dict[str, Any]]:
    return load_reminders(root, write_back=True)


def normalize_intervals(values: Any) -> list[int]:
    """重複や不正値を除き、復習間隔を昇順で返します。"""
    if isinstance(values, str):
        values = values.replace("、", ",").split(",")
    if not isinstance(values, (list, tuple)):
        return []
    intervals: set[int] = set()
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= 3650:
            intervals.add(number)
    return sorted(intervals)


@json_transaction_for(lambda reminder, **options: _root(options.get("root")) / "reminders.json")
def create_reminder(
    reminder: dict[str, Any],
    *,
    schedule_mode: str = "single",
    preset: str = "standard",
    custom_intervals: Any = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """単発または間隔反復スケジュール付きリマインダーを作成します。"""
    from data_utils import now_text

    reminders = load_reminders(root)
    reminder_ids = {str(item.get("reminder_id") or "") for item in reminders}
    schedule_ids = {str(item.get("schedule_id") or "") for item in reminders}
    item = dict(reminder)
    scheduled = _date(item.get("scheduled_date"))
    timestamp = now_text()

    item.setdefault("reminder_id", _unique_id("reminder", scheduled, reminder_ids))
    item.setdefault("created_at", timestamp)
    item.setdefault("updated_at", timestamp)
    item.setdefault("status", "pending")
    item.setdefault("review_history", [])

    if schedule_mode != "single":
        intervals = normalize_intervals(custom_intervals if schedule_mode == "custom" else PRESET_INTERVALS.get(preset, PRESET_INTERVALS["standard"]))
        item.setdefault("schedule_id", _unique_id("schedule", scheduled, schedule_ids))
        item["schedule_mode"] = schedule_mode
        item["schedule_preset"] = preset if schedule_mode != "custom" else "custom"
        item["intervals_days"] = intervals
        item.setdefault("current_step", 0)
        item.setdefault("review_count", 0)
        item.setdefault("algorithm", "sm2" if schedule_mode == "sm2" else "fixed")
        item.setdefault("repetition", 0)
        item.setdefault("interval_days", 0)
        item.setdefault("ease_factor", 2.5)
        item.setdefault("schedule_status", "active")
        item.setdefault("next_review_date", scheduled.isoformat())
    else:
        item.setdefault("schedule_mode", "single")
        item.setdefault("algorithm", "single")

    reminders.append(item)
    save_reminders(reminders, root)
    return item


def add_reminder(reminder: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """既存互換用。単発リマインダーとして追加します。"""
    return create_reminder(reminder, schedule_mode="single", root=root)


@json_transaction_for(lambda reminder_id, updates, root=None: _root(root) / "reminders.json")
def update_reminder(reminder_id: str, updates: dict[str, Any], root: Path | None = None) -> bool:
    from data_utils import now_text

    reminders = load_reminders(root)
    for reminder in reminders:
        if reminder.get("reminder_id") == reminder_id:
            reminder.update(updates)
            reminder["updated_at"] = now_text()
            save_reminders(reminders, root)
            return True
    return False


def find_reminder_by_id(reminder_id: str, root: Path | None = None) -> dict[str, Any] | None:
    return next((reminder for reminder in load_reminders(root) if reminder.get("reminder_id") == reminder_id), None)


def resolve_reminder_label(reminder_id: str | None, root: Path | None = None) -> str:
    from data_utils import resolve_reminder_title

    return resolve_reminder_title(_root(root), reminder_id)


def get_reminders_by_note_id(note_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    return [
        reminder
        for reminder in load_reminders(root)
        if reminder.get("source_note_id") == note_id or note_id in (reminder.get("linked_note_ids") or [])
    ]


def get_reminders_by_card_id(card_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    return [
        reminder
        for reminder in load_reminders(root)
        if reminder.get("source_card_id") == card_id or card_id in (reminder.get("linked_card_ids") or [])
    ]


def get_due_reminders(
    target: date | None = None,
    root: Path | None = None,
    *,
    reminders: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Select due items, optionally reusing this render's display snapshot.

    Save operations still load their own current records; this argument is only
    for callers that have already read the reminder list for display.
    """
    target = target or app_today()
    return [
        reminder
        for reminder in (load_reminders(root) if reminders is None else reminders)
        if _date(reminder.get("scheduled_date") or reminder.get("registered")) <= target
        and reminder.get("status") != "done"
        and reminder.get("schedule_status", "active") == "active"
    ]


def calculate_sm2(
    *,
    quality: int,
    repetition: int = 0,
    interval_days: int = 0,
    ease_factor: float = 2.5,
) -> dict[str, int | float]:
    """SM-2に基づき、次回までの日数と学習状態を計算します。"""
    quality = max(0, min(int(quality), 5))
    repetition = max(int(repetition or 0), 0)
    interval_days = max(int(interval_days or 0), 0)
    ease_factor = max(float(ease_factor or 2.5), 1.3)

    if quality < 3:
        repetition = 0
        interval_days = 1
    else:
        repetition += 1
        if repetition == 1:
            interval_days = 1
        elif repetition == 2:
            interval_days = 6
        else:
            interval_days = max(1, round(interval_days * ease_factor))

    ease_factor = max(
        1.3,
        ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)),
    )
    return {
        "repetition": repetition,
        "interval_days": interval_days,
        "ease_factor": round(ease_factor, 2),
    }


@json_transaction_for(lambda reminder_id, result, **options: _root(options.get("root")) / "reminders.json")
def complete_review(
    reminder_id: str,
    result: str,
    *,
    reviewed_on: date | None = None,
    memo: str = "",
    duration_min: int = 0,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """復習結果を記録し、必要に応じて次回復習日を更新します。"""
    from data_utils import now_text

    reminders = load_reminders(root)
    reviewed_on = reviewed_on or app_today()
    for reminder in reminders:
        if reminder.get("reminder_id") != reminder_id:
            continue

        scheduled_before = _date(reminder.get("scheduled_date"))
        history = reminder.setdefault("review_history", [])
        history.append(
            {
                "reviewed_on": reviewed_on.isoformat(),
                "scheduled_date": scheduled_before.isoformat(),
                "result": result,
                "quality": REVIEW_QUALITY.get(result, 4),
                "memo": memo.strip(),
                "duration_min": max(int(duration_min or 0), 0),
                "recorded_at": now_text(),
            }
        )
        reminder["last_reviewed_at"] = now_text()
        reminder["last_review_result"] = result
        reminder["review_count"] = int(reminder.get("review_count", 0) or 0) + 1
        mode = str(reminder.get("schedule_mode") or "single")

        if mode == "single":
            reminder["status"] = "done"
            reminder["completed_at"] = now_text()
        elif reminder.get("algorithm") == "sm2" or mode == "sm2":
            sm2 = calculate_sm2(
                quality=REVIEW_QUALITY.get(result, 4),
                repetition=int(reminder.get("repetition", 0) or 0),
                interval_days=int(reminder.get("interval_days", 0) or 0),
                ease_factor=float(reminder.get("ease_factor", 2.5) or 2.5),
            )
            reminder.update(sm2)
            next_date = reviewed_on + timedelta(days=int(sm2["interval_days"]))
            reminder["scheduled_date"] = next_date.isoformat()
            reminder["next_review_date"] = next_date.isoformat()
            reminder["status"] = "pending"
            reminder["schedule_status"] = "active"
        else:
            intervals = normalize_intervals(reminder.get("intervals_days"))
            step = int(reminder.get("current_step", 0) or 0)
            if step >= len(intervals):
                reminder["status"] = "done"
                reminder["schedule_status"] = "completed"
                reminder["completed_at"] = now_text()
                reminder["next_review_date"] = ""
            else:
                next_date = reviewed_on + timedelta(days=intervals[step])
                reminder["current_step"] = step + 1
                reminder["interval_days"] = intervals[step]
                reminder["scheduled_date"] = next_date.isoformat()
                reminder["next_review_date"] = next_date.isoformat()
                reminder["status"] = "pending"
                reminder["schedule_status"] = "active"

        reminder["updated_at"] = now_text()
        save_reminders(reminders, root)
        return reminder
    return None


@json_transaction_for(lambda reminder_id, **options: _root(options.get("root")) / "reminders.json")
def skip_review(reminder_id: str, *, reviewed_on: date | None = None, root: Path | None = None) -> dict[str, Any] | None:
    """今回分をスキップし、系列を維持したまま次回予定へ進めます。"""
    from data_utils import now_text

    reminders = load_reminders(root)
    reviewed_on = reviewed_on or app_today()
    for reminder in reminders:
        if reminder.get("reminder_id") != reminder_id:
            continue
        scheduled_before = _date(reminder.get("scheduled_date"))
        reminder.setdefault("review_history", []).append(
            {
                "reviewed_on": reviewed_on.isoformat(),
                "scheduled_date": scheduled_before.isoformat(),
                "result": "skipped",
                "quality": None,
                "memo": "",
                "duration_min": 0,
                "recorded_at": now_text(),
            }
        )
        mode = str(reminder.get("schedule_mode") or "single")
        if mode == "single":
            reminder["status"] = "done"
            reminder["completed_at"] = now_text()
        elif reminder.get("algorithm") == "sm2" or mode == "sm2":
            next_date = reviewed_on + timedelta(days=1)
            reminder["scheduled_date"] = next_date.isoformat()
            reminder["next_review_date"] = next_date.isoformat()
            reminder["status"] = "pending"
        else:
            intervals = normalize_intervals(reminder.get("intervals_days"))
            step = int(reminder.get("current_step", 0) or 0)
            if step >= len(intervals):
                reminder["status"] = "done"
                reminder["schedule_status"] = "completed"
                reminder["completed_at"] = now_text()
                reminder["next_review_date"] = ""
            else:
                next_date = reviewed_on + timedelta(days=intervals[step])
                reminder["current_step"] = step + 1
                reminder["interval_days"] = intervals[step]
                reminder["scheduled_date"] = next_date.isoformat()
                reminder["next_review_date"] = next_date.isoformat()
                reminder["status"] = "pending"
        reminder["updated_at"] = now_text()
        save_reminders(reminders, root)
        return reminder
    return None


@json_transaction_for(lambda reminder_id, days=1, root=None: _root(root) / "reminders.json")
def defer_reminder(reminder_id: str, days: int = 1, root: Path | None = None) -> bool:
    """現在の予定日を指定日数だけ延期します。"""
    reminder = find_reminder_by_id(reminder_id, root)
    if not reminder:
        return False
    next_date = _date(reminder.get("scheduled_date")) + timedelta(days=max(int(days), 1))
    return update_reminder(reminder_id, {"scheduled_date": next_date.isoformat(), "next_review_date": next_date.isoformat(), "status": "pending"}, root)


@json_transaction_for(lambda reminder_id, status, root=None: _root(root) / "reminders.json")
def set_schedule_status(reminder_id: str, status: str, root: Path | None = None) -> bool:
    """復習系列を一時停止・再開・キャンセルします。"""
    if status not in {"active", "paused", "cancelled"}:
        return False
    updates: dict[str, Any] = {"schedule_status": status}
    if status == "active":
        updates["status"] = "pending"
    elif status == "cancelled":
        updates["status"] = "done"
    return update_reminder(reminder_id, updates, root)


def schedule_progress(reminder: dict[str, Any]) -> str:
    """画面表示用の進捗文字列を返します。"""
    mode = str(reminder.get("schedule_mode") or "single")
    if mode == "single":
        return "単発"
    if reminder.get("algorithm") == "sm2" or mode == "sm2":
        return f"適応型 / 復習 {int(reminder.get('review_count', 0) or 0)}回"
    intervals = normalize_intervals(reminder.get("intervals_days"))
    completed = min(int(reminder.get("review_count", 0) or 0), len(intervals) + 1)
    return f"{completed}/{len(intervals) + 1}回完了"


def filter_reminders(
    project_id: str | None = None,
    subject_id: str | None = None,
    material_id: str | None = None,
    unit_id: str | None = None,
    status: str | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    reminders = load_reminders(root)
    filters = {"project_id": project_id, "subject_id": subject_id, "material_id": material_id, "unit_id": unit_id, "status": status}
    for key, value in filters.items():
        if value:
            reminders = [reminder for reminder in reminders if reminder.get(key) == value]
    return reminders
