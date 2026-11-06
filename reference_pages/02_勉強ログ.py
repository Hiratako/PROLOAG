from __future__ import annotations
from app_core.json_store import record_json_files

from datetime import date, datetime, time, timedelta
import math
from app_core.app_clock import app_today
from app_core.settings_service import load_user_settings
from app_core.study_log_service import append_study_session, calculate_session_money_with_daily_total
from app_core.study_log_editor_ui import render_study_log_management
from app_core.reference_snapshot import ReferenceSnapshot
from app_core.master_snapshot import MasterSnapshot
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from master_ui import render_master_quick_add
from app_core.study_category_service import study_category_options
from data_utils import (
    build_master_name_lookup,
    load_json_safe,
    load_master_data,
    load_study_sessions_file,
    resolve_master_names_from_lookup,
)


ROOT = Path(__file__).resolve().parents[1]
STUDY_DIR = ROOT / "study_logs"
DIARY_DIR = ROOT / "diary_entries"
USER_SETTINGS = load_user_settings()
TIMER_SETTINGS = USER_SETTINGS.get("timer", {})


def sec_to_hms(seconds: int | float) -> str:
    try:
        number = float(seconds)
        if not math.isfinite(number):
            return "未記録"
    except (TypeError, ValueError):
        return "未記録"
    total = max(int(number), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def yen(value: int | float) -> str:
    try:
        number = float(value)
        return f"{int(number):,}円" if math.isfinite(number) else "未記録"
    except (TypeError, ValueError):
        return "未記録"


def read_json(path: Path, fallback: Any) -> Any:
    return load_json_safe(path, fallback)


def clean_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, "", [])}


def master_options(items: list[dict[str, Any]], id_key: str, blank_label: str = "すべて") -> dict[str, str]:
    options = {blank_label: ""}
    for item in items:
        options[f"{item.get('name', '名称未設定')} [{item.get(id_key, '')}]"] = item.get(id_key, "")
    return options


def selected_id(label: str, options: dict[str, str]) -> str:
    return options.get(label, "")


def ensure_selectbox_value(key: str, options: dict[str, str]) -> None:
    if key in st.session_state and st.session_state[key] not in options:
        del st.session_state[key]


def clear_selectbox_values(keys: tuple[str, ...]) -> None:
    for key in keys:
        st.session_state.pop(key, None)


def select_master_id(
    label: str,
    options: dict[str, str],
    key: str,
    *,
    clear_on_change: tuple[str, ...] = (),
) -> str:
    ensure_selectbox_value(key, options)
    selectbox_kwargs: dict[str, Any] = {}
    if clear_on_change:
        selectbox_kwargs = {"on_change": clear_selectbox_values, "args": (clear_on_change,)}
    selected_label = st.selectbox(label, list(options.keys()), key=key, **selectbox_kwargs)
    return selected_id(selected_label, options)


def active_master_items(master_data: dict[str, Any], collection: str, **filters: str | None) -> list[dict[str, Any]]:
    items = master_data.get(collection, [])
    if not isinstance(items, list):
        return []
    active = [item for item in items if isinstance(item, dict) and item.get("is_active", True)]
    for key, value in filters.items():
        if value:
            active = [item for item in active if item.get(key) == value]
    return active


