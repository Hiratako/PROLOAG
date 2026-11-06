from __future__ import annotations

from web_demo.runtime import fragment as demo_fragment

import streamlit as st

import prolog_ui.components as ab
from app_core.user_guide_ui import render_user_guide


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

st.set_page_config(page_title="使い方ガイド", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("最初の設定から、日々の記録・振り返り・データの保管まで。", settings=PAGE_SETTINGS)
ab.topbar("使い方ガイド", "今日の記録を、次の物語へ。自分に合う使い方から始めましょう。")
@demo_fragment
def render_guide_content() -> None:
    render_user_guide()


render_guide_content()
