from __future__ import annotations

from datetime import date, timedelta
from app_core.app_clock import app_today
from pathlib import Path
from typing import Any

import matplotlib.dates as mdates
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.life_health_service import (
    build_life_health_comments,
    build_life_health_dashboard_summary,
    calculate_life_health_kpis,
)
from app_core.plot_style import (
    add_bar_value_labels,
    chart_series_style,
    configure_japanese_font,
    plot_styled_line,
    plot_time_bars,
    style_chart_axis,
    style_date_axis,
    style_legend,
)
from app_core.settings_service import should_show_task_metrics


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

ROOT = Path(__file__).resolve().parents[1]
THEME = ab.theme_palette(settings=PAGE_SETTINGS)
SHOW_TASK_METRICS = should_show_task_metrics(PAGE_SETTINGS)
PRIMARY = THEME["primary"]
MID = THEME["primary_2"]
GREEN = THEME["green"]
MUTED = THEME["muted"]
AMBER = THEME["amber"]
PURPLE = THEME["purple"]
GRID = THEME["grid"]
SLEEP_TREND_COLOR = "#CBD5E1" if THEME["mode"] == "dark" else "#44546A"

CONDITION_LABELS = (
    ("physical_score", "体調スコア"),
    ("sleep_quality", "睡眠の質"),
    ("focus_score", "集中しやすさ"),
    ("mood_score", "気分"),
    ("fatigue", "疲労感"),
    ("stress", "ストレス"),
)
CONDITION_STYLES = {
    column: {"label": label, **chart_series_style(THEME, index)}
    for index, (column, label) in enumerate(CONDITION_LABELS)
}

configure_japanese_font()


def period_range(label: str, today: date) -> tuple[date, date]:
    if label == "直近7日":
        return today - timedelta(days=6), today
    if label == "直近14日":
        return today - timedelta(days=13), today
    if label == "今月":
        return date(today.year, today.month, 1), today
    return today - timedelta(days=29), today


def seconds_label(seconds: int | float) -> str:
    total = max(int(seconds or 0), 0)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{hours}時間{minutes:02d}分"


def minutes_label(minutes: int | float) -> str:
    total = max(int(minutes or 0), 0)
    hours, mins = divmod(total, 60)
    return f"{hours}時間{mins:02d}分" if hours else f"{mins}分"


def sleep_label(minutes: int | float) -> str:
    return minutes_label(minutes)


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


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def safe_float_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series([0.0] * len(df), index=df.index)
    return pd.to_numeric(df[column], errors="coerce").fillna(0)


def rows_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(
            columns=[
                "date",
                "has_health_log",
                "sleep_duration_min",
                "sleep_quality",
                "physical_score",
                "fatigue",
                "stress",
                "focus_score",
                "mood_score",
                "study_duration_sec",
                "study_session_count",
                "completed_task_count",
                "task_count",
                "task_completion_rate",
                "has_exercise_log",
                "exercise_duration_min",
                "exercise_distance_km",
                "exercise_types",
                "exercise_pace_min_per_km",
                "exercise_avg_intensity",
                "exercise_avg_fatigue_after",
            ]
        )
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