def master_selects(
    prefix: str,
    master_data: dict[str, Any],
    *,
    blank_label: str = "すべて",
    require_subject_for_material: bool = False,
) -> dict[str, str]:
    project_key = f"{prefix}_project"
    subject_key = f"{prefix}_subject"
    material_key = f"{prefix}_material"
    unit_key = f"{prefix}_unit"

    project_options = master_options(active_master_items(master_data, "projects"), "project_id", blank_label)
    project_id = select_master_id(
        "プロジェクト",
        project_options,
        project_key,
        clear_on_change=(subject_key, material_key, unit_key),
    )

    subject_options = master_options(active_master_items(master_data, "subjects", project_id=project_id or None), "subject_id", blank_label)
    subject_id = select_master_id(
        "科目",
        subject_options,
        subject_key,
        clear_on_change=(material_key, unit_key),
    )

    material_items = [] if require_subject_for_material and not subject_id else active_master_items(
        master_data,
        "materials",
        project_id=project_id or None,
        subject_id=subject_id or None,
    )
    material_options = master_options(material_items, "material_id", blank_label)
    material_id = select_master_id("教材", material_options, material_key, clear_on_change=(unit_key,))

    unit_items = [] if require_subject_for_material and not material_id else active_master_items(
        master_data,
        "units",
        project_id=project_id or None,
        subject_id=subject_id or None,
        material_id=material_id or None,
    )
    unit_options = master_options(unit_items, "unit_id", blank_label)
    unit_id = select_master_id("単元", unit_options, unit_key)
    return {"project_id": project_id, "subject_id": subject_id, "material_id": material_id, "unit_id": unit_id}


def master_filter_selects(prefix: str, master_data: dict[str, Any]) -> dict[str, str]:
    return master_selects(prefix, master_data, blank_label="すべて", require_subject_for_material=False)


def master_input_selects(prefix: str, master_data: dict[str, Any]) -> dict[str, str]:
    return master_selects(prefix, master_data, blank_label="未設定", require_subject_for_material=True)


def timer_setting(name: str, default: Any) -> Any:
    return TIMER_SETTINGS.get(name, default)


def overtime_enabled() -> bool:
    return bool(timer_setting("overtime_enabled", True))


def overtime_threshold_hours() -> float:
    return float(timer_setting("overtime_threshold_hours", 8.0) or 8.0)


def overtime_multiplier() -> float:
    return float(timer_setting("overtime_multiplier", 1.25) or 1.25)


def current_money_policy() -> dict[str, Any]:
    return {
        "overtime_enabled": overtime_enabled(),
        "threshold_hours": overtime_threshold_hours(),
        "multiplier": overtime_multiplier(),
    }


def calculate_manual_money(start_dt: datetime, duration_sec: int, hourly_rate: int | float) -> dict[str, int]:
    session = {
        "start_at": start_dt.isoformat(timespec="seconds"),
        "duration_sec": duration_sec,
        "hourly_rate": hourly_rate,
    }
    return calculate_session_money_with_daily_total(
        session,
        load_sessions(session_path(start_dt.date())),
        hourly_rate=hourly_rate,
        overtime_enabled=overtime_enabled(),
        threshold_hours=overtime_threshold_hours(),
        multiplier=overtime_multiplier(),
    )


def session_path(target: date) -> Path:
    return STUDY_DIR / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"


def load_sessions(path: Path) -> list[dict[str, Any]]:
    return load_study_sessions_file(path)


def append_session(session: dict[str, Any]) -> Path:
    return append_study_session(session, ROOT)


def table_datetime(value: Any) -> datetime | None:
    """Normalize dates for display only; editor snapshots retain original ISO values."""
    from app_core.analysis_dates import parse_analysis_datetime

    return parse_analysis_datetime(value)


def date_from_source(value: Any) -> date | None:
    try:
        return datetime.strptime(Path(str(value)).stem, "%Y%m%d").date()
    except ValueError:
        return None


