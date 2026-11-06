from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from app_core.app_clock import app_today
from html import escape
from typing import Any

import pandas as pd
import streamlit as st

import prolog_ui.components as ab
from app_core.flashcard_service import load_flashcards
from app_core.master_snapshot import MasterSnapshot
from app_core.note_service import load_all_notes
from app_core.reminder_service import load_reminders
from app_core.study_log_service import load_all_study_sessions
from config import PROJECT_ROOT


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

PALETTE = [
    {"base": "#2563EB", "accent": "#60A5FA", "roof": "#1E3A8A"},
    {"base": "#0F766E", "accent": "#5EEAD4", "roof": "#115E59"},
    {"base": "#7C3AED", "accent": "#C4B5FD", "roof": "#5B21B6"},
    {"base": "#D97706", "accent": "#FCD34D", "roof": "#92400E"},
    {"base": "#DC2626", "accent": "#FCA5A5", "roof": "#991B1B"},
    {"base": "#059669", "accent": "#86EFAC", "roof": "#065F46"},
    {"base": "#0891B2", "accent": "#67E8F9", "roof": "#155E75"},
    {"base": "#9333EA", "accent": "#E9D5FF", "roof": "#6B21A8"},
    {"base": "#4F46E5", "accent": "#A5B4FC", "roof": "#3730A3"},
    {"base": "#BE123C", "accent": "#FDA4AF", "roof": "#881337"},
]


def parse_day(value: Any) -> date | None:
    from app_core.analysis_dates import parse_analysis_date

    return parse_analysis_date(value)


def safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def hours_text(seconds: int | float) -> str:
    minutes = max(int(float(seconds or 0) // 60), 0)
    h, m = divmod(minutes, 60)
    return f"{h}時間{m:02d}分"


def days_ago_text(day: date | None) -> str:
    if not day:
        return "学習日なし"
    delta = (app_today() - day).days
    if delta == 0:
        return "今日"
    if delta > 0:
        return f"{delta}日前"
    return day.isoformat()


def apply_record_filter(record: dict[str, Any], filters: dict[str, str]) -> bool:
    for key, selected in filters.items():
        if selected and record.get(key) != selected:
            return False
    return True


def row_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record.get("project_id") or ""),
        str(record.get("subject_id") or ""),
        str(record.get("material_id") or ""),
        str(record.get("unit_id") or ""),
    )


def blank_row(key: tuple[str, str, str, str], master_snapshot: MasterSnapshot | None = None) -> dict[str, Any]:
    project_id, subject_id, material_id, unit_id = key
    snapshot = master_snapshot if master_snapshot is not None else MasterSnapshot.load(PROJECT_ROOT)
    names = snapshot.names(dict(zip(("project_id", "subject_id", "material_id", "unit_id"), key)))
    return {
        "project_id": project_id,
        "subject_id": subject_id,
        "material_id": material_id,
        "unit_id": unit_id,
        **names,
        "duration_sec": 0,
        "money": 0,
        "session_count": 0,
        "note_count": 0,
        "card_count": 0,
        "review_card_count": 0,
        "reminder_count": 0,
        "pending_reminder_count": 0,
        "last_study_date": None,
        "sample_notes": [],
    }


