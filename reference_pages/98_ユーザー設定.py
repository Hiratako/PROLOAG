from __future__ import annotations

from datetime import datetime
from typing import Any

import streamlit as st

import prolog_ui.components as ab
from app_core.form_snapshot import advance_form_snapshots_after_own_patch, discard_form_snapshot, form_snapshot
from app_core.json_store import observed_json_revision
from app_core.settings_edit_service import save_settings_form_patch
from app_core.personalization_ui import render_personalization_settings
from config import SUBSTANCE_PROFILES_PATH, USER_SETTINGS_PATH
from app_core.settings_service import (
    load_user_settings,
    reset_user_settings,
    update_user_settings,
    should_show_task_metrics,
    should_treat_missing_caffeine_as_zero,
)
from app_core.substance_service import (
    create_substance_profile,
    format_mg,
    load_substance_profiles,
    upsert_substance_profile,
)


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

EXERCISE_TYPES = {
    "running": "ランニング",
    "walking": "ウォーキング",
    "strength": "筋トレ",
    "stretch": "ストレッチ",
    "other": "その他",
}
PERIOD_OPTIONS = ["直近7日", "直近14日", "直近30日", "今月"]


def parse_time_text(value: Any, fallback: str) -> str:
    text = str(value or "").strip()[:5]
    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError:
        return fallback


def save_settings_form(updates: dict[str, Any], edit: dict[str, Any]) -> bool:
    try:
        save_settings_form_patch(st.session_state, "user_settings_form", edit, updates)
    except (ValueError, OSError) as exc:
        ab.show_action_error(f"保存できませんでした。入力は保持しています。{exc}")
        return False
    return True


