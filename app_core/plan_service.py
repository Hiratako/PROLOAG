from __future__ import annotations

from datetime import date, datetime, timedelta
from app_core.app_clock import app_today
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from app_core.json_store import json_transaction_for


def _root(root: Path | None = None) -> Path:
    return root or PROJECT_ROOT


def get_plan_path(target: date | datetime | str | None = None, root: Path | None = None) -> Path:
    from data_utils import daily_plan_path

    return daily_plan_path(_root(root), target)


def load_daily_plan(target: date | datetime | str | None = None, root: Path | None = None, *, write_back: bool = False) -> dict[str, Any]:
    from data_utils import load_daily_plan as _load

    return _load(_root(root), target, write_back=write_back)


def save_daily_plan(target: date | datetime | str | None, plan: dict[str, Any], root: Path | None = None) -> None:
    from data_utils import save_daily_plan as _save

    if target and not plan.get("date"):
        plan["date"] = target.isoformat() if hasattr(target, "isoformat") else str(target)
    _save(_root(root), plan)


def ensure_daily_plan(target: date | datetime | str | None = None, root: Path | None = None) -> dict[str, Any]:
    return load_daily_plan(target, root, write_back=True)


@json_transaction_for(lambda target, task, root=None: get_plan_path(target, root))
def add_task(target: date | datetime | str | None, task: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    from data_utils import generate_id, save_daily_plan as _save

    plan = load_daily_plan(target, root)
    existing_ids = {item.get("task_id", "") for item in plan.get("tasks", [])}
    task = dict(task)
    task.setdefault("task_id", generate_id("task", plan.get("date"), existing_ids))
    task.setdefault("linked_note_ids", [])
    task.setdefault("linked_session_ids", [])
    plan.setdefault("tasks", []).append(task)
    _save(_root(root), plan)
    return task


@json_transaction_for(lambda target, task_id, updates, root=None: get_plan_path(target, root))
def update_task(target: date | datetime | str | None, task_id: str, updates: dict[str, Any], root: Path | None = None) -> bool:
    from data_utils import save_daily_plan as _save

    plan = load_daily_plan(target, root)
    for task in plan.get("tasks", []):
        if task.get("task_id") == task_id:
            task.update(updates)
            _save(_root(root), plan)
            return True
    return False


@json_transaction_for(lambda target, task_id, root=None: get_plan_path(target, root))
def delete_task(target: date | datetime | str | None, task_id: str, root: Path | None = None) -> bool:
    from data_utils import save_daily_plan as _save

    plan = load_daily_plan(target, root)
    before = len(plan.get("tasks", []))
    plan["tasks"] = [task for task in plan.get("tasks", []) if task.get("task_id") != task_id]
    if len(plan["tasks"]) != before:
        _save(_root(root), plan)
        return True
    return False


@json_transaction_for(lambda from_date, to_date, root=None: get_plan_path(to_date, root))
def carry_over_unfinished_tasks(from_date: date, to_date: date, root: Path | None = None) -> int:
    from data_utils import generate_id, save_daily_plan as _save

    source = load_daily_plan(from_date, root, write_back=False)
    target = load_daily_plan(to_date, root)
    existing_ids = {task.get("task_id", "") for task in target.get("tasks", [])}
    carried = {task.get("carried_from_task_id") for task in target.get("tasks", [])}
    count = 0
    for task in source.get("tasks", []):
        if task.get("status") not in ("not_started", "in_progress", "pending") or task.get("task_id") in carried:
            continue
        new_task = dict(task)
        new_task["task_id"] = generate_id("task", target.get("date"), existing_ids)
        new_task["status"] = "not_started"
        new_task["linked_session_ids"] = []
        new_task["carried_from_task_id"] = task.get("task_id", "")
        target.setdefault("tasks", []).append(new_task)
        count += 1
    if count:
        _save(_root(root), target)
    return count


def resolve_task_title(task_id: str | None, root: Path | None = None) -> str:
    from data_utils import resolve_task_title as _resolve

    return _resolve(_root(root), task_id)


def get_today_tasks(root: Path | None = None) -> list[dict[str, Any]]:
    from data_utils import load_today_tasks

    return load_today_tasks(_root(root))


@json_transaction_for(lambda target, task_id, session_id, root=None: get_plan_path(target or app_today(), root))
def attach_session_to_task(target: date | None, task_id: str | None, session_id: str | None, root: Path | None = None) -> None:
    from data_utils import link_session_to_plan_task

    if target and target != app_today():
        plan = load_daily_plan(target, root)
        for task in plan.get("tasks", []):
            if task.get("task_id") == task_id:
                linked = task.setdefault("linked_session_ids", [])
                if session_id and session_id not in linked:
                    linked.append(session_id)
                save_daily_plan(target, plan, root)
                return
    link_session_to_plan_task(_root(root), task_id, session_id)

