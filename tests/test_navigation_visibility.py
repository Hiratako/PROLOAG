from __future__ import annotations

import ast
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app_core.navigation import NAV_GROUPS, SETTINGS_PAGE_PATH, hidden_sidebar_pages, visible_nav_groups
from app_core.json_store import JsonReadError
from prolog_ui import components


def all_paths():
    return [item["path"] for group in NAV_GROUPS for item in group["items"]]


class NavigationVisibilityTests(unittest.TestCase):
    def test_defaults_show_all_pages_in_original_order(self):
        for settings in (None, {}, {"display": {}}, {"display": {"hidden_sidebar_pages": []}}):
            with self.subTest(settings=settings):
                self.assertEqual(hidden_sidebar_pages(settings), set())
                self.assertEqual(visible_nav_groups(settings), NAV_GROUPS)

    def test_hide_all_leaves_settings_available_and_omits_empty_groups(self):
        settings = {"display": {"hidden_sidebar_pages": all_paths()}}
        self.assertEqual(visible_nav_groups(settings), [{
            "title": "Administration",
            "items": [{"title": "ユーザー設定", "path": SETTINGS_PAGE_PATH}],
        }])
        self.assertNotIn(SETTINGS_PAGE_PATH, hidden_sidebar_pages(settings))

    def test_malformed_sections_and_nonlist_values_are_ignored(self):
        for display in (None, [], False, "hidden"):
            self.assertEqual(hidden_sidebar_pages({"display": display}), set())
        for values in (None, False, "pages/02_勉強ログ.py", {"pages/02_勉強ログ.py": True}, (all_paths()[0],)):
            self.assertEqual(hidden_sidebar_pages({"display": {"hidden_sidebar_pages": values}}), set())

    def test_unknown_malformed_duplicate_and_protected_values_are_ignored(self):
        chosen = all_paths()[0]
        settings = {"display": {"hidden_sidebar_pages": [
            chosen, chosen, None, 17, False, [], {}, "", "../main.py",
            "main.py", "pages/unknown.py", SETTINGS_PAGE_PATH,
        ]}}
        self.assertEqual(hidden_sidebar_pages(settings), {chosen})

    def test_filter_preserves_order_and_does_not_modify_inputs_or_metadata(self):
        before_metadata = deepcopy(NAV_GROUPS)
        chosen = [all_paths()[0], all_paths()[9], all_paths()[-1]]
        settings = {"display": {"hidden_sidebar_pages": chosen, "theme_mode": "dark"}, "extra": 123}
        before_settings = deepcopy(settings)
        groups = visible_nav_groups(settings)
        self.assertEqual([item["path"] for group in groups for item in group["items"]],
                         [path for path in all_paths() if path not in chosen])
        groups[0]["title"] = "changed group"
        groups[0]["items"][0]["title"] = "changed item"
        groups[0]["items"].clear()
        self.assertEqual(settings, before_settings)
        self.assertEqual(NAV_GROUPS, before_metadata)

    def test_components_keeps_navigation_metadata_compatibility(self):
        self.assertIs(components.NAV_GROUPS, NAV_GROUPS)

    def test_google_guide_precedes_export_and_can_be_hidden(self):
        path = "pages/96_Google連携ガイド.py"
        administration = next(group for group in NAV_GROUPS if group["title"] == "Administration")
        paths = [item["path"] for item in administration["items"]]
        self.assertEqual(paths.index(path) + 1, paths.index("pages/97_データエクスポート.py"))
        settings = {"display": {"hidden_sidebar_pages": [path]}}
        self.assertEqual(hidden_sidebar_pages(settings), {path})
        visible = [item["path"] for group in visible_nav_groups(settings) for item in group["items"]]
        self.assertNotIn(path, visible)
        self.assertIn(SETTINGS_PAGE_PATH, visible)

    def test_user_guide_precedes_google_guide_and_can_be_hidden(self):
        path = "pages/95_使い方ガイド.py"
        administration = next(group for group in NAV_GROUPS if group["title"] == "Administration")
        paths = [item["path"] for item in administration["items"]]
        self.assertEqual(paths[:2], [path, "pages/96_Google連携ガイド.py"])
        settings = {"display": {"hidden_sidebar_pages": [path]}}
        self.assertEqual(hidden_sidebar_pages(settings), {path})
        visible = [item["path"] for group in visible_nav_groups(settings) for item in group["items"]]
        self.assertNotIn(path, visible)
        self.assertIn(SETTINGS_PAGE_PATH, visible)

    def test_portal_administration_matches_navigation_order(self):
        source = Path(__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        feature_groups = next(ast.literal_eval(node.value) for node in tree.body
                              if isinstance(node, ast.Assign) and any(
                                  isinstance(target, ast.Name) and target.id == "FEATURE_GROUPS"
                                  for target in node.targets))
        portal = next(group for group in feature_groups if group["title"] == "Administration")
        navigation = next(group for group in NAV_GROUPS if group["title"] == "Administration")
        self.assertEqual([{key: item[key] for key in ("title", "path")} for item in portal["items"]],
                         navigation["items"])


class SidebarRenderingTests(unittest.TestCase):
    def setUp(self):
        self.ui = SimpleNamespace(
            sidebar=nullcontext(), session_state={}, markdown=Mock(), caption=Mock(),
            toggle=Mock(), divider=Mock(), page_link=Mock(),
        )

    def render(self, settings):
        with patch.object(components, "st", self.ui), \
             patch.object(components, "load_user_settings", return_value=settings) as read, \
             patch.object(components, "save_user_settings") as write, \
             patch.object(components.Path, "exists", return_value=True):
            components.sidemenu("help")
            read.assert_called_once_with()
            write.assert_not_called()

    def test_actual_sidebar_keeps_portal_and_settings_when_all_hidden(self):
        self.render({"display": {"hidden_sidebar_pages": all_paths(), "theme_mode": "dark"}})
        self.assertEqual([call.args[0] for call in self.ui.page_link.call_args_list],
                         ["main.py", SETTINGS_PAGE_PATH])
        self.assertTrue(self.ui.session_state[components.THEME_TOGGLE_KEY])
        self.assertEqual(self.ui.session_state[components.THEME_MODE_STATE_KEY], "dark")
        headings = [call.args[0] for call in self.ui.markdown.call_args_list]
        self.assertEqual(sum("ab-sidebar-group" in text for text in headings), 1)

    def test_actual_sidebar_reads_updated_preferences_on_next_render(self):
        chosen = all_paths()[0]
        self.render({"display": {"hidden_sidebar_pages": [chosen]}})
        self.assertNotIn(chosen, [call.args[0] for call in self.ui.page_link.call_args_list])
        self.ui.page_link.reset_mock()
        self.render({"display": {"hidden_sidebar_pages": []}})
        self.assertEqual([call.args[0] for call in self.ui.page_link.call_args_list], ["main.py", *all_paths()])

    def test_actual_sidebar_tolerates_invalid_display_without_writing(self):
        self.render({"display": None})
        self.assertEqual(self.ui.page_link.call_count, len(all_paths()) + 1)
        self.assertFalse(self.ui.session_state[components.THEME_TOGGLE_KEY])

    def test_read_failure_is_not_silently_replaced_or_saved(self):
        with patch.object(components, "st", self.ui), \
             patch.object(components, "load_user_settings", side_effect=JsonReadError("broken")), \
             patch.object(components, "save_user_settings") as write:
            with self.assertRaises(JsonReadError):
                components.sidemenu()
            write.assert_not_called()
            self.ui.page_link.assert_not_called()


if __name__ == "__main__":
    unittest.main()
