import streamlit as st
import prolog_ui.components as ab
from app_core.settings_service import load_user_settings
from app_core.ocr_ui import render_ocr
from config import PROJECT_ROOT

settings = load_user_settings()
st.set_page_config(page_title="画像・PDFの文字読み取り", layout="wide")
ab.inject_css(settings=settings)
ab.sidemenu("教材の文章を、使える知識へ。", settings=settings)
ab.topbar("画像・PDFの文字読み取り", "教材の文章を、使える知識へ。", eyebrow="HOBBY & TOOLS")
render_ocr(PROJECT_ROOT)