def render_condition_chart(df: pd.DataFrame) -> None:
    if df.empty or not bool(df.get("has_health_log", pd.Series(dtype=bool)).any()):
        st.info("表示期間内の睡眠・体調ログはまだありません。記録が増えると推移を確認できます。")
        return

    chart = df.copy()
    for column in (
        "sleep_duration_min",
        "physical_score",
        "sleep_quality",
        "focus_score",
        "mood_score",
        "fatigue",
        "stress",
    ):
        chart[column] = (
            pd.to_numeric(chart[column], errors="coerce")
            if column in chart.columns
            else pd.Series(float("nan"), index=chart.index, dtype=float)
        )
        chart.loc[~chart["has_health_log"], column] = None
    chart["sleep_hours"] = chart["sleep_duration_min"] / 60

    fig = plt.figure(figsize=(12, 10.5), constrained_layout=True)
    grid = fig.add_gridspec(4, 6, height_ratios=[1.1, 1.5, 1, 1])
    sleep_ax = fig.add_subplot(grid[0, :])
    overview_ax = fig.add_subplot(grid[1, :], sharex=sleep_ax)
    score_axes = {
        "sleep_quality": fig.add_subplot(grid[2, 0:2], sharex=sleep_ax),
        "focus_score": fig.add_subplot(grid[2, 2:4], sharex=sleep_ax),
        "mood_score": fig.add_subplot(grid[2, 4:6], sharex=sleep_ax),
        "fatigue": fig.add_subplot(grid[3, 0:3], sharex=sleep_ax),
        "stress": fig.add_subplot(grid[3, 3:6], sharex=sleep_ax),
    }

    dates = chart["date"].to_numpy()
    sleep_values = chart["sleep_hours"].to_numpy(dtype=float)
    sleep_ax.plot(dates, sleep_values, color=SLEEP_TREND_COLOR, linewidth=6, alpha=0.10, zorder=1)
    sleep_line = sleep_ax.plot(
        dates,
        sleep_values,
        marker="o",
        markersize=5.5,
        markeredgecolor="#FFFFFF",
        markeredgewidth=1.0,
        color=SLEEP_TREND_COLOR,
        linewidth=2.4,
        zorder=2,
    )[0]
    sleep_line.set_path_effects([path_effects.SimpleLineShadow(offset=(1.2, -1.2), alpha=0.22), path_effects.Normal()])
    sleep_ax.set_title("睡眠時間", loc="left", fontsize=11, fontweight="bold")
    sleep_ax.set_ylabel("時間")

    def style_score_axis(ax: Any, title: str, color: str) -> None:
        ax.set_title(title, loc="left", fontsize=10.5, fontweight="bold", color=color)
        ax.set_ylim(0.75, 5.25)
        ax.set_yticks([1, 2, 3, 4, 5])
        ax.axhline(3, color=GRID, linewidth=1, linestyle=(0, (3, 3)), zorder=0)
        ax.grid(axis="y", color=GRID, linewidth=0.8, alpha=0.85)
        for spine in ax.spines.values():
            spine.set_visible(False)

    def draw_score_line(
        ax: Any,
        column: str,
        *,
        show_label: bool = False,
        show_shadow: bool = True,
    ) -> Any:
        style = CONDITION_STYLES[column]
        values = chart[column].to_numpy(dtype=float)
        line = ax.plot(
            dates,
            values,
            color=style["color"],
            linestyle=style["linestyle"],
            linewidth=2.0,
            marker=style["marker"],
            markersize=5,
            markeredgecolor="#FFFFFF",
            markeredgewidth=1.1,
            label=style["label"] if show_label else None,
            zorder=3,
        )[0]
        if show_shadow:
            line.set_path_effects([path_effects.SimpleLineShadow(offset=(1.2, -1.2), alpha=0.24), path_effects.Normal()])
        return line

    def plot_score(ax: Any, column: str) -> None:
        style = CONDITION_STYLES[column]
        draw_score_line(ax, column)
        style_score_axis(ax, style["label"], style["color"])
        valid = chart.loc[chart[column].notna(), column]
        if not valid.empty:
            latest = float(valid.iloc[-1])
            ax.text(
                0.98,
                0.88,
                f"最新 {latest:g}",
                transform=ax.transAxes,
                ha="right",
                va="top",
                color=style["color"],
                fontsize=8.5,
                fontweight="bold",
                bbox={
                    "boxstyle": "round,pad=0.28",
                    "facecolor": THEME["panel"],
                    "edgecolor": style["color"],
                    "alpha": 0.92,
                },
                zorder=4,
            )

    draw_score_line(overview_ax, "physical_score", show_label=True, show_shadow=False)
    for column in CONDITION_STYLES:
        if column != "physical_score":
            draw_score_line(overview_ax, column, show_label=True, show_shadow=False)
    style_score_axis(overview_ax, "コンディションスコア比較（6指標）", THEME["ink"])
    overview_ax.set_ylabel("スコア")
    overview_ax.legend(
        loc="lower right",
        bbox_to_anchor=(1.0, 1.01),
        ncol=3,
        frameon=False,
        fontsize=8.5,
        handlelength=2.4,
        handletextpad=0.5,
        columnspacing=1.2,
        borderaxespad=0,
    )
    for column, ax in score_axes.items():
        plot_score(ax, column)

    for ax in [sleep_ax, overview_ax, *score_axes.values()]:
        ax.grid(color=GRID, linewidth=0.8, alpha=0.85)
        for spine in ax.spines.values():
            spine.set_visible(False)
    for ax in [sleep_ax, overview_ax, score_axes["sleep_quality"], score_axes["focus_score"], score_axes["mood_score"]]:
        ax.tick_params(axis="x", labelbottom=False)
    date_locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    sleep_ax.xaxis.set_major_locator(date_locator)
    sleep_ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(date_locator))
    sleep_ax.margins(x=0.02)
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)


