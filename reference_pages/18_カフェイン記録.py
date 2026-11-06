from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.app_clock import app_now, app_today
from app_core.form_snapshot import discard_form_snapshot, form_snapshot
from app_core.diary_service import get_diary_entry_path, load_diary_entry
from app_core.settings_service import load_user_settings
from app_core.substance_service import (
    CAFFEINE_SUBSTANCE_ID,
    add_substance_record,
    format_mg,
    latest_substance_record,
    load_substance_profiles,
    model_snapshot_for_profile,
    parse_substance_datetime,
    records_for_substance,
    replace_substance_records_for_day,
    substance_records_total,
)


def parse_record_time(value: Any, fallback: dt_time | None = None) -> dt_time:
    try:
        return datetime.fromisoformat(str(value)).time().replace(microsecond=0)
    except (TypeError, ValueError):
        return fallback or app_now().time().replace(second=0, microsecond=0)


def clean_records(
    records: list[dict[str, Any]],
    target: date,
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for index, item in enumerate(records):
        if item.get("remove"):
            continue
        record_id = str(item.get("substance_record_id") or "")
        try:
            amount_mg = round(max(float(item.get("amount_mg", 0) or 0), 0.0), 6)
        except (TypeError, ValueError):
            amount_mg = 0.0
        taken_time = item.get("taken_time")
        if amount_mg <= 0:
            if record_id:
                raise ValueError(f"既存の{index + 1}行目を削除する場合は「削除」を選択してください。")
            continue
        if not isinstance(taken_time, dt_time):
            continue
        # A legacy/offset timestamp can belong to a different local calendar
        # day than its owner file. Editing its memo or clock time must not move
        # that existing intake onto the selected file date.
        original_timestamp = parse_substance_datetime(item.get("taken_at"), target) if record_id else None
        intake_date = original_timestamp.date() if original_timestamp is not None else target
        record = {
            "substance_record_id": str(
                record_id
                or f"{profile['substance_id']}_{target:%Y%m%d}_{uuid4().hex[:12]}"
            ),
            "substance_id": str(profile["substance_id"]),
            "taken_at": datetime.combine(intake_date, taken_time).isoformat(timespec="seconds"),
            "amount_mg": amount_mg,
            "unit": "mg",
            "source": str(item.get("source") or "substance_record_page"),
            "label": str(item.get("label") or "").strip(),
            "memo": str(item.get("memo") or "").strip(),
            "model_snapshot": (
                item.get("model_snapshot")
                if isinstance(item.get("model_snapshot"), dict)
                else model_snapshot_for_profile(profile)
            ),
            "created_at": str(item.get("created_at") or updated_at),
            "updated_at": updated_at,
        }
        cleaned.append(record)
    return cleaned


def recent_summary(
    end_date: date,
    substance_id: str,
    profiles: dict[str, dict[str, Any]],
    days: int = 14,
    *,
    current_entry: dict[str, Any] | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        target = end_date - timedelta(days=offset)
        if not get_diary_entry_path(target).exists():
            continue
        entry = current_entry if target == end_date and current_entry is not None else load_diary_entry(target, profiles=profiles)
        records = records_for_substance(entry, substance_id, profiles=profiles)
        timed_total = substance_records_total(records)
        untimed_mg = (
            max(float(entry.get("caffeine_untimed_mg", 0) or 0), 0.0)
            if substance_id == CAFFEINE_SUBSTANCE_ID
            else 0.0
        )
        if not records and untimed_mg <= 0 and substance_id != CAFFEINE_SUBSTANCE_ID:
            continue
        latest = latest_substance_record(records)
        rows.append(
            {
                "日付": target.isoformat(),
                "日次合計mg": timed_total + untimed_mg,
                "時刻付きmg": timed_total,
                "時刻不明mg": untimed_mg,
                "記録数": len(records),
                "最終摂取": str(latest.get("taken_at", ""))[11:16] if latest else "",
            }
        )
    summary = pd.DataFrame(rows)
    if summary.empty:
        return summary
    for column in ("日次合計mg", "時刻付きmg", "時刻不明mg"):
        summary[column] = summary[column].round(3)
    return summary.sort_values(
        "日付",
        ascending=False,
        na_position="last",
        kind="stable",
    ).reset_index(drop=True)


@st.fragment
def render_substance_editor(
    target: date, selected_id: str, fallback_profile: dict[str, Any], *,
    initial_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Refresh the selected day and settings on every partial run. Form baselines
    # remain fixed until save/discard, independently of these display reads.
    # The outer render and 14-day table share this list on the initial call only.
    all_profiles = initial_context.pop("profiles", None) if initial_context is not None else None
    if all_profiles is None:
        all_profiles = load_substance_profiles(include_inactive=True)
    profile_lookup = {str(item["substance_id"]): item for item in all_profiles}
    active_ids = [str(item["substance_id"]) for item in all_profiles if item.get("is_active", True)]
    if selected_id not in profile_lookup:
        profile_lookup[selected_id] = {**fallback_profile, "is_active": False}
        st.warning("この項目の設定が変更されています。入力内容を確認し、ページを再表示してください。")
    profile = profile_lookup[selected_id]
    default_amount_mg = float(profile.get("default_amount_mg", 1.0) or 1.0)
    default_amount_step = float(profile.get("amount_step_mg", 0.5) or 0.5)
    default_label = str(profile.get("default_label") or "")
    input_defaults_key = sha256(
        repr((default_amount_mg, default_amount_step, default_label)).encode("utf-8")
    ).hexdigest()[:12]

    live_entry = load_diary_entry(target, profiles=profile_lookup)
    entry = live_entry
    edit_key = f"substance:{target.isoformat()}:{selected_id}"
    edit = form_snapshot(st.session_state, edit_key, [get_diary_entry_path(target)], entry)
    if st.button("履歴の入力を破棄して最新データを再読込", key=f"reload_{edit_key}"):
        discard_form_snapshot(st.session_state, edit_key)
        st.rerun()
    entry = edit["data"]
    records = records_for_substance(entry, selected_id, profiles=profile_lookup)
    latest = latest_substance_record(records)
    timed_total = substance_records_total(records)
    untimed_total = (
        max(float(entry.get("caffeine_untimed_mg", 0) or 0), 0.0)
        if selected_id == CAFFEINE_SUBSTANCE_ID
        else 0.0
    )

    summary_cols = st.columns(4)
    with summary_cols[0]:
        ab.metric_block(
            f"{profile['name']} 日次合計",
            f"{format_mg(timed_total + untimed_total)} mg",
            "時刻付き + 時刻不明",
        )
    with summary_cols[1]:
        ab.metric_block("時刻付き", f"{format_mg(timed_total)} mg", f"{len(records)}件")
    with summary_cols[2]:
        ab.metric_block(
            "時刻不明",
            f"{format_mg(untimed_total)} mg",
            "カフェイン旧記録のみ" if selected_id == CAFFEINE_SUBSTANCE_ID else "この項目にはありません",
        )
    with summary_cols[3]:
        latest_text = str(latest.get("taken_at", ""))[11:16] if latest else "未記録"
        ab.metric_block("最終摂取", latest_text, str(latest.get("label") or "") if latest else "時刻付き記録なし")

    if selected_id not in active_ids:
        st.info("この項目はアーカイブされています。過去記録の編集はできますが、新しい記録を追加するにはユーザー設定で再表示してください。")
    else:
        quick_version = int(st.session_state.get("substance_quick_version", 0))
        quick_key = f"{selected_id}_{target.isoformat()}_{quick_version}_{input_defaults_key}"
        st.subheader("摂取を追加")
        with st.form(f"substance_quick_add_{quick_key}"):
            time_col, amount_col, label_col = st.columns([1, 1, 1.6])
            taken_time = time_col.time_input(
                "摂取時刻",
                value=app_now().time().replace(second=0, microsecond=0),
                key=f"substance_quick_time_{quick_key}",
            )
            amount_mg = amount_col.number_input(
                "摂取量（mg）",
                min_value=0.001,
                max_value=100000.0,
                value=default_amount_mg,
                step=default_amount_step,
                format="%.3f",
                key=f"substance_quick_amount_{quick_key}",
            )
            label = label_col.text_input(
                "製品・剤形（任意）",
                value=default_label,
                placeholder="例: コーヒー、錠剤、徐放タイプ",
                key=f"substance_quick_label_{quick_key}",
            )
            memo = st.text_input(
                "メモ（任意）",
                key=f"substance_quick_memo_{quick_key}",
            )
            quick_submit = st.form_submit_button(f"{profile['name']}を記録", type="primary", width="stretch")

        if quick_submit:
            try:
                updated = add_substance_record(
                    target,
                    selected_id,
                    amount_mg,
                    taken_at=datetime.combine(target, taken_time),
                    source="substance_record_page",
                    label=label,
                    memo=memo,
                )
            except (ValueError, OSError) as exc:
                ab.show_action_error(str(exc))
            else:
                updated_records = records_for_substance(updated, selected_id, profiles=profile_lookup)
                st.session_state.substance_quick_version = quick_version + 1
                st.session_state.substance_editor_version = int(st.session_state.get("substance_editor_version", 0)) + 1
                discard_form_snapshot(st.session_state, edit_key)
                ab.queue_success(
                    f"{profile['name']} {format_mg(amount_mg)} mgを{taken_time.strftime('%H:%M')}に記録しました。"
                    f"日次合計は{format_mg(substance_records_total(updated_records) + untimed_total)} mgです。"
                )
                st.rerun()

    st.subheader("選択日の履歴を編集")
    editor_version = edit["version"]
    is_active = selected_id in active_ids
    has_editable_history = bool(records) or (
        selected_id == CAFFEINE_SUBSTANCE_ID and untimed_total > 0
    )
    editor_submit = False
    edited_records: list[dict[str, Any]] = []
    untimed_mg: float | None = None
    if is_active:
        st.caption(f"{profile['name']}の履歴だけを編集します。既存行の削除は「削除」を選び、追加行を使わない場合は0 mgのままにします。")
        row_count = st.number_input(
            "履歴の入力行数",
            min_value=max(1, len(records)),
            max_value=max(50, len(records) + 20),
            value=max(1, len(records) + 1),
            step=1,
            key=f"substance_row_count_{selected_id}_{target.isoformat()}_{editor_version}",
        )
    elif has_editable_history:
        st.caption(f"{profile['name']}はアーカイブ中のため、既存履歴の編集・削除だけ行えます。")
        row_count = len(records)
    else:
        row_count = 0
        st.info("この日に編集できる過去記録はありません。")

    if is_active or has_editable_history:
        with st.form(f"substance_editor_{selected_id}_{target.isoformat()}_{editor_version}"):
            if selected_id == CAFFEINE_SUBSTANCE_ID:
                untimed_mg = st.number_input(
                    "時刻不明分（mg）",
                    min_value=0.0,
                    max_value=100000.0,
                    value=float(untimed_total),
                    step=float(profile.get("amount_step_mg", 25.0) or 25.0),
                    format="%.3f",
                    help="既存データなど、摂取時刻を特定できないカフェイン量だけを入力します。",
                    key=f"substance_untimed_{selected_id}_{target.isoformat()}_{editor_version}",
                )
            for index in range(int(row_count)):
                old = records[index] if index < len(records) else {}
                record_key = str(old.get("substance_record_id") or f"new_{index - len(records)}")
                original_timestamp = parse_substance_datetime(old.get("taken_at"), target)
                if original_timestamp is not None and original_timestamp.date() != target:
                    st.caption(
                        f"{index + 1}行目の摂取日: {original_timestamp.date().isoformat()}"
                        f"（保存先の日付: {target.isoformat()}）。時刻だけ変更し、摂取日は保持します。"
                    )
                time_col, amount_col, label_col, memo_col, remove_col = st.columns([1.1, 1, 1.6, 2.1, 0.8])
                edited_records.append(
                    {
                        "substance_record_id": old.get("substance_record_id", ""),
                        "taken_at": old.get("taken_at", ""),
                        "taken_time": time_col.time_input(
                            f"摂取時刻 {index + 1}",
                            value=parse_record_time(old.get("taken_at")),
                            key=f"substance_edit_time_{selected_id}_{target.isoformat()}_{editor_version}_{record_key}",
                        ),
                        "amount_mg": amount_col.number_input(
                            f"摂取量 {index + 1}（mg）",
                            min_value=0.0,
                            max_value=100000.0,
                            value=float(old.get("amount_mg", 0) or 0),
                            step=float(profile.get("amount_step_mg", 0.5) or 0.5),
                            format="%.3f",
                            key=f"substance_edit_amount_{selected_id}_{target.isoformat()}_{editor_version}_{record_key}",
                        ),
                        "label": label_col.text_input(
                            f"製品・剤形 {index + 1}",
                            value=str(old.get("label", "")) if old else default_label,
                            key=(
                                f"substance_edit_label_{selected_id}_{target.isoformat()}_{editor_version}_{record_key}"
                                + (f"_{input_defaults_key}" if not old else "")
                            ),
                        ),
                        "memo": memo_col.text_input(
                            f"メモ {index + 1}",
                            value=str(old.get("memo", "")),
                            key=f"substance_edit_memo_{selected_id}_{target.isoformat()}_{editor_version}_{record_key}",
                        ),
                        "remove": remove_col.checkbox(
                            f"削除 {index + 1}",
                            value=False,
                            disabled=not bool(old),
                            key=f"substance_edit_remove_{selected_id}_{target.isoformat()}_{editor_version}_{record_key}",
                        ),
                        "source": old.get("source", "substance_record_page"),
                        "model_snapshot": old.get("model_snapshot"),
                        "created_at": old.get("created_at", ""),
                    }
                )
            editor_submit = st.form_submit_button("履歴を保存", type="primary", width="stretch")

    if editor_submit:
        try:
            cleaned_records = clean_records(edited_records, target, profile)
            replace_substance_records_for_day(
                target,
                selected_id,
                cleaned_records,
                caffeine_untimed_mg=untimed_mg,
                expected_revision=edit["revisions"][get_diary_entry_path(target)],
                expected_record_ids={str(item["substance_record_id"]) for item in records},
            )
        except (ValueError, OSError) as exc:
            ab.show_action_error(f"保存できませんでした。入力は保持しています。{exc}")
        else:
            st.session_state.substance_editor_version = editor_version + 1
            discard_form_snapshot(st.session_state, edit_key)
            ab.queue_success(f"{profile['name']}の摂取履歴を保存しました。")
            st.rerun()

    return live_entry


st.set_page_config(page_title="服薬ログ", layout="wide")
settings = load_user_settings()
ab.inject_css(settings=settings)
sidebar_settings = ab.sidemenu("お薬やカフェインの摂取時刻と量を記録・管理します。", settings=settings)
ab.topbar("服薬ログ", "お薬やカフェインを選び、摂取時刻、摂取量（mg）、製品・剤形、メモを記録します。")

with st.sidebar:
    target = st.date_input("記録日", value=app_today(), key="substance_record_date")
    st.caption("保存先は日記JSONです。日記本文とは独立した服薬・摂取データだけを更新します。")
    ab.sidebar_page_link("pages/16_カフェイン・運動分析.py", label="成分リズム・コンディション分析を開く", settings=sidebar_settings)
    ab.sidebar_page_link("pages/98_ユーザー設定.py", label="お薬・カフェイン設定を開く", settings=sidebar_settings)
    ab.sidebar_page_link("pages/06_日記.py", label="日記・ライフログを開く", settings=sidebar_settings)

all_profiles = load_substance_profiles(include_inactive=True)
profile_lookup = {str(item["substance_id"]): item for item in all_profiles}
profile_ids = [str(item["substance_id"]) for item in all_profiles]
active_ids = [str(item["substance_id"]) for item in all_profiles if item.get("is_active", True)]
default_id = CAFFEINE_SUBSTANCE_ID if CAFFEINE_SUBSTANCE_ID in profile_ids else profile_ids[0]
selected_id = st.selectbox(
    "記録・編集する項目",
    options=profile_ids,
    index=profile_ids.index(default_id),
    format_func=lambda item_id: (
        str(profile_lookup[item_id]["name"])
        if profile_lookup[item_id].get("is_active", True)
        else f"{profile_lookup[item_id]['name']}（アーカイブ）"
    ),
    key="substance_record_selected_id",
)
profile = profile_lookup[selected_id]
# Only the full render uses this return for the read-only 14-day table.
# Partial input runs leave that table alone; saves use fresh service reads.
current_entry = render_substance_editor(
    target, selected_id, profile, initial_context={"profiles": all_profiles},
)

st.subheader(f"直近14日の{profile['name']}記録")
summary = recent_summary(target, selected_id, profile_lookup, current_entry=current_entry)
if summary.empty:
    st.info(f"表示できる{profile['name']}記録はまだありません。")
else:
    st.dataframe(summary, width="stretch", hide_index=True)

st.caption("残存量の計算には、各摂取記録に保存された単純半減期モデルの条件を使用します。")
with st.expander("保存先", expanded=False):
    st.caption(str(get_diary_entry_path(target)))
