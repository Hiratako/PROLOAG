from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from app_core.app_clock import app_today
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.analytics_service import (
    LearningDataSnapshot,
    build_daily_learning_summary,
    build_monthly_forest_data,
    build_subject_growth_summary,
    calculate_learning_streaks,
)
from app_core.master_snapshot import MasterSnapshot
from app_core.learning_visual_service import build_learning_visual_payload
from prolog_ui.learning_visuals import render_learning_visuals
from app_core.plot_style import (
    configure_japanese_font,
    plot_rounded_category_bars,
    style_chart_axis,
    style_legend,
)
from app_core.study_log_service import load_all_study_sessions


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

ROOT = Path(__file__).resolve().parents[1]
THEME = ab.theme_palette(settings=PAGE_SETTINGS)
PRIMARY = THEME["primary"]
MID = THEME["primary_2"]
LIGHT = THEME["panel_2"]
MUTED = THEME["muted"]
TEXT = THEME["ink"]
GRID = THEME["grid"]
WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]
IO_INPUT = "インプット"
IO_OUTPUT = "アウトプット"
IO_BOTH = "両方"
IO_UNSET = "未設定"

configure_japanese_font()


def parse_day(value: Any) -> date | None:
    from app_core.analysis_dates import parse_analysis_date

    return parse_analysis_date(value)


def format_duration(seconds: int | float) -> str:
    total = max(int(seconds or 0), 0)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{hours}時間{minutes:02d}分"


def format_yen(value: int | float) -> str:
    return f"{int(value or 0):,}円"


def option_map(items: list[dict[str, Any]], id_key: str, all_label: str = "すべて") -> dict[str, str]:
    options = {all_label: ""}
    for item in items:
        name = item.get("name") or "名称未設定"
        item_id = item.get(id_key) or ""
        options[f"{name} [{item_id}]"] = item_id
    return options


def selected_value(label: str, options: dict[str, str]) -> str:
    return options.get(label, "")


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def rows_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(
            columns=[
                "date",
                "total_duration_sec",
                "total_hours",
                "total_money",
                "session_count",
                "planned_minutes",
                "task_count",
                "main_project",
                "main_subject",
                "note_count",
                "card_count",
                "reminder_count",
            ]
        )
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


def aggregate_for_unit(df: pd.DataFrame, unit: str) -> pd.DataFrame:
    if df.empty:
        return df
    freq = {"日別": "D", "週別": "W-MON", "月別": "MS"}.get(unit, "D")
    work = df.copy()
    work = work.dropna(subset=["date"])
    if work.empty:
        return work
    numeric_cols = [
        "total_duration_sec",
        "total_money",
        "session_count",
        "planned_minutes",
        "task_count",
        "note_count",
        "card_count",
        "reminder_count",
    ]
    grouped = work.set_index("date").resample(freq)[numeric_cols].sum().reset_index()
    grouped["total_hours"] = grouped["total_duration_sec"] / 3600
    return grouped


def matches_master_filters(record: dict[str, Any], filters: dict[str, Any]) -> bool:
    return not any(filters.get(key) and record.get(key) != filters[key] for key in ("project_id", "subject_id", "material_id", "unit_id"))


def session_condition_value(session: dict[str, Any], key: str) -> str:
    condition = session.get("condition") if isinstance(session.get("condition"), dict) else {}
    return str(condition.get(key) or session.get(key) or "").strip()


def normalize_io_type(value: str) -> str:
    text = str(value or "").strip()
    if text in {IO_INPUT, "input", "Input", "INPUT"}:
        return IO_INPUT
    if text in {IO_OUTPUT, "output", "Output", "OUTPUT"}:
        return IO_OUTPUT
    if text in {IO_BOTH, "both", "Both", "BOTH"}:
        return IO_BOTH
    return IO_UNSET