def render_study_chart(df: pd.DataFrame, include_task_metrics: bool = True) -> None:
    if df.empty:
        st.info("表示できる学習データはまだありません。")
        return

    chart = df.copy()
    chart["study_hours"] = safe_float_series(chart, "study_duration_sec") / 3600
    chart["study_session_count"] = safe_float_series(chart, "study_session_count")
    if include_task_metrics:
        chart["completed_task_count"] = safe_float_series(chart, "completed_task_count")
        chart["task_count"] = safe_float_series(chart, "task_count")
        chart["task_completion_rate"] = pd.to_numeric(chart.get("task_completion_rate"), errors="coerce")

    panel_count = 3 if include_task_metrics else 1
    fig, plot_axes = plt.subplots(
        panel_count,
        1,
        figsize=(10, 8.5 if include_task_metrics else 4.2),
        sharex=True,
    )
    axes = list(plot_axes) if include_task_metrics else [plot_axes]
    dates = chart["date"].to_numpy()
    study_values = chart["study_hours"].to_numpy(dtype=float)
    highlight_indices = sorted({len(study_values) - 1, int(study_values.argmax())}) if len(study_values) else []
    bars = plot_time_bars(
        axes[0],
        dates,
        study_values,
        palette=THEME,
        color=PRIMARY,
        accent_color=THEME["accent"],
        highlight_indices=highlight_indices,
        label="勉強時間",
    )
    add_bar_value_labels(
        axes[0],
        bars,
        palette=THEME,
        formatter=lambda value: f"{value:.1f}h",
        indices=highlight_indices,
    )
    session_axis = axes[0].twinx()
    session_line = plot_styled_line(
        session_axis,
        dates,
        chart["study_session_count"].to_numpy(dtype=float),
        palette=THEME,
        label="学習セッション数",
        style_index=1,
    )
    style_chart_axis(axes[0], THEME, title="勉強時間・セッション数", ylabel="時間", grid_axis="y")
    axes[0].margins(y=0.14)
    style_chart_axis(session_axis, THEME, ylabel="回", grid_axis="x")
    session_axis.grid(False)
    session_axis.tick_params(axis="y", colors=chart_series_style(THEME, 1)["color"])
    axes[0].legend(
        [bars, session_line],
        ["勉強時間", "学習セッション数"],
        loc="upper left",
        frameon=False,
        ncol=2,
        fontsize=8.5,
    )

    if include_task_metrics:
        plot_styled_line(
            axes[1],
            dates,
            chart["completed_task_count"].to_numpy(dtype=float),
            palette=THEME,
            label="完了タスク数",
            style_index=2,
        )
        plot_styled_line(
            axes[1],
            dates,
            chart["task_count"].to_numpy(dtype=float),
            palette=THEME,
            label="全タスク数",
            style_index=0,
        )
        style_chart_axis(axes[1], THEME, title="タスク件数", ylabel="件", grid_axis="y")
        task_max = max(
            float(chart["completed_task_count"].max()),
            float(chart["task_count"].max()),
        )
        axes[1].set_ylim(0, max(task_max * 1.12, 1.0))
        style_legend(axes[1], columns=2)

        plot_styled_line(
            axes[2],
            dates,
            chart["task_completion_rate"].to_numpy(dtype=float),
            palette=THEME,
            label="タスク達成率",
            style_index=3,
        )
        style_chart_axis(axes[2], THEME, title="タスク達成率", ylabel="%", grid_axis="y")
        axes[2].set_ylim(0, 105)
        axes[2].set_yticks([0, 25, 50, 75, 100])
        style_legend(axes[2])

    for ax in axes[:-1]:
        ax.tick_params(axis="x", labelbottom=False)
    style_date_axis(axes[-1])
    fig.tight_layout(h_pad=1.35 if include_task_metrics else 0.8)
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)


