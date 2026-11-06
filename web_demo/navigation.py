"""Resolve original product shortcuts through this visitor's registered pages."""
from pathlib import PurePosixPath
import streamlit as st
from web_demo.catalog import PRODUCT_VIEWS

PAGES_STATE_KEY = "_web_experience_pages"


def page_for(path):
    pages = st.session_state.get(PAGES_STATE_KEY, {})
    if not isinstance(path, str):
        return path
    if path == "main.py":
        return pages.get("welcome")
    name = PurePosixPath(path.replace("\\", "/")).name
    route = next((key for key, value in PRODUCT_VIEWS.items() if value == name), None)
    return pages.get(route) if route else None


def product_page_link(path, *, label, **kwargs):
    page = page_for(path)
    if page is None:
        st.caption(label)
        return
    st.page_link(page, label=label, **kwargs)
