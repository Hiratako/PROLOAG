from __future__ import annotations

from html import escape
import logging
from pathlib import Path
from typing import Any

import streamlit as st

from app_core.branding import BRAND_EXPANSION, BRAND_NAME
from app_core.navigation import NAV_GROUPS, hidden_sidebar_pages, visible_nav_groups
from app_core.plot_style import configure_plot_theme
from app_core.settings_service import load_user_settings, save_user_settings
from prolog_ui.theme import THEME_DARK, get_theme_palette, normalize_theme_mode


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
FLASH_MESSAGE_KEY = "_proloag_flash_message"
TOAST_MESSAGE_KEY = "_proloag_toast_messages"
THEME_MODE_STATE_KEY = "_proloag_theme_mode"
THEME_TOGGLE_KEY = "_proloag_dark_theme_toggle"
_LOGGER = logging.getLogger(__name__)

def current_theme_mode(*, settings: dict[str, Any] | None = None) -> str:
    if THEME_MODE_STATE_KEY not in st.session_state:
        settings = load_user_settings() if settings is None else settings
        display = settings.get("display", {})
        saved = display.get("theme_mode", "standard") if isinstance(display, dict) else "standard"
        st.session_state[THEME_MODE_STATE_KEY] = normalize_theme_mode(saved)
    return normalize_theme_mode(st.session_state.get(THEME_MODE_STATE_KEY))


def is_dark_theme() -> bool:
    return current_theme_mode() == THEME_DARK


