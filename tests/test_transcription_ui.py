from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from app_core import transcription_ui as ui
from app_core.transcription import TranscriptionStore, atomic_json


class TranscriptionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config

        config.get_config_options(
            options_from_flags={
                "global.developmentMode": False,
                "browser.gatherUsageStats": False,
            }
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pgasr_ui_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TranscriptionStore(self.root)
        self.job_id = "a" * 32
        self.manager = ui.source_manager(str(self.root))
        self.addCleanup(self.manager.close)

    def app(self):
        from streamlit.testing.v1 import AppTest

        return AppTest.from_string(
            f"from pathlib import Path\nfrom app_core.transcription_ui import render_transcription\nrender_transcription(Path({str(self.root)!r}))",
            default_timeout=30,
        ).run()

    def seed(self, status="done", job_id=None):
        job_id = job_id or self.job_id
        folder = self.store.folder(job_id)
        folder.mkdir(parents=True)
        atomic_json(
            folder / "state.json",
            {
                "id": job_id,
                "status": status,
                "created": time.time(),
                "name": "日英のテスト.mp3",
                "elapsed": 1.0,
                "progress": 1.0,
                "error": "",
            },
        )
        atomic_json(
            folder / "request.json",
            {"model": "small", "language": "ja", "duration": 3.0},
        )
        (folder / "segments.jsonl").write_text(
            json.dumps({"id": 0, "start": 0.0, "end": 2.0, "original_text": "元の文章"})
            + "\n",
            encoding="utf-8",
        )

    def button(self, app, label):
        return next(b for b in app.button if b.label == label)

    def test_open_and_path_typing_do_not_prepare_or_create_storage(self):
        with patch.object(
            self.manager, "_source", side_effect=AssertionError("no probe")
        ):
            app = self.app()
            app.radio[0].set_value("PC内のファイルを直接指定").run()
            app.text_input[0].set_value(str(self.root / "sample.mp3")).run()
        self.assertFalse(app.exception)
        self.assertFalse(self.store.base.exists())
        self.assertIsNone(self.manager._base)

    def test_completed_result_exposes_four_formats_and_editor(self):
        self.seed()
        app = self.app()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("download_button")), 4)
        self.assertEqual(len(app.get("dataframe")), 1)
        self.assertEqual(len(app.get("audio")), 0)
        self.assertTrue(self.button(app, "修正を保存").disabled)

    def test_listening_reads_only_requested_segment(self):
        self.seed()
        with patch.object(
            TranscriptionStore, "playback", return_value=b"RIFF" + b"0" * 100
        ) as play:
            app = self.app()
            play.assert_not_called()
            self.button(app, "この区間を聞く").click().run()
            play.assert_called_once_with(self.job_id, 0.0, 2.0)
            self.assertFalse(app.exception)
            self.assertEqual(len(app.get("audio")), 1)

    def test_history_delete_requires_checkbox_and_removes_owned_data(self):
        self.seed()
        app = self.app()
        self.assertTrue(self.button(app, "履歴を削除").disabled)
        app.checkbox[0].check().run()
        self.button(app, "履歴を削除").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.store.history(), [])

    def test_cancelled_partial_has_warning_and_exports(self):
        self.seed("cancelled")
        app = self.app()
        self.assertFalse(app.exception)
        self.assertTrue(any("途中" in w.value for w in app.warning))
        self.assertEqual(len(app.get("download_button")), 4)

    def test_draft_callback_and_save_preserve_original(self):
        self.seed()
        app = self.app()
        app.session_state["asr_drafts"] = {
            self.job_id: {"revision": 0, "texts": {0: "修正した文章"}}
        }
        app.run()
        self.button(app, "修正を保存").click().run()
        self.assertFalse(app.exception)
        result = self.store.result(self.job_id)
        self.assertEqual(result["segments"][0]["text"], "修正した文章")
        self.assertEqual(result["segments"][0]["original_text"], "元の文章")

    def test_stale_draft_retained_on_conflict(self):
        self.seed()
        app = self.app()
        app.session_state["asr_drafts"] = {
            self.job_id: {"revision": 0, "texts": {0: "my draft"}}
        }
        self.store.save_edits(self.job_id, 0, {0: "external update"})
        app.run()
        self.button(app, "修正を保存").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(
            app.session_state["asr_drafts"][self.job_id]["texts"][0], "my draft"
        )
        self.assertEqual(
            self.store.result(self.job_id)["segments"][0]["text"], "external update"
        )

    def test_active_job_hides_editor_and_delete_is_disabled(self):
        self.seed("transcribing")
        app = self.app()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("download_button")), 0)
        self.assertTrue(self.button(app, "履歴を削除").disabled)
        self.button(app, "文字起こしを中止").click().run()
        self.assertTrue((self.store.folder(self.job_id) / "cancel").exists())

    def test_selected_history_survives_save_with_duplicate_names(self):
        self.seed()
        self.seed(job_id="b" * 32)
        app = self.app()
        history = next(s for s in app.selectbox if s.label == "表示する履歴")
        history.set_value(self.job_id).run()
        app.session_state["asr_drafts"] = {
            self.job_id: {"revision": 0, "texts": {0: "selected result"}}
        }
        app.run()
        self.button(app, "修正を保存").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["asr_selected"], self.job_id)
        self.assertTrue(any("selected result" in a.value for a in app.text_area))


if __name__ == "__main__":
    unittest.main()
