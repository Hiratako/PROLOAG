"""Revision-bound note editing and previewed, recoverable link cleanup.

Readers never persist normalization. Delete/restore plans keep raw envelopes and
unknown fields, and commit through the shared durable JSON operation service.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import PROJECT_ROOT
from app_core.json_store import (
    JsonConflictError, load_json_safe, observed_json_revision, record_json_files,
    resolved_json_path, save_json_safe,
)

EDITABLE_KEYS = {"title", "body", "content", "tags", "project_id", "subject_id", "material_id", "unit_id"}
NOTE_PATH = "notes_data/notebooks.json"
TRASH_PATH = "backups/note_trash.json"


def _root(root: Path | None) -> Path:
    return resolved_json_path(root or PROJECT_ROOT)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _path(root: Path, relative: str) -> Path:
    path = resolved_json_path(root / relative)
    if not path.is_relative_to(root) or path.relative_to(root).as_posix() != relative:
        raise ValueError("ノート操作の保存先が不正です。")
    return path


def _document(root: Path, relative: str, default: Any = None) -> dict[str, Any]:
    path = _path(root, relative)
    data = load_json_safe(path, default)
    return {"path": relative, "revision": observed_json_revision(path), "before": deepcopy(data), "after": deepcopy(data)}


def _pages(store: Any):
    if not isinstance(store, dict) or not isinstance(store.get("notebooks"), list):
        raise ValueError("ノートの保存形式が不正です。整合性チェックを確認してください。")
    for ni, notebook in enumerate(store["notebooks"]):
        if not isinstance(notebook, dict) or not isinstance(notebook.get("sections"), list):
            raise ValueError("ノートブックの保存形式が不正です。")
        for si, section in enumerate(notebook["sections"]):
            if not isinstance(section, dict) or not isinstance(section.get("pages"), list):
                raise ValueError("セクションの保存形式が不正です。")
            for pi, page in enumerate(section["pages"]):
                if not isinstance(page, dict):
                    raise ValueError("ノート本文の保存形式が不正です。")
                yield (ni, si, pi), notebook, section, page


def load_note_workspace(root: Path | None = None) -> dict[str, Any]:
    from data_utils import default_note_store, normalize_note_store

    base = _root(root)
    document = _document(base, NOTE_PATH)
    raw = document["before"]
    if raw is not None:
        list(_pages(raw))  # Reject malformed data instead of replacing it.
    store, _ = normalize_note_store(deepcopy(raw) if raw is not None else default_note_store())
    return {"store": store, "raw": deepcopy(raw), "revision": document["revision"]}


def capture_note_snapshot(workspace: dict[str, Any], note_id: str) -> dict[str, Any]:
    matches = [item for item in _pages(workspace["store"]) if item[3].get("note_id") == note_id]
    if len(matches) != 1:
        raise ValueError("ノートが見つからないかIDが重複しています。")
    location, notebook, section, page = matches[0]
    raw_lookup = {pos: row for pos, _, _, row in _pages(workspace["raw"])} if workspace["raw"] is not None else {}
    return {
        "revision": workspace["revision"], "location": location, "note": deepcopy(page),
        "raw_note": deepcopy(raw_lookup.get(location)),
        "notebook_id": notebook["id"], "section_id": section["id"],
        "initial_store": deepcopy(workspace["store"]) if location not in raw_lookup else None,
    }


def _source(snapshot: dict[str, Any], root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    document = _document(root, NOTE_PATH)
    if document["revision"] != snapshot["revision"]:
        raise JsonConflictError("別の操作でノートが更新されています。入力は保持しています。内容を確認して明示的に再読込してください。")
    if snapshot.get("initial_store") is not None:
        document["after"] = deepcopy(snapshot["initial_store"])
    location = tuple(snapshot["location"])
    matches = [item for item in _pages(document["after"]) if item[0] == location]
    if len(matches) != 1:
        raise JsonConflictError("ノートが削除・移動されています。再読込してください。")
    _, _, section, page = matches[0]
    if snapshot.get("raw_note") is not None and page != snapshot["raw_note"]:
        raise JsonConflictError("編集開始時のノートと一致しません。再読込してください。")
    return document, section, page


def _saved_snapshot(data: dict[str, Any], note_id: str, *, revision: str | None = None) -> dict[str, Any]:
    """Bind the next edit to what WE committed, never a racing later reload."""
    from data_utils import normalize_note_store

    encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    store, _ = normalize_note_store(deepcopy(data))
    return capture_note_snapshot({"store": store, "raw": deepcopy(data), "revision": revision or hashlib.sha256(encoded).hexdigest()}, note_id)


def save_note_edits(snapshot: dict[str, Any], updates: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    from app_core.json_operations import atomic_json_operation

    if set(updates) - EDITABLE_KEYS:
        raise ValueError("ノートのID・関連付けなどの保護項目は変更できません。")
    base = _root(root)
    with atomic_json_operation(root=base, kind="note_edit", expected_revisions={base / NOTE_PATH: snapshot["revision"]}):
        document, _, page = _source(snapshot, base)
        page.update(deepcopy(updates))
        page.setdefault("note_id", snapshot["note"]["note_id"])
        page.setdefault("id", page["note_id"])
        page["updated_at"] = _now()
        save_json_safe(base / NOTE_PATH, document["after"], expected_revision=snapshot["revision"])
        saved = _saved_snapshot(document["after"], page["note_id"],
                                revision=document["revision"] if document["before"] == document["after"] else None)
    return saved


def save_note_workspace(workspace: dict[str, Any], store: dict[str, Any], *, root: Path | None = None) -> None:
    """Guard hierarchy additions against an intervening edit.

    Keep untouched raw pages (including legacy aliases/unknown fields) rather
    than persisting the read-time normalization of the whole notebook store.
    """
    from app_core.json_operations import atomic_json_operation

    result = deepcopy(store)
    if workspace["raw"] is not None:
        original = {pos: page for pos, _, _, page in _pages(workspace["raw"])}
        normalized = {pos: page for pos, _, _, page in _pages(workspace["store"])}
        for pos, _, section, page in list(_pages(result)):
            if pos in original and page == normalized.get(pos):
                section["pages"][pos[2]] = deepcopy(original[pos])
    base = _root(root)
    with atomic_json_operation(root=base, kind="note_add", expected_revisions={base / NOTE_PATH: workspace["revision"]}):
        save_json_safe(base / NOTE_PATH, result, expected_revision=workspace["revision"])


def _list(value: Any) -> list:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("関連付けが配列ではありません。整合性チェックを確認してください。")
    return value


def _reference_documents(root: Path) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    inventories = {
        folder: sorted(resolved_json_path(path).relative_to(root).as_posix() for path in record_json_files(root / folder))
        for folder in ("study_logs", "daily_plans")
    }
    relatives = [*inventories["study_logs"], *inventories["daily_plans"], "flashcards.json", "reminders.json"]
    return [_document(root, relative) for relative in relatives], inventories


def _records(document: dict[str, Any]):
    relative, data = document["path"], document["after"]
    if data is None:
        return
    if relative.startswith("daily_plans/"):
        if not isinstance(data, dict):
            raise ValueError("日次計画の保存形式が不正です。")
        yield "plan", data, "plan_id", 0
        rows = data.get("tasks", [])
        scope, id_key = "task", "task_id"
    elif relative.startswith("study_logs/"):
        rows = data.get("sessions", []) if isinstance(data, dict) else data
        scope, id_key = "session", "session_id"
    else:
        rows = data
        scope, id_key = ("card", "card_id") if relative == "flashcards.json" else ("reminder", "reminder_id")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"関連記録の保存形式が不正です: {relative}")
    ids = [str(row.get(id_key) or (row.get("flashcard_id") if scope == "card" else "") or "") for row in rows]
    meaningful = [value for value in ids if value]
    if len(meaningful) != len(set(meaningful)):
        raise ValueError(f"関連記録のIDが重複しています: {relative}")
    for index, row in enumerate(rows):
        yield scope, row, id_key, index


def _fingerprint(row: dict[str, Any]) -> str:
    def without_links(value):
        if isinstance(value, dict):
            return {key: without_links(item) for key, item in value.items()
                    if key not in {"linked_note_ids", "source_note_id", "review_note_ids", "updated_at"}}
        if isinstance(value, list):
            return [without_links(item) for item in value]
        return value

    stable = without_links(row)
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _descriptor(document: dict[str, Any], scope: str, row: dict[str, Any], id_key: str, index: int) -> dict[str, Any]:
    record_id = str(row.get(id_key) or (row.get("flashcard_id") if scope == "card" else "") or "")
    return {"path": document["path"], "scope": scope, "id_key": id_key, "record_id": record_id,
            "fingerprint": _fingerprint(row), "index": index}


def _trash(root: Path) -> dict[str, Any]:
    document = _document(root, TRASH_PATH, [])
    if not isinstance(document["after"], list) or any(not isinstance(item, dict) or not isinstance(item.get("note"), dict) for item in document["after"]):
        raise ValueError("ノートごみ箱の保存形式が不正です。")
    return document


def list_deleted_notes(root: Path | None = None) -> list[dict[str, Any]]:
    return sorted((deepcopy(item) for item in _trash(_root(root))["after"] if not item.get("restored_at")),
                  key=lambda item: str(item.get("deleted_at", "")), reverse=True)


def reserved_deleted_note_ids(root: Path | None = None) -> set[str]:
    """Never reuse an original or legacy alias ID recorded in the trash."""
    return {str(value) for item in _trash(_root(root))["after"]
            for value in (item.get("note_id"), item["note"].get("note_id"), item["note"].get("id")) if value}


def reserved_note_ids(root: Path | None = None) -> set[str]:
    """Include live raw aliases that read-time normalization does not expose."""
    base = _root(root)
    reserved = reserved_deleted_note_ids(base)
    raw = _document(base, NOTE_PATH)["before"]
    if raw is not None:
        reserved.update(str(value) for _, _, _, page in _pages(raw)
                        for value in (page.get("note_id"), page.get("id")) if value)
    return reserved


def _plan(kind: str, note: dict[str, Any], documents: list[dict[str, Any]], inventories: dict[str, list[str]],
          references: list[dict[str, Any]], warnings: list[str]) -> dict[str, Any]:
    return {"kind": kind, "operation_id": uuid4().hex, "note": deepcopy(note), "references": deepcopy(references),
            "warnings": warnings, "_guards": {d["path"]: d["revision"] for d in documents},
            "_documents": [deepcopy(d) for d in documents if d["before"] != d["after"]], "_inventories": inventories}


def prepare_note_delete(snapshot: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    base = _root(root)
    source, section, page = _source(snapshot, base)
    if len(section["pages"]) <= 1:
        raise ValueError("最後のページは削除できません。先に別のページを追加してください。")
    note = deepcopy(page)
    note_id = str(note.get("note_id") or note.get("id") or snapshot["note"]["note_id"])
    aliases = {str(value) for value in (note.get("note_id"), note.get("id"), note_id) if value}
    for _, _, _, candidate in _pages(source["after"]):
        if candidate is not page and aliases & {str(candidate.get("note_id") or ""), str(candidate.get("id") or "")}:
            raise ValueError("ノートIDが重複しています。削除前に整合性チェックを確認してください。")
    documents, inventories = _reference_documents(base)
    references = []
    for document in documents:
        for scope, row, id_key, index in _records(document):
            changes = {}
            for key in ("linked_note_ids", "review_note_ids"):
                if key not in row:
                    continue
                values = _list(row[key])
                removed = [value for value in values if str(value) in aliases]
                if removed:
                    changes[key] = deepcopy(removed)
                    row[key] = [value for value in values if str(value) not in aliases]
            if str(row.get("source_note_id") or "") in aliases:
                changes["source_note_id"] = row["source_note_id"]
                row["source_note_id"] = ""
            if changes:
                references.append({**_descriptor(document, scope, row, id_key, index), "links": changes})
    outbound_references = {}
    for key, target_scope in (("linked_study_session_ids", "session"), ("linked_flashcard_ids", "card"), ("linked_reminder_ids", "reminder")):
        outbound_references[key] = []
        linked_ids = {str(value) for value in _list(note.get(key))}
        for document in documents:
            for scope, row, id_key, index in _records(document):
                record_id = str(row.get(id_key) or (row.get("flashcard_id") if scope == "card" else "") or "")
                if scope == target_scope and record_id in linked_ids:
                    descriptor = _descriptor(document, scope, row, id_key, index)
                    outbound_references[key].append(descriptor)
    trash = _trash(base)
    location = tuple(snapshot["location"])
    notebook = source["after"]["notebooks"][location[0]]
    trash["after"].append({
        "trash_id": uuid4().hex, "deleted_at": _now(), "note": note, "note_id": note_id,
        "notebook_id": notebook.get("id"), "section_id": section.get("id"), "page_index": location[2],
        "references": deepcopy(references),
        "outbound_references": outbound_references,
    })
    section["pages"].remove(page)
    warnings = [f"関連記録{len(references)}件のリンクだけを外します。勉強ログ・タスク・カード・復習予定の本体は削除しません。"]
    return _plan("note_delete", note, [source, *documents, trash], inventories, references, warnings)


def _locate_reference(documents: list[dict[str, Any]], reference: dict[str, Any]) -> dict[str, Any] | None:
    matches = []
    for document in documents:
        # IDs may have moved or been reused; content identity is required too.
        if not reference["record_id"] and document["path"] != reference["path"]:
            continue
        for scope, row, id_key, index in _records(document):
            if scope != reference["scope"]:
                continue
            record_id = str(row.get(id_key) or (row.get("flashcard_id") if scope == "card" else "") or "")
            if reference["record_id"]:
                matched = record_id == reference["record_id"] and _fingerprint(row) == reference["fingerprint"]
            else:
                matched = index == reference["index"] and _fingerprint(row) == reference["fingerprint"]
            if matched:
                matches.append(row)
    if len(matches) > 1:
        raise ValueError("復元先の関連記録IDが重複しています。整合性チェックを確認してください。")
    return matches[0] if matches else None


def prepare_note_restore(trash_id: str, *, root: Path | None = None) -> dict[str, Any]:
    base = _root(root)
    trash = _trash(base)
    entries = [item for item in trash["after"] if item.get("trash_id") == trash_id and not item.get("restored_at")]
    if len(entries) != 1:
        raise ValueError("ごみ箱の記録は復元済みか変更されています。再読込してください。")
    entry = entries[0]
    source = _document(base, NOTE_PATH)
    note = deepcopy(entry["note"])
    aliases = {str(value) for value in (note.get("note_id"), note.get("id"), entry["note_id"]) if value}
    for _, _, _, candidate in _pages(source["after"]):
        if aliases & {str(candidate.get("note_id") or ""), str(candidate.get("id") or "")}:
            raise ValueError("同じIDのノートが存在するため復元できません。")
    sections = [section for notebook in source["after"]["notebooks"] if notebook.get("id") == entry["notebook_id"]
                for section in notebook["sections"] if section.get("id") == entry["section_id"]]
    if len(sections) != 1:
        raise ValueError("元のノートブック／セクションが存在しません。自動で別の場所へ復元しません。")
    documents, inventories = _reference_documents(base)
    warnings = []
    restored_references = []
    for reference in entry.get("references", []):
        row = _locate_reference(documents, reference)
        if row is None:
            warnings.append(f"削除済み・変更された関連記録へのリンクは戻しません: {reference['scope']} {reference['record_id'] or reference['path']}")
            continue
        for key, value in reference["links"].items():
            if key == "source_note_id":
                if row.get(key) and str(row[key]) not in aliases:
                    warnings.append(f"変更済みの元ノート指定は上書きしません: {reference['record_id']}")
                else:
                    row[key] = value
            else:
                row[key] = list(dict.fromkeys([*_list(row.get(key)), *value]))
        restored_references.append(reference)
    for key, scope in (("linked_study_session_ids", "session"), ("linked_flashcard_ids", "card"), ("linked_reminder_ids", "reminder")):
        if key in note:
            values = _list(note[key])
            safe_ids = {reference["record_id"] for reference in entry.get("outbound_references", {}).get(key, [])
                        if _locate_reference(documents, reference) is not None}
            kept = [value for value in values if str(value) in safe_ids]
            if kept != values:
                warnings.append(f"存在しない・変更された参照先を{key}から除外します。")
            note[key] = kept
    sections[0]["pages"].insert(min(int(entry.get("page_index", 0)), len(sections[0]["pages"])), note)
    entry["restored_at"] = _now()
    plan = _plan("note_restore", note, [source, *documents, trash], inventories, restored_references, warnings)
    plan["selection"] = {"notebook_id": entry["notebook_id"], "section_id": entry["section_id"]}
    return plan


def commit_note_plan(plan: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    from app_core.json_operations import atomic_json_operation

    base = _root(root)
    if plan.get("kind") not in {"note_delete", "note_restore"}:
        raise ValueError("ノート操作の種類が不正です。")
    expected = {_path(base, relative): revision for relative, revision in plan["_guards"].items()}
    with atomic_json_operation(root=base, kind=plan["kind"], operation_id=plan["operation_id"], expected_revisions=expected) as operation:
        if not operation.already_committed:
            for folder, paths in plan["_inventories"].items():
                operation.watch_inventory(base / folder, paths)
            for document in plan["_documents"]:
                save_json_safe(_path(base, document["path"]), document["after"], expected_revision=document["revision"])
    return {"note_id": plan["note"].get("note_id") or plan["note"].get("id"), "warnings": plan["warnings"],
            **plan.get("selection", {})}


def create_note_linked_record(snapshot: dict[str, Any], kind: str, payload: dict[str, Any], *,
                              operation_id: str, root: Path | None = None,
                              schedule_mode: str = "single") -> dict[str, Any]:
    """Create one related entity and its backlink together; retries use one ID."""
    from app_core.json_operations import atomic_json_operation

    if kind not in {"card", "reminder"}:
        raise ValueError("関連記録の種類が不正です。")
    base = _root(root)
    record_id = f"{kind}_{operation_id}"
    with atomic_json_operation(root=base, kind=f"note_create_{kind}", operation_id=operation_id,
                               expected_revisions={base / NOTE_PATH: snapshot["revision"]}) as operation:
        if operation.already_committed:
            # Do not bind old form widgets to an unrelated later revision.
            return {"record_id": record_id, "snapshot": None}
        source, _, note = _source(snapshot, base)
        note_id = str(note.get("note_id") or note.get("id") or snapshot["note"]["note_id"])
        item = {**deepcopy(payload), "source_note_id": note_id, "linked_note_ids": [note_id]}
        if kind == "card":
            from app_core.flashcard_service import add_flashcard

            item.update(card_id=record_id, flashcard_id=record_id)
            add_flashcard(item, base)
            link_key = "linked_flashcard_ids"
        else:
            from app_core.reminder_service import create_reminder

            item["reminder_id"] = record_id
            create_reminder(item, schedule_mode=schedule_mode,
                            preset=schedule_mode if schedule_mode in {"standard", "intensive"} else "standard", root=base)
            link_key = "linked_reminder_ids"
        note[link_key] = list(dict.fromkeys([*_list(note.get(link_key)), record_id]))
        note["updated_at"] = _now()
        save_json_safe(base / NOTE_PATH, source["after"], expected_revision=snapshot["revision"])
        saved = _saved_snapshot(source["after"], note_id)
    return {"record_id": record_id, "snapshot": saved}
