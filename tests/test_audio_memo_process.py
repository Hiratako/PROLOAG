"""Real WAV/FFmpeg/worker lifecycle with deterministic, offline ASR output."""

from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import wave

from app_core import transcription as tr
from app_core.audio_memo_service import AudioMemoStore
from app_core.media_conversion import discover_tools, MediaError
from test_transcription_process import FAKE, FAKE_DOWNLOAD


class AudioMemoProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            discover_tools(Path(__file__).resolve().parents[1])
        except MediaError as exc:
            raise unittest.SkipTest(str(exc))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pgmemo_process_")
        self.root = Path(self.temporary.name)
        self.memos = AudioMemoStore(self.root)
        self.jobs = tr.TranscriptionStore(self.root)
        self.addCleanup(self.cleanup)
        package = self.root / "app_core"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        source = Path(tr.__file__).parent
        for name in ("transcription.py", "transcription_worker.py",
                     "media_conversion.py", "audio_memo_service.py"):
            shutil.copy2(source / name, package / name)
        dependencies = tr.dependency_dir(self.root)
        (dependencies / "faster_whisper").mkdir(parents=True)
        (dependencies / "faster_whisper/__init__.py").write_text(FAKE, encoding="utf-8")
        (dependencies / "faster_whisper/utils.py").write_text(
            FAKE_DOWNLOAD, encoding="utf-8")
        (dependencies / "ctranslate2.py").write_text(
            "def get_cuda_device_count(): return 0\n", encoding="utf-8")
        stream = BytesIO()
        with wave.open(stream, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 16000 * 3)
        self.wav = stream.getvalue()

    def cleanup(self):
        try:
            for state in self.jobs.history():
                if state["status"] in tr.ACTIVE:
                    self.jobs.cancel(state["id"])
            until = time.monotonic() + 15
            while time.monotonic() < until:
                if not any(row["status"] in tr.ACTIVE for row in self.jobs.history()):
                    break
                time.sleep(0.1)
        finally:
            self.temporary.cleanup()

    def capture(self):
        return self.memos.capture(self.wav, "capture-process-1", title="録音テスト")

    def wait_for_worker_and_memo(self, job_id, memo_id, statuses=("done",), timeout=25):
        until = time.monotonic() + timeout
        last = None
        while time.monotonic() < until:
            state = self.jobs._state(job_id)
            memo_file = self.root / "transcription_data/memos" / memo_id / "memo.json"
            try:
                last = json.loads(memo_file.read_text(encoding="utf-8"))
            except PermissionError:
                # Windows may briefly deny a raw read while the worker's
                # atomic replace is committing. Keep observing durable JSON;
                # do not call get()/sync(), which could conceal a missing hook.
                time.sleep(0.025)
                continue
            # Inspect raw durable state so get()/sync() cannot conceal a missing hook.
            if (state["status"] in statuses and last.get("job_id") == job_id
                    and last.get("status") in statuses and last.get("txt_synced")):
                return state, last
            if state["status"] in {"failed", "interrupted"} and state["status"] not in statuses:
                log = (self.jobs.folder(job_id) / "worker.log").read_text(
                    encoding="utf-8", errors="replace")
                self.fail(f"Unexpected worker state {state}: {log[-4000:]}")
            time.sleep(0.1)
        log = (self.jobs.folder(job_id) / "worker.log").read_text(
            encoding="utf-8", errors="replace")
        self.fail(f"Worker/memo did not settle: {last}; {log[-4000:]}")

    def test_worker_saves_full_text_and_txt_without_page_polling(self):
        memo = self.capture()
        job_id = self.memos.start(memo["id"], language="ja")
        _, saved = self.wait_for_worker_and_memo(job_id, memo["id"])
        self.assertIn("テスト Recognition", saved["text"])
        self.assertIn("テスト Recognition", saved["original_text"])
        self.assertEqual(self.memos.audio_path(memo["id"]).read_bytes(), self.wav)
        self.assertIn("テスト Recognition",
                      self.memos.txt_path(memo["id"]).read_text(encoding="utf-8-sig"))
        restarted = AudioMemoStore(self.root)
        reopened = restarted.get(memo["id"])
        changed = restarted.save(memo["id"], reopened["revision"],
                                 "変更したタイトル", "編集した日本語の全文\nsecond line")
        self.assertTrue(changed["txt_synced"])
        self.assertEqual(restarted.txt_path(memo["id"]).read_text(encoding="utf-8-sig").strip(),
                         "編集した日本語の全文\nsecond line")
        self.assertEqual(restarted.audio_path(memo["id"]).read_bytes(), self.wav)

    def test_partial_recognition_failure_keeps_audio_and_text(self):
        memo = self.capture()
        job_id = self.memos.start(memo["id"], hotwords="FAIL_AFTER_ONE")
        state, saved = self.wait_for_worker_and_memo(job_id, memo["id"], ("failed",))
        self.assertIn("Injected inference failure", state["error"])
        self.assertIn("テスト Recognition", saved["text"])
        self.assertEqual(self.memos.audio_path(memo["id"]).read_bytes(), self.wav)

    def test_retry_keeps_user_edited_text_and_preserves_original_recording(self):
        memo = self.capture()
        job_id = self.memos.start(memo["id"])
        self.wait_for_worker_and_memo(job_id, memo["id"])
        saved = self.memos.get(memo["id"])
        self.memos.save(memo["id"], saved["revision"], saved["title"], "自分で修正した文章")
        retry = self.memos.start(memo["id"], retry=True)
        self.assertNotEqual(job_id, retry)
        _, saved_again = self.wait_for_worker_and_memo(retry, memo["id"])
        self.assertEqual(saved_again["text"], "自分で修正した文章")
        self.assertEqual(self.memos.audio_path(memo["id"]).read_bytes(), self.wav)
        self.assertEqual(len(self.jobs.history()), 2)


if __name__ == "__main__":
    unittest.main()
