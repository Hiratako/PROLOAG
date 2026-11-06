from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.app_clock import app_today
from app_core.json_operations import atomic_json_operation
from app_core.master_snapshot import MasterSnapshot
from app_core.reminder_page_state import ReminderSourceTitles
from app_core.reminder_service import create_reminder, get_reminders_by_card_id
from data_utils import (
    ensure_list,
    generate_id,
    link_notes_to_item,
    load_all_flashcards,
    load_all_notes,
    now_text,
    resolve_note_title,
    save_flashcards,
)


ROOT = Path(__file__).resolve().parents[1]
MASTER_SNAPSHOT = MasterSnapshot.load(ROOT)
IMPORTANCE_OPTIONS = {
    "low": "低",
    "normal": "標準",
    "high": "高",
}


def option_map(items: list[dict[str, Any]], id_key: str) -> dict[str, str]:
    values = {"未設定": ""}
    values.update({f"{item.get('name', '名称未設定')} [{item.get(id_key, '')}]": item.get(id_key, "") for item in items})
    return values


def selected_label(options: dict[str, str], selected_id: str | None) -> str:
    for label, value in options.items():
        if value == selected_id:
            return label
    return "未設定"


def master_selects(prefix: str, defaults: dict[str, Any] | None = None) -> dict[str, str]:
    defaults = defaults or {}
    project_options = option_map(MASTER_SNAPSHOT.active("projects"), "project_id")
    project_label = st.selectbox(
        "プロジェクト",
        options=list(project_options.keys()),
        index=list(project_options.keys()).index(selected_label(project_options, defaults.get("project_id"))),
        key=f"{prefix}_project",
    )
    project_id = project_options[project_label]

    subject_options = option_map(MASTER_SNAPSHOT.active("subjects", project_id=project_id or None), "subject_id")
    subject_label = st.selectbox(
        "科目",
        options=list(subject_options.keys()),
        index=list(subject_options.keys()).index(selected_label(subject_options, defaults.get("subject_id"))),
        key=f"{prefix}_subject",
    )
    subject_id = subject_options[subject_label]

    material_options = option_map(MASTER_SNAPSHOT.active("materials", project_id=project_id or None, subject_id=subject_id or None), "material_id")
    material_label = st.selectbox(
        "教材",
        options=list(material_options.keys()),
        index=list(material_options.keys()).index(selected_label(material_options, defaults.get("material_id"))),
        key=f"{prefix}_material",
    )
    material_id = material_options[material_label]

    unit_options = option_map(MASTER_SNAPSHOT.active("units", project_id=project_id or None, subject_id=subject_id or None, material_id=material_id or None), "unit_id")
    unit_label = st.selectbox(
        "単元",
        options=list(unit_options.keys()),
        index=list(unit_options.keys()).index(selected_label(unit_options, defaults.get("unit_id"))),
        key=f"{prefix}_unit",
    )
    return {
        "project_id": project_id,
        "subject_id": subject_id,
        "material_id": material_id,
        "unit_id": unit_options[unit_label],
    }


def note_options(notes: list[dict[str, Any]]) -> dict[str, str]:
    return {f"{note.get('title', '無題のページ')} [{note.get('note_id', '')}]": note.get("note_id", "") for note in notes}


def split_tags(text: str) -> list[str]:
    return [item.strip() for item in text.replace("、", ",").split(",") if item.strip()]


def card_question(card: dict[str, Any]) -> str:
    return str(card.get("question") or card.get("front") or "")


def card_answer(card: dict[str, Any]) -> str:
    return str(card.get("answer") or card.get("back") or "")


def card_tags(card: dict[str, Any]) -> list[str]:
    tags = [str(tag) for tag in ensure_list(card.get("tags")) if str(tag).strip()]
    genre = str(card.get("genre") or "").strip()
    if genre and genre not in tags:
        tags.append(genre)
    return tags


def card_row(card: dict[str, Any], source_titles: ReminderSourceTitles | None = None) -> dict[str, Any]:
    names = MASTER_SNAPSHOT.names(card)
    source_note_id = card.get("source_note_id") or (ensure_list(card.get("linked_note_ids"))[0] if ensure_list(card.get("linked_note_ids")) else "")
    answer = card_answer(card)
    return {
        "カードID": card.get("card_id") or card.get("flashcard_id") or "",
        "問題文": card_question(card),
        "解答": answer[:80] + ("..." if len(answer) > 80 else ""),
        "元ノート": (source_titles.note_title(source_note_id) if source_titles is not None
                   else resolve_note_title(ROOT, source_note_id)) if source_note_id else "未設定",
        "プロジェクト": names["project_name"],
        "科目": names["subject_name"],
        "教材": names["material_name"],
        "単元": names["unit_name"],
        "タグ": " / ".join(card_tags(card)),
        "重要度": IMPORTANCE_OPTIONS.get(str(card.get("importance") or "normal"), str(card.get("importance") or "標準")),
        "要復習": "対象" if card.get("need_review") or card.get("important") else "",
        "作成日時": card.get("created_at", ""),
    }


