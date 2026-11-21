"""Long recording import/resume with synthetic PCM; no real mic or ASR model."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
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

from app_core import audio_memo_service as memo
from app_core import long_recording as native
from app_core import transcription as tr
from app_core import transcription_worker as worker
from app_core.media_conversion import MediaError, Source, Track, Tools, _identity


def write_wav(path, seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        left = int(seconds * 16000)
        while left:
            count = min(left, 512 * 1024)
            output.writeframesraw(b"\0\0" * count)
            left -= count


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pglam_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = memo.AudioMemoStore(self.root)
        self.recording_id = "a" * 32

    def recording(self, lengths=(0.1, 0.2), *, state=None):
        folder = self.root / "transcription_data/recordings" / self.recording_id
        folder.mkdir(parents=True, exist_ok=True)
        rows = []
        for index, seconds in enumerate(lengths):
            name = f"chunks/{index:06d}.wav"
            path = folder / name
            write_wav(path, seconds)
            rows.append({"index": index, "file": name, "frames": int(seconds * 16000),
                         "bytes": path.stat().st_size, "sha256": tr.file_sha256(path)})
        state = state or {"schema_version": 1, "id": self.recording_id, "title": "synthetic native recording",
                          "created": time.time(), "owner": {"pid": 1, "creation_time": "134000000000000000"},
                          "rate": 16000, "channels": 1, "sample_width": 2}
        state.update(status="stopped", segments=rows, total_frames=sum(row["frames"] for row in rows))
        tr.atomic_json(folder / "recording.json", state)
        return folder, state


class LongImportTests(Fixture):
    def seed_recognized(self):
        self.recording()
        item = self.store.import_long_recording(self.recording_id)
        path = self.store.audio_path(item["id"])
        job_id, operation = "d" * 32, "e" * 32
        folder = self.store.transcriptions.folder(job_id)
        folder.mkdir(parents=True)
        request = {"id": job_id, "audio_memo_id": item["id"], "source_path": str(path),
                   "source_sha256": item["audio_sha256"], "duration": item["duration"], "track": {"index": 0},
                   "language": "ja", "model": "small", "hotwords": "", "device": "cpu"}
        tr.atomic_json(folder / "request.json", request)
        tr.atomic_json(folder / "state.json", {"id": job_id, "audio_memo_id": item["id"], "operation_id": operation,
            "status": "failed", "name": "audio.wav", "created": time.time(), "error": "synthetic interrupted recognition"})
        line = (json.dumps({"id": 0, "start": 0.0, "end": 0.05, "original_text": "recognized original"}) + "\n").encode()
        (folder / "segments.jsonl").write_bytes(line)
        checkpoint = {"schema_version": 1, "job_id": job_id, "signature": tr.checkpoint_signature(request),
                      "source_sha256": item["audio_sha256"], "completed_seconds": item["duration"],
                      "segments_count": 1, "segments_bytes": len(line), "segments_sha256": hashlib.sha256(line).hexdigest(),
                      "detected_languages": ["ja"]}
        tr.atomic_json(folder / "checkpoint.json", checkpoint)
        item.update(job_id=job_id, start_operation_id=operation,
                    start_settings={"language": "ja", "mode": "standard", "hotwords": ""})
        self.store._write(item)
        item = self.store.sync(item["id"])
        item = self.store.save(item["id"], item["revision"], "edited", "manually edited memo")
        source = Source(item["id"], path, "audio.wav", "recording", _identity(path), item["duration"],
                        (Track(0, "pcm_s16le", "", "", 1, item["duration"]),))
        return item, source, folder

    def retry_with_fake_start(self, item, source, **options):
        def launch(source, track, language, mode, hotwords, **kwargs):
            job_id = "f" * 32
            folder = self.store.transcriptions.folder(job_id)
            folder.mkdir(parents=True)
            tr.atomic_json(folder / "request.json", {"id": job_id, "audio_memo_id": item["id"], "source_path": str(source.path)})
            tr.atomic_json(folder / "state.json", {"id": job_id, "audio_memo_id": item["id"], "name": "audio.wav",
                "operation_id": kwargs["operation_id"], "status": "starting", "created": time.time()})
            return job_id
        with patch.object(memo.TranscriptionSources, "prepare_recording", return_value=source), patch.object(
            self.store.transcriptions, "start", side_effect=launch
        ) as start:
            self.store.start(item["id"], retry=True, **options)
        return start.call_args.kwargs, self.store.get(item["id"])

    def test_corrupt_checkpoint_falls_back_to_fresh_and_timed_text_stays_original(self):
        item, source, folder = self.seed_recognized()
        checkpoint = tr.read_json(folder / "checkpoint.json")
        tr.atomic_json(folder / "checkpoint.json", {**checkpoint, "segments_sha256": "0" * 64})
        options, current = self.retry_with_fake_start(item, source)
        self.assertNotIn("resume_from", options)
        self.assertTrue(current["resume_warning"])
        self.assertEqual(current["text"], "manually edited memo")
        timed = self.store.timed_text(item["id"]).decode("utf-8-sig")
        self.assertIn("00:00:00.000 --> 00:00:00.050", timed)
        self.assertIn("recognized original", timed)
        self.assertNotIn("manually edited memo", timed)

    def test_no_checkpoint_or_new_settings_use_a_fresh_recognition(self):
        item, source, folder = self.seed_recognized()
        (folder / "checkpoint.json").unlink()
        options, current = self.retry_with_fake_start(item, source, language="en")
        self.assertNotIn("resume_from", options)
        self.assertEqual(current["text"], "manually edited memo")

    def test_valid_checkpoint_is_forwarded_on_retry(self):
        item, source, folder = self.seed_recognized()
        options, current = self.retry_with_fake_start(item, source)
        self.assertEqual(options["resume_from"], folder.name)
        self.assertEqual(current["text"], "manually edited memo")

    def test_checkpoint_without_request_device_uses_safe_fresh_recognition(self):
        item, source, folder = self.seed_recognized()
        request = tr.read_json(folder / "request.json")
        del request["device"]
        tr.atomic_json(folder / "request.json", request)
        checkpoint = tr.read_json(folder / "checkpoint.json")
        tr.atomic_json(folder / "checkpoint.json", {**checkpoint, "signature": tr.checkpoint_signature(request)})
        before = [(folder / name).read_bytes() for name in ("request.json", "checkpoint.json", "segments.jsonl")]
        options, current = self.retry_with_fake_start(item, source)
        self.assertNotIn("resume_from", options)
        self.assertEqual(options["device"], "cpu")
        self.assertTrue(current["resume_warning"])
        self.assertEqual(current["text"], "manually edited memo")
        self.assertEqual(before, [(folder / name).read_bytes() for name in ("request.json", "checkpoint.json", "segments.jsonl")])

    def test_matching_auto_device_and_precise_model_keep_valid_checkpoint(self):
        item, source, folder = self.seed_recognized()
        request = tr.read_json(folder / "request.json")
        request.update(device="auto", model="large-v3")
        tr.atomic_json(folder / "request.json", request)
        checkpoint = tr.read_json(folder / "checkpoint.json")
        tr.atomic_json(folder / "checkpoint.json", {**checkpoint, "signature": tr.checkpoint_signature(request)})
        item["start_settings"] = {"language": "ja", "mode": "precise", "hotwords": "", "device": "auto"}
        self.store._write(item)
        options, current = self.retry_with_fake_start(item, source, mode="precise", device="auto")
        self.assertEqual(options["resume_from"], folder.name)
        self.assertEqual(options["device"], "auto")
        self.assertEqual(current["text"], "manually edited memo")

    def test_requested_device_change_does_not_reuse_cpu_checkpoint(self):
        item, source, folder = self.seed_recognized()
        options, current = self.retry_with_fake_start(item, source, device="auto")
        self.assertNotIn("resume_from", options)
        self.assertEqual(options["device"], "auto")
        self.assertEqual(current["text"], "manually edited memo")

    def test_real_native_start_manifest_bridges_without_a_microphone(self):
        recorder = native.LongRecordingStore(self.root)
        with patch.object(native, "engine_available", return_value=True), patch.object(
            recorder, "devices", return_value=[{"id": 0, "name": "synthetic WASAPI", "default": True}]
        ), patch.object(native.subprocess, "Popen"), patch.object(native.threading, "Thread"):
            created = recorder.start("native bridge", max_seconds=30)
        self.recording_id = created["id"]
        state = tr.read_json(recorder.folder(self.recording_id) / "recording.json")
        self.assertIsInstance(state["owner"]["creation_time"], str)
        self.assertRegex(state["owner"]["creation_time"], r"^[0-9]+$")
        self.recording(state=state)
        imported = self.store.import_long_recording(self.recording_id)
        self.assertEqual(imported["id"], self.recording_id)
        self.assertEqual(imported["source_kind"], "long_recording")

    def test_half_hour_import_adapter_and_playback_never_use_read_bytes(self):
        folder, state = self.recording((600, 600, 600))
        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole audio read forbidden")):
            item = self.store.import_long_recording(self.recording_id)
            manager = tr.TranscriptionSources(self.root, tools=Tools(Path("fake-ffmpeg"), Path("fake-ffprobe")))
            self.addCleanup(manager.close)
            probe = {"format": {"format_name": "wav", "duration": 1800},
                     "streams": [{"codec_type": "audio", "index": 0, "codec_name": "pcm_s16le", "channels": 1}]}
            with patch.object(tr, "_probe", return_value=probe):
                source = manager.prepare_recording("b" * 32, self.store.audio_path(item["id"]))
            audio = self.store.playback(item["id"], start_sec=1700, seconds=1000)
        self.assertEqual(source.duration, 1800)
        self.assertEqual(item["frames"], 1800 * 16000)
        self.assertEqual(tr.file_sha256(self.store.audio_path(item["id"])), item["audio_sha256"])
        self.assertTrue(all((folder / row["file"]).is_file() for row in state["segments"]))
        with wave.open(io.BytesIO(audio), "rb") as clip:
            self.assertEqual(clip.getnframes(), 60 * 16000)
        self.assertLess(len(audio), 2 * 1024**2)

    def test_import_replay_preserves_saved_edits_and_detects_changed_chunks(self):
        folder, state = self.recording()
        item = self.store.import_long_recording(self.recording_id)
        saved = self.store.save(item["id"], item["revision"], "edited title", "edited text")
        again = self.store.import_long_recording(self.recording_id)
        self.assertEqual((again["title"], again["text"], again["revision"]),
                         (saved["title"], saved["text"], saved["revision"]))
        path = folder / state["segments"][0]["file"]
        with path.open("r+b") as stream:
            stream.seek(44)
            stream.write(b"\1\0")
        with self.assertRaises(MediaError):
            self.store.import_long_recording(self.recording_id)
        self.assertEqual(self.store.get(item["id"])["text"], "edited text")

    def test_manifest_and_chunk_faults_do_not_create_a_memo(self):
        folder, state = self.recording()
        cases = []
        for key, value in (("status", "recording"), ("total_frames", state["total_frames"] + 1), ("owner", {"pid": 1, "creation_time": "bad"})):
            changed = deepcopy(state); changed[key] = value; cases.append(changed)
        for key, value in (("index", 2), ("file", "../outside.wav"), ("frames", 1), ("bytes", 1), ("sha256", "0" * 64)):
            changed = deepcopy(state); changed["segments"][0][key] = value; cases.append(changed)
        for changed in cases:
            tr.atomic_json(folder / "recording.json", changed)
            with self.subTest(changed=changed), self.assertRaises(MediaError):
                self.store.import_long_recording(self.recording_id)
            self.assertFalse((self.store.base / self.recording_id / "memo.json").exists())
        self.assertTrue((folder / "chunks/000000.wav").exists())

    def test_browser_recording_limit_and_invalid_playback_are_unchanged(self):
        path = self.root / "long.wav"
        write_wav(path, 601)
        with self.assertRaises(MediaError):
            memo.validate_recording_file(path)
        memo.validate_recording_file(path, long_recording=True)
        self.recording()
        item = self.store.import_long_recording(self.recording_id)
        for start, seconds in ((-1, 1), (float("nan"), 1), (0, 0), (0, float("inf")), (100, 1)):
            with self.subTest(start=start, seconds=seconds), self.assertRaises(MediaError):
                self.store.playback(item["id"], start, seconds)
        self.assertEqual(self.store.timed_text(item["id"]), b"")


@contextmanager
def fake_inference(model, source):
    class Samples:
        def astype(self, dtype): return self
        def __truediv__(self, value): return self
    class Process:
        def __init__(self, command, **kwargs):
            shutil.copyfile(source, command[-1]); self.returncode = 0
        def poll(self): return 0
        def wait(self, **kwargs): return 0
    faster = ModuleType("faster_whisper"); faster.__path__ = []
    faster.WhisperModel = lambda *args, **kwargs: model
    snapshot = source.parent / "synthetic-model"
    snapshot.mkdir(exist_ok=True)
    (snapshot / "model.bin").write_bytes(b"synthetic model")
    (snapshot / "config.json").write_bytes(b"{}")
    utils = ModuleType("faster_whisper.utils")
    utils.download_model = lambda *args, **kwargs: str(snapshot)
    numpy = ModuleType("numpy"); numpy.int16 = "int16"; numpy.float32 = "float32"
    numpy.frombuffer = lambda *args, **kwargs: Samples()
    translate = ModuleType("ctranslate2"); translate.get_cuda_device_count = lambda: 0
    with patch.dict(sys.modules, {"faster_whisper": faster, "faster_whisper.utils": utils, "numpy": numpy, "ctranslate2": translate}), \
            patch.object(worker.subprocess, "Popen", Process), patch.object(worker.threading, "Thread"), patch.object(sys, "stderr", io.StringIO()):
        yield


class ResumeTests(Fixture):
    def setUp(self):
        super().setUp()
        self.transcriptions = tr.TranscriptionStore(self.root)
        self.source_path = self.root / "source.wav"
        write_wav(self.source_path, 610)
        self.source = Source("c" * 32, self.source_path, "source.wav", "local", _identity(self.source_path), 610.0,
                             (Track(0, "pcm_s16le", "", "", 1, 610.0),))
        for item in (patch.object(tr, "engine_available", return_value=True),
                     patch.object(tr, "discover_tools", return_value=SimpleNamespace(ffmpeg=Path("fake-ffmpeg"))),
                     patch.object(tr.subprocess, "Popen"), patch.object(tr.threading, "Thread")):
            item.start(); self.addCleanup(item.stop)

    def start(self, **options):
        return self.transcriptions.start(self.source, 0, "ja", "standard", **options)

    def failed_job(self, silent_first=False):
        previous = self.start()
        calls = []
        def transcribe(*args, **kwargs):
            calls.append(1)
            def rows():
                if not (silent_first and len(calls) == 1):
                    yield SimpleNamespace(start=2.1, end=3.5, text="prefix" if len(calls) == 1 else "unfinished tail")
                if len(calls) == 2: raise RuntimeError("synthetic mid-window failure")
            return rows(), SimpleNamespace(language="ja")
        with fake_inference(SimpleNamespace(transcribe=transcribe), self.source_path):
            worker.run(self.transcriptions.folder(previous))
        self.assertEqual(self.transcriptions._state(previous)["status"], "failed")
        checkpoint = tr.read_json(self.transcriptions.folder(previous) / "checkpoint.json")
        self.assertEqual(checkpoint["completed_seconds"], 300)
        self.assertEqual(checkpoint["segments_count"], 0 if silent_first else 1)
        return previous

    def test_completed_silent_window_resumes_from_an_empty_prefix(self):
        previous = self.failed_job(silent_first=True)
        resumed = self.start(resume_from=previous)
        folder = self.transcriptions.folder(resumed)
        self.assertEqual((folder / "segments.jsonl").stat().st_size, 0)
        model = Mock()
        model.transcribe.return_value = ([SimpleNamespace(start=2.1, end=3.5, text="speech after silence")], SimpleNamespace(language="ja"))
        with fake_inference(model, self.source_path):
            worker.run(folder)
        result = self.transcriptions.result(resumed)
        self.assertEqual(result["state"]["status"], "done")
        self.assertEqual(model.transcribe.call_count, 2)
        self.assertEqual([row["id"] for row in result["segments"]], [0, 1])
        self.assertAlmostEqual(result["segments"][0]["start"], 300.1)

    def test_resume_reuses_only_completed_window_and_keeps_original_tail(self):
        previous = self.failed_job()
        old = self.transcriptions.folder(previous) / "segments.jsonl"
        before = old.read_bytes()
        resumed = self.start(resume_from=previous)
        new_folder = self.transcriptions.folder(resumed)
        prefix = (new_folder / "segments.jsonl").read_text(encoding="utf-8")
        self.assertIn("prefix", prefix)
        self.assertNotIn("unfinished tail", prefix)
        model = Mock()
        model.transcribe.return_value = ([SimpleNamespace(start=2.1, end=3.5, text="continued")], SimpleNamespace(language="ja"))
        with fake_inference(model, self.source_path):
            worker.run(new_folder)
        result = self.transcriptions.result(resumed)
        self.assertEqual(result["state"]["status"], "done")
        self.assertEqual(model.transcribe.call_count, 2)
        self.assertEqual([row["id"] for row in result["segments"]], [0, 1, 2])
        self.assertAlmostEqual(result["segments"][1]["start"], 300.1)
        self.assertEqual(old.read_bytes(), before)

    def test_corrupt_prefix_or_changed_settings_are_never_reused(self):
        previous = self.failed_job()
        with self.assertRaises(MediaError):
            self.transcriptions.start(self.source, 0, "en", "standard", resume_from=previous)
        path = self.transcriptions.folder(previous) / "segments.jsonl"
        with path.open("r+b") as stream:
            stream.seek(0); stream.write(b"X")
        with self.assertRaises(MediaError):
            self.start(resume_from=previous)

    def test_missing_checkpoint_and_changed_source_are_rejected_by_explicit_resume(self):
        previous = self.start()
        folder = self.transcriptions.folder(previous)
        state = tr.read_json(folder / "state.json")
        tr.atomic_json(folder / "state.json", {**state, "status": "failed"})
        with self.assertRaises(MediaError):
            self.start(resume_from=previous)
        previous = self.failed_job()
        with self.source_path.open("r+b") as stream:
            stream.seek(44); stream.write(b"\1\0")
        self.source = Source(self.source.id, self.source.path, self.source.name, self.source.mode,
                             _identity(self.source.path), self.source.duration, self.source.tracks)
        with self.assertRaises(MediaError):
            self.start(resume_from=previous)


if __name__ == "__main__":
    unittest.main()
