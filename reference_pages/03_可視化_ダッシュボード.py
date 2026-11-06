from __future__ import annotations

from web_demo.data import PERIOD_START, REFERENCE_DATE
from app_core.json_store import record_json_files

import re
from collections import Counter
from datetime import date, timedelta
from math import cos, log1p, sin
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, FancyBboxPatch
import pandas as pd
import streamlit as st

from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

try:
    import seaborn as sns
except ImportError:
    sns = None

try:
    import japanize_matplotlib  # noqa: F401
except ImportError:
    pass

import prolog_ui.components as ab
from app_core.app_clock import app_today
from app_core.analysis_dates import analysis_datetime_series
from app_core.daily_analysis_service import build_calendar_study_totals
from app_core.plot_style import (
    add_bar_value_labels,
    configure_japanese_font,
    configure_plot_theme,
    plot_styled_line,
    plot_time_bars,
    style_chart_axis,
    style_date_axis,
    style_legend,
)
from data_utils import load_study_sessions_file


ROOT = Path(__file__).resolve().parents[1]
STUDY_DIR = ROOT / "study_logs"
THEME = ab.theme_palette(settings=PAGE_SETTINGS)
ACCENT = THEME["accent"]
GRID = THEME["grid"]
WEEKDAY_ORDER = ["月", "火", "水", "木", "金", "土", "日"]
WORD_SPLIT_RE = re.compile(r"[一-龥ぁ-んァ-ヶーA-Za-z0-9]+")
WORD_CLOUD_MAX_WORDS = 50
WORD_CLOUD_POINT_SCALE = 1.42
SEABORN_PALETTES = ["Blues", "viridis", "mako", "crest", "rocket"]
MATPLOTLIB_PALETTES = ["Blues", "viridis", "cividis", "YlGnBu", "PuBu"]
STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "nan",
    "of",
    "on",
    "the",
    "to",
    "was",
    "were",
    "with",
    "こと",
    "もの",
    "ため",
    "今日",
    "昨日",
    "明日",
    "勉強",
    "学習",
    "復習",
    "確認",
    "内容",
    "メモ",
    "なし",
    "する",
    "した",
    "して",
    "から",
    "まで",
}

configure_japanese_font()
configure_plot_theme(ab.is_dark_theme())


