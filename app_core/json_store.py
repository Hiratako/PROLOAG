from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import PROJECT_ROOT


def record_json_files(folder: Path) -> list[Path]:
    """List live JSON records in traversal order, excluding backup artifacts."""
    folder = Path(folder)
    return [
        path for path in folder.rglob("*.json")
        if path.is_file()
        and "_backup_" not in path.stem.lower()
        and not any(part.lower() in {"backup", "backups"}
                    for part in path.relative_to(folder).parts[:-1])
    ]


class JsonStoreError(ValueError):
    """A JSON operation was stopped to protect stored data."""


class JsonReadError(JsonStoreError):
    pass


class JsonConflictError(JsonStoreError):
    pass


class JsonLockTimeoutError(JsonStoreError):
    pass


_UNSET = object()
_observations: ContextVar[dict[str, str | None]] = ContextVar("json_observations", default={})
_active_operation: ContextVar[Any] = ContextVar("json_active_operation", default=None)
_committing_operation: ContextVar[Any] = ContextVar("json_committing_operation", default=None)
_registry_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_held = threading.local()


def resolved_json_path(path: Path) -> Path:
    """Resolve symlinks while normalizing equivalent Windows path prefixes."""
    resolved = str(Path(path).resolve())
    # Windows resolve() may retain the extended prefix transiently while an
    # atomic replacement is in progress. Both spellings name the same file.
    if os.name == "nt":
        if resolved[:8].upper() == "\\\\?\\UNC\\":
            resolved = "\\\\" + resolved[8:]
        elif resolved.startswith("\\\\?\\"):
            resolved = resolved[4:]
    return Path(resolved)


def _key(path: Path) -> str:
    return os.path.normcase(str(resolved_json_path(path)))


def _observe(key: str, revision: str | None) -> None:
    _observations.set({**_observations.get(), key: revision})


def observed_json_revision(path: Path) -> Any:
    """Capture the caller's last read revision before an internal reload.

    Pass the result unchanged to save_json_safe(expected_revision=...). None
    denotes an observed missing file; an unobserved path returns the internal
    unspecified sentinel and retains save_json_safe's default behavior.
    """
    return _observations.get().get(_key(Path(path)), _UNSET)


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number: {value}")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _read(path: Path) -> tuple[Any, bytes | None, str | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None, None
    except OSError as exc:
        raise JsonReadError(f"JSONを読み込めません: {path}。アクセス権とバックアップを確認して再読込してください。") from exc
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_pairs,
                          parse_constant=_reject_constant)
        # Overflow such as 1e999 is not passed to parse_constant.
        json.dumps(data, allow_nan=False)
    except (ValueError, UnicodeError, OverflowError) as exc:
        raise JsonReadError(f"JSONが破損しているか無効な値を含みます: {path}。バックアップを確認してください。自動上書きは行いません。") from exc
    return data, raw, hashlib.sha256(raw).hexdigest()


def record_project_root(path: Path) -> Path | None:
    """Identify only established app record locations, including isolated tests."""
    path = resolved_json_path(path)
    if path.name in {"flashcards.json", "reminders.json"}:
        return path.parent
    if path.suffix != ".json":
        return None
    if path.parent.name in {"notes_data", "settings", "master_data"}:
        return path.parent.parent
    if path.parent.name == "backups" and path.name in {"note_trash.json", "study_log_trash.json", "study_session_ids.json"}:
        return path.parent.parent
    for ancestor in path.parents:
        if ancestor.name.lower() in {"backups", "backup"}:
            break
        if ancestor.name in {
            "study_logs", "daily_plans", "diary_entries", "health_logs", "exercise_logs", "statistics_practice"
        }:
            return ancestor.parent
    return None


@contextmanager
def json_transaction(path: Path, timeout: float = 10.0):
    """App records share root -> document lock order with journaled operations.

    Legacy writers also participate, so they cannot race a note deletion between
    its final reference check and commit. This does not automatically authorize
    stale form data or perform recovery.
    """
    path = resolved_json_path(path)
    root = record_project_root(path)
    if root is None:
        with _file_transaction(path, timeout):
            yield
    else:
        operation_lock = root / "backups/json_operations/operations"
        # resolve() can alternate between extended and ordinary Windows paths
        # while another thread creates a directory. Compare canonical keys.
        if not Path(_key(operation_lock)).is_relative_to(Path(_key(root))):
            raise JsonStoreError("操作ロックの保存先がプロジェクト外です。")
        with _file_transaction(operation_lock, timeout), _file_transaction(path, timeout):
            yield


