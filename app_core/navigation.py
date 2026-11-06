"""Navigation metadata and display-only sidebar preferences.

Hidden pages remain available through direct links and the portal. The settings
page cannot be hidden, so every preference can always be reversed from the UI.
"""
from __future__ import annotations

from typing import Any


SETTINGS_PAGE_PATH = "pages/98_ユーザー設定.py"

NAV_GROUPS = [
    {
        "title": "Study Management",
        "items": [
            {"title": "今日の記録・計画", "path": "pages/09_今日の学習プラン.py"},
            {"title": "学習ロードマップ", "path": "pages/19_学習ロードマップ.py"},
            {"title": "秒給カウンター", "path": "pages/01_時は金なり｜秒給カウンター.py"},
            {"title": "勉強ログ", "path": "pages/02_勉強ログ.py"},
            {"title": "可視化ダッシュボード", "path": "pages/03_可視化_ダッシュボード.py"},
            {"title": "学習パフォーマンス・条件分析", "path": "pages/17_学習パフォーマンス・条件分析.py"},
            {"title": "マイデータ統計演習", "path": "pages/05_高度AI分析.py"},
            {"title": "学習蓄積ビジュアライザー", "path": "pages/11_学習蓄積ビジュアライザー.py"},
            {"title": "マスタ管理", "path": "pages/10_マスタ管理.py"},
        ],
    },
    {
        "title": "Knowledge & Review",
        "items": [
            {"title": "ノート・知識ベース", "path": "pages/04_ノート・知識ベース.py"},
            {"title": "知識都市ビュー", "path": "pages/15_知識都市ビュー.py"},
            {"title": "記憶フラッシュ", "path": "pages/07_記憶フラッシュ.py"},
            {"title": "復習リマインダー", "path": "pages/08_忘却曲線リマインダー.py"},
        ],
    },
    {
        "title": "Life & Healthcare",
        "items": [
            {"title": "日記・ライフログ", "path": "pages/06_日記.py"},
            {"title": "服薬ログ", "path": "pages/18_カフェイン記録.py"},
            {"title": "睡眠・体調ログ", "path": "pages/12_睡眠・体調ログ.py"},
            {"title": "運動ログ", "path": "pages/13_運動ログ.py"},
            {"title": "生活・健康ダッシュボード", "path": "pages/14_生活・健康ダッシュボード.py"},
            {"title": "成分リズム・コンディション分析", "path": "pages/16_カフェイン・運動分析.py"},
        ],
    },
    {
        "title": "Hobby & Tools",
        "items": [
            {"title": "メディア変換", "path": "pages/20_メディア変換.py"},
            {"title": "音声・動画の文字起こし", "path": "pages/21_音声・動画の文字起こし.py"},
            {"title": "画像・PDFの文字読み取り", "path": "pages/22_画像・PDFの文字読み取り.py"},
        ],
    },
    {
        "title": "Administration",
        "items": [
            {"title": "使い方ガイド", "path": "pages/95_使い方ガイド.py"},
            {"title": "Google連携ガイド", "path": "pages/96_Google連携ガイド.py"},
            {"title": "データエクスポート", "path": "pages/97_データエクスポート.py"},
            {"title": "旧版ログ移行", "path": "pages/94_旧版ログ移行.py"},
            {"title": "ユーザー設定", "path": SETTINGS_PAGE_PATH},
            {"title": "データ整合性チェック", "path": "pages/99_データ整合性チェック.py"},
        ],
    },
]


def hidden_sidebar_pages(settings: Any) -> set[str]:
    """Read valid opt-out page paths without changing the settings object."""
    if not isinstance(settings, dict):
        return set()
    display = settings.get("display")
    if not isinstance(display, dict):
        return set()
    values = display.get("hidden_sidebar_pages")
    if not isinstance(values, list):
        return set()
    known = {item["path"] for group in NAV_GROUPS for item in group["items"]}
    return {
        value for value in values
        if isinstance(value, str) and value in known and value != SETTINGS_PAGE_PATH
    }


def visible_nav_groups(settings: Any) -> list[dict[str, Any]]:
    """Return independent ordered groups, omitting groups with no visible links."""
    hidden = hidden_sidebar_pages(settings)
    groups = []
    for group in NAV_GROUPS:
        items = [dict(item) for item in group["items"] if item["path"] not in hidden]
        if items:
            groups.append({"title": group["title"], "items": items})
    return groups
