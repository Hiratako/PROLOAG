"""Editable category names with historical aliases; never rewrites study logs."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re
import unicodedata
from typing import Any
from uuid import uuid4

from app_core import json_store
from app_core.json_operations import atomic_json_operation
from config import PROJECT_ROOT

DEFAULT_STUDY_CATEGORIES = ("医学", "英語", "語学", "資格", "IT", "読書", "仕事", "その他")
MAX_CATEGORIES = 200
MAX_NAME_LENGTH = 80


def category_catalog_path(root: Path | None = None) -> Path:
    return Path(root or PROJECT_ROOT) / "settings/study_categories.json"


def default_category_catalog() -> dict[str, Any]:
    return {"schema_version": 1, "categories": [
        {"category_id": f"category_default_{index}", "name": name, "aliases": [], "is_active": True}
        for index, name in enumerate(DEFAULT_STUDY_CATEGORIES, 1)]}


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_NAME_LENGTH:
        raise ValueError(f"勉強カテゴリ名は1〜{MAX_NAME_LENGTH}文字で入力してください。")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("勉強カテゴリ名に制御文字は使用できません。")
    return value.strip()


def _key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def validate_category_catalog(data: Any) -> dict[str, Any]:
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1
            or not isinstance(data.get("categories"), list) or not 1 <= len(data["categories"]) <= MAX_CATEGORIES):
        raise ValueError("勉強カテゴリ設定の形式が不正です。バックアップを確認してください。")
    identifiers, owners = set(), {}
    for item in data["categories"]:
        if not isinstance(item, dict):
            raise ValueError("勉強カテゴリの形式が不正です。")
        identifier = item.get("category_id")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier)
                or identifier in identifiers or type(item.get("is_active")) is not bool):
            raise ValueError("勉強カテゴリのIDまたは有効状態が不正です。")
        identifiers.add(identifier)
        name = _name(item.get("name"))
        if item["name"] != name:
            raise ValueError("勉強カテゴリ名の前後に空白があります。")
        aliases = item.get("aliases")
        if not isinstance(aliases, list) or len(aliases) > 200:
            raise ValueError("勉強カテゴリの旧名称の形式が不正です。")
        local = set()
        for label in [name, *aliases]:
            if _name(label) != label:
                raise ValueError("勉強カテゴリの旧名称が不正です。")
            normalized = _key(label)
            if normalized in local or normalized in owners and owners[normalized] != identifier:
                raise ValueError("同じ勉強カテゴリ名または旧名称が重複しています。")
            local.add(normalized)
            owners[normalized] = identifier
    return deepcopy(data)


def load_study_category_catalog(root: Path | None = None) -> dict[str, Any]:
    path = category_catalog_path(root)
    data = json_store.load_json_safe(path, None)
    if data is None and json_store.observed_json_revision(path) is None:
        return default_category_catalog()
    return validate_category_catalog(data)


def study_category_names(catalog: dict[str, Any] | None = None, *, include_inactive: bool = False) -> tuple[str, ...]:
    catalog = default_category_catalog() if catalog is None else catalog
    return tuple(item["name"] for item in catalog["categories"] if include_inactive or item["is_active"])


def study_category_aliases(catalog: dict[str, Any]) -> dict[str, str]:
    return {label: item["name"] for item in catalog["categories"] for label in [item["name"], *item["aliases"]]}


def other_category_name(catalog: dict[str, Any] | None = None) -> str:
    catalog = default_category_catalog() if catalog is None else catalog
    return next((item["name"] for item in catalog["categories"] if item["category_id"] == "category_default_8"), "その他")


def resolve_study_category(value: Any, catalog: dict[str, Any] | None = None, *, unknown: str | None = None) -> str:
    text = str(value or "").strip()
    catalog = default_category_catalog() if catalog is None else catalog
    normalized = _key(text)
    for item in catalog["categories"]:
        if any(_key(label) == normalized for label in [item["name"], *item["aliases"]]):
            return item["name"]
    return text if unknown is None else unknown


def study_category_options(root: Path | None = None, *, current: str = "") -> list[str]:
    result = ["", *study_category_names(load_study_category_catalog(root))]
    if current and current not in result:
        result.append(current)
    return result


def save_study_category(root: Path, name: str, *, category_id: str = "", is_active: bool = True,
                        expected_revision: Any = json_store._UNSET) -> dict[str, Any]:
    clean_name = _name(name)
    if type(is_active) is not bool:
        raise ValueError("勉強カテゴリの有効状態が不正です。")
    if category_id and expected_revision is json_store._UNSET:
        raise ValueError("編集を開始した時点の設定を確認できません。再読込してください。")
    path = category_catalog_path(root)
    expected = None if expected_revision is json_store._UNSET else {path: expected_revision}
    saved = None
    with atomic_json_operation(root=root, kind="study_category_edit", expected_revisions=expected) as operation:
        catalog = validate_category_catalog(operation.read(path, default_category_catalog()))
        items = catalog["categories"]
        current = next((item for item in items if item["category_id"] == category_id), None) if category_id else None
        if category_id and current is None:
            raise ValueError("編集対象の勉強カテゴリがありません。再読込してください。")
        for item in items:
            if item is current:
                continue
            if any(_key(label) == _key(clean_name) for label in [item["name"], *item["aliases"]]):
                raise ValueError("同じ勉強カテゴリ名または旧名称が既に登録されています。")
        now = datetime.now().isoformat(timespec="microseconds")
        if current is None:
            if len(items) >= MAX_CATEGORIES:
                raise ValueError(f"勉強カテゴリは{MAX_CATEGORIES}件までです。")
            current = {"category_id": "category_" + uuid4().hex, "name": clean_name, "aliases": [], "created_at": now}
            items.append(current)
        aliases = [label for label in current["aliases"] if _key(label) != _key(clean_name)]
        if _key(current["name"]) != _key(clean_name) and _key(current["name"]) not in {_key(label) for label in aliases}:
            aliases.append(current["name"])
        current.update(name=clean_name, aliases=aliases, is_active=is_active, updated_at=now)
        validate_category_catalog(catalog)
        operation.stage(path, catalog)
        saved = deepcopy(current)
    return saved
