from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.app_clock import app_now, app_today
from app_core.daily_analysis_service import analysis_context_window
from app_core.caffeine_timing_service import (
    build_session_timing_frame,
    parse_record_datetime,
    with_caffeine_recording_status,
)
from app_core.diary_service import normalize_diary_entry
from app_core.json_store import load_json_safe
from app_core.performance_analysis_service import (
    _health_summary,
    _exercise_summary,
    _plan_summary,
    summarize_study_records,
    load_study_analysis_context,
    caffeine_daily_measurement,
)
from app_core.plot_style import (
    chart_series_style,
    configure_japanese_font,
    plot_rounded_category_bars,
    plot_styled_line,
    plot_time_bars,
    style_chart_axis,
    style_date_axis,
    style_legend,
)
from app_core.settings_service import should_show_task_metrics, should_treat_missing_caffeine_as_zero
from app_core.substance_residual_service import (
    build_substance_residual_curve,
    load_recent_substance_records,
    required_lookback_days,
    substance_residual_total_at,
)
from app_core.substance_service import format_mg, load_substance_profiles
from app_core.study_category_service import load_study_category_catalog, study_category_names, other_category_name


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

ROOT = Path(__file__).resolve().parents[1]
THEME = ab.theme_palette(settings=PAGE_SETTINGS)
BG = THEME["background"]
PANEL = THEME["panel"]
PANEL_2 = THEME["panel_2"]
TEXT = THEME["ink"]
MUTED = THEME["muted"]
CYAN = THEME["cyan"]
GREEN = THEME["green"]
AMBER = THEME["amber"]
RED = THEME["red"]
PURPLE = THEME["purple"]
_CATEGORY_CATALOG = load_study_category_catalog(ROOT)
STUDY_CATEGORIES = list(dict.fromkeys((*study_category_names(_CATEGORY_CATALOG, include_inactive=True), other_category_name(_CATEGORY_CATALOG))))
MIN_CORR_PAIRS = 4
TIMING_BAND_ORDER = ["0-30分", "31-90分", "91-180分", "181-360分", "361分以上", "時刻不明のみ", "摂取前・記録なし"]


st.set_page_config(page_title="成分リズム・コンディション分析", layout="wide")
configure_japanese_font()
ab.inject_css(settings=PAGE_SETTINGS)


def inject_dark_css() -> None:
    dark = ab.is_dark_theme()
    metric_background = (
        "linear-gradient(145deg, rgba(16,36,58,0.82), rgba(6,17,31,0.84))"
        if dark
        else "linear-gradient(145deg, #FFFFFF, #F5F8FB)"
    )
    css = """
        <style>
        .caff-metric {
            border: 1px solid var(--ab-line);
            border-radius: 22px;
            padding: 18px 18px 16px;
            min-height: 132px;
            background: __METRIC_BACKGROUND__;
            box-shadow: 0 16px 40px var(--ab-shadow);
        }
        .caff-metric-label {
            color: var(--ab-muted);
            font-size: 0.82rem;
            letter-spacing: 0.05em;
        }
        .caff-metric-value {
            color: var(--accent);
            font-size: 1.85rem;
            font-weight: 800;
            margin-top: 8px;
        }
        .caff-metric-caption {
            color: var(--ab-muted);
            font-size: 0.82rem;
            margin-top: 8px;
        }
        .caff-card {
            border: 1px solid var(--ab-line);
            border-radius: 20px;
            background: var(--ab-card);
            padding: 16px 18px;
            color: var(--ab-ink);
            margin: 8px 0;
        }
        .caff-card strong {
            color: var(--ab-primary);
        }
        div[data-testid="stDataFrame"] {
            border: 1px solid var(--ab-line);
            border-radius: 18px;
            overflow: hidden;
        }
        </style>
    """
    st.markdown(
        css.replace("__METRIC_BACKGROUND__", metric_background),
        unsafe_allow_html=True,
    )


def period_range(label: str, today: date) -> tuple[date, date]:
    if label == "直近7日":
        return today - timedelta(days=6), today
    if label == "直近14日":
        return today - timedelta(days=13), today
    if label == "直近90日":
        return today - timedelta(days=89), today
    if label == "今月":
        return date(today.year, today.month, 1), today
    return today - timedelta(days=29), today


