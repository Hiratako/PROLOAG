from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.master_snapshot import MasterSnapshot
from app_core.json_store import load_json_safe
from data_utils import (
    default_master_data,
    generate_master_id,
    load_master_data,
    master_path,
    now_text,
    normalize_master_data,
    save_master_data,
)


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

from config import get_data_root

ROOT = get_data_root()
RAW_MASTER = load_json_safe(master_path(ROOT), default_master_data())
MASTER_SNAPSHOT = MasterSnapshot(normalize_master_data(deepcopy(RAW_MASTER))[0])
MATERIAL_TYPES = ["講義", "問題集", "過去問", "論文", "参考書", "自作資料", "その他"]


def option_map(items: list[dict[str, Any]], id_key: str, include_blank: bool = True) -> dict[str, str]:
    options = {"未設定": ""} if include_blank else {}
    for item in items:
        state = "（無効）" if not item.get("is_active", True) else ""
        options[f"{item.get('name', '名称未設定')} [{item.get(id_key, '')}]{state}"] = item.get(id_key, "")
    return options


def selected_label(options: dict[str, str], value: str | None) -> str:
    for label, item_id in options.items():
        if item_id == (value or ""):
            return label
    return next(iter(options))


def upsert_item(collection: str, id_key: str, item: dict[str, Any], *, original: dict[str, Any] | None = None) -> None:
    from app_core.master_edit_service import validate_classification

    data = load_master_data(ROOT)
    if original is not None:
        validate_classification(data, collection, item, original)
    items = data.setdefault(collection, [])
    timestamp = now_text()
    if item.get(id_key):
        for existing in items:
            if existing.get(id_key) == item[id_key]:
                existing.update(item)
                existing["updated_at"] = timestamp
                break
    else:
        existing_ids = {entry.get(id_key, "") for entry in items}
        item[id_key] = generate_master_id(id_key.removesuffix("_id"), existing_ids)
        item["created_at"] = timestamp
        item["updated_at"] = timestamp
        items.append(item)
    save_master_data(data, ROOT)


def reference_select(label: str, collection: str, id_key: str, value: str, *,
                     key: str, parents: dict[str, str] | None = None, exclude_id: str = "") -> str:
    from app_core.master_edit_service import reference_choices

    options = reference_choices(MASTER_SNAPSHOT.data, collection, id_key, value,
                                parents=parents, exclude_id=exclude_id)
    ids = list(options)
    return st.selectbox(label, ids, index=ids.index(value or ""), format_func=options.get, key=key)


def classification_baseline(collection: str, id_key: str, current: dict[str, Any]) -> dict[str, Any]:
    from copy import deepcopy

    key = f"master_classification_edit_{collection}"
    state = st.session_state.get(key)
    if state is None or state["id"] != current.get(id_key, ""):
        state = {"id": current.get(id_key, ""), "original": deepcopy(current)}
        st.session_state[key] = state
    return state["original"]


def save_classified_item(collection: str, id_key: str, item: dict[str, Any], current: dict[str, Any], message: str) -> None:
    try:
        upsert_item(collection, id_key, item, original=current)
    except (ValueError, OSError) as exc:
        ab.show_action_error(str(exc))
        return
    st.session_state.pop(f"master_classification_edit_{collection}", None)
    ab.queue_success(message)
    st.rerun()


def render_table(collection: str) -> None:
    data = MASTER_SNAPSHOT.data
    rows = []
    for item in data.get(collection, []):
        row = dict(item)
        if row.get("project_id"):
            row["project_name"] = MASTER_SNAPSHOT.name("project_id", row.get("project_id"))
        if row.get("subject_id"):
            row["subject_name"] = MASTER_SNAPSHOT.name("subject_id", row.get("subject_id"))
        if row.get("material_id"):
            row["material_name"] = MASTER_SNAPSHOT.name("material_id", row.get("material_id"))
        rows.append(row)
    if rows:
        master_table = pd.DataFrame(rows)
        for date_column in ("updated_at", "created_at"):
            source = master_table[date_column] if date_column in master_table else pd.Series(pd.NaT, index=master_table.index)
            master_table[f"_sort_{date_column}"] = pd.to_datetime(source, errors="coerce")
        master_table = master_table.sort_values(
            "_sort_created_at", ascending=False, kind="stable", na_position="last"
        ).sort_values(
            "_sort_updated_at", ascending=False, kind="stable", na_position="last"
        )
        master_table = master_table.drop(columns=["_sort_updated_at", "_sort_created_at"])
        st.dataframe(master_table, width="stretch", hide_index=True)
    else:
        st.info("登録データはまだありません。")