def theme_palette(*, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    return get_theme_palette(current_theme_mode(settings=settings))


def reset_theme_state() -> None:
    st.session_state.pop(THEME_MODE_STATE_KEY, None)
    st.session_state.pop(THEME_TOGGLE_KEY, None)


def _theme_toggle_changed() -> None:
    mode = THEME_DARK if bool(st.session_state.get(THEME_TOGGLE_KEY)) else "standard"
    st.session_state[THEME_MODE_STATE_KEY] = mode
    settings = load_user_settings()
    settings.setdefault("display", {})["theme_mode"] = mode
    save_user_settings(settings)


def _theme_css() -> str:
    palette = theme_palette()
    dark = palette["mode"] == THEME_DARK
    primary_soft = "rgba(57, 213, 255, 0.14)" if dark else "rgba(0, 59, 113, 0.12)"
    focus_ring = "rgba(57, 213, 255, 0.28)" if dark else "rgba(0, 59, 113, 0.24)"
    shadow = "rgba(0, 0, 0, 0.30)" if dark else "rgba(15, 35, 55, 0.08)"
    sidebar_background = (
        "linear-gradient(180deg, #06111F 0%, #0B1728 100%)" if dark else palette["card"]
    )
    app_background = (
        "radial-gradient(circle at 18% 12%, rgba(57,213,255,.13), transparent 28rem), "
        "radial-gradient(circle at 82% 8%, rgba(86,240,166,.09), transparent 24rem), "
        "linear-gradient(135deg, #06111F 0%, #081827 48%, #020611 100%)"
        if dark
        else f"linear-gradient(180deg, {palette['background_top']} 0%, {palette['background']} 42%, {palette['background_bottom']} 100%)"
    )
    grid_content = '""' if dark else "none"
    return f"""
    <style>
    :root, .stApp {{
      --ab-primary: {palette['primary']};
      --ab-primary-2: {palette['primary_2']};
      --ab-accent: {palette['accent']};
      --ab-ink: {palette['ink']};
      --ab-muted: {palette['muted']};
      --ab-line: {palette['line']};
      --ab-bg: {palette['background']};
      --ab-card: {palette['card']};
      --ab-panel: {palette['panel']};
      --ab-panel-2: {palette['panel_2']};
      --ab-input: {palette['input']};
      --ab-primary-soft: {primary_soft};
      --ab-focus-ring: {focus_ring};
      --ab-shadow: {shadow};
      --ab-app-background: {app_background};
      --ab-sidebar-background: {sidebar_background};
      --ab-button-bg: #003B71;
      --ab-button-hover: #075C9F;
      --ab-button-active: #002D57;
      --ab-button-disabled: #60758A;
      --ab-button-text: #FFFFFF;
    }}
    .stApp {{
      background: var(--ab-app-background);
      color: var(--ab-ink);
    }}
    .stApp::before {{
      content: {grid_content};
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(255,255,255,.028) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.028) 1px, transparent 1px);
      background-size: 42px 42px;
      mask-image: linear-gradient(to bottom, rgba(0,0,0,.72), transparent 85%);
    }}
    [data-testid="stSidebar"] {{
      background: var(--ab-sidebar-background);
      color: var(--ab-ink);
      border-right-color: var(--ab-line);
    }}
    [data-testid="stHeader"] {{
      background: var(--ab-bg);
      color: var(--ab-ink);
    }}
    [data-testid="stHeader"] button,
    [data-testid="stToolbar"] button {{
      color: var(--ab-ink) !important;
    }}
    .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
    .stApp label, .stApp [data-testid="stCaptionContainer"],
    .stApp [data-testid="stMarkdownContainer"] {{
      color: var(--ab-ink);
    }}
    .ab-topbar, .ab-topbar * {{ color: #FFFFFF !important; }}
    .stApp div[data-baseweb="input"] > div,
    .stApp div[data-baseweb="select"] > div,
    .stApp textarea,
    .stApp [data-testid="stNumberInput"] input,
    .stApp [data-testid="stDateInput"] input {{
      background-color: var(--ab-input) !important;
      color: var(--ab-ink) !important;
      border-color: var(--ab-line) !important;
    }}
    .stApp div[data-baseweb="popover"] > div,
    .stApp div[data-baseweb="menu"] {{
      background-color: var(--ab-panel) !important;
      color: var(--ab-ink) !important;
    }}
    .stApp [data-testid="stExpander"] details,
    .stApp [data-testid="stForm"],
    .stApp [data-testid="stVerticalBlockBorderWrapper"] {{
      background-color: var(--ab-panel);
      border-color: var(--ab-line);
    }}
    .stApp .stButton > button,
    .stApp .stFormSubmitButton > button,
    .stApp .stDownloadButton > button,
    .stApp .stLinkButton > a {{
      background: var(--ab-button-bg) !important;
      border-color: var(--ab-button-bg) !important;
      color: var(--ab-button-text) !important;
    }}
    .stApp .stButton > button *,
    .stApp .stFormSubmitButton > button *,
    .stApp .stDownloadButton > button *,
    .stApp .stLinkButton > a * {{
      color: var(--ab-button-text) !important;
    }}
    .stApp .stButton > button:hover:not(:disabled),
    .stApp .stFormSubmitButton > button:hover:not(:disabled),
    .stApp .stDownloadButton > button:hover:not(:disabled),
    .stApp .stLinkButton > a:hover {{
      background: var(--ab-button-hover) !important;
      border-color: var(--ab-button-hover) !important;
      color: var(--ab-button-text) !important;
    }}
    .stApp .stButton > button:active:not(:disabled),
    .stApp .stFormSubmitButton > button:active:not(:disabled),
    .stApp .stDownloadButton > button:active:not(:disabled),
    .stApp .stLinkButton > a:active {{
      background: var(--ab-button-active) !important;
      border-color: var(--ab-button-active) !important;
    }}
    .stApp .stButton > button:disabled,
    .stApp .stFormSubmitButton > button:disabled,
    .stApp .stDownloadButton > button:disabled {{
      background: var(--ab-button-disabled) !important;
      border-color: var(--ab-button-disabled) !important;
      color: var(--ab-button-text) !important;
      opacity: 0.62;
    }}
    .stApp [data-testid="stDataFrame"] {{
      border: 1px solid var(--ab-line);
      border-radius: 8px;
      overflow: hidden;
    }}
    </style>
    """


def inject_css(*, settings: dict[str, Any] | None = None) -> None:
    if settings is not None and THEME_MODE_STATE_KEY not in st.session_state:
        display = settings.get("display", {})
        saved = display.get("theme_mode", "standard") if isinstance(display, dict) else "standard"
        st.session_state[THEME_MODE_STATE_KEY] = normalize_theme_mode(saved)
    css_path = PACKAGE_DIR / "styles.css"
    if css_path.exists():
        st.markdown(f"<style>{css_path.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)
    st.markdown(_theme_css(), unsafe_allow_html=True)
    configure_plot_theme(is_dark_theme())
    show_queued_message()
    show_queued_toasts()


def _append_notification(key: str, message: str, kind: str) -> None:
    message = str(message).strip()
    if not message:
        return
    payload = {"kind": kind, "message": message}
    queued = st.session_state.get(key)
    if isinstance(queued, list):
        st.session_state[key] = [*queued, payload]
    elif isinstance(queued, dict):
        # Preserve the legacy single-dict format until another event arrives.
        st.session_state[key] = [queued, payload]
    else:
        st.session_state[key] = payload


def queue_message(message: str, *, kind: str = "success") -> None:
    """Show an action's inline message and toast on the next page render."""
    _append_notification(FLASH_MESSAGE_KEY, message, kind)


def queue_success(message: str) -> None:
    queue_message(message, kind="success")


def queue_toast(message: str, *, kind: str = "success") -> None:
    """Queue only a toast when the caller already retains its inline result.

    Use one queue per action: queue_message/queue_success already include a
    toast, so the same event must not also be registered here.
    """
    _append_notification(TOAST_MESSAGE_KEY, message, kind)


def _notification_kind(kind: str) -> str:
    return kind if kind in ("success", "info", "warning", "error") else "info"


def _show_action_toast(message: str, kind: str) -> None:
    message = str(message).strip()
    if not message:
        return
    icon = {"success": "✅", "info": "ℹ️", "warning": "⚠️", "error": "❌"}[kind]
    try:
        st.toast(message, icon=icon, duration="long")
    except TypeError as exc:
        # Older Streamlit versions do not accept duration. Only that precise
        # signature mismatch warrants retrying; other errors are not replayed.
        detail = str(exc)
        if "unexpected keyword argument" in detail and (
            "'duration'" in detail or '"duration"' in detail
        ):
            try:
                st.toast(message, icon=icon)
            except Exception as fallback_exc:
                _LOGGER.warning("Action toast delivery failed (%s)", type(fallback_exc).__name__)
        else:
            _LOGGER.warning("Action toast delivery failed (%s)", type(exc).__name__)
    except Exception as exc:
        # This boundary covers only native toast delivery. A completed save
        # must retain its inline result even if the optional popup fails.
        # Exception messages can contain private input; log only their type.
        _LOGGER.warning("Action toast delivery failed (%s)", type(exc).__name__)


def show_action_message(
    message: str, *, kind: str = "success", toast_message: str | None = None,
) -> None:
    """Display an action result inline and as a ten-second native toast."""
    message = str(message).strip()
    if not message:
        return
    kind = _notification_kind(str(kind))
    renderer = {
        "success": st.success,
        "info": st.info,
        "warning": st.warning,
        "error": st.error,
    }[kind]
    renderer(message)
    _show_action_toast(message if toast_message is None else toast_message, kind)


def show_action_toast(message: str, *, kind: str = "success") -> None:
    """Notify an action immediately when its inline status is retained elsewhere."""
    _show_action_toast(message, _notification_kind(str(kind)))


def show_action_error(
    message: str, *, toast_message: str = "処理を完了できませんでした。詳細表示を確認してください。",
) -> None:
    """Retain error details inline while keeping the popup short."""
    show_action_message(message, kind="error", toast_message=toast_message)


def _consume_notifications(key: str) -> list[dict[str, Any]]:
    # Pop before display so a later rerun cannot replay a delivered event.
    payload = st.session_state.pop(key, None)
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def show_queued_message() -> None:
    for payload in _consume_notifications(FLASH_MESSAGE_KEY):
        show_action_message(
            str(payload.get("message", "")), kind=str(payload.get("kind", "success")),
        )


def show_queued_toasts() -> None:
    for payload in _consume_notifications(TOAST_MESSAGE_KEY):
        _show_action_toast(
            str(payload.get("message", "")), _notification_kind(str(payload.get("kind", "success"))),
        )


def topbar(title: str, subtitle: str = "", *, eyebrow: str | None = None) -> None:
    safe_title = escape(str(title))
    safe_subtitle = escape(str(subtitle))
    safe_eyebrow = escape(str(eyebrow if eyebrow is not None else f"{BRAND_NAME} / {BRAND_EXPANSION}"))
    st.markdown(
        f"""
        <section class="ab-topbar">
          <div>
            <p class="ab-eyebrow">{safe_eyebrow}</p>
            <h1>{safe_title}</h1>
            <p>{safe_subtitle}</p>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def sidemenu(help_text: str = "", *, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    from config import WEB_DEMO_READ_ONLY
    if WEB_DEMO_READ_ONLY:
        # The exhibition entrypoint owns the explicit, read-only navigation.
        # Do not expose local administration, recovery or persisted theme UI.
        return load_user_settings() if settings is None else settings
    settings = load_user_settings() if settings is None else settings
    if THEME_MODE_STATE_KEY not in st.session_state:
        display = settings.get("display", {}) if isinstance(settings, dict) else {}
        saved_theme = display.get("theme_mode", "standard") if isinstance(display, dict) else "standard"
        st.session_state[THEME_MODE_STATE_KEY] = normalize_theme_mode(saved_theme)
    with st.sidebar:
        from app_core.save_recovery_ui import render_save_recovery
        render_save_recovery(PROJECT_ROOT)
        st.markdown(f'<div class="ab-sidebar-title">{BRAND_NAME} Navigation</div>', unsafe_allow_html=True)
        if help_text:
            st.caption(help_text)
        if THEME_TOGGLE_KEY not in st.session_state:
            st.session_state[THEME_TOGGLE_KEY] = is_dark_theme()
        st.toggle(
            "ダークモード",
            key=THEME_TOGGLE_KEY,
            on_change=_theme_toggle_changed,
            help="アプリ全体の配色を切り替え、次回起動時にも引き継ぎます。",
        )
        st.divider()
        st.page_link("main.py", label=f"{BRAND_NAME} ポータル")
        st.divider()
        for group in visible_nav_groups(settings):
            st.markdown(f'<div class="ab-sidebar-group">{escape(str(group["title"]))}</div>', unsafe_allow_html=True)
            for item in group["items"]:
                path = item.get("path", "")
                if path and (PROJECT_ROOT / path).exists():
                    st.page_link(path, label=item["title"])
                else:
                    st.caption(f"{item['title']}（準備中）")
            st.divider()
    return settings


def sidebar_page_link(path: str, label: str, *, settings: dict[str, Any]) -> None:
    """Apply the shared sidebar preference to a page-specific shortcut."""
    from config import WEB_DEMO_READ_ONLY
    if WEB_DEMO_READ_ONLY:
        return
    if path not in hidden_sidebar_pages(settings):
        st.page_link(path, label=label)


def card(title: str, body: str = "", *, icon: str = "", class_name: str = "") -> None:
    icon_html = f'<span class="ab-card-icon">{escape(str(icon))}</span>' if icon else ""
    safe_title = escape(str(title))
    safe_body = escape(str(body)).replace("\n", "<br>")
    safe_class = escape(str(class_name), quote=True)
    st.markdown(
        f"""
        <div class="ab-card {safe_class}">
          <div class="ab-card-title">{icon_html}<span>{safe_title}</span></div>
          <div class="ab-card-body">{safe_body}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def metric_block(label: str, value: Any, caption: str = "", *, icon: str = "") -> None:
    icon_html = f'<span class="ab-metric-icon">{escape(str(icon))}</span>' if icon else ""
    safe_label = escape(str(label))
    safe_value = escape(str(value))
    safe_caption = escape(str(caption))
    st.markdown(
        f"""
        <div class="ab-metric">
          <div class="ab-metric-label">{icon_html}{safe_label}</div>
          <div class="ab-metric-value">{safe_value}</div>
          <div class="ab-metric-caption">{safe_caption}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