def date_range(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def avg(values: list[float]) -> float:
    clean = [value for value in values if pd.notna(value)]
    return round(sum(clean) / len(clean), 3) if clean else 0.0


def normalize_study_category(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in STUDY_CATEGORIES else "その他"


def normalize_io_type(value: Any) -> str:
    text = str(value or "").strip()
    if text == "インプット":
        return "input"
    if text == "アウトプット":
        return "output"
    if text == "両方":
        return "both"
    return "unset"


def task_is_done(task: dict[str, Any]) -> bool:
    return str(task.get("status") or "").strip().lower() == "done"


def seconds_label(seconds: int | float) -> str:
    if pd.isna(seconds):
        return "未記録"
    total = max(int(seconds or 0), 0)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{hours}時間{minutes:02d}分"


def minutes_label(minutes: int | float) -> str:
    if pd.isna(minutes):
        return "未記録"
    total = max(int(minutes or 0), 0)
    hours, mins = divmod(total, 60)
    return f"{hours}時間{mins:02d}分" if hours else f"{mins}分"


def caffeine_band(value: Any) -> str:
    if pd.isna(value):
        return "未記録"
    mg = num(value)
    if mg <= 0:
        return "0 mg"
    if mg <= 100:
        return "1-100 mg"
    if mg <= 200:
        return "101-200 mg"
    return "201 mg以上"


def metric_card(label: str, value: Any, caption: str, accent: str = CYAN) -> None:
    st.markdown(
        f"""
        <div class="caff-metric" style="--accent:{escape(accent, quote=True)};">
          <div class="caff-metric-label">{escape(str(label))}</div>
          <div class="caff-metric-value">{escape(str(value))}</div>
          <div class="caff-metric-caption">{escape(str(caption))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def substance_metric_card(label: str, value: str, caption: str, color: str) -> None:
    st.markdown(
        f"""
        <div class="caff-metric" style="--accent:{escape(TEXT, quote=True)}; border-left:4px solid {escape(color, quote=True)};">
          <div class="caff-metric-label">{escape(label)}</div>
          <div class="caff-metric-value">{escape(value)}</div>
          <div class="caff-metric-caption">{escape(caption)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def insight_card(text: str) -> None:
    st.markdown(f'<div class="caff-card">{escape(text)}</div>', unsafe_allow_html=True)


def daily_path(folder: str, target: date) -> Path:
    return ROOT / folder / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"


def load_study_context(targets: list[date], observed_through: date) -> tuple[dict[date, list[dict[str, Any]]], set[date]]:
    return load_study_analysis_context(ROOT, targets, observed_through)


def load_running_summary(target: date, *, observed: bool = True) -> dict[str, Any]:
    result = _exercise_summary(daily_path("exercise_logs", target), observed=observed)
    duration, distance = result["running_duration_min"], result["running_distance_km"]
    result["running_pace_min_per_km"] = duration / distance if pd.notna(duration) and distance > 0 else np.nan
    return result


def load_plan_summary(target: date, *, observed: bool = True) -> dict[str, Any]:
    result = _plan_summary(daily_path("daily_plans", target), observed=observed)
    result["planned_minutes_total"] = result["planned_hours"] * 60
    return result


def datetime_label(value: Any) -> str:
    if value is None or pd.isna(value):
        return "-"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "-"


def caffeine_timing_summary(diary: dict[str, Any], target: date) -> dict[str, Any]:
    records: list[tuple[datetime, dict[str, Any]]] = []
    for item in diary.get("caffeine_records", []):
        if not isinstance(item, dict):
            continue
        taken_at = parse_record_datetime(item.get("taken_at"))
        if taken_at is None or taken_at.date() != target:
            continue
        records.append((taken_at, item))
    records.sort(key=lambda pair: pair[0])
    timed_mg = round(sum(max(0, num(item.get("amount_mg"))) for _, item in records), 6)
    first_hour = records[0][0].hour + records[0][0].minute / 60 if records else None
    last_hour = records[-1][0].hour + records[-1][0].minute / 60 if records else None
    after_18_mg = round(
        sum(max(0, num(item.get("amount_mg"))) for taken_at, item in records if taken_at.hour >= 18),
        6,
    )
    return {
        "caffeine_record_count": len(records),
        "timed_caffeine_mg": timed_mg,
        "caffeine_untimed_mg": round(max(0, num(diary.get("caffeine_untimed_mg"))), 6),
        "first_caffeine_hour": first_hour,
        "last_caffeine_hour": last_hour,
        "caffeine_after_18_mg": after_18_mg,
    }


def load_daily_row(target: date, *, observed: bool, study: dict[str, Any], diary_cache: dict[date, dict[str, Any]], profiles: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    path = daily_path("diary_entries", target)
    has_diary = observed and path.exists()
    raw = load_json_safe(path, {}) if has_diary else {}
    diary = normalize_diary_entry(raw, target, profiles=profiles) if has_diary else {}
    if isinstance(raw, list):
        raw = raw[-1] if raw else {}
    raw = raw if isinstance(raw, dict) else {}
    caffeine_value = caffeine_daily_measurement(raw, target)
    caffeine_field_recorded = has_diary and bool(np.isfinite(caffeine_value))
    diary_cache[target] = diary
    health = _health_summary(daily_path("health_logs", target), observed=observed)
    row = {
        "date": target,
        "has_diary_log": has_diary,
        "is_observed_day": observed,
        "caffeine_field_recorded": caffeine_field_recorded,
        "caffeine_mg": caffeine_value,
        **health,
        "sleep_duration_min": health["sleep_hours"] * 60,
        **study,
        "study_duration_sec": study["study_hours"] * 3600,
    }
    row.update(caffeine_timing_summary(diary, target))
    row.update(load_running_summary(target, observed=observed))
    row.update(load_plan_summary(target, observed=observed))
    return row


def build_dashboard_frame(
    start_date: date, end_date: date, *, as_of_date: date | None = None,
    diary_cache: dict[date, dict[str, Any]] | None = None,
    study_cache: dict[date, list[dict[str, Any]]] | None = None,
    caffeine_missing_as_zero: bool = False,
) -> pd.DataFrame:
    from app_core.study_category_service import load_study_category_catalog
    category_catalog = load_study_category_catalog(ROOT)
    start_date, end_date, targets = analysis_context_window(start_date, end_date)
    observed_through = as_of_date or app_today()
    diaries = diary_cache if diary_cache is not None else {}
    studies, study_recorded = load_study_context(targets, observed_through)
    profiles = {str(item["substance_id"]): item for item in load_substance_profiles(include_inactive=True)}
    if study_cache is not None:
        study_cache.update(studies)
    rows = [load_daily_row(
        target, observed=target <= observed_through,
        study=summarize_study_records(studies[target], recorded=target in study_recorded, category_catalog=category_catalog),
        diary_cache=diaries, profiles=profiles,
    ) for target in targets]
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = with_caffeine_recording_status(df, missing_as_zero=caffeine_missing_as_zero)
    df["caffeine_band"] = df["caffeine_mg"].apply(caffeine_band)
    df["has_running"] = df["running_count"] > 0
    df["running_state"] = df["has_running"].map({True: "ランニングあり", False: "ランニングなし"})
    df.loc[~df["running_status_recorded"], "running_state"] = "未記録"
    df["next_sleep_hours"] = df["sleep_hours"].shift(-1)
    df["next_morning_sleepiness"] = df["morning_sleepiness"].shift(-1)
    df["next_focus_score"] = df["focus_score"].shift(-1)
    df["next_fatigue"] = df["fatigue"].shift(-1)
    df["next_stress"] = df["stress"].shift(-1)
    df["previous_caffeine_mg"] = df["caffeine_mg"].shift(1)
    df["previous_night_study_hours"] = df["night_study_hours"].shift(1)
    df["previous_running_intensity"] = df["running_avg_intensity"].shift(1)
    df["study_plan_gap_hours"] = df["study_hours"] - df["planned_hours"]
    return df.loc[df["date"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))].reset_index(drop=True)


def build_caffeine_event_frame(start_date: date, end_date: date, *, diary_cache: dict[date, dict[str, Any]] | None = None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    observed_through = app_today() if diary_cache is None else None
    profiles = ({str(item["substance_id"]): item for item in load_substance_profiles(include_inactive=True)}
                if diary_cache is None else None)
    for target in date_range(start_date, end_date):
        if diary_cache is not None:
            diary = diary_cache.get(target, {})
        else:
            path = daily_path("diary_entries", target)
            if target > observed_through or not path.exists():
                continue
            diary = normalize_diary_entry(load_json_safe(path, {}), target, profiles=profiles)
        for item in diary.get("caffeine_records", []):
            if not isinstance(item, dict):
                continue
            taken_at = parse_record_datetime(item.get("taken_at"))
            if taken_at is None or not start_date <= taken_at.date() <= end_date:
                continue
            rows.append(
                {
                    "摂取日時": taken_at,
                    "摂取量mg": round(max(0, num(item.get("amount_mg"))), 6),
                    "種類": str(item.get("label") or ""),
                    "記録元": str(item.get("source") or ""),
                    "メモ": str(item.get("memo") or ""),
                }
            )
    return pd.DataFrame(rows).sort_values("摂取日時") if rows else pd.DataFrame()


def dark_axes(ax: Any) -> None:
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=MUTED)
    ax.xaxis.label.set_color(TEXT)
    ax.yaxis.label.set_color(TEXT)
    ax.title.set_color(TEXT)
    ax.grid(color=THEME["grid"], alpha=0.55, linewidth=0.8)
    for spine in ax.spines.values():
        spine.set_color(THEME["line"])


@st.fragment
def render_substance_residual(analysis_day: date) -> None:
    # Re-read time and profiles on every local interaction; never freeze the
    # current-time marker in the arguments of a fragment rerun.
    reference_time = app_now().replace(microsecond=0)
    if reference_time.date() != analysis_day:
        st.rerun()
    all_profiles = load_substance_profiles(include_inactive=True)
    profile_lookup = {str(item["substance_id"]): item for item in all_profiles}
    active_ids = [str(item["substance_id"]) for item in all_profiles if item.get("is_active", True)]

    st.subheader("成分別・推定残存量")
    st.caption(
        "設定した単純半減期モデルで、各成分の残存推移を同じ時間軸に表示します。"
        "各グラフの面塗りと色はユーザー設定、計算条件は各摂取記録に保存されたモデル値を使います。"
    )

    control_cols = st.columns([2.1, 1.2, 1, 1])
    selected_ids = control_cols[0].multiselect(
        "表示成分",
        options=[str(item["substance_id"]) for item in all_profiles],
        default=active_ids,
        format_func=lambda item_id: (
            str(profile_lookup[item_id]["name"])
            if profile_lookup[item_id].get("is_active", True)
            else f"{profile_lookup[item_id]['name']}（アーカイブ）"
        ),
        key="substance_residual_selected_ids",
    )
    comparison_mode = control_cols[1].radio(
        "比較図の縦軸",
        ["残存率（%）", "残存量（mg）"],
        horizontal=False,
        key="substance_residual_comparison_mode",
    )
    display_hours = control_cols[2].selectbox(
        "表示幅",
        [24, 48, 72],
        index=1,
        format_func=lambda value: f"{value}時間",
        key="substance_residual_display_hours",
    )
    layout_label = control_cols[3].radio(
        "成分別配置",
        ["2列", "1列"],
        key="substance_residual_layout",
    )
    if not selected_ids:
        st.info("表示する成分を1つ以上選択してください。")
        return

    selected_profiles = [profile_lookup[item_id] for item_id in selected_ids]
    history_lookback_days = required_lookback_days(selected_profiles, past_view_days=6)
    records, untimed_by_substance = load_recent_substance_records(
        reference_time,
        lookback_days=history_lookback_days,
        profiles=profile_lookup,
    )
    slider_reference = reference_time.replace(second=0, microsecond=0)
    view_offset_hours = st.slider(
        "グラフ表示位置（現在からの時間）",
        min_value=-6 * 24,
        max_value=0,
        value=0,
        step=1,
        format="%d時間",
        key="substance_residual_view_offset_hours",
        help="右端の0時間が現在です。左へ動かすと過去6日間を確認できます。",
    )
    view_center = slider_reference + timedelta(hours=view_offset_hours)
    half_span = timedelta(hours=int(display_hours) / 2)
    window_start = view_center - half_span
    window_end = view_center + half_span

    series: dict[str, dict[str, Any]] = {}
    for profile in selected_profiles:
        substance_id = str(profile["substance_id"])
        profile_records = [item for item in records if item.get("substance_id") == substance_id]
        curve = build_substance_residual_curve(
            profile_records,
            window_start,
            window_end,
            profile=profile,
        )
        series[substance_id] = {
            "profile": profile,
            "records": profile_records,
            "times": [point[0] for point in curve],
            "values": np.asarray([point[1] for point in curve], dtype=float),
            "center_value": substance_residual_total_at(profile_records, view_center, profile=profile),
        }

    st.caption(
        f"表示中心: {view_center:%Y-%m-%d %H:%M} / "
        f"表示範囲: {window_start:%Y-%m-%d %H:%M} ～ {window_end:%Y-%m-%d %H:%M}"
    )
    for start in range(0, len(selected_ids), 4):
        row_ids = selected_ids[start : start + 4]
        metric_cols = st.columns(len(row_ids))
        for column, substance_id in zip(metric_cols, row_ids):
            item = series[substance_id]
            profile = item["profile"]
            with column:
                substance_metric_card(
                    str(profile["name"]),
                    f"{format_mg(item['center_value'])} mg",
                    f"表示中心 / 新規記録の半減期 {float(profile['half_life_hours']):g}時間",
                    str(profile["color"]),
                )

    st.markdown("#### 選択成分の重ね合わせ")
    fig, ax = plt.subplots(figsize=(11, 4.8))
    fig.patch.set_facecolor(BG)
    dark_axes(ax)
    for index, substance_id in enumerate(selected_ids):
        item = series[substance_id]
        profile = item["profile"]
        values = item["values"]
        if comparison_mode == "残存率（%）":
            maximum = float(np.nanmax(values)) if values.size else 0.0
            display_values = values / maximum * 100 if maximum > 0 else np.zeros_like(values)
        else:
            display_values = values
        style = chart_series_style(THEME, index)
        ax.plot(
            item["times"],
            display_values,
            color=str(profile["color"]),
            linestyle=style["linestyle"],
            linewidth=2.3,
            label=str(profile["name"]),
            zorder=3,
        )
        if profile.get("fill_enabled", True):
            ax.fill_between(item["times"], display_values, color=str(profile["color"]), alpha=0.09, zorder=1)
        visible_records = [
            record
            for record in item["records"]
            if window_start <= record["taken_at"] <= min(window_end, reference_time)
        ]
        if visible_records:
            intake_times = [record["taken_at"] for record in visible_records]
            intake_values = np.asarray(
                [substance_residual_total_at(item["records"], point, profile=profile) for point in intake_times],
                dtype=float,
            )
            if comparison_mode == "残存率（%）":
                maximum = float(np.nanmax(values)) if values.size else 0.0
                intake_values = intake_values / maximum * 100 if maximum > 0 else np.zeros_like(intake_values)
            ax.scatter(
                intake_times,
                intake_values,
                color=str(profile["color"]),
                edgecolor=BG,
                linewidth=0.8,
                s=42,
                zorder=4,
                marker="v",
            )
    if window_start <= reference_time <= window_end:
        ax.axvline(reference_time, color=AMBER, linewidth=1.6, linestyle="--", label="現在", zorder=2)
    ax.set_xlim(window_start, window_end)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("日時")
    ylabel = "表示範囲内の成分別最大値に対する残存率 (%)" if comparison_mode == "残存率（%）" else "推定残存量 (mg)"
    style_chart_axis(ax, THEME, ylabel=ylabel, grid_axis="y")
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d\n%H:%M"))
    style_legend(ax, location="upper right", columns=min(3, len(selected_ids) + 1))
    fig.tight_layout()
    st.pyplot(fig, width="stretch")
    plt.close(fig)
    if comparison_mode == "残存率（%）":
        st.caption("残存率は、表示範囲内における各成分自身の最大推定量を100%として比較しています。成分間のmg量は合算していません。")

    st.markdown("#### 成分別の推定残存量（mg）")
    panel_columns = min(2, len(selected_ids)) if layout_label == "2列" else 1
    panel_rows = int(np.ceil(len(selected_ids) / panel_columns))
    panel_fig, panel_axes = plt.subplots(
        panel_rows,
        panel_columns,
        figsize=(11, 3.35 * panel_rows),
        sharex=True,
        squeeze=False,
        constrained_layout=True,
    )
    panel_fig.patch.set_facecolor(BG)
    flat_axes = panel_axes.ravel()
    for index, (ax_panel, substance_id) in enumerate(zip(flat_axes, selected_ids)):
        item = series[substance_id]
        profile = item["profile"]
        style = chart_series_style(THEME, index)
        dark_axes(ax_panel)
        ax_panel.plot(
            item["times"],
            item["values"],
            color=str(profile["color"]),
            linestyle=style["linestyle"],
            linewidth=2.25,
            zorder=3,
        )
        if profile.get("fill_enabled", True):
            ax_panel.fill_between(
                item["times"],
                item["values"],
                color=str(profile["color"]),
                alpha=0.18,
                zorder=1,
            )
        visible_records = [
            record
            for record in item["records"]
            if window_start <= record["taken_at"] <= min(window_end, reference_time)
        ]
        if visible_records:
            intake_times = [record["taken_at"] for record in visible_records]
            intake_values = [
                substance_residual_total_at(item["records"], point, profile=profile)
                for point in intake_times
            ]
            ax_panel.scatter(
                intake_times,
                intake_values,
                color=str(profile["color"]),
                edgecolor=BG,
                linewidth=0.8,
                s=38,
                zorder=4,
                marker="v",
            )
        if window_start <= reference_time <= window_end:
            ax_panel.axvline(reference_time, color=AMBER, linewidth=1.2, linestyle="--", zorder=2)
        ax_panel.set_xlim(window_start, window_end)
        ax_panel.set_ylim(bottom=0)
        style_chart_axis(
            ax_panel,
            THEME,
            title=f"{profile['name']}  |  表示中心 {format_mg(item['center_value'])} mg",
            title_color=TEXT,
            ylabel="mg",
            grid_axis="y",
        )
        ax_panel.text(
            0.01,
            0.95,
            f"新規記録モデル: 半減期 {float(profile['half_life_hours']):g}時間 / ピーク {int(profile['peak_delay_minutes'])}分",
            transform=ax_panel.transAxes,
            ha="left",
            va="top",
            fontsize=8,
            color=MUTED,
        )
        ax_panel.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=7))
        ax_panel.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d\n%H:%M"))
    for ax_panel in flat_axes[len(selected_ids) :]:
        ax_panel.set_visible(False)
    st.pyplot(panel_fig, width="stretch")
    plt.close(panel_fig)

    selected_records = [item for item in records if item.get("substance_id") in selected_ids]
    if not selected_records:
        st.info(f"過去{history_lookback_days}日間に、選択した成分の時刻付き記録はありません。")
    untimed_labels = [
        f"{profile_lookup[item_id]['name']} {format_mg(untimed_by_substance[item_id])} mg"
        for item_id in selected_ids
        if untimed_by_substance.get(item_id, 0) > 0
    ]
    if untimed_labels:
        st.warning(
            "時刻未設定のため残存推定に含めていない記録: " + " / ".join(untimed_labels)
        )
    st.caption(
        "各摂取はピークまで線形に増加し、その後は「摂取量 × 0.5 ^（ピーク後の経過時間 ÷ 半減期）」で減衰し、"
        "記録時に保存した打切時間以降を0 mgとして描画します。"
    )


def render_timeline(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("表示できるデータがまだありません。")
        return
    fig, axes = plt.subplots(2, 1, figsize=(11, 6.8), sharex=True)
    fig.patch.set_facecolor(BG)
    ax_caff = axes[0]
    dark_axes(ax_caff)
    caffeine_values = df["caffeine_mg"].to_numpy(dtype=float)
    recorded_indices = np.flatnonzero(np.isfinite(caffeine_values))
    highlight_indices = (
        sorted({int(recorded_indices[-1]), int(recorded_indices[caffeine_values[recorded_indices].argmax()])})
        if len(recorded_indices) else []
    )
    caffeine_bars = plot_time_bars(
        ax_caff,
        df["date"].to_numpy(),
        caffeine_values,
        palette=THEME,
        color=CYAN,
        alpha=0.58,
        accent_color=PURPLE,
        highlight_indices=highlight_indices,
        label="カフェイン",
    )
    style_chart_axis(
        ax_caff,
        THEME,
        title="カフェイン量と勉強時間",
        ylabel="カフェイン mg",
        grid_axis="y",
    )

    ax_perf = ax_caff.twinx()
    dark_axes(ax_perf)
    study_line = plot_styled_line(
        ax_perf,
        df["date"].to_numpy(),
        df["study_hours"].to_numpy(dtype=float),
        palette=THEME,
        color=GREEN,
        style_index=2,
        label="勉強時間",
        linewidth=2.3,
    )
    style_chart_axis(ax_perf, THEME, ylabel="勉強時間", grid_axis="x")
    ax_perf.grid(False)
    ax_perf.tick_params(axis="y", colors=GREEN)
    legend = ax_caff.legend(
        [caffeine_bars, study_line],
        ["カフェイン", "勉強時間"],
        frameon=False,
        loc="upper left",
        ncol=2,
        fontsize=8.5,
    )
    for text in legend.get_texts():
        text.set_color(TEXT)

    dark_axes(axes[1])
    plot_styled_line(
        axes[1],
        df["date"].to_numpy(),
        df["running_distance_km"].to_numpy(dtype=float),
        palette=THEME,
        color=AMBER,
        style_index=3,
        label="ランニング距離",
    )
    style_chart_axis(axes[1], THEME, title="ランニング距離", ylabel="km", grid_axis="y")
    axes[1].set_ylim(bottom=0)
    style_legend(axes[1])
    axes[0].tick_params(axis="x", labelbottom=False)
    style_date_axis(axes[1])
    fig.tight_layout(h_pad=1.25)
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)


def render_band_compare(df: pd.DataFrame) -> None:
    df = df.loc[df["has_caffeine_log"]]
    order = ["0 mg", "1-100 mg", "101-200 mg", "201 mg以上"]
    summary = (
        df.groupby("caffeine_band", observed=False)
        .agg(
            study_hours=("study_hours", "mean"),
            study_n=("study_hours", "count"),
            sleep_hours=("sleep_hours", "mean"),
            next_sleepiness=("next_morning_sleepiness", "mean"),
            sleepiness_n=("next_morning_sleepiness", "count"),
            focus_score=("focus_score", "mean"),
            focus_n=("focus_score", "count"),
        )
        .reindex(order)
    )
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    fig.patch.set_facecolor(BG)
    for ax in axes:
        dark_axes(ax)
    band_colors = (
        ["#29445D", "#27708E", "#39D5FF", "#9DA7FF"]
        if THEME["mode"] == "dark"
        else ["#CFE8F3", "#86C5DA", "#3292B6", "#075C9F"]
    )
    study_labels = [
        f"{band}\nn={int(summary.loc[band, 'study_n']) if pd.notna(summary.loc[band, 'study_n']) else 0}"
        for band in order
    ]
    plot_rounded_category_bars(
        axes[0],
        study_labels,
        summary["study_hours"].to_numpy(dtype=float),
        palette=THEME,
        colors=band_colors,
        orientation="vertical",
        linewidth=28,
        formatter=lambda value: f"{value:.1f}h",
    )
    overall_study = pd.to_numeric(df["study_hours"], errors="coerce").mean()
    style_chart_axis(
        axes[0],
        THEME,
        title="カフェイン量別の平均勉強時間",
        ylabel="時間",
        grid_axis="y",
        reference_value=float(overall_study) if pd.notna(overall_study) else None,
    )
    axes[0].set_ylim(bottom=0)
    axes[0].margins(y=0.14)

    sleepiness_labels = [
        f"{band}\nn={int(summary.loc[band, 'sleepiness_n']) if pd.notna(summary.loc[band, 'sleepiness_n']) else 0}"
        for band in order
    ]
    plot_rounded_category_bars(
        axes[1],
        sleepiness_labels,
        summary["next_sleepiness"].to_numpy(dtype=float),
        palette=THEME,
        colors=PURPLE,
        orientation="vertical",
        linewidth=25,
        formatter=lambda value: f"{value:.1f}",
    )
    focus_line = plot_styled_line(
        axes[1],
        list(range(len(order))),
        summary["focus_score"].to_numpy(dtype=float),
        palette=THEME,
        color=CYAN,
        style_index=0,
        label="集中しやすさ",
    )
    sleepiness_proxy = axes[1].plot([], [], color=PURPLE, linewidth=10, solid_capstyle="round", label="翌朝眠気")[0]
    style_chart_axis(
        axes[1],
        THEME,
        title="翌朝眠気と集中しやすさ",
        ylabel="スコア",
        grid_axis="y",
        score_range=True,
    )
    legend = axes[1].legend(
        [sleepiness_proxy, focus_line],
        ["翌朝眠気", "集中しやすさ"],
        frameon=False,
        loc="upper left",
        fontsize=8.5,
    )
    for legend_text in legend.get_texts():
        legend_text.set_color(TEXT)
    fig.tight_layout()
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)


def render_running_scatter(df: pd.DataFrame) -> None:
    data = df[df["has_running"] & df["has_caffeine_log"]].copy()
    data = data.dropna(subset=["caffeine_mg", "running_avg_intensity", "running_distance_km", "study_hours"])
    if data.empty:
        st.info("散布図に必要なカフェイン・ランニングの距離/きつさ・勉強時間がそろう日がありません。欠測値は0で補いません。")
        return
    st.caption(f"散布図の有効n={len(data)}日（距離・きつさ・勉強時間がそろう日のみ）")
    fig, ax = plt.subplots(figsize=(10, 4.5))
    fig.patch.set_facecolor(BG)
    dark_axes(ax)
    sizes = (data["running_distance_km"].clip(lower=0.2) * 90).clip(40, 460)
    scatter = ax.scatter(
        data["caffeine_mg"],
        data["running_avg_intensity"],
        s=sizes,
        c=data["study_hours"],
        cmap="viridis",
        alpha=0.82,
        edgecolors="white",
        linewidths=0.5,
    )
    ax.set_title("カフェイン量 × ランニング主観的きつさ", loc="left", fontsize=12)
    ax.set_xlabel("カフェイン mg")
    ax.set_ylabel("主観的きつさ 1-10")
    cbar = fig.colorbar(scatter, ax=ax)
    cbar.set_label("当日の勉強時間")
    cbar.ax.yaxis.label.set_color(TEXT)
    cbar.ax.tick_params(colors=TEXT)
    cbar.outline.set_edgecolor(THEME["line"])
    fig.tight_layout()
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)


