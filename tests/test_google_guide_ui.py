"""Offline guide rendering: no account, records, or live server are needed."""
from __future__ import annotations

import ast
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from app_core import google_guide_ui as ui
from app_core import json_store
from app_core.navigation import NAV_GROUPS, visible_nav_groups


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUIDE_PAGE = "pages/96_Google連携ガイド.py"
EXPORT_PAGE = "pages/97_データエクスポート.py"


class GoogleGuideUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config as streamlit_config

        streamlit_config.get_config_options(options_from_flags={
            "global.developmentMode": False,
            "browser.gatherUsageStats": False,
        })

    def setUp(self):
        # AppTest.from_string has no registered multi-page navigation.
        page_link = patch.object(ui.st, "page_link")
        self.page_link = page_link.start()
        self.addCleanup(page_link.stop)

    @staticmethod
    def app():
        from streamlit.testing.v1 import AppTest

        return AppTest.from_string(
            "from app_core.google_guide_ui import render_google_guide\n"
            "render_google_guide()\n",
            default_timeout=30,
        ).run()

    @staticmethod
    def rendered_text(app):
        return "\n".join(
            str(item.value)
            for kind in ("markdown", "caption", "info", "warning", "success", "subheader", "code")
            for item in app.get(kind)
        )

    def test_all_six_topics_render_without_exceptions(self):
        self.assertEqual(len(ui.TOPICS), 6)
        self.assertEqual(len(set(ui.TOPICS)), 6)
        expected = {
            "はじめに": "自動同期",
            "初回設定": "Desktop app",
            "Sheets共有": "専用表の管理タブの置き換え",
            "Docsレポート": "日次／週次Docの本文を置き換え",
            "相談結果を戻す": "承認して採用・反映",
            "困ったとき": "同じ内容を再送",
        }
        app = self.app()
        for topic in ui.TOPICS:
            with self.subTest(topic=topic):
                app.radio(key="google_guide_topic").set_value(topic).run()
                self.assertFalse(app.exception)
                self.assertIn(expected[topic], self.rendered_text(app))
                self.assertEqual(app.radio(key="google_guide_topic").value, topic)

    def test_all_four_installation_variants_use_the_matching_environment(self):
        app = self.app()
        app.radio(key="google_guide_topic").set_value("初回設定").run()
        expected = {
            "ソース版 / Python 3.11": ("py -3.11", ".deps-google311"),
            "ソース版 / Python 3.12": ("py -3.12", ".deps-google312"),
            "ポータブル版 / PROLOAG.exe": ("runtime\\python311\\python.exe", "app\\.deps-google311"),
            "仮想環境版": ("python -m pip install -r requirements-google.txt",),
        }
        self.assertEqual(set(app.selectbox(key="google_guide_environment").options), set(expected))
        for variant, fragments in expected.items():
            with self.subTest(variant=variant):
                app.selectbox(key="google_guide_environment").set_value(variant).run()
                self.assertFalse(app.exception)
                command = app.code[0].value
                for fragment in fragments:
                    self.assertIn(fragment, command)
                if "3.11" in variant or variant.startswith("ポータブル"):
                    self.assertNotIn("312", command)
                elif "3.12" in variant:
                    self.assertNotIn("311", command)
                else:
                    self.assertNotIn("--target", command)
                self.assertIn("PowerShell", self.rendered_text(app))

    def test_repeated_navigation_and_environment_changes_have_unique_widget_keys(self):
        app = self.app()
        for _ in range(2):
            for topic in ui.TOPICS:
                app.radio(key="google_guide_topic").set_value(topic).run()
                if topic == "初回設定":
                    for variant in app.selectbox(key="google_guide_environment").options:
                        app.selectbox(key="google_guide_environment").set_value(variant).run()
                        self.assertFalse(app.exception)
                self.assertFalse(app.exception)
                keys = [item.key for kind in ("radio", "selectbox") for item in app.get(kind)]
                self.assertEqual(len(keys), len(set(keys)))

    def test_every_topic_is_read_only_without_network_or_record_access(self):
        with ExitStack() as stack:
            guards = []
            for name in ("load_json_safe", "save_json_safe", "_read", "backup_json", "record_json_files"):
                guards.append(stack.enter_context(patch.object(
                    json_store, name, side_effect=AssertionError("The guide must not access record JSON"),
                )))
            guards.append(stack.enter_context(patch(
                "socket.socket.connect", side_effect=AssertionError("The guide must not connect to the network"),
            )))
            app = self.app()
            for topic in ui.TOPICS:
                app.radio(key="google_guide_topic").set_value(topic).run()
                self.assertFalse(app.exception)
                # Navigation and documentation links are not submit actions.
                self.assertEqual(len(app.button), 0)
                self.assertEqual(len(app.text_input), 0)
                self.assertEqual(len(app.text_area), 0)
            for guard in guards:
                guard.assert_not_called()

    def test_guide_import_does_not_need_google_clients_or_optional_dependencies(self):
        blocked = {
            name: None for name in (
                "googleapiclient", "google_auth_oauthlib", "google.auth",
                "app_core.google_sheets_client", "app_core.google_docs_client",
                "app_core.google_share_service", "app_core.google_report_service",
            )
        }
        spec = importlib.util.spec_from_file_location("_offline_google_guide_test", ui.__file__)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, blocked):
            spec.loader.exec_module(module)
            app = self.app()
            for topic in ui.TOPICS:
                app.radio(key="google_guide_topic").set_value(topic).run()
                self.assertFalse(app.exception)
        self.assertTrue(callable(module.render_google_guide))
        self.assertEqual(module.TOPICS, ui.TOPICS)

    def test_svg_diagrams_are_local_accessible_markup_without_scripts_or_external_images(self):
        namespace = "{http://www.w3.org/2000/svg}"
        for name in ("FLOW_SVG", "SETUP_SVG", "SHARE_SVG"):
            with self.subTest(diagram=name):
                svg = ET.fromstring(getattr(ui, name))
                self.assertEqual(svg.tag, namespace + "svg")
                self.assertEqual(svg.attrib.get("role"), "img")
                self.assertTrue(svg.findtext(namespace + "title"))
                self.assertTrue(svg.findtext(namespace + "desc"))
                self.assertEqual(len(svg.attrib["viewBox"].split()), 4)
                for node in svg.iter():
                    tag = node.tag.rsplit("}", 1)[-1].lower()
                    self.assertNotIn(tag, ("script", "image", "foreignobject", "iframe"))
                    for key, value in node.attrib.items():
                        local_key = key.rsplit("}", 1)[-1].lower()
                        self.assertFalse(local_key.startswith("on"), key)
                        self.assertNotIn(local_key, ("href", "src"))
                        self.assertNotIn("url(", value.lower())

    def test_guide_is_registered_once_and_sidebar_hiding_is_reversible(self):
        matches = [item for group in NAV_GROUPS for item in group["items"] if item["path"] == GUIDE_PAGE]
        self.assertEqual(matches, [{"title": "Google連携ガイド", "path": GUIDE_PAGE}])
        self.assertTrue((PROJECT_ROOT / GUIDE_PAGE).is_file())
        hidden = visible_nav_groups({"display": {"hidden_sidebar_pages": [GUIDE_PAGE]}})
        self.assertNotIn(GUIDE_PAGE, [item["path"] for group in hidden for item in group["items"]])
        restored = visible_nav_groups({})
        self.assertIn(GUIDE_PAGE, [item["path"] for group in restored for item in group["items"]])

    def test_entry_page_delegates_to_guide_and_links_to_export(self):
        source = (PROJECT_ROOT / GUIDE_PAGE).read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertEqual(sum(isinstance(node.func, ast.Name) and node.func.id == "render_google_guide" for node in calls), 1)
        app = self.app()
        self.assertFalse(app.exception)
        self.page_link.assert_called_with(EXPORT_PAGE, label="実際の操作画面：データエクスポートを開く")
        self.assertTrue((PROJECT_ROOT / EXPORT_PAGE).is_file())


if __name__ == "__main__":
    unittest.main()
