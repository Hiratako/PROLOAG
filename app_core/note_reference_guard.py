"""Prevent stale forms from reintroducing references to deleted notes.

This is deliberately not a general foreign-key validator: legacy unknown IDs
remain valid. Only aliases recorded in the application's un-restored note trash
are guarded, and a live note with that alias takes precedence.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from app_core.json_store import JsonConflictError, JsonReadError, load_json_safe, resolved_json_path


_LIST_LINKS = {"linked_note_ids", "review_note_ids"}


def _is_target(root: Path, path: Path) -> bool:
    try:
        relative = resolved_json_path(path).relative_to(resolved_json_path(root))
    except ValueError:
        return False
    if relative.as_posix() in {"flashcards.json", "reminders.json"}:
        return True
    return (
        len(relative.parts) >= 2
        and relative.parts[0] in {"study_logs", "daily_plans"}
        and relative.suffix.lower() == ".json"
        and "_backup_" not in relative.stem.lower()
        and not any(part.lower() in {"backups", "backup"} for part in relative.parts[1:-1])
    )


def _identifier(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise JsonReadError("ノート参照IDの形式が不正です。整合性チェックを確認してください。")
    return str(value)


def _linked_ids(data: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(data, list):
        for item in data:
            result.update(_linked_ids(item))
    elif isinstance(data, dict):
        for key, value in data.items():
            if key == "source_note_id":
                identifier = _identifier(value)
                if identifier:
                    result.add(identifier)
            elif key in _LIST_LINKS:
                if value is None:
                    continue
                if not isinstance(value, list):
                    raise JsonReadError("ノート関連付けが配列ではありません。整合性チェックを確認してください。")
                result.update(identifier for item in value if (identifier := _identifier(item)))
            elif isinstance(value, (dict, list)):
                result.update(_linked_ids(value))
    return result


def _deleted_aliases(trash: Any) -> set[str]:
    if not isinstance(trash, list):
        raise JsonReadError("ノートごみ箱の保存形式が不正です。参照を安全に確認できないため保存しません。")
    deleted: set[str] = set()
    for item in trash:
        if not isinstance(item, dict) or not isinstance(item.get("note"), dict):
            raise JsonReadError("ノートごみ箱の記録が不正です。参照を安全に確認できないため保存しません。")
        note = item["note"]
        aliases = {_identifier(value) for value in (item.get("note_id"), note.get("note_id"), note.get("id"))}
        aliases.discard("")
        if not aliases:
            raise JsonReadError("ノートごみ箱にノートIDのない記録があります。整合性を確認してください。")
        restored_at = item.get("restored_at")
        if restored_at is not None and not isinstance(restored_at, str):
            raise JsonReadError("ノートごみ箱の復元状態が不正です。整合性を確認してください。")
        if not restored_at:
            deleted.update(aliases)
    return deleted


def _live_aliases(store: Any) -> set[str]:
    def rows(value: Any, label: str) -> Iterator[dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise JsonReadError(f"ノートの{label}構造が不正です。復元済みIDを確認できないため保存しません。")
        yield from value

    if not isinstance(store, dict):
        raise JsonReadError("ノートの保存形式が不正です。復元済みIDを確認できないため保存しません。")
    aliases: set[str] = set()
    for notebook in rows(store.get("notebooks", []), "ノートブック"):
        for section in rows(notebook.get("sections", []), "セクション"):
            for page in rows(section.get("pages", []), "ページ"):
                aliases.update(identifier for value in (page.get("note_id"), page.get("id")) if (identifier := _identifier(value)))
    return aliases


def validate_note_references(root: Path, path: Path, data: Any) -> None:
    """Validate live or staged after-data while the caller holds its root lock."""
    root = resolved_json_path(root)
    path = Path(path)
    path = path if path.is_absolute() else root / path
    if not _is_target(root, path):
        return
    linked = _linked_ids(data)
    if not linked:
        return
    # Use the public reader: in a batch this observes the complete staged final
    # state and makes these documents part of the operation's revision guards.
    deleted = _deleted_aliases(load_json_safe(root / "backups" / "note_trash.json", []))
    denied = linked & deleted
    if not denied:
        return
    live = _live_aliases(load_json_safe(root / "notes_data" / "notebooks.json", {"notebooks": []}))
    denied -= live
    if denied:
        raise JsonConflictError(
            "削除済みノートへの参照が含まれています。古い入力から参照を復活させないため保存しません。"
            "入力を確認して最新データを再読込するか、先にノートを復元してください。"
        )
