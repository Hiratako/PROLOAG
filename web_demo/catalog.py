"""Only these existing product views can be executed by the exhibition."""
import builtins
from pathlib import Path
import threading
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
READ_ONLY_VIEWS = {
    "study": "03_可視化_ダッシュボード.py",
    "growth": "11_学習蓄積ビジュアライザー.py",
    "city": "15_知識都市ビュー.py",
    "health": "14_生活・健康ダッシュボード.py",
}
DEMO_ROUTES = ("welcome", "roadmap", "study", "growth", "city", "health", "about")
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
    if route not in READ_ONLY_VIEWS:
        raise ValueError("This page is not part of the web exhibition.")
    path = ROOT / "reference_pages" / READ_ONLY_VIEWS[route]
    _execute_view(path)
