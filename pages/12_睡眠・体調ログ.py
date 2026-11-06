from __future__ import annotations

from datetime import date, datetime, time, timedelta
from app_core.app_clock import app_today
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.plot_style import (
    chart_series_style,
    configure_japanese_font,
    configure_plot_theme,
    plot_rounded_category_bars,
    plot_styled_line,
    style_chart_axis,
    style_date_axis,
    style_legend,
)
from app_core.settings_service import load_user_settings, should_show_task_metrics
from app_core.health_service import (
    build_health_commentary,
    build_health_summary,
    build_health_study_joined_summary,
    build_study_summary_by_date,
    calculate_health_study_indicators,
    calculate_sleep_duration_minutes,
    get_health_log_path,
    load_health_log,
    save_health_log,
)


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

ROOT = Path(__file__).resolve().parents[1]
THEME = ab.theme_palette(settings=PAGE_SETTINGS)

configure_japanese_font()
configure_plot_theme(THEME.get("mode") == "dark")


def parse_time_value(value: Any, fallback: time) -> time:
    if isinstance(value, time):
        return value
    try:
        return datetime.strptime(str(value or "")[:5], "%H:%M").time()
    except ValueError:
        return fallback


def duration_label(minutes: int | float) -> str:
    total = max(int(minutes or 0), 0)
    hours, mins = divmod(total, 60)
    return f"{hours}時間{mins:02d}分"


def seconds_label(seconds: int | float) -> str:
    total = max(int(seconds or 0), 0)
    hours, rem = divmod(total, 3600)
    mins = rem // 60
    return f"{hours}時間{mins:02d}分"


def period_range(label: str, today: date) -> tuple[date, date]:
    if label == "直近7日":
        return today - timedelta(days=6), today
    if label == "直近14日":
        return today - timedelta(days=13), today
    if label == "今月":
        return date(today.year, today.month, 1), today
    return today - timedelta(days=29), today


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def render_trend_chart(df: pd.DataFrame) -> None:
    if df.empty or not df["has_health_log"].any():
        st.info("表示期間内の睡眠・体調ログはまだありません。記録を保存すると推移が表示されます。")
        return

    chart = df.copy()
    chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
    for column in ("sleep_duration_min", "sleep_quality", "fatigue", "focus_score"):
        chart.loc[~chart["has_health_log"], column] = None
    chart["sleep_hours"] = chart["sleep_duration_min"] / 60

    fig, axes = plt.subplots(2, 2, figsize=(10, 6.4), sharex=True)
    plots = [
        ("sleep_hours", "睡眠時間", "時間", 0, False),
        ("sleep_quality", "睡眠の質", "1-5", 1, True),
        ("fatigue", "疲労感", "1-5", 4, True),
        ("focus_score", "集中しやすさ", "1-5", 2, True),
    ]
    for ax, (column, title, ylabel, style_index, is_score) in zip(axes.flat, plots):
        style = chart_series_style(THEME, style_index)
        plot_styled_line(
            ax,
            chart["date"],
            chart[column],
            palette=THEME,
            style_index=style_index,
            max_markers=14,
        )
        style_chart_axis(
            ax,
            THEME,
            title=title,
            title_color=style["color"],
            ylabel=ylabel,
            score_range=is_score,
            reference_value=3 if is_score else None,
        )
    for ax in axes[-1, :]:
        style_date_axis(ax)
    fig.tight_layout()
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)


