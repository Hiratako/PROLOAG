from __future__ import annotations

from web_demo.runtime import fragment as demo_fragment

from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.app_clock import app_today
from app_core.json_operations import atomic_json_operation
from app_core.master_snapshot import MasterSnapshot
from app_core.reminder_page_state import (
    ReminderSourceTitles,
    capture_review_draft,
    discard_review_draft,
    restore_review_draft,
)
from app_core.reminder_service import (
    PRESET_INTERVALS,
    complete_review,
    create_reminder,
    defer_reminder,
    get_due_reminders,
    normalize_intervals,
    schedule_progress,
    set_schedule_status,
    skip_review,
)
from data_utils import (
    ensure_list,
    link_notes_to_item,
    load_all_flashcards,
    load_all_notes,
    load_all_reminders,
)


from config import get_data_root

ROOT = get_data_root()
TODAY = app_today()
MASTER_SNAPSHOT = MasterSnapshot.load(ROOT)

REVIEW_TYPE_OPTIONS = {
    "note_review": "ノート復習",
    "card_review": "カード復習",
    "keyword_review": "キーワード復習",
    "general": "一般",
}
PRIORITY_OPTIONS = {"low": "低", "normal": "標準", "high": "高"}
STATUS_OPTIONS = {"pending": "未対応", "in_progress": "対応中", "done": "完了", "deferred": "保留"}
SCHEDULE_OPTIONS = {
    "single": "単発",
    "light": "軽め（1・7・30日）",
    "standard": "標準（1・3・7・14・30・60日）",
    "intensive": "重点（1・2・4・7・14・30・60・90日）",
    "custom": "カスタム間隔",
    "sm2": "適応型（SM-2）",
}
RESULT_LABELS = {
    "forgot": "忘れた",
    "difficult": "難しかった",
    "normal": "普通",
    "easy": "簡単だった",
    "skipped": "スキップ",
}


def parse_date(value: Any, fallback: date | None = None) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return fallback if fallback is not None else TODAY


def option_map(items: list[dict[str, Any]], id_key: str) -> dict[str, str]:
    values = {"未設定": ""}
    values.update({f"{item.get('name', '名称未設定')} [{item.get(id_key, '')}]": str(item.get(id_key, "")) for item in items})
    return values


def selected_label(options: dict[str, str], selected_id: str | None) -> str:
    return next((label for label, value in options.items() if value == selected_id), "未設定")


def master_selects(prefix: str, defaults: dict[str, Any] | None = None) -> dict[str, str]:
    defaults = defaults or {}
    project_options = option_map(MASTER_SNAPSHOT.active("projects"), "project_id")
    project_label = st.selectbox(
        "プロジェクト",
        list(project_options),
        index=list(project_options).index(selected_label(project_options, defaults.get("project_id"))),
        key=f"{prefix}_project",
    )
    project_id = project_options[project_label]

    subject_options = option_map(MASTER_SNAPSHOT.active("subjects", project_id=project_id or None), "subject_id")
    subject_label = st.selectbox(
        "科目",
        list(subject_options),
        index=list(subject_options).index(selected_label(subject_options, defaults.get("subject_id"))),
        key=f"{prefix}_subject",
    )
    subject_id = subject_options[subject_label]

    material_options = option_map(MASTER_SNAPSHOT.active("materials", project_id=project_id or None, subject_id=subject_id or None), "material_id")
    material_label = st.selectbox(
        "教材",
        list(material_options),
        index=list(material_options).index(selected_label(material_options, defaults.get("material_id"))),
        key=f"{prefix}_material",
    )
    material_id = material_options[material_label]

    unit_options = option_map(MASTER_SNAPSHOT.active("units", project_id=project_id or None, subject_id=subject_id or None, material_id=material_id or None), "unit_id")
    unit_label = st.selectbox(
        "単元",
        list(unit_options),
        index=list(unit_options).index(selected_label(unit_options, defaults.get("unit_id"))),
        key=f"{prefix}_unit",
    )
    return {
        "project_id": project_id,
        "subject_id": subject_id,
        "material_id": material_id,
        "unit_id": unit_options[unit_label],
    }


