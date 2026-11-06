import streamlit as st
import prolog_ui.components as ab
from app_core.settings_service import load_user_settings
from app_core.transcription_ui import render_transcription
from config import PROJECT_ROOT

settings = load_user_settings()
st.set_page_config(page_title="音声・動画の文字起こし", layout="wide")
ab.inject_css(settings=settings)
ab.sidemenu("音声を、読み返せる知識へ。", settings=settings)
ab.topbar("音声・動画の文字起こし", "聞いた内容を、読み返せる文章へ。", eyebrow="HOBBY & TOOLS")
render_transcription(PROJECT_ROOT)