def render_relationship_charts(df: pd.DataFrame) -> None:
    if df.empty or not df["has_health_log"].any():
        st.info("学習パフォーマンスとの関係を見るための記録はまだ十分ではありません。")
        return

    chart = df[df["has_health_log"]].copy()
    if chart.empty:
        st.info("学習パフォーマンスとの関係を見るための記録はまだ十分ではありません。")
        return
    chart["date"] = pd.to_datetime(chart["date"], errors="coerce")
    chart["sleep_hours"] = chart["sleep_duration_min"] / 60
    chart["study_hours"] = chart["study_duration_sec"] / 3600

    fig, axes = plt.subplots(2, 2, figsize=(10, 7.2))
    sleep_style = chart_series_style(THEME, 0)
    focus_style = chart_series_style(THEME, 2)
    fatigue_style = chart_series_style(THEME, 4)

    axes[0, 0].scatter(
        chart["sleep_hours"],
        chart["study_hours"],
        color=sleep_style["color"],
        edgecolor=THEME.get("panel", "#FFFFFF"),
        linewidth=0.8,
        alpha=0.82,
    )
    axes[0, 0].set_xlabel("睡眠時間")
    style_chart_axis(axes[0, 0], THEME, title="睡眠時間と勉強時間", ylabel="勉強時間", grid_axis="both")

    fatigue_summary = (
        chart.dropna(subset=["fatigue"])
        .groupby("fatigue", dropna=True)["study_hours"]
        .agg(["mean", "count"])
        .sort_index()
    )
    fatigue_labels = [
        f"{float(score):g}\n(n={int(count)})"
        for score, count in zip(fatigue_summary.index, fatigue_summary["count"])
    ]
    plot_rounded_category_bars(
        axes[0, 1],
        fatigue_labels,
        fatigue_summary["mean"].tolist(),
        palette=THEME,
        colors=fatigue_style["color"],
        orientation="vertical",
        linewidth=18,
        formatter=lambda value: f"{value:.1f}h",
    )
    axes[0, 1].set_xlabel("疲労感")
    axes[0, 1].set_ylim(bottom=0)
    style_chart_axis(axes[0, 1], THEME, title="疲労感別の平均勉強時間", ylabel="平均勉強時間")

    axes[1, 0].scatter(
        chart["focus_score"],
        chart["study_hours"],
        color=focus_style["color"],
        edgecolor=THEME.get("panel", "#FFFFFF"),
        linewidth=0.8,
        alpha=0.82,
    )
    axes[1, 0].set_xlabel("集中しやすさ")
    style_chart_axis(axes[1, 0], THEME, title="集中しやすさと勉強時間", ylabel="勉強時間", grid_axis="both")

    plot_styled_line(
        axes[1, 1],
        chart["date"],
        chart["sleep_hours"],
        palette=THEME,
        label="睡眠時間",
        style_index=0,
        max_markers=12,
    )
    plot_styled_line(
        axes[1, 1],
        chart["date"],
        chart["study_hours"],
        palette=THEME,
        label="勉強時間",
        style_index=3,
        max_markers=12,
    )
    axes[1, 1].set_ylim(bottom=0)
    style_chart_axis(axes[1, 1], THEME, title="日別の睡眠時間・勉強時間", ylabel="時間")
    style_date_axis(axes[1, 1])
    style_legend(axes[1, 1], columns=2)
    fig.tight_layout()
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)


st.set_page_config(page_title="睡眠・体調ログ", layout="wide")
USER_SETTINGS = PAGE_SETTINGS
HEALTH_DEFAULTS = USER_SETTINGS.get("health", {})
DISPLAY_DEFAULTS = USER_SETTINGS.get("display", {})
SHOW_TASK_METRICS = should_show_task_metrics(PAGE_SETTINGS)
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("睡眠、疲労、ストレス、集中しやすさを日付ごとに記録します。", settings=PAGE_SETTINGS)

today = app_today()
with st.sidebar:
    st.subheader("記録条件")
    target_date = st.date_input("記録日", value=today)
    period_options = ["直近7日", "直近14日", "直近30日", "今月"]
    default_period = DISPLAY_DEFAULTS.get("period_label", "直近30日")
    period_label = st.selectbox("過去データ表示期間", period_options, index=period_options.index(default_period) if default_period in period_options else 2)
    start_date, end_date = period_range(period_label, today)

study_summaries = build_study_summary_by_date(start_date, end_date)
existing = load_health_log(target_date)
summary_rows = build_health_summary(
    start_date, end_date, study_summaries=study_summaries,
    health_logs_by_date={target_date.isoformat(): existing},
)
summary_df = pd.DataFrame(summary_rows)
joined_rows = build_health_study_joined_summary(
    start_date, end_date, health_summary=summary_rows, study_summaries=study_summaries,
)
joined_df = pd.DataFrame(joined_rows)
health_export_df = joined_df.drop(columns=["has_health_log"], errors="ignore")
if not SHOW_TASK_METRICS:
    health_export_df = health_export_df.drop(
        columns=["completed_task_count", "task_count", "task_completion_rate"],
        errors="ignore",
    )

with st.sidebar:
    st.subheader("データ出力")
    if summary_df.empty or not summary_df["has_health_log"].any():
        st.caption("出力対象の睡眠・体調ログはまだありません。")
    st.download_button(
        "CSVをダウンロード",
        data=to_csv_bytes(health_export_df),
        file_name="health_log_summary.csv",
        mime="text/csv",
        width="stretch", on_click="ignore")

