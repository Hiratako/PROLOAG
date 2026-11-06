from __future__ import annotations

from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from app_core.json_store import json_transaction_for


def _root(root: Path | None = None) -> Path:
    return root or PROJECT_ROOT


def load_flashcards(root: Path | None = None, *, write_back: bool = False) -> list[dict[str, Any]]:
    from data_utils import load_all_flashcards

    return load_all_flashcards(_root(root), write_back=write_back)


def save_flashcards(cards: list[dict[str, Any]], root: Path | None = None) -> None:
    from data_utils import save_flashcards as _save

    _save(_root(root), cards)


def ensure_card_ids(root: Path | None = None) -> list[dict[str, Any]]:
    return load_flashcards(root, write_back=True)


@json_transaction_for(lambda card, root=None: _root(root) / "flashcards.json")
def add_flashcard(card: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    from data_utils import generate_id, now_text

    cards = load_flashcards(root)
    existing_ids = {item.get("card_id", "") for item in cards}
    card = dict(card)
    card_id = card.get("card_id") or card.get("flashcard_id") or generate_id("card", existing_ids=existing_ids)
    timestamp = now_text()
    card.setdefault("card_id", card_id)
    card.setdefault("flashcard_id", card_id)
    card.setdefault("created_at", timestamp)
    card.setdefault("updated_at", timestamp)
    cards.append(card)
    save_flashcards(cards, root)
    return card


@json_transaction_for(lambda card_id, updates, root=None: _root(root) / "flashcards.json")
def update_flashcard(card_id: str, updates: dict[str, Any], root: Path | None = None) -> bool:
    from data_utils import now_text

    cards = load_flashcards(root)
    for card in cards:
        if card.get("card_id") == card_id or card.get("flashcard_id") == card_id:
            card.update(updates)
            card["updated_at"] = now_text()
            save_flashcards(cards, root)
            return True
    return False


def find_card_by_id(card_id: str, root: Path | None = None) -> dict[str, Any] | None:
    return next((card for card in load_flashcards(root) if card.get("card_id") == card_id or card.get("flashcard_id") == card_id), None)


def resolve_card_label(card_id: str | None, root: Path | None = None) -> str:
    from data_utils import resolve_card_title

    return resolve_card_title(_root(root), card_id)


def get_cards_by_note_id(note_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    return [
        card
        for card in load_flashcards(root)
        if card.get("source_note_id") == note_id or note_id in (card.get("linked_note_ids") or [])
    ]


def get_review_cards(root: Path | None = None) -> list[dict[str, Any]]:
    return [card for card in load_flashcards(root) if card.get("need_review") or card.get("important")]


def filter_cards(
    project_id: str | None = None,
    subject_id: str | None = None,
    material_id: str | None = None,
    unit_id: str | None = None,
    tags: list[str] | None = None,
    need_review: bool | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    cards = load_flashcards(root)
    filters = {"project_id": project_id, "subject_id": subject_id, "material_id": material_id, "unit_id": unit_id}
    for key, value in filters.items():
        if value:
            cards = [card for card in cards if card.get(key) == value]
    if tags:
        wanted = set(tags)
        cards = [card for card in cards if wanted.intersection(set(card.get("tags") or []))]
    if need_review is not None:
        cards = [card for card in cards if bool(card.get("need_review") or card.get("important")) == need_review]
    return cards
