from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def date_token(value: str | date | datetime | None = None) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed:
            return parsed.strftime("%Y%m%d")
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 8:
            return digits[:8]
    return datetime.now().strftime("%Y%m%d")


def generate_id(prefix: str, date_value: str | date | datetime | None = None, existing_ids: set[str] | None = None, **kwargs: Any) -> str:
    if date_value is None and "date" in kwargs:
        date_value = kwargs["date"]
    if existing_ids is None:
        existing_ids = set()
    token = date_token(date_value)
    pattern = re.compile(rf"^{re.escape(prefix)}_{token}_(\d{{3,}})$")
    max_index = 0
    for item_id in existing_ids:
        match = pattern.match(str(item_id))
        if match:
            max_index = max(max_index, int(match.group(1)))
    index = max_index + 1
    candidate = f"{prefix}_{token}_{index:03d}"
    while candidate in existing_ids:
        index += 1
        candidate = f"{prefix}_{token}_{index:03d}"
    existing_ids.add(candidate)
    return candidate


def generate_daily_sequence_id(prefix: str, existing_ids: set[str] | None = None, date_value: str | date | datetime | None = None, **kwargs: Any) -> str:
    if date_value is None and "date" in kwargs:
        date_value = kwargs["date"]
    return generate_id(prefix, date_value, existing_ids)
