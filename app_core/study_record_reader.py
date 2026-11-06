"""Pure, non-migrating expansion of current and legacy study documents.

The returned records are detached copies. This module never supplies IDs,
normalizes values, reads files, or treats envelope metadata as a session.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StudyRecordDocument:
    container_kind: str
    records: list[tuple[int, dict[str, Any]]]
    metadata: dict[str, Any]
    ignored_items: list[dict[str, Any]]


def unpack_study_document(data: Any) -> StudyRecordDocument:
    """Expand a list/envelope/single legacy record without inventing records.

    A standalone dictionary needs a study-specific field to be a record;
    ``{"sessions": ...}`` is always an envelope, even if malformed. Skipped
    values and outer metadata remain available to lossless CSV side tables.
    Original list positions are retained even when non-records are skipped.
    """
    metadata: dict[str, Any] = {}
    if isinstance(data, list):
        kind, items = "list", data
    elif isinstance(data, dict) and "sessions" in data:
        kind = "sessions"
        metadata = deepcopy({key: value for key, value in data.items() if key != "sessions"})
        if not isinstance(data["sessions"], list):
            return StudyRecordDocument(kind, [], metadata, [{"field": "sessions", "value": deepcopy(data["sessions"])}])
        items = data["sessions"]
    elif isinstance(data, dict) and any(key in data for key in ("session_id", "start_at", "end_at", "duration_sec")):
        return StudyRecordDocument("record", [(0, deepcopy(data))], {}, [])
    else:
        metadata = deepcopy(data) if isinstance(data, dict) else {}
        ignored = [] if isinstance(data, dict) else [{"value": deepcopy(data)}]
        return StudyRecordDocument("unsupported", [], metadata, ignored)

    records: list[tuple[int, dict[str, Any]]] = []
    ignored = []
    for index, item in enumerate(items):
        if isinstance(item, dict):
            records.append((index, deepcopy(item)))
        else:
            ignored.append({"index": index, "value": deepcopy(item)})
    return StudyRecordDocument(kind, records, metadata, ignored)