def render_group_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    df = df.loc[df["has_caffeine_log"]]
    if df.empty:
        return pd.DataFrame()
    return grouped_metric_summary(df, ["caffeine_band", "running_state"], {
        "平均カフェインmg": "caffeine_mg", "平均勉強時間": "study_hours",
        "平均夜勉強時間": "night_study_hours", "平均睡眠時間": "sleep_hours",
        "平均集中しやすさ": "focus_score", "平均ラン距離km": "running_distance_km",
        "平均きつさ": "running_avg_intensity", "平均運動後疲労": "running_avg_fatigue_after",
    }).rename(columns={"caffeine_band": "カフェイン帯", "running_state": "ランニング"})


def grouped_metric_summary(df: pd.DataFrame, by: list[str], metrics: dict[str, str]) -> pd.DataFrame:
    """Keep each outcome's denominator visible; an absent mean stays missing."""
    aggregations = {"日数": ("date", "count")}
    for label, column in metrics.items():
        aggregations[label] = (column, "mean")
        aggregations[f"{label} 有効n"] = (column, "count")
    result = df.groupby(by, observed=False).agg(**aggregations).reset_index()
    for label in metrics:
        result[label] = result[label].round(2)
    return result


def render_caffeine_condition_scatter(df: pd.DataFrame) -> None:
    targets = [
        ("fatigue", "疲労感", RED),
        ("stress", "ストレス", PURPLE),
        ("focus_score", "集中しやすさ", CYAN),
        ("study_hours", "総勉強時間", GREEN),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(11, 7))
    fig.patch.set_facecolor(BG)
    for ax, (column, label, color) in zip(axes.flat, targets):
        dark_axes(ax)
        work = df[["caffeine_mg", column]].dropna()
        if work.empty:
            ax.text(0.5, 0.5, "データ不足", color=MUTED, ha="center", va="center", transform=ax.transAxes)
        else:
            ax.scatter(
                work["caffeine_mg"],
                work[column],
                color=color,
                alpha=0.74,
                edgecolors="white",
                linewidths=0.45,
            )
        ax.set_title(f"カフェインmg × {label}  r={correlation_text(df, 'caffeine_mg', column)}", loc="left", fontsize=11)
        ax.set_xlabel("カフェイン mg")
        ax.set_ylabel(label)
    fig.tight_layout()
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)


