from __future__ import annotations

from datetime import date, time, timedelta
from app_core.app_clock import app_today
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.plot_style import (
    add_bar_value_labels,
    chart_series_style,
    configure_japanese_font,
    configure_plot_theme,
    plot_rounded_category_bars,
    plot_styled_line,
    plot_time_bars,
    style_chart_axis,
    style_date_axis,
)
from app_core.settings_service import load_user_settings
from app_core.exercise_service import (
    add_exercise_record,
    build_exercise_study_joined_summary,
    calculate_running_pace,
    delete_exercise_record,
    get_exercise_log_path,
    load_exercise_log,
    load_exercise_logs_range,
)


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

THEME = ab.theme_palette(settings=PAGE_SETTINGS)

configure_japanese_font()
configure_plot_theme(THEME.get("mode") == "dark")

EXERCISE_TYPES = {
    "running": "ランニング",
    "walking": "ウォーキング",
    "strength": "筋トレ",
    "stretch": "ストレッチ",
    "other": "その他",
}


def period_range(label: str, today: date) -> tuple[date, date]:
    if label == "直近7日":
        return today - timedelta(days=6), today
    if label == "直近14日":
        return today - timedelta(days=13), today
    if label == "今月":
        return date(today.year, today.month, 1), today
    return today - timedelta(days=29), today


def minutes_label(minutes: int | float) -> str:
    total = max(int(minutes or 0), 0)
    hours, mins = divmod(total, 60)
    if hours:
        return f"{hours}時間{mins:02d}分"
    return f"{mins}分"


def seconds_label(seconds: int | float) -> str:
    total = max(int(seconds or 0), 0)
    hours, rem = divmod(total, 3600)
    mins = rem // 60
    return f"{hours}時間{mins:02d}分"


def pace_label(pace: Any) -> str:
    value = float(pace or 0)
    if value <= 0:
        return "-"
    minutes = int(value)
    seconds = int(round((value - minutes) * 60))
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}/km"


def parse_time_value(value: Any, fallback: time) -> time:
    if isinstance(value, time):
        return value
    try:
        parsed = str(value or "")[:5]
        hour, minute = parsed.split(":")
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return fallback


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def render_exercise_charts(summary_df: pd.DataFrame, records_df: pd.DataFrame) -> None:
    if summary_df.empty or not summary_df["has_exercise_log"].any():
        st.info("表示期間内の運動ログはまだありません。記録を保存すると推移が表示されます。")
        return

    chart = summary_df.copy()
    chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
    chart["exercise_duration_min"] = pd.to_numeric(chart["exercise_duration_min"], errors="coerce").fillna(0)
    chart["distance_km"] = pd.to_numeric(chart["distance_km"], errors="coerce").fillna(0)
    chart["study_hours"] = pd.to_numeric(chart["study_duration_sec"], errors="coerce").fillna(0) / 3600

    duration_style = chart_series_style(THEME, 0)
    distance_style = chart_series_style(THEME, 2)
    study_style = chart_series_style(THEME, 3)

    fig, axes = plt.subplots(
        3,
        1,
        figsize=(10, 7.8),
        sharex=True,
        gridspec_kw={"height_ratios": [1.2, 1, 1]},
    )
    duration_values = chart["exercise_duration_min"].tolist()
    peak_index = int(chart["exercise_duration_min"].to_numpy().argmax()) if duration_values else 0
    recorded_indices = [
        index
        for index, recorded in enumerate(chart.get("has_exercise_log", pd.Series(False, index=chart.index)).tolist())
        if bool(recorded)
    ]
    latest_index = recorded_indices[-1] if recorded_indices else peak_index
    label_indices = sorted({peak_index, latest_index}) if duration_values else []
    duration_bars = plot_time_bars(
        axes[0],
        chart["date"],
        duration_values,
        palette=THEME,
        color=duration_style["color"],
        accent_color=distance_style["color"],
        highlight_indices=label_indices,
    )
    add_bar_value_labels(
        axes[0],
        duration_bars,
        palette=THEME,
        formatter=lambda value: f"{value:g}分" if value > 0 else "",
        indices=label_indices,
    )
    axes[0].set_ylim(bottom=0)
    style_chart_axis(axes[0], THEME, title="日別運動時間", ylabel="分")

    plot_styled_line(
        axes[1],
        chart["date"],
        chart["distance_km"],
        palette=THEME,
        style_index=2,
        max_markers=16,
    )
    axes[1].set_ylim(bottom=0)
    style_chart_axis(
        axes[1],
        THEME,
        title="走行距離",
        title_color=distance_style["color"],
        ylabel="km",
    )

    plot_styled_line(
        axes[2],
        chart["date"],
        chart["study_hours"],
        palette=THEME,
        style_index=3,
        max_markers=16,
    )
    axes[2].set_ylim(bottom=0)
    style_chart_axis(
        axes[2],
        THEME,
        title="勉強時間",
        title_color=study_style["color"],
        ylabel="時間",
    )
    style_date_axis(axes[2])
    fig.tight_layout()
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)

    if not records_df.empty:
        type_summary = records_df.groupby("type_label", dropna=False)["duration_min"].sum().sort_values(ascending=False)
        fig2, ax = plt.subplots(figsize=(9, max(3.5, len(type_summary) * 0.65 + 1.2)))
        values = type_summary.astype(float).tolist()
        total_minutes = sum(values)
        colors = [chart_series_style(THEME, index)["color"] for index in range(len(type_summary))]
        plot_rounded_category_bars(
            ax,
            type_summary.index.astype(str).tolist(),
            values,
            palette=THEME,
            colors=colors,
            linewidth=19,
            formatter=lambda value: (
                f"{value:g}分  {value / total_minutes * 100:.0f}%" if total_minutes > 0 else f"{value:g}分"
            ),
        )
        ax.set_xlabel("分")
        if values:
            ax.set_xlim(0, max(values) * 1.24 if max(values) > 0 else 1)
        style_chart_axis(ax, THEME, title="種目別運動時間", grid_axis="x")
        fig2.tight_layout()
        try:
            st.pyplot(fig2, clear_figure=True)
        finally:
            plt.close(fig2)


