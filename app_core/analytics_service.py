from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from app_core.app_clock import app_today
from typing import Any

from config import DAILY_PLANS_DIR, FLASHCARDS_PATH, NOTES_DATA_DIR, REMINDERS_PATH, STUDY_LOGS_DIR
from app_core.json_store import load_json_safe, record_json_files
from app_core.master_snapshot import MasterSnapshot
from app_core.analysis_dates import parse_analysis_date, parse_analysis_datetime
from app_core.study_record_reader import unpack_study_document


def aggregate_by_key(sessions: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"duration_sec": 0, "money": 0, "sessions": 0})
    for session in sessions:
        label = session.get(key) or "未設定"
        rows[label]["duration_sec"] += _num(session.get("duration_sec"))
        rows[label]["money"] += int(float(session.get("money", 0) or 0))
        rows[label]["sessions"] += 1
    return dict(rows)


def aggregate_daily_study_logs(sessions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"duration_sec": 0, "money": 0, "sessions": 0})
    for session in sessions:
        parsed = parse_analysis_date(session.get("start_at") or session.get("end_at"))
        day = parsed.isoformat() if parsed is not None else "未設定"
        rows[day]["duration_sec"] += _num(session.get("duration_sec"))
        rows[day]["money"] += int(float(session.get("money", 0) or 0))
        rows[day]["sessions"] += 1
    return dict(rows)