def render_study_mix_chart(df: pd.DataFrame, *, show_task_metrics: bool = True) -> None:
    if df.empty:
        st.info("表示できる学習データがまだありません。")
        return
    if show_task_metrics:
        fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
        mix_ax, task_ax = axes
    else:
        fig, mix_ax = plt.subplots(figsize=(11, 4.5))
        task_ax = None
    fig.patch.set_facecolor(BG)
    dark_axes(mix_ax)
    if task_ax is not None:
        dark_axes(task_ax)

    bar_edge = THEME["panel"]
    mix_ax.bar(
        df["date"],
        df["input_hours"],
        color=CYAN,
        alpha=0.78,
        edgecolor=bar_edge,
        linewidth=0.7,
        label="インプット",
    )
    mix_ax.bar(
        df["date"],
        df["output_hours"],
        bottom=df["input_hours"],
        color=GREEN,
        alpha=0.78,
        edgecolor=bar_edge,
        linewidth=0.7,
        label="アウトプット",
    )
    mix_ax.bar(
        df["date"],
        df["unset_io_hours"],
        bottom=df["input_hours"] + df["output_hours"],
        color=MUTED,
        alpha=0.34,
        edgecolor=bar_edge,
        linewidth=0.7,
        hatch="//",
        label="未分類",
    )
    if len(df) <= 14:
        totals = df["input_hours"] + df["output_hours"] + df["unset_io_hours"]
        for plotted_date, total in zip(df["date"], totals):
            if total > 0:
                mix_ax.text(plotted_date, total, f"{total:.1f}", ha="center", va="bottom", fontsize=7.5, color=MUTED)
    style_chart_axis(mix_ax, THEME, title="インプット/アウトプット配分", ylabel="時間", grid_axis="y")
    legend = mix_ax.legend(
        loc="lower right",
        bbox_to_anchor=(1.0, 1.08),
        ncol=3,
        frameon=False,
        fontsize=7.5,
        handlelength=1.8,
        columnspacing=0.9,
        borderaxespad=0,
    )
    for text in legend.get_texts():
        text.set_color(TEXT)

    if task_ax is not None:
        plan_data = df.dropna(subset=["task_completion_rate", "caffeine_mg", "study_hours"])
        if plan_data.empty:
            task_ax.text(0.5, 0.5, "比較に必要な有効記録が不足しています", color=MUTED, ha="center", va="center", transform=task_ax.transAxes)
        else:
            scatter = task_ax.scatter(
                plan_data["caffeine_mg"],
                plan_data["task_completion_rate"],
                c=plan_data["study_hours"],
                cmap="viridis",
                s=(plan_data["study_hours"].clip(lower=0.2) * 80).clip(35, 420),
                alpha=0.82,
                edgecolors="white",
                linewidths=0.45,
            )
            cbar = fig.colorbar(scatter, ax=task_ax)
            cbar.set_label("総勉強時間")
            cbar.ax.yaxis.label.set_color(TEXT)
            cbar.ax.tick_params(colors=TEXT)
            cbar.outline.set_edgecolor(THEME["line"])
        task_ax.set_title("カフェインmg × タスク達成率", loc="left", fontsize=11)
        task_ax.set_xlabel("カフェイン mg")
        task_ax.set_ylabel("達成率 %")
    style_date_axis(mix_ax)
    fig.tight_layout()
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)


