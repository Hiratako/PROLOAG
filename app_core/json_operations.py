"""Staged, journaled JSON operations with explicit crash recovery.

All normal writes are staged before any live record changes. Commit holds the
document locks, checks original revisions, and keeps byte-exact before-images.
Ordinary failures roll back; process interruption leaves a durable journal for
explicit recovery. This does NOT promise atomic multi-file reads to readers.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable
from uuid import uuid4

from config import PROJECT_ROOT
from app_core import json_store as store


ACTIVE_STATES = {"prepared", "needs_recovery"}
_ID = re.compile(r"[0-9a-f]{32}")
_FOLDERS = {"study_logs", "daily_plans", "notes_data", "master_data",
            "diary_entries", "health_logs", "exercise_logs", "statistics_practice", "settings"}
_FILES = {"flashcards.json", "reminders.json", "backups/note_trash.json",
          "backups/study_log_trash.json", "backups/study_session_ids.json"}


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _root(root: Path | None) -> Path:
    return store.resolved_json_path(root or PROJECT_ROOT)


def _relative(root: Path, path: Path | str) -> str:
    path = Path(path)
    path = path if path.is_absolute() else root / path
    try:
        return store.resolved_json_path(path).relative_to(store.resolved_json_path(root)).as_posix()
    except ValueError as exc:
        raise store.JsonStoreError("プロジェクト外の記録は操作できません。") from exc


def _record_path(root: Path, relative: str) -> Path:
    if _relative(root, relative) != relative or "\\" in relative:
        raise store.JsonStoreError("記録の保存先が不正です。")
    parts = Path(relative).parts
    if relative not in _FILES and not (
        len(parts) >= 2 and parts[0] in _FOLDERS and Path(relative).suffix == ".json"
        and not any(part.lower() in {"backups", "backup"} for part in parts[1:-1])
    ):
        raise store.JsonStoreError("この保存先は記録更新の対象外です。")
    return root / relative


def _journal_path(root: Path, operation_id: str) -> Path:
    if not _ID.fullmatch(operation_id):
        raise store.JsonStoreError("操作IDが不正です。")
    relative = f"backups/json_operations/{operation_id}/transaction.json"
    if _relative(root, relative) != relative:
        raise store.JsonStoreError("操作バックアップの保存先が不正です。")
    return root / relative


def _lock_sort(relative: str) -> tuple[int, str]:
    # Match existing study editor/timer lock ordering.
    group = 0 if relative.startswith("study_logs/") else 1 if relative.startswith("notes_data/") else 2 if relative.startswith("daily_plans/") else 3
    return group, relative


def pending_json_operations(root: Path | None = None) -> list[dict[str, Any]]:
    root = _root(root)
    result = []
    for path in sorted((root / "backups/json_operations").glob("*/transaction.json")):
        expected = _journal_path(root, path.parent.name)
        entry = store.load_json_safe(expected, {})
        if (not isinstance(entry, dict) or entry.get("operation_id") != path.parent.name
                or entry.get("schema_version") != 1
                or not isinstance(entry.get("status"), str)
                or entry.get("status") not in ACTIVE_STATES | {"committed", "rolled_back"}):
            raise store.JsonReadError("保存操作の復旧情報が不正です。バックアップを確認してください。")
        if entry.get("status") in ACTIVE_STATES:
            result.append({key: entry.get(key) for key in ("operation_id", "kind", "created_at", "status")})
    return result


def assert_no_pending_operations(root: Path | None = None) -> None:
    root = _root(root)
    if pending_json_operations(root):
        raise store.JsonStoreError("未完了の保存操作があります。サイドバーの保存操作の復旧を先に実行してください。")
    # Old study-edit journals remain compatible and use their existing recovery UI.
    for path in (root / "backups/study_log_edits").glob("*/transaction.json"):
        _relative(root, path)
        entry = store.load_json_safe(path, {})
        if (not isinstance(entry, dict) or not isinstance(entry.get("status"), str)
                or entry.get("status") not in ACTIVE_STATES | {"committed", "rolled_back"}):
            raise store.JsonReadError("勉強ログの復旧情報が不正です。")
        if entry.get("status") in ACTIVE_STATES:
            raise store.JsonStoreError("未完了の勉強ログ更新があります。勉強ログ画面で先に復旧してください。")


@contextmanager
def operation_guard(root: Path | None = None, *, allow_pending: bool = False):
    store.require_writable()
    root = _root(root)
    relative = "backups/json_operations/operations"
    if _relative(root, relative) != relative:
        raise store.JsonStoreError("操作ロックの保存先が不正です。")
    with store.json_transaction(root / relative):
        if not allow_pending:
            assert_no_pending_operations(root)
        yield


class JsonOperation:
    def __init__(self, root: Path, kind: str, operation_id: str,
                 expected_revisions: dict[Path, Any] | None = None,
                 request_fingerprint: str | None = None):
        self.root = root
        self.kind = kind
        self.operation_id = operation_id
        self._request_fingerprint = request_fingerprint
        self.already_committed = False
        self.changed_files: list[str] = []
        self._reads: dict[str, tuple[Any, bytes | None, str | None]] = {}
        self._writes: dict[str, Any] = {}
        self._expected = {_relative(root, path): revision for path, revision in (expected_revisions or {}).items()}
        self._inventories: dict[str, set[str]] = {}

    def _document(self, path: Path) -> tuple[str, tuple[Any, bytes | None, str | None]]:
        relative = _relative(self.root, path)
        if relative not in self._reads:
            value = store._read(self.root / relative)
            if relative in self._expected and self._expected[relative] != value[2]:
                raise store.JsonConflictError("編集を開始した後に別の更新が保存されました。入力は保持しています。内容を確認して再読込してください。")
            self._reads[relative] = value
        return relative, self._reads[relative]

    def read(self, path: Path, default: Any) -> Any:
        relative, (data, raw, revision) = self._document(path)
        store._observe(store._key(self.root / relative), revision)
        if relative in self._writes:
            return deepcopy(self._writes[relative])
        return deepcopy(default if raw is None else data)

    def stage(self, path: Path, data: Any, *, expected_revision: Any = store._UNSET) -> None:
        store.require_writable()
        if self.already_committed:
            raise store.JsonStoreError("この保存操作は完了済みです。再送による変更は行いません。")
        _record_path(self.root, _relative(self.root, path))
        relative, (_, _, revision) = self._document(path)
        if expected_revision is store._UNSET:
            expected_revision = store.observed_json_revision(path)
        if expected_revision is not store._UNSET and expected_revision != revision:
            raise store.JsonConflictError("編集開始時の記録と保存済み記録が異なります。再読込してください。")
        json.dumps(data, allow_nan=False)
        self._writes[relative] = deepcopy(data)

    def watch_inventory(self, folder: Path, expected_paths: Iterable[Path | str] | None = None) -> None:
        relative = _relative(self.root, folder)
        if relative not in _FOLDERS:
            raise store.JsonStoreError("一覧確認の対象フォルダが不正です。")
        paths = store.record_json_files(self.root / relative) if expected_paths is None else expected_paths
        expected = {_relative(self.root, path) for path in paths}
        if any(not path.startswith(relative + "/") for path in expected):
            raise store.JsonStoreError("一覧確認の対象ファイルが不正です。")
        self._inventories[relative] = expected

    def _check_inventories(self, *, after: bool = False) -> None:
        for folder, original in self._inventories.items():
            expected = set(original)
            if after:
                expected.update(path for path in self._writes if path.startswith(folder + "/"))
            actual = {_relative(self.root, path) for path in store.record_json_files(self.root / folder)}
            if actual != expected:
                raise store.JsonConflictError("確認後に関連記録が追加・削除されました。再読込してください。")

    def commit(self) -> None:
        store.require_writable()
        from app_core.note_reference_guard import validate_note_references

        # Validate against the complete staged final state, before acquiring the
        # final read-set locks. Validation reads become revision guards too.
        token = store._active_operation.set(self)
        try:
            for relative, after in self._writes.items():
                validate_note_references(self.root, self.root / relative, after)
        finally:
            store._active_operation.reset(token)
        for relative in self._expected:
            self._document(self.root / relative)
        documents = []
        for relative, after in self._writes.items():
            before, raw, revision = self._reads[relative]
            if raw is not None and json.dumps(before, sort_keys=True, allow_nan=False) == json.dumps(after, sort_keys=True, allow_nan=False):
                continue
            encoded = json.dumps(after, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
            documents.append({"path": relative, "revision": revision,
                              "before_raw": None if raw is None else base64.b64encode(raw).decode("ascii"),
                              "after": after, "after_revision": hashlib.sha256(encoded).hexdigest()})
        self.changed_files = [doc["path"] for doc in documents]
        journal_path = _journal_path(self.root, self.operation_id)
        with ExitStack() as locks:
            for relative in sorted(self._reads, key=_lock_sort):
                locks.enter_context(store.json_transaction(self.root / relative))
            self._check_inventories()
            for relative, (_, _, revision) in self._reads.items():
                if store._read(self.root / relative)[2] != revision:
                    raise store.JsonConflictError("保存の準備中に記録が変更されました。入力を確認して再読込してください。")
            journal = {"schema_version": 1, "operation_id": self.operation_id,
                       "kind": self.kind, "created_at": _now(), "status": "prepared", "documents": documents}
            if self._request_fingerprint is not None:
                journal["request_fingerprint"] = self._request_fingerprint
            store.save_json_safe(journal_path, journal, expected_revision=store.observed_json_revision(journal_path))
            commit_token = store._committing_operation.set(self)
            try:
                for document in documents:
                    store.save_json_safe(_record_path(self.root, document["path"]), document["after"], expected_revision=document["revision"])
                self._check_inventories(after=True)
                journal.update(status="committed", committed_at=_now())
                store.save_json_safe(journal_path, journal)
            except Exception as exc:
                try:
                    _rollback(self.root, documents)
                    journal.update(status="rolled_back", rolled_back_at=_now())
                    store.save_json_safe(journal_path, journal)
                except Exception:
                    raise store.JsonStoreError("保存が中断されました。入力は保持しています。サイドバーの保存操作の復旧を確認してください。") from exc
                raise store.JsonStoreError("保存できなかったため変更前へ戻しました。入力は保持しています。内容を確認して再試行してください。") from exc
            finally:
                store._committing_operation.reset(commit_token)


def _validated_documents(root: Path, documents: Any) -> list[dict[str, Any]]:
    if not isinstance(documents, list):
        raise store.JsonReadError("操作バックアップの記録一覧が不正です。")
    seen = set()
    for doc in documents:
        if not isinstance(doc, dict) or not isinstance(doc.get("path"), str):
            raise store.JsonReadError("操作バックアップの記録が不正です。")
        _record_path(root, doc["path"])
        if doc["path"] in seen:
            raise store.JsonReadError("操作バックアップの保存先が重複しています。")
        seen.add(doc["path"])
        try:
            raw = None if doc["before_raw"] is None else base64.b64decode(doc["before_raw"], validate=True)
            revision = hashlib.sha256(raw).hexdigest() if raw is not None else None
            encoded = json.dumps(doc["after"], ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
            if revision != doc["revision"] or hashlib.sha256(encoded).hexdigest() != doc["after_revision"]:
                raise ValueError("revision mismatch")
            if raw is not None:
                parsed = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=store._unique_pairs, parse_constant=store._reject_constant)
                json.dumps(parsed, allow_nan=False)
        except (KeyError, ValueError, TypeError, UnicodeError) as exc:
            raise store.JsonReadError("操作バックアップの内容またはハッシュが不正です。自動復旧しません。") from exc
    return documents


def _rollback(root: Path, documents: list[dict[str, Any]]) -> None:
    store.require_writable()
    documents = _validated_documents(root, documents)
    # Verify every target before restoring anything; never overwrite a later edit.
    for doc in documents:
        if store._read(_record_path(root, doc["path"]))[2] not in {doc["revision"], doc["after_revision"]}:
            raise store.JsonConflictError("中断後に別の変更があります。自動取消は行いません。操作バックアップを確認してください。")
    for doc in reversed(documents):
        path = _record_path(root, doc["path"])
        if store._read(path)[2] == doc["revision"]:
            continue
        raw = doc["before_raw"]
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.recover-", suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(base64.b64decode(raw, validate=True))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                temporary = None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        store.load_json_safe(path, None)


@contextmanager
def atomic_json_operation(*, root: Path | None = None, kind: str,
                          operation_id: str | None = None,
                          expected_revisions: dict[Path, Any] | None = None,
                          request_fingerprint: str | None = None):
    """Optionally bind a committed retry to the exact caller's request.

    A rolled-back operation may be corrected and retried. A committed request
    cannot be relabeled as a different update or deletion after an ACK is lost.
    """
    store.require_writable()
    if store._active_operation.get() is not None:
        raise store.JsonStoreError("保存操作は入れ子にできません。最外側でまとめてください。")
    root = _root(root)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind):
        raise store.JsonStoreError("保存操作の種類が不正です。")
    if request_fingerprint is not None and (not isinstance(request_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", request_fingerprint)):
        raise store.JsonStoreError("保存操作の内容識別子が不正です。")
    operation_id = operation_id or uuid4().hex
    path = _journal_path(root, operation_id)
    with operation_guard(root):
        previous = store.load_json_safe(path, None)
        if previous is not None and (not isinstance(previous, dict) or previous.get("kind") != kind or previous.get("operation_id") != operation_id):
            raise store.JsonConflictError("操作IDが別の保存に使われています。")
        if previous is not None and (not isinstance(previous.get("status"), str)
                                     or previous.get("status") not in {"committed", "rolled_back"}):
            raise store.JsonReadError("保存操作の状態が不正です。復旧情報を確認してください。")
        if (previous and previous.get("status") == "committed"
                and previous.get("request_fingerprint") != request_fingerprint):
            raise store.JsonConflictError("先ほどの操作は保存済みですが、今回の入力または操作が異なります。入力は保持しています。内容を確認して最新データを再読込してください。")
        operation = JsonOperation(root, kind, operation_id, expected_revisions, request_fingerprint)
        operation.already_committed = bool(previous and previous.get("status") == "committed")
        token = store._active_operation.set(operation)
        try:
            yield operation
        finally:
            store._active_operation.reset(token)
        if not operation.already_committed:
            operation.commit()


def rollback_json_operation(operation_id: str, root: Path | None = None) -> None:
    store.require_writable()
    root = _root(root)
    path = _journal_path(root, operation_id)
    with operation_guard(root, allow_pending=True):
        journal = store.load_json_safe(path, {})
        if (not isinstance(journal, dict) or journal.get("schema_version") != 1
                or journal.get("operation_id") != operation_id or not isinstance(journal.get("status"), str)
                or journal.get("status") not in ACTIVE_STATES):
            raise store.JsonStoreError("この操作は未完了ではありません。再読込してください。")
        documents = _validated_documents(root, journal.get("documents"))
        with ExitStack() as locks:
            for relative in sorted({doc["path"] for doc in documents}, key=_lock_sort):
                locks.enter_context(store.json_transaction(_record_path(root, relative)))
            _rollback(root, documents)
            journal.update(status="rolled_back", rolled_back_at=_now())
            store.save_json_safe(path, journal)