st.set_page_config(page_title="ユーザー設定", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("カウントダウン、ページ表示、各ページの初期値と成分モデルを管理します。", settings=PAGE_SETTINGS)
ab.topbar("ユーザー設定", "目標日、サイドバー表示、各ページの初期値、成分ごとの残存モデルと表示を管理します。")

settings = PAGE_SETTINGS
page_rerun_requested = render_personalization_settings(settings)
settings_edit = form_snapshot(st.session_state, "user_settings_form", [USER_SETTINGS_PATH], settings)
if st.button("設定の入力を破棄して最新データを再読込", key="reload_user_settings_form"):
    discard_form_snapshot(st.session_state, "user_settings_form")
    page_rerun_requested = True
settings = settings_edit["data"]
timer = settings.get("timer", {})
health = settings.get("health", {})
exercise = settings.get("exercise", {})
diary = settings.get("diary", {})
display = settings.get("display", {})

with st.form(f"user_settings_form_{settings_edit['version']}"):
    st.subheader("秒給カウンター")
    timer_cols = st.columns(3)
    hourly_rate = timer_cols[0].number_input("初期時給", min_value=0, value=int(timer.get("hourly_rate", 2000) or 2000), step=100)
    goal_minutes = timer_cols[1].number_input("初期目標時間（分）", min_value=1, value=int(timer.get("goal_minutes", 120) or 120), step=15)
    interleaving_minutes = timer_cols[2].number_input("インタリービング周期（分）", min_value=5, value=int(timer.get("interleaving_minutes", 120) or 120), step=5)
    timer_cols = st.columns(3)
    overtime_enabled = timer_cols[0].checkbox("残業計算を初期状態で有効化", value=bool(timer.get("overtime_enabled", True)))
    overtime_multiplier = timer_cols[1].number_input("残業倍率", min_value=1.0, value=float(timer.get("overtime_multiplier", 1.25) or 1.25), step=0.05)
    overtime_threshold = timer_cols[2].number_input("残業閾値（時間）", min_value=0.5, value=float(timer.get("overtime_threshold_hours", 8.0) or 8.0), step=0.5)
    interleaving_enabled = st.checkbox("インタリービング警報を初期状態で有効化", value=bool(timer.get("interleaving_enabled", True)))

    st.divider()
    st.subheader("日記")
    investment_rows = st.number_input(
        "自己投資費用の初期入力行数",
        min_value=1,
        max_value=20,
        value=int(diary.get("investment_row_count", 3) or 3),
        step=1,
    )

    st.divider()
    st.subheader("睡眠・体調ログ")
    sleep_cols = st.columns(2)
    bed_time = sleep_cols[0].text_input("初期就寝時刻", value=parse_time_text(health.get("bed_time", "23:30"), "23:30"), help="HH:MM形式")
    wake_time = sleep_cols[1].text_input("初期起床時刻", value=parse_time_text(health.get("wake_time", "06:30"), "06:30"), help="HH:MM形式")
    score_cols = st.columns(5)
    sleep_quality = score_cols[0].slider("睡眠の質", 1, 5, int(health.get("sleep_quality", 3) or 3))
    morning_sleepiness = score_cols[1].slider("起床時の眠気", 1, 5, int(health.get("morning_sleepiness", 3) or 3))
    physical_score = score_cols[2].slider("体調スコア", 1, 5, int(health.get("physical_score", 3) or 3))
    fatigue = score_cols[3].slider("疲労感", 1, 5, int(health.get("fatigue", 3) or 3))
    stress = score_cols[4].slider("ストレス", 1, 5, int(health.get("stress", 3) or 3))
    score_cols = st.columns(4)
    focus_score = score_cols[0].slider("集中しやすさ", 1, 5, int(health.get("focus_score", 3) or 3))
    mood_score = score_cols[1].slider("気分", 1, 5, int(health.get("mood_score", 3) or 3))
    midnight_awakenings = score_cols[2].number_input("中途覚醒回数", min_value=0, value=int(health.get("midnight_awakenings", 0) or 0), step=1)
    nap_minutes = score_cols[3].number_input("昼寝時間（分）", min_value=0, value=int(health.get("nap_minutes", 0) or 0), step=5)

    st.divider()
    st.subheader("運動ログ")
    exercise_cols = st.columns(3)
    exercise_type = exercise_cols[0].selectbox(
        "初期運動種目",
        options=list(EXERCISE_TYPES.keys()),
        index=list(EXERCISE_TYPES.keys()).index(exercise.get("type", "running")) if exercise.get("type", "running") in EXERCISE_TYPES else 0,
        format_func=lambda key: EXERCISE_TYPES.get(key, "その他"),
    )
    exercise_start_time = exercise_cols[1].text_input("初期開始時刻", value=parse_time_text(exercise.get("start_time", "07:00"), "07:00"), help="HH:MM形式")
    exercise_duration = exercise_cols[2].number_input("初期運動時間（分）", min_value=0, value=int(exercise.get("duration_min", 30) or 30), step=5)
    exercise_cols = st.columns(3)
    exercise_distance = exercise_cols[0].number_input("初期距離（km）", min_value=0.0, value=float(exercise.get("distance_km", 0.0) or 0.0), step=0.1)
    exercise_intensity = exercise_cols[1].slider("主観的きつさ", 1, 10, int(exercise.get("intensity", 5) or 5))
    exercise_fatigue = exercise_cols[2].slider("運動後の疲労感", 1, 5, int(exercise.get("fatigue_after", 3) or 3))

    st.divider()
    st.subheader("表示")
    default_period = st.selectbox(
        "過去データ表示期間の初期値",
        PERIOD_OPTIONS,
        index=PERIOD_OPTIONS.index(display.get("period_label", "直近30日")) if display.get("period_label", "直近30日") in PERIOD_OPTIONS else 2,
    )
    show_task_metrics = st.checkbox(
        "タスク関連指標を表示",
        value=should_show_task_metrics(settings),
        help="タスク達成率・件数・相関・関連コメントの表示を切り替えます。タスクの入力機能は引き続き利用できます。",
    )

    st.divider()
    st.subheader("日次分析")
    caffeine_missing_as_zero = st.checkbox(
        "カフェイン未記録日を0mgとして分析する",
        value=should_treat_missing_caffeine_as_zero(settings),
        key=f"settings_caffeine_missing_as_zero_{settings_edit['version']}",
        help="オンでは、過去日・当日のカフェイン未記録を0mgとして日次比較に含めます。オフでは欠測として除外します。未来日は実績に含めず、元の記録は変更しません。",
    )

    submitted = st.form_submit_button("設定を保存", type="primary", width="stretch")

if submitted:
    settings_saved = save_settings_form(
        {
            "timer": {
                "hourly_rate": int(hourly_rate),
                "goal_minutes": int(goal_minutes),
                "overtime_enabled": bool(overtime_enabled),
                "overtime_multiplier": float(overtime_multiplier),
                "overtime_threshold_hours": float(overtime_threshold),
                "interleaving_enabled": bool(interleaving_enabled),
                "interleaving_minutes": int(interleaving_minutes),
            },
            "diary": {
                "investment_row_count": int(investment_rows),
            },
            "health": {
                "bed_time": parse_time_text(bed_time, "23:30"),
                "wake_time": parse_time_text(wake_time, "06:30"),
                "sleep_quality": int(sleep_quality),
                "morning_sleepiness": int(morning_sleepiness),
                "midnight_awakenings": int(midnight_awakenings),
                "nap_minutes": int(nap_minutes),
                "physical_score": int(physical_score),
                "fatigue": int(fatigue),
                "stress": int(stress),
                "focus_score": int(focus_score),
                "mood_score": int(mood_score),
            },
            "exercise": {
                "type": exercise_type,
                "start_time": parse_time_text(exercise_start_time, "07:00"),
                "duration_min": int(exercise_duration),
                "distance_km": float(exercise_distance),
                "intensity": int(exercise_intensity),
                "fatigue_after": int(exercise_fatigue),
            },
            "display": {
                "period_label": default_period,
                "theme_mode": display.get("theme_mode", "standard"),
                "show_task_metrics": bool(show_task_metrics),
            },
            "analysis": {
                "caffeine_missing_as_zero": bool(caffeine_missing_as_zero),
            },
        }, settings_edit
    )
    if settings_saved:
        ab.queue_success("ユーザー設定を保存しました。")
        page_rerun_requested = True

if st.button("初期値に戻す", width="content"):
    try:
        reset_user_settings()
    except (ValueError, OSError) as exc:
        ab.show_action_error(f"ユーザー設定を初期値に戻せませんでした。入力は保持しています。{exc}")
    else:
        discard_form_snapshot(st.session_state, "user_settings_form")
        discard_form_snapshot(st.session_state, "countdown_settings_form")
        discard_form_snapshot(st.session_state, "sidebar_visibility_form")
        ab.reset_theme_state()
        ab.queue_success("ユーザー設定を初期値に戻しました。")
        page_rerun_requested = True

st.divider()
st.subheader("成分・残存推定")
st.caption(
    "記録ページの初期量・製品や剤形と、単純半減期モデルの計算条件、グラフ色、面塗りを成分ごとに設定します。"
    "半減期などの変更は変更後の摂取記録から適用し、過去記録は記録時点のモデル値を維持します。"
)

profiles = load_substance_profiles(include_inactive=True)
for profile in profiles:
    profile_id = str(profile["substance_id"])
    profile_edit_key = f"substance_profile:{profile_id}"
    profile_edit = form_snapshot(st.session_state, profile_edit_key, [SUBSTANCE_PROFILES_PATH], profile)
    profile = profile_edit["data"]
    profile_widget_id = f"{profile_id}_{profile_edit['version']}"
    active_label = "表示中" if profile.get("is_active", True) else "アーカイブ"
    with st.expander(f"{profile['name']}  ·  {active_label}", expanded=False):
        if st.button("この成分の入力を破棄して再読込", key=f"reload_{profile_edit_key}"):
            discard_form_snapshot(st.session_state, profile_edit_key)
            page_rerun_requested = True
        st.caption(f"固定ID: {profile_id} / モデル版: {int(profile.get('model_revision', 1) or 1)}")
        with st.form(f"substance_profile_form_{profile_widget_id}"):
            name_col, amount_col, step_col = st.columns([1.5, 1, 1])
            profile_name = name_col.text_input(
                "表示名",
                value=str(profile.get("name") or profile_id),
                key=f"substance_name_{profile_widget_id}",
            )
            default_amount = amount_col.number_input(
                "初期摂取量（mg）",
                min_value=0.001,
                max_value=100000.0,
                value=float(profile.get("default_amount_mg", 1.0) or 1.0),
                step=float(profile.get("amount_step_mg", 0.5) or 0.5),
                format="%.3f",
                key=f"substance_default_{profile_widget_id}",
            )
            amount_step = step_col.number_input(
                "入力刻み（mg）",
                min_value=0.001,
                max_value=10000.0,
                value=float(profile.get("amount_step_mg", 0.5) or 0.5),
                step=0.1,
                format="%.3f",
                key=f"substance_step_{profile_widget_id}",
            )
            default_label = st.text_input(
                "製品・剤形の初期値（任意）",
                value=str(profile.get("default_label") or ""),
                placeholder="例: タブレット、コーヒー",
                key=f"substance_default_label_{profile_widget_id}",
            )
            model_cols = st.columns(3)
            half_life = model_cols[0].number_input(
                "半減期（モデル値・時間）",
                min_value=0.05,
                max_value=240.0,
                value=float(profile.get("half_life_hours", 5.0) or 5.0),
                step=0.25,
                format="%.2f",
                key=f"substance_half_life_{profile_widget_id}",
            )
            peak_delay = model_cols[1].number_input(
                "ピークまで（分）",
                min_value=0,
                max_value=720,
                value=int(profile.get("peak_delay_minutes", 30) or 0),
                step=5,
                key=f"substance_peak_{profile_widget_id}",
            )
            cutoff = model_cols[2].number_input(
                "0 mg扱いまで（時間）",
                min_value=0.1,
                max_value=720.0,
                value=float(profile.get("residual_cutoff_hours", 36.0) or 36.0),
                step=1.0,
                format="%.1f",
                key=f"substance_cutoff_{profile_widget_id}",
            )
            display_cols = st.columns(3)
            color = display_cols[0].color_picker(
                "表示色",
                value=str(profile.get("color") or "#25A9D6"),
                key=f"substance_color_{profile_widget_id}",
            )
            fill_enabled = display_cols[1].checkbox(
                "面塗りを表示",
                value=bool(profile.get("fill_enabled", True)),
                key=f"substance_fill_{profile_widget_id}",
            )
            is_active = display_cols[2].checkbox(
                "通常利用する",
                value=bool(profile.get("is_active", True)),
                help="オフにすると新規記録と残存グラフの初期選択から外れます。過去記録と設定は残り、必要なときは手動で確認できます。",
                key=f"substance_active_{profile_widget_id}",
            )
            profile_submit = st.form_submit_button("この成分設定を保存", type="primary", width="stretch")

        if profile_submit:
            if not profile_name.strip():
                ab.show_action_error("表示名を入力してください。")
            else:
                try:
                    saved_profile = upsert_substance_profile(
                        {
                            **profile,
                            "substance_id": profile_id,
                            "name": profile_name.strip(),
                            "default_amount_mg": float(default_amount),
                            "default_label": default_label.strip(),
                            "amount_step_mg": float(amount_step),
                            "half_life_hours": float(half_life),
                            "peak_delay_minutes": int(peak_delay),
                            "residual_cutoff_hours": float(cutoff),
                            "color": color,
                            "fill_enabled": bool(fill_enabled),
                            "is_active": bool(is_active),
                        }, expected_revision=profile_edit["revisions"][SUBSTANCE_PROFILES_PATH]
                    )
                except (ValueError, OSError) as exc:
                    ab.show_action_error(f"保存できませんでした。入力は保持しています。{exc}")
                else:
                    advance_form_snapshots_after_own_patch(
                        st.session_state, SUBSTANCE_PROFILES_PATH,
                        before_revision=profile_edit["revisions"][SUBSTANCE_PROFILES_PATH],
                        after_revision=observed_json_revision(SUBSTANCE_PROFILES_PATH), updates={},
                    )
                    discard_form_snapshot(st.session_state, profile_edit_key)
                    ab.queue_success(
                        f"{saved_profile['name']}の設定を保存しました（初期量 {format_mg(saved_profile['default_amount_mg'])} mg）。"
                    )
                    page_rerun_requested = True

with st.expander("成分を追加", expanded=False):
    with st.form("new_substance_profile_form"):
        new_cols = st.columns([1.5, 1, 1])
        new_name = new_cols[0].text_input("成分名", placeholder="追加する成分名")
        new_default_amount = new_cols[1].number_input(
            "初期摂取量（mg）", min_value=0.001, value=1.0, step=0.5, format="%.3f"
        )
        new_step = new_cols[2].number_input(
            "入力刻み（mg）", min_value=0.001, value=0.5, step=0.1, format="%.3f"
        )
        new_default_label = st.text_input(
            "製品・剤形の初期値（任意）",
            placeholder="例: タブレット、コーヒー",
        )
        new_model_cols = st.columns(3)
        new_half_life = new_model_cols[0].number_input(
            "半減期（モデル値・時間）", min_value=0.05, max_value=240.0, value=5.0, step=0.25
        )
        new_peak_delay = new_model_cols[1].number_input(
            "ピークまで（分）", min_value=0, max_value=720, value=60, step=5
        )
        new_cutoff = new_model_cols[2].number_input(
            "0 mg扱いまで（時間）", min_value=0.1, max_value=720.0, value=40.0, step=1.0
        )
        new_display_cols = st.columns(2)
        new_color = new_display_cols[0].color_picker("表示色", value="#5C8CE6")
        new_fill = new_display_cols[1].checkbox("面塗りを表示", value=True)
        new_submit = st.form_submit_button("成分を追加", type="primary", width="stretch")

    if new_submit:
        before_profile_revision = observed_json_revision(SUBSTANCE_PROFILES_PATH)
        try:
            created = create_substance_profile(
                new_name,
                default_amount_mg=float(new_default_amount),
                default_label=new_default_label.strip(),
                amount_step_mg=float(new_step),
                half_life_hours=float(new_half_life),
                peak_delay_minutes=int(new_peak_delay),
                residual_cutoff_hours=float(new_cutoff),
                color=new_color,
                fill_enabled=bool(new_fill),
            )
        except (ValueError, OSError) as exc:
            ab.show_action_error(str(exc))
        else:
            advance_form_snapshots_after_own_patch(
                st.session_state, SUBSTANCE_PROFILES_PATH,
                before_revision=before_profile_revision,
                after_revision=observed_json_revision(SUBSTANCE_PROFILES_PATH), updates={},
            )
            ab.queue_success(f"{created['name']}を追加しました。")
            page_rerun_requested = True

with st.expander("設定の保存について", expanded=False):
    st.caption("一般設定・成分設定の変更は、このブラウザの体験用データにだけ保存されます。体験をリセットすると初期状態へ戻ります。")

if page_rerun_requested:
    st.rerun()
