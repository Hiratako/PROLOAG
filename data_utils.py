from __future__ import annotations

import re
from datetime import date, datetime
from app_core.app_clock import app_today
from pathlib import Path
from typing import Any

from app_core import ids as id_core
from app_core import json_store
from config import BACKUP_DIR, DAILY_PLANS_DIR, FLASHCARDS_PATH, MASTER_DATA_DIR, NOTES_DATA_DIR, PROJECT_ROOT, REMINDERS_PATH, STUDY_LOGS_DIR


# Public-release note: before this local-first app is exposed to multiple
# users, add authentication, user_id-based data separation, database-backed
# storage, backups, privacy controls, and access control for diary/study logs.


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir(path: Path) -> Path:
    return json_store.ensure_dir(path)


def load_json_safe(path: Path, default: Any) -> Any:
    return json_store.load_json_safe(path, default)


def save_json_safe(path: Path, data: Any) -> None:
    json_store.save_json_safe(path, data)


def backup_json(path: Path) -> Path | None:
    return json_store.backup_json(path, BACKUP_DIR / "auto_normalization", PROJECT_ROOT)


def _date_token(value: str | date | datetime | None = None) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed:
            return parsed.strftime("%Y%m%d")
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 8:
            return digits[:8]
    return datetime.now().strftime("%Y%m%d")


def generate_id(prefix: str, date_value: str | date | datetime | None = None, existing_ids: set[str] | None = None, **kwargs: Any) -> str:
    return id_core.generate_id(prefix, date_value, existing_ids, **kwargs)


def generate_daily_sequence_id(prefix: str, existing_ids: set[str] | None = None, date_value: str | date | datetime | None = None, **kwargs: Any) -> str:
    return id_core.generate_daily_sequence_id(prefix, existing_ids, date_value, **kwargs)


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def ensure_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


MASTER_COLLECTIONS = {
    "projects": "project_id",
    "subjects": "subject_id",
    "materials": "material_id",
    "units": "unit_id",
    "tags": "tag_id",
}

MASTER_LABELS = {
    "project_id": "不明なプロジェクト",
    "subject_id": "不明な科目",
    "material_id": "不明な教材",
    "unit_id": "不明な単元",
    "tag_id": "不明なタグ",
}

MASTER_KEYS = ("project_id", "subject_id", "material_id", "unit_id")


def master_path(root: Path | None = None) -> Path:
    return (root / "master_data" if root else MASTER_DATA_DIR) / "master.json"


def default_master_data() -> dict[str, Any]:
    return {"projects": [], "subjects": [], "materials": [], "units": [], "tags": []}


def generate_master_id(prefix: str, existing_ids: set[str] | None = None) -> str:
    return generate_id(prefix, existing_ids=existing_ids)


