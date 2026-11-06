"""Idempotent manual entry submissions; explicit reset permits intentional repeats."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, MutableMapping
from uuid import uuid4

from app_core.json_operations import atomic_json_operation
from app_core.study_log_service import append_study_session, get_study_log_path


MANUAL_SUBMISSION_KEY = "manual_study_submission"
_DERIVED = {"session_id", "money", "normal_money", "overtime_money", "created_at", "updated_at"}


def save_manual_entry(payload: dict[str, Any], state: MutableMapping[str, Any], root: Path) -> tuple[Path, bool]:
    identity = {key: value for key, value in payload.items() if key not in _DERIVED}
    fingerprint = sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    pending = state.get(MANUAL_SUBMISSION_KEY)
    if not isinstance(pending, dict) or pending.get("fingerprint") != fingerprint:
        pending = {"fingerprint": fingerprint, "operation_id": uuid4().hex}
        state[MANUAL_SUBMISSION_KEY] = pending
    record = deepcopy(payload)
    if pending.get("session_id"):
        record["session_id"] = pending["session_id"]
    saved = get_study_log_path(record["start_at"], root)
    with atomic_json_operation(root=root, kind="manual_study_append", operation_id=pending["operation_id"]) as operation:
        replayed = operation.already_committed
        if not replayed:
            try:
                saved = append_study_session(record, root)
            finally:
                if record.get("session_id"):
                    pending["session_id"] = record["session_id"]
    pending["completed"] = True
    return saved, replayed
