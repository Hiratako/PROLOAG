"""Input, preview and comparison helpers for date-only study records."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date
import re
from numbers import Integral
import unicodedata

from app_core.rough_study_service import MAX_DURATION_MINUTES


def _parse_duration_text(text: str) -> int:
    minutes = re.fullmatch(r"([0-9]{1,4})\s*(?:分)?", text)
    if minutes:
        return int(minutes[1])

    clock_duration = re.fullmatch(r"([0-9]{1,2})\s*:\s*([0-5]?[0-9])", text)
    japanese_duration = re.fullmatch(r"([0-9]{1,2})\s*時間(?:\s*([0-5]?[0-9])\s*分)?", text)
    duration = clock_duration or japanese_duration
    if duration:
        return int(duration[1]) * 60 + int(duration[2] or 0)
    raise ValueError("学習時間は「90分」「1:30」「1時間30分」のように入力してください。")


def parse_study_minutes(value: object) -> int | None:
    """Read a duration in whole minutes. A bare integer means minutes."""
    if value is None:
        return None
    if isinstance(value, str):
        text = unicodedata.normalize("NFKC", value).strip()
        if not text:
            return None
        minutes = _parse_duration_text(text)
    elif isinstance(value, Integral) and not isinstance(value, bool):
        minutes = int(value)
    else:
        raise ValueError("学習時間は「90分」「1:30」「1時間30分」のように入力してください。")
    if not 1 <= minutes <= MAX_DURATION_MINUTES:
        raise ValueError("学習時間は1分から24時間までで入力してください。")
    return minutes


_EDIT_FIELDS = (
    ("study_date", None),
    ("duration_sec", None),
    ("estimated", False),
    ("memo", ""),
    ("project_id", ""),
    ("subject_id", ""),
    ("material_id", ""),
    ("unit_id", ""),
)


def study_edit_diff(before: list[dict], after: list[dict]) -> dict[str, list[dict]]:
    """Compare canonical saved and proposed rows by the saved record ID."""
    original = {row["record_id"]: row for row in before}
    seen = set()
    changes: dict[str, list[dict]] = {"added": [], "changed": [], "deleted": []}
    for row in after:
        record_id = row.get("record_id")
        if record_id not in original:
            changes["added"].append(row)
            continue
        seen.add(record_id)
        old = original[record_id]
        if any(old.get(field, default) != row.get(field, default) for field, default in _EDIT_FIELDS):
            changes["changed"].append({"before": old, "after": row})
    changes["deleted"] = [row for record_id, row in original.items() if record_id not in seen]
    return changes


def summarize_study_selection(
    rows: list[dict], existing: dict[date, dict], *, scope_days: Iterable[date] | None = None,
) -> dict[str, int | float]:
    """Return entered and already recorded time without counting any day twice."""
    selected = {date.fromisoformat(row["study_date"]) for row in rows}
    scope = selected if scope_days is None else set(scope_days)
    registered_seconds = 0
    for day in scope:
        registered = existing.get(day, {})
        registered_seconds += registered.get("timed_seconds", 0) + registered.get("rough_seconds", 0)
    entered_seconds = sum(row.get("duration_sec") or 0 for row in rows)
    return {
        "selected_days": len(selected),
        "record_count": len(rows),
        "entered_seconds": entered_seconds,
        "existing_seconds": registered_seconds,
        "result_seconds": registered_seconds + entered_seconds,
        "unknown_count": sum(row.get("duration_sec") is None for row in rows),
    }


def merge_period_table_rows(
    generated: list[dict], previous_rows: list[dict], previous_defaults: list[dict],
) -> list[dict]:
    """Apply prior manual adjustments to the dates still covered by the period."""
    previous = {row["study_date"]: row for row in previous_rows}
    defaults = {row["study_date"]: row for row in previous_defaults}
    result = []
    for row in generated:
        day = row["study_date"]
        merged = row.copy()
        adjusted = previous.get(day)
        if adjusted is not None:
            if "input_id" in adjusted:
                merged["input_id"] = adjusted["input_id"]
            old_default = defaults.get(day, {})
            for field in ("include", "duration", "subject_id", "memo"):
                if field in adjusted and (field not in old_default or adjusted[field] != old_default[field]):
                    merged[field] = adjusted[field]
        result.append(merged)
    return result
