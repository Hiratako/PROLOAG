"""Saved WAV/ASR integration using synthetic audio and no real worker or model."""

from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from app_core import audio_memo_service as memos
from app_core import transcription as tr
from app_core import transcription_worker as worker
from app_core.media_conversion import MediaError, Source, Tools, Track, _identity


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 1600)
    return output.getvalue()


class SyntheticFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pgamtr_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.memo_id = "a" * 32
        self.job_id = "b" * 32
        self.owner = "c" * 32

    def recording(self):
        path = self.root / "transcription_data/memos" / self.memo_id / "audio.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(wav_bytes())
        return path

    def source(self):
        path = self.recording()
        return Source(
            self.memo_id, path, path.name, "recording", _identity(path), 0.1,
            (Track(0, "pcm_s16le", "", "", 1, 0.1),),
        )

    def seed_job(self, status="starting"):
        store = tr.TranscriptionStore(self.root)
        folder = store.folder(self.job_id)
        folder.mkdir(parents=True, exist_ok=True)
        tr.atomic_json(folder / "state.json", {
            "id": self.job_id, "name": "synthetic.wav", "created": time.time(),
            "status": status, "progress": 0.0, "elapsed": 0.0,
            "audio_memo_id": self.memo_id,
        })
        return store, folder


class RecordingSourceTests(SyntheticFixture):
    def setUp(self):
        super().setUp()
        self.manager = tr.TranscriptionSources(
            self.root, tools=Tools(Path("fake-ffmpeg"), Path("fake-ffprobe"))
        )
        self.addCleanup(self.manager.close)
        self.probe = {
            "format": {"format_name": "wav", "duration": "0.1"},
            "streams": [{"codec_type": "audio", "index": 0,
                         "codec_name": "pcm_s16le", "channels": 1, "duration": "0.1"}],
        }

    def test_saved_wav_has_stable_memo_id_and_original_is_unchanged(self):
        path = self.recording()
        before = path.read_bytes()
        with patch.object(tr, "_probe", return_value=self.probe):
            one = self.manager.prepare_recording(self.owner, path)
            two = self.manager.prepare_recording(self.owner, path)
        self.assertEqual(one.id, self.memo_id)
        self.assertEqual(two.id, one.id)
        self.assertEqual(two.mode, "recording")
        self.assertEqual(two.path, path)
        self.assertEqual(path.read_bytes(), before)
        self.assertIs(self.manager.snapshot(self.owner)["source"], two)

    def test_recording_path_must_be_the_managed_immutable_audio_file(self):
        path = self.root / self.memo_id / "audio.wav"
        path.parent.mkdir()
        path.write_bytes(wav_bytes())
        with patch.object(tr, "_probe") as probe, self.assertRaises(MediaError):
            self.manager.prepare_recording(self.owner, path)
        probe.assert_not_called()

    def test_invalid_or_truncated_wav_is_rejected_before_ffprobe(self):
        path = self.recording()
        for payload in (b"not a wave", wav_bytes()[:-2]):
            with self.subTest(payload_size=len(payload)):
                path.write_bytes(payload)
                with patch.object(tr, "_probe") as probe, self.assertRaises(MediaError):
                    self.manager.prepare_recording(self.owner, path)
                probe.assert_not_called()

    def test_recording_size_is_checked_before_reading_or_probing(self):
        path = self.recording()
        with patch.object(memos, "MAX_RECORDING_BYTES", 16), patch.object(
            tr, "_probe"
        ) as probe, patch.object(Path, "read_bytes") as read, self.assertRaises(MediaError):
            self.manager.prepare_recording(self.owner, path)
        read.assert_not_called()
        probe.assert_not_called()

    def test_wav_must_also_match_the_probed_media_format(self):
        path = self.recording()
        wrong = deepcopy(self.probe)
        wrong["format"]["format_name"] = "mp3"
        with patch.object(tr, "_probe", return_value=wrong), self.assertRaises(MediaError):
            self.manager.prepare_recording(self.owner, path)

    def test_existing_mp3_mp4_inputs_do_not_accept_arbitrary_wav(self):
        path = self.recording()
        with self.assertRaises(MediaError):
            self.manager.prepare_local(self.owner, str(path))
        with self.assertRaises(MediaError):
            self.manager.prepare_upload(self.owner, "recording.wav", path.read_bytes())