def aggregate_by_subject(sessions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return aggregate_by_key(sessions, "subject_id")


def aggregate_by_material(sessions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return aggregate_by_key(sessions, "material_id")


def aggregate_by_unit(sessions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return aggregate_by_key(sessions, "unit_id")


def aggregate_by_hour(sessions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"duration_sec": 0, "money": 0, "sessions": 0})
    for session in sessions:
        parsed = parse_analysis_datetime(session.get("start_at") or session.get("end_at"))
        hour = f"{parsed.hour:02d}" if parsed is not None else "未設定"
        rows[hour]["duration_sec"] += _num(session.get("duration_sec"))
        rows[hour]["money"] += int(float(session.get("money", 0) or 0))
        rows[hour]["sessions"] += 1
    return dict(rows)


def plan_actual_gap(tasks: list[dict[str, Any]], sessions_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for task in tasks:
        actual_sec = sum(
            _num(sessions_by_id.get(session_id, {}).get("duration_sec"))
            for session_id in task.get("linked_session_ids", [])
        )
        planned_min = int(task.get("planned_minutes", 0) or 0)
        actual_min = round(actual_sec / 60, 1)
        rows.append(
            {
                "task_id": task.get("task_id", ""),
                "title": task.get("title", "無題のタスク"),
                "planned_minutes": planned_min,
                "actual_minutes": actual_min,
                "gap_minutes": round(actual_min - planned_min, 1),
                "status": task.get("status", ""),
            }
        )
    return rows


def _parse_date(value: Any) -> date | None:
    return parse_analysis_date(value)


def _date_span(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    days = (end_date - start_date).days
    return [start_date + timedelta(days=offset) for offset in range(days + 1)]


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _matches_filters(record: dict[str, Any], filters: dict[str, Any] | None = None) -> bool:
    filters = filters or {}
    for key in ("project_id", "subject_id", "material_id", "unit_id"):
        selected = filters.get(key)
        if selected and record.get(key) != selected:
            return False
    return True


def _load_study_sessions_readonly() -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    if not STUDY_LOGS_DIR.exists():
        return sessions
    for path in record_json_files(STUDY_LOGS_DIR):
        data = load_json_safe(path, [])
        for _, item in unpack_study_document(data).records:
            item["_source_file"] = str(path)
            sessions.append(item)
    return sessions


def _load_daily_plans_readonly() -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    if not DAILY_PLANS_DIR.exists():
        return plans
    for path in record_json_files(DAILY_PLANS_DIR):
        data = load_json_safe(path, {})
        if isinstance(data, dict):
            item = dict(data)
            item["_source_file"] = str(path)
            plans.append(item)
    return plans


def _load_notes_readonly() -> list[dict[str, Any]]:
    path = NOTES_DATA_DIR / "notebooks.json"
    store = load_json_safe(path, {"notebooks": []})
    notes: list[dict[str, Any]] = []
    if not isinstance(store, dict):
        return notes
    for notebook in store.get("notebooks", []):
        if not isinstance(notebook, dict):
            continue
        for section in notebook.get("sections", []):
            if not isinstance(section, dict):
                continue
            for page in section.get("pages", []):
                if isinstance(page, dict):
                    note = dict(page)
                    note["_notebook"] = notebook.get("name", "")
                    note["_section"] = section.get("name", "")
                    notes.append(note)
    return notes


def _load_flashcards_readonly() -> list[dict[str, Any]]:
    data = load_json_safe(FLASHCARDS_PATH, [])
    return [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _load_reminders_readonly() -> list[dict[str, Any]]:
    data = load_json_safe(REMINDERS_PATH, [])
    return [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []


@dataclass(frozen=True)
class LearningDataSnapshot:
    """One render's input data; no persistence or cross-render cache.

    All views expand the same list/envelope/single-record formats. The two
    session collections retain their established source-path field names.
    """

    sessions: list[dict[str, Any]]
    activity_sessions: list[dict[str, Any]]
    plans: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    cards: list[dict[str, Any]]
    reminders: list[dict[str, Any]]

    @classmethod
    def load(cls) -> LearningDataSnapshot:
        sessions: list[dict[str, Any]] = []
        activity_sessions: list[dict[str, Any]] = []
        if STUDY_LOGS_DIR.exists():
            for path in record_json_files(STUDY_LOGS_DIR):
                data = load_json_safe(path, [])
                for _, item in unpack_study_document(data).records:
                    activity_sessions.append({**item, "source_file": str(path)})
                    sessions.append({**item, "_source_file": str(path)})
        return cls(
            sessions=sessions,
            activity_sessions=activity_sessions,
            plans=_load_daily_plans_readonly(),
            notes=_load_notes_readonly(),
            cards=_load_flashcards_readonly(),
            reminders=_load_reminders_readonly(),
        )


def _default_period(sessions: list[dict[str, Any]], start_date: date | None, end_date: date | None) -> tuple[date, date]:
    dated = [_parse_date(session.get("start_at") or session.get("end_at")) for session in sessions]
    dated = [day for day in dated if day]
    today = app_today()
    start = start_date or (min(dated) if dated else today - timedelta(days=365))
    end = end_date or (max(dated) if dated else today)
    if end < start:
        start, end = end, start
    return start, end


def _main_value(items: list[dict[str, Any]], key: str, weight_key: str = "duration_sec") -> str:
    totals: dict[str, float] = defaultdict(float)
    for item in items:
        value = item.get(key) or ""
        totals[value] += _num(item.get(weight_key))
    if not totals:
        return ""
    return max(totals.items(), key=lambda pair: pair[1])[0]


def build_daily_learning_summary(
    start_date: date | None = None,
    end_date: date | None = None,
    filters: dict[str, Any] | None = None,
    *,
    snapshot: LearningDataSnapshot | None = None,
) -> list[dict[str, Any]]:
    sessions = [session for session in (snapshot.sessions if snapshot is not None else _load_study_sessions_readonly()) if _matches_filters(session, filters)]
    plans = snapshot.plans if snapshot is not None else _load_daily_plans_readonly()
    notes = [note for note in (snapshot.notes if snapshot is not None else _load_notes_readonly()) if _matches_filters(note, filters)]
    cards = [card for card in (snapshot.cards if snapshot is not None else _load_flashcards_readonly()) if _matches_filters(card, filters)]
    reminders = [reminder for reminder in (snapshot.reminders if snapshot is not None else _load_reminders_readonly()) if _matches_filters(reminder, filters)]
    start, end = _default_period(sessions, start_date, end_date)

    rows = {
        day: {
            "date": day.isoformat(),
            "total_duration_sec": 0,
            "total_hours": 0.0,
            "total_money": 0.0,
            "session_count": 0,
            "planned_minutes": 0,
            "task_count": 0,
            "main_project": "",
            "main_subject": "",
            "note_count": 0,
            "card_count": 0,
            "reminder_count": 0,
        }
        for day in _date_span(start, end)
    }
    sessions_by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for session in sessions:
        day = _parse_date(session.get("start_at") or session.get("end_at"))
        if not day or day not in rows:
            continue
        rows[day]["total_duration_sec"] += _num(session.get("duration_sec"))
        rows[day]["total_money"] += _num(session.get("money"))
        rows[day]["session_count"] += 1
        sessions_by_day[day].append(session)

    for plan in plans:
        day = _parse_date(plan.get("date"))
        if day not in rows:
            continue
        for task in plan.get("tasks", []):
            if isinstance(task, dict) and _matches_filters(task, filters):
                rows[day]["planned_minutes"] += int(_num(task.get("planned_minutes")))
                rows[day]["task_count"] += 1

    for note in notes:
        day = _parse_date(note.get("created_at") or note.get("updated_at"))
        if day in rows:
            rows[day]["note_count"] += 1

    for card in cards:
        day = _parse_date(card.get("created_at") or card.get("updated_at"))
        if day in rows:
            rows[day]["card_count"] += 1

    for reminder in reminders:
        day = _parse_date(reminder.get("scheduled_date") or reminder.get("registered") or reminder.get("created_at"))
        if day in rows:
            rows[day]["reminder_count"] += 1

    for day, row in rows.items():
        row["total_hours"] = round(row["total_duration_sec"] / 3600, 3)
        day_sessions = sessions_by_day.get(day, [])
        row["main_project"] = _main_value(day_sessions, "project_id")
        row["main_subject"] = _main_value(day_sessions, "subject_id")
    return [rows[day] for day in sorted(rows)]


def calculate_learning_streaks(daily_summary: list[dict[str, Any]]) -> dict[str, Any]:
    studied_days = {
        _parse_date(row.get("date"))
        for row in daily_summary
        if _num(row.get("total_duration_sec")) > 0
    }
    studied_days = {day for day in studied_days if day}
    if not daily_summary:
        return {"current_streak": 0, "longest_streak": 0, "learning_days": 0}

    ordered_days = [_parse_date(row.get("date")) for row in daily_summary]
    ordered_days = [day for day in ordered_days if day]
    anchor = max(ordered_days) if ordered_days else app_today()
    current = 0
    day = anchor
    while day in studied_days:
        current += 1
        day -= timedelta(days=1)

    longest = 0
    running = 0
    for day in sorted(ordered_days):
        if day in studied_days:
            running += 1
            longest = max(longest, running)
        else:
            running = 0
    return {"current_streak": current, "longest_streak": longest, "learning_days": len(studied_days)}


def build_subject_growth_summary(
    start_date: date | None = None,
    end_date: date | None = None,
    filters: dict[str, Any] | None = None,
    *,
    snapshot: LearningDataSnapshot | None = None,
    master: MasterSnapshot | None = None,
) -> list[dict[str, Any]]:
    sessions = [session for session in (snapshot.sessions if snapshot is not None else _load_study_sessions_readonly()) if _matches_filters(session, filters)]
    start, end = _default_period(sessions, start_date, end_date)
    rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "subject_id": "",
            "subject_name": "未設定",
            "duration_sec": 0,
            "hours": 0.0,
            "session_count": 0,
            "note_count": 0,
            "card_count": 0,
            "reminder_count": 0,
            "done_reminder_count": 0,
            "pending_reminder_count": 0,
        }
    )

    def row_for(record: dict[str, Any]) -> dict[str, Any]:
        nonlocal master
        subject_id = record.get("subject_id") or ""
        if subject_id not in rows:
            row = rows[subject_id]
            row["subject_id"] = subject_id
            if subject_id:
                if master is None:
                    master = MasterSnapshot.load()
                row["subject_name"] = master.name("subject_id", subject_id)
        return rows[subject_id]

    for session in sessions:
        day = _parse_date(session.get("start_at") or session.get("end_at"))
        if day and start <= day <= end:
            row = row_for(session)
            row["duration_sec"] += _num(session.get("duration_sec"))
            row["session_count"] += 1

    for note in snapshot.notes if snapshot is not None else _load_notes_readonly():
        day = _parse_date(note.get("created_at") or note.get("updated_at"))
        if day and start <= day <= end and _matches_filters(note, filters):
            row_for(note)["note_count"] += 1

    for card in snapshot.cards if snapshot is not None else _load_flashcards_readonly():
        day = _parse_date(card.get("created_at") or card.get("updated_at"))
        if day and start <= day <= end and _matches_filters(card, filters):
            row_for(card)["card_count"] += 1

    for reminder in snapshot.reminders if snapshot is not None else _load_reminders_readonly():
        day = _parse_date(reminder.get("scheduled_date") or reminder.get("registered") or reminder.get("created_at"))
        if day and start <= day <= end and _matches_filters(reminder, filters):
            row = row_for(reminder)
            row["reminder_count"] += 1
            if reminder.get("status") == "done":
                row["done_reminder_count"] += 1
            else:
                row["pending_reminder_count"] += 1

    for row in rows.values():
        row["hours"] = round(row["duration_sec"] / 3600, 3)
    return sorted(rows.values(), key=lambda item: item["duration_sec"], reverse=True)


def build_monthly_forest_data(
    year: int,
    month: int,
    filters: dict[str, Any] | None = None,
    *,
    snapshot: LearningDataSnapshot | None = None,
) -> list[dict[str, Any]]:
    start = date(year, month, 1)
    end = date(year + (month // 12), (month % 12) + 1, 1) - timedelta(days=1)
    return build_daily_learning_summary(start, end, filters, snapshot=snapshot)


def build_cumulative_learning_curve(
    start_date: date | None = None,
    end_date: date | None = None,
    filters: dict[str, Any] | None = None,
    *,
    snapshot: LearningDataSnapshot | None = None,
) -> list[dict[str, Any]]:
    return cumulative_curve_from_daily(build_daily_learning_summary(start_date, end_date, filters, snapshot=snapshot))


def cumulative_curve_from_daily(daily_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a cumulative curve from an already ordered daily summary."""
    cumulative_sec = 0
    cumulative_money = 0.0
    rows = []
    for row in daily_summary:
        cumulative_sec += _num(row.get("total_duration_sec"))
        cumulative_money += _num(row.get("total_money"))
        item = dict(row)
        item["cumulative_duration_sec"] = cumulative_sec
        item["cumulative_hours"] = round(cumulative_sec / 3600, 3)
        item["cumulative_money"] = round(cumulative_money, 2)
        rows.append(item)
    return rows
