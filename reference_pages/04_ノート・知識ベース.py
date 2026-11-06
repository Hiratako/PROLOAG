from __future__ import annotations

from web_demo.runtime import fragment as demo_fragment

from copy import deepcopy
import json
from app_core.app_clock import app_today
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.master_snapshot import MasterSnapshot
from app_core.settings_service import load_user_settings
from app_core.reference_snapshot import ReferenceSnapshot
from app_core.note_editor import (
    capture_note_snapshot, commit_note_plan, create_note_linked_record,
    list_deleted_notes, load_note_workspace, prepare_note_delete,
    prepare_note_restore, reserved_note_ids, save_note_edits, save_note_workspace,
)
from master_ui import render_master_quick_add
from data_utils import (
    collect_note_store_ids,
    generate_id,
    load_all_flashcards,
    load_all_reminders,
    notes_path,
    now_text,
)


from config import get_data_root

ROOT = get_data_root()
MASTER_SNAPSHOT = None
IMPORTANCE_OPTIONS = ["low", "normal", "high"]
PRIORITY_OPTIONS = ["low", "normal", "high"]
REMINDER_STATUS_OPTIONS = ["pending", "done", "skipped"]


def sec_to_hms(seconds: int | float) -> str:
    total = max(int(seconds or 0), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def yen(value: int | float) -> str:
    return f"{int(value or 0):,}円"


def option_map(items: list[dict[str, Any]], id_key: str, include_blank: bool = True) -> dict[str, str]:
    options = {"未設定": ""} if include_blank else {}
    for item in items:
        options[f"{item.get('name', '名称未設定')} [{item.get(id_key, '')}]"] = item.get(id_key, "")
    return options


def selected_label(options: dict[str, str], value: str | None) -> str:
    for label, item_id in options.items():
        if item_id == (value or ""):
            return label
    return next(iter(options))


def current_master_snapshot() -> MasterSnapshot:
    global MASTER_SNAPSHOT
    if MASTER_SNAPSHOT is None:
        MASTER_SNAPSHOT = MasterSnapshot.load(ROOT)
    return MASTER_SNAPSHOT


def master_selects(prefix: str, record: dict[str, Any], snapshot: MasterSnapshot | None = None) -> dict[str, str]:
    snapshot = snapshot if snapshot is not None else current_master_snapshot()
    def retain_saved(options: dict[str, str], key: str) -> dict[str, str]:
        value = record.get(key)
        if value and value not in options.values():
            options[f"保存済み（非表示・未登録） [{value}]"] = value
        return options

    def default_index(options: dict[str, str], key: str) -> int | None:
        widget_key = f"{prefix}_{key}"
        if widget_key in st.session_state:
            if st.session_state[widget_key] in options:
                return None
            st.session_state.pop(widget_key, None)
        return list(options).index(selected_label(options, record.get(f"{key}_id")))

    project_options = retain_saved(option_map(snapshot.active("projects"), "project_id"), "project_id")
    project_label = st.selectbox("プロジェクト", list(project_options.keys()), index=default_index(project_options, "project"), key=f"{prefix}_project")
    project_id = project_options[project_label]
    subject_options = retain_saved(option_map(snapshot.active("subjects", project_id=project_id), "subject_id"), "subject_id")
    subject_label = st.selectbox("科目", list(subject_options.keys()), index=default_index(subject_options, "subject"), key=f"{prefix}_subject")
    subject_id = subject_options[subject_label]
    material_options = retain_saved(option_map(snapshot.active("materials", project_id=project_id, subject_id=subject_id), "material_id"), "material_id")
    material_label = st.selectbox("教材", list(material_options.keys()), index=default_index(material_options, "material"), key=f"{prefix}_material")
    material_id = material_options[material_label]
    unit_options = retain_saved(option_map(snapshot.active("units", project_id=project_id, subject_id=subject_id, material_id=material_id), "unit_id"), "unit_id")
    unit_label = st.selectbox("単元", list(unit_options.keys()), index=default_index(unit_options, "unit"), key=f"{prefix}_unit")
    return {"project_id": project_id, "subject_id": subject_id, "material_id": material_id, "unit_id": unit_options[unit_label]}


def find_notebook(store: dict[str, Any], notebook_id: str) -> dict[str, Any]:
    notebooks = store.get("notebooks", [])
    return next((n for n in notebooks if n.get("id") == notebook_id), notebooks[0])


def find_section(notebook: dict[str, Any], section_id: str) -> dict[str, Any]:
    sections = notebook.get("sections", [])
    return next((s for s in sections if s.get("id") == section_id), sections[0])


def find_page(section: dict[str, Any], page_id: str) -> dict[str, Any]:
    pages = section.get("pages", [])
    return next((p for p in pages if p.get("note_id") == page_id or p.get("id") == page_id), pages[0])


def ensure_selection(store: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    notebook = find_notebook(store, st.session_state.get("note_notebook_id", store["notebooks"][0]["id"]))
    st.session_state.note_notebook_id = notebook["id"]
    section = find_section(notebook, st.session_state.get("note_section_id", notebook["sections"][0]["id"]))
    st.session_state.note_section_id = section["id"]
    page = find_page(section, st.session_state.get("note_page_id", section["pages"][0]["note_id"]))
    st.session_state.note_page_id = page["note_id"]
    return notebook, section, page


def new_page(title: str, note_id: str | None = None, *, existing_ids: set[str] | None = None) -> dict[str, Any]:
    note_id = note_id or generate_id("note", existing_ids=existing_ids)
    timestamp = now_text()
    return {
        "id": note_id,
        "note_id": note_id,
        "title": title,
        "body": "",
        "content": "",
        "tags": [],
        "created_at": timestamp,
        "updated_at": timestamp,
        "linked_study_session_ids": [],
        "linked_flashcard_ids": [],
        "linked_reminder_ids": [],
        "project_id": "",
        "subject_id": "",
        "material_id": "",
        "unit_id": "",
    }


def add_notebook(store: dict[str, Any], name: str, *, reserved_note_ids: set[str] | None = None) -> None:
    reserved_ids = collect_note_store_ids(store)
    reserved_ids["note"].update(reserved_note_ids or set())
    section_id = generate_id("section", existing_ids=reserved_ids["section"])
    note_id = generate_id("note", existing_ids=reserved_ids["note"])
    notebook = {"id": generate_id("notebook", existing_ids=reserved_ids["notebook"]), "name": name, "sections": [{"id": section_id, "name": "クイックメモ", "pages": [new_page("無題のページ", note_id)]}]}
    store["notebooks"].append(notebook)
    st.session_state.note_notebook_id = notebook["id"]
    st.session_state.note_section_id = section_id
    st.session_state.note_page_id = note_id


def add_section(store: dict[str, Any], notebook: dict[str, Any], name: str, *, reserved_note_ids: set[str] | None = None) -> None:
    reserved_ids = collect_note_store_ids(store)
    reserved_ids["note"].update(reserved_note_ids or set())
    section_id = generate_id("section", existing_ids=reserved_ids["section"])
    note_id = generate_id("note", existing_ids=reserved_ids["note"])
    notebook["sections"].append({"id": section_id, "name": name, "pages": [new_page("無題のページ", note_id)]})
    st.session_state.note_section_id = section_id
    st.session_state.note_page_id = note_id


def add_page(store: dict[str, Any], section: dict[str, Any], title: str, *, reserved_note_ids: set[str] | None = None) -> None:
    existing_ids = collect_note_store_ids(store)["note"] | (reserved_note_ids or set())
    page = new_page(title, existing_ids=existing_ids)
    section["pages"].append(page)
    st.session_state.note_page_id = page["note_id"]


def page_to_markdown(notebook: dict[str, Any], section: dict[str, Any], page: dict[str, Any]) -> str:
    tags = ", ".join(page.get("tags", []))
    names = current_master_snapshot().names(page)
    return (
        f"# {page.get('title', '無題')}\n\n"
        f"- Notebook: {notebook.get('name', '')}\n"
        f"- Section: {section.get('name', '')}\n"
        f"- note_id: {page.get('note_id', '')}\n"
        f"- Project: {names['project_name']}\n"
        f"- Subject: {names['subject_name']}\n"
        f"- Material: {names['material_name']}\n"
        f"- Unit: {names['unit_name']}\n"
        f"- Tags: {tags}\n"
        f"- Updated: {page.get('updated_at', '')}\n\n"
        f"{page.get('body') or page.get('content', '')}\n"
    )


def related_session_rows(
    page: dict[str, Any], references: ReferenceSnapshot | None = None,
) -> list[dict[str, Any]]:
    linked_ids = list(dict.fromkeys(str(item) for item in page.get("linked_study_session_ids", []) if item))
    if not linked_ids:
        return []
    references = references if references is not None else ReferenceSnapshot(ROOT)
    sessions_by_id = references.sessions_by_id
    rows = []
    for session_id in linked_ids:
        session = sessions_by_id.get(session_id)
        if not session:
            rows.append({"日付": "", "開始時刻": "", "終了時刻": "", "勉強時間": "", "勉強内容": "不明なセッション", "換算金額": "", "_sort_started_at": pd.NaT})
            continue
        start_at = pd.to_datetime(session.get("start_at"), errors="coerce")
        end_at = pd.to_datetime(session.get("end_at"), errors="coerce")
        rows.append(
            {
                "日付": "" if pd.isna(start_at) else start_at.date().isoformat(),
                "開始時刻": "" if pd.isna(start_at) else start_at.strftime("%H:%M:%S"),
                "終了時刻": "" if pd.isna(end_at) else end_at.strftime("%H:%M:%S"),
                "勉強時間": sec_to_hms(session.get("duration_sec", 0)),
                "勉強内容": session.get("memo", ""),
                "換算金額": yen(session.get("money", 0)),
                "_sort_started_at": start_at,
            }
        )
    return (
        pd.DataFrame(rows)
        .sort_values("_sort_started_at", ascending=False, kind="stable", na_position="last")
        .drop(columns="_sort_started_at")
        .to_dict(orient="records")
    )


def related_task_rows(
    page: dict[str, Any], references: ReferenceSnapshot | None = None,
) -> list[dict[str, Any]]:
    if not page.get("linked_study_session_ids"):
        return []
    references = references if references is not None else ReferenceSnapshot(ROOT)
    rows = []
    for session_id in page.get("linked_study_session_ids", []):
        for session in references.sessions_for_id(session_id):
            if session.get("linked_task_id"):
                task = references.tasks.get(session["linked_task_id"])
                rows.append({"タスク名": task.get("title", "不明なタスク") if task else "不明なタスク", "計画日": task.get("date", "") if task else "", "session_id": session_id})
    if not rows:
        return rows
    task_table = pd.DataFrame(rows)
    task_table["_sort_plan_date"] = pd.to_datetime(task_table["計画日"], errors="coerce")
    return (
        task_table.sort_values("_sort_plan_date", ascending=False, kind="stable", na_position="last")
        .drop(columns="_sort_plan_date")
        .to_dict(orient="records")
    )


def related_card_rows(page: dict[str, Any]) -> list[dict[str, Any]]:
    cards = {card.get("card_id"): card for card in load_all_flashcards(ROOT)}
    rows = []
    for card_id in page.get("linked_flashcard_ids", []):
        card = cards.get(card_id)
        if not card:
            rows.append({"問題文": "不明なカード", "解答": "", "タグ": "", "重要度": "", "要復習": "", "作成日時": ""})
            continue
        rows.append({"問題文": card.get("question") or card.get("front", ""), "解答": (card.get("answer") or card.get("back", ""))[:80], "タグ": " / ".join(card.get("tags", [])), "重要度": card.get("importance", "normal"), "要復習": "はい" if card.get("need_review") else "いいえ", "作成日時": card.get("created_at", "")})
    if not rows:
        return rows
    card_table = pd.DataFrame(rows)
    card_table["_sort_created_at"] = pd.to_datetime(card_table["作成日時"], errors="coerce")
    return (
        card_table.sort_values("_sort_created_at", ascending=False, kind="stable", na_position="last")
        .drop(columns="_sort_created_at")
        .to_dict(orient="records")
    )


def related_reminder_rows(page: dict[str, Any]) -> list[dict[str, Any]]:
    reminders = {item.get("reminder_id"): item for item in load_all_reminders(ROOT)}
    rows = []
    for reminder_id in page.get("linked_reminder_ids", []):
        item = reminders.get(reminder_id)
        if not item:
            rows.append({"タイトル": "不明なリマインダー", "復習予定日": "", "ステータス": "", "優先度": "", "メモ": ""})
            continue
        rows.append({"タイトル": item.get("title") or item.get("keyword", ""), "復習予定日": item.get("scheduled_date", ""), "ステータス": item.get("status", ""), "優先度": item.get("priority", ""), "メモ": item.get("memo", "")})
    return rows


def note_input_keys(note_id: str) -> list[str]:
    keys = [f"{prefix}_{note_id}" for prefix in ("title", "master_tags", "tags", "body")]
    keys += [f"note_{note_id}_{key}" for key in ("project", "subject", "material", "unit")]
    return keys


def note_input_default(key: str, value: Any) -> Any:
    return None if key in st.session_state else value


def clear_note_inputs(note_id: str) -> None:
    for key in note_input_keys(note_id):
        st.session_state.pop(key, None)
    st.session_state.setdefault("note_editor_drafts", {}).pop(note_id, None)


def preserve_active_note_inputs() -> None:
    note_id = st.session_state.get("note_editor_active_id")
    if note_id:
        draft = st.session_state.setdefault("note_editor_drafts", {}).setdefault(note_id, {})
        draft.update({key: deepcopy(st.session_state[key]) for key in note_input_keys(note_id) if key in st.session_state})


def restore_note_inputs(note_id: str) -> None:
    draft = st.session_state.setdefault("note_editor_drafts", {}).get(note_id, {})
    for key, value in draft.items():
        if key not in st.session_state:
            st.session_state[key] = deepcopy(value)
    st.session_state.note_editor_active_id = note_id


def protect_missing_note_draft(store: dict[str, Any]) -> None:
    """Do not let Streamlit's widget cleanup erase an externally deleted draft."""
    note_id = st.session_state.get("note_page_id")
    snapshot = st.session_state.get("note_editor_snapshots", {}).get(note_id)
    if snapshot is None or any(page.get("note_id") == note_id for notebook in store["notebooks"]
                               for section in notebook["sections"] for page in section["pages"]):
        return
    restore_note_inputs(note_id)
    st.warning("編集中のノートが別の操作で削除されています。未保存の入力はこの画面に保持しています。自動で別のノートへ保存しません。")
    page = snapshot["note"]
    st.text_input("タイトル", value=note_input_default(f"title_{note_id}", page.get("title", "")), key=f"title_{note_id}")
    st.text_area("本文", value=note_input_default(f"body_{note_id}", page.get("body") or page.get("content", "")), height=380, key=f"body_{note_id}")
    preserve_active_note_inputs()
    draft = st.session_state["note_editor_drafts"][note_id]
    st.caption("タイトル・本文をコピーできます。タグや分類を含む全入力は JSON で取り出せます。この一時保管は現在の画面セッション内だけです。")
    st.download_button("未保存入力を JSON 出力", json.dumps({"note_id": note_id, "saved_note": page, "draft_inputs": draft}, ensure_ascii=False, indent=2),
                       file_name=f"{note_id}_unsaved.json", mime="application/json", on_click="ignore")
    if st.button("未保存入力を破棄して別のノートへ移動"):
        st.session_state.note_editor_reset_inputs = note_id
        st.session_state["note_editor_snapshots"].pop(note_id, None)
        st.session_state.pop("note_page_id", None)
        st.session_state.pop("note_editor_active_id", None)
        st.rerun()
    st.stop()


def refresh_note_snapshot(note_id: str, *, discard_inputs: bool = False) -> None:
    """Only explicit reload or our own successful write may advance a draft."""
    workspace = load_note_workspace(ROOT)
    snapshots = st.session_state.setdefault("note_editor_snapshots", {})
    snapshots[note_id] = capture_note_snapshot(workspace, note_id)
    if discard_inputs:
        clear_note_inputs(note_id)
    st.session_state.pop("note_delete_plan", None)


def render_note_operation(plan: dict[str, Any], *, restore: bool = False, defer_rerun: bool = False) -> bool:
    st.warning("元のIDでノートと安全な関連付けを復元します。" if restore else "ノートをごみ箱へ移し、関連記録からリンクだけを外します。")
    if not restore:
        st.caption("ごみ箱へ保存するのは保存済みの内容です。未保存の入力は対象外です。")
    st.write(f"対象: {plan['note'].get('title') or '無題'} / 関連記録 {len(plan['references'])} 件")
    for warning in plan["warnings"]:
        st.caption(warning)
    if plan["references"]:
        labels = {"session": "勉強ログ", "task": "タスク", "plan": "日次計画", "card": "カード", "reminder": "復習予定"}
        counts = {}
        for reference in plan["references"]:
            label = labels.get(reference["scope"], reference["scope"])
            counts[label] = counts.get(label, 0) + 1
        st.dataframe([{"参照元": label, "件数": count} for label, count in counts.items()], hide_index=True)
    approved = st.checkbox("対象と関連付けの変更を確認しました", key=f"note_confirm_{plan['operation_id']}")
    action, cancel = st.columns(2)
    if action.button("復元を確定" if restore else "ごみ箱へ移動を確定", disabled=not approved, key=f"note_apply_{plan['operation_id']}"):
        try:
            result = commit_note_plan(plan, root=ROOT)
        except (ValueError, OSError) as exc:
            ab.show_action_error(str(exc))
            st.info("確認内容は保持しています。競合時はキャンセルして、最新内容を再読込した後に確認し直してください。")
        else:
            st.session_state.pop("note_restore_plan" if restore else "note_delete_plan", None)
            st.session_state.setdefault("note_editor_snapshots", {}).pop(result["note_id"], None)
            st.session_state.note_editor_reset_inputs = result["note_id"]
            if restore:
                st.session_state.note_notebook_id = result["notebook_id"]
                st.session_state.note_section_id = result["section_id"]
                st.session_state.note_page_id = result["note_id"]
            else:
                st.session_state.pop("note_page_id", None)
            ab.queue_success("ノートを復元しました。" if restore else "ノートをごみ箱へ移しました。関連記録の本体は保持しています。")
            if defer_rerun:
                return True
            st.rerun()
    if cancel.button("キャンセル", key=f"note_cancel_{plan['operation_id']}"):
        st.session_state.pop("note_restore_plan" if restore else "note_delete_plan", None)
        if defer_rerun:
            return True
        st.rerun()

    return False

def request_note_page_refresh() -> None:
    """Finish parent regions on full runs; refresh directly after partial runs."""
    if st.session_state.get("note_parent_rendering", False):
        st.session_state["note_page_refresh_requested"] = True
    else:
        st.rerun()


def render_note_linked_forms(snapshot: dict[str, Any], master_snapshot: MasterSnapshot) -> None:
    """Keep sibling forms outside the editor fragment so drafts survive edits."""
    page = deepcopy(snapshot["note"])
    note_id = page["note_id"]
    snapshots = st.session_state.setdefault("note_editor_snapshots", {})
    today = app_today()
    with st.expander("ノートから単語カードを作成", expanded=False):
        with st.form(f"card_from_note_{page['note_id']}"):
            question = st.text_input("問題文")
            answer = st.text_area("解答", height=100)
            explanation = st.text_area("解説", height=80)
            card_tags = st.text_input("タグ（カンマ区切り）", value=", ".join(page.get("tags", [])))
            importance = st.selectbox("重要度", IMPORTANCE_OPTIONS, index=1)
            need_review = st.checkbox("要復習", value=True)
            card_master = master_selects(f"card_{page['note_id']}", page, master_snapshot)
            if st.form_submit_button("単語カードを保存", type="primary"):
                op_key = f"note_card_operation_{note_id}"
                try:
                    created = create_note_linked_record(snapshot, "card", {
                        "question": question, "answer": answer, "front": question, "back": answer,
                        "explanation": explanation, "tags": [tag.strip() for tag in card_tags.split(",") if tag.strip()],
                        "importance": importance, "need_review": need_review, "important": need_review, **card_master,
                    }, operation_id=st.session_state.setdefault(op_key, uuid4().hex), root=ROOT)
                except (ValueError, OSError) as exc:
                    ab.show_action_error(str(exc))
                else:
                    st.session_state.pop(op_key, None)
                    if created["snapshot"] is not None:
                        snapshots[note_id] = created["snapshot"]
                    ab.queue_success("単語カードを作成しました。")
                    st.session_state["note_page_refresh_requested"] = True

    with st.expander("ノートから復習リマインダーを作成", expanded=False):
        with st.form(f"reminder_from_note_{page['note_id']}"):
            scheduled_date = st.date_input("復習予定日", value=today)
            reminder_title = st.text_input("タイトル", value=f"{page.get('title', 'ノート')}を復習")
            memo = st.text_area("メモ", height=80)
            review_type = st.selectbox("復習タイプ", ["note_review", "card_review", "exam_review", "other"])
            priority = st.selectbox("優先度", PRIORITY_OPTIONS, index=1)
            schedule_mode_label = st.selectbox(
                "復習方式",
                ["単発", "標準間隔", "重点間隔", "適応型（SM-2）"],
                index=1,
            )
            reminder_master = master_selects(f"reminder_{page['note_id']}", page, master_snapshot)
            if st.form_submit_button("復習予定を保存", type="primary"):
                timestamp = now_text()
                schedule_mode = {
                    "単発": "single",
                    "標準間隔": "standard",
                    "重点間隔": "intensive",
                    "適応型（SM-2）": "sm2",
                }[schedule_mode_label]
                op_key = f"note_reminder_operation_{note_id}"
                try:
                    created = create_note_linked_record(snapshot, "reminder", {
                        "source_type": "note",
                        "source_note_id": page["note_id"],
                        "linked_note_ids": [page["note_id"]],
                        "title": reminder_title,
                        "keyword": reminder_title,
                        "memo": memo,
                        "review_type": review_type,
                        "priority": priority,
                        "scheduled_date": scheduled_date.isoformat(),
                        "registered": today.isoformat(),
                        "status": "pending",
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        **reminder_master,
                    }, schedule_mode=schedule_mode,
                        operation_id=st.session_state.setdefault(op_key, uuid4().hex), root=ROOT)
                except (ValueError, OSError) as exc:
                    ab.show_action_error(str(exc))
                else:
                    st.session_state.pop(op_key, None)
                    if created["snapshot"] is not None:
                        snapshots[note_id] = created["snapshot"]
                    ab.queue_success("復習リマインダーを作成しました。")
                    st.session_state["note_page_refresh_requested"] = True


@demo_fragment
def render_note_editor(note_id: str) -> None:
    """Refresh only this editor and preview; writes retain the original guards."""
    global MASTER_SNAPSHOT
    preserve_active_note_inputs()
    workspace = load_note_workspace(ROOT)
    if workspace["revision"] is None:
        workspace = st.session_state.setdefault("note_initial_workspace", workspace)
    else:
        st.session_state.pop("note_initial_workspace", None)
    current_store = deepcopy(workspace["store"])
    protect_missing_note_draft(current_store)
    selection = next(
        ((notebook, section, page)
         for notebook in current_store["notebooks"]
         for section in notebook["sections"]
         for page in section["pages"] if page.get("note_id") == note_id),
        None,
    )
    if selection is None:
        st.warning("選択したノートは別の操作で削除されています。ページを再読込してください。")
        return
    notebook, section, page = selection
    master_snapshot = MasterSnapshot.load(ROOT)
    MASTER_SNAPSHOT = master_snapshot
    refresh_requested = False
    editor_frame = st.empty()
    with editor_frame.container():
        center, right = st.columns([2.1, 1.2])
        with center:
            st.subheader("編集")
            snapshots = st.session_state.setdefault("note_editor_snapshots", {})
            note_id = page["note_id"]
            if note_id not in snapshots:
                snapshots[note_id] = capture_note_snapshot(workspace, note_id)
            snapshot = snapshots[note_id]
            restore_note_inputs(note_id)
            latest_page = page
            page = deepcopy(snapshot["note"])
            if snapshot["revision"] != workspace["revision"]:
                st.warning("編集開始後にノートストアが更新されています。入力は保持されています。保存は競合として停止します。")
                with st.expander("保存済みの最新内容を確認"):
                    st.write(latest_page.get("title", ""))
                    st.text(latest_page.get("body") or latest_page.get("content", ""))
            if st.button("最新内容を再読込（このノートの未保存入力を破棄）", key=f"note_reload_{note_id}"):
                refresh_note_snapshot(note_id, discard_inputs=True)
                snapshot = snapshots[note_id]
                page = deepcopy(snapshot["note"])
                refresh_requested = True
            title = st.text_input("タイトル", value=note_input_default(f"title_{note_id}", page.get("title", "")), key=f"title_{note_id}")
            tag_candidates = [tag.get("name", "") for tag in master_snapshot.active("tags")]
            tag_candidates = list(dict.fromkeys([*tag_candidates, *st.session_state.get(f"master_tags_{note_id}", [])]))
            selected_master_tags = st.multiselect("マスタタグ", options=tag_candidates, default=note_input_default(f"master_tags_{note_id}", [tag for tag in page.get("tags", []) if tag in tag_candidates]), key=f"master_tags_{note_id}")
            tags_text = st.text_input("タグ（カンマ区切り）", value=note_input_default(f"tags_{note_id}", ", ".join([tag for tag in page.get("tags", []) if tag not in tag_candidates])), key=f"tags_{note_id}")
            master_values = master_selects(f"note_{page['note_id']}", page, master_snapshot)
            refresh_requested = render_master_quick_add(
                ROOT, f"note_master_{page['note_id']}", snapshot=master_snapshot, defer_rerun=True,
            ) or refresh_requested
            body_value = page.get("body") or page.get("content", "")
            body = st.text_area("本文", value=note_input_default(f"body_{note_id}", body_value), height=380, key=f"body_{note_id}")
            col_save, col_delete, col_export = st.columns(3)
            if col_save.button("保存", type="primary", width="stretch"):
                free_tags = [tag.strip() for tag in tags_text.split(",") if tag.strip()]
                try:
                    saved_snapshot = save_note_edits(snapshot, {"title": title.strip() or "無題のページ", "body": body, "content": body,
                                                               "tags": sorted(set(selected_master_tags + free_tags)), **master_values}, root=ROOT)
                except (ValueError, OSError) as exc:
                    ab.show_action_error(str(exc))
                else:
                    snapshots[note_id] = saved_snapshot
                    st.session_state.pop("note_delete_plan", None)
                    ab.queue_success("ノートを保存しました。")
                    refresh_requested = True
            if col_delete.button("削除", width="stretch", disabled=len(section["pages"]) <= 1):
                try:
                    st.session_state.note_delete_plan = prepare_note_delete(snapshot, root=ROOT)
                except (ValueError, OSError) as exc:
                    ab.show_action_error(str(exc))
            operation_slot = st.empty()
            if st.session_state.get("note_delete_plan"):
                with operation_slot.container():
                    refresh_requested = render_note_operation(st.session_state.note_delete_plan, defer_rerun=True) or refresh_requested
            col_export.download_button("Markdown出力", page_to_markdown(notebook, section, page).encode("utf-8-sig"), file_name=f"{page.get('title', 'note')}.md", mime="text/markdown", width="stretch", on_click="ignore")


        with right:
            names = master_snapshot.names(page)
            ab.card(
                "メタ情報",
                "\n".join(
                    [
                        f"プロジェクト: {names['project_name']}",
                        f"科目: {names['subject_name']}",
                        f"教材: {names['material_name']}",
                        f"単元: {names['unit_name']}",
                        f"タグ: {' / '.join(page.get('tags', [])) or '未設定'}",
                    ]
                ),
            )
            ab.card("プレビュー", "保存前の本文もここで確認できます。")
            st.markdown(body or "本文はまだありません。")
            st.divider()
            st.caption(f"note_id: {page.get('note_id', '')}")
            st.caption(f"作成: {page.get('created_at', '')}")
            st.caption(f"更新: {page.get('updated_at', '')}")
            st.caption("変更は、このブラウザの体験用データに保存されます。")

    preserve_active_note_inputs()
    if refresh_requested:
        request_note_page_refresh()


st.set_page_config(page_title="ノート・知識ベース", layout="wide")
preserve_active_note_inputs()
if st.session_state.get("note_editor_reset_inputs"):
    clear_note_inputs(st.session_state.pop("note_editor_reset_inputs"))
user_settings = load_user_settings()
ab.inject_css(settings=user_settings)
ab.sidemenu("ノート、セクション、ページを整理できます。", settings=user_settings)

workspace = load_note_workspace(ROOT)
if workspace["revision"] is None:
    # Read-time default IDs must survive reruns until the first explicit save.
    workspace = st.session_state.setdefault("note_initial_workspace", workspace)
else:
    st.session_state.pop("note_initial_workspace", None)
store = deepcopy(workspace["store"])
protect_missing_note_draft(store)
notebook, section, page = ensure_selection(store)
# Capture the edit baseline from the read that supplied this selected page.
snapshots = st.session_state.setdefault("note_editor_snapshots", {})
if page["note_id"] not in snapshots:
    snapshots[page["note_id"]] = capture_note_snapshot(workspace, page["note_id"])

with st.sidebar:
    st.subheader("ノートブック")
    notebook_options = {n["id"]: f"{n['name']} [{n['id']}]" for n in store["notebooks"]}
    selected_notebook_id = st.selectbox("ノートブック選択", list(notebook_options), index=list(notebook_options).index(notebook["id"]), format_func=notebook_options.get)
    if selected_notebook_id != notebook["id"]:
        st.session_state.note_notebook_id = selected_notebook_id
        st.session_state.pop("note_section_id", None)
        st.session_state.pop("note_page_id", None)
        st.rerun()
    new_notebook_name = st.text_input("新しいノートブック名", key="new_notebook_name")
    if st.button("ノートブック追加") and new_notebook_name.strip():
        try:
            add_notebook(store, new_notebook_name.strip(), reserved_note_ids=reserved_note_ids(ROOT))
            save_note_workspace(workspace, store, root=ROOT)
        except (ValueError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            ab.queue_success("ノートブックを追加しました。")
            st.rerun()

    st.subheader("セクション")
    section_options = {s["id"]: f"{s['name']} [{s['id']}]" for s in notebook["sections"]}
    selected_section_id = st.radio("セクション選択", list(section_options), index=list(section_options).index(section["id"]), format_func=section_options.get)
    if selected_section_id != section["id"]:
        st.session_state.note_section_id = selected_section_id
        st.session_state.pop("note_page_id", None)
        st.rerun()
    new_section_name = st.text_input("新しいセクション名", key="new_section_name")
    if st.button("セクション追加") and new_section_name.strip():
        try:
            add_section(store, notebook, new_section_name.strip(), reserved_note_ids=reserved_note_ids(ROOT))
            save_note_workspace(workspace, store, root=ROOT)
        except (ValueError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            ab.queue_success("セクションを追加しました。")
            st.rerun()

ab.topbar("ノート・知識ベース", "学習メモ、調査ログ、復習候補をノートブック形式で整理します。")

left, editor_area = st.columns([1.1, 3.3])

with left:
    ab.card("ページ一覧", f"{notebook['name']} / {section['name']}")
    query = st.text_input("ページ検索", placeholder="タイトル・本文・タグ")
    new_page_title = st.text_input("新しいページ名", value="無題のページ")
    if st.button("ページ追加", type="primary"):
        try:
            add_page(store, section, new_page_title.strip() or "無題のページ", reserved_note_ids=reserved_note_ids(ROOT))
            save_note_workspace(workspace, store, root=ROOT)
        except (ValueError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            ab.queue_success("ページを追加しました。")
            st.rerun()
    pages = section["pages"]
    if query:
        q = query.lower()
        pages = [p for p in pages if q in p.get("title", "").lower() or q in (p.get("body") or p.get("content", "")).lower() or q in " ".join(p.get("tags", [])).lower()]
    for index, candidate in enumerate(sorted(pages, key=lambda p: p.get("updated_at", ""), reverse=True)):
        label = candidate.get("title") or "無題のページ"
        if st.button(label, key=f"open_{candidate.get('note_id', candidate.get('id', index))}_{index}", width="stretch"):
            st.session_state.note_page_id = candidate["note_id"]
            st.rerun()

with editor_area:
    selected_snapshot = snapshots[page["note_id"]]
    st.session_state["note_parent_rendering"] = True
    try:
        render_note_editor(page["note_id"])
    finally:
        st.session_state["note_parent_rendering"] = False
    related_snapshot = snapshots.get(page["note_id"], selected_snapshot)
    related_page = deepcopy(related_snapshot["note"])
    related_center, _related_right = st.columns([2.1, 1.2])
    with related_center:
        st.subheader("関連する勉強セッション")
        related_references = ReferenceSnapshot(ROOT)
        rows = related_session_rows(related_page, related_references)
        if rows:
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            st.info("関連する勉強セッションはまだありません。")

        st.subheader("関連する学習タスク")
        task_rows = related_task_rows(related_page, related_references)
        if task_rows:
            st.dataframe(pd.DataFrame(task_rows), width="stretch", hide_index=True)
        else:
            st.info("関連する学習タスクはまだありません。")

        render_note_linked_forms(related_snapshot, current_master_snapshot())

        st.subheader("関連する単語カード")
        card_rows = related_card_rows(related_page)
        if card_rows:
            st.dataframe(pd.DataFrame(card_rows), width="stretch", hide_index=True)
        else:
            st.info("関連する単語カードはまだありません。")

        st.subheader("関連する復習リマインダー")
        reminder_rows = related_reminder_rows(related_page)
        if reminder_rows:
            st.dataframe(pd.DataFrame(reminder_rows), width="stretch", hide_index=True)
        else:
            st.info("関連する復習リマインダーはまだありません。")


st.divider()
with st.expander("ノートのごみ箱・復元", expanded=False):
    try:
        deleted_notes = list_deleted_notes(ROOT)
    except (ValueError, OSError) as exc:
        st.error(str(exc))
        deleted_notes = []
    if not deleted_notes:
        st.caption("ごみ箱に未復元のノートはありません。")
    else:
        trash_options = {item["trash_id"]: f"{item['note'].get('title') or '無題'} / {item.get('deleted_at', '')} [{item['trash_id'][:8]}]" for item in deleted_notes}
        trash_id = st.selectbox("復元するノート", list(trash_options), format_func=trash_options.get)
        if st.button("復元内容を確認"):
            try:
                st.session_state.note_restore_plan = prepare_note_restore(trash_id, root=ROOT)
            except (ValueError, OSError) as exc:
                ab.show_action_error(str(exc))
        if st.session_state.get("note_restore_plan"):
            render_note_operation(st.session_state.note_restore_plan, restore=True)

if st.session_state.pop("note_page_refresh_requested", False):
    st.rerun()
