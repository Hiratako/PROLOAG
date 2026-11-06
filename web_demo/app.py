from __future__ import annotations

import streamlit as st

from config import WEB_DEMO_READ_ONLY
from prolog_ui import components as ui
from web_demo.catalog import VIEW_RENDER_LOCK, render_product_view
from web_demo.data import PERIOD_START, REFERENCE_DATE, demo_summary, verify_demo_dataset
from web_demo.roadmap import render_roadmap_demo


def study():
    render_product_view("study")


def growth():
    render_product_view("growth")


def city():
    render_product_view("city")


def health():
    render_product_view("health")


def make_pages() -> dict:
    return {
        "welcome": st.Page(welcome, title="はじめに", icon=":material/home:", default=True),
        "roadmap": st.Page(render_roadmap_demo, title="学習ロードマップ", icon=":material/calendar_month:", url_path="roadmap"),
        "study": st.Page(study, title="学習の分析", icon=":material/monitoring:", url_path="study"),
        "growth": st.Page(growth, title="学習の蓄積", icon=":material/auto_awesome:", url_path="growth"),
        "city": st.Page(city, title="知識都市ビュー", icon=":material/location_city:", url_path="city"),
        "health": st.Page(health, title="生活・健康の分析", icon=":material/favorite:", url_path="health"),
        "about": st.Page(about, title="このDemoについて", icon=":material/info:", url_path="about"),
    }


def welcome() -> None:
    ui.topbar("PROLOAG", "学習の計画・実績・知識と、日々のコンディションをひとつに。", eyebrow="WEB DEMO")
    st.info("架空の180日分を使った閲覧用Demoです。期間や表示条件を自由に変えて、PROLOAGの画面を体験できます。")
    summary = demo_summary()
    columns = st.columns(4)
    values = [
        ("学習の記録", f"{summary['study_hours']:,.1f} 時間", f"{summary['sessions']:,} セッション"),
        ("学習した科目", f"{summary['subjects']} 科目", "中小企業診断士・英語などの架空データ"),
        ("知識の蓄積", f"{summary['notes']} ノート", f"{summary['cards']} 枚のカード"),
        ("記録の期間", f"{summary['days']} 日間", f"{PERIOD_START:%Y/%m/%d} — {REFERENCE_DATE:%Y/%m/%d}"),
    ]
    for column, (label, value, caption) in zip(columns, values):
        with column:
            ui.metric_block(label, value, caption)

    st.subheader("まずは、この順番で")
    pages = make_pages()
    steps = [
        ("01", "roadmap", "計画を見渡す", "学習工程と節目をガントチャートで確認。工程を選ぶと、予定・進捗・実績が分かります。"),
        ("02", "study", "実績を振り返る", "日別・曜日・科目などの切り口から、勉強時間と学習の傾向を確認できます。"),
        ("03", "growth", "積み重ねを眺める", "学習銀河・学習の航跡・クリスタル標本で、続けてきた学習を可視化します。"),
    ]
    for column, (number, route, title, text) in zip(st.columns(3), steps):
        with column:
            with st.container(border=True):
                st.caption(f"STEP {number}")
                st.markdown(f"**{title}**")
                st.write(text)
                st.page_link(pages[route], label="画面を開く", icon=":material/arrow_forward:")
    with st.container(border=True):
        st.markdown("**学習と暮らしをつなげて見る**")
        st.write("睡眠・体調・運動の記録を学習実績と同じ日付で確認できます。表示している数値や傾向は、すべて架空データから算出しています。")
        st.page_link(pages["health"], label="生活・健康の分析へ", icon=":material/favorite:")
        st.page_link(pages["city"], label="知識都市ビューへ", icon=":material/location_city:")
    st.caption("CSV出力は架空データのダウンロードです。保存・削除・ファイル取り込み・外部サービスへの接続は、この展示版では利用できません。")


def about() -> None:
    ui.topbar("このDemoについて", "PROLOAGの特徴と、展示版で体験できる範囲。")
    st.markdown("""PROLOAGは、勉強の計画・時間計測・ノート・復習と、睡眠・体調・運動などの生活記録をまとめるローカルアプリです。

このWeb展示版では、実際のアプリの分析・可視化画面とガント表示を、**架空の180日分のデータ**で体験できます。
表示基準日は **2026年11月5日** に固定しています。実ユーザーの記録や認証情報は使っていません。""")
    st.subheader("操作できること")
    st.write("期間・分類の絞り込み、グラフの表示変更、ガントの全体／月／週の切り替え、工程の詳細確認、架空データのCSVダウンロード。")
    st.subheader("ローカル版で利用する機能")
    st.write("記録の追加・保存・削除、秒給カウンター、ノート編集・復習、AI計画ファイルの取り込み、データ管理などは、ローカル版で利用する機能です。")
    st.write("Googleへの接続・送信、動画変換、文字起こし、OCRは、この展示版では実行できません。")
    st.subheader("データと利用条件")
    st.write("操作しても元の架空データは変わらず、他の閲覧者の表示条件にも影響しません。個人の勉強・健康データを入力する場所ではありません。")
    st.write("PROLOAG本体の公開ライセンスは未確定です。第三者ソフトの利用条件は同梱のTHIRD_PARTY_NOTICES.mdとlicensesをご確認ください。")
    st.caption("Community Cloudでは休止中のアプリを開く際に起動待ちが発生することがあります。")


def run() -> None:
    st.set_page_config(page_title="PROLOAG | Web Demo", page_icon="📘", layout="wide")
    pages = make_pages()
    # Register the allowlist even when data verification fails.
    page = st.navigation({
        "3分で体験": [pages[x] for x in ("welcome", "roadmap", "study", "growth")],
        "さらに見る": [pages[x] for x in ("city", "health", "about")],
    }, expanded=True)
    if not WEB_DEMO_READ_ONLY:
        st.error("展示版の設定を確認できないため、表示を停止しました。")
        st.stop()
    try:
        verify_demo_dataset()
    except (OSError, ValueError, RuntimeError):
        st.error("展示データを確認できませんでした。管理者が準備を確認するまでお待ちください。")
        st.stop()
    with VIEW_RENDER_LOCK:
        ui.inject_css()
    with st.sidebar:
        st.caption("PROLOAG · Web展示版")
        st.caption(f"架空データ / 基準日 {REFERENCE_DATE:%Y/%m/%d}")
        st.caption("保存・削除はできません。表示条件は閲覧者ごとに独立しています。")
    st.caption(f"WEB DEMO · 架空180日分 · {REFERENCE_DATE:%Y/%m/%d} 基準 · 閲覧専用")
    page.run()