def reminder_title(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("keyword") or "無題のリマインダー")


def safe_url(value: Any) -> str:
    """http/https URLだけをリンク表示へ渡します。"""
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def source_label(item: dict[str, Any]) -> str:
    card_id = str(item.get("source_card_id") or "")
    note_id = str(item.get("source_note_id") or "")
    if card_id:
        return f"カード: {SOURCE_TITLES.card_title(card_id)}"
    if note_id:
        return f"ノート: {SOURCE_TITLES.note_title(note_id)}"
    return "手動登録"


def schedule_label(item: dict[str, Any]) -> str:
    mode = str(item.get("schedule_mode") or "single")
    if mode == "sm2" or item.get("algorithm") == "sm2":
        return "適応型（SM-2）"
    if mode == "custom":
        return "カスタム"
    return SCHEDULE_OPTIONS.get(mode, mode)


def reminder_row(item: dict[str, Any]) -> dict[str, Any]:
    names = MASTER_SNAPSHOT.names(item)
    scheduled = parse_date(item.get("scheduled_date") or item.get("registered"))
    status = str(item.get("status") or "pending")
    return {
        "タイトル": reminder_title(item),
        "参照元": source_label(item),
        "次回復習日": scheduled.isoformat(),
        "期限状態": "期限到来" if scheduled <= TODAY and status != "done" else "",
        "方式": schedule_label(item),
        "進捗": schedule_progress(item),
        "最終評価": RESULT_LABELS.get(str(item.get("last_review_result") or ""), "未記録"),
        "復習回数": int(item.get("review_count", 0) or 0),
        "状態": STATUS_OPTIONS.get(status, status),
        "系列状態": str(item.get("schedule_status") or "active"),
        "科目": names["subject_name"],
        "教材": names["material_name"],
        "単元": names["unit_name"],
        "メモ概要": str(item.get("memo") or "")[:60],
        "URL": safe_url(item.get("url")),
    }


def render_reminder_details(item: dict[str, Any], context: str) -> None:
    reminder_id = str(item.get("reminder_id") or "")
    names = MASTER_SNAPSHOT.names(item)
    st.caption(
        f"{source_label(item)} / 予定日: {parse_date(item.get('scheduled_date')).isoformat()} / "
        f"{schedule_label(item)} / {schedule_progress(item)}"
    )

    st.markdown("**メモ**")
    st.write(str(item.get("memo") or "メモは登録されていません。"))

    raw_url = str(item.get("url") or "").strip()
    url = safe_url(raw_url)
    if url:
        st.link_button(
            "URLを別タブで開く",
            url,
            key=f"open_url_{context}_{reminder_id}",
            width="content",
        )
        st.caption(url)
    elif raw_url:
        st.warning("登録URLの形式を確認してください。http または https のURLのみ開けます。")
        st.code(raw_url)
    else:
        st.caption("URLは登録されていません。")

    detail_cols = st.columns(4)
    detail_cols[0].metric("プロジェクト", names["project_name"])
    detail_cols[1].metric("科目", names["subject_name"])
    detail_cols[2].metric("教材", names["material_name"])
    detail_cols[3].metric("単元", names["unit_name"])


def reminder_can_be_changed(item: dict[str, Any]) -> bool:
    """ID-based save APIs cannot safely choose between duplicate records."""
    reminder_id = str(item.get("reminder_id") or "")
    count = REMINDER_ID_COUNTS.get(reminder_id, 0)
    if not reminder_id or count != 1:
        st.warning(
            f"復習ID「{reminder_id or '未設定'}」が重複または未設定のため、保存先を一意に特定できません。"
            "IDを確認・修正するまでは、復習入力・スキップ・予定変更を停止しています。"
        )
        return False
    return True


