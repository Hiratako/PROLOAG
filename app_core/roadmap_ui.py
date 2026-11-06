"""Forms and fragment-scoped Plotly view for the study roadmap."""

from copy import deepcopy
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.app_clock import app_today
from app_core.form_snapshot import (
    form_snapshot,
    discard_form_snapshot,
    advance_form_snapshots_after_own_patch,
)
from app_core.json_store import observed_json_revision
from app_core.master_snapshot import MasterSnapshot
from app_core.roadmap_service import (
    CLASS_KEYS,
    STATUS_LABELS,
    allocate_task,
    build_actuals,
    deactivate_allocation,
    load_roadmap,
    load_target_plan,
    parse_day,
    progress_percent,
    roadmap_path,
    save_entry,
    schedule_label,
)
from data_utils import daily_plan_path


def _advance(root, edit):
    path = roadmap_path(root)
    revision = observed_json_revision(path)
    data = load_roadmap(root)
    if observed_json_revision(path) == revision:
        advance_form_snapshots_after_own_patch(
            st.session_state,
            path,
            before_revision=edit["revisions"][path],
            after_revision=revision,
            updates=data,
        )
    for sibling_path, before in edit["revisions"].items():
        if sibling_path == path:
            continue
        from app_core.json_store import load_json_safe

        after = observed_json_revision(sibling_path)
        if after == before:
            continue
        load_json_safe(sibling_path, None)
        if after == observed_json_revision(sibling_path):
            advance_form_snapshots_after_own_patch(
                st.session_state,
                sibling_path,
                before_revision=before,
                after_revision=after,
                updates={},
            )


def _classification(master, current, prefix, disabled=False, project_only=False):
    values = {}
    keys = CLASS_KEYS[:1] if project_only else CLASS_KEYS
    for index, key in enumerate(keys):
        collection = key.removesuffix("_id") + "s"
        records = master.data.get(collection, [])
        options = [
            "",
            *[
                row[key]
                for row in records
                if row.get("is_active", True)
                and all(
                    not values[parent] or row.get(parent, "") == values[parent]
                    for parent in keys[:index]
                )
            ],
        ]
        widget_key = prefix + key
        current_value = st.session_state.get(widget_key, current.get(key, ""))
        if current_value not in options:
            options.append(current_value)

        def clear_descendants(rest=keys[index + 1 :]):
            for child in rest:
                st.session_state[prefix + child] = ""

        values[key] = st.selectbox(
            {
                "project_id": "プロジェクト",
                "subject_id": "科目",
                "material_id": "教材",
                "unit_id": "単元",
            }[key],
            options,
            index=options.index(current_value),
            format_func=lambda value, k=key: (
                master.name(k, value) if value else "未設定"
            ),
            key=widget_key,
            on_change=clear_descendants,
            disabled=disabled,
        )
    return values