def load_study_df(master_name_lookup: dict[str, dict[Any, Any]] | None = None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for path in record_json_files(STUDY_DIR):
        for item in load_sessions(path):
            if isinstance(item, dict):
                row = dict(item)
                row["source_file"] = str(path)
                rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for key in ("start_at", "end_at"):
        values = df[key] if key in df.columns else pd.Series(None, index=df.index, dtype="object")
        df[key] = pd.to_datetime(values.map(table_datetime), errors="coerce")
    for key in ("duration_sec", "money", "hourly_rate"):
        values = df[key] if key in df.columns else pd.Series(None, index=df.index, dtype="object")
        df[key] = pd.to_numeric(values, errors="coerce").replace([float("inf"), float("-inf")], float("nan"))
    df["date"] = df["start_at"].dt.date
    missing_dates = df["date"].isna()
    df.loc[missing_dates, "date"] = df.loc[missing_dates, "source_file"].map(date_from_source)
    df["duration_hms"] = df["duration_sec"].map(sec_to_hms)
    df["money_display"] = df["money"].map(yen)
    df["memo"] = df["memo"].fillna("") if "memo" in df.columns else ""
    if "linked_note_ids" not in df.columns:
        df["linked_note_ids"] = [[] for _ in range(len(df))]
    if "linked_task_id" not in df.columns:
        df["linked_task_id"] = ""
    references = ReferenceSnapshot(ROOT)
    df["related_notes"] = df["linked_note_ids"].map(references.note_titles)
    df["related_task"] = df["linked_task_id"].map(references.task_title)
    for key in ("project_id", "subject_id", "material_id", "unit_id"):
        if key not in df.columns:
            df[key] = ""
    if master_name_lookup is None:
        master_name_lookup = build_master_name_lookup(load_master_data(ROOT))
    names = df.apply(lambda row: resolve_master_names_from_lookup(row.to_dict(), master_name_lookup), axis=1)
    df["project_name"] = names.map(lambda item: item["project_name"])
    df["subject_name"] = names.map(lambda item: item["subject_name"])
    df["material_name"] = names.map(lambda item: item["material_name"])
    df["unit_name"] = names.map(lambda item: item["unit_name"])
    return df.sort_values(["date", "start_at"], ascending=[False, False])


def load_diary_df() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for path in record_json_files(DIARY_DIR):
        data = read_json(path, {})
        records = data if isinstance(data, list) else [data]
        for item in records:
            if not isinstance(item, dict):
                continue
            date_value = item.get("date")
            if not date_value:
                try:
                    date_value = datetime.strptime(path.stem, "%Y%m%d").date().isoformat()
                except ValueError:
                    date_value = ""
            cost_records = item.get("cost_records", [])
            investment = 0
            if isinstance(cost_records, list):
                investment = sum(float(cost.get("amount", 0) or 0) for cost in cost_records if isinstance(cost, dict))
            caffeine_mg = item.get("caffeine_mg")
            if caffeine_mg in (None, "") and item.get("caffeine_cups") not in (None, ""):
                caffeine_mg = float(item.get("caffeine_cups", 0) or 0) * 100
            rows.append(
                {
                    "date": pd.to_datetime(date_value, errors="coerce").date() if date_value else None,
                    "diary_title": item.get("title", ""),
                    "diary_body": item.get("body", ""),
                    "mood": item.get("mood", ""),
                    "weather": item.get("weather", ""),
                    "caffeine_mg": caffeine_mg or 0,
                    "running_distance_km": item.get("running_distance_km", ""),
                    "running_time_min": item.get("running_time_min", ""),
                    "investment_amount": investment,
                }
            )
    return pd.DataFrame(rows)


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


@st.fragment
def render_manual_log_add() -> None:
    from app_core.study_entry_service import MANUAL_SUBMISSION_KEY, save_manual_entry

    manual_master_data = load_master_data(ROOT)
    with st.expander("手動で学習記録を追加", expanded=False):
        if st.session_state.get(MANUAL_SUBMISSION_KEY, {}).get("completed"):
            st.caption("保存済みと同じ内容の再送は、二重追加を防止します。内容を変えると新しい記録として追加します。")
            if st.button("同じ内容を別の記録として追加する準備", key="manual_allow_repeat"):
                st.session_state.pop(MANUAL_SUBMISSION_KEY, None)
                st.info("フォームの「追加する」で、別の記録として保存できます。")
        st.subheader("分類")
        manual_master_values = master_input_selects("manual_log", manual_master_data)
        st.caption("科目を選択すると、教材と単元の候補をその科目に絞り込みます。")
        with st.form("manual_log_form"):
            col1, col2, col3 = st.columns(3)
            target_date = col1.date_input("日付", value=app_today())
            start_time = col2.time_input("開始時刻", value=time(9, 0))
            end_time = col3.time_input("終了時刻", value=time(10, 0))
            memo = st.text_input("勉強内容")
            hourly_rate = st.number_input("適用時給", min_value=0, value=int(timer_setting("hourly_rate", 2000) or 2000), step=100)
            if overtime_enabled():
                st.caption(f"残業計算: その日の累計{overtime_threshold_hours():g}時間超を{overtime_multiplier():g}倍で換算します。")
            with st.expander("コンディション入力", expanded=False):
                effort = st.slider("頑張り度", 1, 10, 7)
                environment = st.selectbox("勉強環境", ["", "自宅", "カフェ", "図書館", "ワーキングスペース", "職場", "移動中"])
                sleep_hours = st.number_input("睡眠時間", min_value=0.0, max_value=24.0, value=7.0, step=0.5)
                alcohol_yesterday = st.selectbox("前日の飲酒", ["", "なし", "あり"])
                mood = st.selectbox("気分", ["", "1", "2", "3", "4", "5"])
                category = st.selectbox("勉強カテゴリ", study_category_options(ROOT, current=str(st.session_state.get("manual_study_category") or "")), key="manual_study_category")
                io_type = st.selectbox("インプット/アウトプット", ["", "インプット", "アウトプット", "両方"])
            submitted = st.form_submit_button("追加する", type="primary")

        if submitted:
            start_dt = datetime.combine(target_date, start_time)
            end_dt = datetime.combine(target_date, end_time)
            if end_dt <= start_dt:
                ab.show_action_error("終了時刻は開始時刻より後にしてください。")
            else:
                duration_sec = int((end_dt - start_dt).total_seconds())
                money_parts = calculate_manual_money(start_dt, duration_sec, hourly_rate)
                condition = clean_dict(
                    {
                        "effort": effort,
                        "environment": environment,
                        "sleep_hours": sleep_hours,
                        "alcohol_yesterday": alcohol_yesterday,
                        "mood": mood,
                        "category": category,
                        "io_type": io_type,
                    }
                )
                payload = clean_dict(
                    {
                        "start_at": start_dt.isoformat(timespec="seconds"),
                        "end_at": end_dt.isoformat(timespec="seconds"),
                        "duration_sec": duration_sec,
                        "money": money_parts["money"],
                        "normal_money": money_parts["normal_money"],
                        "overtime_money": money_parts["overtime_money"],
                        "hourly_rate": hourly_rate,
                        "money_policy": current_money_policy(),
                        "memo": memo,
                        "linked_note_ids": [],
                        "condition": condition,
                        **manual_master_values,
                    }
                )
                try:
                    saved, replayed = save_manual_entry(payload, st.session_state, ROOT)
                except (ValueError, OSError) as exc:
                    ab.show_action_error(f"手動ログを保存できませんでした。入力は保持しています。{exc}")
                    return
                st.session_state.last_saved_path = str(saved)
                ab.queue_success("この手動ログは保存済みです。二重追加は行いませんでした。" if replayed else "手動ログを追加しました。")
                st.rerun()


st.set_page_config(page_title="勉強ログ履歴＆明細", layout="wide")
STUDY_DIR.mkdir(parents=True, exist_ok=True)
DIARY_DIR.mkdir(parents=True, exist_ok=True)
ab.inject_css(settings=USER_SETTINGS)
ab.sidemenu("ログの絞り込みとCSV出力を行います。", settings=USER_SETTINGS)

master_data = load_master_data(ROOT)
master_name_lookup = build_master_name_lookup(master_data)
study_df = load_study_df(master_name_lookup)
diary_df = load_diary_df()

if not study_df.empty and not study_df["date"].dropna().empty:
    min_date = study_df["date"].dropna().min()
    max_date = study_df["date"].dropna().max()
else:
    min_date = app_today() - timedelta(days=7)
    max_date = app_today()

with st.sidebar:
    st.subheader("絞り込み")
    start_date = st.date_input("開始日", value=min_date)
    end_date = st.date_input("終了日", value=max_date)
    keyword = st.text_input("勉強内容キーワード")
    filter_master_values = master_filter_selects("log_filter", master_data)
    st.subheader("データ出力")
    if study_df.empty:
        st.info("ダウンロードできる勉強ログはまだありません。")
    else:
        merged_df = study_df.merge(diary_df, on="date", how="left") if not diary_df.empty else study_df.copy()
        st.download_button("全データ統合CSV", to_csv_bytes(merged_df), "all_study_and_diary_log.csv", "text/csv", on_click="ignore")

ab.topbar("勉強ログ履歴＆明細", "記録の検索・修正、手動追加、日記データとの統合出力を行います。")

render_master_quick_add(ROOT, "study_log_master", snapshot=MasterSnapshot(master_data))
render_manual_log_add()

filtered = study_df.copy()
if not study_df.empty:
    mask = ((study_df["date"] >= start_date) & (study_df["date"] <= end_date)) | study_df["date"].isna()
    if keyword:
        mask &= study_df["memo"].astype(str).str.contains(keyword, case=False, na=False, regex=False)
    for key, value in filter_master_values.items():
        if value:
            mask &= study_df[key] == value
    filtered = study_df.loc[mask].copy()

render_study_log_management(filtered.to_dict(orient="records"), root=ROOT, fallback_policy=current_money_policy())

if study_df.empty:
    st.info("まだ勉強ログがありません。秒給カウンターか手動追加から記録を始められます。")
    st.stop()

st.subheader("日別サマリ")
if filtered.empty:
    st.warning("条件に一致するログがありません。")
else:
    summary = (
        filtered.groupby("date", as_index=False)
        .agg(
            duration_sec=("duration_sec", "sum"),
            money=("money", "sum"),
            sessions=("duration_sec", "count"),
            project_name=("project_name", lambda s: " / ".join(sorted({x for x in s.astype(str) if x and x != "未設定"}))),
            subject_name=("subject_name", lambda s: " / ".join(sorted({x for x in s.astype(str) if x and x != "未設定"}))),
            material_name=("material_name", lambda s: " / ".join(sorted({x for x in s.astype(str) if x and x != "未設定"}))),
            unit_name=("unit_name", lambda s: " / ".join(sorted({x for x in s.astype(str) if x and x != "未設定"}))),
            memo=("memo", lambda s: " / ".join(sorted({x for x in s.astype(str) if x}))),
        )
        .sort_values("date", ascending=False)
    )
    summary["総勉強時間"] = summary["duration_sec"].map(sec_to_hms)
    summary["総換算金額"] = summary["money"].map(yen)
    display_summary = summary[["date", "総勉強時間", "総換算金額", "sessions", "project_name", "subject_name", "material_name", "unit_name", "memo"]].rename(columns={"date": "日付", "sessions": "セッション数", "project_name": "プロジェクト", "subject_name": "科目", "material_name": "教材", "unit_name": "単元", "memo": "勉強内容"})
    st.dataframe(display_summary, width="stretch", hide_index=True)
    st.download_button("日別サマリCSV", to_csv_bytes(summary), "daily_study_summary.csv", "text/csv", on_click="ignore")

    st.subheader("セッション一覧")
    detail = filtered[["date", "duration_hms", "money_display", "project_name", "subject_name", "material_name", "unit_name", "memo", "related_task", "related_notes", "start_at", "end_at", "hourly_rate", "source_file"]].rename(
        columns={"date": "日付", "duration_hms": "勉強時間", "money_display": "換算金額", "project_name": "プロジェクト", "subject_name": "科目", "material_name": "教材", "unit_name": "単元", "memo": "内容", "related_task": "関連タスク", "related_notes": "関連ノート", "start_at": "開始", "end_at": "終了", "hourly_rate": "時給", "source_file": "保存元"}
    )
    st.dataframe(detail, width="stretch", hide_index=True)
    st.download_button("セッション明細CSV", to_csv_bytes(filtered), "study_session_detail.csv", "text/csv", on_click="ignore")

with st.expander("デバッグ情報", expanded=False):
    st.write({"loaded_rows": len(study_df), "filtered_rows": len(filtered), "last_saved_path": st.session_state.get("last_saved_path", "")})
