from __future__ import annotations

from datetime import date, datetime
from typing import Any

from config import SETTINGS_DIR
from app_core.json_store import load_json_safe


DEMO_MODE_PATH = SETTINGS_DIR / "demo_mode.json"


def load_demo_mode() -> dict[str, Any]:
    """デモモード設定を安全に読み込みます。"""
    data = load_json_safe(DEMO_MODE_PATH, {})
    return data if isinstance(data, dict) else {}


def app_today() -> date:
    """画面表示や日次データ参照に使用する、アプリ上の基準日を返します。"""
    settings = load_demo_mode()
    if settings.get("enabled"):
        try:
            return date.fromisoformat(str(settings.get("fixed_today", ""))[:10])
        except (TypeError, ValueError):
            pass
    return date.today()


def app_now() -> datetime:
    """アプリ基準日と実時刻を組み合わせた日時を返します。"""
    now = datetime.now()
    today = app_today()
    return datetime.combine(today, now.time())


def is_demo_mode() -> bool:
    """有効な固定日が設定されたデモモードかを判定します。"""
    settings = load_demo_mode()
    if not settings.get("enabled"):
        return False
    try:
        date.fromisoformat(str(settings.get("fixed_today", ""))[:10])
    except (TypeError, ValueError):
        return False
    return True


def demo_mode_label() -> str:
    """画面表示用のデモモード名称を返します。"""
    settings = load_demo_mode()
    return str(settings.get("demo_name") or "デモモード")
