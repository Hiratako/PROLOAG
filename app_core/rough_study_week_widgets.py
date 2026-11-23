"""Editable rows grouped by study day for the weekly entry screen."""
from __future__ import annotations

from datetime import date
from uuid import uuid4

import streamlit as st


_WEEKDAYS = "月火水木金土日"


def _new_week_row(day: str) -> dict:
    return {
        "input_id": uuid4().hex,
        "study_date": day,
        "duration": "",
        "subject_id": "",
        "memo": "",
        "include": True,
    }


def _render_row(row: dict, subjects: dict[str, str], *, key: str, can_remove: bool) -> dict | None:
    duration_col, subject_col, memo_col, action_col = st.columns([2, 3, 5, 2], vertical_alignment="bottom")
    duration = duration_col.text_input(
        "学習時間", value=row["duration"], key=f"{key}_duration", placeholder="45分、1:30",
    )
    current_subject = row["subject_id"]
    options = {"": "科目を選択（任意）", **subjects}
    if current_subject and current_subject not in options:
        options[current_subject] = "登録済みの科目"
    subject = subject_col.selectbox(
        "科目", options=list(options), index=list(options).index(current_subject),
        format_func=options.__getitem__, key=f"{key}_subject",
    )
    memo = memo_col.text_input("勉強した内容", value=row["memo"], key=f"{key}_memo", placeholder="任意")
    if can_remove and action_col.button("行を外す", key=f"{key}_remove", width="stretch"):
        return None
    return {**row, "duration": duration, "subject_id": subject, "memo": memo}


def render_week_rows(
    rows: list[dict], subjects: dict[str, str], existing: dict[str, str], *, key: str,
) -> list[dict]:
    """Render each day with its records and return the current unsaved rows."""
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        by_day.setdefault(row["study_date"], []).append(row)

    edited = []
    for day, day_rows in sorted(by_day.items()):
        weekday = _WEEKDAYS[date.fromisoformat(day).weekday()]
        with st.container(border=True):
            label, action = st.columns([3, 2], vertical_alignment="center")
            label.markdown(f"**{day[5:].replace('-', '/')}（{weekday}）**")
            if existing.get(day):
                label.caption(f"登録済み {existing[day]}")
            if action.button("＋この日に追加", key=f"{key}_{day}_add", width="stretch"):
                day_rows.append(_new_week_row(day))
            for index, row in enumerate(day_rows):
                current = _render_row(
                    row, subjects, key=f"{key}_{row['input_id']}",
                    can_remove=index > 0 and not row.get("record_id"),
                )
                if current is not None:
                    edited.append(current)
    return edited
