"""Explicit product routes for the isolated, non-Hobby web experience."""
import builtins
from pathlib import Path
import threading
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_VIEWS = {
    "today": "09_今日の学習プラン.py",
    "roadmap": "19_学習ロードマップ.py",
    "timer": "01_時は金なり｜秒給カウンター.py",
    "logs": "02_勉強ログ.py",
    "study": "03_可視化_ダッシュボード.py",
    "performance": "17_学習パフォーマンス・条件分析.py",
    "statistics": "05_高度AI分析.py",
    "growth": "11_学習蓄積ビジュアライザー.py",
    "masters": "10_マスタ管理.py",
    "notes": "04_ノート・知識ベース.py",
    "city": "15_知識都市ビュー.py",
    "cards": "07_記憶フラッシュ.py",
    "review": "08_忘却曲線リマインダー.py",
    "diary": "06_日記.py",
    "substances": "18_カフェイン記録.py",
    "sleep": "12_睡眠・体調ログ.py",
    "exercise": "13_運動ログ.py",
    "health": "14_生活・健康ダッシュボード.py",
    "condition": "16_カフェイン・運動分析.py",
    "guide": "95_使い方ガイド.py",
    "google-guide": "96_Google連携ガイド.py",
    "export": "97_データエクスポート.py",
    "legacy-import": "94_旧版ログ移行.py",
    "settings": "98_ユーザー設定.py",
    "integrity": "99_データ整合性チェック.py",
}
# Compatibility subset retained for existing analysis-view regression checks.
READ_ONLY_VIEWS = {
    "study": "03_可視化_ダッシュボード.py",
    "growth": "11_学習蓄積ビジュアライザー.py",
    "city": "15_知識都市ビュー.py",
    "health": "14_生活・健康ダッシュボード.py",
}
DEMO_ROUTES = ("welcome", *PRODUCT_VIEWS, "about")
VIEW_RENDER_LOCK = threading.RLock()


def _execute_view(path: Path) -> dict[str, Any]:
    """Execute an allowlisted view in a visitor's private module namespace.

    Unlike runpy.run_path, this does not temporarily replace sys.argv or
    sys.modules while another visitor is rendering a page. The selected product
    views use absolute imports and do not require module registration.
    """
    path = Path(path).resolve(strict=True)
    namespace: dict[str, Any] = {
        "__name__": "__proloag_exhibition__",
        "__file__": str(path),
        "__package__": None,
        "__spec__": None,
        "__cached__": None,
        "__loader__": None,
        "__builtins__": builtins,
    }
    code = compile(path.read_bytes(), str(path), "exec")
    # Matplotlib's rcParams/pyplot state is shared by the process. Keep product
    # rendering serial while each visitor retains their own Streamlit context.
    with VIEW_RENDER_LOCK:
        exec(code, namespace)
    return namespace


def render_product_view(route: str) -> None:
    if route not in PRODUCT_VIEWS:
        raise ValueError("This page is not part of the web experience.")
    from web_demo.sandbox import ensure_session_sandbox, sandbox_context
    handle = ensure_session_sandbox()
    path = ROOT / "reference_pages" / PRODUCT_VIEWS[route]
    with sandbox_context(handle):
        _execute_view(path)