def sec_to_hms(seconds: int | float) -> str:
    total = max(int(seconds or 0), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def yen(value: int | float) -> str:
    return f"{int(value or 0):,}円"


def load_sessions(path: Path) -> list[dict[str, Any]]:
    return load_study_sessions_file(path)


def load_df() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for path in record_json_files(STUDY_DIR):
        for item in load_sessions(path):
            if isinstance(item, dict):
                rows.append(item)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    starts = df["start_at"] if "start_at" in df.columns else pd.Series(None, index=df.index, dtype="object")
    df["start_at"] = analysis_datetime_series(starts)
    for key in ("duration_sec", "money"):
        values = df[key] if key in df.columns else pd.Series(None, index=df.index, dtype="object")
        df[key] = pd.to_numeric(values, errors="coerce").replace([float("inf"), float("-inf")], float("nan")).fillna(0)
    invalid_datetime_count = int(df["start_at"].isna().sum())
    df = df.dropna(subset=["start_at"])
    df["date"] = df["start_at"].dt.date
    df["hour"] = df["start_at"].dt.hour
    df["weekday_num"] = df["start_at"].dt.weekday
    df["weekday"] = df["weekday_num"].map(dict(enumerate(WEEKDAY_ORDER)))
    df["study_hours"] = df["duration_sec"] / 3600
    df.attrs["invalid_datetime_count"] = invalid_datetime_count
    return df


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def chart_color(palette: str) -> str:
    if sns is not None:
        return sns.color_palette(palette, 6)[2]
    return ab.theme_palette(settings=PAGE_SETTINGS)["primary"]


def heatmap_cmap(palette: str) -> str:
    if sns is not None:
        return palette
    return palette if palette in plt.colormaps() else "Blues"


def tokenize_study_memo(value: Any) -> list[str]:
    if value is None or pd.isna(value):
        return []
    words: list[str] = []
    for token in WORD_SPLIT_RE.findall(str(value)):
        word = token.strip()
        if len(word) < 2 or word.casefold() in STOP_WORDS or word.isdecimal():
            continue
        words.append(word)
    return words


def aggregate_study_memo_words(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["word", "duration_sec", "session_count", "occurrence_count"]
    if frame.empty or "memo" not in frame.columns:
        return pd.DataFrame(columns=columns)

    study_seconds: Counter[str] = Counter()
    session_counts: Counter[str] = Counter()
    occurrence_counts: Counter[str] = Counter()
    display_labels: dict[str, str] = {}

    for row in frame[["memo", "duration_sec"]].itertuples(index=False):
        words = tokenize_study_memo(row.memo)
        if not words:
            continue

        normalized_words: list[str] = []
        for word in words:
            normalized = word.casefold()
            current_label = display_labels.get(normalized)
            if current_label is None or (word.isupper() and not current_label.isupper()):
                display_labels[normalized] = word
            occurrence_counts[normalized] += 1
            normalized_words.append(normalized)

        duration_sec = max(float(row.duration_sec or 0), 0.0)
        for normalized in set(normalized_words):
            study_seconds[normalized] += duration_sec
            session_counts[normalized] += 1

    rows = [
        {
            "word": display_labels[normalized],
            "duration_sec": float(study_seconds[normalized]),
            "session_count": int(session_counts[normalized]),
            "occurrence_count": int(occurrence_counts[normalized]),
        }
        for normalized in display_labels
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["duration_sec", "session_count", "occurrence_count", "word"],
        ascending=[False, False, False, True],
        ignore_index=True,
    )


def estimated_word_width_units(word: str) -> float:
    return sum(0.62 if character.isascii() else 1.0 for character in word)


def boxes_overlap(
    candidate: tuple[float, float, float, float],
    placed_boxes: list[tuple[float, float, float, float]],
) -> bool:
    left, top, right, bottom = candidate
    return any(
        not (right <= other_left or left >= other_right or bottom <= other_top or top >= other_bottom)
        for other_left, other_top, other_right, other_bottom in placed_boxes
    )


def build_word_cloud_layout(word_stats: pd.DataFrame) -> tuple[int, int, list[dict[str, Any]]]:
    canvas_width = 1200
    canvas_height = 560
    margin = 20
    positive_stats = word_stats.loc[word_stats["duration_sec"] > 0].head(WORD_CLOUD_MAX_WORDS)
    if positive_stats.empty:
        return canvas_width, canvas_height, []

    records = positive_stats.to_dict("records")
    log_weights = [log1p(float(record["duration_sec"])) for record in records]
    min_weight = min(log_weights)
    weight_span = max(max(log_weights) - min_weight, 1e-9)
    colors = [
        THEME["primary"],
        THEME["green"],
        THEME["purple"],
        THEME["cyan"],
        THEME["amber"],
        THEME["primary_2"],
    ]
    placed_boxes: list[tuple[float, float, float, float]] = []
    placed_words: list[dict[str, Any]] = []

    for index, (record, log_weight) in enumerate(zip(records, log_weights)):
        word = str(record["word"])
        display_word = word if len(word) <= 24 else f"{word[:23]}…"
        normalized_weight = (log_weight - min_weight) / weight_span
        font_size = 13.0 + normalized_weight * 55.0
        width_units = max(estimated_word_width_units(display_word), 1.0)
        font_size = max(12.0, min(font_size, (canvas_width * 0.70) / width_units))
        rotation = 90 if index >= 12 and index % 11 == 0 and len(display_word) <= 10 else 0
        text_width = width_units * font_size * 1.02 * WORD_CLOUD_POINT_SCALE + 12
        text_height = font_size * 1.18 * WORD_CLOUD_POINT_SCALE + 10
        box_width, box_height = (text_height, text_width) if rotation else (text_width, text_height)
        placed_at: tuple[float, float] | None = None

        for attempt in range(2400):
            angle = attempt * 0.38 + index * 0.17
            radius = attempt * 0.32
            center_x = canvas_width / 2 + radius * cos(angle)
            center_y = canvas_height / 2 + radius * 0.47 * sin(angle)
            candidate = (
                center_x - box_width / 2 - 6,
                center_y - box_height / 2 - 6,
                center_x + box_width / 2 + 6,
                center_y + box_height / 2 + 6,
            )
            if (
                candidate[0] < margin
                or candidate[1] < margin
                or candidate[2] > canvas_width - margin
                or candidate[3] > canvas_height - margin
                or boxes_overlap(candidate, placed_boxes)
            ):
                continue
            placed_at = (center_x, center_y)
            placed_boxes.append(candidate)
            break

        if placed_at is None:
            continue

        color = colors[(index * 5) % len(colors)]
        opacity = 0.76 + normalized_weight * 0.24
        placed_words.append(
            {
                "word": display_word,
                "x": placed_at[0],
                "y": placed_at[1],
                "font_size": font_size,
                "rotation": rotation,
                "color": color,
                "opacity": opacity,
            }
        )

    return canvas_width, canvas_height, placed_words


def render_word_cloud(word_stats: pd.DataFrame) -> None:
    if word_stats.empty or not (word_stats["duration_sec"] > 0).any():
        st.info("勉強内容として集計できる語句はまだありません。")
        return
    canvas_width, canvas_height, placed_words = build_word_cloud_layout(word_stats)
    if not placed_words:
        st.info("表示領域に配置できる語句がありません。")
        return

    fig, ax = plt.subplots(figsize=(12, canvas_height / 100))
    fig.patch.set_facecolor(THEME["card"])
    ax.set_facecolor(THEME["card"])
    ax.add_patch(
        FancyBboxPatch(
            (5, 5),
            canvas_width - 10,
            canvas_height - 10,
            boxstyle="round,pad=0.01,rounding_size=24",
            facecolor=THEME["card"],
            edgecolor=THEME["line"],
            linewidth=1.2,
            zorder=0,
        )
    )
    ax.add_patch(
        Ellipse(
            (canvas_width / 2, canvas_height / 2),
            width=canvas_width * 0.76,
            height=canvas_height * 0.72,
            facecolor=THEME["primary"],
            edgecolor="none",
            alpha=0.035,
            zorder=1,
        )
    )
    for item in placed_words:
        ax.text(
            item["x"],
            item["y"],
            item["word"],
            fontsize=item["font_size"],
            fontweight="bold",
            color=item["color"],
            alpha=item["opacity"],
            rotation=item["rotation"],
            ha="center",
            va="center",
            zorder=2,
        )
    ax.set_xlim(0, canvas_width)
    ax.set_ylim(canvas_height, 0)
    ax.axis("off")
    fig.tight_layout(pad=0.15)
    st.pyplot(fig, width="stretch")
    plt.close(fig)


st.set_page_config(page_title="可視化ダッシュボード", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("表示期間とグラフ設定を調整できます。", settings=PAGE_SETTINGS)

df = load_df()
default_start = PERIOD_START
if df.empty:
    default_start = app_today() - timedelta(days=30)
    default_end = app_today()
else:
    default_end = app_today()

with st.sidebar:
    st.subheader("絞り込みとオプション")
    start_date = st.date_input("開始日", value=default_start, min_value=PERIOD_START, max_value=REFERENCE_DATE)
    end_date = st.date_input("終了日", value=default_end, min_value=PERIOD_START, max_value=REFERENCE_DATE)
    ma_days = st.slider("移動平均日数", min_value=2, max_value=30, value=7)
    show_grid = st.toggle("グリッド線", value=True)
    palette = st.selectbox("カラーパレット", SEABORN_PALETTES if sns is not None else MATPLOTLIB_PALETTES)

ab.topbar("可視化ダッシュボード", "蓄積された学習記録を、習慣と成果の両面から可視化します。")

if df.attrs.get("invalid_datetime_count", 0):
    st.warning(f"開始日時を解釈できない記録 {df.attrs['invalid_datetime_count']} 件を日時別の集計から除外しています。元の記録は変更していません。勉強ログ・データ整合性チェックで確認できます。")

start_date = max(PERIOD_START, min(start_date, REFERENCE_DATE))


end_date = max(PERIOD_START, min(end_date, REFERENCE_DATE))


if end_date < start_date:
    start_date, end_date = end_date, start_date

if df.empty:
    st.info("まだ可視化できる勉強ログがありません。まずは秒給カウンターから記録を作成してください。")
    st.stop()

filtered = df[(df["date"] >= start_date) & (df["date"] <= end_date)].copy()
if filtered.empty:
    st.warning("選択期間にデータがありません。期間を変更してください。")
    st.stop()

daily = build_calendar_study_totals(
    filtered, start_date, end_date, moving_average_days=ma_days,
)

calendar_days = max((end_date - start_date).days + 1, 1)
calendar_index = pd.date_range(start_date, end_date, freq="D")
weekday_days = int((calendar_index.weekday < 5).sum())
weekend_days = int((calendar_index.weekday >= 5).sum())
active_days = filtered["date"].nunique()
total_hours = filtered["duration_sec"].sum() / 3600
total_money = filtered["money"].sum()
weekday_hours = filtered.loc[filtered["weekday_num"] < 5, "duration_sec"].sum() / 3600
weekend_hours = filtered.loc[filtered["weekday_num"] >= 5, "duration_sec"].sum() / 3600

st.subheader("KPIサマリ")
cols = st.columns(4)
with cols[0]:
    ab.metric_block("総勉強時間", sec_to_hms(filtered["duration_sec"].sum()), "対象期間の合計")
with cols[1]:
    ab.metric_block("総換算金額", yen(total_money), "対象期間の合計")
with cols[2]:
    ab.metric_block("稼働日数", f"{active_days}日", "学習記録がある日")
with cols[3]:
    ab.metric_block("1稼働日平均", f"{(total_hours / active_days if active_days else 0):.2f}h", "実績日の平均")

cols = st.columns(4)
with cols[0]:
    ab.metric_block("カレンダー日数", f"{calendar_days}日", "期間全体")
with cols[1]:
    ab.metric_block("期間1日平均", f"{total_hours / calendar_days:.2f}h", "非稼働日を含む")
with cols[2]:
    ab.metric_block("平日1日平均", f"{weekday_hours / max(weekday_days, 1):.2f}h", f"平日 {weekday_days}日")
with cols[3]:
    ab.metric_block("土日1日平均", f"{weekend_hours / max(weekend_days, 1):.2f}h", f"土日 {weekend_days}日")

if sns is not None:
    sns.set_theme(style="whitegrid" if show_grid else "white", palette=palette)
else:
    plt.style.use("default")
configure_japanese_font()
configure_plot_theme(ab.is_dark_theme())

st.subheader("日別勉強時間")
st.caption("移動平均は暦日単位です。学習記録のない日は集計上0として扱います。")
fig, ax = plt.subplots(figsize=(11, 4))
study_values = daily["study_hours"].to_numpy(dtype=float)
highlight_indices = sorted({len(daily) - 1, int(study_values.argmax())})
study_bars = plot_time_bars(
    ax,
    daily["date"],
    study_values,
    palette=THEME,
    color=chart_color(palette),
    label="勉強時間",
    alpha=0.78,
    accent_color=ACCENT,
    highlight_indices=highlight_indices,
)
add_bar_value_labels(
    ax,
    study_bars,
    palette=THEME,
    formatter=lambda value: f"{value:.1f}h",
    indices=highlight_indices,
)
plot_styled_line(
    ax,
    daily["date"],
    daily["hours_ma"],
    palette=THEME,
    label=f"{ma_days}日移動平均",
    style_index=1,
    linestyle="-",
    max_markers=16,
    linewidth=2.4,
    zorder=4,
)
style_chart_axis(ax, THEME, ylabel="時間", grid_axis="y")
ax.margins(y=0.12)
if not show_grid:
    ax.grid(False)
style_date_axis(ax)
style_legend(ax, columns=2, outside=True)
fig.tight_layout()
st.pyplot(fig)
plt.close(fig)

st.subheader("日別金額換算")
fig, ax = plt.subplots(figsize=(11, 4))
money_line = plot_styled_line(
    ax,
    daily["date"],
    daily["money"],
    palette=THEME,
    label="換算金額",
    style_index=0,
    max_markers=24,
    linewidth=1.7,
    alpha=0.9,
)
money_line.set_markeredgecolor("#FFFFFF")
money_line.set_markeredgewidth(1.1)
plot_styled_line(
    ax,
    daily["date"],
    daily["money_ma"],
    palette=THEME,
    label=f"{ma_days}日移動平均",
    style_index=1,
    linestyle="--",
    show_markers=False,
    linewidth=2.4,
    zorder=4,
)
style_chart_axis(ax, THEME, ylabel="円", grid_axis="y")
if not show_grid:
    ax.grid(False)
style_date_axis(ax)
style_legend(ax, columns=2, outside=True)
fig.tight_layout()
st.pyplot(fig)
plt.close(fig)

st.subheader("時間帯×曜日ヒートマップ")
heat = filtered.pivot_table(index="hour", columns="weekday", values="study_hours", aggfunc="sum", fill_value=0)
heat = heat.reindex(index=range(23, -1, -1), columns=WEEKDAY_ORDER, fill_value=0)
fig, ax = plt.subplots(figsize=(9, 7))
if sns is not None:
    sns.heatmap(heat, cmap=palette, ax=ax)
else:
    image = ax.imshow(heat.values, aspect="auto", cmap=heatmap_cmap(palette))
    ax.set_xticks(range(len(heat.columns)), heat.columns)
    ax.set_yticks(range(len(heat.index)), heat.index)
    fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
ax.set_xlabel("曜日")
ax.set_ylabel("開始時刻")
st.pyplot(fig)
plt.close(fig)

st.subheader("累積カーブ")
fig, ax1 = plt.subplots(figsize=(11, 4))
ax2 = ax1.twinx()
hours_line = plot_styled_line(
    ax1,
    daily["date"],
    daily["cum_hours"],
    palette=THEME,
    label="累積勉強時間",
    style_index=0,
    show_markers=False,
    linewidth=2.3,
)
money_line = plot_styled_line(
    ax2,
    daily["date"],
    daily["cum_money"],
    palette=THEME,
    label="累積換算金額",
    style_index=1,
    linestyle="--",
    show_markers=False,
    linewidth=2.1,
)
style_chart_axis(ax1, THEME, ylabel="累積勉強時間（時間）", grid_axis="y")
style_chart_axis(ax2, THEME, ylabel="累積換算金額（円）", grid_axis="y")
ax2.grid(False)
if not show_grid:
    ax1.grid(False)
style_date_axis(ax1)
ax1.yaxis.label.set_color(hours_line.get_color())
ax2.yaxis.label.set_color(money_line.get_color())
ax1.tick_params(axis="y", colors=hours_line.get_color())
ax2.tick_params(axis="y", colors=money_line.get_color())
ax1.legend(
    [hours_line, money_line],
    [hours_line.get_label(), money_line.get_label()],
    loc="lower right",
    bbox_to_anchor=(1.0, 1.01),
    ncol=2,
    frameon=False,
    fontsize=8.5,
    handlelength=2.4,
    handletextpad=0.5,
    columnspacing=1.2,
    borderaxespad=0,
)
fig.tight_layout()
st.pyplot(fig)
plt.close(fig)

st.subheader("セッション分布")
fig, ax = plt.subplots(figsize=(11, 4))
sizes = (filtered["money"].clip(lower=1) / filtered["money"].clip(lower=1).max()) * 500
ax.scatter(filtered["hour"], filtered["duration_sec"] / 60, s=sizes, alpha=0.55)
ax.set_xlabel("開始時刻")
ax.set_ylabel("学習時間（分）")
ax.set_xticks(range(0, 24, 2))
if show_grid:
    ax.grid(color=GRID, linewidth=0.8)
st.pyplot(fig)
plt.close(fig)

st.subheader("勉強内容ワードクラウド")
st.caption(
    "文字サイズは、その語句を含むセッションの累積勉強時間です。"
    "同じセッション内の同一語句は1回分の時間として集計します。"
    "複数の語句を含むセッションは各語句に同じ時間が関連付くため、語句別時間は合算できません。"
)
word_stats = aggregate_study_memo_words(filtered)
render_word_cloud(word_stats)
with st.expander("語句別の関連勉強時間", expanded=False):
    if word_stats.empty:
        st.info("表示できる語句別データはまだありません。")
    else:
        word_table = word_stats.head(100).copy()
        word_table["関連勉強時間(h)"] = (word_table["duration_sec"] / 3600).round(2)
        word_table = word_table.rename(
            columns={
                "word": "語句",
                "session_count": "関連セッション数",
                "occurrence_count": "出現回数",
            }
        )[["語句", "関連勉強時間(h)", "関連セッション数", "出現回数"]]
        st.dataframe(
            word_table,
            width="stretch",
            hide_index=True,
        )

st.download_button("日別サマリデータCSV", to_csv_bytes(daily), "visualization_daily_summary.csv", "text/csv", on_click="ignore")