def normalize_master_data(data: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    changed = False
    if not isinstance(data, dict):
        return default_master_data(), True
    timestamp = now_text()
    for collection, id_key in MASTER_COLLECTIONS.items():
        if not isinstance(data.get(collection), list):
            data[collection] = []
            changed = True
        existing_ids = {str(item.get(id_key)) for item in data[collection] if isinstance(item, dict) and item.get(id_key)}
        normalized = []
        for item in data[collection]:
            if not isinstance(item, dict):
                changed = True
                continue
            if not item.get(id_key):
                item[id_key] = generate_master_id(id_key.removesuffix("_id"), existing_ids)
                changed = True
            item.setdefault("name", "名称未設定")
            item.setdefault("description", "")
            if "is_active" not in item:
                item["is_active"] = True
                changed = True
            if not item.get("created_at"):
                item["created_at"] = timestamp
                changed = True
            if not item.get("updated_at"):
                item["updated_at"] = item["created_at"]
                changed = True
            if collection in ("subjects", "materials", "units"):
                item.setdefault("project_id", "")
            if collection in ("materials", "units"):
                item.setdefault("subject_id", "")
            if collection == "materials":
                item.setdefault("type", "その他")
            if collection == "units":
                item.setdefault("material_id", "")
                item.setdefault("parent_unit_id", "")
            normalized.append(item)
        data[collection] = normalized
    return data, changed


def load_master_data(root: Path | None = None, *, write_back: bool = False) -> dict[str, Any]:
    path = master_path(root)
    missing = not path.exists()
    data = load_json_safe(path, default_master_data())
    data, changed = normalize_master_data(data)
    if write_back and (missing or changed):
        backup_json(path)
        save_json_safe(path, data)
    return data


def save_master_data(data: dict[str, Any], root: Path | None = None) -> None:
    normalized, _ = normalize_master_data(data)
    save_json_safe(master_path(root), normalized)


def ensure_master_data(root: Path | None = None) -> dict[str, Any]:
    return load_master_data(root, write_back=True)


def _find_master_item(root: Path | None, collection: str, id_key: str, item_id: str | None) -> dict[str, Any] | None:
    if not item_id:
        return None
    for item in load_master_data(root).get(collection, []):
        if item.get(id_key) == item_id:
            return item
    return None


def resolve_master_name(root: Path | None, collection: str, id_key: str, item_id: str | None, unknown: str) -> str:
    if not item_id:
        return "未設定"
    item = _find_master_item(root, collection, id_key, item_id)
    if not item or not item.get("is_active", True):
        return unknown
    return item.get("name", unknown)


def resolve_project_name(project_id: str | None, root: Path | None = None) -> str:
    return resolve_master_name(root, "projects", "project_id", project_id, "不明なプロジェクト")


def resolve_subject_name(subject_id: str | None, root: Path | None = None) -> str:
    return resolve_master_name(root, "subjects", "subject_id", subject_id, "不明な科目")


def resolve_material_name(material_id: str | None, root: Path | None = None) -> str:
    return resolve_master_name(root, "materials", "material_id", material_id, "不明な教材")


def resolve_unit_name(unit_id: str | None, root: Path | None = None) -> str:
    return resolve_master_name(root, "units", "unit_id", unit_id, "不明な単元")


def resolve_tag_name(tag_id: str | None, root: Path | None = None) -> str:
    return resolve_master_name(root, "tags", "tag_id", tag_id, "不明なタグ")


def _active_items(root: Path | None, collection: str, **filters: str | None) -> list[dict[str, Any]]:
    items = [item for item in load_master_data(root).get(collection, []) if item.get("is_active", True)]
    for key, value in filters.items():
        if value:
            items = [item for item in items if item.get(key) == value]
    return items


def get_active_projects(root: Path | None = None) -> list[dict[str, Any]]:
    return _active_items(root, "projects")


def get_active_subjects(project_id: str | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    return _active_items(root, "subjects", project_id=project_id)


def get_active_materials(project_id: str | None = None, subject_id: str | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    return _active_items(root, "materials", project_id=project_id, subject_id=subject_id)


def get_active_units(project_id: str | None = None, subject_id: str | None = None, material_id: str | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    return _active_items(root, "units", project_id=project_id, subject_id=subject_id, material_id=material_id)


def get_active_tags(root: Path | None = None) -> list[dict[str, Any]]:
    return _active_items(root, "tags")


def resolve_master_names(record: dict[str, Any], root: Path | None = None) -> dict[str, str]:
    return {
        "project_name": resolve_project_name(record.get("project_id"), root),
        "subject_name": resolve_subject_name(record.get("subject_id"), root),
        "material_name": resolve_material_name(record.get("material_id"), root),
        "unit_name": resolve_unit_name(record.get("unit_id"), root),
    }


_MASTER_NAME_FIELDS = (
    ("project_id", "projects", "project_name", "不明なプロジェクト"),
    ("subject_id", "subjects", "subject_name", "不明な科目"),
    ("material_id", "materials", "material_name", "不明な教材"),
    ("unit_id", "units", "unit_name", "不明な単元"),
)


def build_master_name_lookup(master_data: dict[str, Any]) -> dict[str, dict[Any, Any]]:
    """Build a per-rerun name lookup without weakening the strict JSON loader."""
    lookup: dict[str, dict[Any, Any]] = {}
    for id_key, collection, _output_key, unknown in _MASTER_NAME_FIELDS:
        names: dict[Any, Any] = {}
        items = master_data.get(collection, [])
        if not isinstance(items, list):
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get(id_key)
            if not item_id:
                continue
            try:
                if item_id not in names:
                    names[item_id] = item.get("name", unknown) if item.get("is_active", True) else unknown
            except TypeError:
                continue
        lookup[id_key] = names
    return lookup


def resolve_master_names_from_lookup(
    record: dict[str, Any],
    lookup: dict[str, dict[Any, Any]],
) -> dict[str, str]:
    """Resolve the four study classifications from a prebuilt master snapshot."""
    resolved: dict[str, str] = {}
    for id_key, _collection, output_key, unknown in _MASTER_NAME_FIELDS:
        item_id = record.get(id_key)
        if not item_id:
            resolved[output_key] = "未設定"
        else:
            try:
                resolved[output_key] = lookup.get(id_key, {}).get(item_id, unknown)
            except TypeError:
                resolved[output_key] = unknown
    return resolved


def notes_path(root: Path | None = None) -> Path:
    return (root / "notes_data" if root else NOTES_DATA_DIR) / "notebooks.json"


def collect_note_store_ids(store: dict[str, Any]) -> dict[str, set[str]]:
    """Reserve all hierarchy IDs, including legacy page aliases, before issuing new IDs."""
    reserved: dict[str, set[str]] = {"notebook": set(), "section": set(), "note": set()}
    for notebook in ensure_list(store.get("notebooks")):
        if not isinstance(notebook, dict):
            continue
        if notebook.get("id"):
            reserved["notebook"].add(str(notebook["id"]))
        for section in ensure_list(notebook.get("sections")):
            if not isinstance(section, dict):
                continue
            if section.get("id"):
                reserved["section"].add(str(section["id"]))
            for page in ensure_list(section.get("pages")):
                if isinstance(page, dict):
                    for key in ("note_id", "id"):
                        if page.get(key):
                            reserved["note"].add(str(page[key]))
    return reserved


def default_note_store() -> dict[str, Any]:
    note_id = generate_id("note")
    timestamp = now_text()
    return {
        "notebooks": [
            {
                "id": generate_id("notebook"),
                "name": "学習ノート",
                "sections": [
                    {
                        "id": generate_id("section"),
                        "name": "クイックメモ",
                        "pages": [
                            {
                                "id": note_id,
                                "note_id": note_id,
                                "title": "はじめてのノート",
                                "body": "ここに学習メモを記録します。",
                                "content": "ここに学習メモを記録します。",
                                "tags": ["メモ"],
                                "created_at": timestamp,
                                "updated_at": timestamp,
                                "linked_study_session_ids": [],
                                "linked_flashcard_ids": [],
                                "linked_reminder_ids": [],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def normalize_note_store(store: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    changed = False
    if not isinstance(store, dict) or not isinstance(store.get("notebooks"), list) or not store.get("notebooks"):
        return default_note_store(), True

    reserved_ids = collect_note_store_ids(store)
    existing_note_ids = reserved_ids["note"]
    seen_note_ids: set[str] = set()

    for notebook in store["notebooks"]:
        if not notebook.get("id"):
            notebook["id"] = generate_id("notebook", existing_ids=reserved_ids["notebook"])
            changed = True
        notebook.setdefault("name", "学習ノート")
        if not isinstance(notebook.get("sections"), list) or not notebook["sections"]:
            notebook["sections"] = [{"id": generate_id("section", existing_ids=reserved_ids["section"]), "name": "クイックメモ", "pages": []}]
            changed = True
        for section in notebook["sections"]:
            if not section.get("id"):
                section["id"] = generate_id("section", existing_ids=reserved_ids["section"])
                changed = True
            section.setdefault("name", "クイックメモ")
            if not isinstance(section.get("pages"), list):
                section["pages"] = []
                changed = True
            if not section["pages"]:
                note_id = generate_id("note", existing_ids=existing_note_ids)
                timestamp = now_text()
                section["pages"].append(
                    {
                        "id": note_id,
                        "note_id": note_id,
                        "title": "無題のページ",
                        "body": "",
                        "content": "",
                        "tags": [],
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "linked_study_session_ids": [],
                        "linked_flashcard_ids": [],
                        "linked_reminder_ids": [],
                    }
                )
                changed = True
            for page in section["pages"]:
                created_at = page.get("created_at") or now_text()
                current_note_id = str(page.get("note_id") or page.get("id") or "")
                if not current_note_id or current_note_id in seen_note_ids:
                    page["note_id"] = generate_id("note", created_at, existing_note_ids)
                    changed = True
                elif not page.get("note_id"):
                    page["note_id"] = current_note_id
                    changed = True
                existing_note_ids.add(page["note_id"])
                seen_note_ids.add(page["note_id"])
                if not page.get("id") or page.get("id") != page["note_id"]:
                    page["id"] = page["note_id"]
                    changed = True
                if "body" not in page and "content" in page:
                    page["body"] = page.get("content", "")
                    changed = True
                if "content" not in page:
                    page["content"] = page.get("body", "")
                    changed = True
                if "created_at" not in page or not page.get("created_at"):
                    page["created_at"] = created_at
                    changed = True
                if "updated_at" not in page or not page.get("updated_at"):
                    page["updated_at"] = page["created_at"]
                    changed = True
                for key in ("tags", "linked_study_session_ids", "linked_flashcard_ids", "linked_reminder_ids"):
                    normalized = ensure_list(page.get(key))
                    if page.get(key) != normalized:
                        page[key] = normalized
                        changed = True
                for key in MASTER_KEYS:
                    if key not in page:
                        page[key] = ""
                        changed = True
    return store, changed


def load_note_store(root: Path, *, write_back: bool = False, backup: bool = True) -> dict[str, Any]:
    path = notes_path(root)
    store = load_json_safe(path, default_note_store())
    store, changed = normalize_note_store(store)
    if write_back and changed:
        if backup:
            backup_json(path)
        save_json_safe(path, store)
    return store


def save_note_store(root: Path, store: dict[str, Any]) -> None:
    normalized, _ = normalize_note_store(store)
    save_json_safe(notes_path(root), normalized)


def iter_note_pages(store: dict[str, Any]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for notebook in store.get("notebooks", []):
        for section in notebook.get("sections", []):
            for page in section.get("pages", []):
                if isinstance(page, dict):
                    pages.append({"notebook": notebook, "section": section, "page": page})
    return pages


def load_all_notes(root: Path) -> list[dict[str, Any]]:
    store = load_note_store(root)
    notes = []
    for item in iter_note_pages(store):
        page = item["page"]
        notes.append(
            {
                "note_id": page.get("note_id", ""),
                "title": page.get("title", "無題のページ"),
                "tags": page.get("tags", []),
                "notebook": item["notebook"].get("name", ""),
                "section": item["section"].get("name", ""),
                **{key: page.get(key, "") for key in MASTER_KEYS},
            }
        )
    return notes


def resolve_note_titles(root: Path, note_ids: list[str]) -> str:
    note_id_set = {str(note_id) for note_id in ensure_list(note_ids) if note_id}
    if not note_id_set:
        return ""
    titles = []
    found_ids = set()
    for note in load_all_notes(root):
        if note["note_id"] in note_id_set:
            titles.append(note["title"])
            found_ids.add(note["note_id"])
    missing_count = len(note_id_set - found_ids)
    titles.extend(["不明なノート"] * missing_count)
    return " / ".join(titles)


@json_store.json_transaction_for(lambda root, note_ids, session_id: notes_path(root))
def link_notes_to_session(root: Path, note_ids: list[str], session_id: str) -> None:
    note_id_set = {str(note_id) for note_id in ensure_list(note_ids) if note_id}
    if not note_id_set or not session_id:
        return
    store = load_note_store(root)
    changed = False
    for item in iter_note_pages(store):
        page = item["page"]
        if page.get("note_id") in note_id_set and session_id not in page["linked_study_session_ids"]:
            page["linked_study_session_ids"].append(session_id)
            page["updated_at"] = now_text()
            changed = True
    if changed:
        save_note_store(root, store)


@json_store.json_transaction_for(lambda root, note_ids, item_id, note_link_key: notes_path(root))
def link_notes_to_item(root: Path, note_ids: list[str], item_id: str, note_link_key: str) -> None:
    note_id_set = {str(note_id) for note_id in ensure_list(note_ids) if note_id}
    if not note_id_set or not item_id:
        return
    store = load_note_store(root)
    changed = False
    for item in iter_note_pages(store):
        page = item["page"]
        if page.get("note_id") not in note_id_set:
            continue
        values = ensure_list(page.get(note_link_key))
        if item_id not in values:
            values.append(item_id)
            page[note_link_key] = values
            page["updated_at"] = now_text()
            changed = True
    if changed:
        save_note_store(root, store)


@json_store.json_transaction_for(lambda root, session, title, tags, body: notes_path(root))
def create_note_from_session(root: Path, session: dict[str, Any], title: str, tags: list[str], body: str) -> str:
    from app_core.note_editor import reserved_note_ids

    store = load_note_store(root)
    first_notebook = store["notebooks"][0]
    first_section = first_notebook["sections"][0]
    existing_ids = collect_note_store_ids(store)["note"] | reserved_note_ids(root)
    note_id = generate_id("note", session.get("start_at"), existing_ids)
    timestamp = now_text()
    page = {
        "id": note_id,
        "note_id": note_id,
        "title": title.strip() or "無題のページ",
        "body": body,
        "content": body,
        "tags": [tag for tag in tags if tag],
        "created_at": timestamp,
        "updated_at": timestamp,
        "linked_study_session_ids": [session["session_id"]],
        "linked_flashcard_ids": [],
        "linked_reminder_ids": [],
        **{key: session.get(key, "") for key in MASTER_KEYS},
    }
    first_section["pages"].append(page)
    save_note_store(root, store)
    return note_id


def normalize_study_sessions(path: Path, sessions: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], bool]:
    from app_core.study_record_reader import unpack_study_document

    raw = load_json_safe(path, []) if sessions is None else sessions
    document = unpack_study_document(raw)
    raw = [item for _, item in document.records]
    existing_ids = {str(item.get("session_id")) for item in raw if isinstance(item, dict) and item.get("session_id")}
    # Unrecognized values remain in their original container on write-back.
    changed = False
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            changed = True
            continue
        if not item.get("session_id"):
            date_value = item.get("start_at") or path.stem
            item["session_id"] = generate_id("session", date_value, existing_ids)
            changed = True
        linked_note_ids = ensure_list(item.get("linked_note_ids"))
        if item.get("linked_note_ids") != linked_note_ids:
            item["linked_note_ids"] = linked_note_ids
            changed = True
        if "linked_task_id" not in item:
            item["linked_task_id"] = ""
            changed = True
        for key in MASTER_KEYS:
            if key not in item:
                item[key] = ""
                changed = True
        normalized.append(item)
    return normalized, changed


def load_study_sessions_file(path: Path, *, write_back: bool = False, backup: bool = True) -> list[dict[str, Any]]:
    sessions, changed = normalize_study_sessions(path)
    if write_back and changed:
        if backup:
            backup_json(path)
        save_study_sessions_file(path, sessions)
    return sessions


def _rebuild_study_document(original: Any, sessions: list[dict[str, Any]]) -> Any:
    """Keep legacy envelopes and unrecognized list values on record updates."""
    from copy import deepcopy
    from app_core.study_record_reader import unpack_study_document

    document = unpack_study_document(original)
    if document.container_kind == "unsupported" or (
        document.container_kind == "sessions" and not isinstance(original.get("sessions"), list)
    ):
        raise json_store.JsonReadError("勉強ログの保存形式を確認できません。元ファイルを保持して保存を中止しました。")
    if document.container_kind == "record":
        return deepcopy(sessions[0]) if len(sessions) == 1 else deepcopy(sessions)
    original_items = original["sessions"] if document.container_kind == "sessions" else original
    replacements = iter(deepcopy(sessions))
    rebuilt = []
    for item in original_items:
        if isinstance(item, dict):
            replacement = next(replacements, None)
            if replacement is not None:
                rebuilt.append(replacement)
        else:
            rebuilt.append(deepcopy(item))
    rebuilt.extend(replacements)
    if document.container_kind == "sessions":
        return {**deepcopy(document.metadata), "sessions": rebuilt}
    return rebuilt


def save_study_sessions_file(path: Path, sessions: list[dict[str, Any]]) -> None:
    # Do not silently accept a newer revision merely because preserving its
    # container requires an internal read before writing.
    expected_revision = json_store.observed_json_revision(path)
    if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
        raise json_store.JsonReadError("保存する勉強記録の形式が不正です。元ファイルは変更していません。")
    normalized, _ = normalize_study_sessions(path, sessions)
    with json_store.json_transaction(path):
        original = load_json_safe(path, [])
        rebuilt = _rebuild_study_document(original, normalized)
        json_store.save_json_safe(path, rebuilt, expected_revision=expected_revision)


def load_all_study_sessions(root: Path) -> list[dict[str, Any]]:
    from app_core.study_record_reader import unpack_study_document

    study_dir = root / "study_logs" if root else STUDY_LOGS_DIR
    rows: list[dict[str, Any]] = []
    for path in json_store.record_json_files(study_dir):
        for _, row in unpack_study_document(load_json_safe(path, [])).records:
            row["source_file"] = str(path)
            rows.append(row)
    return rows


def daily_plan_path(root: Path, target: date | datetime | str | None = None) -> Path:
    token = _date_token(target)
    base = root / "daily_plans" if root else DAILY_PLANS_DIR
    return base / token[:4] / token[4:6] / f"{token}.json"


def default_daily_plan(target: date | datetime | str | None = None) -> dict[str, Any]:
    parsed = parse_datetime(target) if isinstance(target, str) else None
    if isinstance(target, datetime):
        plan_date = target.date()
    elif isinstance(target, date):
        plan_date = target
    elif parsed:
        plan_date = parsed.date()
    else:
        plan_date = app_today()
    timestamp = now_text()
    return {
        "plan_id": f"plan_{plan_date:%Y%m%d}",
        "date": plan_date.isoformat(),
        "target_total_minutes": 0,
        "focus_subjects": [],
        "materials": [],
        "tasks": [],
        "review_note_ids": [],
        "review_card_ids": [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def normalize_daily_plan(plan: dict[str, Any] | None, target: date | datetime | str | None = None) -> tuple[dict[str, Any], bool]:
    changed = False
    default_plan = default_daily_plan(target)
    if not isinstance(plan, dict):
        return default_plan, True

    if not plan.get("date"):
        plan["date"] = default_plan["date"]
        changed = True
    if not plan.get("plan_id"):
        plan["plan_id"] = f"plan_{_date_token(plan.get('date'))}"
        changed = True
    if "target_total_minutes" not in plan:
        plan["target_total_minutes"] = 0
        changed = True
    for key in ("focus_subjects", "materials", "tasks", "review_note_ids", "review_card_ids"):
        normalized = ensure_list(plan.get(key))
        if key == "tasks":
            normalized = [item for item in normalized if isinstance(item, dict)]
        if plan.get(key) != normalized:
            plan[key] = normalized
            changed = True
    if not plan.get("created_at"):
        plan["created_at"] = now_text()
        changed = True
    if not plan.get("updated_at"):
        plan["updated_at"] = plan["created_at"]
        changed = True

    existing_task_ids = {str(task.get("task_id")) for task in plan["tasks"] if task.get("task_id")}
    for task in plan["tasks"]:
        if not task.get("task_id"):
            task["task_id"] = generate_id("task", plan.get("date"), existing_task_ids)
            changed = True
        task.setdefault("title", "無題のタスク")
        task.setdefault("subject", "")
        task.setdefault("material", "")
        if "planned_minutes" not in task:
            task["planned_minutes"] = 0
            changed = True
        if task.get("status") == "paused":
            task["status"] = "pending"
            changed = True
        if task.get("status") not in ("not_started", "in_progress", "done", "pending"):
            task["status"] = "not_started"
            changed = True
        if "carried_from_task_id" not in task:
            task["carried_from_task_id"] = ""
            changed = True
        for key in MASTER_KEYS:
            if key not in task:
                task[key] = ""
                changed = True
        for key in ("linked_note_ids", "linked_session_ids"):
            normalized = ensure_list(task.get(key))
            if task.get(key) != normalized:
                task[key] = normalized
                changed = True
    return plan, changed


def load_daily_plan(root: Path, target: date | datetime | str | None = None, *, write_back: bool = False) -> dict[str, Any]:
    path = daily_plan_path(root, target)
    plan = load_json_safe(path, default_daily_plan(target))
    plan, changed = normalize_daily_plan(plan, target)
    if write_back and changed:
        backup_json(path)
        save_json_safe(path, plan)
    return plan


def save_daily_plan(root: Path, plan: dict[str, Any]) -> None:
    normalized, _ = normalize_daily_plan(plan, plan.get("date"))
    normalized["updated_at"] = now_text()
    save_json_safe(daily_plan_path(root, normalized.get("date")), normalized)


def load_today_tasks(root: Path) -> list[dict[str, Any]]:
    plan = load_daily_plan(root, app_today())
    return plan.get("tasks", [])


def _task_plan_date(target: date | datetime | str | None) -> date:
    if target is None:
        return app_today()
    if isinstance(target, datetime):
        return target.date()
    if isinstance(target, date):
        return target
    try:
        return date.fromisoformat(target)
    except (TypeError, ValueError) as exc:
        raise json_store.JsonStoreError("選択タスクの所属日が不正です。タスクを選び直してください。") from exc


@json_store.json_transaction_for(lambda root, task_id, session_id, target_date=None, require_existing=False: daily_plan_path(root, _task_plan_date(target_date)))
def link_session_to_plan_task(root: Path, task_id: str | None, session_id: str | None, *, target_date: date | datetime | str | None = None, require_existing: bool = False) -> None:
    if not task_id or not session_id:
        return
    owned_day = _task_plan_date(target_date)
    plan = load_daily_plan(root, owned_day)
    # save_daily_plan routes by the document's date. A malformed owner file
    # must never redirect this link update into another day's plan.
    if plan.get("date") != owned_day.isoformat():
        raise json_store.JsonConflictError("選択した日の計画と保存ファイルの日付が一致しません。別の日を上書きしないため保存を中止しました。計画の整合性を確認してください。")
    matches = [task for task in plan.get("tasks", []) if task.get("task_id") == task_id]
    if require_existing and (len(matches) != 1 or matches[0].get("is_active") is False):
        raise json_store.JsonConflictError("選択した日のタスクが削除・無効化されています。入力は保持しています。タスクを選び直すか、選択を解除してください。")
    changed = False
    for task in matches:
        linked_session_ids = ensure_list(task.get("linked_session_ids"))
        if session_id not in linked_session_ids:
            linked_session_ids.append(session_id)
            task["linked_session_ids"] = linked_session_ids
            changed = True
        if task.get("status") == "not_started":
            task["status"] = "in_progress"
            changed = True
    if changed:
        save_daily_plan(root, plan)


def load_all_daily_plans(root: Path, *, write_back: bool = False) -> list[dict[str, Any]]:
    plan_dir = root / "daily_plans" if root else DAILY_PLANS_DIR
    plans = []
    for path in json_store.record_json_files(plan_dir):
        plan = load_json_safe(path, {})
        plan, changed = normalize_daily_plan(plan, path.stem)
        if write_back and changed:
            backup_json(path)
            save_json_safe(path, plan)
        plan["source_file"] = str(path)
        plans.append(plan)
    return plans


def load_task_lookup(root: Path) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for plan in load_all_daily_plans(root):
        for task in plan.get("tasks", []):
            if task.get("task_id"):
                item = dict(task)
                item["plan_id"] = plan.get("plan_id", "")
                item["date"] = plan.get("date", "")
                tasks[task["task_id"]] = item
    return tasks


def resolve_task_title(root: Path, task_id: str | None) -> str:
    if not task_id:
        return ""
    task = load_task_lookup(root).get(task_id)
    return task.get("title", "不明なタスク") if task else "不明なタスク"


def flashcards_path(root: Path) -> Path:
    return root / "flashcards.json" if root else FLASHCARDS_PATH


def normalize_flashcards(cards: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(cards, list):
        return [], True
    changed = False
    existing_ids = {
        str(card.get("card_id") or card.get("flashcard_id"))
        for card in cards
        if isinstance(card, dict) and (card.get("card_id") or card.get("flashcard_id"))
    }
    normalized = []
    for card in cards:
        if not isinstance(card, dict):
            changed = True
            continue
        card_id = card.get("card_id") or card.get("flashcard_id")
        if not card_id:
            card_id = generate_id("card", existing_ids=existing_ids)
            changed = True
        if card.get("card_id") != card_id:
            card["card_id"] = card_id
            changed = True
        if card.get("flashcard_id") != card_id:
            card["flashcard_id"] = card_id
            changed = True
        if "question" not in card:
            card["question"] = card.get("front", "")
            changed = True
        if "answer" not in card:
            card["answer"] = card.get("back", "")
            changed = True
        if "front" not in card:
            card["front"] = card.get("question", "")
            changed = True
        if "back" not in card:
            card["back"] = card.get("answer", "")
            changed = True
        card.setdefault("explanation", "")
        card.setdefault("importance", "normal")
        if "need_review" not in card:
            card["need_review"] = bool(card.get("important", False))
            changed = True
        if "important" not in card:
            card["important"] = bool(card.get("need_review", False))
            changed = True
        card["tags"] = ensure_list(card.get("tags"))
        card["linked_note_ids"] = ensure_list(card.get("linked_note_ids"))
        if card.get("source_note_id") and card["source_note_id"] not in card["linked_note_ids"]:
            card["linked_note_ids"].append(card["source_note_id"])
            changed = True
        elif card["linked_note_ids"] and not card.get("source_note_id"):
            card["source_note_id"] = card["linked_note_ids"][0]
            changed = True
        else:
            card.setdefault("source_note_id", "")
        for key in MASTER_KEYS:
            card.setdefault(key, "")
        if not card.get("created_at"):
            card["created_at"] = now_text()
            changed = True
        if not card.get("updated_at"):
            card["updated_at"] = card["created_at"]
            changed = True
        normalized.append(card)
    return normalized, changed


def load_all_flashcards(root: Path, *, write_back: bool = False) -> list[dict[str, Any]]:
    path = flashcards_path(root)
    cards, changed = normalize_flashcards(load_json_safe(path, []))
    if write_back and changed:
        backup_json(path)
        save_json_safe(path, cards)
    return cards


def save_flashcards(root: Path, cards: list[dict[str, Any]]) -> None:
    normalized, _ = normalize_flashcards(cards)
    save_json_safe(flashcards_path(root), normalized)


def reminders_path(root: Path) -> Path:
    return root / "reminders.json" if root else REMINDERS_PATH


def normalize_reminders(items: Any, *, today: date | None = None) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(items, list):
        return [], True
    changed = False
    existing_ids = {str(item.get("reminder_id")) for item in items if isinstance(item, dict) and item.get("reminder_id")}
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            changed = True
            continue
        if not item.get("reminder_id"):
            item["reminder_id"] = generate_id("reminder", item.get("registered") or item.get("scheduled_date"), existing_ids)
            changed = True
        item.setdefault("source_type", "")
        item.setdefault("source_note_id", "")
        item.setdefault("source_card_id", "")
        item["linked_note_ids"] = ensure_list(item.get("linked_note_ids"))
        item["linked_card_ids"] = ensure_list(item.get("linked_card_ids"))
        if item.get("source_note_id") and item["source_note_id"] not in item["linked_note_ids"]:
            item["linked_note_ids"].append(item["source_note_id"])
            changed = True
        elif item["linked_note_ids"] and not item.get("source_note_id"):
            item["source_note_id"] = item["linked_note_ids"][0]
            changed = True
        item.setdefault("title", item.get("keyword", ""))
        item.setdefault("memo", "")
        item.setdefault("review_type", "general")
        item.setdefault("priority", "normal")
        if not item.get("scheduled_date"):
            if today is None and not item.get("registered"):
                today = app_today()
            item["scheduled_date"] = item.get("registered") or today.isoformat()
            changed = True
        item.setdefault("status", "pending")
        item.setdefault("keyword", item.get("title", ""))
        item.setdefault("url", "")
        if "registered" not in item:
            item["registered"] = item["scheduled_date"]
        item.setdefault("schedule_mode", "single")
        item.setdefault("algorithm", "single" if item.get("schedule_mode") == "single" else "fixed")
        item.setdefault("review_history", [])
        item.setdefault("review_count", len(item.get("review_history", [])) if isinstance(item.get("review_history"), list) else 0)
        item.setdefault("schedule_status", "active")
        for key in MASTER_KEYS:
            item.setdefault(key, "")
        if not item.get("created_at"):
            item["created_at"] = now_text()
            changed = True
        if not item.get("updated_at"):
            item["updated_at"] = item["created_at"]
            changed = True
        normalized.append(item)
    return normalized, changed


def load_all_reminders(root: Path, *, write_back: bool = False, today: date | None = None) -> list[dict[str, Any]]:
    path = reminders_path(root)
    items, changed = normalize_reminders(load_json_safe(path, []), today=today)
    if write_back and changed:
        backup_json(path)
        save_json_safe(path, items)
    return items


def save_reminders(root: Path, items: list[dict[str, Any]]) -> None:
    normalized, _ = normalize_reminders(items)
    save_json_safe(reminders_path(root), normalized)


def resolve_note_title(root: Path, note_id: str | None) -> str:
    if not note_id:
        return "未設定"
    for note in load_all_notes(root):
        if note.get("note_id") == note_id:
            return note.get("title", "無題のページ")
    return "不明なノート"


def resolve_card_title(root: Path, card_id: str | None) -> str:
    if not card_id:
        return "未設定"
    for card in load_all_flashcards(root):
        if card.get("card_id") == card_id or card.get("flashcard_id") == card_id:
            return card.get("question") or card.get("front") or "無題のカード"
    return "不明なカード"


def resolve_reminder_title(root: Path, reminder_id: str | None) -> str:
    if not reminder_id:
        return "未設定"
    for reminder in load_all_reminders(root):
        if reminder.get("reminder_id") == reminder_id:
            return reminder.get("title") or reminder.get("keyword") or "無題のリマインダー"
    return "不明なリマインダー"