def save_reminder_action(action: Any, message: str, *, on_success: Any = None,
                         missing_message: str = "復習予定が見つからなかったため変更していません。入力は保持しています。") -> None:
    try:
        updated = action()
    except (ValueError, OSError) as exc:
        ab.show_action_error(f"復習予定を更新できませんでした。入力は保持しています。{exc}")
        return
    if not updated:
        ab.show_action_message(missing_message, kind="warning")
        return
    if on_success is not None:
        on_success()
    ab.queue_success(message)
    st.rerun()


def render_review_actions(item: dict[str, Any], context: str) -> None:
    reminder_id = str(item.get("reminder_id") or "")
    render_reminder_details(item, context)
    if not reminder_can_be_changed(item):
        return
    memo_key, duration_key = restore_review_draft(st.session_state, reminder_id)

    def remember_draft() -> None:
        capture_review_draft(st.session_state, reminder_id)

    input_cols = st.columns([3, 1])
    with input_cols[0]:
        review_memo = st.text_input("今回の復習メモ", key=memo_key, on_change=remember_draft)
    with input_cols[1]:
        review_duration = st.number_input(
            "復習時間（分）",
            min_value=0,
            max_value=1440,
            step=5,
            key=duration_key,
            on_change=remember_draft,
        )
    capture_review_draft(st.session_state, reminder_id)

    result_cols = st.columns(4)
    for column, result in zip(result_cols, ("forgot", "difficult", "normal", "easy")):
        with column:
            if st.button(RESULT_LABELS[result], key=f"review_{context}_{reminder_id}_{result}", width="stretch"):
                save_reminder_action(
                    lambda: complete_review(reminder_id, result, memo=review_memo, duration_min=int(review_duration), root=ROOT),
                    "復習結果を記録し、次回予定を更新しました。",
                    on_success=lambda: discard_review_draft(st.session_state, reminder_id),
                    missing_message="復習予定が見つからなかったため保存していません。入力は保持しています。",
                )

    action_cols = st.columns(4)
    with action_cols[0]:
        if st.button("明日に延期", key=f"defer_{context}_{reminder_id}", width="stretch"):
            save_reminder_action(lambda: defer_reminder(reminder_id, 1, ROOT), "復習予定を明日に延期しました。")
    with action_cols[1]:
        if st.button("一時停止", key=f"pause_{context}_{reminder_id}", width="stretch"):
            save_reminder_action(lambda: set_schedule_status(reminder_id, "paused", ROOT), "復習予定を一時停止しました。")
    with action_cols[2]:
        if st.button("今回はスキップ", key=f"skip_{context}_{reminder_id}", width="stretch"):
            save_reminder_action(
                lambda: skip_review(reminder_id, root=ROOT), "今回の復習をスキップしました。",
                on_success=lambda: discard_review_draft(st.session_state, reminder_id),
                missing_message="復習予定が見つからなかったためスキップしていません。入力は保持しています。",
            )
    with action_cols[3]:
        if st.button("キャンセル", key=f"cancel_{context}_{reminder_id}", width="stretch"):
            save_reminder_action(lambda: set_schedule_status(reminder_id, "cancelled", ROOT), "復習予定をキャンセルしました。")
    st.divider()


def select_reminder(items: list[dict[str, Any]], context: str) -> dict[str, Any]:
    """Keep one detail selection per view, even while its widget is hidden."""
    options: dict[tuple[str, int], dict[str, Any]] = {}
    occurrences: dict[str, int] = {}
    for item in items:
        reminder_id = str(item.get("reminder_id") or "")
        occurrence = occurrences.get(reminder_id, 0)
        occurrences[reminder_id] = occurrence + 1
        options[(reminder_id, occurrence)] = item
    choices = list(options)
    stored_key = f"p08_selected_reminder_{context}"
    previous = st.session_state.get(stored_key)
    selected = st.selectbox(
        "詳細を表示する復習予定",
        choices,
        index=choices.index(previous) if previous in choices else 0,
        format_func=lambda value: (
            f"{reminder_title(options[value])} / "
            f"{parse_date(options[value].get('scheduled_date')).isoformat()} [{value[0]}]"
        ),
        key=f"p08_reminder_selection_{context}",
    )
    st.session_state[stored_key] = selected
    st.caption(f"{len(items):,}件中、選択した1件の詳細を表示しています。")
    return options[selected]