def _editor(root, data, master, scope, collection, *, identifier=None, on_close=None, on_saved=None):
    entries = {row["id"]: row for row in data[collection]}
    noun = "工程" if collection == "items" else "試験・節目"
    selector = scope + collection + "_selected"
    if st.session_state.get(selector, "") not in {"", *entries}:
        st.session_state[selector] = ""
    if identifier is None:
        selected = st.selectbox(
            "編集する" + noun,
            ["", *entries],
            format_func=lambda value: (
                "新しく追加" if not value else entries[value]["title"]
                + ("（アーカイブ）" if entries[value]["is_archived"] else "")
            ), key=selector,
        )
    else:
        selected = identifier
        if selected and selected not in entries:
            st.error("編集対象の工程・節目が削除されています。入力は保持しています。")
            if on_close and st.button("閉じる", key=scope + "missing_close"):
                on_close()
                st.rerun()
            return
    key = scope + collection + "_" + (selected or "new")
    path = roadmap_path(root)
    edit = form_snapshot(st.session_state, key, [path], data)
    original = next(
        (row for row in edit["data"][collection] if row["id"] == selected), {}
    )
    draft_key = key + "_retained_draft"
    current = {**original, **deepcopy(st.session_state.get(draft_key, {}))}
    pending_key = key + "_pending"
    if st.button("入力を破棄して再読込", key=key + "_reload"):
        discard_form_snapshot(st.session_state, key)
        st.session_state.pop(pending_key, None)
        st.session_state.pop(draft_key, None)
        st.rerun()
    prefix = f"{key}_{edit['version']}_"
    pending = st.session_state.get(pending_key)
    with st.expander("分類", expanded=bool(current.get("project_id"))):
        classification = _classification(
            master,
            current,
            prefix,
            disabled=bool(pending),
            project_only=collection == "milestones",
        )
    with st.form(prefix + "form"):
        title = st.text_input(
            noun + "名",
            value=current.get("title", ""),
            max_chars=200,
            disabled=bool(pending),
        )
        today = app_today()
        if collection == "items":
            a, b, c = st.columns(3)
            start = a.date_input(
                "開始日",
                value=parse_day(current["start_date"]) if current else today,
                min_value=date(1900, 1, 1),
                max_value=date(2199, 12, 31),
                disabled=bool(pending),
            )
            end = b.date_input(
                "終了予定日",
                value=parse_day(current["end_date"])
                if current
                else today + timedelta(days=6),
                min_value=date(1900, 1, 1),
                max_value=date(2199, 12, 31),
                disabled=bool(pending),
            )
            deadline = c.date_input(
                "確定期限（任意）",
                value=parse_day(current["deadline"])
                if current.get("deadline")
                else None,
                min_value=date(1900, 1, 1),
                max_value=date(2199, 12, 31),
                disabled=bool(pending),
            )
            a, b, c = st.columns(3)
            minutes = a.number_input(
                "予定学習時間（分）",
                min_value=0,
                max_value=600000,
                value=int(current.get("planned_minutes", 0)),
                step=30,
                disabled=bool(pending),
            )
            status = b.selectbox(
                "状態",
                list(STATUS_LABELS),
                index=list(STATUS_LABELS).index(current.get("status", "not_started")),
                format_func=STATUS_LABELS.get,
                disabled=bool(pending),
            )
            kind = c.selectbox(
                "進捗の記録方法",
                ["quantity", "percent"],
                index=0
                if current.get("progress_kind", "quantity") == "quantity"
                else 1,
                format_func=lambda value: (
                    "完了数 / 総量" if value == "quantity" else "割合（%）"
                ),
                disabled=bool(pending),
            )
            a, b, c = st.columns(3)
            completed = a.number_input(
                "完了数 / 完了割合",
                min_value=0.0,
                max_value=1e9,
                value=float(current.get("completed", 0)),
                disabled=bool(pending),
            )
            total = b.number_input(
                "総量（割合なら100）",
                min_value=0.000001,
                max_value=1e9,
                value=float(current.get("total", 100)),
                disabled=bool(pending),
            )
            unit = c.text_input(
                "単位（章・問・ページなど）",
                value=current.get("unit", "問"),
                max_chars=20,
                disabled=bool(pending),
            )
            st.caption(
                "学習範囲の進捗を入力します。勉強時間から進捗・理解度を推定しません。"
            )
            payload = {
                "title": title.strip(),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "deadline": deadline.isoformat() if deadline else "",
                "planned_minutes": minutes,
                "status": status,
                "progress_kind": kind,
                "completed": completed,
                "total": 100 if kind == "percent" else total,
                "unit": "%" if kind == "percent" else unit.strip(),
                **classification,
            }
            predecessors = [
                row["id"]
                for row in data["items"]
                if row["id"] != selected
                and (
                    not row["is_archived"]
                    or row["id"] in current.get("predecessor_ids", [])
                )
            ]
            payload["predecessor_ids"] = st.multiselect(
                "前提となる工程（終了後に開始）",
                predecessors,
                default=[
                    value
                    for value in current.get("predecessor_ids", [])
                    if value in predecessors
                ],
                format_func=lambda value: next(
                    row["title"] + ("（アーカイブ）" if row["is_archived"] else "")
                    for row in data["items"]
                    if row["id"] == value
                ),
                max_selections=50,
                disabled=bool(pending),
            )
        else:
            target = st.date_input(
                "日付",
                value=parse_day(current["date"]) if current else today,
                min_value=date(1900, 1, 1),
                max_value=date(2199, 12, 31),
                disabled=bool(pending),
            )
            payload = {
                "title": title.strip(),
                "date": target.isoformat(),
                **classification,
            }
        payload["memo"] = st.text_area(
            "メモ",
            value=current.get("memo", ""),
            max_chars=20000,
            disabled=bool(pending),
        )
        payload["is_archived"] = st.checkbox(
            "アーカイブする",
            value=current.get("is_archived", False),
            disabled=bool(pending),
        )
        submitted = st.form_submit_button(
            "変更内容を確認", type="primary", disabled=bool(pending)
        )
        keep_close = st.form_submit_button("入力を保持して閉じる") if on_close else False
        discard_close = st.form_submit_button("入力を破棄して閉じる") if on_close else False
    if keep_close:
        st.session_state[draft_key] = deepcopy(payload)
        on_close()
        st.rerun()
    if discard_close:
        st.session_state.pop(draft_key, None)
        st.session_state.pop(pending_key, None)
        discard_form_snapshot(st.session_state, key)
        on_close()
        st.rerun()
    if submitted:
        # Save validation still runs against current committed data at confirmation.
        st.session_state[pending_key] = payload
        st.session_state[draft_key] = deepcopy(payload)
        st.rerun()
    if pending:
        st.caption(
            "以下の内容で保存します。日程変更時も当初の予定と変更履歴を残します。"
        )
        labels = {
            "title": noun + "名",
            "start_date": "開始日",
            "end_date": "終了予定日",
            "deadline": "確定期限",
            "planned_minutes": "予定学習時間（分）",
            "status": "状態",
            "progress_kind": "進捗方式",
            "completed": "完了量",
            "total": "総量",
            "unit": "単位",
            "memo": "メモ",
            "is_archived": "アーカイブ",
            "date": "日付",
        }
        rows = []
        for field, value in pending.items():
            if field in CLASS_KEYS:
                old = (
                    master.name(field, original.get(field, ""))
                    if original.get(field)
                    else "未設定"
                )
                new = master.name(field, value) if value else "未設定"
            else:
                old = original.get(field, "")
                new = value
            if not original or original.get(field) != value:
                rows.append(
                    {
                        "項目": labels.get(field, field),
                        "変更前": str(old),
                        "保存する内容": str(new),
                    }
                )
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        a, b = st.columns(2)
        if a.button("この内容で保存", key=prefix + "confirm", type="primary"):
            try:
                committed = save_entry(
                    root,
                    collection,
                    pending,
                    identifier=selected,
                    expected_revision=edit["revisions"][path],
                    operation_id=edit["operation_id"],
                )
            except (ValueError, OSError) as exc:
                ab.show_action_error(
                    f"保存できませんでした。入力を保持しています。{exc}"
                )
            else:
                if committed:
                    _advance(root, edit)
                discard_form_snapshot(st.session_state, key)
                st.session_state.pop(pending_key, None)
                st.session_state.pop(draft_key, None)
                if on_saved:
                    saved_identifier = selected or ("rm" if collection == "items" else "ms") + "_" + uuid5(NAMESPACE_URL, edit["operation_id"]).hex
                    on_saved(collection, saved_identifier, pending)
                ab.queue_success(noun + "を保存しました。")
                st.rerun()
        if b.button("確認を取り消す", key=prefix + "cancel"):
            st.session_state.pop(pending_key, None)
            st.rerun()
    if current.get("schedule_history"):
        with st.expander("日程の変更履歴"):
            st.dataframe(
                pd.DataFrame(current["schedule_history"]),
                hide_index=True,
                width="stretch",
            )


