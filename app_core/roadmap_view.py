"""Read-only roadmap presentation: dates, quantity, time and risk stay distinct.

The helpers never write records and never use elapsed calendar time or logged
study hours to manufacture learning progress, remaining effort or mastery.
"""

from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date, timedelta
import math
from pathlib import Path

from app_core.roadmap_service import STATUS_LABELS, parse_day


MIN_DAY = date(1900, 1, 1)
MAX_DAY = date(2199, 12, 31)
PERIODS = ("週間", "4週間", "月間", "3か月", "全体")
_CLASSIFICATIONS = (
    ("project", "projects", "プロジェクト"),
    ("subject", "subjects", "科目"),
    ("material", "materials", "教材"),
    ("unit", "units", "単元"),
)


def _clamp_day(value: date) -> date:
    if type(value) is not date:
        raise ValueError("表示の基準日は日付で指定してください。")
    return min(MAX_DAY, max(MIN_DAY, value))


def period_bounds(anchor: date, view: str, data: dict | None = None) -> tuple[date, date]:
    """Return inclusive display dates; the whole-plan view fits active records.

    A whole plan can legitimately span more than a year. Capacity loading is
    separately bounded, and a renderer must not create one DOM cell per day
    for an arbitrarily long whole-plan interval.
    """
    anchor = _clamp_day(anchor)
    if view not in PERIODS:
        raise ValueError("表示期間を確認してください。")
    if view == "全体" and isinstance(data, dict):
        days = []
        for item in data.get("items", []):
            if item.get("is_archived", False):
                continue
            days.extend((parse_day(item["start_date"]), parse_day(item["end_date"])))
            if item.get("deadline"):
                days.append(parse_day(item["deadline"]))
        days.extend(
            parse_day(item["date"])
            for item in data.get("milestones", [])
            if not item.get("is_archived", False)
        )
        if days:
            return min(days), max(days)
    if view in {"週間", "4週間", "全体"}:
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6 if view == "週間" else 27)
    else:
        start = anchor.replace(day=1)
        final_month = anchor.year * 12 + anchor.month - 1 + (2 if view == "3か月" else 0)
        final_year, final_month = divmod(final_month, 12)
        final_month += 1
        end = date(final_year, final_month, monthrange(final_year, final_month)[1])
    return _clamp_day(start), _clamp_day(end)


def shift_anchor(anchor: date, view: str, steps: int) -> date:
    """Navigate display periods, clamping month-end days and date boundaries."""
    anchor = _clamp_day(anchor)
    if view not in PERIODS or type(steps) is not int:
        raise ValueError("表示期間の移動を確認してください。")
    if view in {"月間", "3か月"}:
        month_index = anchor.year * 12 + anchor.month - 1 + steps * (3 if view == "3か月" else 1)
        if month_index < 1900 * 12:
            return MIN_DAY
        if month_index > 2199 * 12 + 11:
            return MAX_DAY
        year, month = divmod(month_index, 12)
        month += 1
        return date(year, month, min(anchor.day, monthrange(year, month)[1]))
    days = steps * (7 if view == "週間" else 28)
    ordinal = min(MAX_DAY.toordinal(), max(MIN_DAY.toordinal(), anchor.toordinal() + days))
    return date.fromordinal(ordinal)


def _numeric(value, *, minimum=0):
    if type(value) not in (int, float):
        return False
    try:
        return value >= minimum and math.isfinite(value)
    except OverflowError:
        return False


def _quantity(item):
    completed, total = item.get("completed"), item.get("total")
    if not (_numeric(total, minimum=0.000001) and _numeric(completed) and completed <= total):
        return "進捗未確認", None
    if item.get("progress_kind") == "percent":
        if total != 100:
            return "進捗未確認", None
        return f"{completed:g}%", float(completed)
    return f"{completed:g}/{total:g}{item.get('unit', '')}", 100.0 * completed / total


def schedule_risk(item: dict, items: dict[str, dict], today: date) -> tuple[str, str, bool]:
    """A factual date/reference warning, separate from lifecycle state."""
    today = _clamp_day(today)
    if item.get("is_archived", False):
        return "", "", False
    try:
        start, end = parse_day(item["start_date"]), parse_day(item["end_date"])
        deadline = parse_day(item["deadline"]) if item.get("deadline") else None
        for parent_id in item.get("predecessor_ids", []):
            parent = items.get(parent_id)
            if parent is None:
                return "前提工程の参照を確認", "error", True
            if start <= parse_day(parent["end_date"]):
                return "前提工程の日程と衝突", "error", True
    except (KeyError, ValueError, TypeError):
        return "日付・前提を確認", "error", True
    # Completion date is not recorded here: do not invent a historical lateness
    # judgment for completed stages merely because their deadline is now past.
    if item.get("status") == "done":
        return "", "", False
    if deadline is not None and end > deadline:
        return "終了予定が確定期限を超過", "error", True
    if deadline is not None and deadline < today:
        return "確定期限超過", "error", True
    if end < today:
        return "終了予定日超過", "warning", True
    if deadline == today:
        return "本日期限", "notice", True
    if deadline is not None and 0 < (deadline - today).days <= 7:
        return f"期限まで{(deadline - today).days}日", "notice", False
    return "", "", False


