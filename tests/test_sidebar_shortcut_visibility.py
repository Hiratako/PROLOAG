from __future__ import annotations

import ast
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_core.navigation import NAV_GROUPS, SETTINGS_PAGE_PATH
from prolog_ui import components


ROOT = Path(__file__).resolve().parents[1]


class SidebarShortcutVisibilityTests(unittest.TestCase):
    def test_hidden_shortcut_is_omitted_without_reading_or_writing_settings(self):
        path = "pages/09_今日の学習プラン.py"
        settings = {"display": {"hidden_sidebar_pages": [path]}}
        before = deepcopy(settings)
        with patch.object(components.st, "page_link") as link, \
             patch.object(components, "load_user_settings") as read, \
             patch.object(components, "save_user_settings") as write:
            components.sidebar_page_link(path, "今日の記録・計画を開く", settings=settings)
            link.assert_not_called()
            read.assert_not_called()
            write.assert_not_called()
        self.assertEqual(settings, before)

    def test_visible_shortcut_preserves_path_and_label(self):
        path = "pages/06_日記.py"
        for settings in ({}, {"display": {}}, {"display": {"hidden_sidebar_pages": []}}):
            with self.subTest(settings=settings), patch.object(components.st, "page_link") as link:
                components.sidebar_page_link(path, "日記を開く", settings=settings)
                link.assert_called_once_with(path, label="日記を開く")

    def test_settings_shortcut_cannot_be_hidden(self):
        settings = {"display": {"hidden_sidebar_pages": [SETTINGS_PAGE_PATH]}}
        with patch.object(components.st, "page_link") as link:
            components.sidebar_page_link(SETTINGS_PAGE_PATH, "設定を開く", settings=settings)
            link.assert_called_once_with(SETTINGS_PAGE_PATH, label="設定を開く")

    def test_sidebar_returns_the_same_snapshot_without_additional_reads(self):
        paths = [item["path"] for group in NAV_GROUPS for item in group["items"]]
        settings = {"display": {"hidden_sidebar_pages": paths}}
        ui = SimpleNamespace(
            sidebar=nullcontext(), session_state={}, markdown=Mock(), caption=Mock(),
            toggle=Mock(), divider=Mock(), page_link=Mock(),
        )
        with patch.object(components, "st", ui), \
             patch.object(components, "load_user_settings", return_value=settings) as read, \
             patch.object(components, "save_user_settings") as write, \
             patch.object(components.Path, "exists", return_value=True):
            snapshot = components.sidemenu()
            for path in paths:
                components.sidebar_page_link(path, path, settings=snapshot)
            self.assertIs(snapshot, settings)
            read.assert_called_once_with()
            write.assert_not_called()
        self.assertEqual([call.args[0] for call in ui.page_link.call_args_list],
                         ["main.py", SETTINGS_PAGE_PATH, SETTINGS_PAGE_PATH])

    def test_target_pages_filter_only_sidebar_shortcuts(self):
        expected = {
            "01_時は金なり｜秒給カウンター.py": ["pages/09_今日の学習プラン.py"],
            "06_日記.py": ["pages/18_カフェイン記録.py", "pages/12_睡眠・体調ログ.py", "pages/13_運動ログ.py"],
            "18_カフェイン記録.py": ["pages/16_カフェイン・運動分析.py", SETTINGS_PAGE_PATH, "pages/06_日記.py"],
        }
        for filename, targets in expected.items():
            with self.subTest(filename=filename):
                tree = ast.parse((ROOT / "pages" / filename).read_text(encoding="utf-8-sig"))
                self.assertTrue(any(
                    isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "sidebar_settings" for target in node.targets)
                    and isinstance(node.value, ast.Call)
                    and ast.unparse(node.value.func) == "ab.sidemenu"
                    for node in tree.body
                ))
                sidebar_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.With)
                                 and any(ast.unparse(item.context_expr) == "st.sidebar" for item in node.items)]
                calls = [call for node in sidebar_nodes for call in ast.walk(node) if isinstance(call, ast.Call)]
                self.assertFalse(any(ast.unparse(call.func) == "st.page_link" for call in calls))
                shortcuts = [call for call in calls if ast.unparse(call.func) == "ab.sidebar_page_link"]
                self.assertEqual([ast.literal_eval(call.args[0]) for call in shortcuts], targets)
                for call in shortcuts:
                    self.assertTrue(any(keyword.arg == "settings" and ast.unparse(keyword.value) == "sidebar_settings"
                                        for keyword in call.keywords))

    def test_diary_body_medication_link_is_not_filtered(self):
        tree = ast.parse((ROOT / "pages" / "06_日記.py").read_text(encoding="utf-8-sig"))
        body_links = [node.value for node in ast.walk(tree) if isinstance(node, ast.Expr)
                      and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "st.page_link"]
        self.assertEqual([ast.literal_eval(call.args[0]) for call in body_links], ["pages/18_カフェイン記録.py"])


if __name__ == "__main__":
    unittest.main()
