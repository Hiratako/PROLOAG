"""Pure, read-only data preparation for the three learning visualizations.

The saved duration is authoritative, including manually added sessions. The
service never reads files, infers mastery, or invents links between contents.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from hashlib import sha256
import heapq
import json
import math
from typing import Any, TYPE_CHECKING

from app_core.analysis_dates import parse_analysis_date, parse_analysis_datetime

if TYPE_CHECKING:
    from app_core.master_snapshot import MasterSnapshot


_CLASSIFICATION_KEYS = ("project_id", "subject_id", "material_id", "unit_id")
_HISTORY_LIMIT = 30
_NOTE_LIMIT = 5


def _identity(value: Any) -> str:
    """Keep saved IDs distinct, with one consistent identity for unset fields."""
    if value is None or value == "" or value is False or value == 0:
        return ""
    # Normally IDs are strings. The type marker also separates legacy numeric
    # IDs from strings with the same spelling without relying on display names.
    return json.dumps([type(value).__name__, value], ensure_ascii=True, sort_keys=True, default=str)


def _stable_id(prefix: str, identity: tuple[str, ...]) -> str:
    encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    return f"{prefix}_{sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def _text(value: Any, limit: int | None = None) -> str:
    text = value if isinstance(value, str) else "" if value is None else str(value)
    return text[:limit] if limit is not None else text


def _duration(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _timestamp(value: Any) -> str:
    parsed = parse_analysis_datetime(value)
    return parsed.isoformat(timespec="seconds") if parsed is not None else ""


def _linked_ids(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        values = [value]
    else:
        values = []
    return {_identity(item) for item in values if _identity(item)}


def _matches_filters(record: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    return all(not filters.get(key) or record.get(key) == filters[key] for key in _CLASSIFICATION_KEYS)


def _note_identity(note: Mapping[str, Any]) -> str:
    identity = _identity(note.get("note_id") or note.get("id") or note.get("notebook_page_id"))
    if identity:
        return identity
    encoded = json.dumps(dict(note), ensure_ascii=True, sort_keys=True, default=str)
    return f"anonymous:{sha256(encoded.encode('utf-8')).hexdigest()}"


def _attach_notes(
    notes: Iterable[Mapping[str, Any]],
    nodes: dict[str, dict[str, Any]],
    classifications: dict[str, tuple[str, ...]],
    session_nodes: dict[str, set[str]],
    note_nodes: dict[str, set[str]],
) -> None:
    """Use saved links, or the note's saved classification when it has no links.

    Broad classification notes may appear in several matching units. Explicit
    links take precedence so a linked note is not spread across unrelated units
    merely because an older copy of its classification was broad or stale.
    """
    previews: dict[str, list[tuple[datetime, str, dict[str, str]]]] = defaultdict(list)
    classification_index: dict[tuple[int, str], set[str]] = defaultdict(set)
    for node_id, classification in classifications.items():
        for index, identity in enumerate(classification):
            classification_index[index, identity].add(node_id)

    # Deduplicate before matching. Only five previews per node are retained,
    # even when one broad classification note matches many units.
    unique_notes: dict[str, tuple[datetime, Mapping[str, Any]]] = {}
    for note in notes:
        if not isinstance(note, Mapping):
            continue
        identity = _note_identity(note)
        timestamp = parse_analysis_datetime(note.get("updated_at") or note.get("created_at")) or datetime.min
        previous = unique_notes.get(identity)
        if previous is None or timestamp >= previous[0]:
            unique_notes[identity] = (timestamp, note)

    for identity, (timestamp, note) in unique_notes.items():
        aliases = set()
        for key in ("note_id", "id", "notebook_page_id"):
            aliases.update(_linked_ids(note.get(key)))
        linked_sessions = _linked_ids(note.get("linked_study_session_ids"))
        linked_sessions.update(_linked_ids(note.get("linked_session_ids")))
        targets: set[str] = set()
        for session_id in linked_sessions:
            targets.update(session_nodes.get(session_id, ()))
        for note_id in aliases:
            targets.update(note_nodes.get(note_id, ()))
        has_explicit_links = bool(linked_sessions or any(note_id in note_nodes for note_id in aliases))
        if not has_explicit_links:
            selections = [
                classification_index.get((index, _identity(note.get(key))), set())
                for index, key in enumerate(_CLASSIFICATION_KEYS)
                if _identity(note.get(key))
            ]
            if selections:
                targets = set.intersection(*(set(selection) for selection in selections))
        if not targets:
            continue
        detail = {
            "title": _text(note.get("title") or "無題のページ", 200),
            "excerpt": _text(note.get("body") or note.get("content") or "", 800),
        }
        for node_id in targets:
            if node_id in nodes:
                nodes[node_id]["note_count"] += 1
                heap = previews[node_id]
                heapq.heappush(heap, (timestamp, identity, detail))
                if len(heap) > _NOTE_LIMIT:
                    heapq.heappop(heap)

    for node_id, entries in previews.items():
        nodes[node_id]["notes"] = [dict(detail) for _, _, detail in sorted(entries, reverse=True)]


def build_learning_visual_payload(
    sessions: Iterable[Mapping[str, Any]],
    master: MasterSnapshot,
    start_date: date | datetime | str,
    end_date: date | datetime | str,
    filters: Mapping[str, Any] | None = None,
    *,
    notes: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return JSON-safe totals, daily series and bounded details for all units.

    Every valid zero-duration session is retained. Negative, non-finite or
    nonnumeric durations are excluded and counted; timestamps follow the same
    local-date interpretation as other analysis pages. Exclusion counts apply
    after classification filters, and duration checks after the date range.
    An invalid start timestamp is not replaced with an unrelated end timestamp:
    the established ``start_at or end_at`` analysis rule is retained.

    All classification nodes are returned, with no top-N reduction. History and
    note previews alone are bounded, with their full counts kept separately.
    The supplied sessions, master, filters and notes are never modified.
    """
    start = parse_analysis_date(start_date)
    end = parse_analysis_date(end_date)
    if start is None or end is None:
        raise ValueError("学習ビジュアルの開始日・終了日は有効な日付で指定してください。")
    if end < start:
        start, end = end, start
    days = [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]
    selected_filters = filters or {}
    subjects: dict[str, dict[str, Any]] = {}
    materials: dict[str, dict[str, Any]] = {}
    nodes: dict[str, dict[str, Any]] = {}
    classifications: dict[str, tuple[str, ...]] = {}
    session_nodes: dict[str, set[str]] = defaultdict(set)
    note_nodes: dict[str, set[str]] = defaultdict(set)
    histories: dict[str, list[tuple[tuple[Any, ...], int, dict[str, Any]]]] = defaultdict(list)
    excluded = {"invalid_date": 0, "invalid_duration": 0}
    total_sec = 0.0
    total_sessions = 0

    for session in sessions:
        if not isinstance(session, Mapping):
            excluded["invalid_date"] += 1
            continue
        if not _matches_filters(session, selected_filters):
            continue
        day = parse_analysis_date(session.get("start_at") or session.get("end_at"))
        if day is None:
            excluded["invalid_date"] += 1
            continue
        if not start <= day <= end:
            continue
        seconds = _duration(session.get("duration_sec"))
        # Extremely large finite values must not create Infinity in JSON totals.
        if seconds is None or not math.isfinite(total_sec + seconds):
            excluded["invalid_duration"] += 1
            continue
        classification = tuple(_identity(session.get(key)) for key in _CLASSIFICATION_KEYS)
        subject_id = _stable_id("subject", classification[:2])
        material_id = _stable_id("material", classification[:3])
        node_id = _stable_id("unit", classification)
        if node_id not in nodes:
            names = master.names(dict(session))
            project_name = _text(names.get("project_name") or "プロジェクト未設定")
            subject_name = _text(names.get("subject_name") or "科目未設定")
            material_name = _text(names.get("material_name") or "教材未設定")
            unit_name = _text(names.get("unit_name") or "単元未設定")
            for index, replacement in enumerate(("プロジェクト未設定", "科目未設定", "教材未設定", "単元未設定")):
                if not classification[index]:
                    if index == 0:
                        project_name = replacement
                    elif index == 1:
                        subject_name = replacement
                    elif index == 2:
                        material_name = replacement
                    else:
                        unit_name = replacement
            subjects.setdefault(subject_id, {"id": subject_id, "name": subject_name, "project_name": project_name})
            materials.setdefault(material_id, {"id": material_id, "group_id": subject_id, "name": material_name})
            nodes[node_id] = {
                "id": node_id,
                "group_id": subject_id,
                "material_id": material_id,
                "label": unit_name,
                "project_name": project_name,
                "subject_name": subject_name,
                "material_name": material_name,
                "total_sec": 0.0,
                "daily": [0.0] * len(days),
                "session_count": 0,
                "last_day": day.isoformat(),
                "history": [],
                "history_total": 0,
                "note_count": 0,
                "notes": [],
            }
            classifications[node_id] = classification
        node = nodes[node_id]
        total_sec += seconds
        total_sessions += 1
        node["total_sec"] += seconds
        node["daily"][(day - start).days] += seconds
        node["session_count"] += 1
        node["history_total"] += 1
        node["last_day"] = max(node["last_day"], day.isoformat())
        history = {
            "day": day.isoformat(),
            "start_at": _timestamp(session.get("start_at")),
            "end_at": _timestamp(session.get("end_at")),
            "duration_sec": seconds,
            "memo": _text(session.get("memo"), 1000),
        }
        history_rank = (history["day"], history["start_at"], history["end_at"], history["memo"], seconds)
        history_heap = histories[node_id]
        heapq.heappush(history_heap, (history_rank, total_sessions, history))
        if len(history_heap) > _HISTORY_LIMIT:
            heapq.heappop(history_heap)
        session_id = _identity(session.get("session_id") or session.get("id"))
        if session_id:
            session_nodes[session_id].add(node_id)
        linked_notes = _linked_ids(session.get("linked_note_ids"))
        for key in ("note_id", "notebook_page_id"):
            linked_notes.update(_linked_ids(session.get(key)))
        for note_id in linked_notes:
            note_nodes[note_id].add(node_id)

    for node_id, entries in histories.items():
        nodes[node_id]["history"] = [history for _, _, history in sorted(entries, reverse=True)]
    _attach_notes(notes if notes is not None else (), nodes, classifications, session_nodes, note_nodes)
    return {
        "version": 1,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
        "total_sec": total_sec,
        "total_sessions": total_sessions,
        "subjects": [subjects[key] for key in sorted(subjects)],
        "materials": [materials[key] for key in sorted(materials)],
        "nodes": [nodes[key] for key in sorted(nodes)],
        "excluded": excluded,
    }
