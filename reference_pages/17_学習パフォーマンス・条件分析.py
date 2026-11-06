from __future__ import annotations

from datetime import date, timedelta
from html import escape
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.analysis_statistics import classify_evidence_level
from app_core.app_clock import app_today
from app_core.performance_analysis_service import (
    FACTOR_LABELS,
    MATCHING_LABELS,
    STUDY_CATEGORIES,
    available_date_bounds,
    build_daily_feature_frame,
    build_evidence_table,
    calculate_performance_delta,
    factor_groups,
    find_comparable_days,
    summarize_high_performance_profile,
)
from app_core.plot_style import configure_japanese_font, plot_rounded_category_bars, style_chart_axis
from app_core.performance_result_cache import PerformanceResultCache
from app_core.settings_service import should_show_task_metrics, should_treat_missing_caffeine_as_zero
from config import get_data_root

PROJECT_ROOT = get_data_root()
from app_core.study_category_service import load_study_category_catalog, study_category_names, other_category_name


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()
_CATEGORY_CATALOG = load_study_category_catalog(PROJECT_ROOT)
STUDY_CATEGORIES = tuple(dict.fromkeys((*study_category_names(_CATEGORY_CATALOG, include_inactive=True), other_category_name(_CATEGORY_CATALOG))))

THEME = ab.theme_palette(settings=PAGE_SETTINGS)
NAVY = THEME["ink"]
BLUE = THEME["primary"]
TEAL = THEME["green"]
GREEN = THEME["green"]
AMBER = THEME["amber"]
RED = THEME["red"]
SLATE = THEME["muted"]
PALE = THEME["panel_2"]

OUTCOME_OPTIONS = [
    ("総勉強時間", "study_hours", "時間"),
    ("アウトプット時間", "output_hours", "時間"),
    ("アウトプット比率", "output_ratio", "%"),
    ("計画実行率", "plan_execution_rate", "%"),
    ("タスク達成率", "task_completion_rate", "%"),
    ("集中しやすさ", "focus_score", "/ 5"),
    ("夜間学習時間", "night_study_hours", "時間"),
]
OUTCOME_OPTIONS.extend(
    (f"{category}の学習時間", f"study_category_{category}_hours", "時間")
    for category in STUDY_CATEGORIES
)

NEXT_OPTIONS = [
    ("翌日コンディションバランス", "next_condition_balance", "標準化値", 1),
    ("翌日集中しやすさ", "next_focus_score", "/ 5", 1),
    ("翌朝の眠気", "next_morning_sleepiness", "/ 5", -1),
    ("翌日の疲労感", "next_fatigue", "/ 5", -1),
    ("翌日のストレス", "next_stress", "/ 5", -1),
    ("翌日の睡眠の質", "next_sleep_quality", "/ 5", 1),
]

QUADRANT_COLORS = {
    "持続型": GREEN,
    "高成果・翌日負荷あり": AMBER,
    "回復優先型": BLUE,
    "低調型": SLATE,
}


st.set_page_config(page_title="学習パフォーマンス・条件分析", layout="wide")
configure_japanese_font()
ab.inject_css(settings=PAGE_SETTINGS)