def render_exercise_chart(df: pd.DataFrame) -> None:
    if df.empty or not bool(df.get("has_exercise_log", pd.Series(dtype=bool)).any()):
        st.info("表示期間内の運動ログはまだありません。記録が増えると推移を確認できます。")
        return

    chart = df.copy()
    chart["exercise_duration_min"] = safe_float_series(chart, "exercise_duration_min")
    chart["exercise_distance_km"] = safe_float_series(chart, "exercise_distance_km")
    chart["exercise_avg_intensity"] = safe_float_series(chart, "exercise_avg_intensity")
    chart["exercise_avg_fatigue_after"] = safe_float_series(chart, "exercise_avg_fatigue_after")
    has_exercise = chart.get("has_exercise_log", pd.Series(False, index=chart.index)).fillna(False).astype(bool)
    # 走行距離と同じ日次推移として描画する。運動なしの日は0にそろえ、
    # NaNによって折れ線が点ごとに分断されないようにする。
    chart.loc[~has_exercise, ["exercise_avg_intensity", "exercise_avg_fatigue_after"]] = 0.0

    fig, axes = plt.subplots(4, 1, figsize=(10, 9.5), sharex=True)
    dates = chart["date"].to_numpy()
    duration_values = chart["exercise_duration_min"].to_numpy(dtype=float)
    highlight_indices = sorted({len(duration_values) - 1, int(duration_values.argmax())}) if len(duration_values) else []
    bars = plot_time_bars(
        axes[0],
        dates,
        duration_values,
        palette=THEME,
        color=PRIMARY,
        accent_color=THEME["accent"],
        highlight_indices=highlight_indices,
        label="運動時間",
    )
    add_bar_value_labels(
        axes[0],
        bars,
        palette=THEME,
        formatter=lambda value: f"{value:.0f}分",
        indices=highlight_indices,
    )
    style_chart_axis(axes[0], THEME, title="運動時間", ylabel="分", grid_axis="y")
    axes[0].margins(y=0.14)

    plot_styled_line(
        axes[1],
        dates,
        chart["exercise_distance_km"].to_numpy(dtype=float),
        palette=THEME,
        label="走行距離",
        style_index=2,
    )
    style_chart_axis(axes[1], THEME, title="走行距離", ylabel="km", grid_axis="y")
    axes[1].set_ylim(bottom=0)
    style_legend(axes[1])

    plot_styled_line(
        axes[2],
        dates,
        chart["exercise_avg_intensity"].to_numpy(dtype=float),
        palette=THEME,
        label="主観的きつさ",
        style_index=1,
    )
    style_chart_axis(axes[2], THEME, title="主観的きつさ", ylabel="0=運動なし / 1-10", grid_axis="y")
    axes[2].set_ylim(-0.25, 10.5)
    axes[2].set_yticks([0, 1, 3, 5, 7, 9])
    style_legend(axes[2])

    plot_styled_line(
        axes[3],
        dates,
        chart["exercise_avg_fatigue_after"].to_numpy(dtype=float),
        palette=THEME,
        label="運動後疲労感",
        style_index=4,
    )
    style_chart_axis(
        axes[3],
        THEME,
        title="運動後疲労感",
        ylabel="0=運動なし / 1-5",
        grid_axis="y",
        reference_value=3,
    )
    axes[3].set_ylim(-0.25, 5.25)
    axes[3].set_yticks([0, 1, 2, 3, 4, 5])
    style_legend(axes[3])

    for ax in axes[:-1]:
        ax.tick_params(axis="x", labelbottom=False)
    style_date_axis(axes[-1])
    fig.tight_layout(h_pad=1.2)
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)


def display_table(df: pd.DataFrame, include_task_metrics: bool = True) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    work = df.sort_values(
        "date",
        ascending=False,
        kind="stable",
        na_position="last",
    ).copy()
    work["日付"] = work["date"].dt.date.astype(str)
    work["睡眠時間"] = work["sleep_duration_min"].apply(sleep_label)
    work["総勉強時間"] = work["study_duration_sec"].apply(seconds_label)
    work["総運動時間"] = work["exercise_duration_min"].apply(minutes_label)
    work["走行距離"] = work["exercise_distance_km"].map(lambda value: f"{float(value or 0):.2f} km")
    work["ペース"] = work["exercise_pace_min_per_km"].apply(pace_label)
    columns = [
        "日付",
        "睡眠時間",
        "physical_score",
        "sleep_quality",
        "fatigue",
        "stress",
        "focus_score",
        "mood_score",
        "総勉強時間",
        "study_session_count",
    ]
    if include_task_metrics:
        work["タスク達成率"] = work["task_completion_rate"].map(
            lambda value: "—" if pd.isna(value) else f"{float(value):.1f}%"
        )
        columns.extend(["completed_task_count", "task_count", "タスク達成率"])
    columns.extend(
        [
            "総運動時間",
            "exercise_types",
            "走行距離",
            "ペース",
            "exercise_avg_intensity",
            "exercise_avg_fatigue_after",
        ]
    )
    return work[columns].rename(
        columns={
            "physical_score": "体調スコア",
            "sleep_quality": "睡眠の質",
            "fatigue": "疲労感",
            "stress": "ストレス",
            "focus_score": "集中しやすさ",
            "mood_score": "気分",
            "study_session_count": "学習セッション数",
            "completed_task_count": "完了タスク数",
            "task_count": "全タスク数",
            "exercise_types": "運動種目",
            "exercise_avg_intensity": "主観的きつさ",
            "exercise_avg_fatigue_after": "運動後疲労感",
        }
    )


