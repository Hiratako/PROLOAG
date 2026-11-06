"""Real FFmpeg + real process lifecycle, deterministic ASR test double."""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from app_core import transcription as tr
from app_core.media_conversion import MediaError, _process_options
import test_media_conversion as fixtures

FAKE = """
from types import SimpleNamespace
import os, time
class WhisperModel:
    def __init__(self, model, **kwargs):
        time.sleep(float(os.environ.get("ASR_FAKE_LOAD_SLEEP", "0")))
    def transcribe(self, samples, **kwargs):
        assert len(samples) <= 304 * 16000, "unbounded audio decoding"
        def segments():
            duration = len(samples)/16000
            yield SimpleNamespace(start=max(.1, duration-1.) if duration < 5 else .1,
                                  end=duration-.1 if duration < 5 else 1.5, text="テスト Recognition")
            if kwargs.get("hotwords") == "FAIL_AFTER_ONE":
                raise RuntimeError("Injected inference failure")
            if kwargs.get("hotwords") == "SLOW":
                time.sleep(10)
        return segments(), SimpleNamespace(language=kwargs.get("language") or "en")
"""


class TranscriptionProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.MediaTests.setUpClass()
        cls.mp3 = fixtures.MediaTests.fixtures / "audio.mp3"
        cls.long = fixtures.MediaTests.fixtures / "long-audio.mp3"
        tools = fixtures.MediaTests.tools
        subprocess.run(
            [
                str(tools.ffmpeg),
                "-v",
                "error",
                "-y",
                "-i",
                str(fixtures.MediaTests.video),
                "-map",
                "0:1",
                "-c:a",
                "libmp3lame",
                str(cls.mp3),
            ],
            check=True,
            capture_output=True,
            **_process_options(),
        )
        subprocess.run(
            [
                str(tools.ffmpeg),
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=16000:cl=mono",
                "-t",
                "301",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "32k",
                str(cls.long),
            ],
            check=True,
            capture_output=True,
            **_process_options(),
        )

    @classmethod
    def tearDownClass(cls):
        fixtures.MediaTests.tearDownClass()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pgasr_process_")
        self.root = Path(self.temp.name)
        self.store = tr.TranscriptionStore(self.root)
        self.manager = tr.TranscriptionSources(self.root)
        self.addCleanup(self.cleanup)
        code = self.root / "app_core"
        code.mkdir()
        (code / "__init__.py").write_text("", encoding="utf-8")
        for name in (
            "transcription.py",
            "transcription_worker.py",
            "media_conversion.py",
        ):
            shutil.copy2(Path(tr.__file__).parent / name, code / name)
        deps = tr.dependency_dir(self.root)
        (deps / "faster_whisper").mkdir(parents=True)
        (deps / "faster_whisper/__init__.py").write_text(FAKE, encoding="utf-8")
        (deps / "faster_whisper/utils.py").write_text(
            "def download_model(model, **kwargs): return model\n", encoding="utf-8"
        )
        (deps / "ctranslate2.py").write_text(
            "def get_cuda_device_count(): return 0\n", encoding="utf-8"
        )

    def cleanup(self):
        self.manager.close()
        for state in self.store.history():
            if state["status"] in tr.ACTIVE:
                self.store.cancel(state["id"])
        end = time.monotonic() + 10
        while time.monotonic() < end:
            held = False
            for state in self.store.history():
                try:
                    with tr.file_lock(
                        self.store.folder(state["id"]) / ".worker.lock", blocking=False
                    ):
                        pass
                except OSError:
                    held = True
            if not held and not any(
                s["status"] in tr.ACTIVE for s in self.store.history()
            ):
                break
            time.sleep(0.1)
        self.temp.cleanup()

    def start(
        self, source=None, hotwords="", language="ja", mode="standard", track=None
    ):
        source = source or self.manager.prepare_local("a" * 32, str(self.mp3))
        return self.store.start(
            source,
            track if track is not None else source.tracks[0].index,
            language,
            mode,
            hotwords,
        )

    def wait(self, job, status=None):
        end = time.monotonic() + 30
        while time.monotonic() < end:
            result = self.store.result(job)
            if (
                result["state"]["status"] == status
                if status
                else result["state"]["status"] not in tr.ACTIVE
            ):
                return result
            time.sleep(0.05)
        raise AssertionError(self.store.result(job))

    def test_mp3_process_completes_with_audio_and_persistent_result(self):
        job = self.start(language="en")
        result = self.wait(job)
        self.assertEqual(result["state"]["status"], "done")
        self.assertEqual(len(result["segments"]), 1)
        self.assertTrue(self.store.playback(job, 0.0, 1.0).startswith(b"RIFF"))
        self.assertEqual(
            tr.TranscriptionStore(self.root).result(job)["segments"], result["segments"]
        )

    def test_selected_mp4_track_and_accurate_model_are_preserved(self):
        source = self.manager.prepare_local("a" * 32, str(fixtures.MediaTests.video))
        self.assertEqual(len(source.tracks), 2)
        job = self.start(
            source, language="auto", mode="accurate", track=source.tracks[1].index
        )
        result = self.wait(job)
        self.assertEqual(result["state"]["status"], "done")
        self.assertEqual(result["track"]["index"], source.tracks[1].index)
        self.assertEqual(result["model"], "medium")
        self.assertEqual(result["state"]["detected_languages"], ["en"])

    def test_cancel_inference_preserves_flushed_partial_segment(self):
        job = self.start(hotwords="SLOW")
        end = time.monotonic() + 10
        while time.monotonic() < end and not self.store.result(job)["segments"]:
            time.sleep(0.05)
        self.assertTrue(self.store.result(job)["segments"])
        self.store.cancel(job)
        result = self.wait(job)
        self.assertEqual(result["state"]["status"], "cancelled")
        self.assertEqual(len(result["segments"]), 1)

    def test_model_load_can_be_cancelled(self):
        with patch.dict(os.environ, {"ASR_FAKE_LOAD_SLEEP": "5"}):
            job = self.start()
        self.wait(job, "loading")
        self.store.cancel(job)
        self.assertEqual(self.wait(job)["state"]["status"], "cancelled")

    def test_inference_failure_keeps_partial_text(self):
        job = self.start(hotwords="FAIL_AFTER_ONE")
        result = self.wait(job)
        self.assertEqual(result["state"]["status"], "failed")
        self.assertEqual(len(result["segments"]), 1)
        self.assertIn("Injected inference failure", result["state"]["error"])

    def test_long_audio_is_decoded_in_bounded_windows(self):
        source = self.manager.prepare_local("a" * 32, str(self.long))
        job = self.start(source)
        result = self.wait(job)
        self.assertEqual(result["state"]["status"], "done")
        self.assertEqual(len(result["segments"]), 2)
        self.assertGreaterEqual(result["segments"][1]["start"], 300)

    def test_invalid_replacement_keeps_previous_valid_source(self):
        source = self.manager.prepare_local("a" * 32, str(self.mp3))
        with self.assertRaises(MediaError):
            self.manager.prepare_local("a" * 32, str(fixtures.MediaTests.silent))
        self.assertEqual(self.manager.snapshot("a" * 32)["source"], source)

    def test_wrong_extension_rejected_and_original_bytes_unchanged(self):
        before = self.mp3.read_bytes()
        with self.assertRaises(MediaError):
            self.manager.prepare_upload("a" * 32, "pretend.mp4", before)
        self.assertEqual(self.mp3.read_bytes(), before)

    def test_upload_job_survives_temporary_source_clear(self):
        source = self.manager.prepare_upload(
            "a" * 32, "録音.mp3", self.mp3.read_bytes()
        )
        job = self.start(source)
        self.manager.clear("a" * 32)
        result = self.wait(job)
        self.assertEqual(result["state"]["status"], "done")
        self.assertEqual(result["source_name"], "録音.mp3")
        self.assertTrue((self.store.folder(job) / "input.mp3").is_file())


if __name__ == "__main__":
    unittest.main()
