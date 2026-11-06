from pathlib import Path
import base64
import json
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app_core import ocr_service as ocr

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class OCRStoreTests(unittest.TestCase):
    def test_transient_read_sharing_violation_is_retried(self):
        with patch.object(
            ocr,
            "_shared_read_json",
            side_effect=[PermissionError("busy"), {"ok": True}],
        ) as read:
            self.assertEqual(ocr.read_json("unused"), {"ok": True})
            self.assertEqual(read.call_count, 2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pgocr_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ocr.OCRStore(self.root)
        self.job = "a" * 32

    def seed(self, status="done", created=None):
        folder = self.store.folder(self.job)
        folder.mkdir(parents=True)
        ocr.atomic_json(
            folder / "request.json",
            {
                "pages": [1, 2],
                "source_name": "教材.pdf",
                "source_sha256": "b" * 64,
                "strategy": "auto",
                "languages": ["ja", "en"],
                "transforms": {"1": ocr.transform(), "2": ocr.transform()},
            },
        )
        ocr.atomic_json(
            folder / "state.json",
            {
                "id": self.job,
                "name": "教材.pdf",
                "status": status,
                "created": created or time.time(),
                "completed_pages": [1],
                "progress": 0.5,
            },
        )
        ocr.atomic_json(
            folder / "page-1.json",
            {
                "number": 1,
                "method": "ocr",
                "width": 100,
                "height": 100,
                "transform": ocr.transform(),
                "blocks": [
                    {
                        "id": "p1b0",
                        "original_text": "元の文章 Original",
                        "box": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "score": 0.7,
                    }
                ],
            },
        )
        (folder / "page-1.png").write_bytes(PNG)
        return folder

    def source(self):
        path = self.root / "sample.png"
        path.write_bytes(PNG)
        return ocr.OCRSource(
            "c" * 32,
            path,
            "sample.png",
            "local",
            ocr._identity(path),
            2,
            "image",
            self.root,
        )

    def mock_start(self):
        one = patch.object(ocr, "engine_available", return_value=True)
        one.start()
        self.addCleanup(one.stop)
        two = patch.object(ocr.subprocess, "Popen")
        process = two.start()
        self.addCleanup(two.stop)
        return process

    def test_opening_is_read_only(self):
        self.assertEqual(self.store.history(), [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_parse_pages_sorts_deduplicates_and_supports_japanese_separators(self):
        self.assertEqual(ocr.parse_pages("5、1〜3,2", 8), [1, 2, 3, 5])
        self.assertEqual(ocr.parse_pages("", 3), [1, 2, 3])

    def test_invalid_or_excessive_page_ranges_rejected(self):
        for value in ("0", "4-2", "1-99999", "x", "1,", "101", "1-100,101"):
            with self.subTest(value=value), self.assertRaises(ocr.OCRError):
                ocr.parse_pages(value, 100)

    def test_invalid_crop_rotation_and_nonfinite_numbers_rejected(self):
        for value in (
            {"rotation": 45},
            {"rotation": True},
            {"crop": [0, 0, 0, 1]},
            {"crop": [0, 0, 1.1, 1]},
            {"crop": [0, float("nan"), 1, 1]},
            {"crop": [0, 0, 0.001, 1]},
        ):
            with self.subTest(value=value), self.assertRaises(ocr.OCRError):
                ocr.transform(value)

    def test_history_and_committed_pages_survive_store_restart(self):
        self.seed("cancelled")
        result = ocr.OCRStore(self.root).result(self.job)
        self.assertEqual([p["number"] for p in result["pages"]], [1])
        self.assertEqual(result["requested_pages"], [1, 2])

    def test_edit_preserves_original_box_and_score(self):
        self.seed()
        before = self.store.result(self.job)["pages"][0]["blocks"][0]
        self.store.save_edits(self.job, 0, {"p1b0": "修正した文章"})
        after = self.store.result(self.job)["pages"][0]["blocks"][0]
        self.assertEqual(after["text"], "修正した文章")
        for key in ("original_text", "box", "score"):
            self.assertEqual(after[key], before[key])

    def test_edit_conflict_keeps_saved_text(self):
        self.seed()
        self.store.save_edits(self.job, 0, {"p1b0": "first"})
        with self.assertRaises(ocr.OCRError):
            self.store.save_edits(self.job, 0, {"p1b0": "stale"})
        self.assertEqual(
            self.store.result(self.job)["pages"][0]["blocks"][0]["text"], "first"
        )

    def test_invalid_edits_are_not_written(self):
        folder = self.seed()
        for values in (
            {"unknown": "x"},
            {"p1b0": None},
            {"p1b0": "x" * 20001},
            {"p1b0": "x\0"},
        ):
            with self.subTest(values=str(values)[:30]), self.assertRaises(ocr.OCRError):
                self.store.save_edits(self.job, 0, values)
        self.assertFalse((folder / "edits.json").exists())

    def test_active_job_cannot_be_edited_or_deleted(self):
        self.seed("reading")
        with self.assertRaises(ocr.OCRError):
            self.store.save_edits(self.job, 0, {"p1b0": "x"})
        with self.assertRaises(ocr.OCRError):
            self.store.delete(self.job)

    def test_cancel_flag_is_persistent(self):
        folder = self.seed("reading")
        self.store.cancel(self.job)
        self.assertTrue((folder / "cancel").exists())

    def test_orphan_worker_does_not_hide_completed_pages(self):
        self.seed("reading", time.time() - 100)
        self.assertEqual(self.store.state(self.job)["status"], "interrupted")
        self.assertEqual(len(self.store.result(self.job)["pages"]), 1)

    def test_live_worker_lock_keeps_job_active(self):
        folder = self.seed("reading", time.time() - 100)
        with ocr.file_lock(folder / ".worker.lock"):
            self.assertEqual(self.store.state(self.job)["status"], "reading")

    def test_three_formats_use_saved_edits_json_keeps_raw_text(self):
        self.seed()
        self.store.save_edits(self.job, 0, {"p1b0": "Corrected 日本語"})
        result = self.store.result(self.job)
        for kind in ("txt", "md", "json"):
            self.assertIn(
                "Corrected 日本語", ocr.export_result(result, kind).decode("utf-8-sig")
            )
        parsed = json.loads(ocr.export_result(result, "json"))
        self.assertEqual(
            parsed["pages"][0]["blocks"][0]["original_text"], "元の文章 Original"
        )
        self.assertTrue(ocr.export_result(result, "md").startswith(b"## 1"))

    def test_unknown_format_and_path_traversal_rejected(self):
        self.seed()
        with self.assertRaises(ocr.OCRError):
            ocr.export_result(self.store.result(self.job), "pdf")
        for key in ("../outside", "", "A" * 32, None):
            with self.subTest(key=key), self.assertRaises(ocr.OCRError):
                self.store.folder(key)

    def test_delete_never_touches_original(self):
        source = self.source()
        folder = self.seed()
        self.store.delete(self.job)
        self.assertFalse(folder.exists())
        self.assertEqual(source.path.read_bytes(), PNG)

    def test_only_committed_pages_are_returned(self):
        folder = self.seed("cancelled")
        (folder / "page-2.png").write_bytes(PNG)
        (folder / "page-2.json.uncommitted").write_text("{")
        self.assertEqual(len(self.store.result(self.job)["pages"]), 1)

    def test_job_image_does_not_accept_other_page(self):
        self.seed()
        self.assertEqual(self.store.image(self.job, 1), PNG)
        with self.assertRaises(ocr.OCRError):
            self.store.image(self.job, 999)

    def test_same_operation_is_idempotent_and_other_job_is_blocked(self):
        process = self.mock_start()
        source = self.source()
        first = self.store.start(source, [1], operation_id="d" * 32)
        second = self.store.start(source, [1], operation_id="d" * 32)
        self.assertEqual(first, second)
        self.assertEqual(process.call_count, 1)
        with self.assertRaises(ocr.OCRError):
            self.store.start(source, [2])

    def test_changed_source_cannot_start(self):
        process = self.mock_start()
        source = self.source()
        source.path.write_bytes(b"changed")
        with self.assertRaises(ocr.OCRError):
            self.store.start(source, [1])
        process.assert_not_called()

    def test_source_copy_is_independent_and_identified_by_hash(self):
        self.mock_start()
        source = self.source()
        job = self.store.start(source, [1])
        source.path.unlink()
        request = ocr.read_json(self.store.folder(job) / "request.json")
        self.assertEqual(Path(request["path"]).read_bytes(), PNG)
        self.assertEqual(len(request["source_sha256"]), 64)

    def test_worker_launch_failure_leaves_no_broken_job(self):
        process = self.mock_start()
        process.side_effect = OSError("simulated launch error")
        with self.assertRaises(ocr.OCRError):
            self.store.start(self.source(), [1])
        self.assertEqual(self.store.history(), [])

    def test_disk_space_check_prevents_launch(self):
        process = self.mock_start()
        with patch.object(
            ocr.shutil, "disk_usage", return_value=SimpleNamespace(free=0)
        ):
            with self.assertRaises(ocr.OCRError):
                self.store.start(self.source(), [1])
        process.assert_not_called()

    def test_atomic_edit_failure_preserves_revision(self):
        self.seed()
        with patch.object(ocr, "atomic_json", side_effect=OSError("disk error")):
            with self.assertRaises(ocr.OCRError):
                self.store.save_edits(self.job, 0, {"p1b0": "x"})
        self.assertEqual(self.store.result(self.job)["revision"], 0)


if __name__ == "__main__":
    unittest.main()
