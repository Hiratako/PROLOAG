from __future__ import annotations

from datetime import date
from typing import Any

import streamlit as st

import prolog_ui.components as ab
from app_core.navigation import NAV_GROUPS, SETTINGS_PAGE_PATH, hidden_sidebar_pages
from app_core.form_snapshot import discard_form_snapshot, form_snapshot
from app_core.settings_edit_service import save_settings_form_patch
from config import USER_SETTINGS_PATH


def _target_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def render_personalization_settings(settings: dict[str, Any]) -> bool:
    # The parent reruns only after every sibling form has rendered. An early
    # rerun here lets Streamlit clean up unrendered general-form widget drafts.
    rerun_requested = False
    countdown_edit = form_snapshot(st.session_state, "countdown_settings_form", [USER_SETTINGS_PATH], settings)
    countdown = countdown_edit["data"].get("countdown")
    countdown = countdown if isinstance(countdown, dict) else {}
    st.subheader("目標日カウントダウン")
    st.caption("「今日の記録・計画」の冒頭に表示します。日数は目標日と今日の日付の差で、当日は0日です。")
    if st.button("カウントダウンの入力を破棄して再読込", key="reload_countdown_settings_form"):
        discard_form_snapshot(st.session_state, "countdown_settings_form")
        rerun_requested = True
    with st.form(f"countdown_settings_form_{countdown_edit['version']}"):
        enabled = st.checkbox("カウントダウンを表示", value=countdown.get("enabled") is True)
        title = st.text_input("カウントダウンの名前", value=str(countdown.get("title") or "目標日"), placeholder="例：試験日")
        target = st.date_input(
            "目標日", value=_target_date(countdown.get("target_date")),
            min_value=date.min, max_value=date.max,
        )
        submitted = st.form_submit_button("カウントダウン設定を保存", type="primary")
    if submitted:
        if enabled and target is None:
            ab.show_action_error("カウントダウンを表示する場合は、目標日を選択してください。")
        else:
            try:
                save_settings_form_patch(st.session_state, "countdown_settings_form", countdown_edit, {"countdown": {
                    "enabled": bool(enabled), "title": title.strip() or "目標日",
                    "target_date": target.isoformat() if target else None,
                }})
            except (ValueError, OSError) as exc:
                ab.show_action_error(f"保存できませんでした。入力は保持しています。{exc}")
            else:
                ab.queue_success("カウントダウン設定を保存しました。")
                rerun_requested = True

    st.divider()
    st.subheader("サイドバーに表示するページ")
    st.caption("チェックしたページをサイドバーに表示します。非表示にしても記録や機能は削除しません。ポータルとユーザー設定は常に表示します。")
    visibility_edit = form_snapshot(st.session_state, "sidebar_visibility_form", [USER_SETTINGS_PATH], settings)
    hidden = hidden_sidebar_pages(visibility_edit["data"])
    next_hidden: list[str] = []
    if st.button("サイドバー設定の入力を破棄して再読込", key="reload_sidebar_visibility_form"):
        discard_form_snapshot(st.session_state, "sidebar_visibility_form")
        rerun_requested = True
    with st.form(f"sidebar_visibility_form_{visibility_edit['version']}"):
        for group in NAV_GROUPS:
            st.markdown(f"#### {group['title']}")
            columns = st.columns(2)
            for index, item in enumerate(group["items"]):
                path = item["path"]
                fixed = path == SETTINGS_PAGE_PATH
                visible = columns[index % 2].checkbox(
                    item["title"], value=fixed or path not in hidden,
                    disabled=fixed, help="表示設定を戻せるよう、常に表示します。" if fixed else None,
                )
                if not visible and not fixed:
                    next_hidden.append(path)
        visibility_submitted = st.form_submit_button("サイドバー表示設定を保存", type="primary")
    if visibility_submitted:
        try:
            save_settings_form_patch(st.session_state, "sidebar_visibility_form", visibility_edit, {"display": {"hidden_sidebar_pages": next_hidden}})
        except (ValueError, OSError) as exc:
            ab.show_action_error(f"保存できませんでした。入力は保持しています。{exc}")
        else:
            ab.queue_success("サイドバーの表示設定を保存しました。")
            rerun_requested = True
    st.divider()
    return rerun_requested
