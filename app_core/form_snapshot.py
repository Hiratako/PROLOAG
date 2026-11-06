"""Form baselines survive Streamlit reruns until an explicit save or discard."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, MutableMapping, Iterable
from uuid import uuid4

from app_core.edit_snapshot import capture_edit_snapshot, clear_edit_snapshot
from app_core.json_store import resolved_json_path


def form_snapshot(state: MutableMapping[str, Any], key: str, paths: Iterable[Path], data: Any) -> dict[str, Any]:
    revisions = capture_edit_snapshot(state, key, paths)
    storage_key = f"form_baseline:{key}"
    if storage_key not in state:
        state[storage_key] = {
            "data": deepcopy(data),
            "operation_id": uuid4().hex,
        }
    baseline = state[storage_key]
    return {
        "data": deepcopy(baseline["data"]),
        "revisions": dict(revisions),
        "operation_id": baseline["operation_id"],
        "version": int(state.get(f"form_version:{key}", 0)),
    }


def discard_form_snapshot(state: MutableMapping[str, Any], key: str) -> None:
    clear_edit_snapshot(state, key)
    state.pop(f"form_baseline:{key}", None)
    version_key = f"form_version:{key}"
    state[version_key] = int(state.get(version_key, 0)) + 1


def advance_form_snapshots_after_own_patch(
    state: MutableMapping[str, Any], path: Path, *,
    before_revision: str | None, after_revision: str | None, updates: dict[str, Any],
) -> None:
    """Advance sibling forms only across an exact, successfully saved own patch.

    No disk reload is used to authorize an old form. A baseline predating any
    intervening external update cannot match before_revision and stays stale.
    The version is unchanged, preserving unrelated in-progress widget values.
    """
    path = resolved_json_path(path)

    def merge(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(current)
        for name, value in patch.items():
            result[name] = merge(result[name], value) if isinstance(value, dict) and isinstance(result.get(name), dict) else deepcopy(value)
        return result

    for storage_key in list(state):
        if not str(storage_key).startswith("form_baseline:"):
            continue
        key = str(storage_key)[len("form_baseline:"):]
        revisions = state.get(key)
        baseline = state[storage_key]
        if (not isinstance(revisions, dict) or path not in revisions
                or revisions[path] != before_revision or not isinstance(baseline.get("data"), dict)):
            continue
        state[storage_key] = {**baseline, "data": merge(baseline["data"], updates)}
        state[key] = {**revisions, path: after_revision}
