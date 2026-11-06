"""Local-only material links and resume notes; never fetch linked resources."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from app_core import json_store
from app_core.analysis_dates import parse_analysis_datetime
from app_core.json_operations import atomic_json_operation, operation_guard


class LearningContextError(json_store.JsonStoreError):
    pass


MAX_LINKS = 50
MAX_TITLE_LENGTH = 200
MAX_URL_LENGTH = 2048
MAX_NEXT_STEP_LENGTH = 10000
_RESOURCE_ID = r"[A-Za-z0-9_-]+"


def normalize_resource_url(value: Any) -> str:
    """Validate a direct Google Docs/Sheets/Slides/Drive HTTPS resource URL."""
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH:
        raise LearningContextError("資料リンクは2048文字以内のGoogle資料URLを指定してください。")
    url = value.strip()
    if not url or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url) or "\\" in url:
        raise LearningContextError("資料リンクに空白・制御文字・不正な区切りがあります。")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.hostname not in {"docs.google.com", "drive.google.com"}):
            raise ValueError("unsafe authority")
        path = parsed.path
        if parsed.hostname == "docs.google.com":
            valid = re.fullmatch(rf"/(?:document|spreadsheets|presentation)/(?:u/[0-9]+/)?d/(?:e/)?{_RESOURCE_ID}(?:/[^\\]*)?", path)
        else:
            valid = re.fullmatch(rf"/(?:file/d/{_RESOURCE_ID}(?:/[^\\]*)?|drive/(?:u/[0-9]+/)?folders/{_RESOURCE_ID}/?)", path)
            if path == "/open":
                identifiers = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
                valid = len(identifiers) == 1 and re.fullmatch(_RESOURCE_ID, identifiers[0])
        if not valid or any(part in {".", ".."} for part in path.split("/")) or "%" in path:
            raise ValueError("not a resource")
    except (ValueError, UnicodeError):
        raise LearningContextError("Google Docs・Sheets・Slides・Driveの資料へのHTTPSリンクだけを登録できます。") from None
    return url


def is_valid_google_resource_url(value: Any) -> bool:
    try:
        normalize_resource_url(value)
        return True
    except LearningContextError:
        return False


def _path(root: Path, relative: str) -> Path:
    root = json_store.resolved_json_path(root)
    path = root / relative
    if not json_store.resolved_json_path(path).is_relative_to(root):
        raise LearningContextError("学習引き継ぎの保存先がプロジェクト外を参照しています。")
    return path


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise LearningContextError(f"{field}が不正です。")
    return value


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise LearningContextError(f"{field}は{limit}文字以内で入力してください。")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise LearningContextError("保存日時が不正です。")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise LearningContextError("保存日時が不正です。") from None
    parsed = parse_analysis_datetime(value)
    if parsed is None:
        raise LearningContextError("保存日時をローカル日時として解釈できません。")
    return parsed


def _validate_context(data: Any) -> dict:
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1 or not isinstance(data.get("materials"), dict):
        raise LearningContextError("学習引き継ぎデータの形式が不正です。自動修復・上書きは行いません。")
    for key, entry in data["materials"].items():
        _identifier(key, "教材ID")
        if not isinstance(entry, dict) or entry.get("material_id") != key:
            raise LearningContextError("教材IDと引き継ぎデータの対応が不正です。")
        _text(entry.get("next_step"), "次はここから", MAX_NEXT_STEP_LENGTH)
        created = _timestamp(entry.get("created_at"))
        updated = _timestamp(entry.get("updated_at"))
        if updated < created:
            raise LearningContextError("更新日時が作成日時より前になっています。")
        links = entry.get("links")
        if not isinstance(links, list) or len(links) > MAX_LINKS:
            raise LearningContextError("資料リンク一覧の形式が不正です。")
        seen = set()
        for link in links:
            if not isinstance(link, dict):
                raise LearningContextError("資料リンクの形式が不正です。")
            link_id = _identifier(link.get("link_id"), "リンクID")
            if link_id in seen:
                raise LearningContextError("同じ教材内でリンクIDが重複しています。")
            seen.add(link_id)
            _text(link.get("title"), "リンク名", MAX_TITLE_LENGTH)
            normalize_resource_url(link.get("url"))
    return deepcopy(data)


def load_learning_context(root: Path) -> dict:
    return _validate_context(json_store.load_json_safe(
        _path(root, "settings/learning_context.json"), {"schema_version": 1, "materials": {}}))


def _material_list(master: Any) -> list[dict]:
    if not isinstance(master, dict):
        raise LearningContextError("分類マスタの形式が不正です。")
    indexes = {}
    for collection, key in (("projects", "project_id"), ("subjects", "subject_id"), ("materials", "material_id")):
        items = master.get(collection, [])
        if not isinstance(items, list):
            raise LearningContextError("分類マスタの一覧形式が不正です。")
        index = {}
        for item in items:
            if not isinstance(item, dict):
                raise LearningContextError("分類マスタの記録形式が不正です。")
            identifier = _identifier(item.get(key), "分類ID")
            if identifier in index:
                raise LearningContextError("分類マスタのIDが重複しています。")
            if not isinstance(item.get("name", ""), str) or type(item.get("is_active", True)) is not bool:
                raise LearningContextError("分類マスタの名称・状態が不正です。")
            index[identifier] = item
        indexes[collection] = index
    rows = []
    for identifier, item in indexes["materials"].items():
        row = {"material_id": identifier, "name": item.get("name") or "名称未設定", "is_active": item.get("is_active", True)}
        for singular, plural in (("project", "projects"), ("subject", "subjects")):
            reference = item.get(singular + "_id") or ""
            if not isinstance(reference, str):
                raise LearningContextError("教材の所属分類IDが不正です。")
            row[singular + "_id"] = reference
            row[singular + "_name"] = indexes[plural].get(reference, {}).get("name") or ("不明" if reference else "未設定")
        row["label"] = f"{row['name']} / {row['project_name']} / {row['subject_name']} [{identifier}]" + ("（無効）" if not row["is_active"] else "")
        rows.append(row)
    return sorted(rows, key=lambda row: (row["label"], row["material_id"]))


def list_materials(root: Path, *, master_data: Any = None) -> list[dict]:
    """Validate raw display data shared in one render; writes still read fresh."""
    raw = (json_store.load_json_safe(_path(root, "master_data/master.json"), {})
           if master_data is None else master_data)
    return _material_list(raw)


def load_context_snapshot(root: Path) -> dict:
    with operation_guard(root):
        data = load_learning_context(root)
        context_path, master_path = _path(root, "settings/learning_context.json"), _path(root, "master_data/master.json")
        context_revision = json_store.observed_json_revision(context_path)
        list_materials(root)
        master_revision = json_store.observed_json_revision(master_path)
        if json_store._read(context_path)[2] != context_revision or json_store._read(master_path)[2] != master_revision:
            raise json_store.JsonConflictError("読取中に教材・引き継ぎ情報が変更されました。再読込してください。")
        return {"data": data, "revision": {"context": context_revision, "master": master_revision}}


def save_material_context(root: Path, material_id: str, links: list[dict], next_step: str, *, expected_revision: dict) -> dict:
    material_id = _identifier(material_id, "教材ID")
    next_step = _text(next_step, "次はここから", MAX_NEXT_STEP_LENGTH)
    if not isinstance(links, list) or len(links) > MAX_LINKS or any(not isinstance(link, dict) for link in links):
        raise LearningContextError("資料リンクは50件以内で指定してください。")
    if not isinstance(expected_revision, dict) or set(expected_revision) != {"context", "master"} or any(
        revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision))
        for revision in expected_revision.values()
    ):
        raise LearningContextError("編集開始時の状態がありません。教材を開き直してください。")
    context_path, master_path = _path(root, "settings/learning_context.json"), _path(root, "master_data/master.json")
    with atomic_json_operation(root=root, kind="learning_context_save", expected_revisions={
        context_path: expected_revision["context"], master_path: expected_revision["master"],
    }):
        data = load_learning_context(root)
        if material_id not in {item["material_id"] for item in list_materials(root)}:
            raise LearningContextError("教材が削除されています。入力は保持しています。元の教材を確認してください。")
        existing = deepcopy(data["materials"].get(material_id, {}))
        original_links = {item["link_id"]: item for item in existing.get("links", [])}
        saved_links, seen = [], set()
        for item in links:
            link_id = item.get("link_id")
            if link_id in (None, ""):
                link_id = "link_" + uuid4().hex
            else:
                _identifier(link_id, "リンクID")
                if link_id not in original_links:
                    raise LearningContextError("元の資料リンクが見つかりません。リンクIDを新規に指定せず追加してください。")
            if link_id in seen:
                raise LearningContextError("同じリンクが重複しています。")
            seen.add(link_id)
            saved_links.append({**deepcopy(original_links.get(link_id, {})), **deepcopy(item), "link_id": link_id,
                                "title": _text(item.get("title", ""), "リンク名", MAX_TITLE_LENGTH),
                                "url": normalize_resource_url(item.get("url"))})
        timestamp = datetime.now().astimezone().isoformat(timespec="microseconds")
        existing.update(material_id=material_id, links=saved_links, next_step=next_step,
                        created_at=existing.get("created_at", timestamp), updated_at=timestamp)
        data["materials"][material_id] = existing
        _validate_context(data)
        json_store.save_json_safe(context_path, data, expected_revision=expected_revision["context"])
    return deepcopy(existing)
