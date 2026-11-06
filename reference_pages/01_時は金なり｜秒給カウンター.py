from __future__ import annotations

from web_demo.runtime import fragment as demo_fragment

import time
from datetime import date, datetime
from app_core.app_clock import app_now, app_today
from pathlib import Path
from typing import Any

import streamlit as st

import prolog_ui.components as ab
from app_core.health_service import get_health_log_path, load_health_log
from app_core.master_snapshot import MasterSnapshot
from app_core.json_store import record_json_files
from app_core.settings_service import load_user_settings
from app_core.study_log_service import append_study_session, calculate_session_money_with_daily_total, reserved_study_session_ids
from config import STUDY_LOGS_DIR
from master_ui import render_master_quick_add
from app_core.study_category_service import study_category_options
from prolog_ui.timer_miniwindow import render_timer_miniwindow
from data_utils import (
    create_note_from_session,
    generate_id,
    link_notes_to_session,
    link_session_to_plan_task,
    load_all_notes,
    load_daily_plan,
    load_study_sessions_file,
)


from config import get_data_root

ROOT = get_data_root()


def sec_to_hms(seconds: int | float) -> str:
    total = max(int(seconds or 0), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def minutes_text(seconds: int | float) -> str:
    total_min = max(int(round(float(seconds or 0) / 60)), 0)
    h, m = divmod(total_min, 60)
    return f"{h}時間{m:02d}分" if h else f"{m}分"


def yen(value: int | float) -> str:
    return f"{int(value or 0):,}円"


def clean_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, "", [])}


def calc_money(
    seconds: float,
    hourly_rate: float,
    overtime_enabled: bool,
    threshold_hours: float,
    multiplier: float,
    start_at: datetime | None = None,
    *,
    sessions: list[dict[str, Any]] | None = None,
) -> tuple[float, float, float]:
    session_start = start_at or app_now()
    session = {
        "start_at": session_start.isoformat(timespec="seconds"),
        "duration_sec": seconds,
        "hourly_rate": hourly_rate,
    }
    money = calculate_session_money_with_daily_total(
        session,
        sessions if sessions is not None else load_sessions(daily_path(session_start)),
        hourly_rate=hourly_rate,
        overtime_enabled=overtime_enabled,
        threshold_hours=threshold_hours,
        multiplier=multiplier,
    )
    return money["money"], money["normal_money"], money["overtime_money"]


def study_log_path(target: date | datetime) -> Path:
    target_date = target.date() if isinstance(target, datetime) else target
    return STUDY_LOGS_DIR / f"{target_date:%Y}" / f"{target_date:%m}" / f"{target_date:%Y%m%d}.json"


def daily_path(start_at: datetime) -> Path:
    return study_log_path(start_at)


def load_sessions(path: Path) -> list[dict[str, Any]]:
    return load_study_sessions_file(path)


def load_day_totals(target: date, *, sessions: list[dict[str, Any]] | None = None) -> tuple[float, float, int]:
    if sessions is None:
        sessions = load_sessions(study_log_path(target))
    total_sec = sum(float(item.get("duration_sec", 0) or 0) for item in sessions if isinstance(item, dict))
    total_money = sum(float(item.get("money", 0) or 0) for item in sessions if isinstance(item, dict))
    return total_sec, total_money, len(sessions)


def get_daily_sleep_hours(target: date) -> float | None:
    if not get_health_log_path(target).exists():
        return None
    health = load_health_log(target)
    try:
        minutes = float(health.get("sleep", {}).get("sleep_duration_min", 0) or 0)
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    return round(minutes / 60, 2)


def load_all_totals() -> tuple[float, float]:
    total_sec = 0.0
    total_money = 0.0
    for path in record_json_files(STUDY_LOGS_DIR):
        for item in load_sessions(path):
            if isinstance(item, dict):
                total_sec += float(item.get("duration_sec", 0) or 0)
                total_money += float(item.get("money", 0) or 0)
    return total_sec, total_money


def ensure_session_id(session: dict[str, Any]) -> None:
    start_at = datetime.fromisoformat(session["start_at"])
    path = daily_path(start_at)
    existing_ids = reserved_study_session_ids(ROOT) | {item.get("session_id", "") for item in load_sessions(path) if isinstance(item, dict)}
    if not session.get("session_id"):
        session["session_id"] = generate_id("session", session["start_at"], existing_ids)
    session["linked_note_ids"] = session.get("linked_note_ids") or []


