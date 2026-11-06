"""Reviewable schedule and capacity tools; source files change only on save."""

from copy import deepcopy
from datetime import date, datetime, timedelta
from uuid import uuid4
import math
import re

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import prolog_ui.components as ab
from app_core.app_clock import app_today
from app_core.form_snapshot import discard_form_snapshot, form_snapshot
from app_core.json_store import observed_json_revision
from app_core.roadmap_service import parse_day, roadmap_path
from app_core.roadmap_planning import (
    apply_distribution_preview,
    apply_schedule_preview,
    build_distribution_preview,
    calendar_settings,
    content_hash,
    load_planning_context,
    load_rows,
    save_calendar,
    schedule_preview,
    shift_changes,
    weekly_load,
)


def _integer(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(label + "を整数で入力してください。") from exc
    if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer():
        raise ValueError(label + "を整数で入力してください。")
    return int(number)


def start_schedule_preview(root, data, scope, changes, *, cascade=True):
    preview = schedule_preview(data, changes, cascade=cascade)
    if not preview["changes"]:
        raise ValueError("変更する日程がありません。")
    st.session_state[scope + "schedule_pending"] = {
        "preview": preview,
        "revision": observed_json_revision(roadmap_path(root)),
        "operation_id": uuid4().hex,
    }


def consume_drag(root, data, scope, request):
    event_id = request.get("event_id")
    if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{32}", event_id):
        raise ValueError("日程操作の識別情報が不正です。")
    if event_id == st.session_state.get(scope + "drag_ack"):
        return
    st.session_state[scope + "drag_ack"] = event_id
    st.session_state[scope + "drag_draft"] = deepcopy(request)
    item = next(
        (row for row in data["items"] if row["id"] == request.get("item_id")), None
    )
    if (
        item is None
        or request.get("source_revision") != observed_json_revision(roadmap_path(root))
        or (request.get("old_start"), request.get("old_end"))
        != (item["start_date"], item["end_date"])
    ):
        raise ValueError(
            "操作中に計画が更新されました。変更案を保持しています。「日程の調整」で最新の計画に対する案を作り直せます。"
        )
    start_schedule_preview(
        root,
        data,
        scope,
        {
            item["id"]: {
                "start_date": request.get("start_date"),
                "end_date": request.get("end_date"),
            }
        },
        cascade=True,
    )


def displayed_schedule(data, scope):
    pending = st.session_state.get(scope + "schedule_pending")
    if not pending or pending["preview"]["base_hash"] != content_hash(data):
        return data
    result = deepcopy(data)
    items = {row["id"]: row for row in result["items"]}
    for change in pending["preview"]["changes"]:
        items[change["item_id"]].update(
            start_date=change["start_date"], end_date=change["end_date"]
        )
    return result


def render_schedule_confirmation(root, data, scope, advance):
    pending = st.session_state.get(scope + "schedule_pending")
    if not pending:
        return False
    preview = pending["preview"]
    st.subheader("日程変更の確認")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "工程": row["title"],
                    "変更前の開始": row["before_start"],
                    "変更前の終了": row["before_end"],
                    "変更後の開始": row["start_date"],
                    "変更後の終了": row["end_date"],
                }
                for row in preview["changes"]
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    for warning in preview["warnings"]:
        st.warning(warning)
    if preview["base_hash"] != content_hash(data):
        st.warning(
            "プレビュー後に計画が更新されています。保存時に照合し、一致しない場合は保存しません。"
        )
    a, b = st.columns(2)
    if a.button("日程変更を保存", key=scope + "schedule_save", type="primary"):
        try:
            committed = apply_schedule_preview(
                root,
                preview,
                expected_revision=pending["revision"],
                operation_id=pending["operation_id"],
            )
        except (ValueError, OSError) as exc:
            ab.show_action_error(f"保存できませんでした。変更案を保持しています。{exc}")
        else:
            if committed:
                advance(root, {"revisions": {roadmap_path(root): pending["revision"]}})
            st.session_state.pop(scope + "schedule_pending", None)
            st.session_state.pop(scope + "drag_draft", None)
            ab.queue_success(
                "日程変更を保存しました。"
                if committed
                else "この日程変更は保存済みです。"
            )
            st.rerun()
    if b.button("日程変更を取り消す", key=scope + "schedule_cancel"):
        st.session_state.pop(scope + "schedule_pending", None)
        st.session_state.pop(scope + "drag_draft", None)
        st.rerun()
    return True


