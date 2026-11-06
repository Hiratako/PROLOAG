from __future__ import annotations

from datetime import date, datetime, time, timedelta
from app_core.app_clock import app_today
from pathlib import Path
from typing import Any

from config import DAILY_PLANS_DIR, FLASHCARDS_PATH, HEALTH_LOGS_DIR, NOTES_DATA_DIR, STUDY_LOGS_DIR
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


def _to_time_text(value: Any) -> str:
    if isinstance(value, time):
        return value.strftime("%H:%M")
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text[:5], "%H:%M").time()
        return parsed.strftime("%H:%M")
    except ValueError:
        return ""


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def get_health_log_path(target: date | datetime | str | None = None) -> Path:
    target_date = _parse_date(target)
    return HEALTH_LOGS_DIR / f"{target_date:%Y}" / f"{target_date:%m}" / f"{target_date:%Y%m%d}.json"


def default_health_log(target: date | datetime | str | None = None) -> dict[str, Any]:
    target_date = _parse_date(target)
    timestamp = _now_text()
    return {
        "date": target_date.isoformat(),
        "sleep": {
            "bed_time": "23:30",
            "wake_time": "06:30",
            "sleep_duration_min": 420,
            "sleep_quality": 3,
            "morning_sleepiness": 3,
            "midnight_awakenings": 0,
            "nap_minutes": 0,
        },
        "condition": {
            "physical_score": 3,
            "fatigue": 3,
            "stress": 3,
            "focus_score": 3,
            "mood_score": 3,
            "memo": "",
        },
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def normalize_health_log(data: Any, target: date | datetime | str | None = None) -> dict[str, Any]:
    base = default_health_log(target)
    if not isinstance(data, dict):
        return base

    sleep = data.get("sleep") if isinstance(data.get("sleep"), dict) else {}
    condition = data.get("condition") if isinstance(data.get("condition"), dict) else {}
    base["date"] = str(data.get("date") or base["date"])[:10]
    base["sleep"].update({key: sleep.get(key, base["sleep"][key]) for key in base["sleep"]})
    base["condition"].update({key: condition.get(key, base["condition"][key]) for key in base["condition"]})
    base["sleep"]["bed_time"] = _to_time_text(base["sleep"].get("bed_time")) or "23:30"
    base["sleep"]["wake_time"] = _to_time_text(base["sleep"].get("wake_time")) or "06:30"
    base["sleep"]["sleep_duration_min"] = int(_num(base["sleep"].get("sleep_duration_min")))
    for key in ("sleep_quality", "morning_sleepiness"):
        base["sleep"][key] = int(min(5, max(1, _num(base["sleep"].get(key)) or 3)))
    base["sleep"]["midnight_awakenings"] = int(max(0, _num(base["sleep"].get("midnight_awakenings"))))
    base["sleep"]["nap_minutes"] = int(max(0, _num(base["sleep"].get("nap_minutes"))))
    for key in ("physical_score", "fatigue", "stress", "focus_score", "mood_score"):
        base["condition"][key] = int(min(5, max(1, _num(base["condition"].get(key)) or 3)))
    base["condition"]["memo"] = str(base["condition"].get("memo") or "")
    base["created_at"] = data.get("created_at") or base["created_at"]
    base["updated_at"] = data.get("updated_at") or base["created_at"]
    return base


def load_health_log(target: date | datetime | str | None = None) -> dict[str, Any]:
    path = get_health_log_path(target)
    return normalize_health_log(load_json_safe(path, default_health_log(target)), target)


@json_transaction_for(lambda target, data, **_kwargs: get_health_log_path(target))
def save_health_log(target: date | datetime | str | None, data: dict[str, Any], *, expected_revision: Any = _UNSET) -> None:
    target_date = _parse_date(target)
    if expected_revision is _UNSET:
        expected_revision = observed_json_revision(get_health_log_path(target_date))
    existing = load_health_log(target_date) if get_health_log_path(target_date).exists() else default_health_log(target_date)
    payload = normalize_health_log({**existing, **data}, target_date)
    payload["date"] = target_date.isoformat()
    payload["created_at"] = existing.get("created_at") or _now_text()
    payload["updated_at"] = _now_text()
    save_json_safe(get_health_log_path(target_date), payload, expected_revision=expected_revision)


def ensure_health_log(target: date | datetime | str | None = None) -> dict[str, Any]:
    return load_health_log(target)


def calculate_sleep_duration_minutes(bed_time: Any, wake_time: Any) -> int:
    bed_text = _to_time_text(bed_time)
    wake_text = _to_time_text(wake_time)
    if not bed_text or not wake_text:
        return 0
    try:
        base_day = date(2000, 1, 1)
        bed_dt = datetime.combine(base_day, datetime.strptime(bed_text, "%H:%M").time())
        wake_dt = datetime.combine(base_day, datetime.strptime(wake_text, "%H:%M").time())
    except ValueError:
        return 0
    if wake_dt <= bed_dt:
        wake_dt += timedelta(days=1)
    return int((wake_dt - bed_dt).total_seconds() // 60)


def _study_log_path(target: date) -> Path:
    return STUDY_LOGS_DIR / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"


def _study_duration_sec(target: date) -> float:
    data = load_json_safe(_study_log_path(target), [])
    return sum(_num(item.get("duration_sec")) for _, item in unpack_study_document(data).records)


def _study_summary(target: date) -> dict[str, Any]:
    data = load_json_safe(_study_log_path(target), [])
    sessions = [item for _, item in unpack_study_document(data).records]
    duration_sec = sum(_num(item.get("duration_sec")) for item in sessions)
    return {
        "study_duration_sec": duration_sec,
        "study_hours": round(duration_sec / 3600, 3),
        "study_session_count": len(sessions),
    }


def build_study_summary_by_date(start_date: date, end_date: date) -> dict[str, dict[str, Any]]:
    """Read each day's study log once for summaries built in the same rerun."""
    return {target.isoformat(): _study_summary(target) for target in _date_range(start_date, end_date)}


def _daily_plan_path(target: date) -> Path:
    return DAILY_PLANS_DIR / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"


def _task_summary(target: date) -> dict[str, Any]:
    plan = load_json_safe(_daily_plan_path(target), {})
    tasks = plan.get("tasks", []) if isinstance(plan, dict) else []
    tasks = [task for task in tasks if isinstance(task, dict)]
    completed = [
        task
        for task in tasks
        if str(task.get("status") or "").lower() in {"done", "completed", "complete"}
    ]
    total = len(tasks)
    completed_count = len(completed)
    return {
        "completed_task_count": completed_count,
        "task_count": total,
        "task_completion_rate": round((completed_count / total) * 100, 1) if total else None,
    }


def _parse_item_date(value: Any) -> date | None:
    from app_core.analysis_dates import parse_analysis_date

    return parse_analysis_date(value)


def _load_notes_flat() -> list[dict[str, Any]]:
    store = load_json_safe(NOTES_DATA_DIR / "notebooks.json", {"notebooks": []})
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
                    notes.append(page)
    return notes


def _created_count_by_date(items: list[dict[str, Any]], target: date) -> int:
    count = 0
    for item in items:
        item_date = _parse_item_date(item.get("created_at") or item.get("updated_at"))
        if item_date == target:
            count += 1
    return count


def _created_counts_by_date(items: list[dict[str, Any]]) -> dict[date, int]:
    """Index one render's knowledge items without changing date precedence."""
    counts: dict[date, int] = {}
    for item in items:
        item_date = _parse_item_date(item.get("created_at") or item.get("updated_at"))
        if item_date is not None:
            counts[item_date] = counts.get(item_date, 0) + 1
    return counts


def _knowledge_counts(target: date, notes: list[dict[str, Any]] | None = None, cards: list[dict[str, Any]] | None = None) -> dict[str, int]:
    notes = notes if notes is not None else _load_notes_flat()
    if cards is None:
        raw_cards = load_json_safe(FLASHCARDS_PATH, [])
        cards = [item for item in raw_cards if isinstance(item, dict)] if isinstance(raw_cards, list) else []
    return {
        "note_count": _created_count_by_date(notes, target),
        "card_count": _created_count_by_date(cards, target),
    }


def load_health_logs_range(start_date: date, end_date: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target in _date_range(start_date, end_date):
        path = get_health_log_path(target)
        if path.exists():
            rows.append(load_health_log(target))
    return rows


def build_health_summary(
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    study_summaries: dict[str, dict[str, Any]] | None = None,
    health_logs_by_date: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build daily rows; a supplied study snapshot treats missing days as zero."""
    end_date = end_date or app_today()
    start_date = start_date or (end_date - timedelta(days=30))
    rows: list[dict[str, Any]] = []
    for target in _date_range(start_date, end_date):
        has_health_log = get_health_log_path(target).exists()
        if health_logs_by_date is not None and target.isoformat() in health_logs_by_date:
            log = health_logs_by_date[target.isoformat()]
        else:
            log = load_health_log(target) if has_health_log else default_health_log(target)
        sleep = log.get("sleep", {})
        condition = log.get("condition", {})
        study_sec = (
            _num(study_summaries.get(target.isoformat(), {}).get("study_duration_sec"))
            if study_summaries is not None
            else _study_duration_sec(target)
        )
        rows.append(
            {
                "date": target.isoformat(),
                "bed_time": sleep.get("bed_time", ""),
                "wake_time": sleep.get("wake_time", ""),
                "sleep_duration_min": int(_num(sleep.get("sleep_duration_min"))) if has_health_log else 0,
                "sleep_quality": int(_num(sleep.get("sleep_quality"))) if has_health_log else 0,
                "morning_sleepiness": int(_num(sleep.get("morning_sleepiness"))) if has_health_log else 0,
                "midnight_awakenings": int(_num(sleep.get("midnight_awakenings"))) if has_health_log else 0,
                "nap_minutes": int(_num(sleep.get("nap_minutes"))) if has_health_log else 0,
                "physical_score": int(_num(condition.get("physical_score"))) if has_health_log else 0,
                "fatigue": int(_num(condition.get("fatigue"))) if has_health_log else 0,
                "stress": int(_num(condition.get("stress"))) if has_health_log else 0,
                "focus_score": int(_num(condition.get("focus_score"))) if has_health_log else 0,
                "mood_score": int(_num(condition.get("mood_score"))) if has_health_log else 0,
                "memo": condition.get("memo", "") if has_health_log else "",
                "study_duration_sec": study_sec,
                "study_hours": round(study_sec / 3600, 3),
                "has_health_log": has_health_log,
            }
        )
    return rows


def build_health_study_joined_summary(
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    health_summary: list[dict[str, Any]] | None = None,
    study_summaries: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Join same-period snapshots without rereading their health or study logs."""
    end_date = end_date or app_today()
    start_date = start_date or (end_date - timedelta(days=30))
    if study_summaries is None:
        study_summaries = build_study_summary_by_date(start_date, end_date)
    if health_summary is None:
        health_summary = build_health_summary(start_date, end_date, study_summaries=study_summaries)
    notes = _load_notes_flat()
    raw_cards = load_json_safe(FLASHCARDS_PATH, [])
    cards = [item for item in raw_cards if isinstance(item, dict)] if isinstance(raw_cards, list) else []
    note_counts = _created_counts_by_date(notes)
    card_counts = _created_counts_by_date(cards)
    rows: list[dict[str, Any]] = []
    for row in health_summary:
        target = _parse_date(row.get("date"))
        joined = dict(row)
        joined.update(study_summaries.get(target.isoformat(), {
            "study_duration_sec": 0, "study_hours": 0.0, "study_session_count": 0,
        }))
        joined.update(_task_summary(target))
        joined.update({"note_count": note_counts.get(target, 0), "card_count": card_counts.get(target, 0)})
        rows.append(joined)
    return rows


def calculate_health_study_indicators(joined_rows: Any) -> dict[str, Any]:
    rows = joined_rows.to_dict("records") if hasattr(joined_rows, "to_dict") else list(joined_rows or [])
    health_rows = [row for row in rows if row.get("has_health_log")]
    if not health_rows:
        return {
            "record_days": 0,
            "average_sleep_hours": 0.0,
            "average_study_hours": 0.0,
            "average_task_completion_rate": None,
        }
    task_rows = [row for row in health_rows if _num(row.get("task_count")) > 0]
    return {
        "record_days": len(health_rows),
        "average_sleep_hours": round(sum(_num(row.get("sleep_duration_min")) for row in health_rows) / len(health_rows) / 60, 2),
        "average_study_hours": round(sum(_num(row.get("study_duration_sec")) for row in health_rows) / len(health_rows) / 3600, 2),
        "average_task_completion_rate": (
            round(sum(_num(row.get("task_completion_rate")) for row in task_rows) / len(task_rows), 1)
            if task_rows
            else None
        ),
    }


def build_health_commentary(joined_rows: Any, include_task_metrics: bool = True) -> list[str]:
    rows = joined_rows.to_dict("records") if hasattr(joined_rows, "to_dict") else list(joined_rows or [])
    rows = [row for row in rows if row.get("has_health_log")]
    comments: list[str] = []
    if len(rows) < 5:
        return ["まだデータが少ないため、傾向を見るには継続的な記録が必要です。"]

    short_sleep = [row for row in rows if _num(row.get("sleep_duration_min")) and _num(row.get("sleep_duration_min")) < 360]
    enough_sleep = [row for row in rows if _num(row.get("sleep_duration_min")) >= 420]
    if short_sleep and enough_sleep:
        short_avg = sum(_num(row.get("study_duration_sec")) for row in short_sleep) / len(short_sleep)
        enough_avg = sum(_num(row.get("study_duration_sec")) for row in enough_sleep) / len(enough_sleep)
        if short_avg + 1800 < enough_avg:
            comments.append("記録上は、睡眠時間が短い日の勉強時間が少ない傾向があります。")

    high_focus = [row for row in rows if _num(row.get("focus_score")) >= 4]
    low_focus = [row for row in rows if 0 < _num(row.get("focus_score")) <= 2]
    if high_focus and low_focus:
        high_avg = sum(_num(row.get("study_duration_sec")) for row in high_focus) / len(high_focus)
        low_avg = sum(_num(row.get("study_duration_sec")) for row in low_focus) / len(low_focus)
        if high_avg > low_avg + 1800:
            comments.append("集中しやすさが高い日に学習時間も長い可能性があります。")

    if include_task_metrics:
        high_fatigue = [row for row in rows if _num(row.get("fatigue")) >= 4 and _num(row.get("task_count")) > 0]
        low_fatigue = [row for row in rows if 0 < _num(row.get("fatigue")) <= 2 and _num(row.get("task_count")) > 0]
        if high_fatigue and low_fatigue:
            high_rate = sum(_num(row.get("task_completion_rate")) for row in high_fatigue) / len(high_fatigue)
            low_rate = sum(_num(row.get("task_completion_rate")) for row in low_fatigue) / len(low_fatigue)
            if high_rate + 10 < low_rate:
                comments.append("疲労感が高い日のタスク達成率が低めに見える可能性があります。")

    high_stress = [row for row in rows if _num(row.get("stress")) >= 4]
    if high_stress:
        avg_focus = sum(_num(row.get("focus_score")) for row in high_stress) / len(high_stress)
        if avg_focus <= 3:
            comments.append("ストレスが高い日の集中しやすさは、記録上やや控えめです。")

    if not comments:
        comments.append("現時点では大きな偏りは見えません。記録を続けると、生活状態と学習量の関係を確認しやすくなります。")
    return comments