def render_reminder_table(items: list[dict[str, Any]]) -> None:
    st.dataframe(
        pd.DataFrame([reminder_row(item) for item in items]),
        width="stretch",
        hide_index=True,
        column_config={"URL": st.column_config.LinkColumn("URL", display_text="リンクを開く")},
    )


@demo_fragment
def render_selected_review(selected: dict[str, Any], context: str, initial_context: dict[str, Any]) -> None:
    global MASTER_SNAPSHOT, SOURCE_TITLES, REMINDER_ID_COUNTS, TODAY
    shared = initial_context.pop("context", None)
    if shared is None:
        current_today = app_today()
        if current_today != TODAY:
            st.rerun()
        TODAY = current_today
        shared = {"items": load_all_reminders(ROOT, today=TODAY),
                  "master": MasterSnapshot.load(ROOT),
                  "titles": ReminderSourceTitles(load_all_notes(ROOT), load_all_flashcards(ROOT))}
    MASTER_SNAPSHOT, SOURCE_TITLES = shared["master"], shared["titles"]
    REMINDER_ID_COUNTS = Counter(str(item.get("reminder_id") or "") for item in shared["items"])
    reminder_id = str(selected.get("reminder_id") or "")
    matches = [item for item in shared["items"] if str(item.get("reminder_id") or "") == reminder_id]
    if not matches:
        st.warning("この復習予定は別画面で削除されています。入力は保持しています。表示内容を切り替えて最新の予定を確認してください。")
        return
    # Check duplicates against every record, including filtered-out schedules.
    current = matches[0] if len(matches) == 1 else selected
    render_review_actions(current, context)


st.set_page_config(page_title="復習リマインダー", layout="wide")
page_settings = ab.sidemenu("復習対象と次回予定を管理します。")
ab.inject_css(settings=page_settings)
ab.topbar("復習リマインダー", "単発予定と間隔反復を管理し、復習結果に応じて次回予定を調整します。")

items = load_all_reminders(ROOT, today=TODAY)
REMINDER_ID_COUNTS = Counter(str(item.get("reminder_id") or "") for item in items)
notes = load_all_notes(ROOT)
cards = load_all_flashcards(ROOT)
SOURCE_TITLES = ReminderSourceTitles(notes, cards)
note_options = {f"{note.get('title', '無題のノート')} [{note.get('note_id', '')}]": str(note.get("note_id", "")) for note in notes}
card_options = {
    f"{str(card.get('question') or card.get('front') or '無題のカード')[:70]} [{card.get('card_id') or card.get('flashcard_id', '')}]":
    str(card.get("card_id") or card.get("flashcard_id", ""))
    for card in cards
}

with st.sidebar:
    st.markdown("### 表示条件")
    only_active = st.toggle("有効な予定のみ", value=True)
    filter_keyword = st.text_input("キーワード")

