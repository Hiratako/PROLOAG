from __future__ import annotations

import logging

import streamlit as st

from prolog_ui import components as ui
from web_demo.catalog import PRODUCT_VIEWS, VIEW_RENDER_LOCK, render_product_view
from web_demo.data import PERIOD_START, REFERENCE_DATE
from web_demo.navigation import PAGES_STATE_KEY


LOGGER = logging.getLogger(__name__)


NAV_SECTIONS = {
    "学習・計画": [
        ("today", "今日の記録・計画", "edit_calendar"),
        ("roadmap", "学習ロードマップ", "calendar_month"),
        ("timer", "秒給カウンター", "timer"),
        ("logs", "勉強ログ", "history"),
        ("study", "学習の分析", "monitoring"),
        ("performance", "学習パフォーマンス・条件分析", "query_stats"),
        ("statistics", "マイデータ統計演習", "calculate"),
        ("growth", "学習の蓄積", "auto_awesome"),
        ("masters", "マスタ管理", "category"),
    ],
    "ノート・復習": [
        ("notes", "ノート・知識ベース", "menu_book"),
        ("city", "知識都市ビュー", "location_city"),
        ("cards", "記憶フラッシュ", "style"),
        ("review", "復習リマインダー", "event_repeat"),
    ],
    "生活・健康": [
        ("diary", "日記・ライフログ", "book"),
        ("substances", "服薬ログ", "medication"),
        ("sleep", "睡眠・体調ログ", "bedtime"),
        ("exercise", "運動ログ", "directions_run"),
        ("health", "生活・健康の分析", "favorite"),
        ("condition", "成分リズム・コンディション分析", "vital_signs"),
    ],
    "使い方・データ管理": [
        ("guide", "使い方ガイド", "help"),
        ("google-guide", "Google連携ガイド", "info"),
        ("export", "データエクスポート", "download"),
        ("legacy-import", "旧版ログ移行", "upload_file"),
        ("settings", "ユーザー設定", "settings"),
        ("integrity", "データ整合性チェック", "fact_check"),
    ],
}


def _renderer(route):
    def render():
        render_product_view(route)
    render.__name__ = "render_" + route.replace("-", "_")
    return render


def study():
    render_product_view("study")


def growth():
    render_product_view("growth")


def city():
    render_product_view("city")


def health():
    render_product_view("health")


def make_pages() -> dict:
    from app_core.navigation import hidden_sidebar_pages
    from app_core.settings_service import load_user_settings
    hidden = hidden_sidebar_pages(load_user_settings())
    pages = {
        "welcome": st.Page(welcome, title="はじめに", icon=":material/home:", default=True),
        "about": st.Page(about, title="このDemoについて", icon=":material/info:", url_path="about"),
    }
    for items in NAV_SECTIONS.values():
        for route, title, icon in items:
            visibility = "hidden" if f"pages/{PRODUCT_VIEWS[route]}" in hidden else "visible"
            pages[route] = st.Page(_renderer(route), title=title, icon=f":material/{icon}:", url_path=route, visibility=visibility)
    return pages


def _summary():
    from config import get_data_root
    from data_utils import load_all_study_sessions, load_all_notes, load_all_flashcards, load_master_data
    root = get_data_root()
    records = load_all_study_sessions(root)
    return {
        "hours": sum(max(float(row.get("duration_sec", 0) or 0), 0) for row in records) / 3600,
        "sessions": len(records),
        "subjects": sum(row.get("is_active", True) is not False for row in load_master_data(root).get("subjects", [])),
        "notes": len(load_all_notes(root)),
        "cards": len(load_all_flashcards(root)),
    }


def welcome() -> None:
    ui.topbar("PROLOAG", "今日の記録を、次の物語へ。", eyebrow="PROGRESS & REFLECTION OS FOR LEARNING, ORGANIZATION, AND GROWTH")
    st.info("元のDemo版に近い機能を、あなた専用の一時データで試せます。追加・編集・削除した内容は、他の人や元のサンプルには反映されません。")
    summary = _summary()
    values = [
        ("学習の記録", f"{summary['hours']:,.1f} 時間", f"{summary['sessions']:,} セッション"),
        ("学習した科目", f"{summary['subjects']} 科目", "中小企業診断士・英語などの架空データ"),
        ("知識の蓄積", f"{summary['notes']} ノート", f"{summary['cards']} 枚のカード"),
        ("はじめのサンプル", "180 日間", f"{PERIOD_START:%Y/%m/%d} — {REFERENCE_DATE:%Y/%m/%d}"),
    ]
    for column, (label, value, caption) in zip(st.columns(4), values):
        with column:
            ui.metric_block(label, value, caption)
    st.subheader("まずは、この順番で")
    pages = st.session_state.get(PAGES_STATE_KEY) or make_pages()
    steps = [
        ("01", "today", "今日の計画を立てる", "用意されたタスクを確認し、追加・完了を試します。睡眠や体調も同じ画面から記録できます。"),
        ("02", "timer", "勉強時間を記録する", "タイマーを開始・一時停止し、短いセッションを保存。勉強ログで内容を確認できます。"),
        ("03", "notes", "ノートと復習につなげる", "サンプルのノートを開いて編集。カードやリマインダーも体験できます。"),
        ("04", "study", "記録を振り返る", "追加した記録を含めて、学習・生活の分析や知識の蓄積を眺めます。"),
    ]
    for column, (number, route, title, description) in zip(st.columns(4), steps):
        with column, st.container(border=True):
            st.caption(f"STEP {number}")
            st.markdown(f"**{title}**")
            st.write(description)
            st.page_link(pages[route], label="体験する", icon=":material/arrow_forward:")
    with st.container(border=True):
        st.markdown("**全体の計画から、日々の記録まで**")
        st.write("ロードマップの工程作成・編集、日別の計画、ノート、復習、日記、服薬、睡眠・体調、運動まで、左のメニューから開けます。Hobby & Toolsは含みません。")
        for column, route, label in zip(st.columns(3), ("roadmap", "cards", "health"), ("学習ロードマップ", "記憶フラッシュ", "生活・健康の分析")):
            with column:
                st.page_link(pages[route], label=label)
    st.caption("体験用のサンプル入力をご利用ください。個人情報や実際の健康記録の保存先には使わないでください。Googleへの接続・送信は行いません。")