def build_city_rows(
    sessions: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    reminders: list[dict[str, Any]],
    filters: dict[str, str],
    start_date: date,
    end_date: date,
    *,
    master_snapshot: MasterSnapshot | None = None,
) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def row_for(key: tuple[str, str, str, str]) -> dict[str, Any]:
        nonlocal master_snapshot
        if key not in rows:
            if master_snapshot is None:
                master_snapshot = MasterSnapshot.load(PROJECT_ROOT)
            rows[key] = blank_row(key, master_snapshot)
        return rows[key]

    for session in sessions:
        day = parse_day(session.get("start_at") or session.get("end_at"))
        if not day or day < start_date or day > end_date or not apply_record_filter(session, filters):
            continue
        key = row_key(session)
        row = row_for(key)
        row["duration_sec"] += safe_int(session.get("duration_sec"))
        row["money"] += safe_int(session.get("money"))
        row["session_count"] += 1
        if not row["last_study_date"] or day > row["last_study_date"]:
            row["last_study_date"] = day

    for note in notes:
        day = parse_day(note.get("created_at") or note.get("updated_at")) or start_date
        if day < start_date or day > end_date or not apply_record_filter(note, filters):
            continue
        key = row_key(note)
        row = row_for(key)
        row["note_count"] += 1
        title = str(note.get("title") or "無題のノート")
        if len(row["sample_notes"]) < 3:
            row["sample_notes"].append(title)

    for card in cards:
        day = parse_day(card.get("created_at") or card.get("updated_at")) or start_date
        if day < start_date or day > end_date or not apply_record_filter(card, filters):
            continue
        key = row_key(card)
        row = row_for(key)
        row["card_count"] += 1
        if card.get("need_review") or card.get("important"):
            row["review_card_count"] += 1

    for reminder in reminders:
        day = parse_day(reminder.get("scheduled_date") or reminder.get("created_at") or reminder.get("registered")) or start_date
        if day < start_date or day > end_date or not apply_record_filter(reminder, filters):
            continue
        key = row_key(reminder)
        row = row_for(key)
        row["reminder_count"] += 1
        if str(reminder.get("status") or "").lower() not in {"done", "completed", "complete"}:
            row["pending_reminder_count"] += 1

    result = list(rows.values())
    for row in result:
        row["hours"] = round(row["duration_sec"] / 3600, 2)
        row["knowledge_assets"] = row["note_count"] + row["card_count"] + row["reminder_count"]
        row["knowledge_density"] = round((row["note_count"] * 3 + row["card_count"] + row["reminder_count"]) / max(row["hours"], 1), 2)
    return sorted(result, key=lambda item: (item["subject_name"], -item["duration_sec"], item["unit_name"]))


def color_for_subject(subject_id: str) -> dict[str, str]:
    index = sum(ord(ch) for ch in subject_id or "unset") % len(PALETTE)
    return PALETTE[index]


def window_html(note_count: int, card_count: int, pending_count: int) -> str:
    total = max(4, min(24, note_count * 2 + max(card_count, 1)))
    lit = min(total, card_count)
    windows = []
    for index in range(total):
        class_name = "kc-window kc-lit" if index < lit else "kc-window"
        if pending_count and index >= total - min(pending_count, 4):
            class_name += " kc-alert"
        windows.append(f'<span class="{class_name}"></span>')
    return "".join(windows)


