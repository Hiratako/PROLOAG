from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch

from app_core import json_store as store


class JsonStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proloag_json_test_")
        self.root = Path(self.temp.name)
        self.path = self.root / "record.json"

    def tearDown(self):
        self.temp.cleanup()

    def backups(self):
        return [p for p in (self.root / "backups").rglob("*.json") if p.is_file()]

    def test_record_enumeration_excludes_backup_artifacts_and_directories(self):
        folder = self.root / "BACKUPS" / "live_records"
        names = ["2026/08/28.json", "2026/08/29.json", "2026/BackUp/old.json",
                 "2026/BACKUPS/auto_save/28.json/generation.json", "2026/day_BACKUP_old.json"]
        for name in names:
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}")
        (folder / "directory.json").mkdir()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in folder.rglob("*") if p.is_file()}
        expected = [p for p in folder.rglob("*.json") if p.name in {"28.json", "29.json"} and p.is_file()]
        self.assertEqual(store.record_json_files(folder), expected)
        self.assertEqual(store.record_json_files(self.root / "missing"), [])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns)
                                 for p in folder.rglob("*") if p.is_file()})

    def test_missing_and_bom_reads_do_not_write(self):
        self.assertEqual(store.load_json_safe(self.path, []), [])
        self.assertEqual(list(self.root.iterdir()), [])
        raw = b'\xef\xbb\xbf{"value": 1}'
        self.path.write_bytes(raw)
        self.assertEqual(store.load_json_safe(self.path, {}), {"value": 1})
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_invalid_files_are_not_read_or_overwritten(self):
        for raw in (b'{', b'\xff', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(store.JsonReadError):
                    store.load_json_safe(self.path, {})
                with self.assertRaises(store.JsonReadError):
                    store.save_json_safe(self.path, {"ok": True})
                self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse(self.backups())

    def test_access_error_is_not_missing(self):
        with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            with self.assertRaises(store.JsonReadError):
                store.load_json_safe(self.path, {})

    def test_serialization_and_replace_failure_preserve_original(self):
        store.save_json_safe(self.path, {"x": 1})
        original = self.path.read_bytes()
        for invalid in ({"x": float("nan")}, {"x": object()}, {1: "a", "1": "b"}):
            with self.assertRaises((ValueError, TypeError)):
                store.save_json_safe(self.path, invalid)
            self.assertEqual(self.path.read_bytes(), original)
        original_replace = store.os.replace
        def fail_target_replace(source, destination):
            if Path(destination) == self.path:
                raise OSError("replace failed")
            return original_replace(source, destination)
        with patch.object(store.os, "replace", side_effect=fail_target_replace):
            with self.assertRaises(OSError):
                store.save_json_safe(self.path, {"x": 2})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_fsync_failure_preserves_original(self):
        store.save_json_safe(self.path, {"x": 1})
        original = self.path.read_bytes()
        with patch.object(store.os, "fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                store.save_json_safe(self.path, {"x": 2})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.root.glob("*.tmp")))
        self.assertFalse(self.backups())

    def test_process_lock_timeout(self):
        code = (
            "import sys\nfrom pathlib import Path\n"
            "from app_core.json_store import json_transaction, JsonLockTimeoutError\n"
            "try:\n with json_transaction(Path(sys.argv[1]), timeout=0.1): pass\n"
            "except JsonLockTimeoutError:\n sys.exit(0)\n"
            "sys.exit(2)\n"
        )
        with store.json_transaction(self.path):
            result = subprocess.run([sys.executable, "-B", "-c", code, str(self.path)],
                                    capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_backup_and_equal_save(self):
        store.save_json_safe(self.path, {"x": 1})
        original = self.path.read_bytes()
        store.save_json_safe(self.path, {"x": 2})
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_bytes(), original)
        before = self.path.stat().st_mtime_ns
        store.save_json_safe(self.path, {"x": 2})
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.assertTrue(all(p.is_relative_to(self.root) for p in self.backups()))

    def test_two_thread_stale_read_conflicts(self):
        store.save_json_safe(self.path, {"x": 0})
        barrier = threading.Barrier(2)
        results = []

        def worker(value):
            store.load_json_safe(self.path, {})
            barrier.wait(timeout=5)
            try:
                store.save_json_safe(self.path, {"x": value})
                results.append("saved")
            except store.JsonConflictError:
                results.append("conflict")

        threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ["saved", "conflict"])

    def test_transaction_decorator_and_nested_save(self):
        store.save_json_safe(self.path, {"x": 0})

        @store.json_transaction_for(lambda path: path)
        def increment(path):
            with store.json_transaction(path):
                value = store.load_json_safe(path, {})
                value["x"] += 1
                store.save_json_safe(path, value)

        threads = [threading.Thread(target=increment, args=(self.path,)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(store.load_json_safe(self.path, {}), {"x": 4})

    def test_explicit_revision_and_missing_observation(self):
        self.assertIsNone(store.load_json_safe(self.path, None))
        self.path.write_text('{"x":1}', encoding="utf-8")
        with self.assertRaises(store.JsonConflictError):
            store.save_json_safe(self.path, {"x": 2})
        with self.assertRaises(store.JsonConflictError):
            store.save_json_safe(self.path, {"x": 2}, expected_revision=None)
        store.load_json_safe(self.path, {})
        store.save_json_safe(self.path, {"x": 2})
        self.assertEqual(json.loads(self.path.read_text()), {"x": 2})


if __name__ == "__main__":
    unittest.main()