def append_session(session: dict[str, Any]) -> Path:
    return append_study_session(session, ROOT)


def note_template(session: dict[str, Any]) -> str:
    duration_min = round(float(session.get("duration_sec", 0) or 0) / 60, 1)
    start_at = datetime.fromisoformat(session["start_at"])
    return (
        "# 学習メモ\n\n"
        "## 学習内容\n\n\n"
        "## 理解できたこと\n\n\n"
        "## 不明点\n\n\n"
        "## 次回復習したいこと\n\n\n"
        "## 関連セッション\n\n"
        f"- session_id: {session.get('session_id', '')}\n"
        f"- 学習時間: {duration_min}分\n"
        f"- 記録日: {start_at:%Y-%m-%d}\n"
    )


def master_options(items: list[dict[str, Any]], id_key: str) -> dict[str, str]:
    options = {"未設定": ""}
    for item in items:
        options[f"{item.get('name', '名称未設定')} [{item.get(id_key, '')}]"] = item.get(id_key, "")
    return options


def selected_label(options: dict[str, str], value: str | None) -> str:
    for label, item_id in options.items():
        if item_id == (value or ""):
            return label
    return next(iter(options))


def ensure_selectbox_value(key: str, options: dict[str, str]) -> None:
    if key in st.session_state and st.session_state[key] not in options:
        del st.session_state[key]


def select_master_label(label: str, options: dict[str, str], key: str, value: str | None) -> str:
    ensure_selectbox_value(key, options)
    labels = list(options.keys())
    default_label = selected_label(options, value)
    default_index = labels.index(default_label) if default_label in labels else 0
    return st.selectbox(label, labels, index=default_index, key=key)


def master_selects(prefix: str, record: dict[str, Any], snapshot: MasterSnapshot) -> dict[str, str]:
    project_options = master_options(snapshot.active("projects"), "project_id")
    project_label = select_master_label("プロジェクト", project_options, f"{prefix}_project", record.get("project_id"))
    project_id = project_options[project_label]

    subject_options = master_options(snapshot.active("subjects", project_id=project_id or None), "subject_id")
    subject_label = select_master_label("科目", subject_options, f"{prefix}_subject", record.get("subject_id"))
    subject_id = subject_options[subject_label]

    material_items = snapshot.active("materials", project_id=project_id or None, subject_id=subject_id or None) if subject_id else []
    material_options = master_options(material_items, "material_id")
    material_label = select_master_label("教材", material_options, f"{prefix}_material", record.get("material_id"))
    material_id = material_options[material_label]

    unit_items = snapshot.active("units", project_id=project_id or None, subject_id=subject_id or None, material_id=material_id or None) if material_id else []
    unit_options = master_options(unit_items, "unit_id")
    unit_label = select_master_label("単元", unit_options, f"{prefix}_unit", record.get("unit_id"))
    return {"project_id": project_id, "subject_id": subject_id, "material_id": material_id, "unit_id": unit_options[unit_label]}