def render_schedule_tools(root, data, scope):
    eligible = {
        row["id"]: row
        for row in data["items"]
        if not row["is_archived"] and row["status"] != "done"
    }
    if not eligible:
        st.info("移動できる学習工程を登録してください。")
        return
    draft = st.session_state.get(scope + "shift_draft", {})
    with st.form(scope + "shift_form"):
        selected = st.multiselect(
            "移動する工程",
            list(eligible),
            default=[key for key in draft.get("selected", []) if key in eligible],
            format_func=lambda value: eligible[value]["title"],
        )
        days = st.number_input(
            "移動する日数（負の値で前倒し）",
            min_value=-3650,
            max_value=3650,
            value=draft.get("days", 1),
            step=1,
        )
        include = st.checkbox(
            "後続工程も同じ日数だけ移動する", value=draft.get("include", True)
        )
        submitted = st.form_submit_button("一括移動のプレビュー", type="primary")
    if submitted:
        st.session_state[scope + "shift_draft"] = {
            "selected": selected,
            "days": days,
            "include": include,
        }
        try:
            start_schedule_preview(
                root,
                data,
                scope,
                shift_changes(data, selected, days, include_descendants=include),
            )
        except (ValueError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            st.rerun()
    if st.button("前提関係に合わせて日程を整える", key=scope + "repair_order"):
        try:
            start_schedule_preview(root, data, scope, {})
        except (ValueError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            st.rerun()
    draft = st.session_state.get(scope + "drag_draft")
    if draft and not st.session_state.get(scope + "schedule_pending"):
        st.caption(
            f"保持した変更案：{draft.get('start_date', '')} 〜 {draft.get('end_date', '')}"
        )
        if st.button("最新の計画に対する変更案を作り直す", key=scope + "rebase_drag"):
            try:
                start_schedule_preview(
                    root,
                    data,
                    scope,
                    {
                        draft.get("item_id"): {
                            "start_date": draft.get("start_date"),
                            "end_date": draft.get("end_date"),
                        }
                    },
                )
            except (ValueError, OSError) as exc:
                ab.show_action_error(str(exc))
            else:
                st.rerun()
    st.caption(
        "前提工程の終了翌日から次の工程を開始します。完了・アーカイブ済みの工程と、既存の日別タスクは一括移動しません。"
    )


def _calendar_form(root, data, scope, advance):
    path = roadmap_path(root)
    key = scope + "capacity_baseline"
    edit = form_snapshot(st.session_state, key, [path], data)
    calendar = calendar_settings(edit["data"])
    prefix = scope + "calendar_" + str(edit["version"])
    if st.button("学習可能時間の入力を破棄して再読込", key=prefix + "reload"):
        discard_form_snapshot(st.session_state, key)
        st.rerun()
    with st.form(prefix + "form"):
        weekdays = ["月曜", "火曜", "水曜", "木曜", "金曜", "土曜", "日曜"]
        table = st.data_editor(
            pd.DataFrame(
                {"曜日": weekdays, "学習可能時間（分）": calendar["weekly_minutes"]}
            ),
            hide_index=True,
            disabled=["曜日"],
            column_config={
                "学習可能時間（分）": st.column_config.NumberColumn(
                    min_value=0, max_value=1440, step=15, required=True
                )
            },
            key=prefix + "week",
            width="stretch",
        )
        overrides = pd.DataFrame(
            [
                {"日付": parse_day(day), "学習可能時間（分）": minutes}
                for day, minutes in calendar["overrides"].items()
            ],
            columns=["日付", "学習可能時間（分）"],
        )
        exceptions = st.data_editor(
            overrides,
            hide_index=True,
            num_rows="dynamic",
            key=prefix + "exceptions",
            width="stretch",
            column_config={
                "日付": st.column_config.DateColumn(
                    required=True,
                    min_value=date(1900, 1, 1),
                    max_value=date(2199, 12, 31),
                ),
                "学習可能時間（分）": st.column_config.NumberColumn(
                    min_value=0, max_value=1440, step=15, required=True
                ),
            },
        )
        st.caption(
            "休む曜日や日付は0分に設定します。日付別の設定を曜日設定より優先します。"
        )
        submitted = st.form_submit_button("学習可能時間を保存", type="primary")
    if submitted:
        try:
            weekly = [
                _integer(value, "曜日ごとの時間")
                for value in table["学習可能時間（分）"]
            ]
            values = {}
            for row in exceptions.to_dict("records"):
                day = row.get("日付")
                minutes = row.get("学習可能時間（分）")
                if pd.isna(day) and pd.isna(minutes):
                    continue
                if isinstance(day, datetime):
                    day = day.date()
                day = day.isoformat() if isinstance(day, date) else str(day)
                parse_day(day)
                if day in values:
                    raise ValueError("同じ例外日が重複しています。")
                values[day] = _integer(minutes, "日付別の時間")
            committed = save_calendar(
                root,
                {"weekly_minutes": weekly, "overrides": values},
                expected_revision=edit["revisions"][path],
                operation_id=edit["operation_id"],
            )
        except (ValueError, OSError) as exc:
            ab.show_action_error(f"保存できませんでした。入力を保持しています。{exc}")
        else:
            if committed:
                advance(root, edit)
            discard_form_snapshot(st.session_state, key)
            ab.queue_success("学習可能時間を保存しました。")
            st.rerun()


def _load_display(rows, prefix):
    weeks = weekly_load(rows)
    palette = ab.theme_palette()
    figure = go.Figure()
    for field, label, color in (
        ("existing_minutes", "既存の予定", palette["cyan"]),
        ("additional_minutes", "今回の追加", palette["green"]),
    ):
        figure.add_bar(
            x=[row["week"] for row in weeks],
            y=[row[field] / 60 for row in weeks],
            name=label,
            marker_color=color,
        )
    figure.add_scatter(
        x=[row["week"] for row in weeks],
        y=[row["capacity_minutes"] / 60 for row in weeks],
        name="学習可能時間",
        mode="lines+markers",
        line_color=palette["purple"],
    )
    figure.update_layout(
        barmode="stack",
        height=300,
        margin={"l": 12, "r": 12, "t": 16, "b": 24},
        yaxis_title="時間",
        xaxis_title="週の開始日（月曜）",
        legend={"orientation": "h"},
        paper_bgcolor=palette["card"],
        plot_bgcolor=palette["card"],
        font_color=palette["ink"],
    )
    st.plotly_chart(
        figure,
        width="stretch",
        key=prefix + "load_chart",
        config={"displaylogo": False},
    )
    st.caption(
        "週の値は、指定した表示期間に含まれる日だけを合計しています。予定時間は日別タスクの登録値です。"
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "日付": row["date"],
                    "可能（分）": row["capacity_minutes"],
                    "既存予定（分）": row["existing_minutes"],
                    "今回追加（分）": row["additional_minutes"],
                    "合計（分）": row["total_minutes"],
                    "空き（分）": row["free_minutes"],
                    "超過（分）": row["over_minutes"],
                }
                for row in rows
            ]
        ),
        hide_index=True,
        width="stretch",
    )


