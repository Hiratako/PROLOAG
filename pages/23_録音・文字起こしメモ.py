import streamlit as st

import prolog_ui.components as ab
from app_core.audio_memo_ui import render_audio_memo
from app_core.settings_service import load_user_settings
from config import PROJECT_ROOT


settings = load_user_settings()
st.set_page_config(page_title="録音・文字起こしメモ", layout="wide")
ab.inject_css(settings=settings)
ab.sidemenu("話したことを、そのまま読み返せるメモに。", settings=settings)
ab.topbar("録音・文字起こしメモ", "録音を残して、文章に整える。", eyebrow="HOBBY & TOOLS")
render_audio_memo(PROJECT_ROOT)
