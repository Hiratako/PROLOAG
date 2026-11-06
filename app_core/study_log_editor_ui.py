from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import date, datetime, time
from hashlib import sha256
from pathlib import Path
from typing import Any

import streamlit as st
from web_demo.runtime import fragment as demo_fragment

import prolog_ui.components as ab
from app_core.json_store import JsonStoreError
from app_core.study_log_editor import (
    commit_session_change,
    list_deleted_sessions,
    load_editable_session,
    pending_session_changes,
    prepare_session_delete,
    prepare_session_edit,
    prepare_session_restore,
    rollback_pending_session_change,
)
from data_utils import load_master_data
from app_core.study_category_service import study_category_options


STATE_KEY = "study_log_editor_state"
ERROR_TYPES = (ValueError, JsonStoreError, OSError)
MASTER_FIELDS = (
    ("projects", "project_id", "プロジェクト", ()),
    ("subjects", "subject_id", "科目", ("project_id",)),
    ("materials", "material_id", "教材", ("project_id", "subject_id")),
    ("units", "unit_id", "単元", ("project_id", "subject_id", "material_id")),
)
FIELD_LABELS = {
    "start_at": "開始日時", "end_at": "終了日時", "duration_sec": "実勉強時間",
    "hourly_rate": "適用時給", "memo": "勉強内容", "project_id": "プロジェクトID",
    "subject_id": "科目ID", "material_id": "教材ID", "unit_id": "単元ID",
    "money": "換算金額", "normal_money": "通常分", "overtime_money": "残業分",
    "money_policy": "金額計算設定", "linked_note_ids": "関連ノートID",
    "linked_task_id": "関連タスクID", "created_at": "作成日時", "updated_at": "更新日時",
}
CONDITION_LABELS = {
    "effort": "頑張り度", "environment": "勉強環境", "sleep_hours": "睡眠時間",
    "alcohol_yesterday": "前日の飲酒", "mood": "気分", "category": "勉強カテゴリ",
    "io_type": "インプット/アウトプット",
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _duration_parts(value: Any) -> tuple[int, int, int]:
    seconds = max(int(_number(value) or 0), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return hours, minutes, seconds


def _format_value(value: Any, key: str = "") -> str:
    if value is None or value == "":
        return "未記録"
    if isinstance(value, float) and not math.isfinite(value):
        return "未記録"
    if key == "duration_sec" and _number(value) is not None:
        hours, minutes, seconds = _duration_parts(value)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    if key in {"money", "normal_money", "overtime_money", "hourly_rate"}:
        number = _number(value)
        return f"{number:,.2f}".rstrip("0").rstrip(".") + " 円" if number is not None else str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _session_label(session: dict[str, Any]) -> str:
    start_value = session.get("start_at")
    start_at = str(start_value).replace("T", " ") if _datetime(start_value) is not None else "日時未記録"
    memo = str(session.get("memo") or "内容未記録").replace("\n", " ")
    if len(memo) > 45:
        memo = memo[:44] + "…"
    return f"{start_at} ｜ {_format_value(session.get('duration_sec'), 'duration_sec')} ｜ {memo} [{session.get('session_id', '')}]"


def _reset_snapshot(state: dict[str, Any]) -> None:
    state["snapshot"] = None
    state["plan"] = None
    state["generation"] = int(state.get("generation", 0)) + 1


def _store_plan(state: dict[str, Any], plan: dict[str, Any]) -> None:
    state["plan"] = plan
    state["plan_serial"] = int(state.get("plan_serial", 0)) + 1


def _date_time_update(
    original: Any, selected_date: date | None, selected_time: time | None, label: str,
) -> Any:
    if selected_date is None or selected_time is None:
        raise ValueError(f"{label}の日付と時刻を入力してください。")
    parsed = _datetime(original)
    if parsed is not None:
        displayed_time = parsed.time().replace(tzinfo=None, microsecond=0)
        if selected_date == parsed.date() and selected_time == displayed_time:
            # Keep the original offset, precision and ISO spelling on unrelated edits.
            return original
    selected = datetime.combine(selected_date, selected_time)
    if parsed is not None:
        selected = selected.replace(tzinfo=parsed.tzinfo)
    return selected.isoformat(timespec="seconds")


def _classification_inputs(
    session: dict[str, Any], root: Path, prefix: str, *, disabled: bool,
) -> dict[str, str]:
    master = load_master_data(root)
    selected: dict[str, str] = {}
    for collection, key, label, parents in MASTER_FIELDS:
        rows = [item for item in master.get(collection, []) if isinstance(item, dict)]
        labels = {str(item.get(key)): str(item.get("name") or "名称未設定") for item in rows if item.get(key)}
        options = [""]
        for item in rows:
            item_id = str(item.get(key) or "")
            if not item_id or not item.get("is_active", True):
                continue
            if any(selected.get(parent) and str(item.get(parent) or "") != selected[parent] for parent in parents):
                continue
            if key == "material_id" and not selected.get("subject_id"):
                continue
            if key == "unit_id" and not selected.get("material_id"):
                continue
            if item_id not in options:
                options.append(item_id)
        original = str(session.get(key) or "")
        parents_unchanged = all(str(session.get(parent) or "") == selected.get(parent, "") for parent in parents)
        if original and parents_unchanged and original not in options:
            options.append(original)
            labels[original] = labels.get(original, "登録先不明") + "（現在の値・非表示/不明）"
        widget_key = f"{prefix}_{key}"
        if widget_key not in st.session_state:
            st.session_state[widget_key] = original if original in options else ""
        elif st.session_state[widget_key] not in options:
            st.session_state[widget_key] = ""
        selected[key] = st.selectbox(
            label, options, key=widget_key, disabled=disabled,
            format_func=lambda value, names=labels: f"{names.get(value, '登録先不明')} [{value}]" if value else "未設定",
        )
    return selected


def _condition_inputs(session: dict[str, Any], prefix: str, *, disabled: bool, root: Path | None = None) -> dict[str, Any]:
    original = session.get("condition")
    current = original if isinstance(original, dict) else {}
    updated = deepcopy(current)
    with st.expander("コンディションを修正", expanded=False):
        st.caption("空欄は未記録です。画面にない独自の条件項目はそのまま保持します。")
        for key, minimum, maximum, step in (("effort", 1.0, 10.0, 1.0), ("sleep_hours", 0.0, 24.0, 0.5), ("mood", 1.0, 5.0, 1.0)):
            number = _number(current.get(key))
            initial = number if number is not None and minimum <= number <= maximum else None
            widget_key = f"{prefix}_condition_{key}"
            if widget_key not in st.session_state:
                st.session_state[widget_key] = initial
            value = st.number_input(
                CONDITION_LABELS[key], min_value=minimum, max_value=maximum,
                value=None, step=step, key=widget_key, disabled=disabled,
            )
            if value != initial:
                if value is None:
                    updated[key] = None
                else:
                    updated[key] = int(value) if key != "sleep_hours" and float(value).is_integer() else value
        for key, choices in (
            ("environment", ["", "自宅", "カフェ", "図書館", "ワーキングスペース", "職場", "移動中"]),
            ("alcohol_yesterday", ["", "なし", "あり"]),
            ("category", study_category_options(root, current=str(current.get("category") or ""))),
            ("io_type", ["", "インプット", "アウトプット", "両方"]),
        ):
            initial = str(current.get(key) or "")
            if initial not in choices:
                choices = [*choices, initial]
            value = st.selectbox(
                CONDITION_LABELS[key], choices, index=choices.index(initial),
                key=f"{prefix}_condition_{key}", disabled=disabled,
                format_func=lambda item: item or "未記録",
            )
            if value != initial:
                if value:
                    updated[key] = value
                else:
                    updated[key] = None
    return updated


def _edit_inputs(
    snapshot: dict[str, Any], state: dict[str, Any], root: Path,
    fallback_policy: dict[str, Any], prefix: str,
) -> None:
    session = snapshot["session"]
    locked = state.get("plan") is not None
    with st.expander("分類を修正", expanded=False):
        st.caption("分類を変更する場合は先に選択してください。現在の非表示・不明な分類は、変えなければ保持します。")
        classifications = _classification_inputs(session, root, prefix, disabled=locked)
    start = _datetime(session.get("start_at"))
    end = _datetime(session.get("end_at"))
    initial_duration = _duration_parts(session.get("duration_sec"))
    rate = _number(session.get("hourly_rate"))
    initial_rate = rate if rate is not None and rate >= 0 else None
    memo_initial = str(session.get("memo") or "")
    with st.form(f"{prefix}_form"):
        date_col, time_col, second_col = st.columns([2, 2, 1])
        start_date = date_col.date_input("開始日", value=start.date() if start else None, key=f"{prefix}_start_date", disabled=locked)
        start_clock = time_col.time_input("開始時刻（時:分）", value=start.time().replace(tzinfo=None, second=0, microsecond=0) if start else None, step=60, key=f"{prefix}_start_time", disabled=locked)
        start_second = second_col.number_input("開始秒", min_value=0, max_value=59, value=start.second if start else 0, step=1, key=f"{prefix}_start_second", disabled=locked)
        start_time = start_clock.replace(second=start_second) if start_clock is not None and start_second is not None else None
        date_col, time_col, second_col = st.columns([2, 2, 1])
        end_date = date_col.date_input("終了日", value=end.date() if end else None, key=f"{prefix}_end_date", disabled=locked)
        end_clock = time_col.time_input("終了時刻（時:分）", value=end.time().replace(tzinfo=None, second=0, microsecond=0) if end else None, step=60, key=f"{prefix}_end_time", disabled=locked)
        end_second = second_col.number_input("終了秒", min_value=0, max_value=59, value=end.second if end else 0, step=1, key=f"{prefix}_end_second", disabled=locked)
        end_time = end_clock.replace(second=end_second) if end_clock is not None and end_second is not None else None
        if any(value is not None and value.tzinfo is not None for value in (start, end)):
            st.caption("タイムゾーン付き記録は、保存済みの時刻・タイムゾーンを基準に修正します。")
        st.markdown("実勉強時間")
        st.caption("休止時間は含めません。開始・終了時刻を変えても、この時間は自動では変わりません。")
        hour_col, minute_col, second_col = st.columns(3)
        hours = hour_col.number_input("時間", min_value=0, value=initial_duration[0], step=1, key=f"{prefix}_hours", disabled=locked)
        minutes = minute_col.number_input("分", min_value=0, max_value=59, value=initial_duration[1], step=1, key=f"{prefix}_minutes", disabled=locked)
        seconds = second_col.number_input("秒", min_value=0, max_value=59, value=initial_duration[2], step=1, key=f"{prefix}_seconds", disabled=locked)
        hourly_rate = st.number_input(
            "適用時給（円）", min_value=0.0, value=initial_rate, step=100.0,
            key=f"{prefix}_hourly_rate", disabled=locked,
        )
        if initial_rate is None:
            st.caption("この記録の時給は未記録です。空欄のままなら、時給を勝手に補完しません。")
        memo = st.text_area("勉強内容", value=memo_initial, key=f"{prefix}_memo", disabled=locked)
        condition = _condition_inputs(session, prefix, disabled=locked, root=root)
        submitted = st.form_submit_button("変更内容を確認", type="primary", disabled=locked)
    if submitted:
        try:
            updates: dict[str, Any] = {}
            if any(value is None for value in (hours, minutes, seconds)):
                raise ValueError("実勉強時間の時間・分・秒を入力してください。")
            new_start = _date_time_update(session.get("start_at"), start_date, start_time, "開始")
            new_end = _date_time_update(session.get("end_at"), end_date, end_time, "終了")
            for key, value in (("start_at", new_start), ("end_at", new_end)):
                if value != session.get(key):
                    updates[key] = value
            if (hours, minutes, seconds) != initial_duration or _number(session.get("duration_sec")) is None:
                updates["duration_sec"] = hours * 3600 + minutes * 60 + seconds
            if hourly_rate != initial_rate:
                updates["hourly_rate"] = hourly_rate
            if memo != memo_initial:
                updates["memo"] = memo
            old_condition = session.get("condition") if isinstance(session.get("condition"), dict) else {}
            if condition != old_condition:
                updates["condition"] = condition
            for key, value in classifications.items():
                if value != str(session.get(key) or ""):
                    updates[key] = value
            _store_plan(state, prepare_session_edit(snapshot, updates, root=root, fallback_policy=fallback_policy))
            st.rerun()
        except ERROR_TYPES as exc:
            ab.show_action_error(str(exc), toast_message="変更内容を確認できませんでした。入力と詳細表示を確認してください。")


def _difference_rows(plan: dict[str, Any]) -> list[dict[str, str]]:
    before = plan.get("before") or {}
    after = plan.get("after") or {}
    rows = []
    for key, label in FIELD_LABELS.items():
        if before.get(key) != after.get(key):
            rows.append({"項目": label, "変更前": _format_value(before.get(key), key), "変更後": _format_value(after.get(key), key)})
    before_condition = before.get("condition") if isinstance(before.get("condition"), dict) else {}
    after_condition = after.get("condition") if isinstance(after.get("condition"), dict) else {}
    for key in sorted(set(before_condition) | set(after_condition)):
        if before_condition.get(key) != after_condition.get(key):
            rows.append({"項目": "条件 / " + CONDITION_LABELS.get(key, key), "変更前": _format_value(before_condition.get(key)), "変更後": _format_value(after_condition.get(key))})
    return rows


def _show_plan(
    state: dict[str, Any], root: Path, prefix: str, *, restore: bool = False,
) -> None:
    plan = state.get("plan")
    if not isinstance(plan, dict):
        return
    kind = plan.get("kind")
    st.subheader({"edit": "変更内容の確認", "delete": "削除内容の確認", "restore": "復元内容の確認"}.get(kind, "内容の確認"))
    target = plan.get("after") or plan.get("before") or {}
    st.caption(_session_label(target))
    if kind == "delete":
        st.warning("この記録をごみ箱へ移動し、集計・分析から除きます。関連ノート・タスク自体は残します。")
    elif kind == "restore":
        st.info("ごみ箱の記録を戻し、集計・分析へ再び含めます。関連付けの復元内容も確認してください。")
    differences = _difference_rows(plan)
    if differences:
        st.dataframe(differences, hide_index=True, width="stretch")
    elif kind == "edit":
        st.info("入力値の変更はありません。")
    for warning in plan.get("warnings", []):
        st.warning(str(warning))
    money_changes = plan.get("money_changes") or []
    if money_changes:
        st.markdown("金額が変わる記録")
        st.dataframe([
            {"記録ID": item.get("session_id", ""), "開始日時": item.get("start_at", ""),
             "変更前": _format_value(item.get("before_money"), "money"),
             "変更後": _format_value(item.get("after_money"), "money")}
            for item in sorted(money_changes, key=lambda item: str(item.get("start_at") or ""), reverse=True)
        ], hide_index=True, width="stretch")
    changed_files = plan.get("changed_files") or []
    if changed_files:
        with st.expander(f"更新対象ファイル（{len(changed_files)}件）", expanded=False):
            st.code("\n".join(str(item) for item in changed_files), language=None)
    confirm_key = f"{prefix}_confirm_{state.get('plan_serial', 0)}"
    confirmation = "この記録をごみ箱へ移動することを確認しました" if kind == "delete" else "上の内容と金額の変更を確認しました"
    confirmed = st.checkbox(confirmation, key=confirm_key)
    execute_col, cancel_col = st.columns(2)
    button_label = {"edit": "変更を保存する", "delete": "ごみ箱へ移動", "restore": "記録を復元する"}.get(kind, "保存する")
    if execute_col.button(button_label, key=f"{prefix}_commit", type="primary", disabled=not confirmed, width="stretch"):
        try:
            result = commit_session_change(plan, root=root)
            state["last_backup_path"] = result.get("backup_path", "")
            _reset_snapshot(state)
            if kind == "edit":
                # The selector has already been rendered in this run. Clear it
                # before its next instantiation, not while its widget is active.
                state["clear_selection_after_save"] = True
                state["save_notice"] = "勉強記録を保存しました。編集フォームをクリアしました。"
                ab.queue_toast("勉強記録を保存しました。")
            if restore:
                manager_state = st.session_state.get(STATE_KEY, {})
                _reset_snapshot(manager_state)
            if kind != "edit":
                ab.queue_success({"delete": "勉強記録をごみ箱へ移動しました。", "restore": "勉強記録を復元しました。"}.get(kind, "保存しました。"))
            st.rerun()
        except ERROR_TYPES as exc:
            ab.show_action_error(str(exc), toast_message="勉強記録の更新を完了できませんでした。詳細表示を確認してください。")
            st.caption("別の更新がある場合は、この確認を取り消して記録を再読込してください。")
    if cancel_col.button("確認を取り消す" if restore else "修正入力へ戻る" if kind == "edit" else "削除を取り消す", key=f"{prefix}_cancel", width="stretch"):
        state["plan"] = None
        st.rerun()


def _render_record_editor(
    records: list[dict[str, Any]], state: dict[str, Any], root: Path, fallback_policy: dict[str, Any],
) -> None:
    with st.expander("保存済み記録を修正・削除", expanded=bool(state.get("save_notice"))):
        st.caption("左の絞り込み条件に合う記録から選択します。秒給カウンターと手動追加の両方に対応しています。")
        by_id = {str(record.get("session_id")): record for record in reversed(records) if record.get("session_id")}
        ids = list(reversed(by_id))
        options = ["", *ids]
        selector_key = "study_log_edit_selected_id"
        if state.pop("clear_selection_after_save", False):
            st.session_state[selector_key] = ""
            state["selected_id"] = ""
            state.pop("mode", None)
        if st.session_state.get(selector_key) not in options:
            st.session_state[selector_key] = ""
        selected_id = st.selectbox(
            "修正・削除する記録", options, key=selector_key,
            format_func=lambda value: _session_label(by_id[value]) if value else "記録を選択してください",
        )
        if selected_id != state.get("selected_id"):
            _reset_snapshot(state)
            state["selected_id"] = selected_id
            state.pop("save_notice", None)
        if not selected_id:
            if state.get("save_notice"):
                st.success(state["save_notice"])
            if not records:
                st.info("現在の条件に合う記録はありません。絞り込みを変更するか、下のごみ箱を確認してください。")
            return
        if state.get("snapshot") is None:
            try:
                state["snapshot"] = load_editable_session(selected_id, root=root)
            except ERROR_TYPES as exc:
                st.error(str(exc))
                return
        if st.button("入力を破棄して記録を再読込", key="study_log_edit_reload"):
            _reset_snapshot(state)
            st.rerun()
        snapshot = state["snapshot"]
        token = sha256(selected_id.encode("utf-8")).hexdigest()[:12]
        prefix = f"study_log_edit_{token}_{state.get('generation', 0)}"
        st.caption(f"記録ID: {selected_id} / 保存元: {snapshot.get('source_file', '')}")
        mode = st.radio("操作", ["修正", "削除"], horizontal=True, key=f"{prefix}_mode")
        if mode != state.get("mode"):
            state["plan"] = None
            state["mode"] = mode
        if mode == "修正":
            _edit_inputs(snapshot, state, root, fallback_policy, prefix)
        else:
            st.write(_session_label(snapshot["session"]))
            st.caption("関連ノート・タスク本体は削除せず、この記録への関連付けだけを外します。記録はごみ箱から復元できます。")
            if st.button("削除内容を確認", key=f"{prefix}_prepare_delete", disabled=state.get("plan") is not None):
                try:
                    _store_plan(state, prepare_session_delete(snapshot, root=root, fallback_policy=fallback_policy))
                    st.rerun()
                except ERROR_TYPES as exc:
                    ab.show_action_error(str(exc), toast_message="削除内容を確認できませんでした。詳細表示を確認してください。")
        _show_plan(state, root, prefix)


def _render_trash(root: Path, fallback_policy: dict[str, Any]) -> None:
    with st.expander("ごみ箱から勉強記録を復元", expanded=False):
        try:
            deleted = list_deleted_sessions(root=root)
        except ERROR_TYPES as exc:
            st.error(str(exc))
            return
        if not deleted:
            st.caption("ごみ箱は空です。")
            return
        by_id = {str(item["trash_id"]): item for item in deleted}
        selector_key = "study_log_trash_selected_id"
        if st.session_state.get(selector_key) not in ["", *by_id]:
            st.session_state[selector_key] = ""
        selected = st.selectbox(
            "復元する記録", ["", *by_id], key=selector_key,
            format_func=lambda value: f"削除 {by_id[value].get('deleted_at', '')} ｜ {_session_label(by_id[value].get('session', {}))}" if value else "記録を選択してください",
        )
        state = st.session_state.setdefault("study_log_trash_state", {})
        if state.get("selected_id") != selected:
            state["selected_id"] = selected
            state["plan"] = None
        if not selected:
            return
        prefix = "study_log_restore_" + sha256(selected.encode("utf-8")).hexdigest()[:12]
        restored_session = by_id[selected].get("session") or {}
        missing_rate = _number(restored_session.get("hourly_rate")) is None
        restore_rate = None
        if missing_rate:
            restore_rate = st.number_input(
                "復元する記録の適用時給（円）", min_value=0.0, value=None, step=100.0,
                key=f"{prefix}_hourly_rate", disabled=state.get("plan") is not None,
            )
            st.caption("この旧記録には時給がありません。金額を正しく計算するため、当時の適用時給を入力してください。")
        if st.button("復元内容を確認", key=f"{prefix}_prepare", disabled=state.get("plan") is not None):
            try:
                options = {"root": root, "fallback_policy": fallback_policy}
                if missing_rate:
                    options["hourly_rate"] = restore_rate
                _store_plan(state, prepare_session_restore(selected, **options))
                st.rerun()
            except ERROR_TYPES as exc:
                ab.show_action_error(str(exc), toast_message="復元内容を確認できませんでした。入力と詳細表示を確認してください。")
        _show_plan(state, root, prefix, restore=True)


def _render_pending_changes(root: Path, state: dict[str, Any]) -> bool:
    try:
        pending = pending_session_changes(root=root)
    except ERROR_TYPES as exc:
        st.error(str(exc))
        return True
    if not pending:
        return False
    st.warning("途中で中断した勉強ログの更新があります。新しい修正の前に、更新前の状態へ戻してください。")
    by_id = {str(item["operation_id"]): item for item in pending}
    selected = st.selectbox(
        "復旧する更新", list(by_id), key="study_log_recovery_operation",
        format_func=lambda value: f"{by_id[value].get('created_at', '')} / {by_id[value].get('kind', '')} / {value}",
    )
    confirmed = st.checkbox("この更新を中断前の状態へ戻すことを確認しました", key=f"study_log_recovery_confirm_{selected}")
    if st.button("中断した更新を元に戻す", key="study_log_recovery_apply", disabled=not confirmed):
        try:
            rollback_pending_session_change(selected, root=root)
            _reset_snapshot(state)
            st.session_state.pop("study_log_trash_state", None)
            ab.queue_success("中断した更新を、更新前の状態へ戻しました。")
            st.rerun()
        except ERROR_TYPES as exc:
            ab.show_action_error(str(exc), toast_message="勉強ログの復旧を完了できませんでした。詳細表示を確認してください。")
    return True


@demo_fragment
def render_study_log_management(
    records: list[dict[str, Any]], *, root: Path, fallback_policy: dict[str, Any],
) -> None:
    """Keep classification and its save/confirmation UI in one partial update.

    The parent refreshes filtered records when a filter changes. Classification
    loads its own current master data, while writes still use the editor's
    original revision guards and refresh the complete page on success.
    """
    editor_frame = st.empty()
    with editor_frame.container():
        state = st.session_state.setdefault(STATE_KEY, {})
        if _render_pending_changes(root, state):
            return
        try:
            _render_record_editor(records, state, root, fallback_policy)
        except ERROR_TYPES as exc:
            st.error(str(exc))
        _render_trash(root, fallback_policy)
