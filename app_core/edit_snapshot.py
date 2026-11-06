"""Explicit edit baselines, independent of Streamlit's last-read observations."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, MutableMapping

from app_core.json_store import _UNSET, load_json_safe, observed_json_revision, resolved_json_path


def capture_edit_snapshot(state: MutableMapping[str, Any], key: str,
                          paths: Iterable[Path]) -> dict[Path, str | None]:
    """Call immediately after loading form data; retain until explicit refresh.

    A missing file is a meaningful None revision, not an absent baseline.
    Re-renders and failed submissions must not silently refresh this mapping.
    """
    if key not in state:
        revisions = {}
        for path in paths:
            path = resolved_json_path(path)
            revision = observed_json_revision(path)
            if revision is _UNSET:
                load_json_safe(path, None)
                revision = observed_json_revision(path)
            revisions[path] = revision
        state[key] = revisions
    return dict(state[key])


def clear_edit_snapshot(state: MutableMapping[str, Any], key: str) -> None:
    state.pop(key, None)
