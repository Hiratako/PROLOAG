from __future__ import annotations

import streamlit as st

import prolog_ui.components as ab


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

st.set_page_config(page_title="データエクスポート", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("CSV出力、Googleへの学習共有、教材資料と引き継ぎメモを扱います。", settings=PAGE_SETTINGS)
ab.topbar("データエクスポート", "CSV、学習実績の共有、日次・週次の相談レポート。")

st.page_link("pages/96_Google連携ガイド.py", label="Google連携ガイドを開く")

output_mode = st.radio("出力方法", ["CSV・ZIPダウンロード", "Google Sheets共有", "Google Docs相談レポート", "資料リンク・引き継ぎ", "相談提案の取り込み"],
                       horizontal=True, key="export_output_mode")
if output_mode == "Google Sheets共有":
    from app_core.google_share_ui import render_google_share

    render_google_share()
    st.stop()

if output_mode == "Google Docs相談レポート":
    from app_core.consultation_report_ui import render_consultation_reports

    render_consultation_reports()
    st.stop()

if output_mode == "資料リンク・引き継ぎ":
    from config import PROJECT_ROOT
    from app_core.learning_context_ui import render_learning_context

    render_learning_context(PROJECT_ROOT, key_prefix="export_learning_context")
    st.stop()

if output_mode == "相談提案の取り込み":
    from config import PROJECT_ROOT
    from app_core.consultation_proposal_ui import render_proposal_inbox

    render_proposal_inbox(PROJECT_ROOT, key_prefix="export_proposal_inbox")
    st.stop()

from app_core.export_ui import render_csv_export

@st.fragment
def render_download_content() -> None:
    render_csv_export()


render_download_content()