def render_city(rows: list[dict[str, Any]]) -> None:
    if not rows:
        st.info("表示対象の知識都市データはまだありません。勉強ログ、ノート、カード、復習予定が増えると都市が形成されます。")
        return

    max_sec = max((row["duration_sec"] for row in rows), default=1) or 1
    districts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        districts[row["subject_name"]].append(row)

    district_html = []
    for subject_name, subject_rows in districts.items():
        total_hours = sum(row["duration_sec"] for row in subject_rows)
        total_notes = sum(row["note_count"] for row in subject_rows)
        total_cards = sum(row["card_count"] for row in subject_rows)
        subject_id = subject_rows[0]["subject_id"]
        colors = color_for_subject(subject_id)
        buildings = []
        for row in subject_rows[:10]:
            ratio = row["duration_sec"] / max_sec
            height = 46 + int(ratio * 150)
            width = 62 + min(34, row["session_count"] * 4)
            opacity = 0.74 + min(0.24, ratio * 0.24)
            freshness = ""
            last_day = row.get("last_study_date")
            if last_day and (app_today() - last_day).days <= 7:
                freshness = " kc-recent"
            sparse = " kc-vacant" if row["duration_sec"] == 0 and row["knowledge_assets"] <= 1 else ""
            unit_title = escape(str(row["unit_name"]))
            subject_title = escape(str(subject_name))
            tooltip = escape(
                f"{row['unit_name']} / {hours_text(row['duration_sec'])} / ノート{row['note_count']} / カード{row['card_count']} / 未完了復習{row['pending_reminder_count']}",
                quote=True,
            )
            buildings.append(
                f"""
                <div class="kc-building-wrap" title="{tooltip}">
                  <div class="kc-building{freshness}{sparse}" style="--kc-base:{colors['base']}; --kc-accent:{colors['accent']}; --kc-roof:{colors['roof']}; width:{width}px; height:{height}px; opacity:{opacity:.2f};">
                    <div class="kc-roof"></div>
                    <div class="kc-window-grid">{window_html(row['note_count'], row['card_count'], row['pending_reminder_count'])}</div>
                  </div>
                  <div class="kc-unit">{unit_title}</div>
                  <div class="kc-unit-sub">{subject_title}</div>
                </div>
                """
            )
        district_html.append(
            f"""
            <section class="kc-district" style="--kc-district:{colors['base']}; --kc-district-soft:{colors['accent']};">
              <div class="kc-district-head">
                <div>
                  <p class="kc-district-label">District</p>
                  <h3>{escape(str(subject_name))}</h3>
                </div>
                <div class="kc-district-metrics">
                  <span>{hours_text(total_hours)}</span>
                  <span>ノート {total_notes}</span>
                  <span>カード {total_cards}</span>
                </div>
              </div>
              <div class="kc-skyline">{''.join(buildings)}</div>
            </section>
            """
        )

    city_html = f"""
        <style>
          .kc-city-shell {{
            padding: 18px;
            border-radius: 18px;
            background:
              radial-gradient(circle at 12% 8%, rgba(96,165,250,.26), transparent 28%),
              radial-gradient(circle at 88% 10%, rgba(251,191,36,.22), transparent 26%),
              linear-gradient(145deg, #07172d 0%, #0f2747 48%, #123a5e 100%);
            border: 1px solid rgba(148, 163, 184, .35);
            box-shadow: 0 22px 70px rgba(15, 23, 42, .20);
          }}
          .kc-city-title {{
            display: flex;
            justify-content: space-between;
            gap: 16px;
            align-items: flex-end;
            color: #f8fafc;
            margin-bottom: 18px;
          }}
          .kc-city-title h2 {{
            margin: 0;
            font-size: 1.3rem;
            letter-spacing: 0;
          }}
          .kc-city-title p {{
            margin: 4px 0 0;
            color: #cbd5e1;
            font-size: .88rem;
          }}
          .kc-legend {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            color: #e2e8f0;
            font-size: .78rem;
          }}
          .kc-legend span {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 9px;
            border: 1px solid rgba(226, 232, 240, .22);
            border-radius: 999px;
            background: rgba(15, 23, 42, .32);
          }}
          .kc-dot {{
            width: 9px;
            height: 9px;
            border-radius: 999px;
            background: #67e8f9;
            box-shadow: 0 0 12px currentColor;
          }}
          .kc-district {{
            margin: 14px 0;
            padding: 14px;
            border-radius: 16px;
            background: linear-gradient(180deg, rgba(255,255,255,.12), rgba(255,255,255,.06));
            border: 1px solid color-mix(in srgb, var(--kc-district) 54%, rgba(255,255,255,.34));
            box-shadow: inset 0 1px 0 rgba(255,255,255,.15);
          }}
          .kc-district-head {{
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: flex-start;
            margin-bottom: 14px;
            color: #f8fafc;
          }}
          .kc-district-label {{
            margin: 0 0 3px;
            color: var(--kc-district-soft);
            font-size: .72rem;
            text-transform: uppercase;
            font-weight: 700;
          }}
          .kc-district h3 {{
            margin: 0;
            font-size: 1.08rem;
          }}
          .kc-district-metrics {{
            display: flex;
            flex-wrap: wrap;
            justify-content: flex-end;
            gap: 6px;
          }}
          .kc-district-metrics span {{
            color: #e2e8f0;
            background: rgba(15, 23, 42, .34);
            border: 1px solid rgba(255,255,255,.18);
            border-radius: 999px;
            padding: 5px 9px;
            font-size: .76rem;
            white-space: nowrap;
          }}
          .kc-skyline {{
            display: flex;
            align-items: flex-end;
            gap: 14px;
            min-height: 230px;
            overflow-x: auto;
            padding: 16px 8px 10px;
            border-radius: 12px;
            background:
              linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.02)),
              repeating-linear-gradient(90deg, rgba(255,255,255,.06) 0 1px, transparent 1px 48px);
          }}
          .kc-building-wrap {{
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: flex-end;
            min-width: 90px;
            height: 226px;
          }}
          .kc-building {{
            position: relative;
            display: flex;
            align-items: flex-start;
            justify-content: center;
            min-height: 44px;
            max-height: 196px;
            border-radius: 14px 14px 7px 7px;
            background:
              linear-gradient(155deg, color-mix(in srgb, var(--kc-accent) 32%, #ffffff), transparent 34%),
              linear-gradient(180deg, var(--kc-base), color-mix(in srgb, var(--kc-base) 76%, #020617));
            border: 1px solid rgba(255,255,255,.36);
            box-shadow:
              0 12px 28px rgba(2, 6, 23, .38),
              inset 0 1px 0 rgba(255,255,255,.28);
            transition: transform .18s ease, filter .18s ease;
          }}
          .kc-building:hover {{
            transform: translateY(-5px);
            filter: saturate(1.18) brightness(1.06);
          }}
          .kc-roof {{
            position: absolute;
            top: -8px;
            left: 18%;
            right: 18%;
            height: 12px;
            border-radius: 10px 10px 2px 2px;
            background: var(--kc-roof);
            box-shadow: 0 0 16px color-mix(in srgb, var(--kc-accent) 50%, transparent);
          }}
          .kc-recent {{
            box-shadow:
              0 15px 34px rgba(2, 6, 23, .40),
              0 0 26px color-mix(in srgb, var(--kc-accent) 62%, transparent),
              inset 0 1px 0 rgba(255,255,255,.30);
          }}
          .kc-vacant {{
            filter: grayscale(.18);
            background: linear-gradient(180deg, #94a3b8, #475569);
          }}
          .kc-window-grid {{
            display: grid;
            grid-template-columns: repeat(4, 8px);
            gap: 6px;
            padding: 18px 9px 10px;
            align-content: start;
          }}
          .kc-window {{
            width: 8px;
            height: 8px;
            border-radius: 2px;
            background: rgba(241, 245, 249, .36);
            box-shadow: inset 0 0 0 1px rgba(255,255,255,.12);
          }}
          .kc-lit {{
            background: #FEF3C7;
            box-shadow: 0 0 11px #FCD34D;
          }}
          .kc-alert {{
            background: #FDBA74;
            box-shadow: 0 0 12px #FB923C;
          }}
          .kc-unit {{
            max-width: 102px;
            margin-top: 8px;
            color: #f8fafc;
            font-size: .76rem;
            font-weight: 700;
            text-align: center;
            line-height: 1.28;
          }}
          .kc-unit-sub {{
            max-width: 102px;
            color: #cbd5e1;
            font-size: .66rem;
            text-align: center;
            line-height: 1.25;
          }}
          @media (max-width: 720px) {{
            .kc-city-title, .kc-district-head {{
              display: block;
            }}
            .kc-district-metrics {{
              justify-content: flex-start;
              margin-top: 8px;
            }}
          }}
        </style>
        <div class="kc-city-shell">
          <div class="kc-city-title">
            <div>
              <h2>Knowledge City</h2>
              <p>科目を街区、単元を建物として、学習時間・ノート・カード・復習予定の蓄積を可視化します。</p>
            </div>
            <div class="kc-legend">
              <span><i class="kc-dot"></i>高さ: 学習時間</span>
              <span><i class="kc-dot" style="background:#FEF3C7;"></i>明かり: カード</span>
              <span><i class="kc-dot" style="background:#FDBA74;"></i>琥珀: 未完了復習</span>
            </div>
          </div>
          {''.join(district_html)}
        </div>
        """
    if hasattr(st, "html"):
        st.html(city_html)
    else:
        st.markdown(city_html, unsafe_allow_html=True)


