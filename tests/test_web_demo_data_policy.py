from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import hashlib

import config
from app_core import json_operations, json_store
from web_demo.data import (
    DATA_ROOT, PERIOD_START, REFERENCE_DATE, data_fingerprint,
    demo_snapshot, demo_summary, verify_demo_data,
)
from web_demo import data as demo_data


class ExhibitionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.existing = self.root / "existing.json"
        self.existing.write_text('{"value":1}', encoding="utf-8")
        self.before = self.inventory()

    def tearDown(self):
        self.directory.cleanup()

    def inventory(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def assertDeniedWithoutFiles(self, action):
        with self.assertRaises(json_store.JsonReadOnlyError):
            action()
        self.assertEqual(self.before, self.inventory())
        self.assertFalse((self.root / "backups").exists())

    def test_write_existing_and_new_rejected(self):
        for path in (self.existing, self.root / "new" / "value.json"):
            self.assertDeniedWithoutFiles(lambda: json_store.save_json_safe(path, {"value": 2}))

    def test_lock_rejected_before_creating_lock(self):
        def action():
            with json_store.json_transaction(self.existing):
                self.fail("A write transaction was entered")
        self.assertDeniedWithoutFiles(action)

    def test_internal_lock_rejected(self):
        def action():
            with json_store._file_transaction(self.existing):
                self.fail("A direct write transaction was entered")
        self.assertDeniedWithoutFiles(action)

    def test_backup_and_generation_rejected(self):
        self.assertDeniedWithoutFiles(lambda: json_store.backup_json(self.existing))
        self.assertDeniedWithoutFiles(lambda: json_store._save_generation(self.existing, b"{}"))

    def test_existing_directory_allowed_without_write(self):
        self.assertEqual(json_store.ensure_dir(self.root), self.root)
        self.assertEqual(self.before, self.inventory())

    def test_directory_creation_rejected(self):
        self.assertDeniedWithoutFiles(lambda: json_store.ensure_dir(self.root / "new"))
        self.assertFalse((self.root / "new").exists())

    def test_atomic_operation_and_rollback_rejected_without_journal(self):
        def action():
            with json_operations.atomic_json_operation(root=self.root, kind="test"):
                self.fail("A write operation was entered")
        self.assertDeniedWithoutFiles(action)
        self.assertDeniedWithoutFiles(lambda: json_operations.rollback_json_operation("a" * 32, self.root))

    def test_direct_operation_mutators_rejected(self):
        op = json_operations.JsonOperation(self.root, "test", "a" * 32)
        self.assertDeniedWithoutFiles(lambda: op.stage(self.existing, {"value": 2}))
        self.assertDeniedWithoutFiles(op.commit)
        self.assertDeniedWithoutFiles(lambda: json_operations._rollback(self.root, []))

    def test_missing_policy_fails_closed(self):
        existing = getattr(config, "WEB_DEMO_READ_ONLY", None)
        if hasattr(config, "WEB_DEMO_READ_ONLY"):
            del config.WEB_DEMO_READ_ONLY
        try:
            self.assertDeniedWithoutFiles(lambda: json_store.save_json_safe(self.existing, {}))
        finally:
            if existing is not None:
                config.WEB_DEMO_READ_ONLY = existing

    def test_environment_does_not_disable_policy(self):
        with patch.dict(os.environ, {"WEB_DEMO_READ_ONLY": "false", "PROLOAG_DEMO": "0"}):
            self.assertDeniedWithoutFiles(lambda: json_store.save_json_safe(self.existing, {}))

    def test_service_mutators_are_stopped(self):
        from app_core.note_service import add_note
        from app_core.roadmap_service import save_entry
        self.assertDeniedWithoutFiles(lambda: add_note("new", root=self.root))
        self.assertDeniedWithoutFiles(lambda: save_entry(
            self.root, "items", {"title": "New item"}, expected_revision=None, operation_id="b" * 32))
        self.assertDeniedWithoutFiles(lambda: json_store.save_json_safe(self.root / "master_data/master.json", {}))


class ExhibitionDataTests(unittest.TestCase):
    def test_non_object_manifest_is_rejected_before_reading_records(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            for text in ("[]", "null", '"invalid"', "42", "true"):
                with self.subTest(text=text):
                    manifest.write_text(text, encoding="utf-8")
                    with patch.object(demo_data, "DATA_MANIFEST", manifest), patch.object(demo_data, "data_fingerprint") as fingerprint:
                        with self.assertRaisesRegex(ValueError, "manifest must be an object"):
                            demo_data.verify_demo_dataset()
                        fingerprint.assert_not_called()

    def test_manifest_and_fixed_date(self):
        result = verify_demo_data()
        self.assertEqual(result["records"], 715)
        self.assertEqual(REFERENCE_DATE - PERIOD_START, timedelta(days=179))
        from app_core.app_clock import app_today
        self.assertEqual(app_today(), REFERENCE_DATE)

    def test_summary_has_real_synthetic_records(self):
        result = demo_summary()
        self.assertEqual(result["days"], 180)
        self.assertGreater(result["study_hours"], 0)
        self.assertGreater(result["sessions"], 100)
        self.assertGreater(result["notes"], 0)
        self.assertGreater(result["cards"], 0)
        self.assertEqual(result["subjects"], 12)

    def test_independent_snapshots_for_concurrent_visitors(self):
        before = data_fingerprint()
        def visitor(index):
            snapshot = demo_snapshot()
            snapshot["sessions"][0]["memo"] = str(index)
            snapshot["master"]["subjects"].clear()
            return snapshot
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(visitor, range(8)))
        self.assertEqual(len({x["sessions"][0]["memo"] for x in results}), 8)
        self.assertTrue(demo_snapshot()["master"]["subjects"])
        self.assertEqual(before, data_fingerprint())

    def test_manifest_rejects_traversal_unknown_folders_and_extensions(self):
        for name in ("../escape.json", "/escape.json", "settings/../escape.json", "settings\\escape.json",
                     "settings/file.txt", "private/value.json", "C:/escape.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    demo_data._checked_records(Path(tmp), {"record_sha256": {name: "a" * 64}})

    def test_unknown_json_and_non_json_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "settings"
            folder.mkdir()
            valid = folder / "demo_mode.json"
            valid.write_bytes(b"{}")
            manifest = {"record_sha256": {"settings/demo_mode.json": hashlib.sha256(b"{}").hexdigest()}}
            self.assertEqual(set(demo_data._checked_records(root, manifest)), {"settings/demo_mode.json"})
            for name in ("unknown.json", "unexpected.txt"):
                extra = folder / name
                extra.write_bytes(b"{}")
                with self.assertRaises(ValueError):
                    demo_data._checked_records(root, manifest)
                extra.unlink()

    def test_readers_do_not_change_records(self):
        from app_core.roadmap_service import load_roadmap, build_actuals
        from app_core.study_log_service import load_all_study_sessions
        from app_core.note_service import load_all_notes
        from data_utils import load_master_data
        before = data_fingerprint()
        roadmap = load_roadmap(DATA_ROOT)
        self.assertEqual(len(roadmap["items"]), 9)
        self.assertTrue(build_actuals(DATA_ROOT, roadmap))
        self.assertTrue(load_all_study_sessions(DATA_ROOT))
        self.assertTrue(load_all_notes(DATA_ROOT))
        self.assertTrue(load_master_data(DATA_ROOT)["subjects"])
        self.assertEqual(before, data_fingerprint())


if __name__ == "__main__":
    unittest.main()
