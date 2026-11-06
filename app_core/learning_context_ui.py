"""Explicit local editing and read-only resume cards for material resources."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import streamlit as st
import prolog_ui.components as ab

from app_core.analysis_dates import parse_analysis_datetime
from app_core.json_operations import operation_guard
from app_core.json_store import JsonStoreError
from app_core.learning_context_service import (
    MAX_LINKS, MAX_NEXT_STEP_LENGTH, MAX_TITLE_LENGTH, MAX_URL_LENGTH,
    LearningContextError, list_materials, load_context_snapshot, load_learning_context, save_material_context,
)


def _read(root: Path, *, master_data=None):
    with operation_guard(root):
        data = load_learning_context(root)
        materials = list_materials(root) if master_data is None else list_materials(root, master_data=master_data)
        return data, materials


def _show_entry(entry: dict, label: str, *, missing: bool = False, inactive: bool = False) -> None:
    st.text(label)
    if missing:
        st.warning("教材マスタでは削除済みです。保存済みのメモ・資料リンクは残しています。")
    elif inactive:
        st.caption("この教材は無効になっています。引き継ぎ情報は残しています。")
    if entry.get("next_step"):
        st.text(entry["next_step"])
    for link in entry.get("links", []):
        st.link_button(link.get("title") or "Google資料を開く", link["url"])
    st.caption(f"更新: {entry.get('updated_at', '')}")


def render_resume_cards(root: Path, max_items: int = 5, key_prefix: str = "learning_resume") -> None:
    """Render local notes and safe hrefs; no fetch, source write, or task creation."""
    if type(max_items) is not int or max_items < 1:
        return
    try:
        data, materials = _read(root)
    except (JsonStoreError, OSError) as exc:
        st.error(f"引き継ぎ情報を読み込めません。{exc}")
        return
    lookup = {item["material_id"]: item for item in materials}
    entries = [entry for entry in data["materials"].values() if entry["next_step"] or entry["links"]]
    entries.sort(key=lambda entry: parse_analysis_datetime(entry["updated_at"]), reverse=True)
    if not entries:
        st.caption("教材の『次はここから』はまだありません。マスタ管理の『資料リンク・引き継ぎ』で登録できます。")
        return
    for entry in entries[:max_items]:
        item = lookup.get(entry["material_id"], {})
        label = item.get("label", f"削除済み教材 [{entry['material_id']}]")
        with st.expander(label, expanded=True):
            _show_entry(entry, label, missing=not item, inactive=bool(item) and not item["is_active"])


def _new_row(link: dict | None = None) -> dict:
    return {"widget_id": uuid4().hex, "link": deepcopy(link or {}), "delete": False}


def _prepare_reload(root: Path, edit: dict) -> None:
    with operation_guard(root):
        snapshot = load_context_snapshot(root)
        materials = list_materials(root)
    edit["reload"] = {"snapshot": snapshot, "materials": materials, "token": uuid4().hex}


def _reload_controls(edit: dict, prefix: str) -> tuple[bool, bool]:
    reload = edit.get("reload")
    if not reload:
        return False, False
    st.caption("再読込する保存内容の比較（まだ保存・上書きはしません）")
    identifier = edit["material_id"]
    for column, data, label in zip(st.columns(2), (edit["snapshot"], reload["snapshot"]), ("編集開始時", "最新の保存内容")):
        with column:
            st.caption(label)
            st.text(json.dumps(data["data"]["materials"].get(identifier), ensure_ascii=False, indent=2))
    latest_item = next((item for item in reload["materials"] if item["material_id"] == identifier), None)
    st.text(latest_item["label"] if latest_item else "教材は削除済みです。入力を保持しますが保存できません。")
    st.caption("入力は保持し、他画面で追加されたリンクも残します。削除されたリンクは自動復活せず、除外または新規追加の判断が必要です。読み直した後の保存には再操作が必要です。")
    approved = st.checkbox("最新との差を確認し、入力を保持して読み直します", key=f"{prefix}_reload_approve_{reload['token']}")
    requested = st.form_submit_button("入力を保持して最新を再読込")
    return approved, requested


def _apply_reload(root: Path, edit: dict) -> None:
    reload = edit["reload"]
    latest = load_context_snapshot(root)
    if latest["revision"] != reload["snapshot"]["revision"]:
        raise LearningContextError("比較後に保存内容が変わりました。入力を保持したまま、最新との差をもう一度確認してください。")
    current = latest["data"]["materials"].get(edit["material_id"], {})
    links = {item["link_id"]: item for item in current.get("links", [])}
    seen = set()
    for row in edit["rows"]:
        identifier = row["link"].get("link_id")
        if not identifier:
            continue
        seen.add(identifier)
        row["missing"] = identifier not in links
        row["restore"] = False
        if identifier in links:
            row["link"] = {**deepcopy(links[identifier]), "title": row["link"]["title"], "url": row["link"]["url"]}
    edit["rows"].extend(_new_row(item) for identifier, item in links.items() if identifier not in seen)
    edit.update(snapshot=latest, materials=reload["materials"], version=uuid4().hex)
    item = next((item for item in reload["materials"] if item["material_id"] == edit["material_id"]), None)
    if item:
        edit["label"] = item["label"]
    edit.pop("reload", None)


def render_learning_context(root: Path, key_prefix: str = "learning_context", *, master_data=None) -> None:
    st.subheader("教材の資料リンク・引き継ぎ")
    st.caption("Google資料のリンク先本文は取得しません。ここで保存するメモはローカル専用です。")
    edit_key = key_prefix + "_edit"
    choice_key = key_prefix + "_material"
    notice = st.session_state.pop(key_prefix + "_notice", None)
    if notice:
        ab.show_action_message(notice)
    edit = st.session_state.get(edit_key)
    read_error = None
    try:
        data, materials = _read(root) if master_data is None else _read(root, master_data=master_data)
    except (JsonStoreError, OSError) as exc:
        read_error = exc
        st.error(f"保存済み情報を読み込めません。入力は破棄していません。{exc}")
        if edit is None:
            return
        data, materials = edit["snapshot"]["data"], edit["materials"]
    lookup = {item["material_id"]: item for item in materials}
    if edit is None:
        labels = {identifier: item["label"] for identifier, item in lookup.items()}
        for identifier in data["materials"]:
            labels.setdefault(identifier, f"削除済み教材 [{identifier}]")
        with st.form(key_prefix + "_select_form"):
            selected = st.selectbox("編集する教材", [""] + list(labels),
                                    format_func=lambda value: labels.get(value, "教材を選択してください"), key=choice_key)
            opened = st.form_submit_button("この教材を開く")
        if opened and selected:
            try:
                snapshot = load_context_snapshot(root)
                original = snapshot["data"]["materials"].get(selected, {})
                st.session_state[edit_key] = {
                    "material_id": selected, "label": labels[selected], "snapshot": snapshot, "materials": materials,
                    "version": uuid4().hex, "rows": [_new_row(link) for link in original.get("links", [])] or [_new_row()],
                    "next_step": original.get("next_step", ""),
                }
                st.rerun()
            except (JsonStoreError, OSError) as exc:
                ab.show_action_error(f"教材を開けませんでした。{exc}", toast_message="教材を開けませんでした。画面の案内を確認してください。")
        if data["materials"]:
            with st.expander("保存済みの引き継ぎ一覧"):
                for identifier, entry in sorted(data["materials"].items(), key=lambda pair: parse_analysis_datetime(pair[1]["updated_at"]), reverse=True):
                    item = lookup.get(identifier, {})
                    _show_entry(entry, item.get("label", f"削除済み教材 [{identifier}]"),
                                missing=not item, inactive=bool(item) and not item["is_active"])
        return
    identifier, version = edit["material_id"], edit["version"]
    item = lookup.get(identifier)
    st.text(edit["label"])
    if item is None:
        st.warning("教材は削除済みのため保存できません。メモ・リンクと編集中の入力は残しています。必要な内容をコピーしてください。")
    elif not item["is_active"]:
        st.info("無効な教材です。教材マスタを変更せず、引き継ぎ情報だけを編集できます。")
    original = edit["snapshot"]["data"]["materials"].get(identifier, {})
    with st.form(key_prefix + "_editor_" + version):
        next_step = st.text_area("次はここから", value=edit["next_step"], max_chars=MAX_NEXT_STEP_LENGTH,
                                 key=f"{key_prefix}_next_{version}")
        draft_rows = []
        for index, row in enumerate(edit["rows"], 1):
            link = row["link"]
            widget = f"{key_prefix}_{version}_{row['widget_id']}"
            st.caption(f"資料リンク {index}")
            title = st.text_input("リンク名", value=link.get("title", ""), max_chars=MAX_TITLE_LENGTH, key=widget + "_title")
            url = st.text_input("Google資料のURL", value=link.get("url", ""), max_chars=MAX_URL_LENGTH, key=widget + "_url")
            deleted = st.checkbox("このリンクを削除する", value=row["delete"], key=widget + "_delete")
            restore = False
            if row.get("missing"):
                st.warning("このリンクは他の画面で削除済みです。入力は残しています。除外するか、新しいリンクとして登録し直すことを確認してください。")
                restore = st.checkbox("削除済みリンクを新しいリンクとして登録し直す", key=widget + "_restore")
            draft_rows.append({**row, "link": {**link, "title": title, "url": url}, "delete": deleted, "restore": restore})
        confirm_delete = st.checkbox("選択した資料リンクの削除を確認しました", key=f"{key_prefix}_confirm_links_{version}")
        confirm_clear = st.checkbox("『次はここから』を空欄にして消すことを確認しました", key=f"{key_prefix}_confirm_clear_{version}")
        confirmed_discard = st.checkbox("編集中の入力を破棄して教材選択へ戻る", key=f"{key_prefix}_confirm_discard_{version}")
        columns = st.columns(3)
        save = columns[0].form_submit_button("引き継ぎを保存", type="primary", disabled=item is None or read_error is not None)
        add = columns[1].form_submit_button("リンク入力欄を追加", disabled=len(edit["rows"]) >= MAX_LINKS)
        discard = columns[2].form_submit_button("入力を破棄して戻る")
        compare = st.form_submit_button("最新の保存内容との差を確認", disabled=read_error is not None)
        reload_approved, reload_requested = _reload_controls(edit, key_prefix)
    if not (save or add or discard or compare or reload_requested):
        return
    edit.update(rows=draft_rows, next_step=next_step)
    if compare or reload_requested:
        try:
            if compare:
                _prepare_reload(root, edit)
            elif reload_approved:
                _apply_reload(root, edit)
                st.session_state[key_prefix + "_notice"] = "入力を保持して最新の状態を読み直しました。内容を確認してから、改めて保存してください。"
            else:
                raise LearningContextError("最新との差を確認するチェックが必要です。入力は保持しています。")
            st.rerun()
        except (JsonStoreError, OSError, ValueError) as exc:
            ab.show_action_error(f"再読込できませんでした。入力は保持しています。{exc}",
                                 toast_message="再読込できませんでした。入力は保持しています。")
        return
    if discard:
        if not confirmed_discard:
            ab.show_action_error("入力を破棄する確認にチェックしてください。入力は残しています。",
                                 toast_message="入力を破棄する確認が必要です。入力は残しています。")
            return
    elif add:
        edit["rows"].append(_new_row())
        st.rerun()
    elif save:
        try:
            if any(row["delete"] and row["link"].get("link_id") for row in draft_rows) and not confirm_delete:
                raise LearningContextError("資料リンクの削除を確認してください。入力は残しています。")
            if original.get("next_step", "").strip() and not next_step.strip() and not confirm_clear:
                raise LearningContextError("『次はここから』を空欄にする確認が必要です。入力は残しています。")
            if any(row.get("missing") and not row["delete"] and not row["restore"] for row in draft_rows):
                raise LearningContextError("削除済みリンクの除外または新規追加を確認してください。自動復活はしません。")
            links = [{key: value for key, value in row["link"].items() if not row.get("missing") or key in {"title", "url"}}
                     for row in draft_rows if not row["delete"] and (row["link"].get("link_id") or row["link"].get("title") or row["link"].get("url"))]
            save_material_context(root, identifier, links, next_step, expected_revision=edit["snapshot"]["revision"])
            st.session_state[key_prefix + "_notice"] = f"{edit['label']} の引き継ぎを保存しました。編集対象を未選択に戻しました。"
        except (JsonStoreError, OSError, ValueError) as exc:
            ab.show_action_error(f"保存できませんでした。入力は保持しています。{exc}",
                                 toast_message="引き継ぎを保存できませんでした。入力は保持しています。")
            return
    st.session_state.pop(edit_key, None)
    st.session_state[choice_key] = ""
    st.rerun()
