from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

import prolog_ui.components as ab
from app_core.master_snapshot import MasterSnapshot
from app_core.ids import generate_id
from app_core.master_service import (
    load_master_data,
    save_master_data,
)
from data_utils import (
    now_text,
)


MATERIAL_TYPES = ["講義", "問題集", "過去問", "論文", "参考書", "自作資料", "その他"]


def _option_map(items: list[dict[str, Any]], id_key: str, blank_label: str = "未設定") -> dict[str, str]:
    options = {blank_label: ""}
    options.update({f"{item.get('name', '名称未設定')} [{item.get(id_key, '')}]": item.get(id_key, "") for item in items})
    return options


def _selected_id(label: str, options: dict[str, str]) -> str:
    return options.get(label, "")


def _add_master_item(
    root: Path,
    *,
    collection: str,
    id_key: str,
    prefix: str,
    name: str,
    description: str,
    payload: dict[str, Any] | None = None,
    duplicate_keys: tuple[str, ...] = (),
    defer_rerun: bool = False,
) -> bool:
    clean_name = name.strip()
    if not clean_name:
        ab.show_action_message("名称を入力してください。", kind="warning")
        return False

    try:
        data = load_master_data(root)
    except (ValueError, OSError) as exc:
        ab.show_action_error(f"分類を追加できませんでした。入力は保持しています。{exc}")
        return False
    payload = payload or {}
    for item in data.get(collection, []):
        if str(item.get("name", "")).strip().lower() != clean_name.lower():
            continue
        if all((item.get(key) or "") == (payload.get(key) or "") for key in duplicate_keys):
            ab.show_action_message("同じ分類が既に登録されています。", kind="warning")
            return False

    timestamp = now_text()
    existing_ids = {str(item.get(id_key)) for item in data.get(collection, []) if item.get(id_key)}
    data.setdefault(collection, []).append(
        {
            id_key: generate_id(prefix, existing_ids=existing_ids),
            "name": clean_name,
            "description": description.strip(),
            "is_active": True,
            "created_at": timestamp,
            "updated_at": timestamp,
            **payload,
        }
    )
    try:
        save_master_data(data, root)
    except (ValueError, OSError) as exc:
        ab.show_action_error(f"分類を追加できませんでした。入力は保持しています。{exc}")
        return False
    ab.queue_success("分類を追加しました。")
    if not defer_rerun:
        st.rerun()
    return True