st.set_page_config(page_title="運動ログ", layout="wide")
USER_SETTINGS = PAGE_SETTINGS
EXERCISE_DEFAULTS = USER_SETTINGS.get("exercise", {})
DISPLAY_DEFAULTS = USER_SETTINGS.get("display", {})
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("ランニングや運動の記録を日付ごとに残します。", settings=PAGE_SETTINGS)

today = app_today()
with st.sidebar:
    st.subheader("記録条件")
    target_date = st.date_input("記録日", value=today)
    period_options = ["直近7日", "直近14日", "直近30日", "今月"]
    default_period = DISPLAY_DEFAULTS.get("period_label", "直近30日")
    period_label = st.selectbox("過去データ表示期間", period_options, index=period_options.index(default_period) if default_period in period_options else 2)
    start_date, end_date = period_range(period_label, today)

selected_log = load_exercise_log(target_date)
records = load_exercise_logs_range(
    start_date, end_date, logs_by_date={target_date.isoformat(): selected_log},
)
summary_rows = build_exercise_study_joined_summary(start_date, end_date, exercise_records=records)
summary_df = pd.DataFrame(summary_rows)
records_df = pd.DataFrame(records)
if not records_df.empty:
    records_df["type_label"] = records_df["type"].map(EXERCISE_TYPES).fillna("その他")
    study_by_day = summary_df.set_index("date")["study_duration_sec"].to_dict() if not summary_df.empty else {}
    records_df["study_duration_sec"] = records_df["date"].map(study_by_day).fillna(0).astype(int)
    records_df["study_hours"] = records_df["study_duration_sec"] / 3600

with st.sidebar:
    st.subheader("データ出力")
    if records_df.empty:
        st.caption("出力対象の運動ログはまだありません。")
    st.download_button(
        "CSVをダウンロード",
        data=to_csv_bytes(records_df),
        file_name="exercise_log_summary.csv",
        mime="text/csv",
        width="stretch",
        disabled=records_df.empty, on_click="ignore")

ab.topbar(
    "運動ログ",
    "ランニングや運動の記録を残し、体調や学習パフォーマンスとの関係を振り返ります。",
)
st.caption("このページは生活記録と傾向把握のためのセルフモニタリングです。")

selected_records = selected_log.get("records", [])