class RecordingStartTests(SyntheticFixture):
    def setUp(self):
        super().setUp()
        self.store = tr.TranscriptionStore(self.root)
        for patcher in (
            patch.object(tr, "engine_available", return_value=True),
            patch.object(tr, "discover_tools", return_value=SimpleNamespace(ffmpeg=Path("fake-ffmpeg"))),
            patch.object(tr.threading, "Thread"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.process_patcher = patch.object(tr.subprocess, "Popen")
        self.process = self.process_patcher.start()
        self.addCleanup(self.process_patcher.stop)

    def test_memo_identity_is_durable_and_duplicate_start_is_idempotent(self):
        source = self.source()
        operation = "d" * 32
        job = self.store.start(source, 0, "ja", "standard", operation_id=operation,
                               audio_memo_id=self.memo_id)
        again = self.store.start(source, 0, "ja", "standard", operation_id=operation,
                                 audio_memo_id=self.memo_id)
        self.assertEqual(job, again)
        self.assertEqual(self.process.call_count, 1)
        folder = self.store.folder(job)
        for name in ("request.json", "state.json"):
            self.assertEqual(tr.read_json(folder / name)["audio_memo_id"], self.memo_id)
        self.assertEqual(tr.read_json(folder / "request.json")["source_path"], str(source.path))
        self.assertEqual(source.path.read_bytes(), wav_bytes())
        with self.assertRaises(MediaError):
            self.store.start(source, 0, "ja", "standard", operation_id=operation,
                             audio_memo_id="e" * 32)

    def test_legacy_start_does_not_add_memo_metadata(self):
        job = self.store.start(self.source(), 0, "ja", "standard")
        for name in ("request.json", "state.json"):
            self.assertNotIn("audio_memo_id", tr.read_json(self.store.folder(job) / name))

    def test_invalid_memo_id_is_rejected_without_spawning(self):
        source = self.source()
        for value in ("../memo", "A" * 32, 1, ""):
            with self.subTest(value=value), self.assertRaises(MediaError):
                self.store.start(source, 0, "ja", "standard", audio_memo_id=value)
        self.process.assert_not_called()
        self.assertFalse((self.store.base / "jobs").exists())

    def test_linked_job_cannot_be_deleted_while_its_memo_still_exists(self):
        source = self.source()
        job_id = self.store.start(source, 0, "ja", "standard", audio_memo_id=self.memo_id)
        folder = self.store.folder(job_id)
        state = tr.read_json(folder / "state.json")
        tr.atomic_json(folder / "state.json", {**state, "status": "done"})
        memo = source.path.parent / "memo.json"
        memo.write_text('{"synthetic":"preserve linked memo"}', encoding="utf-8")
        with self.assertRaises(MediaError):
            self.store.delete(job_id)
        self.assertTrue(folder.exists())
        self.assertTrue(memo.exists())
        self.assertEqual(source.path.read_bytes(), wav_bytes())
        memo.unlink()  # Only this test's synthetic memo metadata.
        self.store.delete(job_id)
        self.assertFalse(folder.exists())
        self.assertEqual(source.path.read_bytes(), wav_bytes())

    def test_unreadable_existing_job_remains_visible_and_blocks_new_start(self):
        source = self.source()
        self.seed_job()
        for failure in (PermissionError("temporary conflict"), ValueError("invalid state")):
            with self.subTest(failure=type(failure).__name__), patch.object(
                self.store, "_state", side_effect=failure
            ):
                history = self.store.history()
                self.assertEqual(history[0]["id"], self.job_id)
                self.assertEqual(history[0]["status"], "unavailable")
                self.assertIn(history[0]["status"], tr.BLOCKING)
                self.assertNotIn(history[0]["status"], tr.ACTIVE)
                with self.assertRaises(MediaError):
                    self.store.start(source, 0, "ja", "standard", audio_memo_id=self.memo_id)
        self.process.assert_not_called()
        self.assertEqual(self.store.history()[0]["status"], "starting")

    def test_json_reader_retries_only_a_small_number_of_permission_conflicts(self):
        with patch.object(Path, "read_text", side_effect=[PermissionError(), '{"ok":true}']) as read, patch.object(tr.time, "sleep"):
            self.assertEqual(tr.read_json(Path("synthetic.json")), {"ok": True})
        self.assertEqual(read.call_count, 2)
        with patch.object(Path, "read_text", side_effect=PermissionError()) as read, patch.object(tr.time, "sleep"), self.assertRaises(PermissionError):
            tr.read_json(Path("synthetic.json"))
        self.assertEqual(read.call_count, 3)


class TerminalMemoHookTests(SyntheticFixture):
    def values(self, status="done"):
        folder = self.root / "transcription_data/jobs" / self.job_id
        request = {"id": self.job_id, "root": str(self.root), "audio_memo_id": self.memo_id}
        state = {"id": self.job_id, "status": status, "audio_memo_id": self.memo_id}
        return folder, request, state

    def test_all_terminal_states_sync_the_registered_memo(self):
        for status in ("done", "cancelled", "failed"):
            folder, request, state = self.values(status)
            update = Mock()
            with self.subTest(status=status), patch.object(memos, "sync_audio_memo") as sync:
                worker._sync_audio_memo_terminal(request, folder, state, update)
                sync.assert_called_once_with(self.root, self.memo_id, self.job_id)
                update.assert_not_called()

    def test_invalid_identity_root_or_folder_never_calls_the_memo_service(self):
        folder, request, state = self.values()
        cases = [
            ({**request, "audio_memo_id": "../memo"}, state, folder),
            ({**request, "id": "A" * 32}, state, folder),
            ({**request, "root": "relative-root"}, state, folder),
            (request, {**state, "audio_memo_id": "f" * 32}, folder),
            (request, state, self.root / "outside"),
            ({key: value for key, value in request.items() if key != "audio_memo_id"}, state, folder),
        ]
        with patch.object(memos, "sync_audio_memo") as sync:
            for request_value, state_value, folder_value in cases:
                worker._sync_audio_memo_terminal(request_value, folder_value, state_value, Mock())
        sync.assert_not_called()

    def test_memo_save_error_preserves_asr_success_and_does_not_log_the_payload(self):
        folder, request, state = self.values()
        before = deepcopy(state)
        update = Mock()
        log = io.StringIO()
        with patch.object(memos, "sync_audio_memo", side_effect=OSError("secret transcript")), patch.object(sys, "stderr", log):
            worker._sync_audio_memo_terminal(request, folder, state, update)
        self.assertEqual(state, before)
        self.assertEqual(set(update.call_args.kwargs), {"memo_save_error"})
        self.assertNotIn("secret transcript", log.getvalue())
        self.assertIn("ASR result is preserved", log.getvalue())


class WorkerTerminalIntegrationTests(SyntheticFixture):
    def run_worker(self, outcome):
        source = self.source()
        store, folder = self.seed_job()
        request = {
            "id": self.job_id, "root": str(self.root), "source_path": str(source.path),
            "identity": list(source.identity), "audio_memo_id": self.memo_id,
            "ffmpeg": "fake-ffmpeg", "device": "cpu", "model": "small",
            "language": "ja", "hotwords": "", "track": {"index": 0}, "duration": 0.1,
        }
        tr.atomic_json(folder / "request.json", request)
        if outcome == "cancelled":
            (folder / "cancel").touch()
        callbacks = {}

        class FakeThread:
            def __init__(self, *, target, **kwargs):
                callbacks[target.__name__] = target
            def start(self):
                pass

        class FakeProcess:
            def __init__(self, command, **kwargs):
                shutil.copyfile(source.path, command[-1])
                self.returncode = 0
            def poll(self):
                return self.returncode
            def wait(self, **kwargs):
                return self.returncode
            def kill(self):
                self.returncode = -1

        class Samples:
            def astype(self, dtype):
                return self
            def __truediv__(self, divisor):
                return self

        model = Mock()
        if outcome == "failed":
            model.transcribe.side_effect = RuntimeError("synthetic inference failure")
        else:
            model.transcribe.return_value = (
                [SimpleNamespace(start=0.0, end=0.05, text="合成の原文")],
                SimpleNamespace(language="ja"),
            )

        def make_model(*args, **kwargs):
            if outcome == "hard_cancelled":
                (folder / "cancel").touch()
                callbacks["cancellation_watch"]()
            return model

        faster = ModuleType("faster_whisper")
        faster.__path__ = []
        faster.WhisperModel = make_model
        utils = ModuleType("faster_whisper.utils")
        utils.download_model = lambda *args, **kwargs: "synthetic-model"
        numpy = ModuleType("numpy")
        numpy.int16, numpy.float32 = "int16", "float32"
        numpy.frombuffer = lambda *args, **kwargs: Samples()
        ctranslate = ModuleType("ctranslate2")
        ctranslate.get_cuda_device_count = lambda: 0
        modules = {"faster_whisper": faster, "faster_whisper.utils": utils,
                   "numpy": numpy, "ctranslate2": ctranslate}
        with patch.dict(sys.modules, modules), patch.object(worker.threading, "Thread", FakeThread), patch.object(
            worker.subprocess, "Popen", FakeProcess
        ), patch.object(memos, "sync_audio_memo") as sync, patch.object(
            worker.os, "_exit", side_effect=SystemExit(0)
        ) as exit_process, patch.object(sys, "stderr", io.StringIO()):
            if outcome == "hard_cancelled":
                with self.assertRaises(SystemExit):
                    worker.run(folder)
                exit_process.assert_called_once_with(0)
            else:
                worker.run(folder)
                exit_process.assert_not_called()
            sync.assert_called_once_with(self.root, self.memo_id, self.job_id)
        state = tr.read_json(folder / "state.json")
        expected = "cancelled" if outcome == "hard_cancelled" else outcome
        self.assertEqual(state["status"], expected, state.get("error"))
        self.assertEqual(source.path.read_bytes(), wav_bytes())
        return store, folder

    def test_completed_worker_syncs_once_and_keeps_recognized_original(self):
        store, folder = self.run_worker("done")
        self.assertEqual(store.result(self.job_id)["segments"][0]["original_text"], "合成の原文")

    def test_cancelled_worker_syncs_once(self):
        self.run_worker("cancelled")

    def test_failed_worker_syncs_once(self):
        self.run_worker("failed")

    def test_hard_cancellation_syncs_before_process_exit_and_only_once(self):
        self.run_worker("hard_cancelled")


if __name__ == "__main__":
    unittest.main()
