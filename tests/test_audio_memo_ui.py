"""Real memo storage with synthetic WAVs; no microphone, model, or worker."""
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from streamlit.testing.v1 import AppTest

from app_core import audio_memo_ui as ui
from app_core import audio_memo_service as service
from app_core.audio_memo_service import AudioMemoStore


def synthetic_wav():
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * 16000)
    return output.getvalue()


class Recording(BytesIO):
    def __init__(self, data, file_id):
        super().__init__(data)
        self.file_id = file_id
        self.name = "recording.wav"
        self.type = "audio/wav"


class AudioMemoUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(force_reparse=True, options_from_flags={
            "global.developmentMode": False, "browser.gatherUsageStats": False,
        })

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="am_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AudioMemoStore(self.root)
        self.wav = synthetic_wav()
        self.input_patch = patch.object(ui.st, "audio_input", return_value=None)
        self.audio_input = self.input_patch.start()
        self.addCleanup(self.input_patch.stop)
        self.setup_patch = patch.object(ui, "_setup")
        self.setup_patch.start()
        self.addCleanup(self.setup_patch.stop)

    def app(self):
        return AppTest.from_string(
            "from pathlib import Path\n"
            "from app_core.audio_memo_ui import render_audio_memo\n"
            f"render_audio_memo(Path({str(self.root)!r}))",
            default_timeout=30,
        ).run()

    def button(self, app, label):
        return next(item for item in app.button if item.label == label)

    def field(self, elements, label):
        return next(item for item in elements if item.label == label)

    def healthy(self, app):
        self.assertFalse(app.exception, [item.message for item in app.exception])

    def seed(self, capture_id="seed", title="Synthetic memo", text=None):
        memo = self.store.capture(self.wav, capture_id, title=title)
        if text is not None:
            memo = self.store.save(memo["id"], memo["revision"], title, text)
        return memo

    def test_open_does_not_create_storage_or_start_a_job(self):
        with patch.object(AudioMemoStore, "start") as start:
            app = self.app()
        self.healthy(app)
        self.assertFalse((self.root / "transcription_data").exists())
        start.assert_not_called()
        self.audio_input.assert_called_once()
        self.assertEqual(self.audio_input.call_args.kwargs["sample_rate"], 16000)

    def test_native_recording_widget_renders_without_recording_or_creating_files(self):
        self.input_patch.stop()
        app = self.app()
        self.healthy(app)
        self.assertEqual(len(app.get("audio_input")), 1)
        self.assertFalse((self.root / "transcription_data").exists())

    def test_page_entry_renders_native_widget_with_isolated_data_root(self):
        self.input_patch.stop()
        page = Path(ui.__file__).resolve().parents[1] / "pages/23_録音・文字起こしメモ.py"
        with patch("config.PROJECT_ROOT", self.root), \
                patch("app_core.settings_service.load_user_settings", return_value={}), \
                patch.object(ui.ab, "sidemenu"):
            app = AppTest.from_file(str(page), default_timeout=30).run()
        self.healthy(app)
        self.assertEqual(len(app.get("audio_input")), 1)
        self.assertFalse((self.root / "transcription_data").exists())

    def test_one_recording_is_saved_once_and_identical_new_recording_is_distinct(self):
        self.audio_input.return_value = Recording(self.wav, "first-event")
        app = self.app()
        self.healthy(app)
        first = self.store.history()[0]
        self.assertEqual(self.store.audio_path(first["id"]).read_bytes(), self.wav)
        # Streamlit may reconstruct the UploadedFile on an unrelated rerun.
        self.audio_input.return_value = Recording(self.wav, "first-event")
        app.run()
        self.assertEqual(len(self.store.history()), 1)
        self.audio_input.return_value = Recording(self.wav, "second-event")
        app.run()
        self.healthy(app)
        self.assertEqual(len(self.store.history()), 2)
        self.assertNotEqual(app.session_state[ui.SELECTED], first["id"])

    def test_rejected_recording_has_recovery_download_and_keeps_capture_identity(self):
        self.audio_input.return_value = Recording(b"invalid WAV", "invalid-event")
        app = self.app()
        self.healthy(app)
        self.assertEqual(self.store.history(), [])
        self.assertTrue(any("保存できませんでした" in item.value for item in app.error))
        self.assertTrue(any(item.label == "元の録音WAVをダウンロード" for item in app.get("download_button")))
        before = app.session_state[ui.CAPTURES]["file:invalid-event"]["capture_id"]
        self.button(app, "録音の保存を再試行").click().run()
        self.healthy(app)
        self.assertEqual(app.session_state[ui.CAPTURES]["file:invalid-event"]["capture_id"], before)

    def test_title_and_body_drafts_survive_switching_history(self):
        first, second = self.seed("first"), self.seed("second")
        app = self.app()
        self.field(app.selectbox, "表示する録音メモ").set_value(first["id"]).run()
        self.field(app.text_input, "メモのタイトル").set_value("Edited title").run()
        self.field(app.text_area, "全文を編集").set_value("Unsaved private draft").run()
        self.field(app.selectbox, "表示する録音メモ").set_value(second["id"]).run()
        self.field(app.selectbox, "表示する録音メモ").set_value(first["id"]).run()
        self.healthy(app)
        self.assertEqual(self.field(app.text_input, "メモのタイトル").value, "Edited title")
        self.assertEqual(self.field(app.text_area, "全文を編集").value, "Unsaved private draft")
        self.assertEqual(self.store.get(first["id"])["text"], "")

    def test_save_writes_body_and_txt_and_preserves_selected_history(self):
        first = self.seed("first")
        self.seed("second")
        app = self.app()
        self.field(app.selectbox, "表示する録音メモ").set_value(first["id"]).run()
        self.field(app.text_area, "全文を編集").set_value("A corrected memo\nSecond line").run()
        self.button(app, "メモを保存").click().run()
        self.healthy(app)
        saved = self.store.get(first["id"])
        self.assertEqual(saved["text"], "A corrected memo\nSecond line")
        self.assertIn("A corrected memo", self.store.txt_path(first["id"]).read_text(encoding="utf-8-sig"))
        self.assertTrue(saved["txt_synced"])
        self.assertEqual(app.session_state[ui.SELECTED], first["id"])
        self.assertTrue(self.button(app, "メモを保存").disabled)

    def test_conflict_keeps_draft_and_does_not_overwrite_external_update(self):
        memo = self.seed(text="Original")
        app = self.app()
        self.field(app.text_area, "全文を編集").set_value("My unsaved draft").run()
        self.store.save(memo["id"], memo["revision"], memo["title"], "External update")
        self.button(app, "メモを保存").click().run()
        self.healthy(app)
        self.assertEqual(self.field(app.text_area, "全文を編集").value, "My unsaved draft")
        self.assertEqual(self.store.get(memo["id"])["text"], "External update")
        self.assertTrue(any("入力は保持" in item.value for item in app.error))

    def test_audio_is_only_read_after_playback_request(self):
        memo = self.seed()
        with patch.object(AudioMemoStore, "playback", return_value=self.wav) as playback, \
                patch.object(AudioMemoStore, "audio_path", side_effect=AssertionError("UI must not read a full WAV")):
            app = self.app()
            playback.assert_not_called()
            self.button(app, "録音を再生").click().run()
            self.healthy(app)
            playback.assert_called_once_with(memo["id"], start_sec=0.0, seconds=60)
            self.assertEqual(len(app.get("audio")), 1)

    def test_long_audio_plays_requested_slice_without_loading_whole_recording(self):
        memo = self.seed()
        long_memo = {**memo, "duration": 10800}
        with patch.object(AudioMemoStore, "sync", return_value=long_memo), \
                patch.object(AudioMemoStore, "playback", return_value=self.wav) as playback, \
                patch.object(AudioMemoStore, "audio_path", side_effect=AssertionError("no whole recording read")):
            app = self.app()
            self.field(app.number_input, "再生開始（秒）").set_value(7200.0).run()
            self.button(app, "録音を再生").click().run()
            self.healthy(app)
            playback.assert_called_once_with(memo["id"], start_sec=7200.0, seconds=60)

    def test_timed_download_uses_recognition_original_instead_of_edited_body(self):
        memo = self.seed(text="Edited body without timestamps")
        recognized = {**memo, "original_text": "Raw recognized words"}
        with patch.object(AudioMemoStore, "sync", return_value=recognized), \
                patch.object(AudioMemoStore, "timed_text", return_value=b"00:00:00 Raw recognized words") as timed:
            app = self.app()
            self.healthy(app)
            timed.assert_called_once_with(memo["id"])
            download = next(item for item in app.get("download_button") if item.label == "認識原文（時刻付きTXT）")
            self.assertFalse(download.disabled)
            self.assertTrue(any("編集した全文" in item.value for item in app.caption))

    def test_transcription_defaults_to_japanese_standard_and_busy_disables_start(self):
        memo = self.seed()
        with patch.object(AudioMemoStore, "start", return_value="a" * 32) as start:
            app = self.app()
            self.button(app, "文字起こし").click().run()
        self.healthy(app)
        start.assert_called_once_with(memo["id"], language="ja", mode="standard", hotwords="", retry=False)
        with patch.object(AudioMemoStore, "busy", return_value=True):
            app.run()
        self.assertTrue(self.button(app, "文字起こし").disabled)

    def test_done_memo_can_be_explicitly_transcribed_again(self):
        memo = self.seed(text="Edited text")
        completed = {**memo, "status": "done", "job_id": "b" * 32, "active": False,
                     "resume_warning": "前回の完了区間を確認できないため、最初から認識します。"}
        with patch.object(AudioMemoStore, "sync", return_value=completed), \
                patch.object(AudioMemoStore, "start", return_value="c" * 32) as start:
            app = self.app()
            self.button(app, "もう一度文字起こし").click().run()
            self.assertTrue(any("最初から認識" in item.value for item in app.info))
        self.healthy(app)
        self.assertTrue(start.call_args.kwargs["retry"])
        self.assertEqual(self.store.get(memo["id"])["text"], "Edited text")

    def test_active_progress_can_cancel_without_losing_recording(self):
        memo = self.seed()
        current = {**memo, "status": "transcribing", "job_id": "a" * 32,
                   "active": True, "progress": 0.4, "message": "Synthetic progress"}

        def cancel(_memo_id):
            current.update(status="cancelled", active=False)
            return dict(current)

        with patch.object(AudioMemoStore, "sync", side_effect=lambda _id: dict(current)), \
                patch.object(AudioMemoStore, "cancel", side_effect=cancel) as request:
            app = self.app()
            self.healthy(app)
            self.assertEqual(len(app.get("progress")), 1)
            self.assertFalse(any(item.label == "全文を編集" for item in app.text_area))
            self.button(app, "文字起こしを中止").click().run()
            self.healthy(app)
            request.assert_called_once_with(memo["id"])
            self.assertTrue(any("完了していません" in item.value for item in app.warning))
            self.assertTrue(any(item.label == "全文を編集" for item in app.text_area))
        self.assertEqual(self.store.audio_path(memo["id"]).read_bytes(), self.wav)

    def test_txt_failure_keeps_saved_text_and_can_regenerate(self):
        memo = self.seed()
        atomic_bytes = service._atomic_bytes

        def fail_txt(path, data):
            if Path(path).name == "transcript.txt":
                raise OSError("synthetic TXT failure")
            return atomic_bytes(path, data)

        app = self.app()
        self.field(app.text_area, "全文を編集").set_value("Durable corrected text").run()
        with patch.object(service, "_atomic_bytes", side_effect=fail_txt):
            self.button(app, "メモを保存").click().run()
        self.healthy(app)
        saved = self.store.get(memo["id"])
        self.assertEqual(saved["text"], "Durable corrected text")
        self.assertFalse(saved["txt_synced"])
        self.assertTrue(any("TXT" in item.value for item in app.warning))
        self.assertFalse(any(item.label == "TXTをダウンロード" for item in app.get("download_button")))
        self.button(app, "TXTを作り直す").click().run()
        self.healthy(app)
        self.assertTrue(self.store.get(memo["id"])["txt_synced"])
        self.assertIn("Durable corrected text", self.store.txt_path(memo["id"]).read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
