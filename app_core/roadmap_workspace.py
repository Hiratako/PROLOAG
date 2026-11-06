"""One roadmap workspace: local canvas interactions and explicit safe saves."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
import re

import streamlit as st

import prolog_ui.components as ab
from app_core.app_clock import app_today
from app_core.json_store import JsonConflictError, observed_json_revision
from app_core.master_snapshot import MasterSnapshot
from app_core.roadmap_service import CLASS_KEYS, load_roadmap, parse_day, roadmap_path
from app_core.roadmap_view import build_canvas_rows, build_capacity_strip, period_bounds, shift_anchor

MODES = ["学習工程", "試験・節目", "日別タスクとの連携", "日程の調整", "学習可能時間・配分", "AI計画の取り込み"]


def _request_mode(scope, mode):
    st.session_state[scope + "mode_request"] = mode


def open_editor(scope, collection, identifier=""):
    st.session_state[scope + "dialog_request"] = {"collection": collection, "identifier": identifier}


def handle_canvas_event(root: Path, data: dict, scope: str, event: dict) -> bool:
    """Validate an untrusted client request before changing session state."""
    if not isinstance(event, dict):
        raise ValueError("画面操作の形式が不正です。")
    common = {"kind", "event_id", "source_revision"}
    kind = event.get("kind")
    allowed = {
        "schedule_change": common | {"item_id", "old_start", "old_end", "start_date", "end_date"},
        "edit_item": common | {"item_id"},
        "edit_milestone": common | {"milestone_id"},
        "allocate_item": common | {"item_id"},
        "show_capacity": common,
        "focus_item": common | {"item_id"},
    }
    if not isinstance(kind, str) or kind not in allowed or set(event) != allowed[kind]:
        raise ValueError("未対応の画面操作です。ページを更新してください。")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{32}", event_id):
        raise ValueError("画面操作の識別情報が不正です。")
    if event_id == st.session_state.get(scope + "canvas_ack"):
        return False
    if event["source_revision"] != observed_json_revision(roadmap_path(root)):
        raise JsonConflictError("操作中に計画が更新されています。入力を残したまま「実績・計画を更新」で確認してください。")
    identifier = event.get("item_id", event.get("milestone_id"))
    collection = "milestones" if kind == "edit_milestone" else "items"
    item = None
    if kind != "show_capacity":
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("操作する工程・節目のIDが不正です。")
        matches = [row for row in data[collection] if row["id"] == identifier]
        if len(matches) != 1:
            raise ValueError("操作する工程・節目が見つかりません。最新の計画を確認してください。")
        item = matches[0]
    pending = st.session_state.get(scope + "schedule_pending") or st.session_state.get(scope + "distribution_pending")
    if pending and kind != "focus_item":
        raise ValueError("未保存の変更案があります。保存または取り消してから、次の編集を行ってください。")
    if kind == "schedule_change":
        from app_core.roadmap_planning_ui import consume_drag
        consume_drag(root, data, scope, event)
        st.session_state[scope + "canvas_selected"] = identifier
        st.session_state[scope + "pending_dialog_hidden"] = None
    elif kind in {"edit_item", "edit_milestone"}:
        open_editor(scope, collection, identifier)
        st.session_state[scope + "canvas_selected"] = identifier
    elif kind == "allocate_item":
        if item["is_archived"]:
            raise ValueError("アーカイブ済みの工程には新しい日別タスクを割り当てられません。")
        st.session_state[scope + "allocation_item"] = identifier
        _request_mode(scope, "日別タスクとの連携")
    elif kind == "show_capacity":
        _request_mode(scope, "学習可能時間・配分")
    elif kind == "focus_item":
        # Navigation follows the same dates as the visible, unsaved schedule.
        # The validated ID still comes from committed data; no draft is saved here.
        if pending:
            from app_core.roadmap_planning_ui import displayed_schedule
            item = next(row for row in displayed_schedule(data, scope)["items"] if row["id"] == identifier)
        st.session_state[scope + "canvas_selected"] = identifier
        st.session_state[scope + "jump_anchor"] = parse_day(item["start_date"])
        if st.session_state.get(scope + "view") == "全体":
            start, end = period_bounds(app_today(), "全体", data)
            if parse_day(item["start_date"]) > end or parse_day(item["end_date"]) < start:
                st.session_state[scope + "jump_view"] = "4週間"
        _request_mode(scope, "学習工程")
    st.session_state[scope + "canvas_ack"] = event_id
    st.session_state.pop(scope + "canvas_notice", None)
    return True


def _editor_dialog(root, data, master, scope):
    from app_core.roadmap_ui import _editor
    request = st.session_state.get(scope + "dialog_request")
    if not request:
        return
    def close():
        st.session_state.pop(scope + "dialog_request", None)
    def saved(collection, identifier, payload):
        close()
        st.session_state[scope + "canvas_selected"] = identifier
        if collection == "items":
            start, end = period_bounds(st.session_state.get(scope + "anchor", app_today()),
                                       st.session_state.get(scope + "view", "4週間"), data)
            if parse_day(payload["start_date"]) > end or parse_day(payload["end_date"]) < start:
                st.session_state[scope + "jump_anchor"] = parse_day(payload["start_date"])
        st.session_state.pop(scope + "workload_cache", None)
    @st.dialog("学習工程・試験／節目の編集", width="large", dismissible=False)
    def dialog():
        st.caption("入力を保持して閉じることができます。保存は「変更内容を確認」の後に確定します。")
        _editor(root, data, master, scope, request["collection"],
                identifier=request["identifier"], on_close=close, on_saved=saved)
    dialog()


def _pending_dialog(root, data, scope):
    from app_core.roadmap_planning_ui import render_distribution_confirmation, render_schedule_confirmation
    from app_core.roadmap_ui import _advance
    pending = st.session_state.get(scope + "schedule_pending") or st.session_state.get(scope + "distribution_pending")
    if not pending:
        return False
    token = pending["operation_id"]
    st.info("保存前の変更案を表示しています。工程・日別タスクはまだ更新していません。")
    a, b = st.columns(2)
    if a.button("変更内容を確認する", key=scope + "open_pending"):
        st.session_state[scope + "pending_dialog_hidden"] = None
    if b.button("変更案を破棄する", key=scope + "discard_pending"):
        for key in ("schedule_pending", "distribution_pending", "drag_draft"):
            st.session_state.pop(scope + key, None)
        st.rerun()
    if st.session_state.get(scope + "pending_dialog_hidden") == token:
        return False
    @st.dialog("変更内容を確認", width="large", dismissible=False)
    def dialog():
        if st.session_state.get(scope + "schedule_pending"):
            render_schedule_confirmation(root, data, scope, _advance)
        else:
            render_distribution_confirmation(root, data, scope, _advance)
        if st.button("ガントで変更案を確認", key=scope + "peek_pending"):
            st.session_state[scope + "pending_dialog_hidden"] = token
            st.rerun()
    dialog()
    return True


def _fallback_editor_selector(data, scope, disabled):
    def select(collection):
        identifier = st.session_state.get(scope + "fallback_" + collection, "")
        if identifier:
            open_editor(scope, collection, identifier)
    with st.expander("一覧から工程・節目を選ぶ"):
        for collection, label in (("items", "編集する工程"), ("milestones", "編集する試験・節目")):
            entries = {row["id"]: row for row in data[collection]}
            key = scope + "fallback_" + collection
            if st.session_state.get(key, "") not in {"", *entries}:
                st.session_state[key] = ""
            st.selectbox(label, ["", *entries], key=key, disabled=disabled,
                         format_func=lambda value, rows=entries: "選択してください" if not value else rows[value]["title"],
                         on_change=select, args=(collection,))
            if st.button("選択した" + ("工程" if collection == "items" else "節目") + "を編集", disabled=disabled or not st.session_state.get(key), key=key + "_open"):
                open_editor(scope, collection, st.session_state[key])


def render_workspace(root: Path, actuals: dict, scope: str):
    from app_core.roadmap_planning_ui import displayed_schedule, render_capacity_tools, render_schedule_tools
    from app_core.roadmap_ui import _advance, _allocation, _render_plotly
    from prolog_ui.roadmap_canvas import render_roadmap_canvas
    data = load_roadmap(root)
    master = MasterSnapshot.load(root)
    today = app_today()
    pending = bool(st.session_state.get(scope + "schedule_pending") or st.session_state.get(scope + "distribution_pending"))
    requested = st.session_state.pop(scope + "mode_request", None)
    if requested in MODES:
        st.session_state[scope + "mode"] = requested
        st.session_state[scope + "mode_memory"] = requested
    if scope + "mode" not in st.session_state:
        st.session_state[scope + "mode"] = st.session_state.get(scope + "mode_memory", MODES[0])
    with st.container(key="roadmap_toolbar"):
        actions = st.columns([1.1, 1.1, 1.3, 1, 1.2])
        if actions[0].button("工程を追加", type="primary", disabled=pending, key=scope + "new_item"):
            open_editor(scope, "items")
        if actions[1].button("試験・節目を追加", disabled=pending, key=scope + "new_milestone"):
            open_editor(scope, "milestones")
        if actions[2].button("AI計画を取り込む", disabled=pending, key=scope + "open_import"):
            st.session_state[scope + "mode"] = "AI計画の取り込み"
        if actions[3].button("一括調整", disabled=pending, key=scope + "open_schedule"):
            st.session_state[scope + "mode"] = "日程の調整"
        if actions[4].button("実績・計画を更新", key=scope + "refresh"):
            st.session_state.pop(scope + "workload_cache", None)
            st.session_state.pop(scope + "canvas_notice", None)
            st.rerun()
    with st.expander("詳しい操作"):
        mode = st.radio("管理する内容", MODES, horizontal=True, key=scope + "mode", disabled=pending)
    st.session_state[scope + "mode_memory"] = mode
    if pending:
        mode = "学習工程"
    if mode == "AI計画の取り込み":
        from app_core.roadmap_import_ui import render_import
        render_import(root, scope + "import_")
        return
    if mode == "日別タスクとの連携":
        _allocation(root, data, actuals, scope)
        return
    if mode == "学習可能時間・配分":
        _pending_dialog(root, data, scope)
        render_capacity_tools(root, data, scope, _advance)
        return
    if mode == "日程の調整":
        _pending_dialog(root, data, scope)
        render_schedule_tools(root, data, scope)
        return
    jump = st.session_state.pop(scope + "jump_anchor", None)
    jump_view = st.session_state.pop(scope + "jump_view", None)
    if jump_view is not None:
        st.session_state[scope + "view"] = jump_view
    if jump is not None:
        st.session_state[scope + "anchor"] = jump
    view = st.session_state.get(scope + "view", "4週間")
    anchor = st.session_state.get(scope + "anchor") or st.session_state.get(scope + "valid_anchor", today)
    nav = st.columns([.8, .8, .8, 1.5, 1.8])
    if nav[0].button("前へ", key=scope + "previous", disabled=view == "全体"):
        anchor = shift_anchor(anchor, view, -1)
    if nav[1].button("今日", key=scope + "today"):
        anchor = today
        if view == "全体":
            view = "4週間"
            st.session_state[scope + "view"] = view
    if nav[2].button("次へ", key=scope + "next", disabled=view == "全体"):
        anchor = shift_anchor(anchor, view, 1)
    st.session_state[scope + "anchor"] = anchor
    view = nav[3].selectbox("表示範囲", ["週間", "4週間", "月間", "3か月", "全体"],
                           index=1, key=scope + "view")
    anchor = nav[4].date_input("表示の基準日", value=None, min_value=date(1900, 1, 1), max_value=date(2199, 12, 31), key=scope + "anchor") or anchor
    st.session_state[scope + "valid_anchor"] = anchor
    start, end = period_bounds(anchor, view, data)
    with st.expander("表示の補助設定"):
        baseline = st.checkbox("変更前の当初予定を表示", key=scope + "baseline")
        show_load = st.checkbox("日別配分の負荷を表示", key=scope + "show_load")
    workload = None
    if show_load:
        cache = st.session_state.get(scope + "workload_cache")
        cache_key = (start.isoformat(), end.isoformat(), observed_json_revision(roadmap_path(root)))
        if cache is None or cache.get("key") != cache_key:
            if (end - start).days >= 366:
                st.info("日別負荷は1年以内の表示範囲で確認できます。表示範囲を切り替えてください。")
                cache = {"key": cache_key, "rows": None}
            else:
                cache = {"key": cache_key, "rows": build_capacity_strip(root, data, start, end)}
            st.session_state[scope + "workload_cache"] = cache
        workload = cache["rows"]
    shown = displayed_schedule(data, scope)
    rows = build_canvas_rows(shown, master, actuals, today=today)
    original = {row["id"]: row for row in data["items"]}
    for row in rows:
        previous = original[row["id"]]
        row["view_is_draft"] = (row["start_date"], row["end_date"]) != (previous["start_date"], previous["end_date"])
        row["view_before_start"] = previous["start_date"]
        row["view_before_end"] = previous["end_date"]
    names = {record[key]: record.get("name", "") for key in CLASS_KEYS
             for record in master.data.get(key.removesuffix("_id") + "s", [])}
    names.update({row["id"]: row["title"] for row in data["items"]})
    _pending_dialog(root, data, scope)
    event = render_roadmap_canvas(
        rows, data["milestones"], start, end, today=today,
        source_revision=observed_json_revision(roadmap_path(root)), palette=ab.theme_palette(),
        key=scope + "canvas", pending=pending, ack_event_id=st.session_state.get(scope + "canvas_ack", ""),
        names=names, actuals=actuals, calendar=data.get("calendar"),
        view_mode="detail", workload=workload,
        selected_id=st.session_state.get(scope + "canvas_selected", ""),
        show_baseline=baseline, editable=False,
    )
    if event:
        try:
            if handle_canvas_event(root, data, scope, event):
                st.rerun()
        except (ValueError, OSError) as exc:
            if isinstance(event.get("event_id"), str) and re.fullmatch(r"[0-9a-f]{32}", event["event_id"]):
                st.session_state[scope + "canvas_ack"] = event["event_id"]
            st.session_state[scope + "canvas_notice"] = str(exc)
    if st.session_state.get(scope + "canvas_notice"):
        st.warning(st.session_state[scope + "canvas_notice"])
    for warning in actuals.get("warnings", []):
        st.warning(warning)
    _fallback_editor_selector(data, scope, pending)
    with st.expander("一覧・Plotlyの補助表示"):
        show_plotly = st.checkbox("補助のPlotlyを表示", key=scope + "show_plotly")
        if show_plotly:
            visible = [row for row in shown["items"] if not row["is_archived"] and row["end_date"] >= start.isoformat() and row["start_date"] <= end.isoformat()]
            if len(visible) > 100:
                st.caption(f"補助表示は先頭100工程です（対象{len(visible)}工程）。統一ガントでは全工程を検索できます。")
            milestones = [row for row in data["milestones"] if not row["is_archived"] and start.isoformat() <= row["date"] <= end.isoformat()]
            _render_plotly(visible[:100], milestones, start, end, today, master, actuals, baseline, scope, data)
    if not pending:
        _editor_dialog(root, data, master, scope)
