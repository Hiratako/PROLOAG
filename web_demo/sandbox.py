"""Private, disposable record stores for the public Web experience.

Shipped synthetic records remain immutable. Visitors receive opaque server
handles, never paths selected by URLs, widgets or environment variables.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
from uuid import uuid4

SOURCE_ROOT = Path(__file__).resolve().parents[1]
SESSION_KEY = "_proloag_sandbox"
MAX_SESSION_BYTES = 24 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_SESSION_FILES = 4000
MAX_SESSIONS = 32
IDLE_TTL_SECONDS = 2 * 60 * 60
_MARKER = ".proloag-sandbox.json"
_NAMESPACE = hashlib.sha256(str(SOURCE_ROOT).encode()).hexdigest()[:16]
_DEFAULT_BASE = Path(tempfile.gettempdir()) / ("px-" + _NAMESPACE[:8])
_active: ContextVar[Sandbox | None] = ContextVar("proloag_private_sandbox", default=None)
_registry_lock = threading.RLock()
_registry: dict[str, Sandbox] = {}
_last_cleanup = 0.0


class SandboxAccessError(ValueError):
    """An operation would escape the visitor's disposable record store."""


class SandboxLimitError(SandboxAccessError):
    """The bounded public experience reached its temporary-data limit."""


@dataclass(eq=False)
class Sandbox:
    root: Path
    token: str
    created_at: float = field(default_factory=time.time)
    touched_at: float = field(default_factory=time.time)
    users: int = 0
    retired: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


def _not_link(path: Path) -> None:
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise SandboxAccessError("体験データの保存先を確認できません。体験をリセットしてください。")


def _validate(handle: Sandbox, *, full: bool = True) -> Sandbox:
    with _registry_lock:
        if not isinstance(handle, Sandbox) or _registry.get(handle.token) is not handle:
            raise SandboxAccessError("体験データの接続が切れました。ページを開き直してください。")
        if not full:
            return handle
        _not_link(handle.root.parent)
        _not_link(handle.root)
        if (handle.root.name != handle.token or not handle.root.is_dir()
                or handle.root.resolve().parent != handle.root.parent.resolve()):
            raise SandboxAccessError("体験データの保存先を確認できません。")
        marker = handle.root / _MARKER
        _not_link(marker)
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SandboxAccessError("体験データを確認できません。体験をリセットしてください。") from exc
        if value.get("token") != handle.token or value.get("source") != _NAMESPACE:
            raise SandboxAccessError("体験データの識別情報が一致しません。")
    return handle


def _session_handle() -> Sandbox | None:
    # Streamlit callbacks/fragments can execute before/outside page.run().
    # Resolve their SessionState in their own ScriptRunContext, never a global.
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        if get_script_run_ctx(suppress_warning=True) is None:
            return None
        import streamlit as st
        handle = st.session_state.get(SESSION_KEY)
    except (ImportError, RuntimeError):
        return None
    return _validate(handle, full=False) if handle is not None else None


def current_sandbox() -> Sandbox | None:
    handle = _active.get()
    # Context entry fully validates the handle. Re-reading its marker for each
    # legacy path import would turn a small dashboard into thousands of reads.
    # Every I/O path still receives link/boundary checks in guard_path().
    return _validate(handle, full=False) if handle is not None else _session_handle()


def current_root() -> Path | None:
    handle = current_sandbox()
    return None if handle is None else handle.root


def _inventory(root: Path) -> tuple[int, int]:
    size = files = 0
    for folder, dirs, names in os.walk(root, followlinks=False):
        directory = Path(folder)
        _not_link(directory)
        for name in dirs:
            _not_link(directory / name)
        for name in names:
            path = directory / name
            _not_link(path)
            if not path.is_file():
                raise SandboxAccessError("体験データの形式を確認できません。")
            size += path.stat().st_size
            files += 1
    return size, files


def guard_path(path: os.PathLike | str, *, write: bool = False, additional_bytes: int = 0) -> Path:
    """Fence reads/writes before making locks, backups or temporary files."""
    candidate = Path(path)
    handle = current_sandbox()
    if handle is None:
        if write:
            raise SandboxAccessError("Web体験用の一時データが準備されていません。")
        return candidate.resolve()
    root = handle.root.resolve()
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(root)
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except ValueError as exc:
        raise SandboxAccessError("ほかの利用者や共通サンプルのデータは操作できません。") from exc
    cursor = root
    for component in relative.parts:
        cursor /= component
        _not_link(cursor)
    if write and relative == Path(_MARKER):
        raise SandboxAccessError("体験データの識別情報は変更できません。")
    if write and additional_bytes:
        if additional_bytes < 0 or additional_bytes > MAX_JSON_BYTES:
            raise SandboxLimitError("一度に保存できる体験データの大きさを超えています。内容を短くしてください。")
        used, files = _inventory(root)
        # Atomic replacements/backups can temporarily coexist with the old file.
        if used + additional_bytes > MAX_SESSION_BYTES or files + 2 > MAX_SESSION_FILES:
            raise SandboxLimitError("体験データの上限に達しました。必要なら出力してから体験をリセットしてください。")
    handle.touched_at = time.time()
    return resolved


