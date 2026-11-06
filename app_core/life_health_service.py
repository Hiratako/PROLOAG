from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from app_core.exercise_service import build_exercise_summary, load_exercise_logs_range
from app_core.health_service import build_health_study_joined_summary


EXERCISE_TYPE_LABELS = {
    "running": "ランニング",
    "walking": "ウォーキング",
    "strength": "筋トレ",
    "stretch": "ストレッチ",
    "other": "その他",
}


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _date_range(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _exercise_detail_by_date(
    start_date: date,
    end_date: date,
    *,
    exercise_records: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "exercise_types": [],
            "exercise_pace_min_per_km": 0.0,
            "exercise_pace_source_count": 0,
        }
    )
    if exercise_records is None:
        exercise_records = load_exercise_logs_range(start_date, end_date)
    for record in exercise_records:
        day = str(record.get("date") or "")[:10]
        if not day:
            continue
        label = EXERCISE_TYPE_LABELS.get(str(record.get("type") or "other"), "その他")
        if label not in grouped[day]["exercise_types"]:
            grouped[day]["exercise_types"].append(label)
        pace = _num(record.get("pace_min_per_km"))
        if pace > 0:
            count = int(grouped[day]["exercise_pace_source_count"])
            current = _num(grouped[day]["exercise_pace_min_per_km"])
            grouped[day]["exercise_pace_min_per_km"] = round(((current * count) + pace) / (count + 1), 2)
            grouped[day]["exercise_pace_source_count"] = count + 1
    return grouped


def build_life_health_dashboard_summary(start_date: date, end_date: date) -> list[dict[str, Any]]:
    """Join health, exercise, study and plan summaries on the date axis."""
    health_rows = {
        str(row.get("date") or "")[:10]: dict(row)
        for row in build_health_study_joined_summary(start_date, end_date)
        if row.get("date")
    }
    exercise_records = load_exercise_logs_range(start_date, end_date)
    study_duration_by_date = {day: row.get("study_duration_sec") for day, row in health_rows.items()}
    exercise_rows = {
        str(row.get("date") or "")[:10]: dict(row)
        for row in build_exercise_summary(
            start_date, end_date,
            exercise_records=exercise_records,
            study_duration_by_date=study_duration_by_date,
        )
        if row.get("date")
    }
    exercise_details = _exercise_detail_by_date(start_date, end_date, exercise_records=exercise_records)

    rows: list[dict[str, Any]] = []
    for target in _date_range(start_date, end_date):
        key = target.isoformat()
        health = health_rows.get(key, {})
        exercise = exercise_rows.get(key, {})
        detail = exercise_details.get(key, {})
        rows.append(
            {
                "date": key,
                "has_health_log": bool(health.get("has_health_log")),
                "sleep_duration_min": int(_num(health.get("sleep_duration_min"))),
                "sleep_quality": int(_num(health.get("sleep_quality"))),
                "morning_sleepiness": int(_num(health.get("morning_sleepiness"))),
                "fatigue": int(_num(health.get("fatigue"))),
                "stress": int(_num(health.get("stress"))),
                "focus_score": int(_num(health.get("focus_score"))),
                "mood_score": int(_num(health.get("mood_score"))),
                "physical_score": int(_num(health.get("physical_score"))),
                "study_duration_sec": _num(health.get("study_duration_sec")),
                "study_hours": round(_num(health.get("study_duration_sec")) / 3600, 3),
                "study_session_count": int(_num(health.get("study_session_count"))),
                "completed_task_count": int(_num(health.get("completed_task_count"))),
                "task_count": int(_num(health.get("task_count"))),
                "task_completion_rate": (
                    round(float(health["task_completion_rate"]), 1)
                    if health.get("task_completion_rate") is not None
                    else None
                ),
                "note_count": int(_num(health.get("note_count"))),
                "card_count": int(_num(health.get("card_count"))),
                "has_exercise_log": bool(exercise.get("has_exercise_log")),
                "exercise_record_count": int(_num(exercise.get("record_count"))),
                "exercise_duration_min": int(_num(exercise.get("exercise_duration_min"))),
                "exercise_distance_km": round(_num(exercise.get("distance_km")), 3),
                "exercise_avg_intensity": round(_num(exercise.get("avg_intensity")), 2),
                "exercise_avg_fatigue_after": round(_num(exercise.get("avg_fatigue_after")), 2),
                "exercise_types": " / ".join(detail.get("exercise_types") or []),
                "exercise_pace_min_per_km": round(_num(detail.get("exercise_pace_min_per_km")), 2),
            }
        )
    return rows


