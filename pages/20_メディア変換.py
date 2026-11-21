import streamlit as st
import prolog_ui.components as ab
from app_core.settings_service import load_user_settings
from app_core.media_conversion_ui import render_media_conversion
from config import PROJECT_ROOT

settings = load_user_settings()
st.set_page_config(page_title="メディア変換", layout="wide")
ab.inject_css(settings=settings)
ab.sidemenu("動画の形式変更と音声の取り出し。", settings=settings)
ab.topbar("メディア変換", "動画をMP4へ、動画の音声をMP3へ。", eyebrow="HOBBY & TOOLS")
conversion = st.radio(
    "変換の種類", ["MP4 → MP3", "MKV → MP4"], horizontal=True, key="media_conversion_kind"
)
if conversion == "MKV → MP4":
    from app_core.video_conversion_ui import render_video_conversion

    render_video_conversion(PROJECT_ROOT)
else:
    render_media_conversion(PROJECT_ROOT)
