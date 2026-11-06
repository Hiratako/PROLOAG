from __future__ import annotations

from typing import Any


THEME_STANDARD = "standard"
THEME_DARK = "dark"
THEME_MODES = (THEME_STANDARD, THEME_DARK)

STANDARD_PALETTE: dict[str, Any] = {
    "mode": THEME_STANDARD,
    "primary": "#003B71",
    "primary_2": "#075C9F",
    "accent": "#00A3E0",
    "ink": "#152238",
    "muted": "#667085",
    "line": "#D9E2EC",
    "background": "#F4F7FB",
    "background_top": "#F7FBFF",
    "background_bottom": "#EEF4F8",
    "card": "#FFFFFF",
    "panel": "#FFFFFF",
    "panel_2": "#E8EEF5",
    "input": "#FFFFFF",
    "cyan": "#1F6F9F",
    "green": "#2C7A5A",
    "amber": "#B7791F",
    "red": "#B54747",
    "purple": "#7357A8",
    "grid": "#D5DEE8",
}

DARK_PALETTE: dict[str, Any] = {
    "mode": THEME_DARK,
    "primary": "#39D5FF",
    "primary_2": "#56F0A6",
    "accent": "#56F0A6",
    "ink": "#EAF6FF",
    "muted": "#A8C0D4",
    "line": "#29445D",
    "background": "#06111F",
    "background_top": "#081827",
    "background_bottom": "#020611",
    "card": "#0B1728",
    "panel": "#0B1728",
    "panel_2": "#10243A",
    "input": "#10243A",
    "cyan": "#39D5FF",
    "green": "#56F0A6",
    "amber": "#FFB86B",
    "red": "#FF6B8A",
    "purple": "#A78BFA",
    "grid": "#29445D",
}


def normalize_theme_mode(value: Any) -> str:
    return THEME_DARK if str(value or "").strip().lower() == THEME_DARK else THEME_STANDARD


def get_theme_palette(mode: Any) -> dict[str, Any]:
    source = DARK_PALETTE if normalize_theme_mode(mode) == THEME_DARK else STANDARD_PALETTE
    return dict(source)


# Backward-compatible constants for modules that import the original names.
PRIMARY = STANDARD_PALETTE["primary"]
ACCENT = STANDARD_PALETTE["accent"]
INK = STANDARD_PALETTE["ink"]
MUTED = STANDARD_PALETTE["muted"]
SURFACE = STANDARD_PALETTE["card"]
BACKGROUND = STANDARD_PALETTE["background"]
