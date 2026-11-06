"""Revision-bound edits to an existing daily task; preserve unrelated data."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from app_core import json_store
from app_core.json_operations import atomic_json_operation


EDIT_FIELDS = frozenset({
    "title", "subject", "material", "planned_minutes", "status", "linked_note_ids",
    "project_id", "subject_id", "material_id", "unit_id",
})


def _validate_changed_classification(root: Path, original: dict, updates: dict) -> None:
    keys = ("project_id", "subject_id", "material_id", "unit_id")
    chosen = {key: updates.get(key, original.get(key, "")) for key in keys}
    if chosen == {key: original.get(key, "") for key in keys}:
        return  # Preserve inactive/unknown historic references on unrelated edits.
    from data_utils import load_master_data

    master = load_master_data(root)
    for index, key in enumerate(keys):
        if not chosen[key]:
            continue
        collection = key.removesuffix("_id") + "s"
        matches = [item for item in master.get(collection, []) if item.get(key) == chosen[key]]
        if (len(matches) != 1 or not matches[0].get("is_active", True)
                or any(matches[0].get(parent, "") != chosen[parent] for parent in keys[:index])):
            raise json_store.JsonStoreError("変更後の分類の所属・有効状態が一致しません。プロジェクトから順に選び直してください。")


def save_task_edit(
    root: Path, target: date, task_id: str, updates: dict[str, Any] | None, *,
    expected_revisions: Mapping[Path, str | None], operation_id: str,
) -> bool:
    """Update/delete one task only after the displayed plan revision matches.

    ``None`` means an explicitly requested deletion, not an empty edit. The
    journal makes a repeated successful submission harmless and protects note
    references using the same checks as other record writers. Return True only
    for a newly committed operation; an acknowledged retry must not authorize
    other forms against a newer externally edited revision.
    """
    from data_utils import daily_plan_path, load_daily_plan, save_daily_plan

    if type(target) is not date or not isinstance(task_id, str) or not task_id:
        raise json_store.JsonStoreError("編集するタスクと日付を確認してください。")
    path = json_store.resolved_json_path(daily_plan_path(root, target))
    revisions = {json_store.resolved_json_path(key): value for key, value in expected_revisions.items()}
    if path not in revisions:
        raise json_store.JsonStoreError("編集開始時の計画がありません。タスク一覧を再読込してください。")
    if updates is not None:
        if not isinstance(updates, dict) or set(updates) - EDIT_FIELDS:
            raise json_store.JsonStoreError("タスクの編集項目が不正です。")
        for key in set(updates) - {"planned_minutes", "linked_note_ids"}:
            if not isinstance(updates[key], str):
                raise json_store.JsonStoreError("タスクの文字項目が不正です。")
        if "planned_minutes" in updates and (type(updates["planned_minutes"]) is not int or updates["planned_minutes"] < 0):
            raise json_store.JsonStoreError("予定時間は0以上の整数で入力してください。")
        if "status" in updates and updates["status"] not in {"not_started", "in_progress", "done", "pending"}:
            raise json_store.JsonStoreError("タスクの状態が不正です。")
        if "linked_note_ids" in updates and (not isinstance(updates["linked_note_ids"], list)
                or any(not isinstance(value, str) or not value for value in updates["linked_note_ids"])):
            raise json_store.JsonStoreError("関連ノートの形式が不正です。")
    fingerprint = sha256(json.dumps({"date": target.isoformat(), "task_id": task_id,
        "action": "delete" if updates is None else "update", "updates": updates},
        ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()
    with atomic_json_operation(root=root, kind="daily_task_edit", operation_id=operation_id,
                               expected_revisions=revisions, request_fingerprint=fingerprint) as operation:
        if operation.already_committed:
            return False
        raw = json_store.load_json_safe(path, None)
        if (not isinstance(raw, dict) or raw.get("date", target.isoformat()) != target.isoformat()
                or not isinstance(raw.get("tasks", []), list)
                or any(not isinstance(item, dict) for item in raw.get("tasks", []))):
            raise json_store.JsonStoreError("保存済み計画の形式が不正か削除されています。上書きしません。")
        plan = load_daily_plan(root, target)
        matches = [item for item in plan["tasks"] if item.get("task_id") == task_id]
        if len(matches) != 1:
            raise json_store.JsonStoreError("対象タスクが削除されたかIDが重複しています。自動で追加し直すことはありません。")
        if updates is None:
            plan["tasks"] = [item for item in plan["tasks"] if item.get("task_id") != task_id]
        else:
            _validate_changed_classification(root, matches[0], updates)
            matches[0].update(deepcopy(updates))
        save_daily_plan(root, plan)
    return True