def render_condition_group_table(df: pd.DataFrame, *, show_task_metrics: bool = True) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    df = df.loc[df["has_caffeine_log"]]
    if df.empty:
        return pd.DataFrame()
    metrics = {
        "平均カフェインmg": "caffeine_mg", "平均勉強時間": "study_hours",
        "平均疲労感": "fatigue", "平均ストレス": "stress", "平均集中しやすさ": "focus_score",
        "平均インプット時間": "input_hours", "平均アウトプット時間": "output_hours",
    }
    if show_task_metrics:
        metrics["平均タスク達成率%"] = "task_completion_rate"
    return grouped_metric_summary(df, ["caffeine_band"], metrics).rename(columns={"caffeine_band": "カフェイン帯"})


def correlation_source(df: pd.DataFrame, *, show_task_metrics: bool = True) -> pd.DataFrame:
    columns = {
        "カフェインmg": "caffeine_mg",
        "時刻付きカフェインmg": "timed_caffeine_mg",
        "時刻不明カフェインmg": "caffeine_untimed_mg",
        "摂取記録数": "caffeine_record_count",
        "最初の摂取時刻": "first_caffeine_hour",
        "最後の摂取時刻": "last_caffeine_hour",
        "18時以降カフェインmg": "caffeine_after_18_mg",
        "前日カフェインmg": "previous_caffeine_mg",
        "総勉強時間": "study_hours",
        "夜勉強時間": "night_study_hours",
        "セッション数": "study_session_count",
        "平均頑張り度": "avg_effort",
        "インプット時間": "input_hours",
        "アウトプット時間": "output_hours",
        "アウトプット比率": "output_ratio",
        "予定時間": "planned_hours",
        "タスク達成率": "task_completion_rate",
        "予定差分": "study_plan_gap_hours",
        "睡眠時間": "sleep_hours",
        "睡眠の質": "sleep_quality",
        "朝の眠気": "morning_sleepiness",
        "疲労感": "fatigue",
        "ストレス": "stress",
        "集中しやすさ": "focus_score",
        "気分": "mood_score",
        "ラン距離": "running_distance_km",
        "ラン時間": "running_duration_min",
        "ランきつさ": "running_avg_intensity",
        "ラン後疲労": "running_avg_fatigue_after",
        "翌日睡眠時間": "next_sleep_hours",
        "翌日朝眠気": "next_morning_sleepiness",
        "翌日集中": "next_focus_score",
        "翌日疲労": "next_fatigue",
        "翌日ストレス": "next_stress",
    }
    if not show_task_metrics:
        columns.pop("タスク達成率")
    for category in STUDY_CATEGORIES:
        columns[f"{category}時間"] = f"study_category_{category}_hours"

    data = pd.DataFrame()
    for label, column in columns.items():
        if column in df.columns:
            data[label] = pd.to_numeric(df[column], errors="coerce")
    useful_columns = [column for column in data.columns if data[column].notna().sum() >= 2 and data[column].nunique(dropna=True) >= 2]
    return data[useful_columns]


def render_correlation_heatmap(df: pd.DataFrame, *, show_task_metrics: bool = True) -> None:
    data = correlation_source(df, show_task_metrics=show_task_metrics)
    if data.shape[1] < 2:
        st.info("相関ヒートマップを作るには、変動のある数値データが2種類以上必要です。")
        return

    corr = data.corr(min_periods=MIN_CORR_PAIRS)
    pair_counts = data.notna().astype(int).T.dot(data.notna().astype(int))
    values = corr.fillna(0).to_numpy()
    cmap = plt.get_cmap("coolwarm")
    norm = plt.Normalize(vmin=-1, vmax=1)
    rgba = cmap(norm(values))
    valid = corr.notna().to_numpy()
    enough = pair_counts.to_numpy() >= (MIN_CORR_PAIRS * 2)
    rgba[..., 3] = np.where(valid, np.where(enough, 0.92, 0.45), 0.12)

    size = len(data.columns)
    fig, ax = plt.subplots(figsize=(max(10, size * 0.52), max(7, size * 0.48)))
    fig.patch.set_facecolor(BG)
    dark_axes(ax)
    ax.imshow(rgba, aspect="equal")
    ax.set_xticks(range(size))
    ax.set_xticklabels(
        data.columns,
        rotation=65,
        ha="right",
        color=TEXT,
        fontsize=9,
        fontweight="medium",
    )
    ax.set_yticks(range(size))
    ax.set_yticklabels(data.columns, color=TEXT, fontsize=9, fontweight="medium")
    ax.set_title("相関係数ヒートマップ", loc="left", fontsize=12)
    ax.set_xticks(np.arange(-0.5, size, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, size, 1), minor=True)
    ax.grid(which="minor", color=THEME["line"], alpha=0.7, linewidth=0.8)
    ax.tick_params(which="minor", bottom=False, left=False)

    if size <= 24:
        for row in range(size):
            for col in range(size):
                value = corr.iloc[row, col]
                if pd.isna(value):
                    continue
                red, green, blue = rgba[row, col, :3]
                luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
                text_color = "#06111F" if luminance >= 0.58 else "#FFFFFF"
                ax.text(col, row, f"{value:.2f}", ha="center", va="center", color=text_color, fontsize=7)

    scalar_mappable = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    scalar_mappable.set_array([])
    cbar = fig.colorbar(scalar_mappable, ax=ax, fraction=0.028, pad=0.02)
    cbar.set_label("相関係数")
    cbar.ax.yaxis.label.set_color(TEXT)
    cbar.ax.tick_params(colors=TEXT)
    cbar.outline.set_edgecolor(THEME["line"])
    fig.tight_layout()
    try:
        st.pyplot(fig, clear_figure=True)
    finally:
        plt.close(fig)
    st.caption(f"薄いセルは有効ペア数が{MIN_CORR_PAIRS * 2}未満です。相関は因果ではなく、記録上の同時変動として読んでください。")


