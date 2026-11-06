from __future__ import annotations

from web_demo.runtime import fragment as demo_fragment

import streamlit as st

import prolog_ui.components as ab
from app_core.google_guide_ui import render_google_guide


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

st.set_page_config(page_title="Google連携ガイド", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("Googleへの共有と、相談結果を記録へ戻すための利用ガイドです。", settings=PAGE_SETTINGS)
ab.topbar("Google連携ガイド", "ローカル版で利用するGoogle連携の仕組みと操作を紹介します。")
st.info("このWeb体験版ではGoogleへのログイン・接続・送信は行いません。以下はローカル版向けの利用ガイドです。CSV出力や相談提案の取り込みは、この体験版でも試せます。")
@demo_fragment
def render_guide_content() -> None:
    render_google_guide()


render_guide_content()
