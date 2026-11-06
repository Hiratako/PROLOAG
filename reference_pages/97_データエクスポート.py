from __future__ import annotations

from web_demo.runtime import fragment as demo_fragment

import streamlit as st

import prolog_ui.components as ab


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

st.set_page_config(page_title="データエクスポート", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("体験中の記録をCSV・ZIPへ出力し、教材資料や相談提案を管理します。", settings=PAGE_SETTINGS)
ab.topbar("データエクスポート", "体験用データのダウンロード、資料リンク、相談提案の取り込み。")
st.caption("出力されるのは、このブラウザの体験用データです。個人の実記録や認証情報を入力・取り込みしないでください。")

ab.product_page_link("pages/96_Google連携ガイド.py", label="Google連携ガイドを開く")

output_mode = st.radio("出力方法", ["CSV・ZIPダウンロード", "資料リンク・引き継ぎ", "相談提案の取り込み", "Google連携について"],
                       horizontal=True, key="export_output_mode")
if output_mode == "Google連携について":
    st.subheader("Google連携はローカル版で利用できます")
    st.write("ローカル版では、集計結果をGoogle Sheetsへ共有したり、日次・週次の相談レポートをGoogle Docsへ出力できます。送信前のプレビューと確認を経て、本人が操作します。")
    st.info("公開Web体験版では、Googleアカウントへの接続や外部への送信を提供していません。架空データのCSV・ZIP出力、資料リンクの編集、相談提案の取り込みは体験できます。")
    ab.product_page_link("pages/96_Google連携ガイド.py", label="ローカル版のGoogle連携ガイドを見る")
    st.stop()

if output_mode == "資料リンク・引き継ぎ":
    from config import get_data_root
    from app_core.learning_context_ui import render_learning_context

    render_learning_context(get_data_root(), key_prefix="export_learning_context")
    st.stop()

if output_mode == "相談提案の取り込み":
    from config import get_data_root
    from app_core.consultation_proposal_ui import render_proposal_inbox

    render_proposal_inbox(get_data_root(), key_prefix="export_proposal_inbox")
    st.stop()

from app_core.export_ui import render_csv_export

@demo_fragment
def render_download_content() -> None:
    render_csv_export()


render_download_content()
