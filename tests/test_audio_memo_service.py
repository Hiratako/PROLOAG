"""Synthetic WAV/storage boundaries; no microphone, model, or worker startup."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
import wave

from app_core import audio_memo_service as memo
from app_core.media_conversion import MediaError, Source, Track, _identity
from app_core.transcription import atomic_json, read_json


def recording(seconds=0.1, value=b"\0\0"):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(value * int(seconds * 16000))
    return buffer.getvalue()


class AudioMemoServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="am_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = memo.AudioMemoStore(self.root)
        self.raw = recording()

    def capture(self, capture_id="synthetic-capture", title="test"):
        return self.store.capture(self.raw, capture_id, title)

    def prepare(self, owner, path):
        details = memo.validate_recording(path.read_bytes())
        return Source(path.parent.name, path, path.name, "recording", _identity(path), details["duration"],
                      (Track(0, "pcm_s16le", "", "", 1, details["duration"]),))

    def seed_job(self, source, *, operation_id, audio_memo_id, status="starting", text=""):
        job_id = uuid4().hex
        folder = self.store.transcriptions.folder(job_id)
        folder.mkdir(parents=True)
        atomic_json(folder / "request.json", {"id": job_id, "audio_memo_id": audio_memo_id,
                    "source_path": str(source.path), "language": "ja", "model": "small", "duration": source.duration,
                    "track": {"index": 0}})
        atomic_json(folder / "state.json", {"id": job_id, "audio_memo_id": audio_memo_id,
                    "operation_id": operation_id, "status": status, "created": time.time(), "name": "audio.wav",
                    "progress": 1.0 if status == "done" else 0.1, "elapsed": 1.0, "error": "", "message": status})
        if text:
            (folder / "segments.jsonl").write_text(json.dumps({"id": 0, "start": 0.0, "end": source.duration,
                                                              "original_text": text}, ensure_ascii=False) + "\n", encoding="utf-8")
        return job_id

    def start(self, item, status="done", text="認識した原文", early_hook=False, lost_ack=False, retry=False):
        def launch(source, track, language, mode, hotwords, *, operation_id, audio_memo_id):
            job_id = self.seed_job(source, operation_id=operation_id, audio_memo_id=audio_memo_id, status=status, text=text)
            if early_hook:
                memo.sync_audio_memo(self.root, audio_memo_id, job_id)
            if lost_ack:
                raise OSError("synthetic lost ACK after durable job creation")
            return job_id

        with patch.object(memo.TranscriptionSources, "prepare_recording", side_effect=self.prepare), \
                patch.object(self.store.transcriptions, "start", side_effect=launch) as process:
            job_id = self.store.start(item["id"], retry=retry)
        return job_id, process.call_count

    def test_readonly_empty_store_and_invalid_ids_create_nothing(self):
        self.assertEqual(self.store.history(), [])
        self.assertFalse(self.store.busy())
        for value in ("../outside", "A" * 32, "x" * 32, None):
            with self.subTest(value=value), self.assertRaises(MediaError):
                self.store.get(value)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_capture_saves_wav_json_txt_and_title_never_becomes_a_path(self):
        item = self.capture(title="../自由タイトル\\別名")
        folder = self.root / "transcription_data/memos" / item["id"]
        self.assertRegex(item["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(self.store.audio_path(item["id"]).read_bytes(), self.raw)
        self.assertEqual(self.store.txt_path(item["id"]).read_text(encoding="utf-8-sig"), "")
        self.assertEqual(item["revision"], 0)
        self.assertAlmostEqual(item["duration"], 0.1)
        self.assertTrue(item["txt_synced"])
        self.assertTrue((folder / "memo.json").is_file())
        self.assertEqual(memo.AudioMemoStore(self.root).history()[0]["id"], item["id"])

    def test_replayed_capture_is_idempotent_but_distinct_takes_are_not_deduplicated(self):
        item = self.capture()
        paths = [self.store.audio_path(item["id"]), self.store.txt_path(item["id"]), self.store._folder(item["id"]) / "memo.json"]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        again = self.capture(title="replayed title must not overwrite")
        self.assertEqual(again["id"], item["id"])
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths])
        with self.assertRaises(MediaError):
            self.store.capture(recording(value=b"\x01\0"), "synthetic-capture")
        other = self.capture("different-take")
        self.assertNotEqual(other["id"], item["id"])

    def test_invalid_or_truncated_wave_and_limits_fail_before_storage_creation(self):
        for raw in (b"", b"not a wav", self.raw[:-1]):
            with self.subTest(size=len(raw)), self.assertRaises(MediaError):
                self.store.capture(raw, uuid4().hex)
        with patch.object(memo, "MAX_RECORDING_BYTES", 20), self.assertRaises(MediaError):
            self.store.capture(self.raw, uuid4().hex)
        with patch.object(memo, "MAX_RECORDING_SECONDS", 0.01), self.assertRaises(MediaError):
            self.store.capture(self.raw, uuid4().hex)
        self.assertFalse(self.store.base.exists())

    def test_metadata_failure_preserves_audio_and_same_capture_can_recover(self):
        with patch.object(memo, "atomic_json", side_effect=OSError("synthetic disk failure")), self.assertRaises(MediaError):
            self.capture()
        originals = list(self.store.base.glob("*/audio.wav"))
        self.assertEqual(len(originals), 1)
        self.assertEqual(originals[0].read_bytes(), self.raw)
        item = self.capture()
        self.assertEqual(self.store.audio_path(item["id"]), originals[0])

    def test_text_cas_lost_ack_resubmission_and_json_failure_preserve_data(self):
        item = self.capture()
        saved = self.store.save(item["id"], 0, "title", "手入力")
        self.assertEqual(saved["revision"], 1)
        replay = self.store.save(item["id"], 0, "title", "手入力")
        self.assertEqual(replay["revision"], 1)
        with self.assertRaises(MediaError):
            self.store.save(item["id"], 0, "stale", "stale")
        before = (self.store._folder(item["id"]) / "memo.json").read_bytes()
        with patch.object(memo, "atomic_json", side_effect=OSError("synthetic disk failure")), self.assertRaises(MediaError):
            self.store.save(item["id"], 1, "new", "unsaved")
        self.assertEqual((self.store._folder(item["id"]) / "memo.json").read_bytes(), before)
        self.assertEqual(self.store.audio_path(item["id"]).read_bytes(), self.raw)

    def test_txt_failure_leaves_json_saved_and_explicit_regeneration_repairs(self):
        item = self.capture()
        with patch.object(memo, "_atomic_bytes", side_effect=OSError("synthetic TXT failure")):
            saved = self.store.save(item["id"], 0, "saved", "JSONに残る本文")
        self.assertFalse(saved["txt_synced"])
        self.assertTrue(saved["txt_error"])
        self.assertEqual(self.store.get(item["id"])["text"], "JSONに残る本文")
        repaired = self.store.regenerate_txt(item["id"], saved["revision"])
        self.assertTrue(repaired["txt_synced"])
        self.assertEqual(repaired["revision"], saved["revision"])
        self.assertEqual(self.store.txt_path(item["id"]).read_text(encoding="utf-8-sig"), saved["text"])

    def test_worker_hook_before_start_returns_links_and_persists_text_once(self):
        item = self.capture()
        job_id, count = self.start(item, early_hook=True)
        current = self.store.get(item["id"])
        self.assertEqual(count, 1)
        self.assertEqual(current["job_id"], job_id)
        self.assertEqual(current["original_text"], "認識した原文")
        self.assertEqual(current["text"], "認識した原文")
        self.assertTrue(current["txt_synced"])
        self.assertEqual(self.store.txt_path(item["id"]).read_text(encoding="utf-8-sig"), current["text"])
        self.assertEqual(self.store.sync(item["id"])["revision"], current["revision"])
        with patch.object(self.store.transcriptions, "start", side_effect=AssertionError("duplicate spawn")):
            self.assertEqual(self.store.start(item["id"]), job_id)

    def test_partial_active_results_wait_until_terminal_and_title_only_does_not_block(self):
        item = self.capture()
        titled = self.store.save(item["id"], 0, "タイトル変更のみ", "")
        self.assertFalse(titled["text_edited"])
        job_id, _ = self.start(item, status="transcribing", text="途中の原文")
        current = self.store.sync(item["id"])
        self.assertEqual(current["text"], "")
        self.assertEqual(current["original_text"], "")
        folder = self.store.transcriptions.folder(job_id)
        state = read_json(folder / "state.json")
        state["status"] = "cancelled"
        atomic_json(folder / "state.json", state)
        terminal = self.store.sync(item["id"])
        self.assertEqual(terminal["title"], "タイトル変更のみ")
        self.assertEqual(terminal["text"], "途中の原文")

    def test_manual_text_and_explicit_clear_survive_recognition_and_retry(self):
        item = self.capture()
        saved = self.store.save(item["id"], 0, "manual", "先に手入力")
        self.start(item)
        current = self.store.sync(item["id"])
        self.assertEqual(current["text"], "先に手入力")
        self.assertEqual(current["original_text"], "認識した原文")
        cleared = self.store.save(item["id"], saved["revision"], "manual", "")
        self.start(item, text="再認識の原文", retry=True)
        result = self.store.sync(item["id"])
        self.assertEqual(result["text"], "")
        self.assertTrue(result["text_edited"])
        self.assertEqual(result["revision"], cleared["revision"])
        self.assertEqual(len(result["original_versions"]), 2)

    def test_start_failure_keeps_audio_and_retry_reuses_the_reserved_operation(self):
        item = self.capture()
        with patch.object(memo.TranscriptionSources, "prepare_recording", side_effect=MediaError("engine missing")), self.assertRaises(MediaError):
            self.store.start(item["id"])
        failed = self.store.get(item["id"])
        operation = failed["start_operation_id"]
        self.assertEqual(failed["status"], "start_failed")
        self.assertEqual(self.store.audio_path(item["id"]).read_bytes(), self.raw)
        job_id, _ = self.start(item)
        self.assertEqual(self.store.get(item["id"])["start_operation_id"], operation)
        self.assertEqual(read_json(self.store.transcriptions.folder(job_id) / "state.json")["operation_id"], operation)

    def test_lost_start_ack_recovers_job_and_late_old_hook_cannot_replace_retry(self):
        item = self.capture()
        old, _ = self.start(item, lost_ack=True)
        new, _ = self.start(item, retry=True, text="new original")
        before = self.store.get(item["id"])
        memo.sync_audio_memo(self.root, item["id"], old)
        after = self.store.get(item["id"])
        self.assertNotEqual(old, new)
        self.assertEqual(after, before)

    def reserve_without_job(self, item):
        state = self.store._read(item["id"])
        state.update(status="starting", start_operation_id=uuid4().hex, start_reserved_at=time.time() - 60,
                     start_settings={"language": "ja", "mode": "standard", "hotwords": ""})
        self.store._write(state)
        return state

    def test_crashed_launch_reservation_becomes_retryable_and_reuses_operation(self):
        item = self.capture()
        reserved = self.reserve_without_job(item)
        recovered = memo.AudioMemoStore(self.root).sync(item["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertFalse(recovered["active"])
        self.start(item)
        self.assertEqual(self.store.get(item["id"])["start_operation_id"], reserved["start_operation_id"])

    def test_live_launcher_lease_is_not_misclassified_and_cancel_can_release_orphan(self):
        item = self.capture()
        self.reserve_without_job(item)
        with self.store._launch_guard(item["id"]):
            active = self.store.sync(item["id"])
            self.assertEqual(active["status"], "starting")
            with self.assertRaises(MediaError):
                self.store.cancel(item["id"])
        cancelled = self.store.cancel(item["id"])
        self.assertEqual(cancelled["status"], "interrupted")
        self.assertIsNone(cancelled["start_operation_id"])
        self.assertEqual(self.store.audio_path(item["id"]).read_bytes(), self.raw)

    def test_changed_saved_wav_is_rejected_before_recognition(self):
        item = self.capture()
        self.store.audio_path(item["id"]).write_bytes(recording(value=b"\x01\0"))
        with patch.object(self.store.transcriptions, "start", side_effect=AssertionError("must not recognize changed audio")), self.assertRaises(MediaError):
            self.store.start(item["id"])

    def test_empty_successful_retry_preserves_previous_text_and_records_empty_result(self):
        item = self.capture()
        self.start(item, text="前回の原文")
        before = self.store.sync(item["id"])
        self.start(item, text="", retry=True)
        after = self.store.sync(item["id"])
        self.assertEqual(after["status"], "done")
        self.assertTrue(after["latest_result_empty"])
        self.assertEqual(after["original_text"], "前回の原文")
        self.assertEqual(after["text"], "前回の原文")
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["original_versions"][-1]["text"], "")

    def test_global_asr_busy_and_active_memo_delete_are_rejected(self):
        item = self.capture()
        self.start(item, status="loading", text="")
        self.assertTrue(self.store.busy())
        with self.assertRaises(MediaError):
            self.store.delete(item["id"], item["revision"])
        self.assertTrue(self.store.audio_path(item["id"]).exists())

    def test_delete_requires_cas_removes_only_own_folder_and_replay_cannot_resurrect(self):
        item = self.capture()
        other = self.capture("other")
        neighbor = self.root / "unrelated.txt"
        neighbor.write_text("keep", encoding="utf-8")
        with self.assertRaises(MediaError):
            self.store.delete(item["id"], 9)
        self.assertTrue(self.store.delete(item["id"], 0))
        self.assertFalse(self.store.delete(item["id"], 0))
        self.assertEqual(neighbor.read_text(encoding="utf-8"), "keep")
        self.assertTrue(self.store.audio_path(other["id"]).exists())
        with self.assertRaises(MediaError):
            self.capture()

    def test_corrupt_metadata_remains_visible_without_new_writes(self):
        item = self.capture()
        path = self.store._folder(item["id"]) / "memo.json"
        path.write_text("{broken", encoding="utf-8")
        before = path.read_bytes()
        rows = self.store.history()
        self.assertEqual(rows[0]["status"], "unavailable")
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(MediaError):
            self.store.get(item["id"])

    def test_symlinked_audio_is_never_followed_or_deleted(self):
        item = self.capture()
        audio = self.store.audio_path(item["id"])
        outside = self.root / "outside.wav"
        outside.write_bytes(self.raw)
        audio.unlink()
        try:
            audio.symlink_to(outside)
        except OSError:
            self.skipTest("OS permission does not allow creating a test symlink")
        with self.assertRaises(MediaError):
            self.store.get(item["id"])
        with self.assertRaises(MediaError):
            self.store.delete(item["id"], 0)
        self.assertEqual(outside.read_bytes(), self.raw)


if __name__ == "__main__":
    unittest.main()
