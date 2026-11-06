from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta
from app_core.app_clock import app_today
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.countdown_service import get_countdown
from app_core.daily_save_service import save_daily_records
from app_core.daily_task_service import save_task_edit
from app_core.form_snapshot import advance_form_snapshots_after_own_patch, discard_form_snapshot, form_snapshot
from app_core.json_store import observed_json_revision, resolved_json_path
from app_core.diary_service import get_diary_entry_path, investment_total, load_diary_entry, upsert_daily_hub_cost
from app_core.exercise_service import (
    calculate_running_pace,
    get_exercise_log_path,
    load_exercise_log,
)
from app_core.health_service import calculate_sleep_duration_minutes, get_health_log_path, load_health_log
from app_core.settings_service import load_user_settings, should_show_task_metrics
from app_core.master_snapshot import MasterSnapshot
from master_ui import render_master_quick_add
from data_utils import (
    daily_plan_path,
    generate_id,
    load_all_flashcards,
    load_all_daily_plans,
    load_all_notes,
    load_all_study_sessions,
    load_daily_plan,
    save_daily_plan,
)


ROOT = Path(__file__).resolve().parents[1]
MASTER_SNAPSHOT = MasterSnapshot.load(ROOT)
STATUS_LABELS = {
    "not_started": "未着手",
    "in_progress": "進行中",
    "done": "完了",
    "pending": "保留",
}
LABEL_TO_STATUS = {label: key for key, label in STATUS_LABELS.items()}


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def sec_to_hms(seconds: int | float) -> str:
    total = max(int(seconds or 0), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def yen(value: int | float) -> str:
    return f"{int(value or 0):,}円"


def session_rows(
    session_ids: list[str], sessions_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    wanted = list(dict.fromkeys(session_id for session_id in session_ids if session_id))
    rows = []
    if not wanted:
        return rows
    sessions = sessions_by_id if sessions_by_id is not None else {
        session.get("session_id"): session for session in load_all_study_sessions(ROOT)
    }
    for session_id in wanted:
        session = sessions.get(session_id)
        if not session:
            rows.append(
                {
                    "日付": "",
                    "開始": "",
                    "終了": "",
                    "学習時間": "",
                    "内容": "不明なセッション",
                    "換算金額": "",
                    "_sort_started_at": pd.NaT,
                }
            )
            continue
        start_at = pd.to_datetime(session.get("start_at"), errors="coerce")
        end_at = pd.to_datetime(session.get("end_at"), errors="coerce")
        rows.append(
            {
                "日付": "" if pd.isna(start_at) else start_at.date().isoformat(),
                "開始": "" if pd.isna(start_at) else start_at.strftime("%H:%M:%S"),
                "終了": "" if pd.isna(end_at) else end_at.strftime("%H:%M:%S"),
                "学習時間": sec_to_hms(session.get("duration_sec", 0)),
                "内容": session.get("memo", ""),
                "換算金額": yen(session.get("money", 0)),
                "_sort_started_at": start_at,
            }
        )
    return (
        pd.DataFrame(rows)
        .sort_values("_sort_started_at", ascending=False, kind="stable", na_position="last")
        .drop(columns="_sort_started_at")
        .to_dict(orient="records")
    )


def note_title_map(notes: list[dict[str, Any]] | None = None) -> dict[str, str]:
    return {note["note_id"]: note["title"] for note in (load_all_notes(ROOT) if notes is None else notes)}


def card_title(value: list[str], empty: str = "未設定") -> str:
    return " / ".join(value) if value else empty


def task_actual_seconds(task: dict[str, Any], sessions_by_id: dict[str, dict[str, Any]]) -> int:
    total = 0
    for session_id in task.get("linked_session_ids", []):
        session = sessions_by_id.get(session_id)
        if session:
            total += int(float(session.get("duration_sec", 0) or 0))
    return total


def format_minutes(seconds: int | float) -> str:
    return f"{round(float(seconds or 0) / 60, 1)}分"


def note_titles(note_ids: list[str], notes_by_id: dict[str, str]) -> list[str]:
    return [notes_by_id.get(note_id, "不明なノート") for note_id in note_ids]


def plan_task_ids(plan: dict[str, Any]) -> set[str]:
    return {task.get("task_id", "") for task in plan.get("tasks", []) if task.get("task_id")}


def save_plan_and_advance_tasks(plan: dict[str, Any]) -> str | None:
    path = daily_plan_path(ROOT, plan["date"])
    before = observed_json_revision(path)
    if any(task.get("roadmap_allocation_id") for task in plan.get("tasks", [])):
        from app_core.roadmap_service import save_linked_daily_plan
        save_linked_daily_plan(ROOT, plan, expected_revision=before)
    else:
        save_daily_plan(ROOT, plan)
    advance_form_snapshots_after_own_patch(st.session_state, path, before_revision=before,
                                         after_revision=observed_json_revision(path), updates={})
    return observed_json_revision(path)


def task_form_baseline(target: date, task: dict[str, Any], revision: str | None) -> tuple[str, dict[str, Any]]:
    key = f"daily_task:{target.isoformat()}:{task['task_id']}"
    path = resolved_json_path(daily_plan_path(ROOT, target))
    if key not in st.session_state:
        # Bind to the read that supplied task, not a later incidental reread.
        st.session_state[key] = {path: revision}
    return key, form_snapshot(st.session_state, key, [path], task)


def visible_task_drafts(target: date, plan: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = list(plan.get("tasks", []))
    known = plan_task_ids(plan)
    prefix = f"form_baseline:daily_task:{target.isoformat()}:"
    for key in list(st.session_state):
        if str(key).startswith(prefix):
            draft = st.session_state[key]["data"]
            if draft["task_id"] not in known:
                tasks.append(draft)
                known.add(draft["task_id"])
    return tasks


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


def master_selects(prefix: str, record: dict[str, Any]) -> dict[str, str]:
    def retain_saved(options: dict[str, str], key: str) -> dict[str, str]:
        value = record.get(key) or ""
        if value and value not in options.values():
            options[f"保存済み（非表示・未登録） [{value}]"] = value
        return options

    project_options = retain_saved(master_options(MASTER_SNAPSHOT.active("projects"), "project_id"), "project_id")
    project_label = st.selectbox("プロジェクト", list(project_options.keys()), index=list(project_options.keys()).index(selected_label(project_options, record.get("project_id"))), key=f"{prefix}_project")
    project_id = project_options[project_label]
    subject_options = retain_saved(master_options(MASTER_SNAPSHOT.active("subjects", project_id=project_id), "subject_id"), "subject_id")
    subject_label = st.selectbox("科目", list(subject_options.keys()), index=list(subject_options.keys()).index(selected_label(subject_options, record.get("subject_id"))), key=f"{prefix}_subject")
    subject_id = subject_options[subject_label]
    material_options = retain_saved(master_options(MASTER_SNAPSHOT.active("materials", project_id=project_id, subject_id=subject_id), "material_id"), "material_id")
    material_label = st.selectbox("教材", list(material_options.keys()), index=list(material_options.keys()).index(selected_label(material_options, record.get("material_id"))), key=f"{prefix}_material")
    material_id = material_options[material_label]
    unit_options = retain_saved(master_options(MASTER_SNAPSHOT.active("units", project_id=project_id, subject_id=subject_id, material_id=material_id), "unit_id"), "unit_id")
    unit_label = st.selectbox("単元", list(unit_options.keys()), index=list(unit_options.keys()).index(selected_label(unit_options, record.get("unit_id"))), key=f"{prefix}_unit")
    return {"project_id": project_id, "subject_id": subject_id, "material_id": material_id, "unit_id": unit_options[unit_label]}


def safe_index(options: list[Any], value: Any, default: int = 0) -> int:
    try:
        return options.index(value)
    except ValueError:
        return default


def parse_time_value(value: Any, fallback: str = "07:00") -> time:
    if isinstance(value, time):
        return value
    text = str(value or fallback)[:5]
    try:
        return datetime.strptime(text, "%H:%M").time()
    except ValueError:
        return datetime.strptime(fallback, "%H:%M").time()


def minutes_label(minutes: int | float) -> str:
    total = max(int(minutes or 0), 0)
    hours, mins = divmod(total, 60)
    return f"{hours}時間{mins:02d}分" if hours else f"{mins}分"


def find_daily_hub_exercise_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    for record in records:
        if record.get("source") == "daily_hub" or str(record.get("exercise_id", "")).endswith("_daily_hub"):
            return record
    return {}


def render_countdown(settings: dict[str, Any], today: date) -> None:
    countdown = get_countdown(settings, today)
    if countdown is not None:
        ab.metric_block(
            countdown["title"],
            countdown["value"],
            f"目標日 {countdown['target_date']} / ユーザー設定で変更できます",
        )


def render_daily_quick_log(target: date, *, settings: dict[str, Any] | None = None,
                           defer_rerun: bool = False) -> bool:
    rerun_requested = False
    if settings is None:
        settings = load_user_settings()
    health_settings = settings.get("health", {})
    exercise_settings = settings.get("exercise", {})

    diary = load_diary_entry(target)
    health_exists = get_health_log_path(target).exists()
    health = load_health_log(target)
    if not health_exists:
        health["sleep"].update(
            {
                "bed_time": health_settings.get("bed_time", health["sleep"].get("bed_time", "23:30")),
                "wake_time": health_settings.get("wake_time", health["sleep"].get("wake_time", "06:30")),
                "sleep_quality": int(health_settings.get("sleep_quality", health["sleep"].get("sleep_quality", 3))),
                "morning_sleepiness": int(health_settings.get("morning_sleepiness", health["sleep"].get("morning_sleepiness", 3))),
                "midnight_awakenings": int(health_settings.get("midnight_awakenings", health["sleep"].get("midnight_awakenings", 0))),
                "nap_minutes": int(health_settings.get("nap_minutes", health["sleep"].get("nap_minutes", 0))),
            }
        )
        health["condition"].update(
            {
                "physical_score": int(health_settings.get("physical_score", health["condition"].get("physical_score", 3))),
                "fatigue": int(health_settings.get("fatigue", health["condition"].get("fatigue", 3))),
                "stress": int(health_settings.get("stress", health["condition"].get("stress", 3))),
                "focus_score": int(health_settings.get("focus_score", health["condition"].get("focus_score", 3))),
                "mood_score": int(health_settings.get("mood_score", health["condition"].get("mood_score", 3))),
            }
        )
    exercise_log = load_exercise_log(target)
    edit_key = f"daily_quick:{target.isoformat()}"
    edit = form_snapshot(st.session_state, edit_key, [get_diary_entry_path(target), get_health_log_path(target), get_exercise_log_path(target)],
                         {"diary": diary, "health": health, "health_exists": health_exists, "exercise_log": exercise_log})
    if st.button("入力を破棄して最新の主要記録を再読込", key=f"reload_{edit_key}"):
        discard_form_snapshot(st.session_state, edit_key)
        rerun_requested = True
    diary, health, exercise_log = (edit["data"][name] for name in ("diary", "health", "exercise_log"))
    health_exists = edit["data"]["health_exists"]
    widget_prefix = f"hub_{target.isoformat()}_{edit['version']}"
    exercise_records = [item for item in exercise_log.get("records", []) if isinstance(item, dict)]
    hub_exercise = find_daily_hub_exercise_record(exercise_records)

    with st.expander("今日の主要記録", expanded=True):
        summary_cols = st.columns(4)
        with summary_cols[0]:
            caffeine_record_count = len([item for item in diary.get("caffeine_records", []) if isinstance(item, dict)])
            ab.metric_block(
                "日記・自己投資",
                yen(investment_total(diary)),
                f"カフェイン {diary.get('caffeine_mg', 0)} mg / 時刻付き {caffeine_record_count}件",
            )
        with summary_cols[1]:
            ab.metric_block("睡眠", minutes_label(health.get("sleep", {}).get("sleep_duration_min", 0) if health_exists else 0), "睡眠・体調ログに保存")
        with summary_cols[2]:
            exercise_duration = sum(float(item.get("duration_min", 0) or 0) for item in exercise_records)
            exercise_distance = sum(float(item.get("distance_km", 0) or 0) for item in exercise_records)
            ab.metric_block("運動", minutes_label(exercise_duration), f"{exercise_distance:.2f} km")
        with summary_cols[3]:
            ab.metric_block("集中しやすさ", f"{health.get('condition', {}).get('focus_score', 0) if health_exists else 0} / 5", "体調ログの簡易指標")

        st.page_link("pages/18_カフェイン記録.py", label="服薬ログを開く")

        record_exercise = st.checkbox(
            "運動の簡易記録を保存する",
            value=bool(hub_exercise),
            key=f"{widget_prefix}_record_exercise",
        )
        if exercise_records and not hub_exercise:
            st.caption("詳細な運動ログが既にあります。必要な場合のみ、この画面から簡易記録を追加してください。")

        with st.form(f"daily_quick_log_form_{widget_prefix}"):
            st.markdown("##### 日記・ライフログ")
            mood_options = ["未記録", "良好", "普通", "低調", "集中", "安定"]
            weather_options = ["未記録", "晴れ", "曇り", "雨", "雪"]
            seat_options = ["未記録", "Yes", "So-so", "No"]
            seat_label_by_status = {"yes": "Yes", "so_so": "So-so", "no": "No"}
            seat_label = seat_label_by_status.get(diary.get("good_seat_status"), "未記録")
            c1, c2, c3 = st.columns(3)
            mood = c1.selectbox("気分", mood_options, index=safe_index(mood_options, diary.get("mood", "未記録")), key=f"{widget_prefix}_mood")
            weather = c2.selectbox("天気", weather_options, index=safe_index(weather_options, diary.get("weather", "未記録")), key=f"{widget_prefix}_weather")
            good_seat_label = c3.selectbox(
                "よい席を取れたか",
                seat_options,
                index=safe_index(seat_options, seat_label),
                key=f"{widget_prefix}_good_seat_status",
            )
            diary_title = st.text_input("日記タイトル", value=str(diary.get("title", "")), key=f"{widget_prefix}_diary_title")
            diary_body = st.text_area("今日のメモ", value=str(diary.get("body", "")), height=100, key=f"{widget_prefix}_diary_body")
            cost_source = next((item for item in diary.get("cost_records", []) if isinstance(item, dict) and item.get("source") == "daily_hub"), {})
            c4, c5 = st.columns([2, 1])
            investment_name = c4.text_input("自己投資項目", value=str(cost_source.get("name", "")), key=f"{widget_prefix}_investment_name")
            investment_amount = c5.number_input("自己投資額", min_value=0.0, value=float(cost_source.get("amount", 0) or 0), step=100.0, key=f"{widget_prefix}_investment_amount")

            st.markdown("##### 睡眠・体調")
            sleep = health.get("sleep", {})
            condition = health.get("condition", {})
            c6, c7, c8 = st.columns(3)
            bed_time = c6.time_input("就寝時刻", value=parse_time_value(sleep.get("bed_time"), "23:30"), key=f"{widget_prefix}_bed_time")
            wake_time = c7.time_input("起床時刻", value=parse_time_value(sleep.get("wake_time"), "06:30"), key=f"{widget_prefix}_wake_time")
            sleep_duration = calculate_sleep_duration_minutes(bed_time, wake_time)
            c8.metric("睡眠時間", minutes_label(sleep_duration))
            sleep_cols = st.columns(3)
            sleep_quality = sleep_cols[0].slider("睡眠の質", 1, 5, int(sleep.get("sleep_quality", 3)), help="1: 低い / 5: 高い", key=f"{widget_prefix}_sleep_quality")
            morning_sleepiness = sleep_cols[1].slider("起床時の眠気", 1, 5, int(sleep.get("morning_sleepiness", 3)), help="1: 低い / 5: 高い", key=f"{widget_prefix}_morning_sleepiness")
            midnight_awakenings = sleep_cols[2].number_input("中途覚醒回数", min_value=0, value=int(sleep.get("midnight_awakenings", 0)), step=1, key=f"{widget_prefix}_midnight_awakenings")
            c10, c11, c12, c13 = st.columns(4)
            fatigue = c10.slider("疲労感", 1, 5, int(condition.get("fatigue", 3)), key=f"{widget_prefix}_fatigue")
            stress = c11.slider("ストレス", 1, 5, int(condition.get("stress", 3)), key=f"{widget_prefix}_stress")
            focus_score = c12.slider("集中しやすさ", 1, 5, int(condition.get("focus_score", 3)), key=f"{widget_prefix}_focus")
            physical_score = c13.slider("体調スコア", 1, 5, int(condition.get("physical_score", 3)), key=f"{widget_prefix}_physical")

            st.markdown("##### 運動")
            type_options = {
                "ランニング": "running",
                "ウォーキング": "walking",
                "筋トレ": "strength",
                "ストレッチ": "stretch",
                "その他": "other",
            }
            if record_exercise:
                type_label_by_value = {value: label for label, value in type_options.items()}
                c14, c15, c16, c17 = st.columns(4)
                exercise_type_label = c14.selectbox(
                    "運動種目",
                    list(type_options.keys()),
                    index=safe_index(list(type_options.keys()), type_label_by_value.get(hub_exercise.get("type") or exercise_settings.get("type", "running"), "ランニング")),
                    key=f"{widget_prefix}_exercise_type",
                )
                exercise_title = c15.text_input("タイトル", value=str(hub_exercise.get("title", "")), key=f"{widget_prefix}_exercise_title")
                exercise_duration_min = c16.number_input(
                    "運動時間（分）",
                    min_value=0,
                    value=int(hub_exercise.get("duration_min", 0) or 0),
                    step=5,
                    key=f"{widget_prefix}_exercise_duration",
                )
                exercise_distance_km = c17.number_input(
                    "距離（km）",
                    min_value=0.0,
                    value=float(hub_exercise.get("distance_km", 0.0) or 0),
                    step=0.1,
                    key=f"{widget_prefix}_exercise_distance",
                )
                c18, c19, c20 = st.columns(3)
                exercise_intensity = c18.slider("主観的きつさ", 1, 10, int(hub_exercise.get("intensity", exercise_settings.get("intensity", 5)) or 5), key=f"{widget_prefix}_exercise_intensity")
                exercise_fatigue_after = c19.slider("運動後疲労感", 1, 5, int(hub_exercise.get("fatigue_after", exercise_settings.get("fatigue_after", 3)) or 3), key=f"{widget_prefix}_exercise_fatigue")
                pace = calculate_running_pace(exercise_distance_km, exercise_duration_min)
                c20.metric("ペース", f"{pace:.2f} 分/km" if pace else "未計算")
                exercise_memo = st.text_input("運動メモ", value=str(hub_exercise.get("memo", "")), key=f"{widget_prefix}_exercise_memo")
            else:
                st.caption("運動しない日や未入力の日は、運動ログへ書き込みません。")

            submitted = st.form_submit_button("今日の主要記録を保存", type="primary")

        if submitted:
            diary_payload = dict(diary)
            diary_payload.update(
                {
                    "title": diary_title,
                    "body": diary_body,
                    "mood": mood,
                    "weather": weather,
                    "good_seat_status": {"Yes": "yes", "So-so": "so_so", "No": "no"}.get(good_seat_label),
                }
            )
            diary_payload = upsert_daily_hub_cost(diary_payload, investment_name, investment_amount)
            health_payload = {
                    "sleep": {
                        "bed_time": bed_time.strftime("%H:%M"),
                        "wake_time": wake_time.strftime("%H:%M"),
                        "sleep_duration_min": sleep_duration,
                        "sleep_quality": int(sleep_quality),
                        "morning_sleepiness": int(morning_sleepiness),
                        "midnight_awakenings": int(midnight_awakenings),
                        "nap_minutes": int(sleep.get("nap_minutes", 0)),
                    },
                    "condition": {
                        "physical_score": int(physical_score),
                        "fatigue": int(fatigue),
                        "stress": int(stress),
                        "focus_score": int(focus_score),
                        "mood_score": int(condition.get("mood_score", 3)),
                        "memo": str(condition.get("memo", "")),
                    },
                }
            exercise_payload = None
            if record_exercise:
                exercise_payload = {
                        "type": type_options[exercise_type_label],
                        "title": exercise_title,
                        "duration_min": int(exercise_duration_min),
                        "distance_km": float(exercise_distance_km),
                        "pace_min_per_km": pace,
                        "intensity": int(exercise_intensity),
                        "fatigue_after": int(exercise_fatigue_after),
                        "memo": exercise_memo,
                    }
            try:
                save_daily_records(
                    target, diary_payload, root=ROOT, operation_id=edit["operation_id"], expected_revisions=edit["revisions"],
                    health_data=health_payload, exercise_mode="hub" if record_exercise else "delete_hub", exercise_data=exercise_payload,
                )
            except (ValueError, OSError) as exc:
                ab.show_action_error(f"保存できませんでした。入力は保持しています。{exc}")
            else:
                discard_form_snapshot(st.session_state, edit_key)
                ab.queue_success("今日の主要記録を保存しました。")
                rerun_requested = True

    if rerun_requested and not defer_rerun:
        st.rerun()
    return rerun_requested


@st.fragment
def render_daily_inputs(target_date: date, initial_context: dict[str, Any],
                        session_display_rows: dict[str, dict[str, Any]]) -> None:
    global MASTER_SNAPSHOT
    if app_today() != target_date:
        st.rerun()
    context = initial_context.pop("context", None)
    if context is None:
        current_plan = load_daily_plan(ROOT, target_date)
        context = {"plan": current_plan,
                   "plan_revision": observed_json_revision(daily_plan_path(ROOT, target_date)),
                   "notes": load_all_notes(ROOT), "cards": load_all_flashcards(ROOT),
                   "master": MasterSnapshot.load(ROOT), "settings": load_user_settings()}
    plan, plan_revision = context["plan"], context["plan_revision"]
    MASTER_SNAPSHOT, user_settings = context["master"], context["settings"]
    notes, flashcards = context["notes"], context["cards"]
    notes_by_id = note_title_map(notes)
    note_options = {f"{note['title']} [{note['note_id']}]": note["note_id"] for note in notes}
    note_label_by_id = {value: label for label, value in note_options.items()}
    card_options = {f"{card.get('front', '無題のカード')} [{card.get('flashcard_id', '')}]": card.get("flashcard_id", "")
                    for card in flashcards if card.get("flashcard_id")}
    card_label_by_id = {value: label for label, value in card_options.items()}
    target_minutes = int(plan.get("target_total_minutes", 0) or 0)
    plan_action_rerun = render_daily_quick_log(target_date, settings=user_settings, defer_rerun=True)

    if st.checkbox("次はここから（教材の引き継ぎを表示）", key="daily_show_learning_resume"):
        from app_core.learning_context_ui import render_resume_cards

        render_resume_cards(ROOT, max_items=5, key_prefix="daily_learning_resume")
        st.caption("更新の新しい5件を表示しています。編集はマスタ管理またはデータエクスポートの『資料リンク・引き継ぎ』から行えます。")

    if plan.get("consultation_memos"):
        from app_core.consultation_proposal_ui import render_daily_consultation_memos

        with st.expander("採用した相談提案（計画メモ）", expanded=True):
            render_daily_consultation_memos(ROOT, target_date)

    if st.checkbox("相談提案箱を開く", key="daily_open_proposal_inbox"):
        from app_core.consultation_proposal_ui import render_proposal_inbox

        render_proposal_inbox(ROOT, key_prefix="daily_proposal_inbox")

    with st.expander("基本計画", expanded=True):
        with st.form("plan_base_form"):
            target_total_minutes = st.number_input("今日の目標時間（分）", min_value=0, value=target_minutes, step=30)
            focus_subjects_text = st.text_input("今日の重点科目（カンマ区切り）", value=", ".join(plan.get("focus_subjects", [])))
            materials_text = st.text_input("今日使う教材（カンマ区切り）", value=", ".join(plan.get("materials", [])))
            review_note_labels = st.multiselect(
                "今日復習すべきノート",
                options=list(note_options.keys()),
                default=[note_label_by_id[note_id] for note_id in plan.get("review_note_ids", []) if note_id in note_label_by_id],
            )
            review_card_labels = st.multiselect(
                "今日復習すべきカード",
                options=list(card_options.keys()),
                default=[card_label_by_id[card_id] for card_id in plan.get("review_card_ids", []) if card_id in card_label_by_id],
            )
            if st.form_submit_button("基本計画を保存", type="primary"):
                plan_before_action = deepcopy(plan)
                plan["target_total_minutes"] = int(target_total_minutes)
                plan["focus_subjects"] = split_csv(focus_subjects_text)
                plan["materials"] = split_csv(materials_text)
                plan["review_note_ids"] = [note_options[label] for label in review_note_labels]
                plan["review_card_ids"] = [card_options[label] for label in review_card_labels]
                try:
                    plan_revision = save_plan_and_advance_tasks(plan)
                except (ValueError, OSError) as exc:
                    plan = plan_before_action
                    ab.show_action_error(f"基本計画を保存できませんでした。入力は保持しています。{exc}")
                else:
                    ab.queue_success("基本計画を保存しました。")
                    plan_action_rerun = True

    previous_plan = load_daily_plan(ROOT, target_date - timedelta(days=1), write_back=False)
    carryover_candidates = [
        task
        for task in previous_plan.get("tasks", [])
        if task.get("status") in ("not_started", "in_progress", "pending")
    ]
    already_carried = {task.get("carried_from_task_id") for task in plan.get("tasks", []) if task.get("carried_from_task_id")}
    new_carryover_candidates = [task for task in carryover_candidates if task.get("task_id") not in already_carried]
    with st.expander("未完了タスクの繰越", expanded=False):
        if not new_carryover_candidates:
            st.info("繰り越し対象の未完了タスクはありません。")
        else:
            st.write(f"{len(new_carryover_candidates)}件の未完了タスクを前日から繰り越せます。")
            for task in new_carryover_candidates:
                st.caption(f"{task.get('title', '無題のタスク')} / {STATUS_LABELS.get(task.get('status'), '未着手')}")
            if st.button("未完了タスクを今日に繰り越す", type="primary"):
                plan_before_action = deepcopy(plan)
                existing_ids = plan_task_ids(plan)
                for task in new_carryover_candidates:
                    plan.setdefault("tasks", []).append(
                        {
                            "task_id": generate_id("task", plan.get("date"), existing_ids),
                            "title": task.get("title", "無題のタスク"),
                            "subject": task.get("subject", ""),
                            "material": task.get("material", ""),
                            "planned_minutes": int(task.get("planned_minutes", 0) or 0),
                            "status": "not_started",
                            "linked_note_ids": list(task.get("linked_note_ids", [])),
                            "linked_session_ids": [],
                            "carried_from_task_id": task.get("task_id", ""),
                            **{key: task[key] for key in ("roadmap_item_id", "roadmap_allocation_id") if key in task},
                            "project_id": task.get("project_id", ""),
                            "subject_id": task.get("subject_id", ""),
                            "material_id": task.get("material_id", ""),
                            "unit_id": task.get("unit_id", ""),
                        }
                    )
                try:
                    plan_revision = save_plan_and_advance_tasks(plan)
                except (ValueError, OSError) as exc:
                    plan = plan_before_action
                    ab.show_action_error(f"タスクを繰り越せませんでした。入力は保持しています。{exc}")
                else:
                    ab.queue_success("未完了タスクを今日に繰り越しました。")
                    plan_action_rerun = True

    summary_cols = st.columns(2)
    with summary_cols[0]:
        ab.card("今日の重点科目", card_title(plan.get("focus_subjects", [])))
    with summary_cols[1]:
        ab.card("今日使う教材", card_title(plan.get("materials", [])))

    st.subheader("計画と実績")
    st.caption("関連セッションの実績は最後の全体表示時点の内容です。保存後は自動更新されます。")
    if st.button("関連記録を更新", key="p09_refresh_related_records"):
        plan_action_rerun = True
    if plan.get("tasks"):
        comparison_rows = []
        for task in plan.get("tasks", []):
            planned_minutes = int(task.get("planned_minutes", 0) or 0)
            display = session_display_rows.get(task.get("task_id", ""), {})
            actual_sec = display.get("actual_seconds", 0) if tuple(task.get("linked_session_ids", [])) == display.get("session_ids", ()) else None
            actual_minutes = round(actual_sec / 60, 1) if actual_sec is not None else None
            names = MASTER_SNAPSHOT.names(task)
            comparison_rows.append(
                {
                    **{
                        "タスク名": task.get("title", "無題のタスク"),
                        "プロジェクト": names["project_name"],
                        "科目": names["subject_name"],
                        "教材": names["material_name"],
                        "単元": names["unit_name"],
                    },
                    **{
                        "予定時間": f"{planned_minutes}分",
                        "実績時間": f"{actual_minutes}分" if actual_minutes is not None else "関連記録の更新が必要",
                        "差分": f"{round(actual_minutes - planned_minutes, 1)}分" if actual_minutes is not None else "—",
                        "ステータス": STATUS_LABELS.get(task.get("status"), "未着手"),
                        "関連セッション数": len(task.get("linked_session_ids", [])),
                    },
                }
            )
        st.dataframe(pd.DataFrame(comparison_rows), width="stretch", hide_index=True)
    else:
        st.info("比較対象のタスクはまだありません。")

    st.subheader("タスク追加")
    plan_action_rerun = render_master_quick_add(ROOT, "daily_plan_master",
                                              snapshot=MASTER_SNAPSHOT, defer_rerun=True) or plan_action_rerun
    with st.form("new_task_form"):
        c1, c2, c3, c4 = st.columns([2, 1.2, 1.2, 1])
        title = c1.text_input("タスク名")
        subject = c2.text_input("科目")
        material = c3.text_input("教材")
        planned_minutes = c4.number_input("予定時間（分）", min_value=0, value=60, step=15)
        new_master_values = master_selects("new_task", {})
        selected_note_labels = st.multiselect("関連ノート", options=list(note_options.keys()), key="new_task_notes")
        if st.form_submit_button("タスクを追加", type="primary"):
            plan_before_action = deepcopy(plan)
            existing_ids = {task.get("task_id", "") for task in plan.get("tasks", [])}
            plan.setdefault("tasks", []).append(
                {
                    "task_id": generate_id("task", plan.get("date"), existing_ids),
                    "title": title.strip() or "無題のタスク",
                    "subject": subject.strip(),
                    "material": material.strip(),
                    "planned_minutes": int(planned_minutes),
                    "status": "not_started",
                    "linked_note_ids": [note_options[label] for label in selected_note_labels],
                    "linked_session_ids": [],
                    **new_master_values,
                }
            )
            try:
                plan_revision = save_plan_and_advance_tasks(plan)
            except (ValueError, OSError) as exc:
                plan = plan_before_action
                ab.show_action_error(f"タスクを追加できませんでした。入力は保持しています。{exc}")
            else:
                ab.queue_success("タスクを追加しました。")
                plan_action_rerun = True

    st.subheader("今日のタスク一覧")
    task_drafts = visible_task_drafts(target_date, plan)
    task_list_rerun = False
    if not task_drafts:
        st.info("今日のタスクはまだ登録されていません。")
    else:
        for index, live_task in enumerate(task_drafts, start=1):
            task_edit_key, task_edit = task_form_baseline(target_date, live_task, plan_revision)
            task = task_edit["data"]
            task_widget_key = f"{target_date.isoformat()}_{task['task_id']}_{task_edit['version']}"
            with st.expander(f"{index}. {task.get('title', '無題のタスク')} / {STATUS_LABELS.get(task.get('status'), '未着手')}", expanded=index == 1):
                if task["task_id"] not in plan_task_ids(plan):
                    st.warning("このタスクは別画面で削除されています。編集中の入力は残していますが、保存して復活させることはできません。")
                if st.button("このタスクの入力を破棄して最新データを再読込", key=f"reload_{task_edit_key}"):
                    discard_form_snapshot(st.session_state, task_edit_key)
                    task_list_rerun = True
                if task.get("linked_session_ids"):
                    st.warning("このタスクには関連する勉強セッションがあります。削除しても過去の勉強ログは保持され、勉強ログ側では削除済みタスクとして扱われます。")
                with st.form(f"task_form_{task_widget_key}"):
                    c1, c2, c3, c4 = st.columns([2, 1.2, 1.2, 1])
                    task_title = c1.text_input("タスク名", value=task.get("title", ""), key=f"title_{task_widget_key}")
                    task_subject = c2.text_input("科目", value=task.get("subject", ""), key=f"subject_{task_widget_key}")
                    task_material = c3.text_input("教材", value=task.get("material", ""), key=f"material_{task_widget_key}")
                    task_minutes = c4.number_input("予定時間（分）", min_value=0, value=int(task.get("planned_minutes", 0) or 0), step=15, key=f"minutes_{task_widget_key}")
                    status_label = st.radio(
                        "ステータス",
                        list(STATUS_LABELS.values()),
                        index=list(STATUS_LABELS.keys()).index(task.get("status", "not_started")),
                        horizontal=True,
                        key=f"status_{task_widget_key}",
                    )
                    current_note_labels = [note_label_by_id[note_id] for note_id in task.get("linked_note_ids", []) if note_id in note_label_by_id]
                    task_master_values = master_selects(f"task_{task_widget_key}", task)
                    task_note_labels = st.multiselect("関連ノート", options=list(note_options.keys()), default=current_note_labels, key=f"notes_{task_widget_key}")
                    delete_confirmed = True
                    if task.get("linked_session_ids"):
                        delete_confirmed = st.checkbox("関連セッションがあることを確認して削除する", key=f"delete_confirm_{task_widget_key}")
                    save_col, delete_col = st.columns([1, 1])
                    save_task = save_col.form_submit_button("タスクを保存", type="primary")
                    delete_task = delete_col.form_submit_button("タスクを削除")
                    if save_task:
                        updates = {"title": task_title.strip() or "無題のタスク", "subject": task_subject.strip(),
                                   "material": task_material.strip(), "planned_minutes": int(task_minutes),
                                   "status": LABEL_TO_STATUS[status_label],
                                   "linked_note_ids": [note_options[label] for label in task_note_labels]
                                       + [identifier for identifier in task.get("linked_note_ids", []) if identifier not in note_label_by_id],
                                   **task_master_values}
                        try:
                            newly_saved = save_task_edit(ROOT, target_date, task["task_id"], updates,
                                           expected_revisions=task_edit["revisions"], operation_id=task_edit["operation_id"])
                        except (ValueError, OSError) as exc:
                            ab.show_action_error(f"保存できませんでした。入力は保持しています。{exc}")
                        else:
                            path = resolved_json_path(daily_plan_path(ROOT, target_date))
                            if newly_saved:
                                advance_form_snapshots_after_own_patch(st.session_state, path,
                                    before_revision=task_edit["revisions"][path], after_revision=observed_json_revision(path), updates={})
                            discard_form_snapshot(st.session_state, task_edit_key)
                            ab.queue_success("タスクを保存しました。")
                            task_list_rerun = True
                    if delete_task:
                        if not delete_confirmed:
                            ab.show_action_message("関連セッションがあるタスクを削除する場合は、確認チェックを入れてください。", kind="warning")
                        else:
                            try:
                                newly_saved = save_task_edit(ROOT, target_date, task["task_id"], None,
                                               expected_revisions=task_edit["revisions"], operation_id=task_edit["operation_id"])
                            except (ValueError, OSError) as exc:
                                ab.show_action_error(f"削除できませんでした。入力は保持しています。{exc}")
                            else:
                                path = resolved_json_path(daily_plan_path(ROOT, target_date))
                                if newly_saved:
                                    advance_form_snapshots_after_own_patch(st.session_state, path,
                                        before_revision=task_edit["revisions"][path], after_revision=observed_json_revision(path), updates={})
                                discard_form_snapshot(st.session_state, task_edit_key)
                                ab.queue_success("タスクを削除しました。")
                                task_list_rerun = True

                linked_note_titles = note_titles(task.get("linked_note_ids", []), notes_by_id)
                st.caption(f"関連ノート: {' / '.join(linked_note_titles) if linked_note_titles else '未設定'}")
                display = session_display_rows.get(task.get("task_id", ""), {})
                same_references = (tuple(task.get("linked_session_ids", [])) == display.get("session_ids", ())
                                   and tuple(live_task.get("linked_session_ids", [])) == display.get("session_ids", ()))
                rows = display.get("rows", []) if same_references else []
                if not same_references:
                    st.warning("関連セッションの参照が変更されています。入力を保持して「関連記録を更新」から表示を更新できます。")
                if rows:
                    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
                else:
                    st.info("関連する勉強セッションはまだありません。")

    st.subheader("今日復習すべきカード")
    if plan.get("review_card_ids"):
        card_titles = [card_label_by_id.get(card_id, card_id) for card_id in plan["review_card_ids"]]
        st.write(" / ".join(card_titles))
    else:
        st.info("今日復習すべきカードはまだ設定されていません。")

    with st.expander("保存情報", expanded=False):
        st.write({"plan_id": plan.get("plan_id"), "保存先": str(daily_plan_path(ROOT, target_date))})

    if task_list_rerun or plan_action_rerun:
        # Render every sibling form, including quick log and master tabs, before
        # requesting the full refresh that updates summaries and related records.
        st.session_state.pop("_p09_integrity_result", None)
        st.rerun()


def check_daily_integrity(plan: dict[str, Any], notes_by_id: dict[str, str],
                          all_sessions: list[dict[str, Any]],
                          sessions_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    current_task_ids = plan_task_ids(plan)
    all_task_ids = {
        task.get("task_id", "")
        for item in load_all_daily_plans(ROOT)
        for task in item.get("tasks", [])
        if task.get("task_id")
    }
    session_ids_in_plan = [
        session_id
        for task in plan.get("tasks", [])
        for session_id in task.get("linked_session_ids", [])
        if session_id
    ]
    note_ids_in_plan = [
        note_id
        for task in plan.get("tasks", [])
        for note_id in task.get("linked_note_ids", [])
        if note_id
    ] + [note_id for note_id in plan.get("review_note_ids", []) if note_id]
    known_note_ids = set(notes_by_id)
    missing_session_count = sum(1 for session_id in session_ids_in_plan if session_id not in sessions_by_id)
    missing_note_count = sum(1 for note_id in note_ids_in_plan if note_id not in known_note_ids)
    unknown_task_ref_count = sum(
        1
        for session in all_sessions
        if session.get("linked_task_id") and session.get("linked_task_id") not in all_task_ids
    )
    linked_session_count = len(session_ids_in_plan)
    check_rows = [
        {"確認項目": "今日の plan_id", "値": plan.get("plan_id", "")},
        {"確認項目": "タスク数", "値": len(plan.get("tasks", []))},
        {"確認項目": "linked_session_ids の件数", "値": linked_session_count},
        {"確認項目": "存在しない session_id の件数", "値": missing_session_count},
        {"確認項目": "存在しない note_id の件数", "値": missing_note_count},
        {"確認項目": "存在しない task_id を参照している勉強ログ件数", "値": unknown_task_ref_count},
    ]
    check_df = pd.DataFrame(check_rows)
    check_df["値"] = check_df["値"].map(lambda value: "" if value is None else str(value))
    return {"rows": check_df, "ok": missing_session_count == 0 and missing_note_count == 0 and unknown_task_ref_count == 0}


st.set_page_config(page_title="今日の記録・計画", layout="wide")
user_settings = ab.sidemenu("今日の計画、主要記録、タスク、復習対象を管理します。")
ab.inject_css(settings=user_settings)

target_date = app_today()
plan = load_daily_plan(ROOT, target_date)
plan_revision = observed_json_revision(daily_plan_path(ROOT, target_date))
plan_action_rerun = False
notes = load_all_notes(ROOT)
flashcards = load_all_flashcards(ROOT)
all_sessions = load_all_study_sessions(ROOT)
sessions_by_id = {session.get("session_id"): session for session in all_sessions if session.get("session_id")}
show_task_metrics = should_show_task_metrics(user_settings)
notes_by_id = note_title_map(notes)
note_options = {f"{note['title']} [{note['note_id']}]": note["note_id"] for note in notes}
note_label_by_id = {value: label for label, value in note_options.items()}
card_options = {
    f"{card.get('front', '無題のカード')} [{card.get('flashcard_id', '')}]": card.get("flashcard_id", "")
    for card in flashcards
    if card.get("flashcard_id")
}
card_label_by_id = {value: label for label, value in card_options.items()}

ab.topbar("今日の記録・計画", "学習計画と主要な生活記録をまとめ、日々の運用入口として使います。")
render_countdown(user_settings, target_date)

total_planned = sum(int(task.get("planned_minutes", 0) or 0) for task in plan.get("tasks", []))
target_minutes = int(plan.get("target_total_minutes", 0) or 0)
target_date_text = target_date.isoformat()
actual_seconds = sum(
    int(float(session.get("duration_sec", 0) or 0))
    for session in all_sessions
    if str(session.get("start_at") or "")[:10] == target_date_text
)
time_achievement = actual_seconds / (target_minutes * 60) if target_minutes else 0
task_count = len(plan.get("tasks", []))
done_task_count = sum(1 for task in plan.get("tasks", []) if task.get("status") == "done")
task_achievement = done_task_count / task_count if task_count else 0

cols = st.columns(4 if show_task_metrics else 2)
with cols[0]:
    ab.metric_block("今日の目標時間", f"{target_minutes}分", "計画全体の目標")
with cols[2 if show_task_metrics else 1]:
    time_value = f"{time_achievement * 100:.1f}%" if target_minutes else "—"
    time_caption = f"実績 {format_minutes(actual_seconds)}" if target_minutes else f"目標時間未設定 / 実績 {format_minutes(actual_seconds)}"
    ab.metric_block("時間ベース達成率", time_value, time_caption)
if show_task_metrics:
    with cols[1]:
        ab.metric_block("予定タスク時間", f"{total_planned}分", "タスク予定時間の合計")
    with cols[3]:
        task_value = f"{task_achievement * 100:.1f}%" if task_count else "—"
        task_caption = f"{done_task_count}/{task_count}件完了" if task_count else "タスク未記録"
        ab.metric_block("タスクベース達成率", task_value, task_caption)
if target_minutes:
    st.progress(min(time_achievement, 1.0))
else:
    st.caption("目標時間を設定すると、当日の総勉強時間を基準に達成率を表示します。")

session_display_rows = {
    task.get("task_id", ""): {"session_ids": tuple(task.get("linked_session_ids", [])),
                              "rows": session_rows(task.get("linked_session_ids", []), sessions_by_id),
                              "actual_seconds": task_actual_seconds(task, sessions_by_id)}
    for task in visible_task_drafts(target_date, plan)
}
render_daily_inputs(target_date, {"context": {"plan": plan, "plan_revision": plan_revision,
                    "notes": notes, "cards": flashcards, "master": MASTER_SNAPSHOT,
                    "settings": user_settings}}, session_display_rows)

with st.expander("データ整合性チェック", expanded=False):
    st.caption("保存済み記録の参照を確認します。確認ボタンを押した時だけ全計画を読み込みます。")
    if st.button("整合性を確認", key="p09_check_integrity"):
        checked = check_daily_integrity(plan, notes_by_id, all_sessions, sessions_by_id)
        st.session_state["_p09_integrity_result"] = {
            "root": str(ROOT), "date": target_date.isoformat(), "plan_revision": plan_revision,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **checked,
        }
    checked = st.session_state.get("_p09_integrity_result")
    if checked is not None and checked["root"] == str(ROOT) and checked["date"] == target_date.isoformat():
        if checked["plan_revision"] != plan_revision:
            st.info("計画が変更されたため、前回の確認結果は表示していません。もう一度確認してください。")
        else:
            st.caption(f"確認日時: {checked['checked_at']} / この時点の結果です。")
            st.dataframe(checked["rows"], width="stretch", hide_index=True)
            if checked["ok"]:
                st.success("整合性チェックに問題はありません。")
            else:
                st.warning("参照切れがあります。対象データは不明な項目として表示されます。")