ab.topbar(
    "睡眠・体調ログ",
    "睡眠、疲労、ストレス、集中しやすさを記録し、学習パフォーマンスとの関係を振り返るためのログです。",
)
st.caption("このページは生活記録と傾向把握のためのセルフモニタリングです。")

has_existing = get_health_log_path(target_date).exists()
sleep = existing.get("sleep", {})
condition = existing.get("condition", {})


def health_value(section: dict[str, Any], key: str, default: Any) -> Any:
    return section.get(key, default) if has_existing else HEALTH_DEFAULTS.get(key, default)

st.subheader("選択日の記録")
with st.form("health_log_form"):
    left, right = st.columns(2)
    with left:
        st.markdown("#### 睡眠")
        bed_time = st.time_input("就寝時刻", value=parse_time_value(health_value(sleep, "bed_time", "23:30"), time(23, 30)))
        wake_time = st.time_input("起床時刻", value=parse_time_value(health_value(sleep, "wake_time", "06:30"), time(6, 30)))
        sleep_duration_min = calculate_sleep_duration_minutes(bed_time, wake_time)
        st.info(f"自動計算された睡眠時間: {duration_label(sleep_duration_min)}")
        sleep_quality = st.slider("睡眠の質", min_value=1, max_value=5, value=int(health_value(sleep, "sleep_quality", 3)), help="1: 低い / 5: 高い")
        morning_sleepiness = st.slider("起床時の眠気", min_value=1, max_value=5, value=int(health_value(sleep, "morning_sleepiness", 3)), help="1: 低い / 5: 高い")
        midnight_awakenings = st.number_input("中途覚醒回数", min_value=0, step=1, value=int(health_value(sleep, "midnight_awakenings", 0)))
        nap_minutes = st.number_input("昼寝時間（分）", min_value=0, step=5, value=int(health_value(sleep, "nap_minutes", 0)))

    with right:
        st.markdown("#### 体調・コンディション")
        st.caption("各スコアは 1: 低い / 5: 高い として記録します。")
        physical_score = st.slider("体調スコア", min_value=1, max_value=5, value=int(health_value(condition, "physical_score", 3)))
        fatigue = st.slider("疲労感", min_value=1, max_value=5, value=int(health_value(condition, "fatigue", 3)))
        stress = st.slider("ストレス", min_value=1, max_value=5, value=int(health_value(condition, "stress", 3)))
        focus_score = st.slider("集中しやすさ", min_value=1, max_value=5, value=int(health_value(condition, "focus_score", 3)))
        mood_score = st.slider("気分", min_value=1, max_value=5, value=int(health_value(condition, "mood_score", 3)))
        memo = st.text_area("メモ", value=str(condition.get("memo", "")), height=120)

    submitted = st.form_submit_button("保存する", type="primary", width="stretch")
    if submitted:
        payload = {
            "date": target_date.isoformat(),
            "sleep": {
                "bed_time": bed_time.strftime("%H:%M"),
                "wake_time": wake_time.strftime("%H:%M"),
                "sleep_duration_min": sleep_duration_min,
                "sleep_quality": sleep_quality,
                "morning_sleepiness": morning_sleepiness,
                "midnight_awakenings": int(midnight_awakenings),
                "nap_minutes": int(nap_minutes),
            },
            "condition": {
                "physical_score": physical_score,
                "fatigue": fatigue,
                "stress": stress,
                "focus_score": focus_score,
                "mood_score": mood_score,
                "memo": memo,
            },
        }
        try:
            save_health_log(target_date, payload)
        except (ValueError, OSError) as exc:
            ab.show_action_error(f"睡眠・体調ログを保存できませんでした。入力は保持しています。{exc}")
        else:
            ab.queue_success("睡眠・体調ログを保存しました。")
            st.rerun()

current = existing
current_sleep = current.get("sleep", {})
current_condition = current.get("condition", {})

st.subheader("選択日のサマリー")
cols = st.columns(3)
with cols[0]:
    ab.metric_block("睡眠時間", duration_label(current_sleep.get("sleep_duration_min", 0)), "就寝・起床時刻から自動計算")
with cols[1]:
    ab.metric_block("睡眠の質", f"{current_sleep.get('sleep_quality', 0)} / 5", "主観的な睡眠評価")
with cols[2]:
    ab.metric_block("疲労感", f"{current_condition.get('fatigue', 0)} / 5", "主観的な疲労の記録")
