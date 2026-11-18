"""Native recorder tests use synthetic PCM only; no real input stream."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
import wave

from app_core import long_recording as rec, recording_worker as worker
from app_core.media_conversion import MediaError
from app_core.transcription import atomic_json, read_json


DEVICE = {"id": 4, "name": "synthetic input", "default": True, "hostapi": "Windows WASAPI"}


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lr_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = rec.LongRecordingStore(self.root)

    def seed(self, status="starting", max_seconds=2):
        identifier = uuid4().hex
        folder = self.store.folder(identifier)
        (folder / "chunks").mkdir(parents=True)
        state = {"schema_version": 1, "id": identifier, "title": "synthetic", "created": time.time(),
                 "created_at": rec.now_text(), "status": status, "rate": 16000, "channels": 1, "sample_width": 2,
                 "max_seconds": max_seconds, "prevent_sleep": False, "device": dict(DEVICE), "owner": {"pid": os.getpid(), "creation_time": "1"},
                 "worker": None, "total_frames": 0, "saved_frames": 0, "segments": [], "events": [], "heartbeat": time.time(),
                 "level": 0.0, "last_saved_at": "", "error": "", "memo_id": None, "import_error": ""}
        atomic_json(folder / "recording.json", state)
        atomic_json(folder / "control.json", {"id": identifier, "revision": 0, "desired": "recording"})
        return folder, state

    def fake_source(self, count=12, frames=1600, status=False):
        class SyntheticInput:
            def __init__(instance, callback, device):
                instance.callback, instance.active = callback, False
            def start(instance):
                instance.active = True
                for _ in range(count):
                    instance.callback(b"\0\0" * frames, frames, None, status)
            def stop(instance):
                instance.active = False
            def close(instance):
                instance.active = False
        return SyntheticInput

    def run_worker(self, folder, source=None, owner=None, **options):
        owner = owner or SimpleNamespace(alive=lambda: True, close=lambda: None)
        with patch.object(worker, "process_identity", return_value={"pid": os.getpid(), "creation_time": "1"}), \
                patch.object(worker, "import_memo") as importer, \
                patch.object(rec, "process_alive", return_value=True):
            result = worker.run(folder, source_factory=source or self.fake_source(), owner_factory=lambda _: owner, **options)
        return result, importer

    def test_empty_history_and_device_query_do_not_create_storage_or_import_sounddevice(self):
        before = sys.modules.get("sounddevice")
        self.assertEqual(self.store.history(), [])
        result = SimpleNamespace(returncode=0, stdout=json.dumps([DEVICE]), stderr="")
        with patch.object(rec, "engine_available", return_value=True), patch.object(rec.subprocess, "run", return_value=result) as query:
            self.assertEqual(self.store.devices()[0]["id"], DEVICE["id"])
        self.assertIs(sys.modules.get("sounddevice"), before)
        self.assertFalse(self.store.base.exists())
        self.assertEqual(query.call_args.args[0][-1], "devices")
        self.assertEqual(query.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0], str(rec.dependency_dir(self.root)))

    def test_query_helper_filters_wasapi_and_uses_its_default_without_opening_stream(self):
        fake = SimpleNamespace(query_hostapis=lambda: [{"name": "MME", "default_input_device": 0}, {"name": "Windows WASAPI", "default_input_device": 2}],
            query_devices=lambda: [{"name": "duplicate", "hostapi": 0, "max_input_channels": 1, "default_samplerate": 48000},
                                   {"name": "output", "hostapi": 1, "max_input_channels": 0, "default_samplerate": 48000},
                                   {"name": "microphone", "hostapi": 1, "max_input_channels": 2, "default_samplerate": 48000}],
            RawInputStream=lambda **kwargs: self.fail("query must not open mic"))
        with patch.dict(sys.modules, sounddevice=fake):
            rows = worker.query_devices()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 2)
        self.assertTrue(rows[0]["default"])

    def test_start_idempotency_global_single_recording_and_worker_environment(self):
        operation = uuid4().hex
        with patch.object(rec, "engine_available", return_value=True), patch.object(self.store, "devices", return_value=[DEVICE]), \
                patch.object(rec, "process_identity", return_value={"pid": os.getpid(), "creation_time": "1"}), \
                patch.object(rec.subprocess, "Popen") as spawn:
            first = self.store.start(title="first", operation_id=operation)
            again = self.store.start(title="first", operation_id=operation)
            self.assertEqual(first["id"], again["id"])
            self.assertEqual(spawn.call_count, 1)
            with self.assertRaises(MediaError):
                self.store.start(title="second")
            with self.assertRaises(MediaError):
                self.store.start(title="different", operation_id=operation)
        self.assertEqual(spawn.call_args.kwargs["env"]["PYTHONNOUSERSITE"], "1")
        self.assertEqual(first["max_seconds"], 10800)

    def test_pause_resume_and_stop_controls_are_idempotent_and_stop_is_sticky(self):
        folder, state = self.seed()
        self.store.pause(state["id"])
        one = read_json(folder / "control.json")
        self.store.pause(state["id"])
        self.assertEqual(read_json(folder / "control.json"), one)
        self.store.resume(state["id"])
        self.assertEqual(read_json(folder / "control.json")["desired"], "recording")
        self.store.stop(state["id"])
        with self.assertRaises(MediaError):
            self.store.resume(state["id"])
        self.assertEqual(read_json(folder / "recording.json")["status"], "starting")

    def test_pcm_writer_clamps_sample_count_and_each_chunk_is_independent(self):
        folder, state = self.seed(max_seconds=2)
        writer = worker.PCMWriter(folder, state, chunk_seconds=0.3)
        for _ in range(30):
            if writer.append(b"\x01\0" * 1600):
                break
        writer.finish()
        self.assertEqual(state["total_frames"], 32000)
        self.assertEqual(state["saved_frames"], 32000)
        self.assertEqual(sum(row["frames"] for row in state["segments"]), 32000)
        for row in state["segments"]:
            path = folder / row["file"]
            with wave.open(str(path), "rb") as audio:
                self.assertEqual(audio.getparams()[:3], (1, 2, 16000))
                self.assertEqual(audio.getnframes(), row["frames"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row["sha256"])
        self.assertFalse(list((folder / "chunks").glob("*.part.pcm")))

    def test_checkpoint_and_crashed_partial_recovery_keep_complete_frames(self):
        folder, state = self.seed()
        writer = worker.PCMWriter(folder, state)
        writer.append(b"\x02\0" * 1234)
        writer.checkpoint()
        self.assertEqual(read_json(folder / "recording.json")["saved_frames"], 1234)
        writer.stream.close()  # Simulate process loss without finish().
        writer.stream = None
        with patch.object(worker, "import_memo"):
            result = self.store.recover(state["id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["total_frames"], 1234)
        self.assertEqual(len(result["segments"]), 1)

    def test_manifest_ack_before_partial_cleanup_does_not_duplicate_pcm(self):
        folder, state = self.seed()
        writer = worker.PCMWriter(folder, state, chunk_seconds=0.1)
        writer.append(b"\x01\0" * 1600)
        part = folder / "chunks/000000.part.pcm"
        part.write_bytes(b"\x01\0" * 1600)  # Leftover after a committed WAV ACK.
        worker.recover_files(folder, state)
        self.assertEqual(state["total_frames"], 1600)
        self.assertEqual(len(state["segments"]), 1)

    def test_corrupt_confirmed_chunk_stops_recovery_without_overwrite(self):
        folder, state = self.seed()
        writer = worker.PCMWriter(folder, state, chunk_seconds=0.1)
        writer.append(b"\x01\0" * 1600)
        path = folder / state["segments"][0]["file"]
        raw = path.read_bytes()
        path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        changed = path.read_bytes()
        with self.assertRaises(MediaError):
            self.store.recover(state["id"])
        self.assertEqual(path.read_bytes(), changed)

    def test_live_worker_lease_blocks_second_worker_and_recovery(self):
        folder, state = self.seed()
        with self.store.worker_guard():
            with self.assertRaises(MediaError):
                self.store.recover(state["id"])
            with self.assertRaises(MediaError):
                worker.run(folder, source_factory=lambda *_: self.fail("must not open mic"))

    def test_silence_is_valid_and_upper_bound_stops_exactly_without_external_asr(self):
        folder, state = self.seed(max_seconds=1)
        result, importer = self.run_worker(folder)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["total_frames"], 16000)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["level"], 0)
        importer.assert_called_once()

    def test_pause_resume_releases_input_and_keeps_only_recorded_frames(self):
        folder, state = self.seed(max_seconds=4)
        calls = [0]
        def alive():
            calls[0] += 1
            if calls[0] == 3:
                self.store.pause(state["id"])
            elif calls[0] == 4:
                self.store.resume(state["id"])
            elif calls[0] == 6:
                self.store.stop(state["id"])
            return True
        result, _ = self.run_worker(folder, source=self.fake_source(count=2), owner=SimpleNamespace(alive=alive, close=lambda: None))
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["total_frames"], 6400)
        self.assertEqual([event["kind"] for event in result["events"]], ["pause", "resume"])
        self.assertEqual(len(result["segments"]), 2)

    def test_parent_exit_before_stream_and_terminal_startup_race_open_no_input(self):
        folder, state = self.seed()
        result, _ = self.run_worker(folder, source=lambda *_: self.fail("dead parent must not open mic"),
                                    owner=SimpleNamespace(alive=lambda: False, close=lambda: None))
        self.assertEqual(result["status"], "interrupted")
        result, _ = self.run_worker(folder, source=lambda *_: self.fail("terminal manifest must not reopen mic"))
        self.assertEqual(result["status"], "interrupted")

    def test_slow_pause_seal_and_resume_initialization_are_not_sleep_gaps(self):
        folder, state = self.seed(max_seconds=4)
        ticks = [0.0]
        calls = [0]
        def alive():
            calls[0] += 1
            if calls[0] == 3:
                self.store.pause(state["id"])
            elif calls[0] == 4:
                self.store.resume(state["id"])
            elif calls[0] == 6:
                self.store.stop(state["id"])
            return True
        basic = self.fake_source(count=2)
        class SlowResume(basic):
            starts = 0
            def start(instance):
                instance.starts += 1
                super().start()
                if instance.starts > 1:
                    ticks[0] += 6
        original = worker.PCMWriter.seal_pause
        def slow_seal(writer):
            original(writer)
            ticks[0] += 6
        with patch.object(worker.PCMWriter, "seal_pause", slow_seal):
            result, _ = self.run_worker(folder, source=SlowResume,
                owner=SimpleNamespace(alive=alive, close=lambda: None), clock=lambda: ticks[0], wall_clock=lambda: 1000 + ticks[0])
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["error"], "")
        self.assertEqual(result["total_frames"], 6400)
        self.assertEqual(len(result["segments"]), 2)
        self.assertFalse(list((folder / "chunks").glob("*.part.pcm")))

    def test_stopped_control_before_worker_start_opens_no_input(self):
        folder, state = self.seed()
        self.store.stop(state["id"])
        result, _ = self.run_worker(folder, source=lambda *_: self.fail("already stopped command must not open mic"))
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["total_frames"], 0)

    def test_parent_exit_during_recording_preserves_enqueued_pcm(self):
        folder, state = self.seed(max_seconds=4)
        checks = iter([True, True, False])
        result, _ = self.run_worker(folder, source=self.fake_source(count=3),
                                    owner=SimpleNamespace(alive=lambda: next(checks, False), close=lambda: None))
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["total_frames"], 4800)
        self.assertIn("アプリ本体", result["error"])

    def test_overflow_and_bounded_queue_interrupt_instead_of_silent_loss(self):
        for count, status in ((3, True), (worker.QUEUE_BLOCKS + 1, False)):
            with self.subTest(count=count):
                folder, state = self.seed(max_seconds=10)
                result, _ = self.run_worker(folder, source=self.fake_source(count=count, status=status))
                self.assertEqual(result["status"], "interrupted")
                self.assertIn("欠落", result["error"])
                self.assertLessEqual(result["total_frames"], worker.QUEUE_BLOCKS * 1600)

    def test_disk_full_and_sleep_gap_stop_with_raw_audio_retained(self):
        folder, state = self.seed(max_seconds=4)
        with patch.object(worker.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            result, _ = self.run_worker(folder, source=self.fake_source(count=3))
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["total_frames"], 4800)
        self.assertIn("容量", result["error"])
        folder, state = self.seed(max_seconds=4)
        calls = [0]
        def wall():
            calls[0] += 1
            return 1000 if calls[0] < 5 else 1020
        result, _ = self.run_worker(folder, source=self.fake_source(count=3), wall_clock=wall)
        self.assertEqual(result["status"], "interrupted")
        self.assertIn("スリープ", result["error"])

    def test_writer_finalize_failure_retains_partial_then_explicit_recovery_succeeds(self):
        folder, state = self.seed(max_seconds=1)
        with patch.object(worker, "_seal_part", side_effect=OSError("synthetic disk error")):
            result, _ = self.run_worker(folder)
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(list((folder / "chunks").glob("*.part.pcm")))
        with patch.object(worker, "import_memo"):
            repaired = self.store.recover(state["id"])
        self.assertEqual(repaired["total_frames"], 16000)
        self.assertEqual(len(repaired["segments"]), 1)

    def test_import_error_keeps_terminal_audio_and_recovery_can_retry(self):
        folder, state = self.seed(status="done")
        writer = worker.PCMWriter(folder, state, chunk_seconds=0.1)
        writer.append(b"\0\0" * 1600)
        with patch("app_core.audio_memo_service.AudioMemoStore.import_long_recording", side_effect=MediaError("synthetic import failure")):
            worker.import_memo(self.root, folder, state)
        self.assertTrue(state["import_error"])
        self.assertTrue((folder / state["segments"][0]["file"]).is_file())
        with patch("app_core.audio_memo_service.AudioMemoStore.import_long_recording", return_value={"id": state["id"]}):
            result = self.store.recover(state["id"])
        self.assertEqual(result["memo_id"], state["id"])
        self.assertEqual(result["import_error"], "")

    @unittest.skipUnless(os.name == "nt", "Windows process HANDLE test")
    def test_windows_owner_identity_uses_handle_and_rejects_pid_reuse_identity(self):
        owner = rec.process_identity()
        self.assertTrue(rec.process_alive(owner))
        self.assertFalse(rec.process_alive(dict(owner, creation_time="0")))
        self.assertEqual(owner["pid"], os.getpid())


if __name__ == "__main__":
    unittest.main()
