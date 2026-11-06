"""Save an owned settings patch without invalidating sibling form drafts."""
from __future__ import annotations

from typing import Any, MutableMapping

from app_core.form_snapshot import advance_form_snapshots_after_own_patch, discard_form_snapshot
from app_core.json_store import observed_json_revision
from app_core.settings_service import update_user_settings
from config import USER_SETTINGS_PATH


def save_settings_form_patch(
    state: MutableMapping[str, Any], key: str, edit: dict[str, Any], updates: dict[str, Any],
) -> None:
    before_revision = edit["revisions"][USER_SETTINGS_PATH]
    update_user_settings(updates, expected_revision=before_revision)
    after_revision = observed_json_revision(USER_SETTINGS_PATH)
    advance_form_snapshots_after_own_patch(
        state, USER_SETTINGS_PATH, before_revision=before_revision,
        after_revision=after_revision, updates=updates,
    )
    discard_form_snapshot(state, key)