def _allocation(root, data, actuals, scope):
    entries = {row["id"]: row for row in data["items"] if not row["is_archived"]}
    if not entries:
        st.info("学習工程を作成すると、指定日へタスクを割り当てられます。")
        return
    item_id = st.selectbox(
        "割り当てる学習工程",
        list(entries),
        format_func=lambda value: entries[value]["title"],
        key=scope + "allocation_item",
    )
    target = st.date_input(
        "割当日",
        value=app_today(),
        min_value=date(1900, 1, 1),
        max_value=date(2199, 12, 31),
        key=scope + "allocation_date",
    )
    plan = load_target_plan(root, target)
    tasks = {row["task_id"]: row for row in plan.get("tasks", [])}
    selected = st.selectbox(
        "日別タスク",
        ["", *tasks],
        format_func=lambda value: (
            "新しいタスクを作成" if not value else tasks[value].get("title", value)
        ),
        key=scope + "allocation_task_" + target.isoformat(),
    )
    path = roadmap_path(root)
    key = f"{scope}allocation_{item_id}_{target}_{selected}"
    edit = form_snapshot(
        st.session_state, key, [path, daily_plan_path(root, target)], data
    )
    if st.button("割当入力を破棄して再読込", key=key + "reload"):
        discard_form_snapshot(st.session_state, key)
        st.rerun()
    with st.form(key + "_" + str(edit["version"])):
        title = st.text_input(
            "日別タスク名",
            value=entries[item_id]["title"],
            max_chars=200,
            disabled=bool(selected),
        )
        minutes = st.number_input(
            "この日の予定時間（分）",
            min_value=0,
            max_value=1440,
            value=60,
            step=15,
            disabled=bool(selected),
        )
        st.caption(
            "既存タスクに関連付ける場合は、そのタスクの内容と予定時間を保ちます。"
        )
        submitted = st.form_submit_button("日別タスクに割り当てる", type="primary")
    if submitted:
        try:
            created = allocate_task(
                root,
                item_id,
                target,
                task_id=selected,
                title=title,
                planned_minutes=minutes,
                expected_revisions=edit["revisions"],
                operation_id=edit["operation_id"],
            )
        except (ValueError, OSError) as exc:
            ab.show_action_error(
                f"割り当てできませんでした。入力を保持しています。{exc}"
            )
        else:
            if created:
                _advance(root, edit)
            discard_form_snapshot(st.session_state, key)
            ab.queue_success(
                "日別タスクに割り当てました。"
                if created
                else "この工程には既に関連付いています。"
            )
            st.rerun()
    st.page_link("pages/09_今日の学習プラン.py", label="今日の記録・計画を開く")
    allocations = [
        row
        for row in data["allocations"]
        if row["item_id"] == item_id and row["is_active"]
    ]
    if allocations:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "割当日": row["task_date"],
                        "関連タスク数（繰越を含む）": len(
                            actuals["tasks"].get(row["id"], [])
                        ),
                        "タスクID": row["task_id"],
                    }
                    for row in allocations
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        chosen = st.selectbox(
            "解除する関連付け",
            [row["id"] for row in allocations],
            format_func=lambda value: next(
                row["task_date"] + " / " + row["task_id"]
                for row in allocations
                if row["id"] == value
            ),
            key=scope + "unlink",
        )
        unlink_key = scope + "unlink_" + chosen
        unlink_edit = form_snapshot(st.session_state, unlink_key, [path], data)
        confirmed = st.checkbox(
            "日別タスクと勉強ログを残し、工程との関連付けを解除する",
            key=unlink_key + "check",
        )
        if st.button(
            "関連付けを解除", disabled=not confirmed, key=unlink_key + "button"
        ):
            try:
                deactivate_allocation(
                    root,
                    chosen,
                    expected_revision=unlink_edit["revisions"][path],
                    operation_id=unlink_edit["operation_id"],
                )
            except (ValueError, OSError) as exc:
                ab.show_action_error(str(exc))
            else:
                _advance(root, unlink_edit)
                discard_form_snapshot(st.session_state, unlink_key)
                ab.queue_success("関連付けを解除しました。")
                st.rerun()