def render_lag_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    df = df.loc[df["has_caffeine_log"]]
    if df.empty:
        return pd.DataFrame()
    return grouped_metric_summary(df, ["caffeine_band"], {
        "平均カフェインmg": "caffeine_mg", "当日平均勉強時間": "study_hours",
        "翌日睡眠時間": "next_sleep_hours", "翌朝眠気": "next_morning_sleepiness",
        "翌日集中しやすさ": "next_focus_score", "翌日疲労感": "next_fatigue", "翌日ストレス": "next_stress",
    }).rename(columns={"caffeine_band": "当日カフェイン帯"})


def correlation_text(df: pd.DataFrame, x: str, y: str) -> str:
    work = df[[x, y]].dropna()
    if len(work) < 4 or work[x].nunique() < 2 or work[y].nunique() < 2:
        return "データ不足"
    return f"{work[x].corr(work[y]):.2f}"


def render_timing_analysis(events: pd.DataFrame, sessions: pd.DataFrame) -> None:
    timed_session_count = int(sessions["minutes_since_last_intake"].notna().sum()) if not sessions.empty else 0
    median_minutes = sessions["minutes_since_last_intake"].dropna().median() if timed_session_count else None
    metric_cols = st.columns(4)
    with metric_cols[0]:
        metric_card("時刻付き摂取", f"{len(events)} 件", "選択期間内", CYAN)
    with metric_cols[1]:
        event_total = float(events["摂取量mg"].sum()) if not events.empty else 0
        metric_card("時刻付き合計", f"{format_mg(event_total)} mg", "時刻不明分を除く", GREEN)
    with metric_cols[2]:
        metric_card("関連付け可能セッション", f"{timed_session_count} 件", "開始前の摂取記録あり", AMBER)
    with metric_cols[3]:
        median_label = f"{median_minutes:.0f} 分" if median_minutes is not None else "データ不足"
        metric_card("摂取から開始まで", median_label, "中央値", PURPLE)

    st.caption("学習セッション開始以前で最も近い摂取記録との時間差を集計します。時刻不明分は推測して関連付けません。")
    if sessions.empty:
        st.info("選択期間に学習セッションがありません。")
    else:
        grouped_source = sessions.copy()
        grouped_source["timing_band"] = pd.Categorical(
            grouped_source["timing_band"], categories=TIMING_BAND_ORDER, ordered=True
        )
        grouped = (
            grouped_source.groupby("timing_band", observed=False)
            .agg(
                session_count=("session_start", "count"),
                total_study_min=("duration_min", lambda values: values.sum(min_count=1)),
                avg_session_min=("duration_min", "mean"),
                duration_n=("duration_min", "count"),
                avg_effort=("effort", "mean"),
                effort_n=("effort", "count"),
                avg_caffeine_6h_mg=("caffeine_6h_mg", "mean"),
            )
            .reset_index()
        )
        grouped = grouped[grouped["session_count"] > 0].copy()
        if not grouped.empty:
            fig, ax = plt.subplots(figsize=(10.5, max(4.3, len(grouped) * 0.72)))
            fig.patch.set_facecolor(BG)
            dark_axes(ax)
            timing_labels = [
                f"{band}\nn={int(count)}"
                for band, count in zip(grouped["timing_band"].astype(str), grouped["duration_n"])
            ]
            plot_rounded_category_bars(
                ax,
                timing_labels,
                grouped["avg_session_min"].to_numpy(dtype=float),
                palette=THEME,
                colors=CYAN,
                orientation="horizontal",
                linewidth=21,
                formatter=lambda value: f"{value:.1f}分",
            )
            style_chart_axis(
                ax,
                THEME,
                title="摂取からセッション開始までの経過時間別・平均学習時間",
                ylabel="",
                grid_axis="x",
            )
            ax.set_xlabel("平均セッション時間（分）")
            fig.tight_layout()
            try:
                st.pyplot(fig, clear_figure=True)
            finally:
                plt.close(fig)

            grouped["total_study_hours"] = (grouped["total_study_min"] / 60).round(2)
            grouped["avg_session_min"] = grouped["avg_session_min"].round(1)
            grouped["avg_effort"] = grouped["avg_effort"].round(2)
            grouped["avg_caffeine_6h_mg"] = grouped["avg_caffeine_6h_mg"].round(1)
            st.dataframe(
                grouped[
                    ["timing_band", "session_count", "total_study_hours", "avg_session_min", "duration_n", "avg_effort", "effort_n", "avg_caffeine_6h_mg"]
                ].rename(
                    columns={
                        "timing_band": "経過時間区分",
                        "session_count": "セッション数",
                        "total_study_hours": "総勉強時間",
                        "avg_session_min": "平均セッション分",
                        "duration_n": "学習時間 有効n",
                        "avg_effort": "平均頑張り度",
                        "effort_n": "頑張り度 有効n",
                        "avg_caffeine_6h_mg": "開始前6時間の平均記録量mg",
                    }
                ),
                width="stretch",
                hide_index=True,
            )

        detail = sessions.sort_values(
            "session_start",
            ascending=False,
            na_position="last",
            kind="stable",
        ).copy()
        detail["セッション開始"] = detail["session_start"].map(datetime_label)
        detail["直近摂取"] = detail["last_intake_at"].map(datetime_label)
        detail["経過分"] = detail["minutes_since_last_intake"].apply(
            lambda value: round(float(value), 1) if pd.notna(value) else None
        )
        detail["学習分"] = detail["duration_min"].round(1)
        st.markdown("#### セッションとの対応")
        st.dataframe(
            detail[
                ["セッション開始", "直近摂取", "timing_band", "経過分", "last_intake_mg", "caffeine_6h_mg", "学習分", "effort", "category", "io_type"]
            ].rename(
                columns={
                    "timing_band": "経過時間区分",
                    "last_intake_mg": "直近摂取mg",
                    "caffeine_6h_mg": "開始前6時間mg",
                    "effort": "頑張り度",
                    "category": "勉強カテゴリ",
                    "io_type": "インプット/アウトプット",
                }
            ),
            width="stretch",
            hide_index=True,
        )

    st.markdown("#### 時刻付き摂取履歴")
    if events.empty:
        st.info("時刻付きのカフェイン摂取記録はまだありません。既存の日次合計は時刻不明分として維持されます。")
    else:
        event_table = events.sort_values(
            "摂取日時",
            ascending=False,
            na_position="last",
            kind="stable",
        ).copy()
        event_table["摂取日時"] = event_table["摂取日時"].map(datetime_label)
        event_table["記録元"] = event_table["記録元"].replace(
            {
                "caffeine_record_page": "カフェイン記録",
                "timer_quick_add": "秒給カウンター（旧導線）",
                "diary": "日記（旧導線）",
                "demo_generator": "デモ生成",
            }
        )
        st.dataframe(event_table, width="stretch", hide_index=True)
        st.download_button(
            "摂取履歴CSVをダウンロード",
            data=to_csv_bytes(event_table),
            file_name="caffeine_timing_events.csv",
            mime="text/csv",
            width="stretch", on_click="ignore")