st.set_page_config(page_title="生活・健康ダッシュボード", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("睡眠、体調、運動、学習状況を日付軸で確認します。", settings=PAGE_SETTINGS)

today = app_today()
with st.sidebar:
    st.subheader("表示条件")
    period_label = st.selectbox("期間", ["直近7日", "直近14日", "直近30日", "今月", "任意期間"], index=2)
    if period_label == "任意期間":
        start_date = st.date_input("開始日", value=today - timedelta(days=29))
        end_date = st.date_input("終了日", value=today)
    else:
        start_date, end_date = period_range(period_label, today)
        st.caption(f"{start_date.isoformat()} から {end_date.isoformat()} まで")

if end_date < start_date:
    start_date, end_date = end_date, start_date

ab.topbar("生活・健康ダッシュボード", "睡眠・体調、運動、学習状況を日付軸で統合し、直近のコンディションを俯瞰します。")
st.caption("この画面はセルフモニタリングと傾向把握のための生活記録です。")

rows = build_life_health_dashboard_summary(start_date, end_date)
df = rows_to_frame(rows)
kpis = calculate_life_health_kpis(rows)

st.subheader("KPIサマリ")
kpi_rows = [st.columns(4), st.columns(4), st.columns(5 if SHOW_TASK_METRICS else 4)]
with kpi_rows[0][0]:
    ab.metric_block("平均睡眠時間", f"{kpis['average_sleep_hours']:.2f}時間", f"記録日 {kpis['health_record_days']}日")
with kpi_rows[0][1]:
    ab.metric_block("平均睡眠の質", f"{kpis['average_sleep_quality']:.2f} / 5", "睡眠・体調ログより")
with kpi_rows[0][2]:
    ab.metric_block("平均体調スコア", f"{kpis['average_physical_score']:.2f} / 5", "睡眠・体調ログより")
with kpi_rows[0][3]:
    ab.metric_block("平均疲労感", f"{kpis['average_fatigue']:.2f} / 5", "睡眠・体調ログより")
with kpi_rows[1][0]:
    ab.metric_block("平均ストレス", f"{kpis['average_stress']:.2f} / 5", "睡眠・体調ログより")
with kpi_rows[1][1]:
    ab.metric_block("平均集中しやすさ", f"{kpis['average_focus_score']:.2f} / 5", "睡眠・体調ログより")
with kpi_rows[1][2]:
    ab.metric_block("総勉強時間", seconds_label(kpis["total_study_sec"]), "選択期間の合計")
with kpi_rows[1][3]:
    ab.metric_block("1日平均勉強時間", seconds_label(kpis["average_study_sec_per_day"]), "選択期間の日数ベース")
with kpi_rows[2][0]:
    ab.metric_block("学習セッション数", f"{kpis['study_session_count']}件", "勉強ログより")
with kpi_rows[2][1]:
    ab.metric_block("運動日数", f"{kpis['exercise_days']}日", "運動ログがある日")
with kpi_rows[2][2]:
    ab.metric_block("総運動時間", minutes_label(kpis["total_exercise_minutes"]), "選択期間の合計")
with kpi_rows[2][3]:
    ab.metric_block("総走行距離", f"{kpis['total_distance_km']:.2f} km", "ランニング・ウォーキング等")
if SHOW_TASK_METRICS:
    task_completion_rate = kpis["task_completion_rate"]
    with kpi_rows[2][4]:
        ab.metric_block(
            "タスク達成率",
            "—" if task_completion_rate is None else f"{task_completion_rate:.1f}%",
            f"{kpis['completed_task_count']}/{kpis['task_count']}件" if kpis["task_count"] else "タスク未記録",
        )

st.subheader("生活コンディション推移")
render_condition_chart(df)

st.subheader("学習パフォーマンス推移")
render_study_chart(df, include_task_metrics=SHOW_TASK_METRICS)

st.subheader("運動ログ推移")
render_exercise_chart(df)

st.subheader("統合テーブル")
table = display_table(df, include_task_metrics=SHOW_TASK_METRICS)
if table.empty:
    st.info("表示できる統合データはまだありません。")
else:
    st.dataframe(table, width="stretch", hide_index=True)

st.subheader("記録上の傾向コメント")
for comment in build_life_health_comments(rows, kpis, include_task_metrics=SHOW_TASK_METRICS):
    st.info(comment)

st.subheader("CSV出力")
download_df = table.copy()
st.download_button(
    "統合テーブルCSVをダウンロード",
    data=to_csv_bytes(download_df),
    file_name="life_health_dashboard.csv",
    mime="text/csv",
    width="stretch",
    disabled=download_df.empty, on_click="ignore")
