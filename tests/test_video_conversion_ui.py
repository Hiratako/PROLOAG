"""Explicit conversion decisions and the existing page route, with no media IO."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_core import video_conversion_ui as ui
from app_core.media_conversion import MediaError, MediaManager


class FakeVideoManager:
    def __init__(self, root):
        self.root = root
        self.source = self.plan_value = self.job = None
        self.prepared = []
        self.planned = []
        self.started = []
        self.cancelled = []
        self.plan_error = None

    def prune(self):
        pass

    def snapshot(self, owner):
        return {"source": self.source, "plan": self.plan_value, "job": self.job}

    def prepare_local(self, owner, path):
        self.prepared.append((owner, path))
        self.source = SimpleNamespace(
            id="a" * 32, path=Path(path), name=Path(path).name, mode="local",
            identity=(1024, 0, 0, 0), duration=8.0,
            videos=(SimpleNamespace(index=0, codec="h264", language="", title="", width=1280, height=720),),
            audios=(SimpleNamespace(index=1, codec="aac", language="jpn", title="主音声", default=False),
                    SimpleNamespace(index=2, codec="aac", language="eng", title="English", default=True)),
            subtitles=(SimpleNamespace(index=3, codec="subrip", language="jpn", title="字幕"),
                       SimpleNamespace(index=4, codec="ass", language="jpn", title="字幕")),
        )
        self.plan_value = None
        return self.source

    def plan(self, owner, source_id, **values):
        self.planned.append((owner, source_id, dict(values)))
        if self.plan_error:
            raise self.plan_error
        self.plan_value = SimpleNamespace(
            id=str(len(self.planned)) * 32,
            target=Path(values["directory"]) / values["name"],
            video_codec="copy" if values["mode"] == "copy" else "libx264",
            audio_codec="copy" if values["audio_index"] is not None else None,
            subtitle_codec="mov_text" if values["subtitle_index"] is not None else None,
            estimated_bytes=8192, expected_duration=8.0,
            warnings=("元のMKVは変更しません。",),
            dropped_tracks=("トラック 4: ASS字幕は出力に含みません。",),
        )
        return self.plan_value

    def start(self, owner, plan_id, *, operation_id):
        self.started.append((owner, plan_id, operation_id))
        self.job = {
            "id": "b" * 32, "status": "done", "plan": self.plan_value,
            "output_path": str(self.plan_value.target), "size": 4096,
            "error": "", "details": "", "elapsed": 0.1,
        }
        return self.job["id"]

    def cancel(self, owner):
        self.cancelled.append(owner)
        self.job["status"] = "cancelled"

    def clear(self, owner):
        self.source = self.plan_value = self.job = None


class VideoConversionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config

        config.get_config_options(options_from_flags={
            "global.developmentMode": False, "browser.gatherUsageStats": False,
        })

    def setUp(self):
        self.temp = TemporaryDirectory(prefix="proloag_mkv_ui_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = FakeVideoManager(self.root)
        self.inject = patch.object(ui, "get_video_manager", return_value=self.manager)
        self.inject.start()
        self.addCleanup(self.inject.stop)

    def app(self):
        from streamlit.testing.v1 import AppTest

        return AppTest.from_string(
            "from pathlib import Path\n"
            "from app_core.video_conversion_ui import render_video_conversion\n"
            f"render_video_conversion(Path({str(self.root)!r}))",
            default_timeout=30,
        ).run()

    @staticmethod
    def widget(app, kind, label):
        return next(widget for widget in getattr(app, kind) if widget.label == label)

    def prepared(self):
        app = self.app()
        self.widget(app, "text_input", "MKVファイルの場所（フルパス）").set_value(
            str(self.root / "日本語 動画.mkv")
        ).run()
        self.widget(app, "button", "動画を確認").click().run()
        self.assertFalse(app.exception)
        return app

    def planned(self):
        app = self.prepared()
        self.widget(app, "button", "変換内容を確認").click().run()
        self.assertFalse(app.exception)
        return app

    def test_open_and_path_typing_do_not_prepare_plan_start_or_create_files(self):
        app = self.app()
        self.widget(app, "text_input", "MKVファイルの場所（フルパス）").set_value(
            str(self.root / "日本語 動画.mkv")
        ).run()
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(self.manager.prepared, [])
        self.assertEqual(self.manager.planned, [])
        self.assertEqual(self.manager.started, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_confirmation_prepares_once_and_setting_changes_do_not_plan_or_start(self):
        app = self.prepared()
        self.assertEqual(self.widget(app, "selectbox", "音声").value, 2)
        self.widget(app, "radio", "変換方式").set_value("compatible").run()
        self.widget(app, "selectbox", "音声").set_value(None).run()
        self.widget(app, "selectbox", "字幕").set_value(3).run()
        self.assertFalse(app.exception)
        self.assertEqual(len(self.manager.prepared), 1)
        self.assertEqual(self.manager.planned, [])
        self.assertEqual(self.manager.started, [])
        self.assertTrue(self.widget(app, "button", "MP4に変換して保存").disabled)

    def test_explicit_plan_shows_omissions_and_does_not_start_conversion(self):
        app = self.planned()
        self.assertEqual(len(self.manager.prepared), 1)
        self.assertEqual(len(self.manager.planned), 1)
        self.assertEqual(self.manager.planned[0][2]["audio_index"], 2)
        self.assertFalse(self.widget(app, "button", "MP4に変換して保存").disabled)
        self.assertTrue(any("ASS字幕" in item.value for item in app.text))
        self.assertEqual(self.manager.started, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_changed_settings_invalidate_confirmed_plan_until_explicit_replan(self):
        app = self.planned()
        self.widget(app, "text_input", "保存ファイル名").set_value("別の保存名.mp4").run()
        self.assertTrue(self.widget(app, "button", "MP4に変換して保存").disabled)
        self.assertEqual(len(self.manager.planned), 1)
        self.assertEqual(self.manager.started, [])
        self.widget(app, "button", "変換内容を確認").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(self.manager.planned), 2)
        self.assertFalse(self.widget(app, "button", "MP4に変換して保存").disabled)

    def test_changed_input_path_blocks_old_plan_without_automatic_probe(self):
        app = self.planned()
        self.widget(app, "text_input", "MKVファイルの場所（フルパス）").set_value(
            str(self.root / "別の動画.mkv")
        ).run()
        self.assertFalse(app.exception)
        self.assertTrue(self.widget(app, "button", "MP4に変換して保存").disabled)
        self.assertTrue(self.widget(app, "button", "変換内容を確認").disabled)
        self.assertEqual(len(self.manager.prepared), 1)
        self.assertEqual(len(self.manager.planned), 1)

    def test_explicit_start_shows_saved_mp4_and_rerun_does_not_repeat_start(self):
        app = self.planned()
        self.widget(app, "button", "MP4に変換して保存").click().run()
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(len(self.manager.started), 1)
        self.assertTrue(any("MP4を保存しました" in item.value for item in app.success))
        self.assertIn(str(self.manager.plan_value.target), [item.value for item in app.text])
        self.assertTrue(self.widget(app, "button", "MP4に変換して保存").disabled)

    def test_plan_failure_keeps_source_and_never_starts_on_rerun(self):
        app = self.prepared()
        self.manager.plan_error = MediaError("この映像は高速方式に対応していません。")
        self.widget(app, "button", "変換内容を確認").click().run()
        app.run()
        self.assertFalse(app.exception)
        self.assertIsNotNone(self.manager.source)
        self.assertEqual(len(self.manager.planned), 1)
        self.assertEqual(self.manager.started, [])
        self.assertTrue(self.widget(app, "button", "MP4に変換して保存").disabled)

    def test_page_keeps_original_converter_as_default_and_switches_to_mkv(self):
        from streamlit.testing.v1 import AppTest
        from app_core import media_conversion_ui as old_ui
        import config
        import prolog_ui.components as components
        from app_core import settings_service

        old_manager = MediaManager(self.root)
        self.addCleanup(old_manager.close)
        page = Path(__file__).resolve().parents[1] / "pages/20_メディア変換.py"
        with patch.object(config, "PROJECT_ROOT", self.root), \
                patch.object(settings_service, "load_user_settings", return_value={}), \
                patch.object(components, "inject_css"), \
                patch.object(components, "sidemenu"), \
                patch.object(components, "topbar"), \
                patch.object(old_ui, "get_manager", return_value=old_manager):
            app = AppTest.from_file(str(page), default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertEqual(self.widget(app, "radio", "変換の種類").value, "MP4 → MP3")
            self.assertTrue(any(item.label == "MP4ファイルを選択" for item in app.get("file_uploader")))
            self.widget(app, "radio", "変換の種類").set_value("MKV → MP4").run()
            self.assertFalse(app.exception)
            self.assertTrue(any(item.label == "MKVファイルの場所（フルパス）" for item in app.text_input))
            self.widget(app, "radio", "変換の種類").set_value("MP4 → MP3").run()
            self.assertFalse(app.exception)
            self.assertIsNone(old_manager._base)
        self.assertEqual(self.manager.prepared, [])
        self.assertEqual(self.manager.planned, [])
        self.assertEqual(self.manager.started, [])


if __name__ == "__main__":
    unittest.main()
