"""The product's roadmap canvas, restricted to browsing synthetic records.

The normal canvas's ``editable=False`` is only its initial UI setting.  The
normal toolbar can turn editing back on, so this renderer also removes editing
controls, disables drag handlers, and accepts only a validated focus request.
No roadmap workspace/editor or persistence function is imported here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import MutableMapping

import pandas as pd
import streamlit as st

from app_core.json_store import observed_json_revision
from app_core.master_snapshot import MasterSnapshot
from app_core.roadmap_service import CLASS_KEYS, build_actuals, load_roadmap, roadmap_path
from app_core.roadmap_view import build_canvas_rows, build_capacity_strip, period_bounds, shift_anchor
from prolog_ui import roadmap_canvas as product_canvas
from prolog_ui.theme import get_theme_palette
from web_demo.data import REFERENCE_DATE


ROOT = Path(__file__).resolve().parents[1]
SCOPE = "web_demo_roadmap_"
VIEWS = ("全体", "月間", "週間")


def _replace_once(source: str, before: str, after: str) -> str:
    if source.count(before) != 1:
        raise RuntimeError("展示用ガントの構成が変わっています。公開前の確認が必要です。")
    return source.replace(before, after, 1)


def readonly_canvas_javascript() -> str:
    """Reuse the product renderer, failing closed when its expected hooks change."""
    source = product_canvas._JS
    source = _replace_once(
        source,
        " if(state.externalEditable!==Boolean(data.editable)){state.editable=Boolean(data.editable);state.externalEditable=Boolean(data.editable);}",
        " state.editable=false;state.externalEditable=false;",
    )
    source = _replace_once(
        source,
        "function emit(kind,values={}){if(state.clientPending||(data.pending&&kind!=='focus_item'))return;",
        "function emit(kind,values={}){if(kind!=='focus_item'||state.clientPending)return;",
    )
    source = _replace_once(
        source,
        "say(kind==='schedule_change'?'変更案を確認しています。':'選択した操作を開いています。');",
        "say('選択した工程の期間へ移動しています。');",
    )
    toolbar_line = next(
        (line for line in source.splitlines() if "const edit=checkbox('日程を編集'" in line),
        "",
    )
    if not toolbar_line:
        raise RuntimeError("展示用ガントの操作欄を確認してください。")
    source = _replace_once(
        source, toolbar_line,
        "  toolbar.append(node('span','rc-view-help','展示データの閲覧専用'));",
    )
    controls_line = next(
        (line for line in source.splitlines() if line.startswith(" function updateControls()")),
        "",
    )
    if not controls_line:
        raise RuntimeError("展示用ガントの操作状態を確認してください。")
    source = _replace_once(
        source, controls_line,
        " function updateControls(){searchNode.querySelectorAll('button').forEach(b=>b.disabled=state.clientPending);}",
    )
    source = _replace_once(
        source,
        "const b=button(label,'rc-milestone-button',()=>{if(!disabled())emit('edit_milestone',{milestone_id:item.id});});b.dataset.milestoneId=item.id;b.setAttribute('aria-label',label+'を編集');b.dataset.serverAction='true';b.disabled=disabled();",
        "const b=button(label,'rc-milestone-button',()=>say('試験・節目：'+label));b.dataset.milestoneId=item.id;b.setAttribute('aria-label','試験・節目：'+label);",
    )
    source = _replace_once(
        source,
        "canEdit=state.viewMode==='detail'&&state.editable&&!locked&&!state.mobile&&!disabled();",
        "canEdit=false;",
    )
    source = _replace_once(
        source,
        "const actions=node('div','rc-detail-actions');actions.append(serverButton('工程を編集','edit_item',{item_id:item.id}),serverButton('日別タスクへ割当','allocate_item',{item_id:item.id}),serverButton('負荷・配分を開く','show_capacity',{item_id:item.id}));inspectorNode.append(actions);",
        "",
    )
    source = _replace_once(
        source,
        "if(state.mobile)inspectorNode.append(node('p','rc-detail-note','日程は「工程を編集」から変更できます。小さな画面ではドラッグせず、日付を入力します。'));",
        "",
    )
    source = _replace_once(
        source,
        "const b=serverButton('日別配分の負荷を確認','show_capacity',{});b.classList.add('rc-more');capacityNode.append(b);return;",
        "capacityNode.append(node('div','rc-empty','日別の予定は上の表示設定から確認できます。'));return;",
    )
    # A value from a former local-app session must not enable editing.
    source = source.replace("'viewMode','editable','scrollTop'", "'viewMode','scrollTop'")
    return source


READONLY_CANVAS_JS = readonly_canvas_javascript()


def consume_focus_event(
    event: object,
    data: dict,
    revision: str | None,
    state: MutableMapping,
    *,
    scope: str = SCOPE,
) -> bool:
    """Accept display navigation once; every editing message is ignored."""
    message = product_canvas.canvas_action(event)
    if (not message or message["kind"] != "focus_item" or
            set(message) != {"kind", "event_id", "source_revision", "item_id"}):
        return False
    if message["source_revision"] != revision:
        return False
    if message["event_id"] == state.get(scope + "ack", ""):
        return False
    item = next((row for row in data.get("items", [])
                 if row.get("id") == message["item_id"] and not row.get("is_archived")), None)
    if not item:
        return False
    # Widget values are changed at the beginning of the next run, rather than
    # mutating a date/select widget that Streamlit has already instantiated.
    state[scope + "focus"] = {"id": item["id"], "start": item["start_date"]}
    state[scope + "ack"] = message["event_id"]
    return True


def _render_canvas(rows, milestones, start, end, *, revision, actuals, names,
                   workload, calendar, selected_id, ack_event_id):
    component = st.components.v2.component(
        "proloag_web_demo_roadmap", html=product_canvas._HTML,
        css=product_canvas._CSS, js=READONLY_CANVAS_JS,
    )
    fields = {
        "id", "title", "start_date", "end_date", "deadline", "planned_minutes",
        "status", "completed", "total", "unit", "progress_kind", "is_archived",
        "predecessor_ids", "baseline_start_date", "baseline_end_date", "memo",
        "project_id", "subject_id", "material_id", "unit_id",
    }
    result = component(
        key=SCOPE + "canvas",
        data={
            "items": [{key: deepcopy(value) for key, value in row.items()
                       if key in fields or key.startswith("view_")} for row in rows],
            "milestones": [{key: deepcopy(value) for key, value in row.items()
                            if key in {"id", "title", "date", "project_id", "is_archived"}}
                           for row in milestones],
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "today": REFERENCE_DATE.isoformat(), "source_revision": revision,
            "palette": get_theme_palette("standard"), "pending": False,
            "ack_event_id": ack_event_id, "names": deepcopy(names),
            "actuals": {name: deepcopy(actuals.get(name, {})) for name in (
                "seconds", "counts", "allocation_counts", "task_counts", "broken_allocation_counts")},
            "view_mode": "detail", "calendar": deepcopy(calendar),
            "selected_id": selected_id, "show_baseline": False,
            "editable": False, "workload": deepcopy(workload),
        },
        on_action=lambda: None,
        width="stretch",
    )
    return result.get("action")


def _move_period(steps: int) -> None:
    view = st.session_state.get(SCOPE + "view", "全体")
    if view != "全体":
        anchor = st.session_state.get(SCOPE + "anchor", REFERENCE_DATE)
        st.session_state[SCOPE + "anchor"] = shift_anchor(anchor, view, steps)


def _choose_item() -> None:
    selected = st.session_state.get(SCOPE + "select_item")
    if selected:
        st.session_state[SCOPE + "selected"] = selected


def render_roadmap_demo() -> None:
    st.title("学習ロードマップ")
    st.caption("学ぶ工程、予定期間、進捗、関連する勉強時間を一つの画面で確認します。")
    data = load_roadmap(ROOT)
    master = MasterSnapshot.load(ROOT)
    actuals = build_actuals(ROOT, data)
    rows = build_canvas_rows(data, master, actuals, today=REFERENCE_DATE)
    active = [row for row in rows if not row.get("is_archived")]
    if not active:
        st.info("展示用の工程データを準備中です。")
        return
    start_all, end_all = period_bounds(REFERENCE_DATE, "全体", data)
    metrics = st.columns(4)
    metrics[0].metric("学習工程", f"{len(active)}件")
    metrics[1].metric("進行中", f"{sum(row.get('status') == 'in_progress' for row in active)}件")
    metrics[2].metric("完了", f"{sum(row.get('status') == 'done' for row in active)}件")
    metrics[3].metric("試験・節目", f"{sum(not row.get('is_archived') for row in data['milestones'])}件")

    focus = st.session_state.pop(SCOPE + "focus", None)
    if focus:
        st.session_state[SCOPE + "anchor"] = date.fromisoformat(focus["start"])
        st.session_state[SCOPE + "selected"] = focus["id"]
    st.session_state.setdefault(SCOPE + "anchor", REFERENCE_DATE)
    st.session_state.setdefault(SCOPE + "view", "全体")
    st.session_state.setdefault(SCOPE + "selected", active[0]["id"])

    filters = st.columns([1.1, 1.2, 1.4])
    view = filters[0].selectbox("表示する期間", VIEWS, key=SCOPE + "view")
    anchor = filters[1].date_input(
        "表示の基準日", key=SCOPE + "anchor", min_value=date(1900, 1, 1),
        max_value=date(2199, 12, 31), disabled=view == "全体",
    )
    show_workload = filters[2].checkbox("日別の学習予定を表示", key=SCOPE + "show_workload")
    if view != "全体":
        previous, current, following, _ = st.columns([1, 1, 1, 3])
        previous.button("前の期間", key=SCOPE + "previous", on_click=_move_period, args=(-1,))
        current.button("基準日に戻る", key=SCOPE + "today", on_click=lambda: st.session_state.update({SCOPE + "anchor": REFERENCE_DATE}))
        following.button("次の期間", key=SCOPE + "next", on_click=_move_period, args=(1,))
    start, end = period_bounds(anchor, view, data)
    st.caption(f"表示期間：{start:%Y/%m/%d}〜{end:%Y/%m/%d}　｜　全工程：{start_all:%Y/%m/%d}〜{end_all:%Y/%m/%d}")
    st.info("工程名や棒を選ぶと詳細が開きます。「俯瞰」で科目ごとの予定を見渡せます。検索・分類・表示変更は自由に試せます。")
    workload = build_capacity_strip(ROOT, data, start, end) if show_workload and (end - start).days < 366 else None
    if show_workload and (end - start).days >= 366:
        st.caption("日別予定は1年以内の表示期間で確認できます。")
    names = {record[key]: record.get("name", "") for key in CLASS_KEYS
             for record in master.data.get(key.removesuffix("_id") + "s", [])}
    names.update({row["id"]: row["title"] for row in data["items"]})
    revision = observed_json_revision(roadmap_path(ROOT))
    event = _render_canvas(
        rows, data["milestones"], start, end, revision=revision, actuals=actuals,
        names=names, workload=workload, calendar=data.get("calendar", {}),
        selected_id=st.session_state[SCOPE + "selected"],
        ack_event_id=st.session_state.get(SCOPE + "ack", ""),
    )
    if consume_focus_event(event, data, revision, st.session_state):
        st.rerun()
    with st.expander("工程を一覧で確認する"):
        by_id = {row["id"]: row for row in active}
        st.selectbox(
            "工程を選んで詳細を見る", ["", *by_id], key=SCOPE + "select_item",
            format_func=lambda value: "選択してください" if not value else by_id[value]["title"],
            on_change=_choose_item,
        )
        st.dataframe(pd.DataFrame([{
            "科目": row["view_subject"], "工程": row["title"],
            "開始": row["start_date"], "終了予定": row["end_date"],
            "状態": row["view_state_label"], "数量進捗": row["view_progress_label"],
            "関連する勉強時間": row["view_actual_label"],
        } for row in active]), hide_index=True, width="stretch")
    st.caption("この展示版では、日程・工程・日別タスクの追加や変更は保存しません。")
