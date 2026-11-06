"""Explicit, previewed edits of saved study sessions.

Reads and previews never write. A commit keeps byte-exact before-images and a
durable journal, holds all affected file locks, and rolls back ordinary write
failures. A process interruption requires an explicit, conflict-checked rollback;
this is not a database transaction or a promise of atomic multi-file reads.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import uuid4

from config import PROJECT_ROOT
from app_core.json_store import (
    JsonConflictError, JsonReadError, JsonStoreError, json_transaction, load_json_safe,
    observed_json_revision, record_json_files, save_json_safe, _reject_constant, _unique_pairs,
)
from app_core.json_operations import operation_guard
from app_core.study_log_service import (
    calculate_session_money, get_study_log_path, reserve_study_session_ids,
    study_id_registry_path, study_trash_path,
)


MASTER_KEYS = ("project_id", "subject_id", "material_id", "unit_id")
EDITABLE_KEYS = {*MASTER_KEYS, "start_at", "end_at", "duration_sec", "hourly_rate", "memo", "condition"}
_DATE_FILE = re.compile(r"^(study_logs|daily_plans)/\d{4}/\d{2}/\d{8}\.json$")
_ACTIVE_JOURNAL_STATES = {"prepared", "needs_recovery"}


def _root(root: Path | None) -> Path:
    return Path(root or PROJECT_ROOT).resolve()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("プロジェクト外の記録は変更できません。") from exc


def _path(root: Path, relative: str, *, writable: bool = False) -> Path:
    candidate = root / relative
    normalized = _relative(candidate, root)
    if normalized != relative or "\\" in relative:
        raise ValueError("記録の保存先が不正です。")
    if writable and not (
        _DATE_FILE.fullmatch(relative)
        or relative in {"notes_data/notebooks.json", "backups/study_log_trash.json"}
    ):
        raise ValueError("この保存先は勉強ログ編集の対象ではありません。")
    return candidate


def _document(path: Path, root: Path, default: Any = None) -> dict[str, Any]:
    data = load_json_safe(path, default)
    revision = observed_json_revision(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = None
    if (hashlib.sha256(raw).hexdigest() if raw is not None else None) != revision:
        raise JsonConflictError("読込中に記録が変更されました。再読込してください。")
    return {
        "path": _relative(path, root), "revision": revision,
        "before": deepcopy(data), "after": deepcopy(data),
        "before_raw": base64.b64encode(raw).decode("ascii") if raw is not None else None,
    }


def _study_rows(document: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    """Retain envelope/unknown keys; materialize legacy IDs only on explicit save."""
    from data_utils import normalize_study_sessions

    data = document["after"]
    rows = data.get("sessions") if isinstance(data, dict) else data
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"勉強ログの形式が不正です。自動変換は行いません: {document['path']}")
    normalized, _ = normalize_study_sessions(_path(root, document["path"]), deepcopy(rows))
    for row, normalized_row in zip(rows, normalized):
        if not row.get("session_id"):
            row["session_id"] = normalized_row["session_id"]
    ids = [row["session_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("同じIDの勉強ログが複数あります。整合性チェックを確認してください。")
    return rows


def _study_documents(root: Path) -> list[dict[str, Any]]:
    return [_document(path, root) for path in record_json_files(root / "study_logs")]


def load_editable_session(session_id: str, root: Path | None = None) -> dict[str, Any]:
    root = _root(root)
    matches = []
    for document in _study_documents(root):
        for row in _study_rows(document, root):
            if row.get("session_id") == session_id:
                matches.append({"session": deepcopy(row), "source_file": document["path"], "revision": document["revision"]})
    if len(matches) != 1:
        raise ValueError("記録が削除・移動されたか、IDが重複しています。再読込して確認してください。")
    return matches[0]


def _snapshot_document(snapshot: dict[str, Any], root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    document = _document(_path(root, snapshot["source_file"], writable=True), root)
    if document["revision"] != snapshot["revision"]:
        raise JsonConflictError("この記録は別の操作で更新されています。編集対象を再読込してください。")
    rows = _study_rows(document, root)
    matches = [row for row in rows if row.get("session_id") == snapshot["session"].get("session_id")]
    if len(matches) != 1 or matches[0] != snapshot["session"]:
        raise JsonConflictError("編集対象の内容が変わっています。再読込してください。")
    return document, rows, matches[0]


def _datetime(value: Any, label: str) -> datetime:
    try:
        # Existing files use local calendar dates, including old offset strings.
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}を有効な日時で入力してください。") from exc


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{label}が未入力または不正です。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}を数値で入力してください。") from exc
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise ValueError(f"{label}は{'0より大きい' if positive else '0以上の'}有限の数値で入力してください。")
    return number


def _validate_session(session: dict[str, Any], warnings: list[str]) -> None:
    start = _datetime(session.get("start_at"), "開始日時")
    end = _datetime(session.get("end_at"), "終了日時")
    if end < start:
        raise ValueError("終了日時は開始日時以降にしてください。")
    duration = _number(session.get("duration_sec"), "実勉強時間", positive=True)
    if duration > (end - start).total_seconds():
        warnings.append("実勉強時間が開始〜終了の経過時間を超えています。カウンターの時間補正など、意図した値か確認してください。")
    if session.get("hourly_rate") not in (None, ""):
        _number(session["hourly_rate"], "時給")
    condition = session.get("condition", {})
    if not isinstance(condition, dict):
        raise ValueError("コンディションは項目ごとに入力してください。")
    for key, lower, upper in (("effort", 1, 10), ("sleep_hours", 0, 24), ("mood", 1, 5)):
        if condition.get(key) in (None, ""):
            continue
        value = _number(condition[key], f"コンディションの{key}")
        if not lower <= value <= upper:
            raise ValueError(f"{key}は{lower}〜{upper}の範囲で入力してください。")


def _masters(root: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    document = _document(root / "master_data" / "master.json", root, {})
    if not isinstance(document["before"], dict):
        raise ValueError("分類マスタの形式が不正です。")
    lookups = {}
    for collection, key in zip(("projects", "subjects", "materials", "units"), MASTER_KEYS):
        rows = document["before"].get(collection, [])
        if not isinstance(rows, list):
            raise ValueError("分類マスタの形式が不正です。")
        lookups[key] = {str(row[key]): row for row in rows if isinstance(row, dict) and row.get(key)}
    return document, lookups


def _validate_classification(session: dict[str, Any], lookups: dict[str, dict[str, Any]]) -> None:
    for position, key in enumerate(MASTER_KEYS):
        item_id = session.get(key) or ""
        if not item_id:
            continue
        item = lookups[key].get(str(item_id))
        if item is None:
            raise ValueError(f"選択された分類が見つかりません: {key}={item_id}。再選択してください。")
        for parent in MASTER_KEYS[:position]:
            if (item.get(parent) or "") != (session.get(parent) or ""):
                raise ValueError("プロジェクト・科目・教材・単元の所属が一致しません。分類を再選択してください。")


def _policy(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value or {}
    enabled = value.get("overtime_enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("残業計算の有効・無効が不正です。")
    threshold = _number(value.get("threshold_hours", 8.0), "残業計算の閾値")
    multiplier = _number(value.get("multiplier", 1.25), "残業倍率")
    if multiplier < 1:
        raise ValueError("残業倍率は1以上にしてください。")
    return {"overtime_enabled": enabled, "threshold_hours": threshold, "multiplier": multiplier}


def _fallback_policy(root: Path, value: dict[str, Any] | None) -> dict[str, Any]:
    if value is not None:
        return _policy(value)
    settings = load_json_safe(root / "settings" / "user_settings.json", {})
    timer = settings.get("timer", {}) if isinstance(settings, dict) else {}
    return _policy({"overtime_enabled": timer.get("overtime_enabled", True),
                    "threshold_hours": timer.get("overtime_threshold_hours", 8.0),
                    "multiplier": timer.get("overtime_multiplier", 1.25)})


def _previous_duration(rows: list[dict[str, Any]], session: dict[str, Any], *, legacy: bool = False) -> float:
    def parsed_start(row):
        try:
            return _datetime(row.get("start_at"), "開始日時")
        except ValueError:
            if legacy:
                return None
            raise

    start = parsed_start(session)
    total = 0.0
    for row in rows:
        if row.get("session_id") == session.get("session_id"):
            continue
        previous_start = parsed_start(row)
        if start is not None and previous_start is not None and previous_start >= start:
            continue
        if legacy:
            # Match the old calculator's handling of malformed/negative values
            # so an erroneous session can itself be corrected or deleted.
            try:
                value = float(row.get("duration_sec") or 0)
            except (TypeError, ValueError):
                value = 0.0
            total += max(value, 0.0) if math.isfinite(value) else 0.0
        else:
            total += _number(row.get("duration_sec"), "先行記録の実勉強時間")
    return total


def _reprice(documents: list[dict[str, Any]], root: Path, fallback: dict[str, Any], warnings: list[str]) -> list[dict[str, Any]]:
    before_by_id: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for document in documents:
        before_document = {**document, "after": deepcopy(document["before"])}
        rows = _study_rows(before_document, root)
        before_by_id.update({row["session_id"]: (row, rows) for row in rows})
    changes = []
    fallback_ids = []
    for document in documents:
        rows = _study_rows(document, root)
        for row in rows:
            old, old_rows = before_by_id.get(row["session_id"], (None, []))
            previous = _previous_duration(rows, row)
            affected = old is None or any(row.get(key) != old.get(key) for key in ("start_at", "duration_sec", "hourly_rate"))
            if not affected:
                affected = _previous_duration(old_rows, old, legacy=True) != previous
            if not affected:
                continue
            try:
                rate = _number(row.get("hourly_rate"), "時給")
            except ValueError as exc:
                raise ValueError(f"金額を再計算する記録 {row['session_id']} の時給が未記録・不正です。この記録の時給を先に設定してください。") from exc
            saved_policy = row.get("money_policy")
            if saved_policy is not None and not isinstance(saved_policy, dict):
                raise ValueError(f"{row['session_id']} の保存済み金額計算設定が不正です。")
            policy = _policy(saved_policy) if saved_policy else fallback
            if not saved_policy:
                fallback_ids.append(row["session_id"])
            money = calculate_session_money(row["duration_sec"], rate, previous_duration_sec=previous, **policy)
            row.update(money)
            row["money_policy"] = deepcopy(policy)
            row["updated_at"] = _now()
            changes.append({"session_id": row["session_id"], "start_at": row.get("start_at", ""),
                            "before_money": old.get("money") if old else None, "after_money": row["money"]})
    if fallback_ids:
        warnings.append(f"残業設定が未保存の旧記録{len(fallback_ids)}件は、確認時の現在設定（残業{'あり' if fallback['overtime_enabled'] else 'なし'}・{fallback['threshold_hours']:g}時間・{fallback['multiplier']:g}倍）で再計算し、その設定を保存します。")
    return changes


def _new_plan(kind: str, before: dict[str, Any] | None, after: dict[str, Any] | None,
              documents: list[dict[str, Any]], warnings: list[str], money_changes: list[dict[str, Any]],
              guards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    changed = [document for document in documents if document["before"] != document["after"]]
    guard_map = {document["path"]: document["revision"] for document in [*documents, *(guards or [])]}
    return {"kind": kind, "session_id": (after or before or {})["session_id"],
            "before": deepcopy(before), "after": deepcopy(after), "warnings": warnings,
            "money_changes": money_changes, "changed_files": [item["path"] for item in changed],
            "_documents": deepcopy(changed), "_guards": guard_map}


def prepare_session_edit(snapshot: dict[str, Any], updates: dict[str, Any], *, root: Path | None = None,
                         fallback_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    root = _root(root)
    if set(updates) - EDITABLE_KEYS:
        raise ValueError("ID・作成日時・関連付けなどの保護項目はここでは変更できません。")
    source, rows, row = _snapshot_document(snapshot, root)
    before = deepcopy(row)
    updates = deepcopy(updates)
    if isinstance(updates.get("condition"), dict):
        original_condition = row.get("condition", {})
        if isinstance(original_condition, dict):
            condition = deepcopy(original_condition)
            for key, value in updates["condition"].items():
                if value in (None, ""):
                    condition.pop(key, None)
                else:
                    condition[key] = value
            updates["condition"] = condition
    row.update(updates)
    warnings: list[str] = []
    _validate_session(row, warnings)
    guards = []
    if any(row.get(key, "") != before.get(key, "") for key in MASTER_KEYS):
        master, lookups = _masters(root)
        _validate_classification(row, lookups)
        guards.append(master)
    if row == before:
        return _new_plan("edit", before, row, [], warnings, [])
    row["updated_at"] = _now()
    documents = [source]
    target_path = get_study_log_path(row["start_at"], root)
    if _relative(target_path, root) != source["path"]:
        target = _document(target_path, root, [])
        target_rows = _study_rows(target, root)
        if any(item.get("session_id") == row["session_id"] for item in target_rows):
            raise ValueError("移動先に同じIDの勉強ログがあります。")
        rows.remove(row)
        target_rows.append(row)
        documents.append(target)
    money_changes = _reprice(documents, root, _fallback_policy(root, fallback_policy), warnings)
    return _new_plan("edit", before, row, documents, warnings, money_changes, guards)


def _note_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    store = document["after"]
    if store is None:
        return []
    if not isinstance(store, dict) or not isinstance(store.get("notebooks", []), list):
        raise ValueError("ノートの保存形式が不正です。関連付けを安全に更新できません。")
    pages = []
    for notebook in store.get("notebooks", []):
        for section in notebook.get("sections", []):
            pages.extend(page for page in section.get("pages", []) if isinstance(page, dict))
    return pages


def _link_ids(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    return [str(item) for item in (value if isinstance(value, list) else [value]) if item]


def _link_documents(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    notes = _document(root / "notes_data" / "notebooks.json", root)
    plans = [_document(path, root) for path in record_json_files(root / "daily_plans")]
    for document in plans:
        plan = document["after"]
        if not isinstance(plan, dict) or not isinstance(plan.get("tasks", []), list):
            raise ValueError("日次計画の形式が不正です。関連付けを安全に更新できません。")
    return notes, plans


def _trash_document(root: Path) -> dict[str, Any]:
    document = _document(study_trash_path(root), root, [])
    if not isinstance(document["after"], list) or any(not isinstance(item, dict) or not isinstance(item.get("session"), dict) for item in document["after"]):
        raise ValueError("勉強ログのごみ箱の形式が不正です。")
    return document


def list_deleted_sessions(root: Path | None = None) -> list[dict[str, Any]]:
    document = _trash_document(_root(root))
    return sorted((deepcopy(item) for item in document["after"] if not item.get("restored_at")),
                  key=lambda item: (str(item.get("deleted_at", "")), str(item.get("trash_id", ""))), reverse=True)


def prepare_session_delete(snapshot: dict[str, Any], *, root: Path | None = None,
                           fallback_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    root = _root(root)
    source, rows, row = _snapshot_document(snapshot, root)
    before = deepcopy(row)
    rows.remove(row)
    warnings: list[str] = []
    money_changes = _reprice([source], root, _fallback_policy(root, fallback_policy), warnings)
    notes, plans = _link_documents(root)
    note_backlinks = []
    task_backlinks = []
    for note in _note_rows(notes):
        linked = _link_ids(note.get("linked_study_session_ids"))
        if row["session_id"] in linked:
            note_backlinks.append(str(note.get("note_id") or note.get("id") or ""))
            note["linked_study_session_ids"] = [value for value in linked if value != row["session_id"]]
            note["updated_at"] = _now()
    for document in plans:
        for task in document["after"].get("tasks", []):
            if not isinstance(task, dict):
                continue
            linked = _link_ids(task.get("linked_session_ids"))
            if row["session_id"] in linked:
                task_backlinks.append({"source_file": document["path"], "task_id": task.get("task_id", "")})
                task["linked_session_ids"] = [value for value in linked if value != row["session_id"]]
                task["updated_at"] = document["after"]["updated_at"] = _now()
    trash = _trash_document(root)
    trash["after"].append({"trash_id": uuid4().hex, "deleted_at": _now(), "session": before,
                           "source_file": source["path"], "note_backlinks": note_backlinks,
                           "task_backlinks": task_backlinks})
    warnings.append(f"ノート{len(note_backlinks)}件・タスク{len(task_backlinks)}件の関連付けを外します。ノート本文とタスク自体は削除しません。")
    plan = _new_plan("delete", before, None, [source, notes, *plans, trash], warnings, money_changes)
    plan["_inventories"] = {"daily_plans": sorted(document["path"] for document in plans)}
    return plan


def _task_document_day(document: dict[str, Any]) -> str | None:
    """Return only an exact, internally consistent writable plan owner day."""
    path = document["path"]
    if not isinstance(path, str) or not path.startswith("daily_plans/") or not _DATE_FILE.fullmatch(path):
        return None
    try:
        owner = datetime.strptime(Path(path).stem, "%Y%m%d").date()
    except ValueError:
        return None
    if path != f"daily_plans/{owner:%Y}/{owner:%m}/{owner:%Y%m%d}.json":
        return None
    return owner.isoformat() if document["after"].get("date") == owner.isoformat() else None


def _restore_task_link(row: dict[str, Any], entry: dict[str, Any], plans: list[dict[str, Any]], warnings: list[str]) -> None:
    task_rows = [(document, task, _task_document_day(document))
                 for document in plans for task in document["after"].get("tasks", [])
                 if isinstance(task, dict) and task.get("task_id")]
    original = str(row.get("linked_task_id") or "")
    backlinks = [item for item in entry.get("task_backlinks", []) if isinstance(item, dict) and item.get("task_id")]
    id_counts: dict[str, int] = {}
    for _, task, _ in task_rows:
        identifier = str(task["task_id"])
        id_counts[identifier] = id_counts.get(identifier, 0) + 1

    def backed(candidate: tuple) -> bool:
        document, task, _ = candidate
        return any(str(item["task_id"]) == str(task["task_id"])
                   and (item.get("source_file") == document["path"]
                        or not item.get("source_file") and id_counts[str(task["task_id"])] == 1)
                   for item in backlinks)

    matches = [candidate for candidate in task_rows if str(candidate[1]["task_id"]) == original] if original else []
    chosen = None
    if len(matches) == 1 and matches[0][2] is not None:
        chosen = matches[0]
    elif matches:
        valid = [candidate for candidate in matches if candidate[2] is not None]
        by_day = [candidate for candidate in valid if candidate[2] == row.get("linked_task_date")]
        by_source = [candidate for candidate in valid if backed(candidate)]
        # A conflicting day and backlink is not a basis for guessing ownership.
        candidates = ([candidate for candidate in by_day if backed(candidate)]
                      if by_day and by_source else by_day or by_source)
        if len(candidates) == 1:
            chosen = candidates[0]
    else:
        candidates = [candidate for candidate in task_rows if candidate[2] is not None and backed(candidate)]
        if len(candidates) == 1:
            chosen = candidates[0]
    if original and not matches:
        warnings.append("元の関連タスクが見つからないため、そのタスクへのリンクは復元しません。")
    if chosen is None and (original or backlinks):
        warnings.append("関連タスクの所属日を一意に確認できません（日付不一致・重複・削除の可能性）。タスクへのリンク復元を省略します。")
    if len({str(item["task_id"]) for item in backlinks}) > 1:
        warnings.append("元のタスク側に複数の関連付けがありました。特定できた1件だけを復元します。")
    chosen_id = str(chosen[1]["task_id"]) if chosen else ""
    row["linked_task_id"] = chosen_id
    if "linked_task_date" in row or chosen_id and (chosen_id != original or len(matches) != 1):
        row["linked_task_date"] = chosen[2] if chosen else ""
    if chosen:
        document, task, _ = chosen
        task["linked_session_ids"] = list(dict.fromkeys([*_link_ids(task.get("linked_session_ids")), row["session_id"]]))
        task["updated_at"] = document["after"]["updated_at"] = _now()


def _restore_links(row: dict[str, Any], entry: dict[str, Any], notes: dict[str, Any], plans: list[dict[str, Any]], warnings: list[str]) -> None:
    wanted_notes = set(_link_ids(row.get("linked_note_ids"))) | set(_link_ids(entry.get("note_backlinks")))
    pages = _note_rows(notes)
    existing_notes = {str(page.get("note_id") or page.get("id") or "") for page in pages}
    missing = wanted_notes - existing_notes
    if missing:
        warnings.append(f"削除済み・不明なノート{len(missing)}件へのリンクは復元しません。")
    row["linked_note_ids"] = sorted((wanted_notes & existing_notes) - {""})
    for note in pages:
        if str(note.get("note_id") or note.get("id") or "") in row["linked_note_ids"]:
            note["linked_study_session_ids"] = list(dict.fromkeys([*_link_ids(note.get("linked_study_session_ids")), row["session_id"]]))
            note["updated_at"] = _now()
    _restore_task_link(row, entry, plans, warnings)


def prepare_session_restore(trash_id: str, *, root: Path | None = None,
                            fallback_policy: dict[str, Any] | None = None,
                            hourly_rate: float | None = None) -> dict[str, Any]:
    root = _root(root)
    trash = _trash_document(root)
    entries = [item for item in trash["after"] if item.get("trash_id") == trash_id and not item.get("restored_at")]
    if len(entries) != 1:
        raise ValueError("記録は復元済みか、ごみ箱の内容が変更されています。再読込してください。")
    entry = entries[0]
    row = deepcopy(entry["session"])
    if hourly_rate is not None:
        row["hourly_rate"] = _number(hourly_rate, "復元する記録の時給")
    study_documents = _study_documents(root)
    for document in study_documents:
        if any(item.get("session_id") == row["session_id"] for item in _study_rows(document, root)):
            raise ValueError("同じIDの勉強ログがすでに存在するため、重複して復元できません。")
    warnings: list[str] = []
    _validate_session(row, warnings)
    master, lookups = _masters(root)
    for position, key in enumerate(MASTER_KEYS):
        item_id = row.get(key) or ""
        if not item_id:
            continue
        item = lookups[key].get(str(item_id))
        if item is None or any((item.get(parent) or "") != (row.get(parent) or "") for parent in MASTER_KEYS[:position]):
            warnings.append(f"現在のマスタで無効な分類 {key} は未設定に戻します。必要に応じて復元後に再選択してください。")
            for descendant in MASTER_KEYS[position:]:
                row[descendant] = ""
    notes, plans = _link_documents(root)
    _restore_links(row, entry, notes, plans, warnings)
    target = _document(get_study_log_path(row["start_at"], root), root, [])
    _study_rows(target, root).append(row)
    money_changes = _reprice([target], root, _fallback_policy(root, fallback_policy), warnings)
    row["updated_at"] = _now()
    entry["restored_at"] = _now()
    plan = _new_plan("restore", None, row, [target, notes, *plans, trash], warnings, money_changes, [master, *study_documents])
    plan["_inventories"] = {
        "daily_plans": sorted(document["path"] for document in plans),
        "study_logs": sorted({document["path"] for document in study_documents}),
    }
    return plan


def _journal_directory(root: Path) -> Path:
    return root / "backups" / "study_log_edits"


def pending_session_changes(root: Path | None = None) -> list[dict[str, Any]]:
    root = _root(root)
    pending = []
    for path in _journal_directory(root).glob("*/transaction.json"):
        entry = load_json_safe(path, {})
        if (not isinstance(entry, dict) or entry.get("operation_id") != path.parent.name
                or not re.fullmatch(r"[0-9a-f]{32}", path.parent.name)
                or not isinstance(entry.get("status"), str)
                or entry.get("status") not in _ACTIVE_JOURNAL_STATES | {"committed", "rolled_back"}):
            raise JsonReadError("勉強ログの復旧情報が不正です。操作バックアップを確認してください。")
        if entry.get("status") in _ACTIVE_JOURNAL_STATES:
            pending.append({key: entry.get(key) for key in ("operation_id", "kind", "created_at", "session_id", "status")})
    return sorted(pending, key=lambda item: str(item.get("created_at", "")))


def _lock_sort(relative: str) -> tuple[int, str]:
    # Match the timer's study -> notes -> plans order, never alphabetical-only.
    group = 0 if relative.startswith("study_logs/") else 1 if relative.startswith("notes_data/") else 2 if relative.startswith("daily_plans/") else 3
    return group, relative


def _check_inventories(plan: dict[str, Any], root: Path, *, after_write: bool = False) -> None:
    for folder, files in plan.get("_inventories", {}).items():
        if folder not in {"study_logs", "daily_plans"}:
            raise ValueError("確認対象の記録フォルダが不正です。")
        expected = set(files)
        if after_write:
            expected.update(document["path"] for document in plan["_documents"] if document["path"].startswith(folder + "/"))
        actual = {_relative(path, root) for path in record_json_files(root / folder)}
        if expected != actual:
            raise JsonConflictError("確認後に記録ファイルが追加・削除されました。再読込して変更内容を確認してください。")


def _validated_recovery_documents(documents: Any, root: Path) -> list[dict[str, Any]]:
    """Validate every before-image before touching any recovery target.

    Existing journals have no after hash, so derive it from their serialized
    payload. Missing raw images cannot establish a safe byte-exact rollback and
    are rejected; keep that journal pending for manual backup inspection.
    """
    if not isinstance(documents, list) or not documents:
        raise JsonReadError("勉強ログの復旧対象一覧が不正です。自動復旧しません。")
    seen = set()
    for document in documents:
        try:
            if not isinstance(document, dict) or not isinstance(document.get("path"), str):
                raise ValueError("invalid recovery document")
            key = os.path.normcase(str(_path(root, document["path"], writable=True)))
            if key in seen:
                raise ValueError("duplicate recovery target")
            seen.add(key)
            raw = document["before_raw"]
            if raw is not None and not isinstance(raw, str):
                raise ValueError("invalid before image")
            decoded = None if raw is None else base64.b64decode(raw, validate=True)
            revision = hashlib.sha256(decoded).hexdigest() if decoded is not None else None
            if revision != document["revision"]:
                raise ValueError("before image revision mismatch")
            before = json.dumps(document["before"], sort_keys=True, allow_nan=False)
            json.dumps(document["after"], ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
            if decoded is not None:
                parsed = json.loads(decoded.decode("utf-8-sig"), object_pairs_hook=_unique_pairs,
                                    parse_constant=_reject_constant)
                if json.dumps(parsed, sort_keys=True, allow_nan=False) != before:
                    raise ValueError("before image content mismatch")
        except (KeyError, ValueError, TypeError, UnicodeError, OverflowError) as exc:
            raise JsonReadError("勉強ログの復旧情報の内容またはハッシュが不正です。自動復旧せず、未完了のまま保持します。操作バックアップを確認してください。") from exc
    return documents


def _restore_before(document: dict[str, Any], root: Path) -> None:
    """Byte-exact rollback while the caller owns the document lock."""
    path = _path(root, document["path"], writable=True)
    raw = document["before_raw"]
    if raw is None:
        path.unlink(missing_ok=True)
    else:
        restored = base64.b64decode(raw, validate=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.undo-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(restored)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    load_json_safe(path, None)  # Refresh the JSON store's observed revision.


def _rollback_documents(documents: list[dict[str, Any]], root: Path) -> None:
    # Check the entire set before reverting anything. Never erase a later edit.
    documents = _validated_recovery_documents(documents, root)
    current_revisions = []
    for document in documents:
        path = _path(root, document["path"], writable=True)
        load_json_safe(path, None)
        current_revision = observed_json_revision(path)
        after_raw = json.dumps(document["after"], ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if current_revision not in {document["revision"], hashlib.sha256(after_raw).hexdigest()}:
            raise JsonConflictError("未完了の更新後に別の変更があります。自動取消は行いません。操作バックアップを確認してください。")
        current_revisions.append(current_revision)
    for document, revision in reversed(list(zip(documents, current_revisions))):
        if revision != document["revision"]:
            _restore_before(document, root)


def commit_session_change(plan: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    root = _root(root)
    documents = deepcopy(plan["_documents"])
    if not documents:
        return {"backup_path": "", "changed_files": [], "kind": plan["kind"]}
    guards = dict(plan["_guards"])
    for document in documents:
        _path(root, document["path"], writable=True)
        json.dumps(document["after"], allow_nan=False)
    with operation_guard(root), json_transaction(_journal_directory(root) / "operations"):
        if pending_session_changes(root):
            raise JsonStoreError("未完了の勉強ログ更新があります。先に画面の復旧操作で取り消してください。")
        with ExitStack() as locks:
            for relative in sorted(guards, key=_lock_sort):
                locks.enter_context(json_transaction(_path(root, relative)))
            locks.enter_context(json_transaction(study_id_registry_path(root)))
            _check_inventories(plan, root)
            for relative, revision in guards.items():
                path = _path(root, relative)
                load_json_safe(path, None)
                if observed_json_revision(path) != revision:
                    raise JsonConflictError("確認後に保存データが変わりました。再読込して変更内容を確認してください。")
            # Append-only reservation is intentionally not undone with the edit.
            # It closes the ID gap during cross-date writes and crash recovery.
            reserve_study_session_ids({plan["session_id"]}, root)
            operation_id = uuid4().hex
            journal_path = _journal_directory(root) / operation_id / "transaction.json"
            journal = {"operation_id": operation_id, "status": "prepared", "kind": plan["kind"],
                       "session_id": plan["session_id"], "created_at": _now(), "documents": documents}
            save_json_safe(journal_path, journal, expected_revision=None)
            try:
                for document in documents:
                    save_json_safe(_path(root, document["path"], writable=True), document["after"], expected_revision=document["revision"])
                _check_inventories(plan, root, after_write=True)
                journal["status"] = "committed"
                journal["committed_at"] = _now()
                save_json_safe(journal_path, journal)
            except Exception as exc:
                try:
                    _rollback_documents(documents, root)
                    journal["status"] = "rolled_back"
                    journal["rolled_back_at"] = _now()
                    save_json_safe(journal_path, journal)
                except Exception:
                    # The durable 'prepared' journal is sufficient even when a
                    # disk error prevents recording a more specific state.
                    raise JsonStoreError(f"保存を完了できませんでした。未完了の更新を画面から取り消してください。操作バックアップ: {journal_path.parent}") from exc
                raise JsonStoreError("保存に失敗したため、変更前の状態へ戻しました。入力を再確認してやり直してください。") from exc
    return {"backup_path": str(journal_path.parent), "changed_files": plan["changed_files"], "kind": plan["kind"]}


def rollback_pending_session_change(operation_id: str, root: Path | None = None) -> None:
    root = _root(root)
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("復旧対象の操作IDが不正です。")
    with operation_guard(root, allow_pending=True), json_transaction(_journal_directory(root) / "operations"):
        path = _journal_directory(root) / operation_id / "transaction.json"
        journal = load_json_safe(path, {})
        if (not isinstance(journal, dict) or journal.get("operation_id") != operation_id
                or not isinstance(journal.get("status"), str)):
            raise JsonReadError("勉強ログの復旧情報が不正です。操作バックアップを確認してください。")
        if journal.get("status") not in _ACTIVE_JOURNAL_STATES:
            raise ValueError("この操作は未完了ではありません。再読込してください。")
        documents = _validated_recovery_documents(journal.get("documents"), root)
        with ExitStack() as locks:
            for relative in sorted({document["path"] for document in documents}, key=_lock_sort):
                locks.enter_context(json_transaction(_path(root, relative, writable=True)))
            _rollback_documents(documents, root)
            journal["status"] = "rolled_back"
            journal["rolled_back_at"] = _now()
            save_json_safe(path, journal)
