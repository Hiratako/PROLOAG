import streamlit as st
import prolog_ui.components as ab
from app_core.settings_service import load_user_settings
from app_core.roadmap_ui import render_roadmap
from config import get_data_root

PROJECT_ROOT = get_data_root()

settings = load_user_settings()
st.set_page_config(page_title="学習ロードマップ", layout="wide")
ab.inject_css(settings=settings)
ab.sidemenu(
    "数週間〜数か月の学習工程と、日々のタスク・実績をつなぎます。", settings=settings
)
st.markdown("""<style>
[data-testid="stMainBlockContainer"] {padding:4.5rem 1.6rem 2rem;max-width:none;}
.st-key-roadmap_toolbar button[kind="secondary"] {background:var(--ab-card)!important;color:var(--ab-ink)!important;border:1px solid var(--ab-line)!important;box-shadow:none!important;}
.stApp .st-key-roadmap_toolbar .stButton > button[kind="secondary"] * {color:var(--ab-ink)!important;}
.st-key-roadmap_toolbar button[kind="secondary"]:hover {background:var(--ab-panel-2)!important;}
@media(max-width:640px){[data-testid="stMainBlockContainer"]{padding:4.5rem .8rem 1rem;}}
</style>""", unsafe_allow_html=True)
st.header("学習ロードマップ")
st.caption("学習工程・期限・日別の配分を、同じ画面で確認します。")
render_roadmap(PROJECT_ROOT)
