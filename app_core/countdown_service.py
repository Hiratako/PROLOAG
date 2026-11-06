from __future__ import annotations

from datetime import date, datetime
from typing import Any


def get_countdown(settings: dict[str, Any], today: date) -> dict[str, Any] | None:
    """Build a calendar-day countdown without reading or changing any data.

    The target is an exact ISO calendar date, not a timestamp. A disabled,
    missing, or malformed configuration is simply not displayed.
    """
    if not isinstance(settings, dict) or not isinstance(today, date) or isinstance(today, datetime):
        return None
    section = settings.get("countdown")
    if not isinstance(section, dict) or section.get("enabled") is not True:
        return None
    target_text = section.get("target_date")
    if not isinstance(target_text, str):
        return None
    try:
        target = date.fromisoformat(target_text)
    except ValueError:
        return None
    if target.isoformat() != target_text:
        return None

    raw_title = section.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    days_remaining = (target - today).days
    if days_remaining > 0:
        value = f"あと {days_remaining:,} 日"
    elif days_remaining == 0:
        value = "今日が目標日"
    else:
        value = f"目標日から {-days_remaining:,} 日"
    return {
        "title": title or "目標日",
        "target_date": target.isoformat(),
        "days_remaining": days_remaining,
        "value": value,
    }