def _duration_text(seconds) -> str:
    if seconds == 0:
        return "0分"
    if seconds < 1:
        return "1秒未満"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return "".join(
        f"{value}{unit}"
        for value, unit in ((hours, "時間"), (minutes, "分"), (seconds, "秒"))
        if value
    )


def _actual_view(identifier, allocations, actuals):
    allocation_count = len(allocations)
    if actuals is None or not {"counts", "seconds"}.intersection(actuals):
        return ("未連携", "unlinked", 0, 0, allocation_count, 0) if not allocation_count else (
            "未集計", "uncomputed", 0, 0, allocation_count, 0
        )
    logs = actuals.get("counts", {}).get(identifier, 0)
    if not logs and allocation_count and not {"task_counts", "tasks"}.intersection(actuals):
        return "未集計", "uncomputed", 0, 0, allocation_count, 0
    task_count = actuals.get("task_counts", {}).get(identifier)
    broken = actuals.get("broken_allocation_counts", {}).get(identifier)
    if task_count is None:
        task_count = len({
            (task.get("date"), task.get("task_id"))
            for row in allocations for task in actuals.get("tasks", {}).get(row["id"], [])
        })
    if broken is None:
        broken = sum(not actuals.get("tasks", {}).get(row["id"]) for row in allocations)
    if type(logs) is int and logs > 0:
        seconds = actuals.get("seconds", {}).get(identifier)
        if not _numeric(seconds):
            return "集計を確認", "reference_error", logs, task_count, allocation_count, broken
        label = _duration_text(seconds)
        if broken:
            label += "（参照要確認）"
        return label, "recorded", logs, task_count, allocation_count, broken
    if not allocation_count:
        return "未連携", "unlinked", 0, task_count, 0, broken
    if broken:
        return "参照要確認", "reference_error", 0, task_count, allocation_count, broken
    return "連携済・記録なし", "unrecorded", 0, task_count, allocation_count, 0


def build_canvas_rows(data: dict, master, actuals: dict | None, *, today: date) -> list[dict]:
    """Augment copies with display-only fields; original IDs/values are retained."""
    today = _clamp_day(today)
    master = getattr(master, "data", master)
    master = master if isinstance(master, dict) else {}
    lookups = {}
    for name, collection, label in _CLASSIFICATIONS:
        lookups[name] = {
            row[name + "_id"]: row.get("name") or "名称未設定"
            for row in master.get(collection, []) if isinstance(row, dict) and row.get(name + "_id")
        }
    items = {item["id"]: item for item in data.get("items", [])}
    allocations_by_item = {}
    for allocation in data.get("allocations", []):
        if allocation.get("is_active"):
            allocations_by_item.setdefault(allocation.get("item_id"), []).append(allocation)
    result = []
    for item in data.get("items", []):
        row = deepcopy(item)
        for name, _, label in _CLASSIFICATIONS:
            identifier = item.get(name + "_id", "")
            row["view_" + name] = lookups[name].get(identifier, "未登録の" + label) if identifier else "未設定"
        risk, severity, attention = schedule_risk(item, items, today)
        progress, percentage = _quantity(item)
        label, actual_state, logs, task_count, allocations, broken = _actual_view(
            item["id"], allocations_by_item.get(item["id"], []), actuals
        )
        row.update(
            view_state_label=STATUS_LABELS.get(item.get("status"), "状態未確認"),
            view_risk_label=risk,
            view_risk_severity=severity,
            view_needs_attention=attention,
            view_progress_label=progress,
            view_progress_percent=percentage,
            view_actual_label=label,
            view_actual_state=actual_state,
            view_log_count=logs,
            view_task_count=task_count,
            view_allocation_count=allocations,
            view_missing_links=broken,
            view_group_color_key=item.get("subject_id") or item.get("project_id") or "unassigned",
        )
        result.append(row)
    return result


def build_capacity_strip(root: Path, data: dict, start: date, end: date) -> list[dict]:
    """Read explicitly registered daily tasks once, without inferring allocation.

    This IO helper should only be called when load display is requested or
    refreshed. An absent calendar is unknown capacity; an explicit zero is a
    genuine zero-capacity day. Existing task totals include the values already
    registered on that date and are not estimated from roadmap bar overlaps.
    """
    from app_core.roadmap_planning import load_planning_context, load_rows

    start, end = _clamp_day(start), _clamp_day(end)
    if not 0 <= (end - start).days < 366:
        raise ValueError("負荷表示は366日以内の範囲に切り替えてください。")
    plans, _ = load_planning_context(root, start, end)
    rows = load_rows(data, plans, start, end)
    configured = "calendar" in data
    return [
        {
            "date": row["date"],
            "planned_minutes": row["existing_minutes"],
            "capacity_minutes": row["capacity_minutes"] if configured else None,
            "capacity_configured": configured,
            "over_minutes": row["over_minutes"] if configured else None,
            "free_minutes": row["free_minutes"] if configured else None,
            "task_count": len(plans[row["date"]].get("tasks", [])),
        }
        for row in rows
    ]