with st.expander("復習予定を追加", expanded=not bool(items)):
    with st.form("spaced_reminder_form"):
        left, right = st.columns(2)
        with left:
            title = st.text_input("タイトル")
            memo = st.text_area("メモ", height=90)
            url = st.text_input("URL")
            first_review_date = st.date_input("最初の復習日", value=TODAY)
            review_type_label = st.selectbox("復習タイプ", list(REVIEW_TYPE_OPTIONS.values()))
            priority_label = st.selectbox("優先度", list(PRIORITY_OPTIONS.values()), index=1)
        with right:
            schedule_label_selected = st.selectbox("復習方式", list(SCHEDULE_OPTIONS.values()), index=2)
            schedule_mode = next(key for key, value in SCHEDULE_OPTIONS.items() if value == schedule_label_selected)
            custom_intervals = st.text_input(
                "カスタム間隔（日数）",
                value="1, 3, 7, 14, 30, 60",
                disabled=schedule_mode != "custom",
                help="カンマ区切りで入力します。",
            )
            selected_note = st.selectbox("関連ノート", ["未設定", *note_options.keys()])
            selected_card = st.selectbox("関連カード", ["未設定", *card_options.keys()])
            st.markdown("#### 分類")
            master_values = master_selects("new_spaced_reminder")

        submitted = st.form_submit_button("復習予定を登録", type="primary")
        if submitted:
            if not title.strip():
                ab.show_action_error("タイトルを入力してください。")
            elif schedule_mode == "custom" and not normalize_intervals(custom_intervals):
                ab.show_action_error("カスタム間隔を1日以上の整数で入力してください。")
            else:
                note_id = note_options.get(selected_note, "")
                card_id = card_options.get(selected_card, "")
                try:
                    with atomic_json_operation(root=ROOT, kind="reminder_add"):
                        created = create_reminder(
                            {
                                "source_type": "card" if card_id else ("note" if note_id else "manual"),
                                "source_note_id": note_id,
                                "source_card_id": card_id,
                                "linked_note_ids": [note_id] if note_id else [],
                                "linked_card_ids": [card_id] if card_id else [],
                                "title": title.strip(),
                                "keyword": title.strip(),
                                "memo": memo.strip(),
                                "url": url.strip(),
                                "review_type": next((key for key, value in REVIEW_TYPE_OPTIONS.items() if value == review_type_label), "general"),
                                "priority": next((key for key, value in PRIORITY_OPTIONS.items() if value == priority_label), "normal"),
                                "status": "pending",
                                "scheduled_date": first_review_date.isoformat(),
                                "registered": TODAY.isoformat(),
                                **master_values,
                            },
                            schedule_mode=schedule_mode,
                            preset=schedule_mode if schedule_mode in PRESET_INTERVALS else "standard",
                            custom_intervals=custom_intervals,
                            root=ROOT,
                        )
                        if note_id:
                            link_notes_to_item(ROOT, [note_id], str(created.get("reminder_id")), "linked_reminder_ids")
                except (OSError, ValueError) as exc:
                    ab.show_action_error(f"復習予定を保存できませんでした。入力は保持しています。{exc}")
                else:
                    ab.queue_success("復習予定を登録しました。")
                    st.rerun()

visible_items = items
if only_active:
    visible_items = [
        item for item in visible_items
        if item.get("status") != "done" and item.get("schedule_status", "active") == "active"
    ]
if filter_keyword.strip():
    needle = filter_keyword.strip().lower()
    visible_items = [
        item for item in visible_items
        if needle in f"{reminder_title(item)} {item.get('memo', '')} {source_label(item)}".lower()
    ]

due_items = get_due_reminders(TODAY, ROOT, reminders=visible_items)
upcoming_items = sorted(
    [
        item for item in visible_items
        if parse_date(item.get("scheduled_date")) > TODAY
    ],
    key=lambda item: parse_date(item.get("scheduled_date")),
)
active_schedules = [
    item for item in items
    if item.get("schedule_mode", "single") != "single"
]

metrics = st.columns(4)
with metrics[0]:
    ab.metric_block("本日・期限超過", f"{len(due_items):,}件", "現在対応できる復習")
with metrics[1]:
    ab.metric_block("今後7日", f"{sum(1 for item in upcoming_items if parse_date(item.get('scheduled_date')) <= TODAY + timedelta(days=7)):,}件", "直近の復習予定")
with metrics[2]:
    ab.metric_block("有効な復習系列", f"{sum(1 for item in active_schedules if item.get('schedule_status', 'active') == 'active'):,}件", "固定間隔・適応型")
