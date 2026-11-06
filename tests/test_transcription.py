from pathlib import Path
import json
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_core import transcription as tr
from app_core.transcription_worker import normalized_segments
from app_core.media_conversion import MediaError, Source, Track, _identity


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pgasr_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = tr.TranscriptionStore(self.root)
        self.job_id = "a" * 32

    def seed(self, status="done", created=None):
        folder = self.store.folder(self.job_id)
        folder.mkdir(parents=True)
        tr.atomic_json(
            folder / "state.json",
            {
                "id": self.job_id,
                "status": status,
                "name": "日英.mp3",
                "created": created or time.time(),
                "progress": 1.0,
            },
        )
        tr.atomic_json(
            folder / "request.json",
            {
                "language": "ja",
                "model": "small",
                "duration": 5.0,
                "track": {"index": 0},
            },
        )
        rows = [
            {"id": 0, "start": 0.1, "end": 1.5, "original_text": "原文です。"},
            {"id": 1, "start": 2.0, "end": 4.5, "original_text": "Hello world."},
        ]
        (folder / "segments.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
        return folder

    def test_empty_open_creates_nothing(self):
        self.assertEqual(self.store.history(), [])
        self.assertFalse(self.store.base.exists())

    def test_restarted_store_preserves_history(self):
        self.seed()
        result = tr.TranscriptionStore(self.root).result(self.job_id)
        self.assertEqual(len(result["segments"]), 2)

    def test_corrections_preserve_original_and_timestamps(self):
        self.seed()
        before = self.store.result(self.job_id)
        self.assertEqual(self.store.save_edits(self.job_id, 0, {0: "修正済み。"}), 1)
        after = self.store.result(self.job_id)
        self.assertEqual(after["segments"][0]["text"], "修正済み。")
        self.assertEqual(after["segments"][0]["original_text"], "原文です。")
        self.assertEqual(after["segments"][0]["start"], before["segments"][0]["start"])

    def test_concurrent_edit_rejected_without_losing_saved_text(self):
        self.seed()
        self.store.save_edits(self.job_id, 0, {0: "first"})
        with self.assertRaises(MediaError):
            self.store.save_edits(self.job_id, 0, {0: "stale"})
        self.assertEqual(self.store.result(self.job_id)["segments"][0]["text"], "first")

    def test_partial_last_line_preserves_all_flushed_segments(self):
        folder = self.seed("cancelled")
        with (folder / "segments.jsonl").open("a", encoding="utf-8") as output:
            output.write('{"id": 2')
        self.assertEqual(len(self.store.result(self.job_id)["segments"]), 2)

    def test_orphan_worker_becomes_interrupted(self):
        self.seed("transcribing", time.time() - 90)
        self.assertEqual(self.store.history()[0]["status"], "interrupted")

    def test_held_worker_lock_remains_active(self):
        folder = self.seed("transcribing", time.time() - 90)
        with tr.file_lock(folder / ".worker.lock"):
            self.assertEqual(self.store.history()[0]["status"], "transcribing")

    def test_cancel_is_durable_flag(self):
        folder = self.seed("transcribing")
        self.store.cancel(self.job_id)
        self.assertTrue((folder / "cancel").is_file())

    def test_cannot_edit_running_job(self):
        self.seed("transcribing")
        with self.assertRaises(MediaError):
            self.store.save_edits(self.job_id, 0, {0: "x"})

    def test_cannot_delete_running_job(self):
        folder = self.seed("loading")
        with self.assertRaises(MediaError):
            self.store.delete(self.job_id)
        self.assertTrue(folder.is_dir())

    def test_delete_only_owned_job_not_original(self):
        original = self.root / "original.mp3"
        original.write_bytes(b"original")
        folder = self.seed()
        self.store.delete(self.job_id)
        self.assertFalse(folder.exists())
        self.assertEqual(original.read_bytes(), b"original")

    def test_path_traversal_rejected(self):
        for value in ("../outside", "x" * 32, "A" * 32, "", None):
            with self.subTest(value=value), self.assertRaises(MediaError):
                self.store.folder(value)

    def test_unknown_segment_or_invalid_text_is_rejected(self):
        self.seed()
        for edit in ({5: "x"}, {0: None}, {0: "a" * 20001}, {0: "x\0y"}):
            with self.subTest(edit=str(edit)[:30]), self.assertRaises(MediaError):
                self.store.save_edits(self.job_id, 0, edit)

    def test_exports_use_corrected_text_json_keeps_original(self):
        self.seed()
        self.store.save_edits(self.job_id, 0, {0: "日本語の修正"})
        result = self.store.result(self.job_id)
        for kind in ("txt", "srt", "vtt", "json"):
            self.assertIn(
                "日本語の修正", tr.export_result(result, kind).decode("utf-8-sig")
            )
        original = json.loads(tr.export_result(result, "json"))
        self.assertEqual(original["segments"][0]["original_text"], "原文です。")
        self.assertIn(
            "00:00:00,100 --> 00:00:01,500", tr.export_result(result, "srt").decode()
        )
        self.assertTrue(tr.export_result(result, "vtt").startswith(b"WEBVTT\n\n"))

    def test_caption_text_cannot_add_fake_cues(self):
        self.seed()
        self.store.save_edits(self.job_id, 0, {0: "hello\n\n1 --> 5"})
        value = tr.export_result(self.store.result(self.job_id), "srt").decode()
        self.assertEqual(value.count(" --> "), 2)

    def test_atomic_failure_preserves_previous_file(self):
        folder = self.seed()
        with patch.object(
            tr.os, "replace", side_effect=OSError("simulated disk error")
        ):
            with self.assertRaises(OSError):
                tr.atomic_json(folder / "request.json", {"bad": "x"})
        self.assertEqual(tr.read_json(folder / "request.json")["language"], "ja")
        self.assertFalse(list(folder.glob("*.tmp")))

    def source(self):
        path = self.root / "test.mp3"
        path.write_bytes(b"mock source")
        return Source(
            "b" * 32,
            path,
            path.name,
            "local",
            _identity(path),
            10.0,
            (Track(0, "mp3", "ja", "", 1, 10.0),),
        )

    def start_patches(self):
        self.patchers = [
            patch.object(tr, "engine_available", return_value=True),
            patch.object(
                tr,
                "discover_tools",
                return_value=SimpleNamespace(ffmpeg=Path("ffmpeg.exe")),
            ),
            patch.object(tr.subprocess, "Popen"),
        ]
        values = [p.start() for p in self.patchers]
        for p in self.patchers:
            self.addCleanup(p.stop)
        return values[-1]

    def test_duplicate_start_idempotent_and_second_job_blocked(self):
        process = self.start_patches()
        source = self.source()
        one = self.store.start(source, 0, "ja", "standard", operation_id="c" * 32)
        two = self.store.start(source, 0, "ja", "standard", operation_id="c" * 32)
        self.assertEqual(one, two)
        self.assertEqual(process.call_count, 1)
        with self.assertRaises(MediaError):
            self.store.start(source, 0, "en", "standard")

    def test_modified_source_rejected_before_spawn(self):
        process = self.start_patches()
        source = self.source()
        source.path.write_bytes(b"changed")
        with self.assertRaises(MediaError):
            self.store.start(source, 0, "ja", "standard")
        process.assert_not_called()

    def test_worker_spawn_failure_leaves_no_broken_job(self):
        process = self.start_patches()
        process.side_effect = OSError("no process")
        with self.assertRaises(MediaError):
            self.store.start(self.source(), 0, "ja", "standard")
        self.assertEqual(self.store.history(), [])

    def test_overlap_keeps_boundary_segments_once_in_original_time(self):
        rows = [
            SimpleNamespace(start=297.0, end=300.0, text="left"),
            SimpleNamespace(start=300.0, end=303.0, text="right"),
        ]
        first = list(normalized_segments(rows, 0, 0, 300, 600, 0))
        shifted = [
            SimpleNamespace(start=-1.0, end=2.0, text="left"),
            SimpleNamespace(start=2.0, end=5.0, text="right"),
        ]
        second = list(normalized_segments(shifted, 298, 300, 600, 600, len(first)))
        self.assertEqual(
            [r["original_text"] for r in first + second], ["left", "right"]
        )
        self.assertEqual(second[0]["start"], 300.0)


if __name__ == "__main__":
    unittest.main()
