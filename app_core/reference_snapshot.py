"""Lazy display-only references shared within one page execution.

Create a new instance on every rerun. These indexes must not be stored in
session state or used as the authority for a write or conflict check.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from data_utils import ensure_list, load_all_notes, load_all_study_sessions, load_task_lookup


class ReferenceSnapshot:
    def __init__(self, root: Path):
        self.root = root
        self._note_titles: dict[Any, list[tuple[int, Any]]] | None = None
        self._tasks: dict[str, dict[str, Any]] | None = None
        self._sessions_by_id: dict[Any, dict[str, Any]] | None = None
        self._session_groups: dict[Any, list[dict[str, Any]]] | None = None

    def note_titles(self, note_ids: Any) -> str:
        """Match resolve_note_titles, including duplicates and stored note order."""
        wanted = {str(note_id) for note_id in ensure_list(note_ids) if note_id}
        if not wanted:
            return ""
        if self._note_titles is None:
            note_titles = {}
            for index, note in enumerate(load_all_notes(self.root)):
                note_titles.setdefault(note["note_id"], []).append((index, note["title"]))
            self._note_titles = note_titles
        matched = [item for note_id in wanted for item in self._note_titles.get(note_id, [])]
        titles = [title for _, title in sorted(matched, key=lambda item: item[0])]
        missing_count = len(wanted - self._note_titles.keys())
        return " / ".join([*titles, *(["不明なノート"] * missing_count)])

    @property
    def tasks(self) -> dict[str, dict[str, Any]]:
        if self._tasks is None:
            # The existing loader defines last-record-wins for duplicate IDs.
            self._tasks = load_task_lookup(self.root)
        return self._tasks

    def task_title(self, task_id: str | None) -> str:
        if not task_id:
            return ""
        task = self.tasks.get(task_id)
        return task.get("title", "不明なタスク") if task else "不明なタスク"

    def _load_sessions(self) -> None:
        if self._sessions_by_id is not None:
            return
        sessions_by_id = {}
        session_groups = {}
        for session in load_all_study_sessions(self.root):
            session_id = session.get("session_id")
            sessions_by_id[session_id] = session
            session_groups.setdefault(session_id, []).append(session)
        self._sessions_by_id = sessions_by_id
        self._session_groups = session_groups

    @property
    def sessions_by_id(self) -> dict[Any, dict[str, Any]]:
        self._load_sessions()
        return self._sessions_by_id

    def sessions_for_id(self, session_id: Any) -> list[dict[str, Any]]:
        self._load_sessions()
        # Unlike the session table, related tasks have historically retained
        # every matching session row, including duplicate session IDs.
        return self._session_groups.get(session_id, [])