def _render_content(root: Path, actuals: dict, scope: str):
    from app_core.roadmap_workspace import render_workspace
    render_workspace(root, actuals, scope)


def _render_plotly(
    visible, milestones, start, end, today, master, actuals, baseline, scope, data
):
    from app_core.roadmap_chart import build_roadmap_figure

    if visible or milestones:
        names = {
            row[key]: row.get("name", "")
            for key in CLASS_KEYS
            for row in master.data.get(key.removesuffix("_id") + "s", [])
        }
        figure = build_roadmap_figure(
            visible,
            milestones,
            start,
            end,
            today=today,
            names=names,
            actuals=actuals["seconds"],
            palette=ab.theme_palette(),
            show_baseline=baseline,
            show_actuals=False,
        )

        st.plotly_chart(
            figure, width="stretch", on_select="ignore", key=scope + "gantt",
            config={"displaylogo": False, "scrollZoom": False},
        )
        st.caption("補助の期間表示です。工程の選択・編集は統一ガントまたは一覧から行えます。")



@st.fragment
def _content(root: Path, actuals: dict, scope: str):
    try:
        _render_content(root, actuals, scope)
    except (ValueError, OSError) as exc:
        st.error(f"学習ロードマップを表示できません。入力を保持しています。{exc}")


def render_roadmap(root: Path):
    scope = "roadmap_" + sha256(str(root.resolve()).encode()).hexdigest()[:10] + "_"
    try:
        data = load_roadmap(root)
        actuals = build_actuals(root, data)
        _content(root, actuals, scope)
    except (ValueError, OSError) as exc:
        st.error(f"学習ロードマップを表示できません。保存内容は変更していません。{exc}")