def init_state() -> None:
    defaults = {
        "running": False,
        "miniwindow_control_revision": 0,
        "start_perf": None,
        "start_at": None,
        "accumulated_sec": 0.0,
        "display_adjust_sec": 0,
        "pending_session": None,
        "goal_toasted": False,
        "last_interleaving_index": 0,
        "selected_plan_task_id": "",
        "selected_plan_task_date": "",
        "condition_sleep_hours_source_date": None,
        "condition_sleep_hours_source_value": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def init_timer_settings(settings: dict[str, Any]) -> None:
    timer = settings.get("timer", {})
    defaults = {
        "hourly_rate": int(timer.get("hourly_rate", 2000)),
        "overtime_enabled": bool(timer.get("overtime_enabled", True)),
        "overtime_multiplier": float(timer.get("overtime_multiplier", 1.25)),
        "overtime_threshold": float(timer.get("overtime_threshold_hours", 8.0)),
        "goal_minutes": int(timer.get("goal_minutes", 120)),
        "interleaving_enabled": bool(timer.get("interleaving_enabled", True)),
        "interleaving_minutes": int(timer.get("interleaving_minutes", 120)),
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def init_daily_condition_defaults() -> float | None:
    today_key = app_today().isoformat()
    source_value = get_daily_sleep_hours(app_today())
    previous_source_value = st.session_state.get("condition_sleep_hours_source_value")
    should_apply = (
        st.session_state.get("condition_sleep_hours_source_date") != today_key
        or "condition_sleep_hours" not in st.session_state
        or st.session_state.get("condition_sleep_hours") == previous_source_value
    )
    if source_value is not None and should_apply:
        st.session_state.condition_sleep_hours = source_value
    elif source_value is None:
        st.session_state.setdefault("condition_sleep_hours", 7.0)
    st.session_state.condition_sleep_hours_source_date = today_key
    st.session_state.condition_sleep_hours_source_value = source_value
    return source_value


def current_elapsed() -> float:
    elapsed = float(st.session_state.accumulated_sec)
    if st.session_state.running and st.session_state.start_perf is not None:
        elapsed += time.perf_counter() - float(st.session_state.start_perf)
    return max(elapsed + int(st.session_state.display_adjust_sec), 0)


def freeze_pending() -> None:
    duration = int(current_elapsed())
    if duration <= 0 and not st.session_state.pending_session:
        return
    now = app_now()
    start_at = st.session_state.start_at or now
    total_money, normal_money, overtime_money = calc_money(
        duration,
        st.session_state.hourly_rate,
        st.session_state.overtime_enabled,
        st.session_state.overtime_threshold,
        st.session_state.overtime_multiplier,
        start_at,
    )
    previous = st.session_state.pending_session or {}
    st.session_state.pending_session = {
        "start_at": start_at.isoformat(timespec="seconds"),
        "end_at": now.isoformat(timespec="seconds"),
        "duration_sec": int(duration),
        "money": round(total_money),
        "normal_money": round(normal_money),
        "overtime_money": round(overtime_money),
        "hourly_rate": st.session_state.hourly_rate,
        "linked_task_id": st.session_state.get("selected_plan_task_id") or "",
        "linked_task_date": (st.session_state.get("selected_plan_task_date") or previous.get("linked_task_date") or start_at.date().isoformat()) if st.session_state.get("selected_plan_task_id") else "",
        **{key: previous[key] for key in ("session_id", "_save_operation_id") if key in previous},
    }


def update_pending_duration(duration: float) -> None:
    """Update the unsaved duration and price without changing its timestamps."""
    session = dict(st.session_state.pending_session)
    duration = max(int(duration), 0)
    total_money, normal_money, overtime_money = calc_money(
        duration,
        session.get("hourly_rate", st.session_state.hourly_rate),
        st.session_state.overtime_enabled,
        st.session_state.overtime_threshold,
        st.session_state.overtime_multiplier,
        datetime.fromisoformat(session["start_at"]),
    )
    session.update({
        "duration_sec": duration,
        "money": round(total_money),
        "normal_money": round(normal_money),
        "overtime_money": round(overtime_money),
    })
    st.session_state.pending_session = session


def adjust_elapsed(delta: int) -> None:
    if not st.session_state.running and st.session_state.pending_session:
        paused = st.session_state.start_at is not None
        elapsed = current_elapsed() if paused else float(st.session_state.pending_session["duration_sec"])
        duration = max(elapsed + delta, 0.0)
        update_pending_duration(duration)
        st.session_state.accumulated_sec = duration if paused else 0.0
        st.session_state.display_adjust_sec = 0
        return

    # Fold any adjustment into the elapsed time once, without negative debt.
    st.session_state.accumulated_sec = max(current_elapsed() + delta, 0.0)
    st.session_state.display_adjust_sec = 0
    if st.session_state.running:
        st.session_state.start_perf = time.perf_counter()
    elif st.session_state.start_at is not None:
        freeze_pending()


def render_realtime() -> None:
    elapsed = int(current_elapsed())
    start_at = st.session_state.start_at or datetime.combine(app_today(), app_now().time())
    # Share only within this tick; the next tick and saving still read afresh.
    saved_sessions = load_sessions(daily_path(start_at))
    current_money, normal_money, overtime_money = calc_money(
        elapsed,
        st.session_state.hourly_rate,
        st.session_state.overtime_enabled,
        st.session_state.overtime_threshold,
        st.session_state.overtime_multiplier,
        start_at,
        sessions=saved_sessions,
    )
    current_money = round(current_money)
    normal_money = round(normal_money)
    overtime_money = round(overtime_money)
    saved_sec, saved_money, session_count = load_day_totals(start_at.date(), sessions=saved_sessions)
    today_total_sec = saved_sec + elapsed
    today_total_money = saved_money + current_money
    goal_sec = max(int(st.session_state.goal_minutes) * 60, 1)
    progress = min(today_total_sec / goal_sec, 1.0)

    if progress >= 1 and not st.session_state.goal_toasted:
        st.toast("今日の目標時間に到達しました。")
        st.session_state.goal_toasted = True

    if st.session_state.interleaving_enabled:
        interval = max(int(st.session_state.interleaving_minutes) * 60, 1)
        current_index = int(elapsed // interval)
        if elapsed > 0 and current_index > st.session_state.last_interleaving_index:
            st.session_state.last_interleaving_index = current_index
            st.toast("トピック切り替えのタイミングです。")

    cols = st.columns(4)
    with cols[0]:
        ab.metric_block("現在セッション", sec_to_hms(elapsed), "画面上の計測時間")
    with cols[1]:
        ab.metric_block("今日の累計勉強時間", sec_to_hms(today_total_sec), f"保存済み {sec_to_hms(saved_sec)} / {session_count}件")
    with cols[2]:
        ab.metric_block("現在セッション換算", yen(current_money), f"通常 {yen(normal_money)} / 残業 {yen(overtime_money)}")
    with cols[3]:
        ab.metric_block("今日の換算金額", yen(today_total_money), f"目標達成率 {progress * 100:.1f}%")
    st.progress(progress)


@demo_fragment(run_every="1s")
def ticking_realtime() -> None:
    render_realtime()
    # Keep one component identity across start/pause and refresh paused prices too.
    command = render_timer_miniwindow(
        current_elapsed(), bool(st.session_state.running),
        control_revision=st.session_state.miniwindow_control_revision,
        can_resume=not st.session_state.running and st.session_state.start_at is not None,
    )
    if apply_miniwindow_control(command):
        # The controls and unsaved form live outside this one-second fragment.
        st.rerun()


def apply_estimated_rate(rate: int) -> None:
    st.session_state.hourly_rate = rate


def render_timer_settings() -> None:
    with st.expander("タイマー設定", expanded=False):
        st.caption("通常使う初期値はユーザー設定ページで変更できます。この画面では今回の計測に使う値を調整します。")
        calc_col, setting_col = st.columns([1, 2])
        with calc_col:
            st.markdown("##### 時給換算")
            monthly_salary = st.number_input("月給", min_value=0, value=300000, step=10000, key="calc_monthly_salary")
            work_days = st.number_input("月間稼働日数", min_value=1, value=20, step=1, key="calc_work_days")
            work_hours = st.number_input("1日の労働時間", min_value=1.0, value=8.0, step=0.5, key="calc_work_hours")
            estimated_rate = monthly_salary / work_days / work_hours
            st.caption(f"目安時給: {yen(estimated_rate)}")
            st.button("目安時給を適用", key="apply_estimated_rate", on_click=apply_estimated_rate, args=(int(estimated_rate),))
        with setting_col:
            st.markdown("##### 計測設定")
            c1, c2, c3 = st.columns(3)
            c1.number_input("適用時給", min_value=0, step=100, key="hourly_rate")
            c2.number_input("今日の目標時間（分）", min_value=1, step=15, key="goal_minutes")
            c3.number_input("残業閾値（時間）", min_value=0.5, step=0.5, key="overtime_threshold")
            c4, c5, c6 = st.columns(3)
            c4.toggle("残業計算を有効化", key="overtime_enabled")
            c5.number_input("残業倍率", min_value=1.0, step=0.05, key="overtime_multiplier")
            c6.number_input("警報周期（分）", min_value=5, step=5, key="interleaving_minutes")
            st.toggle("インタリービング警報を有効化", key="interleaving_enabled")


def render_plan_task_selection() -> dict[str, Any]:
    """Retain the selected task's owning day through midnight and reruns."""
    today = app_today()
    current_id = st.session_state.get("selected_plan_task_id", "")
    pending = st.session_state.get("pending_session") or {}
    owner = st.session_state.get("selected_plan_task_date") or pending.get("linked_task_date") or today.isoformat()
    task_options = {"選択しない": ("", "")}
    task_records = {}
    for owner_day in dict.fromkeys([today.isoformat()] + ([owner] if current_id else [])):
        plan = load_daily_plan(ROOT, date.fromisoformat(owner_day))
        for task in plan.get("tasks", []):
            task_id = task.get("task_id", "")
            if not task_id or (owner_day != today.isoformat() and task_id != current_id):
                continue
            identity = (task_id, owner_day)
            label = f"{task.get('title', '無題のタスク')} / {task.get('planned_minutes', 0)}分 [{task_id}]"
            if owner_day != today.isoformat():
                label = f"{owner_day} / {label}"
            task_options[label] = identity
            task_records[identity] = task
    current = (current_id, owner) if current_id else ("", "")
    if current_id and current not in task_records:
        task_options[f"選択中（削除・無効の可能性） / {owner} [{current_id}]"] = current
    selected_label = st.selectbox("今日の記録・計画からタスクを選択", list(task_options), index=list(task_options.values()).index(current))
    selected_id, selected_day = task_options[selected_label]
    st.session_state.selected_plan_task_id = selected_id
    st.session_state.selected_plan_task_date = selected_day
    if selected_day and selected_day != today.isoformat():
        st.caption(f"選択中タスクの所属日: {selected_day}（保存もこの日の計画へ連携します）")
    return task_records.get((selected_id, selected_day), {})


def render_session_context(snapshot: MasterSnapshot) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    daily_sleep = init_daily_condition_defaults()
    with st.expander("このセッションの記録内容", expanded=True):
        selected_task = render_plan_task_selection()
        if selected_task:
            names = snapshot.names(selected_task)
            st.caption(f"分類: {names['project_name']} / {names['subject_name']} / {names['material_name']} / {names['unit_name']}")

        st.markdown("##### 分類")
        st.caption("必要な分類がない場合は、この画面から簡易追加できます。")
        render_master_quick_add(ROOT, "timer_master_quick_add", snapshot=snapshot)
        master_values = master_selects(f"timer_master_{st.session_state.selected_plan_task_id or 'none'}", selected_task or {}, snapshot)

        st.markdown("##### コンディション")
        c1, c2, c3, c4 = st.columns(4)
        effort = c1.slider("頑張り度", 1, 10, 7, key="condition_effort")
        environment = c2.selectbox("勉強環境", ["", "自宅", "カフェ", "図書館", "ワーキングスペース", "職場", "移動中"], key="condition_environment")
        sleep_hours = c3.number_input("睡眠時間", min_value=0.0, max_value=24.0, step=0.25, key="condition_sleep_hours")
        if daily_sleep is not None:
            c3.caption("睡眠・体調ログから初期入力")
        else:
            c3.caption("睡眠・体調ログ未記録時は手入力")
        alcohol_yesterday = c4.selectbox("前日の飲酒", ["", "なし", "あり"], key="condition_alcohol")
        c5, c6, c7 = st.columns(3)
        mood = c5.selectbox("気分", ["", "1", "2", "3", "4", "5"], key="condition_mood")
        category = c6.selectbox("勉強カテゴリ", study_category_options(ROOT, current=str(st.session_state.get("condition_category") or "")), key="condition_category")
        io_type = c7.selectbox("インプット/アウトプット", ["", "インプット", "アウトプット", "両方"], key="condition_io_type")

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
    return selected_task, master_values, condition


def advance_timer_control_revision() -> None:
    st.session_state.miniwindow_control_revision += 1


def apply_miniwindow_control(command: Any) -> bool:
    """Apply an explicit command only to the timer state the small window saw."""
    if not isinstance(command, dict):
        return False
    revision = command.get("revision")
    if type(revision) is not int or revision != st.session_state.miniwindow_control_revision:
        return False
    if command.get("action") == "pause" and st.session_state.running:
        pause_timer()
        return True
    if command.get("action") == "resume" and not st.session_state.running and st.session_state.start_at is not None:
        start_timer()
        return bool(st.session_state.running)
    return False


def start_timer() -> None:
    if st.session_state.running or (st.session_state.pending_session and st.session_state.start_at is None):
        return
    st.session_state.running = True
    st.session_state.start_perf = time.perf_counter()
    st.session_state.start_at = st.session_state.start_at or app_now()
    advance_timer_control_revision()


def pause_timer() -> None:
    if not st.session_state.running:
        return
    st.session_state.accumulated_sec = current_elapsed()
    st.session_state.display_adjust_sec = 0
    st.session_state.running = False
    st.session_state.start_perf = None
    freeze_pending()
    advance_timer_control_revision()


def reset_timer() -> None:
    if current_elapsed() > 0 or (st.session_state.start_at is not None and st.session_state.pending_session):
        freeze_pending()
    st.session_state.running = False
    st.session_state.start_perf = None
    st.session_state.start_at = None
    st.session_state.accumulated_sec = 0.0
    st.session_state.display_adjust_sec = 0
    advance_timer_control_revision()


def render_controls() -> None:
    st.subheader("コントロール")
    reset_pending = bool(st.session_state.pending_session) and st.session_state.start_at is None
    if reset_pending:
        st.caption("未保存セッションを保存または破棄してから、新しい計測を開始してください。±60秒は未保存セッションを補正します。")
    # Callbacks run before the page redraw, preserving later input widgets.
    c1, c2, c3, c4 = st.columns(4)
    c1.button("開始", type="primary", disabled=st.session_state.running or reset_pending, on_click=start_timer, width="stretch")
    c2.button("一時停止", disabled=not st.session_state.running, on_click=pause_timer, width="stretch")
    c3.button("リセット", on_click=reset_timer, width="stretch")
    with c4:
        left, right = st.columns(2)
        left.button("-60秒", on_click=adjust_elapsed, args=(-60,), width="stretch")
        right.button("+60秒", on_click=adjust_elapsed, args=(60,), width="stretch")


def render_pending_save(master_values: dict[str, str], condition: dict[str, Any]) -> None:
    if not st.session_state.pending_session:
        return
    running = st.session_state.running
    if running:
        st.info("計測を再開しています。一時停止すると、最新の時間を保存・破棄できます。")
    else:
        duration = current_elapsed() if st.session_state.start_at is not None else st.session_state.pending_session["duration_sec"]
        update_pending_duration(duration)

    with st.expander("未保存セッション", expanded=True):
        session = dict(st.session_state.pending_session)
        ab.metric_block("保存予定時間", sec_to_hms(session.get("duration_sec", 0)), f"換算金額 {yen(session.get('money', 0))}")
        if running:
            st.caption("この欄は前回の一時停止時点です。入力内容は保持したまま、保存・破棄を一時的に無効にしています。")
        else:
            st.caption("±60秒の補正は、この保存予定時間と換算金額にも反映されます。")
        if session["duration_sec"] <= 0:
            st.info("保存予定時間が0秒です。時間を加算するか、このセッションを破棄してください。")
        memo = st.text_area("勉強内容・メモ", value="", key="pending_memo")
        notes = load_all_notes(ROOT)
        note_options = {f"{note['title']} [{note['note_id']}]": note["note_id"] for note in notes}
        selected_note_labels = st.multiselect("関連ノート", options=list(note_options.keys()), key="pending_notes")
        create_note = st.checkbox("このセッションから新規ノートを作成", key="pending_create_note")
        default_title_date = datetime.fromisoformat(session["start_at"]).strftime("%Y-%m-%d")
        default_note_title = f"{default_title_date} {memo or condition.get('category') or '学習メモ'}"
        new_note_title = ""
        if create_note:
            new_note_title = st.text_input("新規ノートタイトル", value=default_note_title, key="pending_new_note_title")

        save_col, discard_col = st.columns(2)
        if save_col.button("保存する", type="primary", disabled=running or session["duration_sec"] <= 0, width="stretch"):
            from uuid import uuid4
            from app_core.json_operations import atomic_json_operation

            if running or session["duration_sec"] <= 0:
                return
            session_start = datetime.fromisoformat(session["start_at"])
            total_money, normal_money, overtime_money = calc_money(
                session.get("duration_sec", 0),
                session.get("hourly_rate", st.session_state.hourly_rate),
                st.session_state.overtime_enabled,
                st.session_state.overtime_threshold,
                st.session_state.overtime_multiplier,
                session_start,
            )
            session["money"] = round(total_money)
            session["normal_money"] = round(normal_money)
            session["overtime_money"] = round(overtime_money)
            session["money_policy"] = {
                "overtime_enabled": bool(st.session_state.overtime_enabled),
                "threshold_hours": float(st.session_state.overtime_threshold),
                "multiplier": float(st.session_state.overtime_multiplier),
            }
            session.update(clean_dict({"memo": memo, "condition": condition}))
            session.update(master_values)
            operation_id = st.session_state.pending_session.setdefault("_save_operation_id", uuid4().hex)
            session.pop("_save_operation_id", None)
            saved_path = daily_path(session_start)
            try:
                # The operation is outermost: it stages every participating file
                # before acquiring commit locks, then journals/rolls back as a set.
                with atomic_json_operation(root=ROOT, kind="study_append", operation_id=operation_id) as operation:
                    if not operation.already_committed:
                        if any(label not in note_options for label in selected_note_labels):
                            raise ValueError("選択した関連ノートを確認できません。入力を残したまま、関連ノートを選び直してください。")
                        linked_note_ids = [note_options[label] for label in selected_note_labels]
                        if linked_note_ids:
                            live_note_ids = {note["note_id"] for note in load_all_notes(ROOT)}
                            if set(linked_note_ids) - live_note_ids:
                                raise ValueError("選択した関連ノートが別の操作で削除されています。関連ノートを選び直してから保存してください。")
                        ensure_session_id(session)
                        st.session_state.pending_session["session_id"] = session["session_id"]
                        if create_note:
                            note_tags = [tag for tag in [condition.get("category"), "勉強ログ由来"] if tag]
                            created_note_id = create_note_from_session(
                                ROOT,
                                session,
                                new_note_title or default_note_title,
                                note_tags,
                                note_template(session),
                            )
                            linked_note_ids.append(created_note_id)
                        session["linked_note_ids"] = sorted(set(linked_note_ids))
                        session["linked_task_id"] = st.session_state.get("selected_plan_task_id", session.get("linked_task_id", ""))
                        session["linked_task_date"] = (st.session_state.get("selected_plan_task_date") or session.get("linked_task_date") or session_start.date().isoformat()) if session["linked_task_id"] else ""
                        link_notes_to_session(ROOT, session["linked_note_ids"], session["session_id"])
                        link_session_to_plan_task(ROOT, session.get("linked_task_id"), session["session_id"], target_date=session.get("linked_task_date") or None, require_existing=True)
                        saved_path = append_session(session)
            except (OSError, ValueError) as exc:
                ab.show_action_error(f"セッションを保存できませんでした。入力は保持しています。{exc}")
                return
            st.session_state.pending_session = None
            st.session_state.running = False
            st.session_state.start_perf = None
            st.session_state.accumulated_sec = 0.0
            st.session_state.display_adjust_sec = 0
            st.session_state.start_at = None
            st.session_state.last_saved_path = str(saved_path)
            advance_timer_control_revision()
            ab.queue_success("セッションを保存しました。")
            st.rerun()
        if discard_col.button("破棄する", disabled=running, width="stretch"):
            if running:
                return
            st.session_state.pending_session = None
            st.session_state.running = False
            st.session_state.start_perf = None
            st.session_state.accumulated_sec = 0.0
            st.session_state.display_adjust_sec = 0
            st.session_state.start_at = None
            advance_timer_control_revision()
            st.rerun()


@demo_fragment
def render_session_entry() -> None:
    # Load once per fragment run, including selection changes; never cache JSON.
    # Saving lives in this fragment so it uses the current selection values,
    # rather than return values frozen at the last full-page render.
    # Settings share this fragment so pending money updates immediately too.
    render_timer_settings()
    snapshot = MasterSnapshot.load(ROOT)
    _selected_task, master_values, condition = render_session_context(snapshot)
    render_pending_save(master_values, condition)


st.set_page_config(page_title="秒給カウンター", layout="wide")
STUDY_LOGS_DIR.mkdir(parents=True, exist_ok=True)
init_state()
USER_SETTINGS = load_user_settings()
init_timer_settings(USER_SETTINGS)

ab.inject_css(settings=USER_SETTINGS)
sidebar_settings = ab.sidemenu("勉強セッション中の計測と記録を行います。", settings=USER_SETTINGS)

total_sec, total_money = load_all_totals()
with st.sidebar:
    st.subheader("学習サマリー")
    st.metric("全期間の総勉強時間", sec_to_hms(total_sec))
    st.metric("全期間の総換算金額", yen(total_money))
    ab.sidebar_page_link("pages/09_今日の学習プラン.py", label="今日の記録・計画を開く", settings=sidebar_settings)

ab.topbar("秒給カウンター", "勉強セッション中の時間、価値換算、分類、コンディションを記録します。")

ticking_realtime()

render_controls()
render_session_entry()

with st.expander("利用上の注意", expanded=False):
    st.write("一時停止またはリセット後、未保存セッションとして保存・破棄を選択できます。保存するまでは勉強ログには反映されません。")
    st.write("±60秒は計測中の時間、または停止・リセット後の未保存セッションを補正します。保存予定時間と換算金額にも反映されます。保存済みの過去ログは変更しません。")