@contextmanager
def _file_transaction(path: Path, timeout: float = 10.0):
    """Serialize a complete read/modify/write operation; reentrant per thread."""
    path = resolved_json_path(path)
    key = _key(path)
    if timeout < 0:
        raise ValueError("timeout must not be negative")
    with _registry_guard:
        lock = _locks.setdefault(key, threading.RLock())
    deadline = time.monotonic() + timeout
    if not lock.acquire(timeout=timeout):
        raise JsonLockTimeoutError(f"JSONの更新待ちがタイムアウトしました: {path}")
    held = getattr(_held, "paths", None)
    if held is None:
        held = _held.paths = set()
    handle = None
    acquired = False
    nested = key in held
    try:
        if not nested:
            ensure_dir(path.parent)
            handle = path.with_name(f".{path.name}.lock").open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise JsonLockTimeoutError(f"JSONの更新待ちがタイムアウトしました: {path}") from exc
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            held.add(key)
        yield
    finally:
        try:
            if not nested:
                held.discard(key)
                if handle is not None:
                    try:
                        if acquired:
                            handle.seek(0)
                            if os.name == "nt":
                                import msvcrt
                                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                            else:
                                import fcntl
                                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()
        finally:
            lock.release()


def json_transaction_for(path_getter, *, timeout: float = 10.0):
    """path_getter receives the wrapped function's positional/keyword arguments."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with json_transaction(path_getter(*args, **kwargs), timeout=timeout):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json_safe(path: Path, default: Any) -> Any:
    path = Path(path)
    operation = _active_operation.get()
    if operation is not None:
        return operation.read(path, default)
    data, raw, revision = _read(path)
    _observe(_key(path), revision)
    return default if raw is None else data


def save_json_safe(path: Path, data: Any, *, expected_revision: Any = _UNSET) -> None:
    # Serialize before touching any file, including lock and backup files.
    encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    canonical = json.dumps(json.loads(encoded, object_pairs_hook=_unique_pairs),
                           sort_keys=True, allow_nan=False)
    operation = _active_operation.get()
    if operation is not None:
        operation.stage(Path(path), data, expected_revision=expected_revision)
        return
    path = resolved_json_path(path)
    key = _key(path)
    with json_transaction(path):
        existing, raw, revision = _read(path)
        observed = _observations.get().get(key, _UNSET) if expected_revision is _UNSET else expected_revision
        if observed is not _UNSET and observed != revision:
            # An internal reload must not authorize retrying the same stale edit.
            _observe(key, observed)
            raise JsonConflictError(f"別の更新が保存されています。再読込してください: {path}")
        root = record_project_root(path)
        if root is not None and _committing_operation.get() is None:
            from app_core.json_operations import pending_json_operations
            from app_core.note_reference_guard import validate_note_references
            if pending_json_operations(root):
                raise JsonStoreError("未完了の保存操作があります。サイドバーの保存操作の復旧を先に実行してください。")
            validate_note_references(root, path, data)
        if raw is not None and json.dumps(existing, sort_keys=True, allow_nan=False) == canonical:
            _observe(key, revision)
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.save-", suffix=".tmp",
                                             dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if raw is not None:
                _save_generation(path, raw)
            os.replace(temporary, path)
            temporary = None
            _observe(key, hashlib.sha256(encoded).hexdigest())
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _save_generation(path: Path, raw: bytes) -> None:
    try:
        relative = Path(_key(path)).relative_to(Path(_key(PROJECT_ROOT)))
    except ValueError:
        folder = path.parent / "backups" / "auto_save" / path.name
    else:
        folder = PROJECT_ROOT / "backups" / "auto_save" / relative.parent / relative.name
    ensure_dir(folder)
    name = f"{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex}.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".generation-", suffix=".tmp",
                                         dir=folder, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, folder / name)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def backup_json(path: Path, backup_root: Path | None = None, relative_to: Path | None = None) -> Path | None:
    if not path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if backup_root is None:
        backup_path = path.with_name(f"{path.stem}_backup_{timestamp}{path.suffix}")
    else:
        relative_path = Path(path.name)
        if relative_to is not None:
            try:
                relative_path = path.resolve().relative_to(relative_to.resolve())
            except ValueError:
                pass
        backup_path = backup_root / timestamp / relative_path
        ensure_dir(backup_path.parent)
    shutil.copy2(path, backup_path)
    return backup_path