def build_comments(df: pd.DataFrame, *, show_task_metrics: bool = True) -> list[str]:
    if df.empty:
        return ["表示できるデータがまだありません。"]
    comments: list[str] = []
    caff = df[df["has_caffeine"]]
    no_caff = df[df["has_no_caffeine_record"]]
    if caff["study_hours"].notna().any() and no_caff["study_hours"].notna().any():
        diff = caff["study_hours"].mean() - no_caff["study_hours"].mean()
        direction = "長め" if diff > 0 else "短め"
        comments.append(f"記録上、カフェインありの日の平均勉強時間は、0mg扱いの日（現在の欠測設定を適用）より{abs(diff):.2f}時間{direction}です。")
    run = df[df["has_running"]]
    if not run.empty:
        comments.append(
            f"ランニング日は{len(run)}日あり、平均距離は{observed_mean_label(run['running_distance_km'], 'km')}、平均主観的きつさは{observed_mean_label(run['running_avg_intensity'], '/10')}です。"
        )
    both = df[df["has_caffeine"] & df["has_running"]]
    if not both.empty:
        comments.append(
            f"カフェインあり+ランニングありの日は{len(both)}日で、平均勉強時間は{observed_mean_label(both['study_hours'], '時間')}、運動後疲労は{observed_mean_label(both['running_avg_fatigue_after'], '/5')}です。"
        )
    corr = correlation_text(df, "caffeine_mg", "next_morning_sleepiness")
    if corr != "データ不足":
        comments.append(f"カフェイン量と翌朝眠気の相関係数は{corr}です。因果ではなく、記録上の同時変動として見てください。")
    focus_corr = correlation_text(df, "caffeine_mg", "focus_score")
    fatigue_corr = correlation_text(df, "caffeine_mg", "fatigue")
    stress_corr = correlation_text(df, "caffeine_mg", "stress")
    if focus_corr != "データ不足" or fatigue_corr != "データ不足" or stress_corr != "データ不足":
        comments.append(
            f"同日の記録では、カフェイン量との相関は集中しやすさ={focus_corr}、疲労感={fatigue_corr}、ストレス={stress_corr}です。"
        )
    if show_task_metrics:
        task_corr = correlation_text(df, "caffeine_mg", "task_completion_rate")
        if task_corr != "データ不足":
            comments.append(f"カフェイン量とタスク達成率の相関係数は{task_corr}です。予定を入れた日が増えるほど読みやすくなります。")
    if len(comments) < 2:
        comments.append("カフェイン、睡眠・体調、運動ログを同じ期間に記録すると、傾向コメントの精度が上がります。")
    return comments


def observed_mean_label(values: pd.Series, unit: str) -> str:
    count = int(values.count())
    return f"{values.mean():.2f}{unit}（有効n={count}）" if count else "未記録（有効n=0）"


def measured_label(value: Any, unit: str) -> str:
    return "未記録" if pd.isna(value) else f"{float(value):.2f}{unit}"


def display_table(df: pd.DataFrame, *, show_task_metrics: bool = True) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    table = df.sort_values(
        "date",
        ascending=False,
        na_position="last",
        kind="stable",
    ).copy()
    table["日付"] = table["date"].dt.date.astype(str)
    table["勉強時間"] = table["study_duration_sec"].apply(seconds_label)
    table["夜勉強"] = table["night_study_hours"].map(lambda value: measured_label(value, "時間"))
    table["インプット"] = table["input_hours"].map(lambda value: measured_label(value, "時間"))
    table["アウトプット"] = table["output_hours"].map(lambda value: measured_label(value, "時間"))
    table["アウトプット比率"] = table["output_ratio"].map(lambda value: "-" if pd.isna(value) else f"{float(value):.1f}%")
    table["予定時間"] = table["planned_hours"].map(lambda value: measured_label(value, "時間"))
    if show_task_metrics:
        table["タスク達成率"] = table["task_completion_rate"].map(lambda value: "-" if pd.isna(value) else f"{float(value):.1f}%")
    table["睡眠時間"] = table["sleep_duration_min"].apply(lambda value: "-" if pd.isna(value) else minutes_label(value))
    table["ラン時間"] = table["running_duration_min"].apply(minutes_label)
    table["ラン距離"] = table["running_distance_km"].map(lambda value: measured_label(value, " km"))
    table["学習記録"] = table["study_recorded"].map({True: "あり", False: "未記録"})
    table["健康記録"] = table["health_recorded"].map({True: "あり", False: "未記録"})
    table["カフェイン記録"] = table["caffeine_recorded"].map({True: "あり", False: "未記録"})
    table.loc[table["caffeine_imputed_zero"], "カフェイン記録"] = "未記録（0mg扱い）"
    table["初回摂取"] = table["first_caffeine_hour"].map(
        lambda value: "-" if pd.isna(value) else f"{int(value):02d}:{int(round((float(value) % 1) * 60)) % 60:02d}"
    )
    table["最終摂取"] = table["last_caffeine_hour"].map(
        lambda value: "-" if pd.isna(value) else f"{int(value):02d}:{int(round((float(value) % 1) * 60)) % 60:02d}"
    )
    display_columns = [
        "日付",
        "カフェイン記録",
        "学習記録",
        "健康記録",
        "running_state",
        "caffeine_mg",
        "caffeine_band",
        "timed_caffeine_mg",
        "caffeine_untimed_mg",
        "caffeine_record_count",
        "初回摂取",
        "最終摂取",
        "caffeine_after_18_mg",
        "勉強時間",
        "study_session_count",
        "夜勉強",
        "インプット",
        "アウトプット",
        "アウトプット比率",
        "予定時間",
    ]
    if show_task_metrics:
        display_columns.append("タスク達成率")
    display_columns.extend(
        [
            "avg_effort",
            "睡眠時間",
            "sleep_quality",
            "morning_sleepiness",
            "focus_score",
            "fatigue",
            "stress",
            "ラン時間",
            "ラン距離",
            "running_avg_intensity",
            "running_avg_fatigue_after",
            "next_morning_sleepiness",
            "next_focus_score",
        ]
    )
    return table[display_columns].rename(
        columns={
            "caffeine_mg": "カフェインmg",
            "running_state": "ランニング記録",
            "caffeine_band": "カフェイン帯",
            "timed_caffeine_mg": "時刻付きmg",
            "caffeine_untimed_mg": "時刻不明mg",
            "caffeine_record_count": "摂取記録数",
            "caffeine_after_18_mg": "18時以降mg",
            "study_session_count": "学習セッション数",
            "avg_effort": "平均頑張り度",
            "sleep_quality": "睡眠の質",
            "morning_sleepiness": "朝の眠気",
            "focus_score": "集中しやすさ",
            "fatigue": "疲労感",
            "stress": "ストレス",
            "running_avg_intensity": "ラン平均きつさ",
            "running_avg_fatigue_after": "ラン後疲労",
            "next_morning_sleepiness": "翌朝眠気",
            "next_focus_score": "翌日集中しやすさ",
        }
    )


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


inject_dark_css()
page_settings = ab.sidemenu("成分の推定残存量と、カフェイン、学習、睡眠・体調、ランニングの関係を確認します。", settings=PAGE_SETTINGS)

today = app_today()
with st.sidebar:
    st.subheader("分析条件")
    period_label = st.selectbox("期間", ["直近7日", "直近14日", "直近30日", "直近90日", "今月", "任意期間"], index=2)
    if period_label == "任意期間":
        start_date = st.date_input("開始日", value=today - timedelta(days=29))
        end_date = st.date_input("終了日", value=today)
    else:
        start_date, end_date = period_range(period_label, today)
        st.caption(f"{start_date.isoformat()} から {end_date.isoformat()} まで")

if end_date < start_date:
    start_date, end_date = end_date, start_date

show_task_metrics = should_show_task_metrics(page_settings)
caffeine_missing_as_zero = should_treat_missing_caffeine_as_zero(page_settings)

ab.topbar(
    "成分リズム・コンディション分析",
    "設定した成分の推定残存量を比較し、カフェインについては摂取タイミング、勉強時間、睡眠・集中、運動との記録上の傾向も確認します。",
)

diary_context: dict[date, dict[str, Any]] = {}
study_context: dict[date, list[dict[str, Any]]] = {}
df = build_dashboard_frame(start_date, end_date, diary_cache=diary_context, study_cache=study_context, caffeine_missing_as_zero=caffeine_missing_as_zero)

if df.empty:
    render_substance_residual(today)
    st.info("表示できるデータがまだありません。")
    st.stop()

