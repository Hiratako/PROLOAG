"""The actual portal Demo banner must work with ASCII-only Windows date formats."""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


PORTAL = Path(__file__).resolve().parents[1] / "main.py"


class AsciiOnlyDate(date):
    """Model the locale encoding error observed on the English Windows runner."""

    @staticmethod
    def _check_format(spec):
        try:
            spec.encode("ascii")
        except UnicodeEncodeError as exc:
            raise UnicodeEncodeError("locale", spec, exc.start, exc.end, "non-ASCII date format") from None

    def strftime(self, spec):
        self._check_format(spec)
        return super().strftime(spec)

    def __format__(self, spec):
        self._check_format(spec)
        return super().__format__(spec)


def portal_demo_banner():
    # Execute the real display branch without importing the application or
    # opening its settings, JSON records, navigation, or Streamlit server.
    tree = ast.parse(PORTAL.read_text(encoding="utf-8-sig"), filename=str(PORTAL))
    branches = [node for node in tree.body if isinstance(node, ast.If)
                and isinstance(node.test, ast.Call)
                and isinstance(node.test.func, ast.Name)
                and node.test.func.id == "is_demo_mode"]
    if len(branches) != 1:
        raise AssertionError("Expected exactly one portal Demo banner branch")
    return compile(ast.Module(body=branches, type_ignores=[]), str(PORTAL), "exec")


class PortalDemoBannerTests(unittest.TestCase):
    def test_fixed_date_banner_does_not_use_locale_encoded_date_formats(self):
        code = portal_demo_banner()
        for target, expected in (
            (AsciiOnlyDate(2026, 11, 5), "展示用デモ / 表示基準日: 2026年11月05日"),
            (AsciiOnlyDate(2026, 1, 2), "展示用デモ / 表示基準日: 2026年01月02日"),
        ):
            with self.subTest(day=target.isoformat()):
                with self.assertRaises(UnicodeEncodeError):
                    target.strftime("%Y年%m月%d日")
                with self.assertRaises(UnicodeEncodeError):
                    format(target, "%Y年%m月%d日")
                info = Mock()
                exec(code, {"today": target, "is_demo_mode": lambda: True,
                            "demo_mode_label": lambda: "展示用デモ", "st": SimpleNamespace(info=info)})
                info.assert_called_once_with(expected)

    def test_normal_mode_does_not_display_a_demo_banner(self):
        info, label = Mock(), Mock()
        exec(portal_demo_banner(), {"today": AsciiOnlyDate(2026, 11, 5),
                                   "is_demo_mode": lambda: False, "demo_mode_label": label,
                                   "st": SimpleNamespace(info=info)})
        info.assert_not_called()
        label.assert_not_called()


if __name__ == "__main__":
    unittest.main()