def render_master_quick_add(root: Path, key_prefix: str, *, expanded: bool = False, snapshot: MasterSnapshot | None = None, defer_rerun: bool = False) -> bool:
    # Reuse a caller's per-render snapshot; standalone callers load safely once.
    snapshot = snapshot if snapshot is not None else MasterSnapshot.load(root)
    changed = False
    st.caption("分類はマスタ管理画面で一括管理できます。必要な分類はこの画面からも簡易追加できます。")
    with st.expander("必要な分類がない場合はこちら", expanded=expanded):
        tabs = st.tabs(["プロジェクト", "科目", "教材", "単元", "勉強カテゴリ"])

        with tabs[0]:
            with st.form(f"{key_prefix}_quick_project_form"):
                project_name = st.text_input("プロジェクト名", key=f"{key_prefix}_quick_project_name")
                project_description = st.text_area("説明", height=80, key=f"{key_prefix}_quick_project_description")
                if st.form_submit_button("プロジェクトを追加", type="primary"):
                    changed = _add_master_item(
                        root,
                        defer_rerun=defer_rerun,
                        collection="projects",
                        id_key="project_id",
                        prefix="project",
                        name=project_name,
                        description=project_description,
                    ) or changed

        with tabs[1]:
            project_options = _option_map(snapshot.active("projects"), "project_id")
            with st.form(f"{key_prefix}_quick_subject_form"):
                project_label = st.selectbox("所属プロジェクト", options=list(project_options.keys()), key=f"{key_prefix}_quick_subject_project")
                subject_name = st.text_input("科目名", key=f"{key_prefix}_quick_subject_name")
                subject_description = st.text_area("説明", height=80, key=f"{key_prefix}_quick_subject_description")
                if st.form_submit_button("科目を追加", type="primary"):
                    changed = _add_master_item(
                        root,
                        defer_rerun=defer_rerun,
                        collection="subjects",
                        id_key="subject_id",
                        prefix="subject",
                        name=subject_name,
                        description=subject_description,
                        payload={"project_id": _selected_id(project_label, project_options)},
                        duplicate_keys=("project_id",),
                    ) or changed

        with tabs[2]:
            project_options = _option_map(snapshot.active("projects"), "project_id")
            material_project_label = st.selectbox("所属プロジェクト", options=list(project_options.keys()), key=f"{key_prefix}_quick_material_project")
            material_project_id = _selected_id(material_project_label, project_options)
            subject_options = _option_map(snapshot.active("subjects", project_id=material_project_id or None), "subject_id")
            with st.form(f"{key_prefix}_quick_material_form"):
                subject_label = st.selectbox("所属科目", options=list(subject_options.keys()), key=f"{key_prefix}_quick_material_subject")
                material_name = st.text_input("教材名", key=f"{key_prefix}_quick_material_name")
                material_type = st.selectbox("教材タイプ", options=MATERIAL_TYPES, key=f"{key_prefix}_quick_material_type")
                material_description = st.text_area("説明", height=80, key=f"{key_prefix}_quick_material_description")
                if st.form_submit_button("教材を追加", type="primary"):
                    changed = _add_master_item(
                        root,
                        defer_rerun=defer_rerun,
                        collection="materials",
                        id_key="material_id",
                        prefix="material",
                        name=material_name,
                        description=material_description,
                        payload={
                            "project_id": material_project_id,
                            "subject_id": _selected_id(subject_label, subject_options),
                            "type": material_type,
                        },
                        duplicate_keys=("project_id", "subject_id"),
                    ) or changed

        with tabs[3]:
            project_options = _option_map(snapshot.active("projects"), "project_id")
            unit_project_label = st.selectbox("所属プロジェクト", options=list(project_options.keys()), key=f"{key_prefix}_quick_unit_project")
            unit_project_id = _selected_id(unit_project_label, project_options)
            subject_options = _option_map(snapshot.active("subjects", project_id=unit_project_id or None), "subject_id")
            unit_subject_label = st.selectbox("所属科目", options=list(subject_options.keys()), key=f"{key_prefix}_quick_unit_subject")
            unit_subject_id = _selected_id(unit_subject_label, subject_options)
            material_options = _option_map(snapshot.active("materials", project_id=unit_project_id or None, subject_id=unit_subject_id or None), "material_id")
            unit_material_label = st.selectbox("所属教材", options=list(material_options.keys()), key=f"{key_prefix}_quick_unit_material")
            unit_material_id = _selected_id(unit_material_label, material_options)
            parent_options = _option_map(snapshot.active("units", project_id=unit_project_id or None, subject_id=unit_subject_id or None, material_id=unit_material_id or None), "unit_id", "親単元なし")
            with st.form(f"{key_prefix}_quick_unit_form"):
                parent_label = st.selectbox("親単元", options=list(parent_options.keys()), key=f"{key_prefix}_quick_unit_parent")
                unit_name = st.text_input("単元名", key=f"{key_prefix}_quick_unit_name")
                unit_description = st.text_area("説明", height=80, key=f"{key_prefix}_quick_unit_description")
                if st.form_submit_button("単元を追加", type="primary"):
                    changed = _add_master_item(
                        root,
                        defer_rerun=defer_rerun,
                        collection="units",
                        id_key="unit_id",
                        prefix="unit",
                        name=unit_name,
                        description=unit_description,
                        payload={
                            "project_id": unit_project_id,
                            "subject_id": unit_subject_id,
                            "material_id": unit_material_id,
                            "parent_unit_id": _selected_id(parent_label, parent_options),
                        },
                        duplicate_keys=("project_id", "subject_id", "material_id", "parent_unit_id"),
                    ) or changed
        with tabs[4]:
            from app_core.study_category_ui import render_study_category_quick_add
            changed = render_study_category_quick_add(root, f"{key_prefix}_study_category", defer_rerun=defer_rerun) or changed
    return changed