total_caffeine = float(df["caffeine_mg"].sum())
timed_record_count = int(df["caffeine_record_count"].sum())
caffeine_days = int(df["has_caffeine"].sum())
running_days = int(df["has_running"].sum())
avg_study_caff = df.loc[df["has_caffeine"], "study_hours"].mean()
avg_study_no_caff = df.loc[df["has_no_caffeine_record"], "study_hours"].mean()
avg_running_fatigue = df.loc[df["has_running"], "running_avg_fatigue_after"].mean()

st.subheader("Signal Board")
if caffeine_missing_as_zero:
    st.caption(f"設定：カフェイン未記録は0mgとして比較（補完 {int(df['caffeine_imputed_zero'].sum())}日）。元の記録有無は日別表に残し、未来日は除外します。")
else:
    st.caption("設定：日記またはカフェイン項目が未記録の日は比較から除外します。保存済み0mgには保存時の初期値も含まれます。")
st.caption("学習・運動・健康の未記録や欠けた項目は0として扱いません。明示された0は有効です。平均は各指標の有効値のみで計算し、表の「有効n」はその件数です。ランニング未記録は「なし」と別群です。")
st.caption(
    f"有効記録：勉強時間 {df['study_hours'].count()}日 / 睡眠時間 {df['sleep_hours'].count()}日 / "
    f"集中 {df['focus_score'].count()}日 / ランニング有無 {int(df['running_status_recorded'].sum())}日（表示 {len(df)}日）"
)
metric_cols = st.columns(5)
with metric_cols[0]:
    metric_card("総カフェイン量", f"{format_mg(total_caffeine)} mg", f"記録日 {caffeine_days}日 / 時刻付き {timed_record_count}件", CYAN)
with metric_cols[1]:
    metric_card("カフェインあり平均勉強", "—" if pd.isna(avg_study_caff) else f"{avg_study_caff:.2f} h", f"有効n={df.loc[df['has_caffeine'], 'study_hours'].count()}", GREEN)
with metric_cols[2]:
    metric_card("0mg扱いの平均勉強", "—" if pd.isna(avg_study_no_caff) else f"{avg_study_no_caff:.2f} h", f"現在の欠測設定を適用 / 有効n={df.loc[df['has_no_caffeine_record'], 'study_hours'].count()}", MUTED)
with metric_cols[3]:
    metric_card("ランニング日数", f"{running_days} 日", f"総距離 {df['running_distance_km'].sum():.2f} km", AMBER)
with metric_cols[4]:
    metric_card("ラン後疲労平均", "—" if pd.isna(avg_running_fatigue) else f"{avg_running_fatigue:.2f} / 5", f"有効n={df.loc[df['has_running'], 'running_avg_fatigue_after'].count()}", RED)

render_substance_residual(today)

st.subheader("Timeline")
render_timeline(df)

st.subheader("相関係数ヒートマップ")
st.caption("Correlation Matrix: カフェイン、学習、睡眠・体調、ランニング、翌日指標、カテゴリ別学習時間をまとめた相関行列です。")
render_correlation_heatmap(df, show_task_metrics=show_task_metrics)

analysis_view = st.radio(
    "表示内容",
    ["カフェイン比較", "摂取タイミング", "勉強・体調", "翌日ラグ", "ランニング", "日別データ"],
    horizontal=True,
    key="rhythm_analysis_view",
)

if analysis_view == "カフェイン比較":
    render_band_compare(df)
    st.markdown("#### カフェイン帯 × ランニング有無")
    group_table = render_group_table(df)
    if group_table.empty:
        st.info("比較できるデータがまだありません。")
    else:
        st.dataframe(group_table, width="stretch", hide_index=True)

elif analysis_view == "摂取タイミング":
    event_df = build_caffeine_event_frame(start_date, end_date, diary_cache=diary_context)
    session_timing_df = build_session_timing_frame(
        start_date, end_date, ROOT, preloaded_diaries=diary_context, preloaded_study_by_date=study_context,
    )
    render_timing_analysis(event_df, session_timing_df)

elif analysis_view == "勉強・体調":
    corr_cols = st.columns(4)
    with corr_cols[0]:
        metric_card("カフェイン×集中", correlation_text(df, "caffeine_mg", "focus_score"), "相関係数", CYAN)
    with corr_cols[1]:
        metric_card("カフェイン×疲労感", correlation_text(df, "caffeine_mg", "fatigue"), "相関係数", RED)
    with corr_cols[2]:
        metric_card("カフェイン×ストレス", correlation_text(df, "caffeine_mg", "stress"), "相関係数", PURPLE)
    with corr_cols[3]:
        metric_card("カフェイン×総勉強", correlation_text(df, "caffeine_mg", "study_hours"), "相関係数", GREEN)
    st.caption("疲労感・ストレス・集中しやすさは健康ログ側の主観スコア、総勉強時間は当日の学習ログ合計です。")
    render_caffeine_condition_scatter(df)

    if show_task_metrics:
        st.markdown("#### インプット/アウトプット・タスク達成率")
        mix_cols = st.columns(3)
        with mix_cols[0]:
            metric_card("カフェイン×達成率", correlation_text(df, "caffeine_mg", "task_completion_rate"), "相関係数", AMBER)
        with mix_cols[1]:
            metric_card("カフェイン×インプット", correlation_text(df, "caffeine_mg", "input_hours"), "相関係数", CYAN)
        with mix_cols[2]:
            metric_card("カフェイン×アウトプット", correlation_text(df, "caffeine_mg", "output_hours"), "相関係数", GREEN)
    else:
        st.markdown("#### インプット/アウトプット")
        mix_cols = st.columns(2)
        with mix_cols[0]:
            metric_card("カフェイン×インプット", correlation_text(df, "caffeine_mg", "input_hours"), "相関係数", CYAN)
        with mix_cols[1]:
            metric_card("カフェイン×アウトプット", correlation_text(df, "caffeine_mg", "output_hours"), "相関係数", GREEN)
    render_study_mix_chart(df, show_task_metrics=show_task_metrics)

    st.markdown("#### カフェイン帯ごとの勉強・体調")
    condition_table = render_condition_group_table(df, show_task_metrics=show_task_metrics)
    if condition_table.empty:
        st.info("比較できるデータがまだありません。")
    else:
        st.dataframe(condition_table, width="stretch", hide_index=True)

elif analysis_view == "翌日ラグ":
    lag_cols = st.columns(4)
    with lag_cols[0]:
        metric_card("当日カフェイン×翌日睡眠", correlation_text(df, "caffeine_mg", "next_sleep_hours"), "相関係数", CYAN)
    with lag_cols[1]:
        metric_card("当日カフェイン×翌朝眠気", correlation_text(df, "caffeine_mg", "next_morning_sleepiness"), "相関係数", PURPLE)
    with lag_cols[2]:
        metric_card("当日カフェイン×翌日集中", correlation_text(df, "caffeine_mg", "next_focus_score"), "相関係数", GREEN)
    with lag_cols[3]:
        metric_card("当日カフェイン×翌日疲労", correlation_text(df, "caffeine_mg", "next_fatigue"), "相関係数", RED)
    st.caption("翌日ラグは表示期間の前後1日も読み込んで計算します。最終日の翌日に実記録があれば利用し、未来の日付・未記録は比較から除外します。")
    lag_table = render_lag_table(df)
    if lag_table.empty:
        st.info("翌日比較に使えるデータがまだありません。")
    else:
        st.dataframe(lag_table, width="stretch", hide_index=True)

elif analysis_view == "ランニング":
    render_running_scatter(df)
    run_cols = st.columns(3)
    with run_cols[0]:
        metric_card("カフェイン×ラン距離", correlation_text(df, "caffeine_mg", "running_distance_km"), "相関係数", AMBER)
    with run_cols[1]:
        metric_card("カフェイン×きつさ", correlation_text(df, "caffeine_mg", "running_avg_intensity"), "相関係数", RED)
    with run_cols[2]:
        metric_card("ラン後疲労×勉強", correlation_text(df, "running_avg_fatigue_after", "study_hours"), "相関係数", GREEN)

elif analysis_view == "日別データ":
    table = display_table(df, show_task_metrics=show_task_metrics)
    if table.empty:
        st.info("表示できる日別データはまだありません。")
    else:
        st.dataframe(table, width="stretch", hide_index=True)
        st.download_button(
            "CSVをダウンロード",
            data=to_csv_bytes(table),
            file_name="caffeine_running_analysis.csv",
            mime="text/csv",
            width="stretch", on_click="ignore")

st.subheader("読み取りコメント")
for comment in build_comments(df, show_task_metrics=show_task_metrics):
    insight_card(comment)
