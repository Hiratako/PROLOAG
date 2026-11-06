import streamlit as st
import prolog_ui.components as ab
from app_core.settings_service import load_user_settings
from app_core.media_conversion_ui import render_media_conversion
from config import PROJECT_ROOT

settings = load_user_settings()
st.set_page_config(page_title="メディア変換", layout="wide")
ab.inject_css(settings=settings)
ab.sidemenu("動画の音声を、聞きやすいMP3へ。", settings=settings)
ab.topbar("メディア変換", "動画を、持ち歩ける音声へ。", eyebrow="HOBBY & TOOLS")
render_media_conversion(PROJECT_ROOT)
