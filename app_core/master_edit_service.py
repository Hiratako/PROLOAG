"""Classification choices and validation for the master editor (no I/O)."""
from __future__ import annotations

from typing import Any

from app_core.json_store import JsonConflictError, JsonStoreError


_FIELDS = {
    "subjects": ("project_id",),
    "materials": ("project_id", "subject_id"),
    "units": ("project_id", "subject_id", "material_id", "parent_unit_id"),
}
_COLLECTIONS = {"project_id": "projects", "subject_id": "subjects", "material_id": "materials", "parent_unit_id": "units"}


def reference_choices(data: dict, collection: str, id_key: str, current_id: str = "", *,
                      parents: dict[str, str] | None = None, exclude_id: str = "") -> dict[str, str]:
    """ID -> label, always retaining the saved value, including blank/orphan IDs."""
    parents = parents or {}
    choices = {"": "未設定"}
    existing = None
    for item in data.get(collection, []):
        identifier = item.get(id_key, "")
        if not identifier:
            continue
        if identifier == current_id:
            existing = item
        if (identifier != exclude_id and item.get("is_active", True)
                and all(item.get(key, "") == value for key, value in parents.items())):
            choices[identifier] = f"{item.get('name') or '名称未設定'} [{identifier}]"
    if current_id and current_id not in choices:
        name = existing.get("name") or "名称未設定" if existing else "未登録"
        reason = "未登録" if existing is None else "無効" if not existing.get("is_active", True) else "所属不一致"
        choices[current_id] = f"{name} [{current_id}]（{reason}・既存値を保持）"
    return choices


def validate_classification(data: dict, collection: str, item: dict, original: dict | None = None) -> None:
    """Unchanged legacy relationships are preserved; edits must form a valid chain.

    This validates only the editor's classification fields, not unrelated master
    fields. It is called again against the fresh data immediately before save.
    """
    fields = _FIELDS.get(collection)
    if not fields:
        return
    id_key = {"subjects": "subject_id", "materials": "material_id", "units": "unit_id"}[collection]
    original = original or {}
    identifier = item.get(id_key, "")
    if identifier:
        stored = next((entry for entry in data.get(collection, []) if entry.get(id_key) == identifier), None)
        if stored is None:
            raise JsonConflictError("編集対象が削除されています。再読込して確認してください。")
        if any(stored.get(key, "") != original.get(key, "") for key in fields):
            raise JsonConflictError("編集中に所属が変更されています。再読込して確認してください。")
        if all(item.get(key, "") == original.get(key, "") for key in fields):
            return
    resolved: dict[str, dict[str, Any]] = {}
    for key in fields:
        value = item.get(key, "")
        if not value:
            if key in ("project_id", "subject_id"):
                raise JsonStoreError("所属を変更するときはプロジェクト・科目を明示的に選択してください。")
            continue
        target_key = "unit_id" if key == "parent_unit_id" else key
        target = next((entry for entry in data.get(_COLLECTIONS[key], []) if entry.get(target_key) == value), None)
        if target is None:
            raise JsonStoreError("未登録の所属を含む変更は保存できません。親から順に選び直してください。")
        if value != original.get(key, "") and not target.get("is_active", True):
            raise JsonStoreError("新しい所属には有効な項目を選択してください。")
        parent_fields = {"project_id": (), "subject_id": ("project_id",),
                         "material_id": ("project_id", "subject_id"),
                         "parent_unit_id": ("project_id", "subject_id", "material_id")}[key]
        if any(target.get(parent, "") != item.get(parent, "") for parent in parent_fields):
            raise JsonStoreError("親と子の所属が一致しません。所属を上から順に選び直してください。")
        resolved[key] = target
    parent = resolved.get("parent_unit_id")
    seen = {identifier} if identifier else set()
    units = {entry.get("unit_id"): entry for entry in data.get("units", [])}
    while parent is not None:
        parent_id = parent.get("unit_id")
        if parent_id in seen:
            raise JsonStoreError("親単元が循環する変更は保存できません。")
        seen.add(parent_id)
        parent = units.get(parent.get("parent_unit_id"))