def build_category_io_summary(
    start: date,
    end: date,
    filters: dict[str, Any],
    *,
    sessions: list[dict[str, Any]] | None = None,
    category_aliases: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "category": "",
            "input_sec": 0.0,
            "output_sec": 0.0,
            "unset_sec": 0.0,
            "session_count": 0,
        }
    )
    for session in sessions if sessions is not None else load_all_study_sessions(ROOT):
        day = parse_day(session.get("start_at") or session.get("end_at"))
        if not day or not (start <= day <= end) or not matches_master_filters(session, filters):
            continue
        duration_sec = float(session.get("duration_sec", 0) or 0)
        if duration_sec < 0:
            continue
        category = session_condition_value(session, "category") or IO_UNSET
        category = (category_aliases or {}).get(category, category)
        io_type = normalize_io_type(session_condition_value(session, "io_type"))
        row = rows[category]
        row["category"] = category
        row["session_count"] += 1
        if io_type == IO_INPUT:
            row["input_sec"] += duration_sec
        elif io_type == IO_OUTPUT:
            row["output_sec"] += duration_sec
        elif io_type == IO_BOTH:
            row["input_sec"] += duration_sec / 2
            row["output_sec"] += duration_sec / 2
        else:
            row["unset_sec"] += duration_sec

    summary = []
    for row in rows.values():
        total_sec = row["input_sec"] + row["output_sec"] + row["unset_sec"]
        summary.append(
            {
                "category": row["category"],
                "input_hours": round(row["input_sec"] / 3600, 3),
                "output_hours": round(row["output_sec"] / 3600, 3),
                "unset_hours": round(row["unset_sec"] / 3600, 3),
                "total_duration_sec": total_sec,
                "total_hours": round(total_sec / 3600, 3),
                "session_count": row["session_count"],
            }
        )
    return sorted(summary, key=lambda item: item["total_hours"], reverse=True)


def render_category_io_summary(rows: list[dict[str, Any]]) -> None:
    if not rows:
        st.info("対象期間にカテゴリとインプット/アウトプットを集計できる勉強ログはありません。")
        return

    df = pd.DataFrame(rows)
    denominator = df["total_hours"].where(df["total_hours"] > 0)
    df["input_ratio_pct"] = (df["input_hours"] / denominator * 100).fillna(0).round(1)
    df["output_ratio_pct"] = (df["output_hours"] / denominator * 100).fillna(0).round(1)
    df["unset_ratio_pct"] = (df["unset_hours"] / denominator * 100).fillna(0).round(1)
    plot_df = df.sort_values("total_hours").set_index("category")
    fig, ax = plt.subplots(figsize=(9, max(2.8, len(plot_df) * 0.45)))
    left = pd.Series(0.0, index=plot_df.index)
    max_total = max(float(plot_df["total_hours"].max()), 1.0)
    strong_segment_text = THEME["background"] if THEME.get("mode") == "dark" else THEME["card"]
    series = [
        ("input_hours", IO_INPUT, PRIMARY),
        ("output_hours", IO_OUTPUT, MID),
        ("unset_hours", IO_UNSET, LIGHT),
    ]
    for column, label, color in series:
        if column == "unset_hours" and plot_df[column].sum() <= 0:
            continue
        widths = plot_df[column]
        container = ax.barh(
            plot_df.index,
            widths,
            left=left,
            color=color,
            edgecolor=THEME["panel"],
            linewidth=1.1,
            alpha=0.94,
            label=label,
            zorder=2,
        )
        label_color = TEXT if column == "unset_hours" else strong_segment_text
        for patch, width, total in zip(container.patches, widths, plot_df["total_hours"]):
            width_value = float(width)
            total_value = float(total)
            if (
                width_value <= 0
                or total_value <= 0
                or width_value / total_value < 0.14
                or width_value / max_total < 0.08
            ):
                continue
            ax.text(
                patch.get_x() + width_value / 2,
                patch.get_y() + patch.get_height() / 2,
                f"{width_value:.1f}h",
                ha="center",
                va="center",
                color=label_color,
                fontsize=7.5,
                fontweight="bold",
                zorder=3,
            )
        left += widths

    label_pad = max_total * 0.025
    for y_position, total in zip(ax.get_yticks(), plot_df["total_hours"]):
        ax.text(
            float(total) + label_pad,
            y_position,
            f"{float(total):.1f}h",
            ha="left",
            va="center",
            color=MUTED,
            fontsize=8,
            fontweight="bold",
        )
    ax.set_xlim(0, max_total * 1.20)
    ax.set_xlabel("学習時間（時間）")
    style_chart_axis(ax, THEME, title="勉強カテゴリ×インプット/アウトプット", grid_axis="x")
    style_legend(ax, columns=3, outside=True)
    ax.margins(y=0.12)
    fig.tight_layout()
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)

    display = df.rename(
        columns={
            "category": "勉強カテゴリ",
            "input_hours": "インプット時間",
            "input_ratio_pct": "インプット比率(%)",
            "output_hours": "アウトプット時間",
            "output_ratio_pct": "アウトプット比率(%)",
            "unset_hours": "未設定時間",
            "unset_ratio_pct": "未設定比率(%)",
            "total_hours": "合計時間",
            "session_count": "セッション数",
        }
    )
    st.dataframe(
        display[
            [
                "勉強カテゴリ",
                "インプット時間",
                "インプット比率(%)",
                "アウトプット時間",
                "アウトプット比率(%)",
                "未設定時間",
                "未設定比率(%)",
                "合計時間",
                "セッション数",
            ]
        ],
        width="stretch",
        hide_index=True,
    )


