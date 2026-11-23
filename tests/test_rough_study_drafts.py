from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import rough_study_drafts as drafts
from app_core.json_store import JsonReadError


class RoughStudyDraftTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "rough_study_logs" / "drafts.json"

    def test_read_and_discard_empty_do_not_create_storage(self):
        self.assertIsNone(drafts.load_rough_draft(self.root, "week:2026-11-23"))
        drafts.discard_rough_draft(self.root, "period")
        self.assertFalse(self.path.parent.exists())

    def test_round_trip_and_discard_preserve_distinct_drafts(self):
        week = "week:2026-11-23"
        next_week = "week:2026-11-30"
        batch_id = uuid4()
        payload = {
            "rows": [{"study_date": "2026-11-23", "memo": "復習", "duration": None}],
            "operation_id": uuid4().hex,
            "confirmed": False,
        }
        drafts.save_rough_draft(self.root, week, payload)
        drafts.save_rough_draft(self.root, next_week, {"rows": []})
        drafts.save_rough_draft(self.root, "period", {"start": "2026-11-23"})
        drafts.save_rough_draft(self.root, f"edit:{batch_id}", {"revision": 3})
        self.assertEqual(drafts.load_rough_draft(self.root, week), payload)
        self.assertEqual(drafts.load_rough_draft(self.root, f"edit:{batch_id.hex}"), {"revision": 3})
        drafts.discard_rough_draft(self.root, week)
        self.assertIsNone(drafts.load_rough_draft(self.root, week))
        self.assertEqual(drafts.load_rough_draft(self.root, next_week), {"rows": []})
        self.assertEqual(drafts.load_rough_draft(self.root, "period"), {"start": "2026-11-23"})
        self.assertFalse((self.path.parent / "batches.json").exists())

    def test_same_data_does_not_rewrite_and_edits_do_not_create_backups(self):
        drafts.save_rough_draft(self.root, "period", {"duration": "1:30", "memo": "学習"})
        with patch.object(drafts, "_replace_draft_file") as write:
            drafts.save_rough_draft(self.root, "period", {"memo": "学習", "duration": "1:30"})
            drafts.discard_rough_draft(self.root, "week:2026-11-23")
            write.assert_not_called()
        drafts.save_rough_draft(self.root, "period", {"duration": "1:45"})
        self.assertEqual(drafts.load_rough_draft(self.root, "period"), {"duration": "1:45"})
        self.assertFalse((self.path.parent / "backups").exists())
        self.assertFalse((self.root / "backups").exists())

    def test_concurrent_saves_do_not_overwrite_other_keys(self):
        keys = [f"edit:{uuid4().hex}" for _ in range(12)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda key: drafts.save_rough_draft(self.root, key, {"id": key}), keys))
        for key in keys:
            self.assertEqual(drafts.load_rough_draft(self.root, key), {"id": key})

    def test_reads_wait_for_an_in_progress_draft_replacement(self):
        drafts.save_rough_draft(self.root, "period", {"duration": 45})
        replace = drafts._replace_draft_file
        writing = Event()
        reading = Event()
        finish = Event()

        def pause_replace(path, data):
            writing.set()
            if not finish.wait(timeout=5):
                raise TimeoutError("draft replacement was not released")
            replace(path, data)

        def read():
            reading.set()
            return drafts.load_rough_draft(self.root, "period")

        with patch.object(drafts, "_replace_draft_file", side_effect=pause_replace):
            with ThreadPoolExecutor(max_workers=2) as pool:
                writer = pool.submit(drafts.save_rough_draft, self.root, "period", {"duration": 90})
                try:
                    self.assertTrue(writing.wait(timeout=5))
                    reader = pool.submit(read)
                    self.assertTrue(reading.wait(timeout=5))
                    with self.assertRaises(TimeoutError):
                        reader.result(timeout=0.1)
                finally:
                    finish.set()
                writer.result(timeout=5)
                self.assertEqual(reader.result(timeout=5), {"duration": 90})

    def test_invalid_input_does_not_create_a_file(self):
        for key, payload in [
            ("unrecognized", {}),
            ("week:2026-11-31", {}),
            ("edit:invalid", {}),
            ("period", {"duration": float("nan")}),
        ]:
            with self.subTest(key=key, payload=payload):
                with self.assertRaises(ValueError):
                    drafts.save_rough_draft(self.root, key, payload)
        self.assertFalse(self.path.parent.exists())

    def test_corrupt_or_unsupported_file_is_not_replaced(self):
        self.path.parent.mkdir()
        for document in (
            b"{broken",
            b'{"schema_version":2,"drafts":{}}',
            b'{"schema_version":1,"drafts":{"period":1}}',
        ):
            with self.subTest(document=document):
                self.path.write_bytes(document)
                with self.assertRaises(JsonReadError):
                    drafts.load_rough_draft(self.root, "period")
                with self.assertRaises(JsonReadError):
                    drafts.save_rough_draft(self.root, "period", {})
                with self.assertRaises(JsonReadError):
                    drafts.discard_rough_draft(self.root, "period")
                self.assertEqual(self.path.read_bytes(), document)

    def test_temporary_windows_file_contention_is_retried(self):
        drafts.save_rough_draft(self.root, "period", {"duration": 90})
        replace = drafts.os.replace
        denied = PermissionError("temporary access denial")
        denied.winerror = 5
        attempted = []

        def busy_once(source, target):
            attempted.append(target)
            if len(attempted) == 1:
                raise denied
            replace(source, target)

        with patch.object(drafts.os, "replace", side_effect=busy_once):
            drafts.save_rough_draft(self.root, "period", {"duration": 45})
        self.assertEqual(len(attempted), 2)
        self.assertEqual(drafts.load_rough_draft(self.root, "period"), {"duration": 45})

    def test_failed_replace_leaves_saved_draft_and_cleans_its_temp_file(self):
        drafts.save_rough_draft(self.root, "period", {"duration": 90})
        original = self.path.read_bytes()
        with patch.object(drafts.os, "replace", side_effect=OSError("forced failure")):
            with self.assertRaises(OSError):
                drafts.save_rough_draft(self.root, "period", {"duration": 45})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".rough-draft-*.tmp")), [])

    def test_resolved_alias_is_rejected(self):
        resolve = drafts.resolved_json_path

        def redirected(path):
            result = resolve(path)
            if result == resolve(self.path):
                return result.with_name("batches.json")
            return result

        with patch.object(drafts, "resolved_json_path", side_effect=redirected):
            with self.assertRaises(ValueError):
                drafts.save_rough_draft(self.root, "period", {})
        self.assertFalse(self.path.parent.exists())


if __name__ == "__main__":
    unittest.main()
