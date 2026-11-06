import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import media_conversion_ui as ui
from app_core.navigation import NAV_GROUPS, hidden_sidebar_pages, visible_nav_groups
import test_media_conversion as fixtures


class MediaUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config

        config.get_config_options(
            options_from_flags={
                "global.developmentMode": False,
                "browser.gatherUsageStats": False,
            }
        )
        fixtures.MediaTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.MediaTests.tearDownClass()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pgmed_ui_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ui.get_manager(str(self.root.resolve()))
        self.addCleanup(self.manager.close)

    def app(self):
        from streamlit.testing.v1 import AppTest

        return AppTest.from_string(
            f"from pathlib import Path\nfrom app_core.media_conversion_ui import render_media_conversion\nrender_media_conversion(Path({str(self.root)!r}))",
            default_timeout=30,
        ).run()

    def widget(self, app, kind, label):
        return next(widget for widget in getattr(app, kind) if widget.label == label)

    def direct(self):
        app = self.app()
        self.widget(app, "radio", "動画の指定方法").set_value(
            "PC内のファイルを直接指定"
        ).run()
        self.widget(app, "text_input", "MP4ファイルの場所（フルパス）").set_value(
            str(fixtures.MediaTests.video)
        ).run()
        return app

    def test_opening_and_typing_do_not_probe_or_create_temp_storage(self):
        with patch.object(
            self.manager, "_source", side_effect=AssertionError("Unexpected probe")
        ):
            app = self.direct()
        self.assertFalse(app.exception)
        self.assertIsNone(self.manager._base)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_confirmation_is_explicit_and_quality_edits_do_not_reprobe(self):
        app = self.direct()
        self.widget(app, "button", "動画を確認").click().run()
        self.assertFalse(app.exception)
        owner = app.session_state["media_owner"]
        self.assertEqual(
            self.manager.snapshot(owner)["source"].name, fixtures.MediaTests.video.name
        )
        with patch.object(
            self.manager, "_source", side_effect=AssertionError("Unexpected probe")
        ):
            self.widget(app, "selectbox", "MP3の音質").set_value(320).run()
        self.assertFalse(app.exception)
        self.assertIsNone(self.manager.snapshot(owner)["job"])

    def test_new_path_requires_confirmation_before_conversion(self):
        app = self.direct()
        self.widget(app, "button", "動画を確認").click().run()
        self.widget(app, "text_input", "MP4ファイルの場所（フルパス）").set_value(
            str(self.root / "different.mp4")
        ).run()
        self.assertTrue(self.widget(app, "button", "MP3に変換").disabled)
        self.assertIsNone(
            self.manager.snapshot(app.session_state["media_owner"])["job"]
        )

    def test_conversion_results_and_direct_save_are_available(self):
        app = self.direct()
        self.widget(app, "button", "動画を確認").click().run()
        self.widget(app, "button", "MP3に変換").click().run()
        owner = app.session_state["media_owner"]
        fixtures.wait_for(self.manager, owner)
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(app.get("audio"))
        self.assertTrue(app.get("download_button"))
        job_id = self.manager.snapshot(owner)["job"]["id"]
        self.widget(app, "text_input", "保存先フォルダー（フルパス）").set_value(
            str(self.root)
        ).run()
        self.widget(app, "button", "PCへ直接保存").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(list(self.root.glob("*.mp3"))), 1)
        self.assertEqual(self.manager.snapshot(owner)["job"]["id"], job_id)

    def test_filename_requires_explicit_apply_and_does_not_reencode(self):
        app = self.direct()
        self.widget(app, "button", "動画を確認").click().run()
        self.widget(app, "button", "MP3に変換").click().run()
        owner = app.session_state["media_owner"]
        fixtures.wait_for(self.manager, owner)
        app.run()
        job_id = self.manager.snapshot(owner)["job"]["id"]
        with patch.object(
            self.manager, "start", side_effect=AssertionError("Unexpected reencode")
        ):
            self.widget(app, "text_input", "保存ファイル名").set_value(
                "変更した名前.mp3"
            )
            self.widget(app, "button", "保存名を適用").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(
            app.session_state["media_applied_name"]["name"], "変更した名前.mp3"
        )
        self.assertEqual(self.manager.snapshot(owner)["job"]["id"], job_id)

    def test_clear_removes_owned_temp_source_but_preserves_original_video(self):
        app = self.direct()
        self.widget(app, "button", "動画を確認").click().run()
        owner = app.session_state["media_owner"]
        source = self.manager.snapshot(owner)["source"]
        self.widget(app, "button", "結果・一時ファイルをクリア").click().run()
        self.assertFalse(app.exception)
        self.assertIsNone(self.manager.snapshot(owner)["source"])
        self.assertTrue(source.path.exists())

    def test_navigation_and_portal_share_the_new_section(self):
        group = next(group for group in NAV_GROUPS if group["title"] == "Hobby & Tools")
        self.assertEqual(
            group["items"],
            [{"title": "メディア変換", "path": "pages/20_メディア変換.py"},
             {"title": "音声・動画の文字起こし", "path": "pages/21_音声・動画の文字起こし.py"},
             {"title": "画像・PDFの文字読み取り", "path": "pages/22_画像・PDFの文字読み取り.py"}],
        )
        tree = ast.parse(
            (Path(__file__).resolve().parents[1] / "main.py").read_text(
                encoding="utf-8-sig"
            )
        )
        groups = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "FEATURE_GROUPS"
                for target in node.targets
            )
        )
        portal = next(group for group in groups if group["title"] == "Hobby & Tools")
        self.assertEqual([item["path"] for item in portal["items"]], [item["path"] for item in group["items"]])
        settings = {"display": {"hidden_sidebar_pages": ["pages/20_メディア変換.py"]}}
        self.assertEqual(hidden_sidebar_pages(settings), {"pages/20_メディア変換.py"})
        self.assertIn("Hobby & Tools", [group["title"] for group in visible_nav_groups(settings)])
        settings["display"]["hidden_sidebar_pages"].append("pages/21_音声・動画の文字起こし.py")
        settings["display"]["hidden_sidebar_pages"].append("pages/22_画像・PDFの文字読み取り.py")
        self.assertNotIn(
            "Hobby & Tools", [group["title"] for group in visible_nav_groups(settings)]
        )


if __name__ == "__main__":
    unittest.main()
