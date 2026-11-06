from __future__ import annotations
from app_core.json_store import record_json_files

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.app_clock import app_today
from app_core.daily_save_service import save_daily_records
from app_core.form_snapshot import discard_form_snapshot, form_snapshot
from app_core.diary_service import get_diary_entry_path, latest_caffeine_record, load_diary_entry
from app_core.exercise_service import get_exercise_log_path, load_exercise_log
from app_core.health_service import get_health_log_path, load_health_log
from app_core.settings_service import load_user_settings
from app_core.substance_service import format_mg, substance_profile_map


ROOT = Path(__file__).resolve().parents[1]
DIARY_DIR = ROOT / "diary_entries"


def diary_path(target: date) -> Path:
    return get_diary_entry_path(target)


def clean_costs(costs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = []
    for item in costs:
        name = str(item.get("name", "")).strip()
        amount = float(item.get("amount", 0) or 0)
        if name or amount:
            record = {**item, "name": name, "amount": amount}
            if item.get("source"):
                record["source"] = str(item["source"])
            cleaned.append(record)
    return cleaned


def yen(value: int | float) -> str:
    return f"{int(value or 0):,}円"


def minutes_label(minutes: int | float) -> str:
    total = max(int(minutes or 0), 0)
    hours, mins = divmod(total, 60)
    if hours:
        return f"{hours}時間{mins:02d}分"
    return f"{mins}分"


def caffeine_mg(existing: dict[str, Any], default_mg: int) -> float:
    if existing.get("caffeine_mg") not in (None, ""):
        return round(max(float(existing.get("caffeine_mg") or 0), 0.0), 6)
    if existing.get("caffeine_cups") not in (None, ""):
        return round(max(float(existing.get("caffeine_cups") or 0) * 100, 0.0), 6)
    return float(default_mg)


def load_diary_record(target: date, *, profiles: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    return load_diary_entry(target, profiles=profiles)


def load_all_diaries(
    *,
    profiles: dict[str, dict[str, Any]] | None = None,
    current_day: date | None = None,
    current_entry: dict[str, Any] | None = None,
) -> pd.DataFrame:
    profile_lookup = substance_profile_map(include_inactive=True) if profiles is None else profiles
    rows: list[dict[str, Any]] = []
    for path in sorted(record_json_files(DIARY_DIR), reverse=True):
        try:
            target = datetime.strptime(path.stem, "%Y%m%d").date()
        except ValueError:
            continue
        item = current_entry if target == current_day and current_entry is not None else load_diary_entry(target, profiles=profile_lookup)
        costs = item.get("cost_records", [])
        caffeine_records = [record for record in item.get("caffeine_records", []) if isinstance(record, dict)]
        latest = latest_caffeine_record(item)
        investment = sum(float(cost.get("amount", 0) or 0) for cost in costs if isinstance(cost, dict)) if isinstance(costs, list) else 0
        rows.append(
            {
                "日付": item.get("date", target.isoformat()),
                "タイトル": item.get("title", ""),
                "気分": item.get("mood", ""),
                "天気": item.get("weather", ""),
                "カフェインmg": caffeine_mg(item, 0),
                "時刻付き摂取": len(caffeine_records),
                "最終摂取": str(latest.get("taken_at", ""))[11:16] if latest else "",
                "自己投資額": int(investment),
                "ランニング距離km": item.get("running_distance_km", 0),
                "ランニング時間分": item.get("running_time_min", 0),
            }
        )
    return pd.DataFrame(rows)


def health_summary(target: date, *, log: dict[str, Any] | None = None) -> dict[str, Any]:
    if log is None:
        if not get_health_log_path(target).exists():
            return {}
        log = load_health_log(target)
    sleep = log.get("sleep", {})
    condition = log.get("condition", {})
    return {
        "睡眠時間": minutes_label(sleep.get("sleep_duration_min", 0)),
        "睡眠の質": f"{sleep.get('sleep_quality', 0)} / 5",
        "疲労感": f"{condition.get('fatigue', 0)} / 5",
        "集中しやすさ": f"{condition.get('focus_score', 0)} / 5",
    }


def exercise_summary(target: date, *, log: dict[str, Any] | None = None) -> dict[str, Any]:
    if log is None:
        if not get_exercise_log_path(target).exists():
            return {}
        log = load_exercise_log(target)
    records = [item for item in log.get("records", []) if isinstance(item, dict)]
    duration = sum(float(item.get("duration_min", 0) or 0) for item in records)
    distance = sum(float(item.get("distance_km", 0) or 0) for item in records)
    return {
        "記録数": len(records),
        "運動時間": minutes_label(duration),
        "距離": f"{distance:.2f} km",
    }


@st.fragment
def render_diary_editor(
    target: date, *, initial_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Use the full render's small-file inputs once; every partial run reloads them.
    # This display context is separate from the form's fixed CAS baseline.
    shared = initial_context.pop("context", None) if initial_context is not None else None
    settings = load_user_settings() if shared is None else shared["settings"]
    diary_settings = settings.get("diary", {})
    profile_lookup = substance_profile_map(include_inactive=True) if shared is None else shared["profiles"]
    path = diary_path(target)
    live_entry = load_diary_record(target, profiles=profile_lookup)
    existing = live_entry
    exercise_log = load_exercise_log(target)
    health = health_summary(target)
    exercise = exercise_summary(target, log=exercise_log) if get_exercise_log_path(target).exists() else {}
    edit_key = f"diary:{target.isoformat()}"
    edit = form_snapshot(st.session_state, edit_key, [get_diary_entry_path(target), get_exercise_log_path(target)], existing)
    if st.button("入力を破棄して最新の日記を再読込", key=f"reload_{edit_key}"):
        discard_form_snapshot(st.session_state, edit_key)
        st.rerun()
    existing = edit["data"]
    widget_prefix = f"diary_{target.isoformat()}_{edit['version']}"
    old_costs = existing.get("cost_records", []) if isinstance(existing.get("cost_records", []), list) else []
    old_caffeine_records = existing.get("caffeine_records", []) if isinstance(existing.get("caffeine_records", []), list) else []
    default_cost_rows = max(1, int(diary_settings.get("investment_row_count", 3) or 3), len(old_costs))
    cost_count = st.number_input("自己投資費用の入力行数", min_value=max(1, len(old_costs)), max_value=max(20, default_cost_rows + 20), value=default_cost_rows, step=1, key=f"{widget_prefix}_cost_count")
    st.caption("既存の費用はすべて表示します。削除する明細は「削除」を選んで保存してください。")

    st.subheader("選択日のサマリー")
    investment_total = sum(float(item.get("amount", 0) or 0) for item in old_costs if isinstance(item, dict))
    summary_cols = st.columns(4)
    with summary_cols[0]:
        ab.metric_block("自己投資額", yen(investment_total), "日記に保存された費用合計")
    with summary_cols[1]:
        timed_count = len(old_caffeine_records)
        untimed_mg = max(float(existing.get("caffeine_untimed_mg", 0) or 0), 0.0)
        ab.metric_block(
            "カフェイン",
            f"{format_mg(caffeine_mg(existing, int(diary_settings.get('caffeine_mg', 0) or 0)))} mg",
            f"時刻付き {timed_count}件 / 時刻不明 {format_mg(untimed_mg)}mg",
        )
    with summary_cols[2]:
        ab.metric_block("睡眠・体調", health.get("睡眠時間", "未記録"), health.get("集中しやすさ", "専用ページで詳細記録"))
    with summary_cols[3]:
        ab.metric_block("運動", exercise.get("運動時間", "未記録"), exercise.get("距離", "専用ページで詳細記録"))

    st.page_link("pages/18_カフェイン記録.py", label="服薬ログを開く")

    with st.form(f"diary_form_{widget_prefix}"):
        title = st.text_input("タイトル", value=existing.get("title", ""), key=f"{widget_prefix}_title")
        body = st.text_area("本文", value=existing.get("body", ""), height=180, key=f"{widget_prefix}_body")
        col1, col2 = st.columns(2)
        mood_options = ["未記録", "良好", "普通", "低調", "集中", "安定"]
        weather_options = ["未記録", "晴れ", "曇り", "雨", "雪"]
        mood_default = existing.get("mood", "未記録") if existing.get("mood", "未記録") in mood_options else "未記録"
        weather_default = existing.get("weather", "未記録") if existing.get("weather", "未記録") in weather_options else "未記録"
        mood = col1.selectbox("気分", mood_options, index=mood_options.index(mood_default), key=f"{widget_prefix}_mood")
        weather = col2.selectbox("天気", weather_options, index=weather_options.index(weather_default), key=f"{widget_prefix}_weather")

        st.subheader("運動の簡易記録")
        col4, col5 = st.columns(2)
        running_distance = col4.number_input("ランニング距離（km）", min_value=0.0, value=float(existing.get("running_distance_km", 0) or 0), step=0.1, key=f"{widget_prefix}_distance")
        running_time = col5.number_input("ランニング時間（分）", min_value=0.0, value=float(existing.get("running_time_min", 0) or 0), step=1.0, key=f"{widget_prefix}_duration")
        sync_exercise = st.checkbox("ランニングの簡易記録を運動ログへ同期する", value=bool(running_distance or running_time), key=f"{widget_prefix}_sync")

        st.subheader("自己投資費用")
        cost_records = []
        for i in range(int(cost_count)):
            c1, c2, c3 = st.columns([2, 1, 0.6])
            old = old_costs[i] if i < len(old_costs) and isinstance(old_costs[i], dict) else {}
            record = {
                **old,
                "name": c1.text_input(f"項目 {i + 1}", value=old.get("name", ""), key=f"{widget_prefix}_cost_name_{i}"),
                "amount": c2.number_input(
                    f"金額 {i + 1}",
                    min_value=0.0,
                    value=float(old.get("amount", 0) or 0),
                    step=100.0,
                    key=f"{widget_prefix}_cost_amount_{i}",
                ),
            }
            if old.get("source"):
                record["source"] = old["source"]
            remove_cost = c3.checkbox(f"削除 {i + 1}", value=False, disabled=not bool(old), key=f"{widget_prefix}_cost_remove_{i}")
            if not remove_cost:
                cost_records.append(record)
        submitted = st.form_submit_button("保存する", type="primary")

    if submitted:
        payload = {
            "date": target.isoformat(),
            "title": title,
            "body": body,
            "mood": mood,
            "weather": weather,
            "running_distance_km": running_distance,
            "running_time_min": running_time,
            "cost_records": clean_costs(cost_records),
        }
        try:
            paths = edit["revisions"] if sync_exercise else {get_diary_entry_path(target): edit["revisions"][get_diary_entry_path(target)]}
            save_daily_records(
                target, payload, root=ROOT, operation_id=edit["operation_id"], expected_revisions=paths,
                exercise_mode="diary_running" if sync_exercise else "unchanged",
                exercise_data={"distance_km": running_distance, "duration_min": running_time, "memo": "日記・ライフログの簡易入力から同期"},
            )
        except (ValueError, OSError) as exc:
            ab.show_action_error(f"保存できませんでした。入力は保持しています。{exc}")
        else:
            discard_form_snapshot(st.session_state, edit_key)
            ab.queue_success("日記を保存しました。")
            st.rerun()

    st.subheader("当日の詳細記録")
    detail_cols = st.columns(2)
    with detail_cols[0]:
        ab.card(
            "睡眠・体調ログ",
            "\n".join([f"{key}: {value}" for key, value in health.items()]) or "当日の睡眠・体調ログはまだありません。",
        )
    with detail_cols[1]:
        ab.card(
            "運動ログ",
            "\n".join([f"{key}: {value}" for key, value in exercise.items()]) or "当日の運動ログはまだありません。",
        )

    with st.expander("保存先", expanded=False):
        st.caption(str(path))
    return live_entry


st.set_page_config(page_title="日記・ライフログ", layout="wide")
DIARY_DIR.mkdir(parents=True, exist_ok=True)
settings = load_user_settings()

ab.inject_css(settings=settings)
sidebar_settings = ab.sidemenu("日記、簡易ライフログ、当日の関連記録を確認します。", settings=settings)
ab.topbar("日記・ライフログ", "日々の出来事、気分、自己投資を簡易に記録します。")

with st.sidebar:
    target = st.date_input("記録日", value=app_today())
    st.caption("詳細な睡眠・運動記録は専用ページで管理できます。このページでは当日の簡易入力とサマリー確認を行います。")
    ab.sidebar_page_link("pages/18_カフェイン記録.py", label="服薬ログを開く", settings=sidebar_settings)
    ab.sidebar_page_link("pages/12_睡眠・体調ログ.py", label="睡眠・体調ログを開く", settings=sidebar_settings)
    ab.sidebar_page_link("pages/13_運動ログ.py", label="運動ログを開く", settings=sidebar_settings)

# The fragment return is used only during a full render for this read-only list.
# On partial runs the list stays in place; saving never uses this return value.
history_profiles = substance_profile_map(include_inactive=True)
current_entry = render_diary_editor(
    target, initial_context={"context": {"settings": settings, "profiles": history_profiles}},
)

st.subheader("過去の日記一覧")
diary_df = load_all_diaries(profiles=history_profiles, current_day=target, current_entry=current_entry)
if diary_df.empty:
    st.info("保存済みの日記はまだありません。")
else:
    st.dataframe(diary_df, width="stretch", hide_index=True)