def calculate_life_health_kpis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    period_days = max(len(rows), 1)
    health_rows = [row for row in rows if row.get("has_health_log")]
    exercise_rows = [row for row in rows if row.get("has_exercise_log")]
    task_total = sum(int(_num(row.get("task_count"))) for row in rows)
    completed_total = sum(int(_num(row.get("completed_task_count"))) for row in rows)
    study_sec = sum(_num(row.get("study_duration_sec")) for row in rows)

    def average(key: str, source: list[dict[str, Any]]) -> float:
        values = [_num(row.get(key)) for row in source if _num(row.get(key)) > 0]
        return round(sum(values) / len(values), 2) if values else 0.0

    return {
        "health_record_days": len(health_rows),
        "average_sleep_hours": round(average("sleep_duration_min", health_rows) / 60, 2),
        "average_sleep_quality": average("sleep_quality", health_rows),
        "average_physical_score": average("physical_score", health_rows),
        "average_fatigue": average("fatigue", health_rows),
        "average_stress": average("stress", health_rows),
        "average_focus_score": average("focus_score", health_rows),
        "total_study_sec": study_sec,
        "average_study_sec_per_day": int(study_sec / period_days),
        "study_session_count": sum(int(_num(row.get("study_session_count"))) for row in rows),
        "exercise_days": len(exercise_rows),
        "total_exercise_minutes": sum(int(_num(row.get("exercise_duration_min"))) for row in rows),
        "total_distance_km": round(sum(_num(row.get("exercise_distance_km")) for row in rows), 2),
        "task_completion_rate": round((completed_total / task_total) * 100, 1) if task_total else None,
        "completed_task_count": completed_total,
        "task_count": task_total,
    }


def build_life_health_comments(
    rows: list[dict[str, Any]],
    kpis: dict[str, Any],
    include_task_metrics: bool = True,
) -> list[str]:
    comments: list[str] = []
    health_rows = [row for row in rows if row.get("has_health_log")]
    if len(health_rows) < 5:
        comments.append("データ数が少ないため、生活コンディションと学習状況の傾向は参考値です。")

    high_focus = [row for row in health_rows if _num(row.get("focus_score")) >= 4]
    low_focus = [row for row in health_rows if 0 < _num(row.get("focus_score")) <= 2]
    if high_focus and low_focus:
        high_avg = sum(_num(row.get("study_duration_sec")) for row in high_focus) / len(high_focus)
        low_avg = sum(_num(row.get("study_duration_sec")) for row in low_focus) / len(low_focus)
        if high_avg > low_avg + 1800:
            comments.append("記録上は、集中しやすさが高い日の勉強時間が長めに見える可能性があります。")

    short_sleep = [row for row in health_rows if 0 < _num(row.get("sleep_duration_min")) < 360]
    enough_sleep = [row for row in health_rows if _num(row.get("sleep_duration_min")) >= 420]
    if short_sleep and enough_sleep:
        short_avg = sum(_num(row.get("study_duration_sec")) for row in short_sleep) / len(short_sleep)
        enough_avg = sum(_num(row.get("study_duration_sec")) for row in enough_sleep) / len(enough_sleep)
        if short_avg + 1800 < enough_avg:
            comments.append("記録上は、睡眠時間が短い日の勉強時間が少なめに見える傾向があります。")

    if include_task_metrics:
        high_fatigue = [row for row in health_rows if _num(row.get("fatigue")) >= 4 and _num(row.get("task_count")) > 0]
        low_fatigue = [row for row in health_rows if 0 < _num(row.get("fatigue")) <= 2 and _num(row.get("task_count")) > 0]
        if high_fatigue and low_fatigue:
            high_rate = sum(_num(row.get("task_completion_rate")) for row in high_fatigue) / len(high_fatigue)
            low_rate = sum(_num(row.get("task_completion_rate")) for row in low_fatigue) / len(low_fatigue)
            if high_rate + 10 < low_rate:
                comments.append("疲労感が高い日のタスク達成率は、記録上やや控えめに見える可能性があります。")

    exercise_days = int(_num(kpis.get("exercise_days")))
    if exercise_days:
        comments.append(f"選択期間では{exercise_days}日に運動記録があります。学習時間との並びを継続して見ると、生活リズムの傾向を確認しやすくなります。")

    if not comments:
        comments.append("選択期間では大きな偏りは目立ちません。記録を継続すると、生活コンディションと学習状況の関係をより確認しやすくなります。")
    return comments[:5]