def render_heatmap(df: pd.DataFrame, start: date, end: date) -> None:
    if end < start:
        start, end = end, start
    start_monday = start - timedelta(days=start.weekday())
    end_sunday = end + timedelta(days=6 - end.weekday())
    days = pd.date_range(start_monday, end_sunday, freq="D")
    weeks = max(1, len(days) // 7)
    values = df.set_index("date")["total_hours"].to_dict() if not df.empty else {}
    matrix = [[0.0 for _ in range(weeks)] for _ in range(7)]
    for idx, day in enumerate(days):
        matrix[day.weekday()][idx // 7] = float(values.get(pd.Timestamp(day), 0) or 0)

    fig_width = max(10, weeks * 0.35)
    fig, ax = plt.subplots(figsize=(fig_width, 2.9))
    cmap = LinearSegmentedColormap.from_list("learning_navy", ["#F3F4F6", "#C9D7E8", "#6F9AC3", PRIMARY])
    image = ax.imshow(matrix, aspect="auto", cmap=cmap, vmin=0, vmax=max(1, max(max(row) for row in matrix)))
    ax.set_yticks(range(7), WEEKDAY_LABELS)
    month_ticks = []
    month_labels = []
    for week in range(weeks):
        day = (start_monday + timedelta(days=week * 7))
        if day.day <= 7:
            month_ticks.append(week)
            month_labels.append(f"{day.month}月")
    ax.set_xticks(month_ticks, month_labels)
    ax.set_xlabel("週")
    ax.set_title("年間学習ヒートマップ", loc="left", color=TEXT, fontsize=12)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(axis="both", length=0, labelsize=9)
    colorbar = fig.colorbar(image, ax=ax, fraction=0.024, pad=0.02)
    colorbar.set_label("勉強時間", fontsize=9)
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)


def forest_height(row: dict[str, Any], basis: str) -> float:
    if basis == "セッション数":
        return float(row.get("session_count", 0) or 0)
    if basis == "ノート数":
        return float(row.get("note_count", 0) or 0)
    return float(row.get("total_duration_sec", 0) or 0) / 3600




def render_subject_growth(subject_rows: list[dict[str, Any]]) -> None:
    if not subject_rows:
        st.info("対象期間の科目別データはまだありません。")
        return
    df = pd.DataFrame(subject_rows)
    display = df.rename(
        columns={
            "subject_name": "科目",
            "hours": "累積勉強時間",
            "note_count": "ノート数",
            "card_count": "カード数",
            "reminder_count": "リマインダー数",
            "done_reminder_count": "完了済みリマインダー数",
            "pending_reminder_count": "未完了リマインダー数",
        }
    )
    st.dataframe(
        display[
            [
                "科目",
                "累積勉強時間",
                "ノート数",
                "カード数",
                "リマインダー数",
                "完了済みリマインダー数",
                "未完了リマインダー数",
            ]
        ],
        width="stretch",
        hide_index=True,
    )
    chart_df = display.head(12).sort_values("累積勉強時間", ascending=False)
    fig, ax = plt.subplots(figsize=(8, max(2.8, len(chart_df) * 0.42)))
    values = chart_df["累積勉強時間"].astype(float).tolist()
    colors = [THEME["accent"] if index < 3 else PRIMARY for index in range(len(chart_df))]
    plot_rounded_category_bars(
        ax,
        chart_df["科目"].astype(str).tolist(),
        values,
        palette=THEME,
        colors=colors,
        orientation="horizontal",
        linewidth=18,
        formatter=lambda value: f"{value:.1f} h",
    )
    max_value = max(values, default=1.0)
    max_value = max(max_value, 1.0)
    ax.set_xlim(-max_value * 0.025, max_value * 1.18)
    ax.set_xlabel("累積勉強時間")
    style_chart_axis(ax, THEME, title="科目別成長マップ", grid_axis="x")
    ax.margins(y=0.12)
    fig.tight_layout()
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)