def render_project_tab() -> None:
    data = MASTER_SNAPSHOT.data
    projects = data.get("projects", [])
    options = option_map(projects, "project_id")
    selected = st.selectbox("編集対象", list(options.keys()), key="project_select")
    current = next((item for item in projects if item.get("project_id") == options[selected]), {})
    with st.form(f"project_form_{current.get('project_id') or 'new'}"):
        name = st.text_input("プロジェクト名", value=current.get("name", ""))
        description = st.text_area("説明", value=current.get("description", ""), height=80)
        is_active = st.checkbox("有効", value=current.get("is_active", True))
        if st.form_submit_button("保存", type="primary"):
            try:
                upsert_item("projects", "project_id", {"project_id": current.get("project_id", ""), "name": name.strip() or "名称未設定", "description": description, "is_active": is_active})
            except (ValueError, OSError) as exc:
                ab.show_action_error(f"プロジェクトを保存できませんでした。入力は保持しています。{exc}")
            else:
                ab.queue_success("プロジェクトを保存しました。")
                st.rerun()
    render_table("projects")


def render_subject_tab() -> None:
    data = MASTER_SNAPSHOT.data
    subjects = data.get("subjects", [])
    options = option_map(subjects, "subject_id")
    selected = st.selectbox("編集対象", list(options.keys()), key="subject_select")
    current = next((item for item in subjects if item.get("subject_id") == options[selected]), {})
    original = classification_baseline("subjects", "subject_id", current)
    project_id = reference_select("所属プロジェクト", "projects", "project_id", original.get("project_id", ""),
                                  key=f"subject_parent_{current.get('subject_id', 'new')}_project")
    with st.form(f"subject_form_{current.get('subject_id') or 'new'}"):
        name = st.text_input("科目名", value=current.get("name", ""))
        description = st.text_area("説明", value=current.get("description", ""), height=80)
        is_active = st.checkbox("有効", value=current.get("is_active", True))
        if st.form_submit_button("保存", type="primary"):
            save_classified_item("subjects", "subject_id", {"subject_id": current.get("subject_id", ""), "project_id": project_id, "name": name.strip() or "名称未設定", "description": description, "is_active": is_active}, original, "科目を保存しました。")
    render_table("subjects")


def render_material_tab() -> None:
    data = MASTER_SNAPSHOT.data
    materials = data.get("materials", [])
    options = option_map(materials, "material_id")
    selected = st.selectbox("編集対象", list(options.keys()), key="material_select")
    current = next((item for item in materials if item.get("material_id") == options[selected]), {})
    original = classification_baseline("materials", "material_id", current)
    prefix = f"material_parent_{current.get('material_id', 'new')}"
    st.info("所属は上から順に選択し、最後に保存してください。無効・未登録の既存IDは、選び直さない限り保持します。")
    selected_project = reference_select("所属プロジェクト", "projects", "project_id", original.get("project_id", ""), key=f"{prefix}_project")
    subject_id = reference_select("所属科目", "subjects", "subject_id", original.get("subject_id", ""),
                                  key=f"{prefix}_subject", parents={"project_id": selected_project})
    with st.form(f"material_form_{current.get('material_id') or 'new'}"):
        name = st.text_input("教材名", value=current.get("name", ""))
        material_type = st.selectbox("教材タイプ", MATERIAL_TYPES, index=MATERIAL_TYPES.index(current.get("type", "その他")) if current.get("type", "その他") in MATERIAL_TYPES else len(MATERIAL_TYPES) - 1)
        description = st.text_area("説明", value=current.get("description", ""), height=80)
        is_active = st.checkbox("有効", value=current.get("is_active", True))
        if st.form_submit_button("保存", type="primary"):
            save_classified_item("materials", "material_id", {"material_id": current.get("material_id", ""), "project_id": selected_project, "subject_id": subject_id, "name": name.strip() or "名称未設定", "type": material_type, "description": description, "is_active": is_active}, original, "教材を保存しました。")
    render_table("materials")


