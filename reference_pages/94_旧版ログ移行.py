from __future__ import annotations

from web_demo.runtime import fragment as demo_fragment

import streamlit as st

import prolog_ui.components as ab
from app_core.legacy_study_import_ui import render_legacy_study_import
from app_core.settings_service import load_user_settings
from config import get_data_root

PROJECT_ROOT = get_data_root()


PAGE_SETTINGS = load_user_settings()
st.set_page_config(page_title="旧版ログ移行", layout="wide")
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("旧式カウンターのCSVを確認してから取り込みます。", settings=PAGE_SETTINGS)
ab.topbar("旧版ログ移行", "元CSVを残して診断・変換し、確認した記録だけを追加します。")


@demo_fragment
def render_import_content() -> None:
    render_legacy_study_import(PROJECT_ROOT)


render_import_content()