def filter_cards(cards: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
    filtered = cards
    for key in ("project_id", "subject_id", "material_id", "unit_id"):
        if filters.get(key):
            filtered = [card for card in filtered if card.get(key) == filters[key]]
    if filters.get("tag"):
        filtered = [card for card in filtered if filters["tag"] in card_tags(card)]
    if filters.get("need_review"):
        filtered = [card for card in filtered if card.get("need_review") or card.get("important")]
    keyword = str(filters.get("keyword") or "").strip().lower()
    if keyword:
        filtered = [
            card
            for card in filtered
            if keyword in " ".join([card_question(card), card_answer(card), str(card.get("explanation", ""))]).lower()
        ]
    return filtered


def choose_review_card(cards: list[dict[str, Any]], *, advance: bool = False) -> dict[str, Any]:
    """Keep the visible card stable while a button-triggered rerun is handled."""
    occurrences: dict[str, int] = {}
    available: dict[tuple[str, int], dict[str, Any]] = {}
    for card in cards:
        identifier = str(card.get("card_id") or card.get("flashcard_id") or "")
        occurrence = occurrences.get(identifier, 0)
        occurrences[identifier] = occurrence + 1
        available[(identifier, occurrence)] = card
    selected = st.session_state.get("p07_current_card")
    if advance or selected not in available:
        candidates = [key for key in available if not advance or key != selected] or list(available)
        weighted = candidates + [key for key in candidates if available[key].get("need_review") or available[key].get("important")] * 2
        selected = random.choice(weighted)
        st.session_state.p07_current_card = selected
        st.session_state.p07_show_answer = False
    return available[selected]


@st.fragment
def render_card_review(filters: dict[str, Any], initial_context: dict[str, Any]) -> None:
    global MASTER_SNAPSHOT
    # Consume the full render's small-file context once. Local interactions
    # reload it; no data or classification snapshot is retained in session state.
    context = initial_context.pop("context", None)
    review_today = app_today()
    if context is None:
        context = {"cards": load_all_flashcards(ROOT), "notes": load_all_notes(ROOT),
                   "master": MasterSnapshot.load(ROOT)}
    MASTER_SNAPSHOT = context["master"]
    source_titles = ReminderSourceTitles(context["notes"], [])
    filtered_cards = filter_cards(context["cards"], filters)
    if not filtered_cards:
        st.info("条件に一致するカードはありません。")
        return
    next_card = st.button("次のカード", key="p07_next_card", help="別の問題に進むと解答表示を閉じます。")
    current_card = choose_review_card(filtered_cards, advance=next_card)
    names = MASTER_SNAPSHOT.names(current_card)

    left, right = st.columns([1.2, 1])
    with left:
        ab.card("本日の出題", card_question(current_card))
        if st.checkbox("解答を表示", key="p07_show_answer"):
            st.write(card_answer(current_card))
            if current_card.get("explanation"):
                st.markdown("#### 解説")
                st.write(current_card.get("explanation"))
    with right:
        source_note_id = current_card.get("source_note_id") or (ensure_list(current_card.get("linked_note_ids"))[0] if ensure_list(current_card.get("linked_note_ids")) else "")
        ab.card(
            "カード情報",
            "\n".join(
                [
                    f"元ノート: {source_titles.note_title(source_note_id) if source_note_id else '未設定'}",
                    f"プロジェクト: {names['project_name']}",
                    f"科目: {names['subject_name']}",
                    f"教材: {names['material_name']}",
                    f"単元: {names['unit_name']}",
                    f"タグ: {' / '.join(card_tags(current_card)) or '未設定'}",
                ]
            ),
        )

        current_card_id = str(current_card.get("card_id") or current_card.get("flashcard_id") or "")
        active_card_schedules = [
            reminder
            for reminder in get_reminders_by_card_id(current_card_id, ROOT)
            if reminder.get("status") != "done" and reminder.get("schedule_status", "active") == "active"
        ]
        if active_card_schedules:
            st.caption(f"有効な復習スケジュール: {len(active_card_schedules)}件")
        elif st.button("このカードを適応型復習に登録", key=f"schedule_card_{current_card_id}", width="stretch"):
            try:
                with atomic_json_operation(root=ROOT, kind="card_reminder_add"):
                    created = create_reminder(
                        {
                            "source_type": "card",
                            "source_note_id": source_note_id,
                            "source_card_id": current_card_id,
                            "linked_note_ids": [source_note_id] if source_note_id else [],
                            "linked_card_ids": [current_card_id],
                            "title": f"{card_question(current_card)[:60]}を復習",
                            "keyword": card_question(current_card)[:60],
                            "memo": "",
                            "review_type": "card_review",
                            "priority": "normal",
                            "status": "pending",
                            "scheduled_date": review_today.isoformat(),
                            "registered": review_today.isoformat(),
                            **{key: current_card.get(key, "") for key in ("project_id", "subject_id", "material_id", "unit_id")},
                        },
                        schedule_mode="sm2",
                        root=ROOT,
                    )
                    if source_note_id:
                        link_notes_to_item(ROOT, [source_note_id], str(created.get("reminder_id")), "linked_reminder_ids")
            except (OSError, ValueError) as exc:
                ab.show_action_error(f"復習予定を保存できませんでした。{exc}")
            else:
                ab.queue_success("このカードを適応型復習に登録しました。")
                st.rerun()


st.set_page_config(page_title="記憶フラッシュ", layout="wide")
page_settings = ab.sidemenu("単語カードを登録して復習できます。")
ab.inject_css(settings=page_settings)
ab.topbar("記憶フラッシュ", "ノートや分類情報とつながる単語カードを管理します。")

cards = load_all_flashcards(ROOT)
notes = load_all_notes(ROOT)
note_label_to_id = note_options(notes)

with st.sidebar:
    st.markdown("### 絞り込み")
    filter_master_values = master_selects("card_filter")
    all_tags = sorted({tag for card in cards for tag in card_tags(card)})
    tag_label = st.selectbox("タグ", options=["すべて"] + all_tags, key="card_filter_tag")
    review_only = st.checkbox("要復習のみ", key="card_filter_review")
    keyword = st.text_input("キーワード", key="card_filter_keyword")

filters = {
    **filter_master_values,
    "tag": "" if tag_label == "すべて" else tag_label,
    "need_review": review_only,
    "keyword": keyword,
}

with st.expander("カードを追加", expanded=True):
    with st.form("card_form"):
        question = st.text_area("問題文", height=80)
        answer = st.text_area("解答", height=100)
        explanation = st.text_area("解説", height=100)
        genre = st.text_input("ジャンル")
        tag_text = st.text_input("タグ", help="カンマ区切りで複数登録できます。")
        importance_label = st.selectbox("重要度", options=list(IMPORTANCE_OPTIONS.values()), index=1)
        need_review = st.checkbox("要復習")
        selected_note_labels = st.multiselect("関連ノート", options=list(note_label_to_id.keys()))
        st.markdown("#### 分類")
        card_master_values = master_selects("new_card")
        if st.form_submit_button("追加", type="primary"):
            if not question.strip() or not answer.strip():
                ab.show_action_error("問題文と解答を入力してください。")
            else:
                existing_ids = {str(card.get("card_id") or card.get("flashcard_id")) for card in cards if isinstance(card, dict)}
                card_id = generate_id("card", existing_ids=existing_ids)
                linked_note_ids = [note_label_to_id[label] for label in selected_note_labels]
                source_note_id = linked_note_ids[0] if linked_note_ids else ""
                timestamp = now_text()
                new_card = {
                        "card_id": card_id,
                        "flashcard_id": card_id,
                        "question": question.strip(),
                        "answer": answer.strip(),
                        "front": question.strip(),
                        "back": answer.strip(),
                        "explanation": explanation.strip(),
                        "genre": genre.strip(),
                        "tags": split_tags(tag_text),
                        "importance": next((key for key, value in IMPORTANCE_OPTIONS.items() if value == importance_label), "normal"),
                        "need_review": need_review,
                        "important": need_review,
                        "source_note_id": source_note_id,
                        "linked_note_ids": linked_note_ids,
                        **card_master_values,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    }
                try:
                    with atomic_json_operation(root=ROOT, kind="flashcard_add"):
                        save_flashcards(ROOT, [*cards, new_card])
                        link_notes_to_item(ROOT, linked_note_ids, card_id, "linked_flashcard_ids")
                except (OSError, ValueError) as exc:
                    ab.show_action_error(f"カードを保存できませんでした。入力は保持しています。{exc}")
                else:
                    ab.queue_success("カードを追加しました。")
                    st.rerun()

if not cards:
    st.info("カードがまだありません。まずは1枚追加してください。")
    st.stop()

filtered_cards = filter_cards(cards, filters)
if not filtered_cards:
    st.info("条件に一致するカードはありません。")
    st.stop()

render_card_review(filters, {"context": {"cards": cards, "notes": notes, "master": MASTER_SNAPSHOT}})

st.markdown("### カード一覧")
source_titles = ReminderSourceTitles(notes, [])
card_table = pd.DataFrame([card_row(card, source_titles) for card in filtered_cards])
card_table["_sort_created_at"] = pd.to_datetime(card_table["作成日時"], errors="coerce")
card_table = (
    card_table.sort_values("_sort_created_at", ascending=False, kind="stable", na_position="last")
    .drop(columns="_sort_created_at")
)
st.dataframe(card_table, width="stretch", hide_index=True)
