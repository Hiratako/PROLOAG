"""Fixed synthetic examples for the public Web experience only.

These values illustrate the application's simple residual model. They are not
individual treatment recommendations. Public source and new Windows packages
use the same starter examples; this overlay also handles older Web sessions.
"""
from copy import deepcopy
from typing import Any, MutableMapping


WEB_SUBSTANCE_VARIANT = "web-caffeine-acetaminophen-v1"
LEGACY_DEMO_SUBSTANCE_IDS = frozenset({"eszopiclone", "trazodone", "melatonin"})

WEB_SUBSTANCE_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "substance_id": "caffeine",
        "name": "カフェイン",
        "unit": "mg",
        "default_amount_mg": 200.0,
        "default_label": "タブレット",
        "amount_step_mg": 25.0,
        "half_life_hours": 5.0,
        "peak_delay_minutes": 30,
        "residual_cutoff_hours": 36.0,
        "color": "#25A9D6",
        "fill_enabled": True,
        "is_active": True,
        "sort_order": 10,
        "model_revision": 1,
    },
    {
        "substance_id": "acetaminophen",
        "name": "アセトアミノフェン",
        "unit": "mg",
        "default_amount_mg": 300.0,
        "default_label": "錠剤",
        "amount_step_mg": 100.0,
        "half_life_hours": 2.4,
        "peak_delay_minutes": 30,
        "residual_cutoff_hours": 24.0,
        "color": "#EF7D57",
        "fill_enabled": True,
        "is_active": True,
        "sort_order": 20,
        "model_revision": 1,
    },
)


def default_web_substance_profiles() -> list[dict[str, Any]]:
    """Return detached defaults so a visitor's editor cannot mutate the seed."""
    return deepcopy(list(WEB_SUBSTANCE_PROFILES))


def web_profile_rows(rows: list[Any]) -> list[Any]:
    """Retire only the three old bundled examples, preserving custom profiles."""
    return [row for row in rows if not isinstance(row, dict)
            or str(row.get("substance_id") or "").strip().lower()
            not in LEGACY_DEMO_SUBSTANCE_IDS]


def sanitize_substance_selection(
    state: MutableMapping[str, Any], key: str, choices: list[str], *,
    default: str | list[str], multiple: bool = False,
) -> None:
    """Keep an open tab's valid choices while retiring removed example IDs.

    Call before the widget is instantiated. This changes no records, profiles,
    other widget state, or custom substance that is still in the options list.
    """
    if key not in state:
        return
    previous = state[key]
    allowed = set(choices)
    if multiple:
        selection = [value for value in previous if isinstance(value, str) and value in allowed] \
            if isinstance(previous, (list, tuple)) else []
        if previous and not selection:
            selection = [value for value in default if value in allowed] \
                if isinstance(default, list) else ([default] if default in allowed else [])
        if previous != selection:
            state[key] = selection
    elif not isinstance(previous, str) or previous not in allowed:
        state[key] = default if isinstance(default, str) and default in allowed else choices[0]