with metrics[3]:
    ab.metric_block("累計復習回数", f"{sum(int(item.get('review_count', 0) or 0) for item in items):,}回", "記録済みの復習")

selected_view = st.radio(
    "表示内容",
    ["今日の復習", "今後の予定", "全件", "スケジュール管理"],
    horizontal=True,
    key="p08_selected_view",
)

if selected_view == "今日の復習":
    if not due_items:
        st.info("本日対応する復習はありません。")
    else:
        render_reminder_table(due_items)
        render_selected_review(select_reminder(due_items, "due"), "due",
                               {"context": {"items": items, "master": MASTER_SNAPSHOT, "titles": SOURCE_TITLES}})

elif selected_view == "今後の予定":
    if upcoming_items:
        render_reminder_table(upcoming_items)
        st.markdown("#### 詳細")
        render_reminder_details(select_reminder(upcoming_items, "upcoming"), "upcoming")
    else:
        st.info("今後の復習予定はありません。")

elif selected_view == "全件":
    if visible_items:
        render_reminder_table(visible_items)
        st.markdown("#### 詳細")
        render_reminder_details(select_reminder(visible_items, "all"), "all")
    else:
        st.info("条件に一致する復習予定はありません。")

elif selected_view == "スケジュール管理":
    if not active_schedules:
        st.info("間隔反復スケジュールはまだありません。")
    else:
        item = select_reminder(active_schedules, "schedule")
        reminder_id = str(item.get("reminder_id") or "")
        with st.container():
            render_reminder_details(item, "schedule")
            st.write(f"次回復習日: {parse_date(item.get('scheduled_date')).isoformat()}")
            st.write(f"系列状態: {item.get('schedule_status', 'active')}")
            if item.get("algorithm") == "sm2":
                st.caption(
                    f"反復段階: {int(item.get('repetition', 0) or 0)} / "
                    f"次期間隔: {int(item.get('interval_days', 0) or 0)}日 / "
                    f"易しさ係数: {float(item.get('ease_factor', 2.5) or 2.5):.2f}"
                )
            else:
                st.caption(f"設定間隔: {', '.join(map(str, normalize_intervals(item.get('intervals_days')))) or '未設定'} 日")
            history = ensure_list(item.get("review_history"))
            if history:
                history_table = pd.DataFrame(history)
                for date_column in ("recorded_at", "reviewed_on"):
                    source = history_table[date_column] if date_column in history_table else pd.Series(pd.NaT, index=history_table.index)
                    history_table[f"_sort_{date_column}"] = pd.to_datetime(source, errors="coerce")
                history_table = history_table.sort_values(
                    "_sort_reviewed_on", ascending=False, kind="stable", na_position="last"
                ).sort_values(
                    "_sort_recorded_at", ascending=False, kind="stable", na_position="last"
                )
                history_table = history_table.drop(columns=["_sort_recorded_at", "_sort_reviewed_on"])
                st.dataframe(history_table, width="stretch", hide_index=True)
            if reminder_can_be_changed(item):
                controls = st.columns(3)
                with controls[0]:
                    if st.button("再開", key=f"resume_schedule_{reminder_id}", width="stretch"):
                        save_reminder_action(lambda: set_schedule_status(reminder_id, "active", ROOT), "復習スケジュールを再開しました。")
                with controls[1]:
                    if st.button("一時停止", key=f"pause_schedule_{reminder_id}", width="stretch"):
                        save_reminder_action(lambda: set_schedule_status(reminder_id, "paused", ROOT), "復習スケジュールを一時停止しました。")
                with controls[2]:
                    if st.button("キャンセル", key=f"cancel_schedule_{reminder_id}", width="stretch"):
                        save_reminder_action(lambda: set_schedule_status(reminder_id, "cancelled", ROOT), "復習スケジュールをキャンセルしました。")