def continuation_metrics(
    df: pd.DataFrame,
    start: date,
    end: date,
    streaks: dict[str, Any],
    filters: dict[str, Any],
    *,
    sessions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    now = app_today()
    month_start = date(now.year, now.month, 1)
    month_df = df[(df["date"].dt.date >= month_start) & (df["date"].dt.date <= now)] if not df.empty else df
    studied_month = month_df[month_df["total_duration_sec"] > 0] if not month_df.empty else month_df
    week_count = max(1, ((end - start).days + 1) / 7)
    most_day = ""
    if not df.empty and df["total_duration_sec"].max() > 0:
        row = df.loc[df["total_duration_sec"].idxmax()]
        most_day = f"{row['date'].date().isoformat()} ({format_duration(row['total_duration_sec'])})"

    weekday_label = "未設定"
    if not df.empty:
        weekday_totals = defaultdict(float)
        for _, row in df.iterrows():
            if pd.notna(row["date"]):
                weekday_totals[int(row["date"].weekday())] += float(row["total_duration_sec"] or 0)
        if weekday_totals and max(weekday_totals.values()) > 0:
            weekday_label = WEEKDAY_LABELS[max(weekday_totals, key=weekday_totals.get)]

    hour_label = most_studied_hour(start, end, filters, sessions=sessions)
    return [
        {"指標": "現在の連続学習日数", "値": f"{streaks.get('current_streak', 0)}日"},
        {"指標": "最長連続学習日数", "値": f"{streaks.get('longest_streak', 0)}日"},
        {"指標": "今月の学習日数", "値": f"{len(studied_month)}日"},
        {"指標": "今月の総勉強時間", "値": format_duration(month_df["total_duration_sec"].sum() if not month_df.empty else 0)},
        {"指標": "今月の平均勉強時間", "値": format_duration(studied_month["total_duration_sec"].mean() if len(studied_month) else 0)},
        {"指標": "週平均勉強時間", "値": format_duration((df["total_duration_sec"].sum() if not df.empty else 0) / week_count)},
        {"指標": "最も勉強した日", "値": most_day or "未設定"},
        {"指標": "最も勉強した曜日", "値": weekday_label},
        {"指標": "最も勉強した時間帯", "値": hour_label},
    ]


def most_studied_hour(
    start: date,
    end: date,
    filters: dict[str, Any],
    *,
    sessions: list[dict[str, Any]] | None = None,
) -> str:
    totals: dict[int, float] = defaultdict(float)
    for session in sessions if sessions is not None else load_all_study_sessions(ROOT):
        day = parse_day(session.get("start_at") or session.get("end_at"))
        if not day or not (start <= day <= end):
            continue
        if any(filters.get(key) and session.get(key) != filters[key] for key in ("project_id", "subject_id", "material_id", "unit_id")):
            continue
        from app_core.analysis_dates import parse_analysis_datetime

        started = parse_analysis_datetime(session.get("start_at"))
        if started is None:
            continue
        hour = started.hour
        totals[hour] += float(session.get("duration_sec", 0) or 0)
    if not totals or max(totals.values()) <= 0:
        return "未設定"
    hour = max(totals, key=totals.get)
    return f"{hour:02d}:00台"


def build_comments(month_rows: list[dict[str, Any]], subject_rows: list[dict[str, Any]]) -> list[str]:
    comments: list[str] = []
    month_duration = sum(float(row.get("total_duration_sec", 0) or 0) for row in month_rows)
    learning_days = sum(1 for row in month_rows if float(row.get("total_duration_sec", 0) or 0) > 0)
    note_count = sum(int(row.get("note_count", 0) or 0) for row in month_rows)
    card_count = sum(int(row.get("card_count", 0) or 0) for row in month_rows)
    reminder_count = sum(int(row.get("reminder_count", 0) or 0) for row in month_rows)
    if subject_rows:
        top = subject_rows[0]
        comments.append(f"今月は{top.get('subject_name', '未設定')}の学習時間が最も多くなっています。")
    if learning_days >= 20:
        comments.append("学習日数は安定しており、習慣として定着しつつあります。")
    elif learning_days > 0:
        comments.append("学習記録は残っています。次は学習日数を少しずつ増やすと蓄積が見えやすくなります。")
    else:
        comments.append("今月の学習ログはまだありません。まずは短いセッションから記録を始められます。")
    if month_duration > 0 and note_count == 0:
        comments.append("学習時間に対してノート作成数が少なめです。重要事項だけでもノート化すると復習につながります。")
    if note_count > 0 and card_count == 0:
        comments.append("ノートは作成されていますが、カード化された知識はまだ少なめです。暗記したい項目のカード化を検討してください。")
    if reminder_count > 0:
        comments.append("復習予定が登録されています。未完了の予定が残っていないか確認すると知識定着に役立ちます。")
    return comments[:5]


st.set_page_config(page_title="学習蓄積ビジュアライザー", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("学習時間、ノート、カード、復習予定の蓄積を確認します。", settings=PAGE_SETTINGS)

today = app_today()
default_start = today - timedelta(days=365)
default_end = today
master = MasterSnapshot.load(ROOT)

with st.sidebar:
    st.subheader("表示条件")
    start_date = st.date_input("開始日", value=default_start)
    end_date = st.date_input("終了日", value=default_end)

    project_options = option_map(master.active("projects"), "project_id")
    project_label = st.selectbox("プロジェクト", list(project_options.keys()))
    project_id = selected_value(project_label, project_options)

    subject_options = option_map(master.active("subjects", project_id=project_id), "subject_id")
    subject_label = st.selectbox("科目", list(subject_options.keys()))
    subject_id = selected_value(subject_label, subject_options)

    material_options = option_map(master.active("materials", project_id=project_id, subject_id=subject_id), "material_id")
    material_label = st.selectbox("教材", list(material_options.keys()))
    material_id = selected_value(material_label, material_options)

    unit_options = option_map(master.active("units", project_id=project_id, subject_id=subject_id, material_id=material_id), "unit_id")
    unit_label = st.selectbox("単元", list(unit_options.keys()))
    unit_id = selected_value(unit_label, unit_options)

    display_unit = st.selectbox("表示単位", ["日別", "週別", "月別"])

filters = {"project_id": project_id, "subject_id": subject_id, "material_id": material_id, "unit_id": unit_id}
if end_date < start_date:
    start_date, end_date = end_date, start_date

ab.topbar("学習蓄積ビジュアライザー", "日々の学習時間、知識整理、復習準備の蓄積を視覚的に確認します。")

learning_data = LearningDataSnapshot.load()
daily_rows = build_daily_learning_summary(start_date, end_date, filters, snapshot=learning_data)
daily_df = rows_to_frame(daily_rows)
unit_df = aggregate_for_unit(daily_df, display_unit)
streaks = calculate_learning_streaks(daily_rows)
subject_rows = build_subject_growth_summary(start_date, end_date, filters, snapshot=learning_data, master=master)
month_rows = build_monthly_forest_data(today.year, today.month, filters, snapshot=learning_data)
from app_core.study_category_service import load_study_category_catalog, study_category_aliases
category_io_rows = build_category_io_summary(start_date, end_date, filters, sessions=learning_data.activity_sessions,
    category_aliases=study_category_aliases(load_study_category_catalog(ROOT)))

total_duration = int(daily_df["total_duration_sec"].sum()) if not daily_df.empty else 0
total_money = float(daily_df["total_money"].sum()) if not daily_df.empty else 0
learning_days = int((daily_df["total_duration_sec"] > 0).sum()) if not daily_df.empty else 0
note_count = int(daily_df["note_count"].sum()) if not daily_df.empty else 0
card_count = int(daily_df["card_count"].sum()) if not daily_df.empty else 0
pending_reminders = sum(int(row.get("pending_reminder_count", 0) or 0) for row in subject_rows)

st.subheader("主要KPI")
kpi_rows = [st.columns(4), st.columns(4)]
with kpi_rows[0][0]:
    ab.metric_block("期間内の総勉強時間", format_duration(total_duration), "対象期間の学習実績")
with kpi_rows[0][1]:
    ab.metric_block("期間内の総換算金額", format_yen(total_money), "勉強時間の価値換算")
with kpi_rows[0][2]:
    ab.metric_block("学習日数", f"{learning_days}日", "勉強ログがある日数")
with kpi_rows[0][3]:
    ab.metric_block("連続学習日数", f"{streaks.get('current_streak', 0)}日", "対象期間末日時点")
with kpi_rows[1][0]:
    ab.metric_block("最長連続学習日数", f"{streaks.get('longest_streak', 0)}日", "対象期間内の最長記録")
with kpi_rows[1][1]:
    ab.metric_block("作成ノート数", f"{note_count}件", "期間内に作成されたノート")
with kpi_rows[1][2]:
    ab.metric_block("作成カード数", f"{card_count}件", "期間内に作成されたカード")
with kpi_rows[1][3]:
    ab.metric_block("未完了リマインダー数", f"{pending_reminders}件", "未完了または保留中の復習予定")

if daily_df.empty or total_duration == 0:
    st.info("対象期間に勉強ログはまだありません。ノートや復習データがある場合も、学習時間の可視化はログ保存後に表示されます。")

st.subheader("年間学習ヒートマップ")
render_heatmap(daily_df, start_date, end_date)
with st.expander("ヒートマップ補助データ", expanded=False):
    heatmap_table = daily_df.sort_values(
        "date",
        ascending=False,
        kind="stable",
        na_position="last",
    ).copy()
    if not heatmap_table.empty:
        heatmap_table["date"] = heatmap_table["date"].dt.date.astype(str)
        heatmap_table["主なプロジェクト"] = heatmap_table["main_project"].apply(lambda value: master.name("project_id", value) if value else "未設定")
        heatmap_table["主な科目"] = heatmap_table["main_subject"].apply(lambda value: master.name("subject_id", value) if value else "未設定")
        st.dataframe(
            heatmap_table[["date", "total_duration_sec", "total_hours", "session_count", "主なプロジェクト", "主な科目", "note_count", "card_count", "reminder_count"]],
            width="stretch",
            hide_index=True,
        )

st.subheader("学習アトラス")
st.caption("学習銀河・学習の航跡・クリスタル標本を切り替えて、科目・教材・単元の蓄積を探索できます。対象期間はサイドバーの表示条件に従います。")
visual_payload = build_learning_visual_payload(
    learning_data.activity_sessions,
    master,
    start_date,
    end_date,
    filters,
    notes=learning_data.notes,
)
render_learning_visuals(visual_payload, palette=THEME)

left, right = st.columns([1.2, 1])
with left:
    st.subheader("科目別成長マップ")
    render_subject_growth(subject_rows)

with right:
    st.subheader("継続指標")
    metrics = continuation_metrics(daily_df, start_date, end_date, streaks, filters, sessions=learning_data.activity_sessions)
    st.dataframe(pd.DataFrame(metrics), width="stretch", hide_index=True)

st.subheader("勉強カテゴリ×インプット/アウトプット")
render_category_io_summary(category_io_rows)

st.subheader("今月の蓄積コメント")
for comment in build_comments(month_rows, subject_rows):
    st.info(comment)

st.subheader("集計データ出力")
download_df = unit_df.copy()
if not download_df.empty:
    download_df["date"] = download_df["date"].dt.date.astype(str)
st.download_button(
    "日別集計CSVをダウンロード",
    data=to_csv_bytes(download_df),
    file_name="learning_accumulation_summary.csv",
    mime="text/csv",
    width="stretch", on_click="ignore")
