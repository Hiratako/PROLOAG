"""Shared quick-add and master editor for study categories."""
from __future__ import annotations

from pathlib import Path
from hashlib import sha256
import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.form_snapshot import discard_form_snapshot, form_snapshot, advance_form_snapshots_after_own_patch
from app_core.json_store import observed_json_revision
from app_core.study_category_service import category_catalog_path, load_study_category_catalog, save_study_category


def _advance_owned_save(root: Path, edit: dict) -> None:
    path = category_catalog_path(root)
    own_revision = observed_json_revision(path)
    current = load_study_category_catalog(root)
    if observed_json_revision(path) == own_revision:
        advance_form_snapshots_after_own_patch(st.session_state, path,
            before_revision=edit["revisions"][path], after_revision=own_revision, updates=current)


def _scope(root: Path, prefix: str) -> str:
    return prefix + "_" + sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:10]


def render_study_category_quick_add(root: Path, key_prefix: str, *, defer_rerun: bool = False) -> bool:
    key_prefix = _scope(root, key_prefix)
    st.caption("名称変更・無効化は「マスタ管理」→「勉強カテゴリ」で行えます。")
    path = category_catalog_path(root)
    catalog = load_study_category_catalog(root)
    key = f"{key_prefix}_category_add_baseline"
    edit = form_snapshot(st.session_state, key, [path], catalog)
    if st.button("入力を破棄してカテゴリを再読込", key=f"{key_prefix}_category_reload"):
        discard_form_snapshot(st.session_state, key)
        st.rerun()
    with st.form(f"{key_prefix}_category_add_{edit['version']}"):
        name = st.text_input("勉強カテゴリ名", key=f"{key_prefix}_category_name_{edit['version']}", max_chars=80)
        submitted = st.form_submit_button("勉強カテゴリを追加", type="primary")
    if not submitted:
        return False
    try:
        save_study_category(root, name, expected_revision=edit["revisions"][path])
    except (ValueError, OSError) as exc:
        ab.show_action_error(f"勉強カテゴリを追加できませんでした。入力は保持しています。{exc}")
        return False
    _advance_owned_save(root, edit)
    discard_form_snapshot(st.session_state, key)
    ab.queue_success("勉強カテゴリを追加しました。")
    if not defer_rerun:
        st.rerun()
    return True


def render_study_category_management(root: Path, key_prefix: str = "master_categories") -> None:
    key_prefix = _scope(root, key_prefix)
    path = category_catalog_path(root)
    catalog = load_study_category_catalog(root)
    entries = {item["category_id"]: item for item in catalog["categories"]}
    st.caption("名称の変更後も旧名称の記録を同じカテゴリとして集計します。無効にすると新しい記録の候補から外れ、過去の記録は残ります。")
    if st.session_state.get(f"{key_prefix}_selected", "") not in {"", *entries}:
        st.session_state[f"{key_prefix}_selected"] = ""
    selected = st.selectbox("編集する勉強カテゴリ", ["", *entries], key=f"{key_prefix}_selected",
                            format_func=lambda value: "新しいカテゴリを追加" if not value else entries[value]["name"] + ("（無効）" if not entries[value]["is_active"] else ""))
    key = f"{key_prefix}_{selected or 'new'}_baseline"
    edit = form_snapshot(st.session_state, key, [path], catalog)
    current = next((item for item in edit["data"]["categories"] if item["category_id"] == selected), {})
    if st.button("入力を破棄してカテゴリを再読込", key=f"{key_prefix}_reload"):
        discard_form_snapshot(st.session_state, key)
        st.rerun()
    with st.form(f"{key_prefix}_{selected or 'new'}_{edit['version']}"):
        name = st.text_input("勉強カテゴリ名", value=current.get("name", ""), max_chars=80)
        active = st.checkbox("新しい記録の選択肢に表示する", value=current.get("is_active", True))
        submitted = st.form_submit_button("勉強カテゴリを保存", type="primary")
    if submitted:
        try:
            save_study_category(root, name, category_id=selected, is_active=active,
                                expected_revision=edit["revisions"][path])
        except (ValueError, OSError) as exc:
            ab.show_action_error(f"勉強カテゴリを保存できませんでした。入力は保持しています。{exc}")
        else:
            _advance_owned_save(root, edit)
            discard_form_snapshot(st.session_state, key)
            ab.queue_success("勉強カテゴリを保存しました。")
            st.rerun()
    st.dataframe(pd.DataFrame([{"勉強カテゴリ": item["name"], "状態": "有効" if item["is_active"] else "無効",
                                "旧名称": " / ".join(item["aliases"])} for item in catalog["categories"]]), hide_index=True, width="stretch")