st.subheader("選択日の運動記録")
with st.form("exercise_log_form"):
    left, right = st.columns(2)
    with left:
        default_type = EXERCISE_DEFAULTS.get("type", "running")
        exercise_type = st.selectbox(
            "運動種目",
            list(EXERCISE_TYPES.keys()),
            index=list(EXERCISE_TYPES.keys()).index(default_type) if default_type in EXERCISE_TYPES else 0,
            format_func=lambda key: EXERCISE_TYPES.get(key, "その他"),
        )
        title = st.text_input("タイトル", placeholder="朝ラン、ジム、軽い散歩")
        start_time = st.time_input("開始時刻", value=parse_time_value(EXERCISE_DEFAULTS.get("start_time", "07:00"), time(7, 0)))
        duration_min = st.number_input("運動時間（分）", min_value=0, step=5, value=int(EXERCISE_DEFAULTS.get("duration_min", 30) or 30))
    with right:
        distance_km = st.number_input("距離（km）", min_value=0.0, step=0.1, value=float(EXERCISE_DEFAULTS.get("distance_km", 0.0) or 0.0), format="%.2f")
        calculated_pace = calculate_running_pace(distance_km, duration_min)
        st.info(f"自動計算ペース: {pace_label(calculated_pace)}")
        intensity = st.slider("主観的きつさ", min_value=1, max_value=10, value=int(EXERCISE_DEFAULTS.get("intensity", 5) or 5), help="1: 低い / 10: 高い")
        fatigue_after = st.slider("運動後の疲労感", min_value=1, max_value=5, value=int(EXERCISE_DEFAULTS.get("fatigue_after", 3) or 3), help="1: 低い / 5: 高い")
    memo = st.text_area("メモ", height=100)

    submitted = st.form_submit_button("運動記録を保存する", type="primary", width="stretch")
    if submitted:
        try:
            add_exercise_record(
                target_date,
                {
                    "type": exercise_type,
                    "title": title,
                    "start_time": start_time.strftime("%H:%M"),
                    "duration_min": int(duration_min),
                    "distance_km": float(distance_km),
                    "pace_min_per_km": calculated_pace,
                    "intensity": int(intensity),
                    "fatigue_after": int(fatigue_after),
                    "memo": memo,
                },
            )
        except (ValueError, OSError) as exc:
            ab.show_action_error(f"運動ログを保存できませんでした。入力は保持しています。{exc}")
        else:
            ab.queue_success("運動ログを保存しました。")
            st.rerun()

st.subheader("選択日の運動記録一覧")
if selected_records:
    display_rows = []
    for record in selected_records:
        display_rows.append(
            {
                "種目": EXERCISE_TYPES.get(record.get("type"), "その他"),
                "タイトル": record.get("title", ""),
                "開始時刻": record.get("start_time", ""),
                "時間": minutes_label(record.get("duration_min", 0)),
                "距離": f"{float(record.get('distance_km') or 0):.2f} km" if record.get("distance_km") else "-",
                "ペース": pace_label(record.get("pace_min_per_km")),
                "主観的きつさ": record.get("intensity", 0),
                "運動後疲労感": record.get("fatigue_after", 0),
                "メモ": record.get("memo", ""),
            }
        )
    st.dataframe(pd.DataFrame(display_rows), width="stretch", hide_index=True)
    with st.expander("削除", expanded=False):
        st.caption("削除しても他の学習ログや体調ログには影響しません。")
        for index, record in enumerate(selected_records):
            label = record.get("title") or EXERCISE_TYPES.get(record.get("type"), "運動記録")
            cols = st.columns([4, 1])
            with cols[0]:
                st.write(f"{record.get('start_time', '')}  {label}")
            with cols[1]:
                if st.button("削除", key=f"delete_exercise_{record.get('exercise_id', index)}", width="stretch"):
                    try:
                        deleted = delete_exercise_record(target_date, record.get("exercise_id", ""))
                    except (ValueError, OSError) as exc:
                        ab.show_action_error(f"運動ログを削除できませんでした。{exc}")
                    else:
                        if deleted:
                            ab.queue_success("運動ログを削除しました。")
                            st.rerun()
                        else:
                            ab.show_action_message("削除対象の運動ログが見つからなかったため、削除していません。", kind="warning")
else:
    st.info("選択日の運動記録はまだありません。")

st.subheader("過去データの推移")
render_exercise_charts(summary_df, records_df)

st.subheader("運動ログと勉強時間")
if not summary_df.empty:
    table_df = summary_df.copy()
    table_df["_sort_date"] = pd.to_datetime(table_df["date"], errors="coerce")
    table_df = table_df.sort_values("_sort_date", ascending=False, na_position="last", kind="stable")
    table_df["運動時間"] = table_df["exercise_duration_min"].apply(minutes_label)
    table_df["走行距離"] = table_df["distance_km"].map(lambda value: f"{float(value or 0):.2f} km")
    table_df["その日の総勉強時間"] = table_df["study_duration_sec"].apply(seconds_label)
    display_df = table_df[
        [
            "date",
            "record_count",
            "運動時間",
            "走行距離",
            "avg_intensity",
            "avg_fatigue_after",
            "その日の総勉強時間",
        ]
    ].rename(
        columns={
            "date": "日付",
            "record_count": "運動記録数",
            "avg_intensity": "平均主観的きつさ",
            "avg_fatigue_after": "平均運動後疲労感",
        }
    )
    st.dataframe(display_df, width="stretch", hide_index=True)
else:
    st.info("表示できる過去データはまだありません。")

with st.expander("保存先", expanded=False):
    st.caption(str(get_exercise_log_path(target_date)))
