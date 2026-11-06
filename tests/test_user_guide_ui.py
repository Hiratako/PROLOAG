"""Read-only app guide: standalone rendering without personal or Google data."""
from __future__ import annotations

import ast
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from app_core import json_store
from app_core import user_guide_ui as ui
from app_core.navigation import NAV_GROUPS, visible_nav_groups


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUIDE_PAGE = "pages/95_使い方ガイド.py"
GOOGLE_GUIDE_PAGE = "pages/96_Google連携ガイド.py"
EXPECTED_TOPICS = (
    "はじめに", "最初の設定", "毎日の記録", "学習・復習",
    "振り返り・分析", "記録の修正", "保存・共有", "困ったとき",
)


class UserGuideUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config as streamlit_config

        streamlit_config.get_config_options(options_from_flags={
            "global.developmentMode": False, "browser.gatherUsageStats": False,
        })

    def setUp(self):
        # The inline AppTest script has no registered multipage navigation.
        page_link = patch.object(ui.st, "page_link")
        self.page_link = page_link.start()
        self.addCleanup(page_link.stop)

    @staticmethod
    def app():
        from streamlit.testing.v1 import AppTest

        return AppTest.from_string(
            "from app_core.user_guide_ui import render_user_guide\n"
            "render_user_guide()\n", default_timeout=30,
        ).run()

    @staticmethod
    def rendered_text(app):
        return "\n".join(
            str(item.value)
            for kind in ("markdown", "caption", "text", "info", "warning", "success", "subheader", "code")
            for item in app.get(kind)
        )

    @staticmethod
    def diagrams():
        return {name: value for name, value in vars(ui).items()
                if name.endswith("_SVG") and isinstance(value, str)}

    def test_eight_topics_render_practical_guidance_without_exceptions(self):
        self.assertEqual(tuple(ui.TOPICS), EXPECTED_TOPICS)
        expected = {
            "はじめに": ("今日の記録・計画",),
            "最初の設定": ("ユーザー設定", "マスタ管理"),
            "毎日の記録": ("今日の記録・計画", "服薬ログ"),
            "学習・復習": ("ノート", "復習"),
            "振り返り・分析": ("ダッシュボード", "統計"),
            "記録の修正": ("勉強ログ", "保存"),
            "保存・共有": ("CSV", "Google"),
            "困ったとき": ("入力", "再読込"),
        }
        app = self.app()
        rendered_topics = set()
        for topic in ui.TOPICS:
            with self.subTest(topic=topic):
                app.radio(key="user_guide_topic").set_value(topic).run()
                self.assertFalse(app.exception)
                rendered_topics.add(self.rendered_text(app))
                for fragment in expected[topic]:
                    self.assertIn(fragment, self.rendered_text(app))
                self.assertEqual(app.radio(key="user_guide_topic").value, topic)
        self.assertEqual(len(rendered_topics), len(EXPECTED_TOPICS))

    def test_repeated_navigation_renders_one_topic_selector_with_unique_keys(self):
        app = self.app()
        for _ in range(2):
            for topic in ui.TOPICS:
                app.radio(key="user_guide_topic").set_value(topic).run()
                self.assertFalse(app.exception)
                self.assertEqual(len(app.radio), 1)
                self.assertEqual(list(app.radio[0].options), list(EXPECTED_TOPICS))
                keys = [item.key for kind in ("radio", "selectbox", "checkbox")
                        for item in app.get(kind) if item.key is not None]
                self.assertEqual(len(keys), len(set(keys)))

    def test_all_topics_avoid_record_reads_writes_and_network(self):
        with ExitStack() as stack:
            guards = []
            for name in ("load_json_safe", "save_json_safe", "_read", "backup_json", "record_json_files"):
                guards.append(stack.enter_context(patch.object(
                    json_store, name, side_effect=AssertionError("Guide must not access personal records"),
                )))
            guards.append(stack.enter_context(patch(
                "socket.socket.connect", side_effect=AssertionError("Guide must not connect to the network"),
            )))
            app = self.app()
            for topic in ui.TOPICS:
                app.radio(key="user_guide_topic").set_value(topic).run()
                self.assertFalse(app.exception)
                for kind in ("button", "text_input", "text_area", "file_uploader"):
                    self.assertEqual(len(app.get(kind)), 0, (topic, kind))
            for guard in guards:
                guard.assert_not_called()

    def test_import_and_render_work_without_google_clients_or_credentials(self):
        blocked = {name: None for name in (
            "googleapiclient", "google_auth_oauthlib", "google.auth",
            "app_core.google_sheets_client", "app_core.google_docs_client",
            "app_core.google_share_service", "app_core.google_share_ui",
            "app_core.consultation_report_service", "app_core.consultation_report_ui",
        )}
        spec = importlib.util.spec_from_file_location("_offline_user_guide_test", ui.__file__)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, blocked):
            spec.loader.exec_module(module)
            app = self.app()
            for topic in ui.TOPICS:
                app.radio(key="user_guide_topic").set_value(topic).run()
                self.assertFalse(app.exception)
        self.assertTrue(callable(module.render_user_guide))
        self.assertEqual(module.TOPICS, ui.TOPICS)

    def test_svg_diagrams_have_accessible_text_and_no_active_or_external_content(self):
        namespace = "{http://www.w3.org/2000/svg}"
        diagrams = self.diagrams()
        self.assertIn(len(diagrams), (3, 4))
        for name, source in diagrams.items():
            with self.subTest(diagram=name):
                svg = ET.fromstring(source)
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

    def test_rendered_images_are_only_the_embedded_explanatory_diagrams(self):
        with patch.object(ui.st, "image", wraps=ui.st.image) as shown:
            app = self.app()
            for topic in ui.TOPICS:
                app.radio(key="user_guide_topic").set_value(topic).run()
                self.assertFalse(app.exception)
            displayed = {call.args[0] if call.args else call.kwargs["image"]
                         for call in shown.call_args_list}
        self.assertEqual(displayed, set(self.diagrams().values()))
        self.assertTrue(all(item.lstrip().startswith("<svg") for item in displayed))

    def test_all_navigation_targets_exist_and_google_uses_the_separate_guide(self):
        app = self.app()
        for topic in ui.TOPICS:
            app.radio(key="user_guide_topic").set_value(topic).run()
            self.assertFalse(app.exception)
        targets = {call.args[0] if call.args else call.kwargs["page"]
                   for call in self.page_link.call_args_list}
        self.assertIn(GOOGLE_GUIDE_PAGE, targets)
        for target in targets:
            with self.subTest(page=target):
                path = (PROJECT_ROOT / target).resolve()
                self.assertTrue(path.is_relative_to(PROJECT_ROOT))
                self.assertTrue(path.is_file())
                self.assertEqual(path.suffix, ".py")

    def test_guide_is_registered_once_and_sidebar_hiding_is_reversible(self):
        matches = [item for group in NAV_GROUPS for item in group["items"] if item["path"] == GUIDE_PAGE]
        self.assertEqual(matches, [{"title": "使い方ガイド", "path": GUIDE_PAGE}])
        self.assertTrue((PROJECT_ROOT / GUIDE_PAGE).is_file())
        hidden = visible_nav_groups({"display": {"hidden_sidebar_pages": [GUIDE_PAGE]}})
        self.assertNotIn(GUIDE_PAGE, [item["path"] for group in hidden for item in group["items"]])
        restored = visible_nav_groups({})
        self.assertIn(GUIDE_PAGE, [item["path"] for group in restored for item in group["items"]])

    def test_page_entry_delegates_once_to_the_readonly_guide(self):
        tree = ast.parse((PROJECT_ROOT / GUIDE_PAGE).read_text(encoding="utf-8-sig"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertEqual(sum(isinstance(node.func, ast.Name) and node.func.id == "render_user_guide"
                             for node in calls), 1)


if __name__ == "__main__":
    unittest.main()
