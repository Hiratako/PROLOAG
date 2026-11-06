from __future__ import annotations

from copy import deepcopy
from typing import Any

from config import USER_SETTINGS_PATH
from app_core.json_store import _UNSET, json_transaction, load_json_safe, observed_json_revision, save_json_safe


DEFAULT_USER_SETTINGS: dict[str, Any] = {
    "timer": {
        "hourly_rate": 2000,
        "goal_minutes": 120,
        "overtime_enabled": True,
        "overtime_multiplier": 1.25,
        "overtime_threshold_hours": 8.0,
        "interleaving_enabled": True,
        "interleaving_minutes": 120,
    },
    "health": {
        "bed_time": "23:30",
        "wake_time": "06:30",
        "sleep_quality": 3,
        "morning_sleepiness": 3,
        "midnight_awakenings": 0,
        "nap_minutes": 0,
        "physical_score": 3,
        "fatigue": 3,
        "stress": 3,
        "focus_score": 3,
        "mood_score": 3,
    },
    "exercise": {
        "type": "running",
        "start_time": "07:00",
        "duration_min": 30,
        "distance_km": 0.0,
        "intensity": 5,
        "fatigue_after": 3,
    },
    "diary": {
        "investment_row_count": 3,
        "caffeine_mg": 0,
    },
    "display": {
        "period_label": "直近30日",
        "theme_mode": "standard",
        "show_task_metrics": True,
        "hidden_sidebar_pages": [],
    },
    "analysis": {
        "caffeine_missing_as_zero": True,
    },
    "countdown": {
        "enabled": False,
        "title": "目標日",
        "target_date": None,
    },
}


def _deep_merge(defaults: dict[str, Any], current: Any) -> dict[str, Any]:
    merged = deepcopy(defaults)
    if not isinstance(current, dict):
        return merged
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_user_settings() -> dict[str, Any]:
    settings = _deep_merge(DEFAULT_USER_SETTINGS, load_json_safe(USER_SETTINGS_PATH, {}))
    return settings


def save_user_settings(settings: dict[str, Any]) -> None:
    save_json_safe(USER_SETTINGS_PATH, _deep_merge(DEFAULT_USER_SETTINGS, settings))


def update_user_settings(updates: dict[str, Any], *, expected_revision: Any = _UNSET) -> None:
    """Save only supplied fields, preserving other sections and unknown values."""
    if not isinstance(updates, dict):
        raise ValueError("設定の更新内容は辞書で指定してください。")
    revision = observed_json_revision(USER_SETTINGS_PATH) if expected_revision is _UNSET else expected_revision
    with json_transaction(USER_SETTINGS_PATH):
        current = load_user_settings()
        save_json_safe(USER_SETTINGS_PATH, _deep_merge(current, updates), expected_revision=revision)


def reset_user_settings() -> None:
    save_user_settings(DEFAULT_USER_SETTINGS)


def get_setting(section: str, key: str, default: Any = None) -> Any:
    settings = load_user_settings()
    section_settings = settings.get(section, {})
    if not isinstance(section_settings, dict):
        return default
    value = section_settings.get(key, default)
    return default if value is None else value


def should_show_task_metrics(settings: dict[str, Any] | None = None) -> bool:
    """Return whether task-derived metrics should be shown in the UI."""
    if settings is None:
        value = get_setting("display", "show_task_metrics", True)
    else:
        section = settings.get("display", {}) if isinstance(settings, dict) else {}
        value = section.get("show_task_metrics", True) if isinstance(section, dict) else True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return True


def should_treat_missing_caffeine_as_zero(settings: dict[str, Any] | None = None) -> bool:
    """Default missing daily caffeine to zero unless explicitly disabled.

    Supplying a captured settings snapshot avoids a new read during form
    rendering. Only actual JSON booleans override the default; strings and
    numeric lookalikes do not silently change the analysis policy.
    """
    current = load_user_settings() if settings is None else settings
    section = current.get("analysis", {}) if isinstance(current, dict) else {}
    value = section.get("caffeine_missing_as_zero") if isinstance(section, dict) else None
    return value if isinstance(value, bool) else True
