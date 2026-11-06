"""Display references and review drafts for the reminder page only."""
from __future__ import annotations

from typing import Any, MutableMapping


class ReminderSourceTitles:
    """Reuse already-loaded records, preserving first-match resolver behavior."""

    def __init__(self, notes: list[dict[str, Any]], cards: list[dict[str, Any]]):
        self.notes: dict[Any, Any] = {}
        self.cards: dict[Any, Any] = {}
        for note in notes:
            self.notes.setdefault(note.get("note_id"), note.get("title", "無題のページ"))
        for card in cards:
            title = card.get("question") or card.get("front") or "無題のカード"
            for id_key in ("card_id", "flashcard_id"):
                self.cards.setdefault(card.get(id_key), title)

    def note_title(self, note_id: str) -> str:
        return self.notes.get(note_id, "不明なノート") if note_id else "未設定"

    def card_title(self, card_id: str) -> str:
        return self.cards.get(card_id, "不明なカード") if card_id else "未設定"


def review_draft_keys(reminder_id: str) -> tuple[str, str]:
    return f"p08_review_memo_{reminder_id}", f"p08_review_duration_{reminder_id}"


def capture_review_draft(state: MutableMapping[str, Any], reminder_id: str) -> None:
    """Copy widgets into non-widget state before Streamlit hides/cleans them."""
    memo_key, duration_key = review_draft_keys(reminder_id)
    drafts = dict(state.get("p08_review_drafts", {}))
    previous = drafts.get(reminder_id, {"memo": "", "duration": 0})
    drafts[reminder_id] = {
        "memo": state.get(memo_key, previous["memo"]),
        "duration": state.get(duration_key, previous["duration"]),
    }
    state["p08_review_drafts"] = drafts


def restore_review_draft(state: MutableMapping[str, Any], reminder_id: str) -> tuple[str, str]:
    """Restore only missing widget keys; keep newly received browser values."""
    memo_key, duration_key = review_draft_keys(reminder_id)
    draft = state.get("p08_review_drafts", {}).get(reminder_id, {"memo": "", "duration": 0})
    if memo_key not in state:
        state[memo_key] = draft["memo"]
    if duration_key not in state:
        state[duration_key] = draft["duration"]
    return memo_key, duration_key


def discard_review_draft(state: MutableMapping[str, Any], reminder_id: str) -> None:
    """Discard only a successfully completed/skipped occurrence's inputs."""
    drafts = dict(state.get("p08_review_drafts", {}))
    drafts.pop(reminder_id, None)
    state["p08_review_drafts"] = drafts
    for key in review_draft_keys(reminder_id):
        state.pop(key, None)