def _remove(handle: Sandbox) -> bool:
    """Remove only a validated, idle root registered by this process."""
    if not isinstance(handle, Sandbox) or _registry.get(handle.token) is not handle:
        raise SandboxAccessError("体験データの識別情報が一致しません。")
    if not handle.root.exists() and not handle.root.is_symlink():
        _registry.pop(handle.token, None)
        return True
    _validate(handle)
    if handle.users:
        handle.retired = True
        return False
    target = handle.root.resolve()
    parent = handle.root.parent.resolve()
    if target.parent != parent or target.name != handle.token or target == SOURCE_ROOT:
        raise SandboxAccessError("体験データの削除先が不正です。")
    for attempt in range(3):
        try:
            shutil.rmtree(target)
            break
        except FileNotFoundError:
            if not target.exists():
                break
            raise
        except PermissionError:
            # Windows scanners/sync clients can briefly retain a just-closed
            # journal handle. Do not make resetting the public experience fail
            # or widen permissions; retire it and retry during later cleanup.
            if attempt == 2:
                handle.retired = True
                return False
            time.sleep(0.03 * (attempt + 1))
    _registry.pop(handle.token, None)
    return True


def destroy_sandbox(handle: Sandbox) -> bool:
    """Retire a server-owned visitor store; active users finish before cleanup."""
    with _registry_lock:
        return _remove(handle)


def cleanup_expired_sandboxes(*, now: float | None = None, force: bool = False) -> int:
    global _last_cleanup
    now = time.time() if now is None else now
    with _registry_lock:
        if not force and now - _last_cleanup < 60:
            return 0
        _last_cleanup = now
        removed = 0
        for handle in list(_registry.values()):
            if not handle.users and (handle.retired or now - handle.touched_at > IDLE_TTL_SECONDS):
                removed += int(_remove(handle))
        return removed


def create_sandbox(*, base_dir: Path | None = None, _replaces: Sandbox | None = None) -> Sandbox:
    from web_demo.data import DATA_MANIFEST, DATA_ROOT, verify_demo_dataset
    verify_demo_dataset()
    records = json.loads(DATA_MANIFEST.read_text(encoding="utf-8"))["record_sha256"]
    with _registry_lock:
        cleanup_expired_sandboxes(force=True)
        replacement = int(isinstance(_replaces, Sandbox) and _registry.get(_replaces.token) is _replaces)
        if len(_registry) - replacement >= MAX_SESSIONS:
            raise SandboxLimitError("ただいま体験中の利用者が多いため、少し時間をおいて開き直してください。")
        base = (_DEFAULT_BASE if base_dir is None else Path(base_dir)).absolute()
        _not_link(base)
        base.mkdir(parents=True, exist_ok=True)
        base = base.resolve()
        handle = Sandbox(root=base / uuid4().hex, token="")
        handle.token = handle.root.name
        handle.root.mkdir(mode=0o700)
        try:
            for relative, digest in records.items():
                raw = (DATA_ROOT / relative).read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise SandboxAccessError("共通サンプルを確認できません。")
                destination = handle.root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
            for folder in ("statistics_practice", "backups"):
                (handle.root / folder).mkdir(exist_ok=True)
            (handle.root / _MARKER).write_text(json.dumps({"token": handle.token, "source": _NAMESPACE,
                "created_at": handle.created_at}), encoding="utf-8")
            _registry[handle.token] = handle
            return _validate(handle)
        except Exception:
            target = handle.root.resolve()
            if target.parent == base and target.name == handle.token:
                shutil.rmtree(target)
            _registry.pop(handle.token, None)
            raise


@contextmanager
def sandbox_context(handle: Sandbox):
    with _registry_lock:
        _validate(handle)
        handle.users += 1
        handle.touched_at = time.time()
    token = _active.set(handle)
    try:
        yield handle.root
    finally:
        _active.reset(token)
        with _registry_lock:
            handle.users -= 1
            handle.touched_at = time.time()
            if not handle.users and handle.retired:
                _remove(handle)


def ensure_session_sandbox() -> Sandbox:
    import streamlit as st
    handle = st.session_state.get(SESSION_KEY)
    if handle is not None:
        try:
            _validate(handle)
            handle.touched_at = time.time()
            return handle
        except SandboxAccessError:
            pass
    handle = create_sandbox()
    st.session_state[SESSION_KEY] = handle
    return handle


def reset_session_sandbox() -> Sandbox:
    import streamlit as st
    old = st.session_state.get(SESSION_KEY)
    # A reset replaces the caller's existing slot. At capacity it must not
    # require a second permanent slot or discard the old data before seeding.
    fresh = create_sandbox(_replaces=old if isinstance(old, Sandbox) else None)
    st.session_state[SESSION_KEY] = fresh
    if isinstance(old, Sandbox):
        with _registry_lock:
            if _registry.get(old.token) is old:
                _remove(old)
    return fresh