cols = st.columns(3)
with cols[0]:
    ab.metric_block("ストレス", f"{current_condition.get('stress', 0)} / 5", "主観的なストレスの記録")
with cols[1]:
    ab.metric_block("集中しやすさ", f"{current_condition.get('focus_score', 0)} / 5", "学習前後の振り返りに利用")
with cols[2]:
    ab.metric_block("体調スコア", f"{current_condition.get('physical_score', 0)} / 5", "生活記録としての体調評価")

st.subheader("過去データの推移")
render_trend_chart(summary_df)

st.subheader("睡眠・体調と勉強時間")
table_df = summary_df.sort_values(
    "date",
    ascending=False,
    kind="stable",
    na_position="last",
).copy()
if not table_df.empty:
    table_df["睡眠時間"] = table_df["sleep_duration_min"].apply(duration_label)
    table_df["その日の総勉強時間"] = table_df["study_duration_sec"].apply(seconds_label)
    display_df = table_df[
        [
            "date",
            "睡眠時間",
            "sleep_quality",
            "fatigue",
            "stress",
            "focus_score",
            "physical_score",
            "その日の総勉強時間",
            "memo",
        ]
    ].rename(
        columns={
            "date": "日付",
            "sleep_quality": "睡眠の質",
            "fatigue": "疲労感",
            "stress": "ストレス",
            "focus_score": "集中しやすさ",
            "physical_score": "体調スコア",
            "memo": "メモ",
        }
    )
    st.dataframe(display_df, width="stretch", hide_index=True)
else:
    st.info("表示できる過去データはまだありません。")

with st.expander("保存先", expanded=False):
    st.caption(str(get_health_log_path(target_date)))

st.subheader("学習パフォーマンスとの関係")
st.caption("睡眠・体調と学習量の関係を、記録上の傾向として確認します。因果や状態を断定するものではありません。")

indicators = calculate_health_study_indicators(joined_rows)
metric_cols = st.columns(4 if SHOW_TASK_METRICS else 3)
with metric_cols[0]:
    ab.metric_block("記録日数", f"{indicators['record_days']}日", "睡眠・体調ログがある日")
with metric_cols[1]:
    ab.metric_block("平均睡眠時間", f"{indicators['average_sleep_hours']:.2f}時間", "記録日の平均")
with metric_cols[2]:
    ab.metric_block("平均勉強時間", f"{indicators['average_study_hours']:.2f}時間", "記録日の平均")
if SHOW_TASK_METRICS:
    average_task_completion_rate = indicators["average_task_completion_rate"]
    with metric_cols[3]:
        ab.metric_block(
            "平均タスク達成率",
            "—" if average_task_completion_rate is None else f"{average_task_completion_rate:.1f}%",
            "タスク記録日の平均",
        )

for comment in build_health_commentary(joined_rows, include_task_metrics=SHOW_TASK_METRICS):
    st.info(comment)

render_relationship_charts(joined_df)

if not joined_df.empty:
    relation_df = joined_df.sort_values(
        "date",
        ascending=False,
        kind="stable",
        na_position="last",
    ).copy()
    relation_df["睡眠時間"] = relation_df["sleep_duration_min"].apply(duration_label)
    relation_df["総勉強時間"] = relation_df["study_duration_sec"].apply(seconds_label)
    relation_columns = [
        "date",
        "睡眠時間",
        "sleep_quality",
        "fatigue",
        "stress",
        "focus_score",
        "総勉強時間",
        "study_session_count",
    ]
    if SHOW_TASK_METRICS:
        relation_df["タスク達成率"] = relation_df["task_completion_rate"].map(
            lambda value: "—" if pd.isna(value) else f"{float(value):.1f}%"
        )
        relation_columns.extend(["completed_task_count", "task_count", "タスク達成率"])
    relation_columns.extend(["note_count", "card_count"])
    relation_display = relation_df[relation_columns].rename(
        columns={
            "date": "日付",
            "sleep_quality": "睡眠の質",
            "fatigue": "疲労感",
            "stress": "ストレス",
            "focus_score": "集中しやすさ",
            "study_session_count": "学習セッション数",
            "completed_task_count": "完了タスク数",
            "task_count": "全タスク数",
            "note_count": "作成ノート数",
            "card_count": "作成カード数",
        }
    )
    st.dataframe(relation_display, width="stretch", hide_index=True)
else:
    st.info("学習パフォーマンスと結合できるデータはまだありません。")