def render_distribution_confirmation(root, data, scope, advance):
    pending = st.session_state.get(scope + "distribution_pending")
    if not pending:
        return False
    preview = pending["preview"]
    st.subheader("日別配分の確認")
    st.caption(
        f"新たに追加する予定：{sum(row['minutes'] for row in preview['rows'])}分 / {len(preview['rows'])}件。"
    )
    st.dataframe(
        pd.DataFrame(preview["rows"])
        .rename(
            columns={"title": "工程", "date": "割当日", "minutes": "追加する時間（分）"}
        )
        .drop(columns=["item_id"], errors="ignore"),
        hide_index=True,
        width="stretch",
    )
    _load_display(preview["load"], scope + "preview_")
    partial = False
    if preview["unassigned"]:
        st.warning(
            "期間・期限内に配分できない時間があります。工程の順序・空き時間を見直すか、配分できた分だけ保存できます。"
        )
        st.dataframe(
            pd.DataFrame(preview["unassigned"])
            .rename(
                columns={"title": "工程", "minutes": "未配分（分）", "reason": "理由"}
            )
            .drop(columns=["item_id"]),
            hide_index=True,
            width="stretch",
        )
        partial = st.checkbox(
            "未配分時間を確認し、配分できた分だけ保存する",
            key=scope + pending["operation_id"] + "partial",
        )
    a, b = st.columns(2)
    if a.button(
        "配分案を保存",
        type="primary",
        disabled=not preview["rows"] or (bool(preview["unassigned"]) and not partial),
        key=scope + "distribution_save",
    ):
        try:
            committed = apply_distribution_preview(
                root,
                preview,
                expected_revision=pending["revision"],
                operation_id=pending["operation_id"],
                allow_partial=partial,
            )
        except (ValueError, OSError) as exc:
            ab.show_action_error(f"保存できませんでした。配分案を保持しています。{exc}")
        else:
            if committed:
                revisions = {roadmap_path(root): pending["revision"]}
                changed = {row["date"] for row in preview["rows"]}
                from data_utils import daily_plan_path

                for day in changed:
                    target = daily_plan_path(root, parse_day(day))
                    relative = target.relative_to(root).as_posix()
                    revisions[target] = preview["plan_revisions"][relative]
                advance(root, {"revisions": revisions})
            st.session_state.pop(scope + "distribution_pending", None)
            st.session_state.pop(scope + "load_context", None)
            ab.queue_success(
                "日別タスクに配分しました。"
                if committed
                else "この配分案は保存済みです。"
            )
            st.rerun()
    if b.button("配分案を取り消す", key=scope + "distribution_cancel"):
        st.session_state.pop(scope + "distribution_pending", None)
        st.rerun()
    return True


