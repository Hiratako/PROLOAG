from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from uuid import UUID

from app_core.json_store import JsonReadError, json_transaction, load_json_safe, resolved_json_path


def _draft_path(root: Path) -> Path:
    root = resolved_json_path(root)
    path = root / "rough_study_logs" / "drafts.json"
    if resolved_json_path(path) != path:
        raise ValueError("下書きの保存先がアプリの既定のフォルダーと異なります。")
    return path


def _draft_key(key: str) -> str:
    if key == "period":
        return key
    if isinstance(key, str):
        kind, _, value = key.partition(":")
        try:
            if kind == "week" and value == date.fromisoformat(value).isoformat():
                return key
            if kind == "edit":
                return f"edit:{UUID(value).hex}"
        except ValueError:
            pass
    raise ValueError("下書きの識別子が不正です。")


def _load_document(path: Path) -> dict:
    data = load_json_safe(path, {"schema_version": 1, "drafts": {}})
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or not isinstance(data.get("drafts"), dict)
    ):
        raise JsonReadError("下書きの保存形式を読み込めません。ファイルを確認してください。")
    try:
        for key, payload in data["drafts"].items():
            if _draft_key(key) != key or not isinstance(payload, dict):
                raise ValueError("下書きの識別子または内容が不正です。")
    except ValueError as exc:
        raise JsonReadError("下書きの保存形式を読み込めません。ファイルを確認してください。") from exc
    return data


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("下書きの内容を保存できる形式に変換できません。") from exc


def _replace_draft_file(path: Path, data: dict) -> None:
    # Input autosaves are frequent. Keep one atomic draft document without
    # generating the version backups reserved for explicitly saved records.
    encoded = _json_bytes(data)
    pending = None
    try:
        with NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".rough-draft-", suffix=".tmp", delete=False,
        ) as handle:
            pending = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(6):
            try:
                os.replace(pending, path)
                pending = None
                break
            except OSError as exc:
                # Windows path resolution, another app process, or a scanner
                # can briefly keep the destination open during a replacement.
                if getattr(exc, "winerror", None) not in {5, 32} or attempt == 5:
                    raise
                sleep(0.02 * (attempt + 1))
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def _draft_exists(path: Path) -> bool:
    try:
        return path.exists()
    except PermissionError:
        # Windows can briefly deny access during another tab's replacement.
        # Let the reader or discard wait for that transaction before retrying.
        return True


def load_rough_draft(root: Path, key: str) -> dict | None:
    """Read one draft without creating storage for a missing document."""
    key = _draft_key(key)
    path = _draft_path(root)
    if not _draft_exists(path):
        return None
    # On Windows a concurrent read and os.replace can fail with a sharing
    # violation even though both sides only access one complete document.
    with json_transaction(path):
        return _load_document(path)["drafts"].get(key)


@contextmanager
def locked_rough_draft(root: Path, key: str):
    """Keep a draft snapshot current through dependent reads and writes."""
    key = _draft_key(key)
    path = _draft_path(root)
    with json_transaction(path):
        yield _load_document(path)["drafts"].get(key)


def save_rough_draft(root: Path, key: str, payload: dict) -> None:
    """Atomically replace one draft while retaining the other drafts."""
    key = _draft_key(key)
    if not isinstance(payload, dict):
        raise ValueError("下書きの内容は項目ごとのデータで指定してください。")
    encoded = _json_bytes(payload)
    path = _draft_path(root)
    with json_transaction(path):
        data = _load_document(path)
        drafts = data["drafts"]
        if key in drafts and _json_bytes(drafts[key]) == encoded:
            return
        drafts[key] = json.loads(encoded)
        _replace_draft_file(path, data)


def discard_rough_draft(root: Path, key: str) -> None:
    """Remove one draft after a confirmed save or explicit discard."""
    key = _draft_key(key)
    path = _draft_path(root)
    if not _draft_exists(path):
        return
    with json_transaction(path):
        data = _load_document(path)
        drafts = data["drafts"]
        if key in drafts:
            del drafts[key]
            _replace_draft_file(path, data)
