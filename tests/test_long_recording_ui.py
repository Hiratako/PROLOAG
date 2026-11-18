"""PC recording controls with a fake recorder; never open a microphone."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app_core import long_recording_ui as ui
from app_core.media_conversion import MediaError


class FakeRecorder:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.devices_calls = 0
        self.fail_start_once = False
        self.memo_id = None

    def seed(self, status="recording", recording_id=None):
        recording_id = recording_id or f"{len(self.rows) + 1:032x}"
        row = {"id": recording_id, "title": "Synthetic PC recording", "status": status,
               "duration": 4, "saved_duration": 3, "elapsed": 8, "level": 0.3,
               "created_at": "2026-11-17T12:00:00", "last_saved_at": "2026-11-17T12:00:03",
               "error": "", "import_error": "", "memo_id": None,
               "active": status in ui.ACTIVE, "free_bytes": 5 * 1024 ** 3, "max_seconds": 10800}
        self.rows[recording_id] = row
        return row

    def devices(self):
        self.devices_calls += 1
        return [{"id": 4, "name": "Synthetic WASAPI input", "default": True, "hostapi": "Windows WASAPI"}]

    def history(self):
        return [deepcopy(row) for row in reversed(list(self.rows.values()))]

    def get(self, recording_id):
        return deepcopy(self.rows[recording_id])

    def start(self, **kwargs):
        self.calls.append(("start", deepcopy(kwargs)))
        if self.fail_start_once:
            self.fail_start_once = False
            raise MediaError("synthetic start failure")
        return self.seed()

    def pause(self, recording_id):
        self.calls.append(("pause", recording_id))
        self.rows[recording_id].update(status="paused")
        return self.get(recording_id)

    def resume(self, recording_id):
        self.calls.append(("resume", recording_id))
        self.rows[recording_id].update(status="recording")
        return self.get(recording_id)

    def stop(self, recording_id):
        self.calls.append(("stop", recording_id))
        self.rows[recording_id].update(status="stopped", active=False, memo_id=self.memo_id)
        return self.get(recording_id)

    def recover(self, recording_id):
        self.calls.append(("recover", recording_id))
        self.rows[recording_id].update(active=False, memo_id=self.memo_id, import_error="")
        return self.get(recording_id)


class LongRecordingUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(force_reparse=True, options_from_flags={
            "global.developmentMode": False, "browser.gatherUsageStats": False,
        })

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lr_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recorder = FakeRecorder()
        for target, value in (("LongRecordingStore", self.recorder), ("engine_available", True),
                              ("dependency_dir", self.root / ".deps-record311")):
            mock = patch.object(ui, target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def app(self):
        return AppTest.from_string(
            "from pathlib import Path\nimport streamlit as st\n"
            "from app_core.long_recording_ui import render_long_recording\n"
            "def open_memo(memo_id):\n"
            "    st.session_state['opened_memo'] = memo_id\n"
            "    st.session_state.setdefault('open_calls', []).append(memo_id)\n"
            f"render_long_recording(Path({str(self.root)!r}), on_memo_ready=open_memo)",
            default_timeout=30,
        ).run()

    def button(self, app, label):
        return next(item for item in app.button if item.label == label)

    def healthy(self, app):
        self.assertFalse(app.exception, [item.message for item in app.exception])

    def test_open_does_not_enumerate_or_start_recording_or_create_storage(self):
        app = self.app()
        self.healthy(app)
        self.assertEqual(self.recorder.devices_calls, 0)
        self.assertEqual(self.recorder.calls, [])
        self.assertFalse((self.root / "transcription_data").exists())
        self.assertTrue(any("3時間" in item.value for item in app.info))

    def test_start_uses_default_mic_three_hour_limit_and_sleep_prevention(self):
        app = self.app()
        self.button(app, "PCで録音を開始").click().run()
        self.healthy(app)
        request = self.recorder.calls[0][1]
        self.assertIsNone(request["device"])
        self.assertEqual(request["max_seconds"], 10800)
        self.assertTrue(request["prevent_sleep"])
        self.assertEqual(len(request["operation_id"]), 32)
        self.assertEqual(self.recorder.devices_calls, 0)
        self.assertTrue(self.button(app, "PCで録音を開始").disabled)

    def test_devices_are_queried_only_by_explicit_button(self):
        app = self.app()
        self.button(app, "マイク一覧を確認").click().run()
        self.healthy(app)
        self.assertEqual(self.recorder.devices_calls, 1)
        device = next(item for item in app.selectbox if item.label == "録音するマイク")
        device.set_value(4).run()
        self.button(app, "PCで録音を開始").click().run()
        self.healthy(app)
        self.assertEqual(self.recorder.calls[0][1]["device"], 4)
        self.assertEqual(self.recorder.devices_calls, 1)

    def test_pause_resume_stop_are_bound_to_selected_recording(self):
        self.recorder.seed("done", "a" * 32)
        current = self.recorder.seed("recording", "b" * 32)
        app = self.app()
        self.assertEqual({item.label: item.value for item in app.metric}, {
            "録音時間": "00:00:04", "保存済み音声": "00:00:03", "開始からの経過": "00:00:08",
        })
        next(item for item in app.selectbox if item.label == "表示するPC録音").set_value("a" * 32).run()
        self.assertFalse(any(item.label == "録音を一時停止" for item in app.button))
        self.button(app, "進行中のPC録音を表示").click().run()
        self.button(app, "録音を一時停止").click().run()
        self.button(app, "録音を再開").click().run()
        self.button(app, "録音を停止して保存").click().run()
        self.healthy(app)
        self.assertEqual(self.recorder.calls, [(action, current["id"]) for action in ("pause", "resume", "stop")])

    def test_start_failure_reuses_operation_identity_until_success(self):
        self.recorder.fail_start_once = True
        app = self.app()
        self.button(app, "PCで録音を開始").click().run()
        self.healthy(app)
        first_id = self.recorder.calls[-1][1]["operation_id"]
        self.button(app, "PCで録音を開始").click().run()
        self.healthy(app)
        self.assertEqual(self.recorder.calls[-1][1]["operation_id"], first_id)
        self.button(app, "録音を停止して保存").click().run()
        self.button(app, "PCで録音を開始").click().run()
        self.assertNotEqual(self.recorder.calls[-1][1]["operation_id"], first_id)

    def test_interrupted_audio_can_recover_and_open_memo_once(self):
        current = self.recorder.seed("interrupted")
        current["import_error"] = "synthetic import failure"
        self.recorder.memo_id = "c" * 32
        app = self.app()
        self.button(app, "保存済み音声をメモへ取り込み直す").click().run()
        self.healthy(app)
        self.assertEqual(app.session_state["opened_memo"], "c" * 32)
        before = len(app.session_state["open_calls"])
        app.run()
        self.assertEqual(len(app.session_state["open_calls"]), before)
        self.button(app, "この録音のメモを開く").click().run()
        self.assertEqual(len(app.session_state["open_calls"]), before + 1)

    def test_unknown_recording_state_blocks_start_and_controls(self):
        self.recorder.seed("unavailable")
        app = self.app()
        self.healthy(app)
        self.assertTrue(self.button(app, "PCで録音を開始").disabled)
        self.assertFalse(any(item.label == "録音を停止して保存" for item in app.button))
        self.assertFalse(any(item.label == "保存済み音声をメモへ取り込み直す" for item in app.button))
        self.assertEqual(self.recorder.calls, [])

    def test_time_limit_does_not_claim_memo_import_succeeded(self):
        current = self.recorder.seed("done")
        current["import_error"] = "synthetic import failure"
        app = self.app()
        self.healthy(app)
        self.assertFalse(app.success)
        self.assertTrue(any("まだメモへ" in item.value for item in app.info))
        self.assertTrue(any("時間上限" in item.value for item in app.markdown))

    def test_completion_opens_real_memo_without_discarding_another_draft(self):
        from io import BytesIO
        import wave
        from app_core import audio_memo_ui as memo_ui
        from app_core.audio_memo_service import AudioMemoStore

        output = BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b"\x00\x00" * 16000)
        store = AudioMemoStore(self.root)
        old = store.capture(output.getvalue(), "old-memo", title="Old memo")
        new = store.capture(output.getvalue(), "new-memo", title="PC recording")
        with patch.object(memo_ui.st, "audio_input", return_value=None), patch.object(memo_ui, "_setup"):
            app = AppTest.from_string(
                "from pathlib import Path\nfrom app_core.audio_memo_ui import render_audio_memo\n"
                f"render_audio_memo(Path({str(self.root)!r}))", default_timeout=30,
            ).run()
            next(item for item in app.selectbox if item.label == "表示する録音メモ").set_value(old["id"]).run()
            next(item for item in app.text_area if item.label == "全文を編集").set_value("Keep my unsaved draft").run()
            current = self.recorder.seed("recording")
            app.run()
            current.update(status="stopped", active=False, memo_id=new["id"])
            app.run()
            self.healthy(app)
            self.assertEqual(app.session_state[memo_ui.SELECTED], new["id"])
            next(item for item in app.selectbox if item.label == "表示する録音メモ").set_value(old["id"]).run()
            self.healthy(app)
            self.assertEqual(next(item for item in app.text_area if item.label == "全文を編集").value,
                             "Keep my unsaved draft")
        self.assertEqual(store.get(old["id"])["text"], "")


if __name__ == "__main__":
    unittest.main()
