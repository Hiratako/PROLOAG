from __future__ import annotations

from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from app_core.json_store import json_transaction_for


def _root(root: Path | None = None) -> Path:
    return root or PROJECT_ROOT


def load_notebooks(root: Path | None = None, *, write_back: bool = False) -> dict[str, Any]:
    from data_utils import load_note_store

    return load_note_store(_root(root), write_back=write_back)


def save_notebooks(data: dict[str, Any], root: Path | None = None) -> None:
    from data_utils import save_note_store

    save_note_store(_root(root), data)


def ensure_note_ids(root: Path | None = None) -> dict[str, Any]:
    return load_notebooks(root, write_back=True)


def load_all_notes(root: Path | None = None) -> list[dict[str, Any]]:
    from data_utils import load_all_notes as _load

    return _load(_root(root))


def find_note_by_id(note_id: str, root: Path | None = None) -> dict[str, Any] | None:
    from data_utils import iter_note_pages

    for item in iter_note_pages(load_notebooks(root)):
        page = item["page"]
        if page.get("note_id") == note_id or page.get("id") == note_id:
            return page
    return None


def resolve_note_title(note_id: str | None, root: Path | None = None) -> str:
    from data_utils import resolve_note_title as _resolve

    return _resolve(_root(root), note_id)


@json_transaction_for(lambda title, body="", root=None, **metadata: _root(root) / "notes_data" / "notebooks.json")
def add_note(title: str, body: str = "", root: Path | None = None, **metadata: Any) -> str:
    from data_utils import collect_note_store_ids, generate_id, now_text, save_note_store
    from app_core.note_editor import reserved_note_ids

    store = load_notebooks(root)
    notebook = store["notebooks"][0]
    section = notebook["sections"][0]
    existing_ids = collect_note_store_ids(store)["note"] | reserved_note_ids(_root(root))
    note_id = generate_id("note", existing_ids=existing_ids)
    timestamp = now_text()
    page = {
        "title": title or "無題のページ",
        "body": body,
        "content": body,
        "tags": metadata.pop("tags", []),
        "created_at": timestamp,
        "updated_at": timestamp,
        "linked_study_session_ids": [],
        "linked_flashcard_ids": [],
        "linked_reminder_ids": [],
        **metadata,
        "id": note_id,
        "note_id": note_id,
    }
    section.setdefault("pages", []).append(page)
    save_note_store(_root(root), store)
    return note_id


@json_transaction_for(lambda note_id, updates, root=None: _root(root) / "notes_data" / "notebooks.json")
def update_note(note_id: str, updates: dict[str, Any], root: Path | None = None) -> bool:
    from data_utils import iter_note_pages, now_text, save_note_store

    store = load_notebooks(root)
    for item in iter_note_pages(store):
        page = item["page"]
        if page.get("note_id") == note_id:
            page.update(updates)
            page["updated_at"] = now_text()
            save_note_store(_root(root), store)
            return True
    return False


@json_transaction_for(lambda note_id, item_id, key, root=None: _root(root) / "notes_data" / "notebooks.json")
def _attach(note_id: str, item_id: str, key: str, root: Path | None = None) -> bool:
    note = find_note_by_id(note_id, root)
    if not note:
        return False
    values = note.setdefault(key, [])
    if item_id not in values:
        values.append(item_id)
    return update_note(note_id, {key: values}, root)


def attach_session_to_note(note_id: str, session_id: str, root: Path | None = None) -> bool:
    return _attach(note_id, session_id, "linked_study_session_ids", root)


def attach_flashcard_to_note(note_id: str, card_id: str, root: Path | None = None) -> bool:
    return _attach(note_id, card_id, "linked_flashcard_ids", root)


def attach_reminder_to_note(note_id: str, reminder_id: str, root: Path | None = None) -> bool:
    return _attach(note_id, reminder_id, "linked_reminder_ids", root)


def get_related_sessions(note_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    from app_core.study_log_service import find_session_by_id

    note = find_note_by_id(note_id, root)
    if not note:
        return []
    return [session for sid in note.get("linked_study_session_ids", []) if (session := find_session_by_id(sid, root))]


def get_related_flashcards(note_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    from app_core.flashcard_service import get_cards_by_note_id

    return get_cards_by_note_id(note_id, root)


def get_related_reminders(note_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    from app_core.reminder_service import get_reminders_by_note_id

    return get_reminders_by_note_id(note_id, root)