def about() -> None:
    ui.topbar("このDemoについて", "PROLOAGの主要機能を、一時データで操作できるWeb体験版。")
    st.markdown("""PROLOAGは、学習計画・時間計測・ノート・復習と、睡眠・体調・運動などの生活記録をまとめる個人開発アプリです。

このWeb版は、**Hobby & Toolsを除く主要機能**を体験できる構成です。学習・知識・生活の記録を追加・編集し、分析への反映まで試せます。表示基準日は **2026年11月5日**、初期データは架空の180日分です。""")
    st.subheader("データの扱い")
    st.write("体験データはブラウザのセッションごとに分かれています。共通サンプルやほかの人のデータは変更しません。メニューを移動しても、このタブの操作内容を引き継ぎます。")
    st.write("永続保存は行いません。ページの再読み込み・接続切れ・Cloudの再起動等で初期状態に戻る場合があります。サイドバーの『体験をリセット』でも初期状態に戻せます。長時間使わない一時データは自動的に削除します。")
    st.subheader("ローカル版との違い")
    st.write("Google連携は説明のみで、認証・外部送信は実行しません。Hobby & Toolsのメディア変換・文字起こし・OCRはこのWeb版には含みません。インポート・エクスポートも体験用のサンプルでお試しください。")
    st.subheader("利用条件と検証")
    st.write("PROLOAG本体の公開ライセンスは未確定です。第三者ソフトの利用条件はTHIRD_PARTY_NOTICES.mdとlicensesをご確認ください。")
    st.caption("Community Cloudでは起動待ちや、ブラウザの読み込み情報による表示の遅れが発生する場合があります。")


def _sidebar(pages):
    from web_demo.sandbox import reset_session_sandbox
    reset_scope = st.session_state["_proloag_sandbox"].token
    with st.sidebar:
        st.caption("PROLOAG · Web体験版")
        st.caption(f"架空データ / 基準日 {REFERENCE_DATE:%Y/%m/%d}")
        st.caption("追加・編集・保存はこのタブ専用。元のサンプルは変わりません。")
        st.toggle("ダークモード", key=ui.THEME_TOGGLE_KEY, value=ui.is_dark_theme(), on_change=ui._theme_toggle_changed)
        with st.expander("体験データを最初に戻す"):
            st.caption("このタブで追加・編集した記録と、タイマーの状態を初期化します。")
            confirmed = st.checkbox("このタブの操作内容をリセットする", key="_experience_reset_confirm_" + reset_scope)
            if st.button("体験をリセット", disabled=not confirmed, key="_experience_reset_" + reset_scope):
                handle = reset_session_sandbox()
                for key in list(st.session_state):
                    if key != "_proloag_sandbox":
                        del st.session_state[key]
                st.session_state["_proloag_sandbox"] = handle
                st.rerun()


def run() -> None:
    st.set_page_config(page_title="PROLOAG | Web Demo", page_icon="📘", layout="wide")
    try:
        from web_demo.sandbox import ensure_session_sandbox, sandbox_context
        handle = ensure_session_sandbox()
    except (OSError, ValueError, RuntimeError):
        LOGGER.exception("Web experience sandbox preparation failed")
        st.error("体験データを準備できませんでした。少し待ってから開き直してください。")
        st.stop()
    with sandbox_context(handle):
        pages = make_pages()
        st.session_state[PAGES_STATE_KEY] = pages
        nav = {"案内": [pages["welcome"], pages["about"]]}
        nav.update({section: [pages[route] for route, _, _ in items] for section, items in NAV_SECTIONS.items()})
        page = st.navigation(nav, expanded=True)
        with VIEW_RENDER_LOCK:
            ui.inject_css()
        _sidebar(pages)
        st.caption(f"WEB DEMO · 架空180日分 · {REFERENCE_DATE:%Y/%m/%d} 基準 · このタブ専用の体験データ")
        page.run()
