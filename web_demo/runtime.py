"""Bind visitor storage for partial reruns as well as complete page renders."""
from functools import wraps
import streamlit as st


def fragment(func=None, **options):
    def decorate(original):
        @wraps(original)
        def isolated(*args, **kwargs):
            from web_demo.catalog import VIEW_RENDER_LOCK
            from web_demo.sandbox import ensure_session_sandbox, sandbox_context
            handle = ensure_session_sandbox()
            with sandbox_context(handle), VIEW_RENDER_LOCK:
                return original(*args, **kwargs)
        return st.fragment(isolated, **options)
    return decorate(func) if func is not None else decorate