def inject_page_css() -> None:
    st.markdown(
        """
        <style>
        .perf-metric {
            border: 1px solid var(--ab-line);
            border-top: 4px solid var(--accent);
            border-radius: 16px;
            padding: 15px 16px;
            min-height: 126px;
            background: var(--ab-card);
            box-shadow: 0 8px 24px var(--ab-shadow);
        }
        .perf-metric-label { color: var(--ab-muted); font-size: .82rem; font-weight: 700; }
        .perf-metric-value { color: var(--ab-ink); font-size: 1.62rem; font-weight: 800; margin-top: .45rem; }
        .perf-metric-caption { color: var(--ab-muted); font-size: .78rem; margin-top: .4rem; }
        .perf-note {
            border-left: 4px solid var(--ab-primary);
            background: var(--ab-panel-2);
            border-radius: 0 12px 12px 0;
            padding: 12px 15px;
            color: var(--ab-ink);
            margin: .5rem 0;
        }
        .perf-control {
            display: inline-block;
            border: 1px solid var(--ab-line);
            border-radius: 999px;
            background: var(--ab-card);
            color: var(--ab-ink);
            padding: 5px 10px;
            margin: 0 6px 6px 0;
            font-size: .78rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def period_range(label: str, today: date) -> tuple[date, date]:
    if label == "直近30日":
        return today - timedelta(days=29), today
    if label == "今月":
        return date(today.year, today.month, 1), today
    if label == "記録全期間":
        earliest, latest = available_date_bounds()
        return earliest or today - timedelta(days=89), latest or today
    return today - timedelta(days=89), today


def metric_card(label: str, value: Any, caption: str, accent: str = BLUE) -> None:
    st.markdown(
        f"""
        <div class="perf-metric" style="--accent:{escape(accent, quote=True)}">
          <div class="perf-metric-label">{escape(str(label))}</div>
          <div class="perf-metric-value">{escape(str(value))}</div>
          <div class="perf-metric-caption">{escape(str(caption))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def number_text(value: Any, unit: str = "", digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if not np.isfinite(number):
        return "-"
    suffix = f" {unit}" if unit else ""
    return f"{number:+.{digits}f}{suffix}"


def interval_text(low: Any, high: Any, unit: str = "") -> str:
    if pd.isna(low) or pd.isna(high):
        return "-"
    suffix = f" {unit}" if unit else ""
    return f"{float(low):+.2f}～{float(high):+.2f}{suffix}"


def outcome_options(frame: pd.DataFrame, include_task_metrics: bool = True) -> list[tuple[str, str, str]]:
    return [
        option
        for option in OUTCOME_OPTIONS
        if (include_task_metrics or option[1] != "task_completion_rate")
        and option[1] in frame
        and pd.to_numeric(frame[option[1]], errors="coerce").notna().any()
    ]


def next_options(frame: pd.DataFrame) -> list[tuple[str, str, str, int]]:
    return [
        option
        for option in NEXT_OPTIONS
        if option[1] in frame and pd.to_numeric(frame[option[1]], errors="coerce").notna().any()
    ]


def render_matrix(
    frame: pd.DataFrame,
    outcome_label: str,
    next_label: str,
    next_column: str,
    next_direction: int,
    primary_column: str,
) -> pd.DataFrame:
    plot = frame.dropna(subset=["_analysis_outcome", next_column]).copy()
    if len(plot) < 2:
        st.info("成果と翌日状態がそろう日が不足しています。")
        return pd.DataFrame()

    outcome_median = float(plot["_analysis_outcome"].median())
    next_median = float(plot[next_column].median())
    high_outcome = plot["_analysis_outcome"] >= outcome_median
    favorable_next = plot[next_column] >= next_median if next_direction > 0 else plot[next_column] <= next_median
    plot["quadrant"] = np.select(
        [high_outcome & favorable_next, high_outcome & ~favorable_next, ~high_outcome & favorable_next],
        ["持続型", "高成果・翌日負荷あり", "回復優先型"],
        default="低調型",
    )

    if primary_column == "study_hours":
        size_source = pd.to_numeric(plot["study_session_count"], errors="coerce").fillna(0)
        sizes = 55 + size_source.clip(upper=12) * 12
        size_caption = "点の大きさは学習セッション数"
    else:
        size_source = pd.to_numeric(plot["study_hours"], errors="coerce").fillna(0)
        sizes = 55 + size_source.clip(upper=10) * 15
        size_caption = "点の大きさは総勉強時間"

    fig, ax = plt.subplots(figsize=(10.8, 6.1))
    fig.patch.set_facecolor(THEME["card"])
    legend_added: set[str] = set()
    for quadrant, color in QUADRANT_COLORS.items():
        subset = plot.loc[plot["quadrant"] == quadrant]
        for running, marker in ((False, "o"), (True, "D")):
            points = subset.loc[subset["running_recorded"].fillna(False) == running]
            if points.empty:
                continue
            ax.scatter(
                points["_analysis_outcome"],
                points[next_column],
                s=sizes.loc[points.index],
                c=color,
                marker=marker,
                alpha=0.78,
                edgecolors=THEME["card"],
                linewidths=0.9,
                label=quadrant if quadrant not in legend_added else None,
            )
            legend_added.add(quadrant)
    ax.axvline(outcome_median, color=NAVY, linestyle="--", linewidth=1.2, alpha=0.65)
    ax.axhline(next_median, color=NAVY, linestyle="--", linewidth=1.2, alpha=0.65)
    ax.set_xlabel(outcome_label)
    ax.set_ylabel(f"{next_label}（{'高い' if next_direction > 0 else '低い'}ほど良好）")
    ax.set_title("当日の成果と翌日コンディション", loc="left", color=NAVY, fontweight="bold")
    ax.grid(alpha=0.16)
    handles, labels = ax.get_legend_handles_labels()
    unique = dict(zip(labels, handles))
    if unique:
        ax.legend(unique.values(), unique.keys(), loc="best", frameon=False)
    fig.tight_layout()
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)
    st.caption(f"{size_caption}、ひし形はランニング記録がある日です。破線は選択期間内の中央値です。")

    counts = plot["quadrant"].value_counts().reindex(QUADRANT_COLORS, fill_value=0).rename_axis("区分").reset_index(name="日数")
    st.dataframe(counts, width="stretch", hide_index=True)
    return plot


def render_profile(profile: pd.DataFrame) -> None:
    if profile.empty:
        st.info("高パフォーマンス日と通常日を比較できるデータが不足しています。")
        return
    chart = profile.dropna(subset=["standardized_difference"]).copy()
    left, right = st.columns([1.05, 1.35])
    with left:
        if chart.empty:
            st.info("標準化できる条件がまだありません。")
        else:
            chart = chart.sort_values("standardized_difference", ascending=False)
            fig, ax = plt.subplots(figsize=(7.2, max(4.2, len(chart) * 0.46)))
            values = chart["standardized_difference"].astype(float).tolist()
            colors = [TEAL if value >= 0 else AMBER for value in values]
            plot_rounded_category_bars(
                ax,
                chart["label"].astype(str).tolist(),
                values,
                palette=THEME,
                colors=colors,
                orientation="horizontal",
                linewidth=18,
                formatter=lambda value: f"{value:+.2f}",
                alpha=0.9,
            )
            max_abs = max((abs(value) for value in values), default=0.0)
            axis_limit = max(max_abs * 1.22, 1.0)
            ax.set_xlim(-axis_limit, axis_limit)
            ax.axvline(0, color=NAVY, linewidth=1.2, alpha=0.78, zorder=2)
            ax.set_xlabel("標準化した条件差")
            style_chart_axis(
                ax,
                THEME,
                title="高パフォーマンス日 − 通常日",
                title_color=NAVY,
                grid_axis="x",
            )
            ax.margins(y=0.12)
            fig.tight_layout()
            try:
                st.pyplot(fig, clear_figure=True)
            finally:
                plt.close(fig)
            st.caption("連続値は中央値差、記録率は割合の差です。棒は四分位範囲（0の場合は標準偏差）で標準化しています。")
    with right:
        table = profile.copy()
        table["差"] = table.apply(lambda row: number_text(row["difference"], row["unit"]), axis=1)
        table["95%区間"] = table.apply(lambda row: interval_text(row["interval_low"], row["interval_high"], row["unit"]), axis=1)
        table["有効日数"] = table.apply(lambda row: f"{int(row['high_n'])} / {int(row['other_n'])}", axis=1)
        table["充足率"] = table["coverage_pct"].map(lambda value: f"{float(value):.0f}%")
        st.dataframe(
            table[["label", "comparison_label", "差", "95%区間", "有効日数", "充足率", "evidence_level"]].rename(
                columns={"label": "条件", "comparison_label": "比較方法", "evidence_level": "状態"}
            ),
            width="stretch",
            hide_index=True,
        )


def available_factors(frame: pd.DataFrame) -> list[str]:
    factors: list[str] = []
    for factor_key in FACTOR_LABELS:
        groups, _ = factor_groups(frame, factor_key)
        valid = frame["_analysis_outcome"].notna()
        if (groups.loc[valid] == "A").any() and (groups.loc[valid] == "B").any():
            factors.append(factor_key)
    return factors


def render_similar_days(
    result: dict[str, Any],
    outcome_unit: str,
    next_unit: str,
) -> None:
    pairs = result["pairs"]
    metadata = result["metadata"]
    matching_columns = result["matching_columns"]
    st.caption(
        f"条件A: {metadata['group_a_label']} / 条件B: {metadata['group_b_label']}。"
        + (f"{metadata['exclusion_note']}。" if metadata.get("exclusion_note") else "")
        + "平日・休日をそろえ、利用可能な開始時点の条件が近い日を1対1で対応させます。"
    )
    if not matching_columns:
        st.info("両群に共通してそろう比較条件が不足しているため、似た日比較を作成できません。")
        return
    st.markdown(
        "".join(
            f'<span class="perf-control">{escape(MATCHING_LABELS.get(column, column))}</span>'
            for column in matching_columns
        ),
        unsafe_allow_html=True,
    )
    if pairs.empty:
        st.info("現在の距離基準で対応づけられる日がありません。")
        return

    summary = result["summary"]
    next_summary = result["next_summary"]
    cols = st.columns(4)
    with cols[0]:
        metric_card("対応ペア", f"{len(pairs)} 組", "同じ日は1回だけ使用", BLUE)
    with cols[1]:
        metric_card("成果の対応差中央値", number_text(summary["median_difference"], outcome_unit), "各ペアの条件A − 条件Bの中央値", TEAL)
    with cols[2]:
        metric_card("成果差の95%区間", interval_text(summary["interval_low"], summary["interval_high"], outcome_unit), "ブートストラップ", AMBER)
    with cols[3]:
        metric_card("エビデンス状態", summary["evidence_level"], f"翌日対応差 {number_text(next_summary['median_difference'], next_unit)}", SLATE)

    table = pairs.copy()
    table["_latest_pair_date"] = pd.concat(
        [
            pd.to_datetime(table["condition_a_date"], errors="coerce"),
            pd.to_datetime(table["condition_b_date"], errors="coerce"),
        ],
        axis=1,
    ).max(axis=1)
    table = table.sort_values(
        "_latest_pair_date",
        ascending=False,
        na_position="last",
        kind="stable",
    ).drop(columns="_latest_pair_date")
    table["条件Aの成果"] = table["condition_a_value"].map(lambda value: number_text(value, outcome_unit))
    table["条件Bの成果"] = table["condition_b_value"].map(lambda value: number_text(value, outcome_unit))
    table["成果差"] = table["outcome_difference"].map(lambda value: number_text(value, outcome_unit))
    table["距離"] = table["matching_distance"].map(lambda value: f"{float(value):.2f}")
    display_columns = ["condition_a_date", "condition_b_date", "条件Aの成果", "条件Bの成果", "成果差", "距離"]
    st.dataframe(
        table[display_columns].rename(columns={"condition_a_date": "条件Aの日", "condition_b_date": "条件Bの日"}),
        width="stretch",
        hide_index=True,
    )
    st.caption("距離は比較条件を標準化した近さです。対応づけは交絡を完全に取り除くものではなく、観察上の比較を補助します。")


def evidence_display_table(evidence: pd.DataFrame, outcome_unit: str, next_unit: str) -> pd.DataFrame:
    if evidence.empty:
        return pd.DataFrame()
    table = evidence.copy()
    table["主指標差"] = table["outcome_difference"].map(lambda value: number_text(value, outcome_unit))
    table["95%区間"] = table.apply(
        lambda row: interval_text(row["outcome_interval_low"], row["outcome_interval_high"], outcome_unit), axis=1
    )
    table["翌日指標差"] = table["next_difference"].map(lambda value: number_text(value, next_unit))
    table["主指標日数"] = table.apply(lambda row: f"{int(row['group_a_n'])} / {int(row['group_b_n'])}", axis=1)
    table["翌日指標日数"] = table.apply(lambda row: f"{int(row['next_group_a_n'])} / {int(row['next_group_b_n'])}", axis=1)
    return table[
        ["factor", "comparison", "主指標差", "95%区間", "翌日指標差", "主指標日数", "翌日指標日数", "evidence_level"]
    ].rename(columns={"factor": "要因", "comparison": "比較", "evidence_level": "状態"})


def csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8-sig")


inject_page_css()
page_settings = ab.sidemenu("学習成果が高く、翌日も崩れにくかった条件を横断的に確認します。", settings=PAGE_SETTINGS)

today = app_today()
show_task_metrics = should_show_task_metrics(page_settings)
caffeine_missing_as_zero = should_treat_missing_caffeine_as_zero(page_settings)
with st.sidebar:
    st.subheader("Analysis Control")
    period_label = st.selectbox("対象期間", ["直近30日", "直近90日", "今月", "記録全期間", "任意期間"], index=1)
    if period_label == "任意期間":
        start_date = st.date_input("開始日", value=today - timedelta(days=89), key="performance_start")
        end_date = st.date_input("終了日", value=today, key="performance_end")
    else:
        start_date, end_date = period_range(period_label, today)
        st.caption(f"{start_date.isoformat()} から {end_date.isoformat()} まで")

if end_date < start_date:
    start_date, end_date = end_date, start_date

frame = build_daily_feature_frame(
    start_date, end_date, as_of_date=today,
    caffeine_missing_as_zero=caffeine_missing_as_zero,
)
st.caption(
    f"カフェイン未記録：{'0mg扱い' if caffeine_missing_as_zero else '比較から除外'} "
    f"（未記録から0mg補完 {int(frame['caffeine_imputed_zero'].sum())}日）。元の記録有無は保持し、未来日は補完しません。"
)
if frame.empty:
    st.info("分析できる日付範囲がありません。")
    st.stop()

with st.sidebar:
    weekday_filter = st.selectbox("曜日", ["すべて", "平日", "休日"], key="performance_weekday")
    running_filter = st.selectbox(
        "ランニング記録",
        ["すべて", "記録あり", "記録なし（運動記録あり）", "運動未記録・有無不明"],
        key="performance_running",
    )
    status_order = ["日記記録なし", "カフェイン未明示", "未記録（0mg扱い）", "0mgまたは初期値", "1-100mg", "101-200mg", "201mg以上"]
    available_statuses = [status for status in status_order if (frame["caffeine_status"] == status).any()]
    caffeine_statuses = st.multiselect(
        "カフェイン状態",
        available_statuses,
        default=available_statuses,
        key="performance_caffeine_status",
    )

filter_mask = pd.Series(True, index=frame.index)
if weekday_filter == "平日":
    filter_mask &= ~frame["is_weekend"]
elif weekday_filter == "休日":
    filter_mask &= frame["is_weekend"]
if running_filter == "記録あり":
    filter_mask &= frame["running_recorded"]
elif running_filter == "記録なし（運動記録あり）":
    filter_mask &= frame["running_status_recorded"] & ~frame["running_recorded"]
elif running_filter == "運動未記録・有無不明":
    filter_mask &= ~frame["running_status_recorded"]
filter_mask &= frame["caffeine_status"].isin(caffeine_statuses)

filtered_source = frame.loc[filter_mask]
primary_options = outcome_options(filtered_source, include_task_metrics=show_task_metrics)
condition_options = next_options(filtered_source)
if not primary_options or not condition_options:
    st.info("現在のフィルター条件では、成果と翌日状態を組み合わせて分析できません。")
    st.stop()

primary_labels = [item[0] for item in primary_options]
primary_default = primary_labels.index("アウトプット時間") if "アウトプット時間" in primary_labels else 0
next_labels = [item[0] for item in condition_options]
stored_primary = st.session_state.get("performance_primary")
if stored_primary is not None and stored_primary not in primary_labels:
    st.session_state["performance_primary"] = primary_labels[primary_default]
stored_next = st.session_state.get("performance_next")
if stored_next is not None and stored_next not in next_labels:
    st.session_state["performance_next"] = next_labels[0]

with st.sidebar:
    primary_label = st.selectbox("主指標", primary_labels, index=primary_default, key="performance_primary")
    next_label = st.selectbox("翌日指標", next_labels, index=0, key="performance_next")
    comparison_mode = st.selectbox(
        "比較モード",
        ["生データ", "個人中央値差", "直近28日中央値差"],
        key="performance_mode",
    )
    minimum_days = st.number_input(
        "最低有効日数",
        min_value=1,
        max_value=60,
        value=8,
        step=1,
        key="performance_minimum_days",
    )

primary_label, primary_column, primary_unit = next(item for item in primary_options if item[0] == primary_label)
next_label, next_column, next_unit, next_direction = next(item for item in condition_options if item[0] == next_label)
frame["_analysis_outcome"] = calculate_performance_delta(frame, primary_column, comparison_mode)
analysis = frame.loc[filter_mask].copy()
analysis_label = primary_label if comparison_mode == "生データ" else f"{primary_label}（{comparison_mode}）"

ab.topbar(
    "学習パフォーマンス・条件分析",
    "どのような条件の日に学習成果が高く、その翌日も崩れにくかったかを、睡眠・計画・よい席・カフェイン・運動・学習構成から横断的に確認します。個人記録上の比較として表示します。",
)

st.markdown(
    "".join(
        [
            f'<span class="perf-control">主指標: {escape(analysis_label)}</span>',
            f'<span class="perf-control">翌日指標: {escape(next_label)}</span>',
            f'<span class="perf-control">期間: {start_date.isoformat()}～{end_date.isoformat()}</span>',
            f'<span class="perf-control">最低日数: {int(minimum_days)}日</span>',
        ]
    ),
    unsafe_allow_html=True,
)

numeric_cache = PerformanceResultCache(
    st.session_state,
    frame,
    root=PROJECT_ROOT,
    context={
        "as_of_date": today, "start_date": start_date, "end_date": end_date,
        "caffeine_missing_as_zero": caffeine_missing_as_zero,
        "weekday_filter": weekday_filter, "running_filter": running_filter,
        "caffeine_statuses": tuple(sorted(caffeine_statuses)),
        "primary_column": primary_column, "comparison_mode": comparison_mode,
        "next_column": next_column, "next_direction": next_direction,
        "show_task_metrics": show_task_metrics,
    },
)
profile, high_threshold = numeric_cache.get_or_compute(
    "profile", {"outcome_column": "_analysis_outcome"},
    lambda: summarize_high_performance_profile(analysis, "_analysis_outcome"),
)
evidence = numeric_cache.get_or_compute(
    "evidence", {"outcome_column": "_analysis_outcome", "next_column": next_column},
    lambda: build_evidence_table(analysis, "_analysis_outcome", next_column, minimum_days=1),
)
# This control changes presentation eligibility, not the statistical estimates.
evidence["eligible"] = evidence["effective_days"] >= max(int(minimum_days), 1)
valid_outcome = analysis["_analysis_outcome"].dropna()
paired = analysis.dropna(subset=["_analysis_outcome", next_column])
high_count = int((valid_outcome >= high_threshold).sum()) if high_threshold is not None else 0
if high_threshold is not None and not paired.empty:
    next_baseline = float(paired[next_column].median())
    next_favorable = paired[next_column] >= next_baseline if next_direction > 0 else paired[next_column] <= next_baseline
    sustained_count = int(((paired["_analysis_outcome"] >= high_threshold) & next_favorable).sum())
else:
    sustained_count = 0
other_count = max(len(valid_outcome) - high_count, 0)
board_state = classify_evidence_level(high_count, other_count)
eligible_evidence = evidence.loc[evidence["eligible"] & evidence["outcome_difference"].notna()]
if eligible_evidence.empty:
    max_difference_text = "-"
    max_difference_caption = "最低日数を満たす比較なし"
else:
    largest = eligible_evidence.loc[eligible_evidence["outcome_difference"].abs().idxmax()]
    max_difference_text = number_text(largest["outcome_difference"], primary_unit)
    max_difference_caption = f"{largest['factor']} / {largest['evidence_level']}"

st.subheader("Evidence Board")
board_rows = [st.columns(3), st.columns(3)]
with board_rows[0][0]:
    metric_card("分析対象日数", f"{len(analysis)} 日", "フィルター後の暦日", BLUE)
with board_rows[0][1]:
    metric_card("主指標の有効日数", f"{len(valid_outcome)} 日", f"{primary_label}が記録済み", TEAL)
with board_rows[0][2]:
    metric_card("成果×翌日の有効日数", f"{len(paired)} 日", "両方がそろう日", GREEN)
with board_rows[1][0]:
    metric_card("高パフォーマンス日", f"{high_count} 日", f"主指標の上位25% / {board_state}", NAVY)
with board_rows[1][1]:
    metric_card("持続型パフォーマンス日", f"{sustained_count} 日", "高成果かつ翌日良好", GREEN)
with board_rows[1][2]:
    metric_card("最大観察差", max_difference_text, max_difference_caption, AMBER)

st.subheader("成果 × 翌日コンディション")
st.caption("中央値を基準に、当日の成果と翌日の状態を同時に分類します。高パフォーマンス日の上位25%基準とは別の、分布を見やすくするための四象限です。")
render_matrix(analysis, analysis_label, next_label, next_column, next_direction, primary_column)

st.subheader("高パフォーマンス日の条件プロファイル")
st.caption("主指標の上位25%の日と、それ以外の日を比較します。学習構成の項目は成果の一部である可能性があるため、原因ではなく特徴として読んでください。")
render_profile(profile)

st.subheader("似た日比較")
factor_keys = available_factors(analysis)
if not factor_keys:
    st.info("現在の条件では、2群を作れる比較要因がありません。")
else:
    factor_labels = [FACTOR_LABELS[key] for key in factor_keys]
    default_factor = factor_keys.index("caffeine") if "caffeine" in factor_keys else 0
    stored_factor = st.session_state.get("performance_factor")
    if stored_factor is not None and stored_factor not in factor_labels:
        st.session_state["performance_factor"] = factor_labels[default_factor]
    selected_factor_label = st.selectbox("比較する条件", factor_labels, index=default_factor, key="performance_factor")
    selected_factor = next(key for key in factor_keys if FACTOR_LABELS[key] == selected_factor_label)
    similar_result = numeric_cache.get_or_compute(
        "similar_days",
        {"factor_key": selected_factor, "outcome_column": "_analysis_outcome",
         "next_condition_column": next_column, "maximum_distance": 1.5},
        lambda: find_comparable_days(
            analysis, selected_factor, "_analysis_outcome",
            next_condition_column=next_column, maximum_distance=1.5,
        ),
    )
    render_similar_days(similar_result, primary_unit, next_unit)

st.subheader("Evidence Table")
st.caption(
    "主指標差と翌日指標差はいずれも「条件A − 条件B」です。状態は両指標で少ない側の有効日数から判定します。"
    "「よい席」の比較はYes対Noとし、So-so・未記録は両群から除外します。"
    "ランニングの成果比較は運動記録から有無を確認できる日だけを使い、未記録を「なし」には含めません。"
    "一方「ランニング記録率」は記録が存在する割合で、運動しなかった割合ではありません。"
)
display_evidence = evidence_display_table(evidence, primary_unit, next_unit)
if display_evidence.empty:
    st.info("比較結果を作成できません。")
else:
    st.dataframe(display_evidence, width="stretch", hide_index=True)

st.markdown("#### 次に確かめる仮説")
ranked = evidence.loc[evidence["eligible"] & evidence["standardized_outcome_difference"].notna()].copy()
ranked = ranked.reindex(ranked["standardized_outcome_difference"].abs().sort_values(ascending=False).index)
if ranked.empty:
    st.markdown(
        '<div class="perf-note">最低有効日数を満たす比較がまだありません。記録を増やし、同じ条件の比較日を確保する段階です。</div>',
        unsafe_allow_html=True,
    )
else:
    for _, row in ranked.head(2).iterrows():
        direction = "高く" if float(row["outcome_difference"]) > 0 else "低く"
        text = (
            f"{row['factor']}では、{row['comparison']}の順で比べると、"
            f"{primary_label}の中央値が{abs(float(row['outcome_difference'])):.2f}{primary_unit}{direction}記録されています。"
            f"状態は{row['evidence_level']}で、次の比較候補として扱います。"
        )
        st.markdown(f'<div class="perf-note">{escape(text)}</div>', unsafe_allow_html=True)

with st.expander("日別明細・CSV", expanded=False):
    details = analysis.sort_values(
        "date",
        ascending=False,
        na_position="last",
        kind="stable",
    ).copy()
    primary_detail_label = f"主指標：{analysis_label}"
    next_detail_label = f"翌日指標：{next_label}"
    details["日付"] = details["date"].dt.strftime("%Y-%m-%d")
    details[primary_detail_label] = details["_analysis_outcome"]
    details[next_detail_label] = details[next_column]
    details["学習記録"] = details["study_recorded"].map({True: "あり", False: "なし"})
    details["健康記録"] = details["health_recorded"].map({True: "あり", False: "なし"})
    details["計画記録"] = details["plan_recorded"].map({True: "あり", False: "なし"})
    details["ランニング記録"] = details["running_recorded"].map({True: "あり", False: "なし"})
    details.loc[~details["running_status_recorded"], "ランニング記録"] = "未記録・有無不明"
    details["よい席"] = details["good_seat_status"].map(
        {"yes": "Yes", "so_so": "So-so", "no": "No"}
    ).fillna("未記録")
    detail_columns = [
        "日付",
        primary_detail_label,
        next_detail_label,
        "caffeine_status",
        "study_hours",
        "output_hours",
        "sleep_hours",
        "morning_sleepiness",
        "planned_hours",
        "学習記録",
        "健康記録",
        "計画記録",
        "ランニング記録",
        "よい席",
    ]
    detail_table = details[detail_columns].rename(
        columns={
            "caffeine_status": "カフェイン状態",
            "study_hours": "総勉強時間",
            "output_hours": "アウトプット時間",
            "sleep_hours": "前夜睡眠時間",
            "morning_sleepiness": "朝の眠気",
            "planned_hours": "予定学習時間",
        }
    )
    st.dataframe(detail_table, width="stretch", hide_index=True)
    download_cols = st.columns(2)
    with download_cols[0]:
        st.download_button(
            "日別明細CSV",
            data=csv_bytes(detail_table),
            file_name="performance_daily_details.csv",
            mime="text/csv",
            width="stretch", on_click="ignore")
    with download_cols[1]:
        st.download_button(
            "Evidence Table CSV",
            data=csv_bytes(evidence),
            file_name="performance_evidence_table.csv",
            mime="text/csv",
            width="stretch", on_click="ignore")

st.divider()
st.markdown("#### 要因別の詳細分析")
st.caption("このページでカフェインとの観察差が気になった場合は、専門ページで成分残存量、タイムライン、散布図、相関行列、ランニングとの詳細を確認できます。")
ab.product_page_link("pages/16_カフェイン・運動分析.py", label="成分リズム・コンディション分析を詳しく見る")

st.caption(
    "翌日コンディションバランス = 標準化した翌日集中しやすさ、−翌日疲労感、−翌朝眠気、−翌日ストレスの平均（2項目以上がある日）。"
    "相関や群間差は因果関係を示しません。"
)