def render_unit_tab() -> None:
    data = MASTER_SNAPSHOT.data
    units = data.get("units", [])
    options = option_map(units, "unit_id")
    selected = st.selectbox("編集対象", list(options.keys()), key="unit_select")
    current = next((item for item in units if item.get("unit_id") == options[selected]), {})
    original = classification_baseline("units", "unit_id", current)
    prefix = f"unit_parent_{current.get('unit_id', 'new')}"
    st.info("所属は上から順に選択し、最後に保存してください。親を変更しても子は自動変更しません。所属が合わないままの保存は停止します。")
    project_id = reference_select("所属プロジェクト", "projects", "project_id", original.get("project_id", ""), key=f"{prefix}_project")
    subject_id = reference_select("所属科目", "subjects", "subject_id", original.get("subject_id", ""),
                                  key=f"{prefix}_subject", parents={"project_id": project_id})
    material_id = reference_select("所属教材", "materials", "material_id", original.get("material_id", ""),
                                   key=f"{prefix}_material", parents={"project_id": project_id, "subject_id": subject_id})
    parent_id = reference_select("親単元", "units", "unit_id", original.get("parent_unit_id", ""),
                                 key=f"{prefix}_parent", exclude_id=current.get("unit_id", ""),
                                 parents={"project_id": project_id, "subject_id": subject_id, "material_id": material_id})
    with st.form(f"unit_form_{current.get('unit_id') or 'new'}"):
        name = st.text_input("単元名", value=current.get("name", ""))
        description = st.text_area("説明", value=current.get("description", ""), height=80)
        is_active = st.checkbox("有効", value=current.get("is_active", True))
        if st.form_submit_button("保存", type="primary"):
            save_classified_item("units", "unit_id", {"unit_id": current.get("unit_id", ""), "project_id": project_id, "subject_id": subject_id, "material_id": material_id, "parent_unit_id": parent_id, "name": name.strip() or "名称未設定", "description": description, "is_active": is_active}, original, "単元を保存しました。")
    render_table("units")


def render_tag_tab() -> None:
    data = MASTER_SNAPSHOT.data
    tags = data.get("tags", [])
    options = option_map(tags, "tag_id")
    selected = st.selectbox("編集対象", list(options.keys()), key="tag_select")
    current = next((item for item in tags if item.get("tag_id") == options[selected]), {})
    with st.form(f"tag_form_{current.get('tag_id') or 'new'}"):
        name = st.text_input("タグ名", value=current.get("name", ""))
        description = st.text_area("説明", value=current.get("description", ""), height=80)
        is_active = st.checkbox("有効", value=current.get("is_active", True))
        if st.form_submit_button("保存", type="primary"):
            try:
                upsert_item("tags", "tag_id", {"tag_id": current.get("tag_id", ""), "name": name.strip() or "名称未設定", "description": description, "is_active": is_active})
            except (ValueError, OSError) as exc:
                ab.show_action_error(f"タグを保存できませんでした。入力は保持しています。{exc}")
            else:
                ab.queue_success("タグを保存しました。")
                st.rerun()
    render_table("tags")


st.set_page_config(page_title="マスタ管理", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("学習データの分類軸を管理します。", settings=PAGE_SETTINGS)
ab.topbar("マスタ管理", "プロジェクト、科目、教材、単元、勉強カテゴリ、タグを管理します。")

tabs = st.tabs(["学習プロジェクト", "科目", "教材", "単元", "タグ", "資料リンク・引き継ぎ", "勉強カテゴリ"])
with tabs[0]:
    render_project_tab()
with tabs[1]:
    render_subject_tab()
with tabs[2]:
    render_material_tab()
with tabs[3]:
    render_unit_tab()
with tabs[4]:
    render_tag_tab()
with tabs[5]:
    from app_core.learning_context_ui import render_learning_context
    render_learning_context(ROOT, key_prefix="master_learning_context", master_data=RAW_MASTER)

with tabs[6]:
    from app_core.study_category_ui import render_study_category_management
    render_study_category_management(ROOT)

with st.expander("保存情報", expanded=False):
    st.caption("分類の変更は、このブラウザの体験用データに保存されます。")