def render_capacity_tools(root, data, scope, advance):
    with st.expander(
        "曜日・例外日の学習可能時間",
        expanded=not any(calendar_settings(data)["weekly_minutes"]),
    ):
        _calendar_form(root, data, scope, advance)
    a, b = st.columns(2)
    draft = st.session_state.get(scope + "distribution_draft", {})
    start = a.date_input(
        "配分・負荷確認の開始日",
        value=parse_day(draft["start"]) if draft.get("start") else app_today(),
        min_value=date(1900, 1, 1),
        max_value=date(2199, 12, 31),
        key=scope + "planning_start",
    )
    end = b.date_input(
        "配分・負荷確認の終了日",
        value=parse_day(draft["end"])
        if draft.get("end")
        else app_today() + timedelta(days=27),
        min_value=date(1900, 1, 1),
        max_value=date(2199, 12, 31),
        key=scope + "planning_end",
    )
    if st.button("負荷を確認・更新", key=scope + "load_refresh"):
        try:
            plans, _ = load_planning_context(root, start, end)
        except (ValueError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            st.session_state[scope + "load_context"] = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "plans": plans,
                "at": datetime.now().strftime("%H:%M:%S"),
            }
    context = st.session_state.get(scope + "load_context")
    if context and (context["start"], context["end"]) == (
        start.isoformat(),
        end.isoformat(),
    ):
        st.caption("日別計画の確認時刻：" + context["at"])
        _load_display(load_rows(data, context["plans"], start, end), scope + "current_")
    eligible = {
        row["id"]: row
        for row in data["items"]
        if not row["is_archived"] and row["status"] not in {"done", "pending"}
    }
    if not eligible:
        st.info("配分できる工程を登録してください。")
        return
    selected = st.multiselect(
        "自動配分する工程",
        list(eligible),
        default=[key for key in draft.get("values", {}) if key in eligible],
        format_func=lambda value: eligible[value]["title"],
        max_selections=100,
        key=scope + "distribution_items",
    )
    if not selected:
        return
    with st.form(scope + "distribution_form_" + content_hash(selected)[:12]):
        requests = st.data_editor(
            pd.DataFrame(
                [
                    {
                        "id": key,
                        "工程": eligible[key]["title"],
                        "工程全体の予定（分）": eligible[key]["planned_minutes"],
                        "今回追加する時間（分）": draft.get("values", {}).get(key, 0),
                    }
                    for key in selected
                ]
            ),
            hide_index=True,
            disabled=["id", "工程", "工程全体の予定（分）"],
            column_config={
                "id": None,
                "今回追加する時間（分）": st.column_config.NumberColumn(
                    min_value=0, max_value=600000, step=15, required=True
                ),
            },
            width="stretch",
        )
        st.caption(
            "今回新たに追加する時間を入力してください。既存タスクを含めた空き時間へ、前提関係・工程の期間・確定期限を守って配分します。"
        )
        submitted = st.form_submit_button("配分案を作成", type="primary")
    if submitted:
        try:
            values = {
                row["id"]: _integer(row["今回追加する時間（分）"], "追加する時間")
                for row in requests.to_dict("records")
            }
            st.session_state[scope + "distribution_draft"] = {
                "values": values,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
            preview = build_distribution_preview(root, data, values, start, end)
            st.session_state[scope + "distribution_pending"] = {
                "preview": preview,
                "revision": observed_json_revision(roadmap_path(root)),
                "operation_id": uuid4().hex,
            }
        except (ValueError, OSError) as exc:
            ab.show_action_error(
                f"配分案を作成できませんでした。入力を保持しています。{exc}"
            )
        else:
            st.rerun()