def render_insight(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    top_time = max(rows, key=lambda item: item["duration_sec"])
    sparse_notes = [
        row
        for row in rows
        if row["duration_sec"] >= 3600 and row["note_count"] == 0
    ]
    pending = [row for row in rows if row["pending_reminder_count"] > 0]
    card_gap = [
        row
        for row in rows
        if row["note_count"] > 0 and row["card_count"] == 0
    ]
    comments = [
        f"記録上、最も大きく育っている区画は「{top_time['subject_name']} / {top_time['unit_name']}」で、累積は {hours_text(top_time['duration_sec'])} です。"
    ]
    if sparse_notes:
        sample = sparse_notes[0]
        comments.append(f"「{sample['unit_name']}」は学習時間がありますが、ノート化がまだ少なめです。知識ベース化の候補にできます。")
    if card_gap:
        sample = card_gap[0]
        comments.append(f"「{sample['unit_name']}」はノートがありますが、カード化がまだありません。復習しやすい形に分解できる可能性があります。")
    if pending:
        sample = max(pending, key=lambda item: item["pending_reminder_count"])
        comments.append(f"「{sample['unit_name']}」には未完了の復習予定が残っています。次の学習計画に入れる候補です。")
    for comment in comments[:4]:
        st.caption(comment)


def csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    export_rows = []
    for row in rows:
        export_rows.append(
            {
                "project_name": row["project_name"],
                "subject_name": row["subject_name"],
                "material_name": row["material_name"],
                "unit_name": row["unit_name"],
                "duration_hours": row["hours"],
                "session_count": row["session_count"],
                "note_count": row["note_count"],
                "card_count": row["card_count"],
                "review_card_count": row["review_card_count"],
                "reminder_count": row["reminder_count"],
                "pending_reminder_count": row["pending_reminder_count"],
                "last_study_date": row["last_study_date"].isoformat() if row["last_study_date"] else "",
            }
        )
    return pd.DataFrame(export_rows).to_csv(index=False).encode("utf-8-sig")


st.set_page_config(page_title="知識都市ビュー", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("学習データを都市として俯瞰します。", settings=PAGE_SETTINGS)
ab.topbar("知識都市ビュー", "学習時間、ノート、カード、復習予定がつくる知識の街を眺めます。")

sessions = load_all_study_sessions(PROJECT_ROOT)
notes = load_all_notes(PROJECT_ROOT)
cards = load_flashcards(PROJECT_ROOT)
reminders = load_reminders(PROJECT_ROOT)
master_snapshot = MasterSnapshot.load(PROJECT_ROOT)

dated_sessions = [parse_day(session.get("start_at") or session.get("end_at")) for session in sessions]
dated_sessions = [day for day in dated_sessions if day]
today = app_today()
default_start = min(dated_sessions) if dated_sessions else today - timedelta(days=180)
default_end = max(dated_sessions) if dated_sessions else today

with st.sidebar:
    st.markdown("### 表示条件")
    start_date = st.date_input("開始日", value=default_start)
    end_date = st.date_input("終了日", value=default_end)
    if end_date < start_date:
        start_date, end_date = end_date, start_date

    projects = master_snapshot.active("projects")
    project_options = {"すべて": ""}
    project_options.update({item.get("name", "名称未設定"): item.get("project_id", "") for item in projects})
    selected_project_label = st.selectbox("プロジェクト", list(project_options.keys()))
    selected_project_id = project_options[selected_project_label]

    subjects = master_snapshot.active("subjects", project_id=selected_project_id or None)
    subject_options = {"すべて": ""}
    subject_options.update({item.get("name", "名称未設定"): item.get("subject_id", "") for item in subjects})
    selected_subject_label = st.selectbox("科目", list(subject_options.keys()))
    selected_subject_id = subject_options[selected_subject_label]

    min_hours = st.slider("表示する最小学習時間", min_value=0.0, max_value=10.0, value=0.0, step=0.5)

filters = {
    "project_id": selected_project_id,
    "subject_id": selected_subject_id,
}
rows = build_city_rows(sessions, notes, cards, reminders, filters, start_date, end_date, master_snapshot=master_snapshot)
rows = [row for row in rows if row["hours"] >= min_hours or row["knowledge_assets"] > 0]

total_sec = sum(row["duration_sec"] for row in rows)
total_notes = sum(row["note_count"] for row in rows)
total_cards = sum(row["card_count"] for row in rows)
pending_reminders = sum(row["pending_reminder_count"] for row in rows)
active_units = len([row for row in rows if row["duration_sec"] > 0 or row["knowledge_assets"] > 0])
knowledge_ready = len([row for row in rows if row["note_count"] > 0 and row["card_count"] > 0])
knowledge_rate = round(knowledge_ready / active_units * 100, 1) if active_units else 0.0

st.subheader("都市サマリー")
cols = st.columns(5)
with cols[0]:
    ab.metric_block("総学習時間", hours_text(total_sec), "建物の高さに反映")
with cols[1]:
    ab.metric_block("知識化単元", f"{knowledge_ready}/{active_units}", f"{knowledge_rate}%")
with cols[2]:
    ab.metric_block("ノート数", total_notes, "窓の密度に反映")
with cols[3]:
    ab.metric_block("カード数", total_cards, "明かりに反映")
with cols[4]:
    ab.metric_block("未完了復習", pending_reminders, "琥珀色の窓")

render_city(rows)

st.markdown("### 読み取りコメント")
render_insight(rows)

st.markdown("### 単元別データ")
if rows:
    table_rows = [
        {
            "プロジェクト": row["project_name"],
            "科目": row["subject_name"],
            "教材": row["material_name"],
            "単元": row["unit_name"],
            "学習時間": hours_text(row["duration_sec"]),
            "セッション": row["session_count"],
            "ノート": row["note_count"],
            "カード": row["card_count"],
            "要復習カード": row["review_card_count"],
            "未完了復習": row["pending_reminder_count"],
            "最終学習": days_ago_text(row["last_study_date"]),
        }
        for row in sorted(rows, key=lambda item: item["duration_sec"], reverse=True)
    ]
    st.dataframe(pd.DataFrame(table_rows), width="stretch", hide_index=True)
    st.download_button(
        "知識都市データをCSV出力",
        data=csv_bytes(rows),
        file_name="knowledge_city_summary.csv",
        mime="text/csv",
        width="content", on_click="ignore")
else:
    st.info("対象期間に表示できるデータがありません。")
