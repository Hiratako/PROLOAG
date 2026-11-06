from __future__ import annotations

from contextvars import Context
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_core import json_store as store
from app_core.edit_snapshot import capture_edit_snapshot, clear_edit_snapshot
from app_core.json_operations import (
    _relative, atomic_json_operation, pending_json_operations, rollback_json_operation,
)


class SimulatedCrash(BaseException):
    pass


class JsonOperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_atomic_test_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        root_patch = patch.object(store, "PROJECT_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.a = self.root / "diary_entries/2026/09/20260916.json"
        self.b = self.root / "health_logs/2026/09/20260916.json"
        self.write(self.a, {"value": "before", "unknown": [1, 2]})
        self.write(self.b, {"value": "before_b"})
        self.operation_id = "a" * 32

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def operation(self, **options):
        return atomic_json_operation(root=self.root, kind="test_save", operation_id=self.operation_id, **options)

    def test_stage_reads_own_writes_without_touching_live_files(self):
        before = self.a.read_bytes()
        with self.operation() as operation:
            value = store.load_json_safe(self.a, {})
            value["value"] = "after"
            store.save_json_safe(self.a, value)
            self.assertEqual(self.a.read_bytes(), before)
            self.assertEqual(store.load_json_safe(self.a, {})["value"], "after")
            store.save_json_safe(self.b, {"value": "after_b"})
        self.assertEqual(store.load_json_safe(self.a, {})["unknown"], [1, 2])
        self.assertEqual(len(operation.changed_files), 2)
        self.assertEqual(pending_json_operations(self.root), [])

    def test_committed_retry_requires_same_request_fingerprint(self):
        with self.operation(request_fingerprint="1" * 64):
            store.save_json_safe(self.a, {"value": "saved"})
        before = {path: path.read_bytes() for path in self.root.rglob("*.json") if path.is_file()}
        with self.operation(request_fingerprint="1" * 64) as operation:
            self.assertTrue(operation.already_committed)
        for fingerprint in ("2" * 64, None):
            with self.subTest(fingerprint=fingerprint), self.assertRaises(store.JsonConflictError):
                with self.operation(request_fingerprint=fingerprint):
                    self.fail("Different request must not enter a committed operation")
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*.json") if path.is_file()}, before)

    def test_old_unbound_commit_cannot_confirm_a_new_bound_request(self):
        with self.operation():
            store.save_json_safe(self.a, {"value": "saved"})
        with self.assertRaises(store.JsonConflictError):
            with self.operation(request_fingerprint="1" * 64):
                self.fail("Unknown old request must not be acknowledged as a match")

    def test_invalid_request_fingerprint_does_not_create_a_journal(self):
        for fingerprint in ("", "x" * 64, "1" * 63, 1):
            with self.subTest(fingerprint=fingerprint), self.assertRaises(store.JsonStoreError):
                with self.operation(request_fingerprint=fingerprint):
                    self.fail("Invalid fingerprint must be rejected")
        self.assertEqual(list((self.root / "backups/json_operations").glob("*/transaction.json")), [])

    @unittest.skipUnless(os.name == "nt", "Windows extended paths only")
    def test_extended_path_spelling_does_not_look_outside_the_root(self):
        target = self.root / "backups/json_operations/operations"
        original = Path.resolve

        def alternate_spelling(path, *args, **kwargs):
            resolved = original(path, *args, **kwargs)
            return Path("\\\\?\\" + str(resolved)) if path == target else resolved

        with patch.object(Path, "resolve", alternate_spelling):
            self.assertEqual(_relative(self.root, target), "backups/json_operations/operations")
            self.assertEqual(store.resolved_json_path(target), target)

    def test_body_failure_writes_no_live_json_or_journal(self):
        before = self.a.read_bytes()
        with self.assertRaises(ValueError):
            with self.operation():
                store.save_json_safe(self.a, {"value": "after"})
                raise ValueError("validation failed")
        self.assertEqual(self.a.read_bytes(), before)
        self.assertEqual(list((self.root / "backups/json_operations").glob("*/transaction.json")), [])

    def test_plain_writer_waits_for_whole_operation_before_taking_document_lock(self):
        started = threading.Event()
        finished = threading.Event()
        errors = []

        def plain_writer():
            started.set()
            try:
                # A legacy service first takes its document transaction, then
                # reads and writes. It must not invert root/document lock order.
                with store.json_transaction(self.b):
                    value = store.load_json_safe(self.b, {})
                    value["legacy_writer"] = True
                    store.save_json_safe(self.b, value)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=plain_writer, daemon=True)
        with self.operation():
            store.save_json_safe(self.b, {"value": "atomic_update"})
            worker.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.1))
        self.assertTrue(finished.wait(3), "root/document lock order deadlocked")
        worker.join(timeout=1)
        self.assertEqual(errors, [])
        self.assertEqual(store.load_json_safe(self.b, {}), {"value": "atomic_update", "legacy_writer": True})

    def test_boolean_is_not_treated_as_unchanged_integer(self):
        self.write(self.a, {"value": 1})
        with self.operation():
            store.save_json_safe(self.a, {"value": True})
        self.assertIs(store.load_json_safe(self.a, {})["value"], True)

    def test_stale_explicit_baseline_cannot_be_refreshed_by_internal_read(self):
        store.load_json_safe(self.a, {})
        revision = store.observed_json_revision(self.a)
        self.write(self.a, {"value": "other tab"})
        store.load_json_safe(self.a, {})
        with self.assertRaises(store.JsonConflictError):
            with self.operation(expected_revisions={self.a: revision}):
                store.load_json_safe(self.a, {})
        self.assertEqual(store.load_json_safe(self.a, {})["value"], "other tab")

    def test_unread_expected_guard_is_checked_before_any_commit(self):
        before = self.a.read_bytes()
        with self.assertRaises(store.JsonConflictError):
            with self.operation(expected_revisions={self.b: None}):
                store.save_json_safe(self.a, {"value": "after"})
        self.assertEqual(self.a.read_bytes(), before)

    def test_missing_file_baseline_is_not_unspecified(self):
        missing = self.root / "settings/user_settings.json"
        with self.operation(expected_revisions={missing: None}):
            store.save_json_safe(missing, {"first": True})
        self.assertEqual(store.load_json_safe(missing, {}), {"first": True})

    def test_change_during_staging_is_not_overwritten(self):
        with self.assertRaises(store.JsonConflictError):
            with self.operation():
                store.load_json_safe(self.a, {})
                store.save_json_safe(self.a, {"value": "after"})
                self.write(self.a, {"value": "other"})
        self.assertEqual(store.load_json_safe(self.a, {})["value"], "other")

    def _interrupt(self, exception_type):
        original = store.save_json_safe
        failed = False

        def fail_second(path, data, **kwargs):
            nonlocal failed
            if Path(path) == self.b and store._active_operation.get() is None and not failed:
                failed = True
                raise exception_type("synthetic failure")
            return original(path, data, **kwargs)

        with patch.object(store, "save_json_safe", side_effect=fail_second):
            with self.operation():
                store.save_json_safe(self.a, {"value": "after"})
                store.save_json_safe(self.b, {"value": "after_b"})

    def test_second_write_failure_rolls_back_byte_exactly(self):
        before_a, before_b = self.a.read_bytes(), self.b.read_bytes()
        with self.assertRaises(store.JsonStoreError):
            self._interrupt(OSError)
        self.assertEqual(self.a.read_bytes(), before_a)
        self.assertEqual(self.b.read_bytes(), before_b)
        self.assertFalse(pending_json_operations(self.root))
        # Retry the same operation ID after an ordinary rollback is allowed.
        with self.operation():
            store.save_json_safe(self.a, {"value": "retried"})
        self.assertEqual(store.load_json_safe(self.a, {})["value"], "retried")

    def test_crash_leaves_pending_blocks_new_save_and_allows_explicit_recovery(self):
        before = self.a.read_bytes()
        with self.assertRaises(SimulatedCrash):
            self._interrupt(SimulatedCrash)
        self.assertEqual(len(pending_json_operations(self.root)), 1)
        with self.assertRaises(store.JsonStoreError):
            with atomic_json_operation(root=self.root, kind="new_save"):
                pass
        for target in (self.b, self.root / "study_logs/legacy.json"):
            before_target = target.read_bytes() if target.exists() else None
            with self.assertRaises(store.JsonStoreError):
                store.save_json_safe(target, {"value": "must not interleave"})
            self.assertEqual(target.read_bytes() if target.exists() else None, before_target)
        rollback_json_operation(self.operation_id, self.root)
        self.assertEqual(self.a.read_bytes(), before)
        self.assertFalse(pending_json_operations(self.root))

    def test_recovery_does_not_erase_a_later_edit(self):
        with self.assertRaises(SimulatedCrash):
            self._interrupt(SimulatedCrash)
        self.write(self.a, {"value": "later independent edit"})
        before_b = self.b.read_bytes()
        with self.assertRaises(store.JsonConflictError):
            rollback_json_operation(self.operation_id, self.root)
        self.assertEqual(store.load_json_safe(self.a, {})["value"], "later independent edit")
        self.assertEqual(self.b.read_bytes(), before_b)

    def test_committed_operation_replay_does_not_write_twice(self):
        with self.operation():
            store.save_json_safe(self.a, {"value": "after"})
        before, mtime = self.a.read_bytes(), self.a.stat().st_mtime_ns
        with self.operation() as operation:
            self.assertTrue(operation.already_committed)
        self.assertEqual((self.a.read_bytes(), self.a.stat().st_mtime_ns), (before, mtime))
        with self.assertRaises(store.JsonStoreError):
            with self.operation():
                store.save_json_safe(self.a, {"value": "accidental replay"})

    def test_kind_collision_and_nested_operations_rejected(self):
        with self.operation():
            store.save_json_safe(self.a, {"value": 1})
        with self.assertRaises(store.JsonConflictError):
            with atomic_json_operation(root=self.root, kind="other", operation_id=self.operation_id):
                pass
        with atomic_json_operation(root=self.root, kind="outer"):
            with self.assertRaises(store.JsonStoreError):
                with atomic_json_operation(root=self.root, kind="inner"):
                    pass

    def test_inventory_changes_are_detected(self):
        before = self.a.read_bytes()
        with self.assertRaises(store.JsonConflictError):
            with self.operation() as operation:
                operation.watch_inventory(self.root / "study_logs", [])
                store.save_json_safe(self.a, {"value": 1})
                self.write(self.root / "study_logs/2026/09/20260916.json", [])
        self.assertEqual(self.a.read_bytes(), before)

    def test_own_new_file_is_allowed_by_inventory_guard(self):
        with self.operation() as operation:
            operation.watch_inventory(self.root / "study_logs", [])
            store.save_json_safe(self.root / "study_logs/2026/09/20260916.json", [])
        self.assertTrue((self.root / "study_logs/2026/09/20260916.json").exists())

    def test_outside_root_code_and_traversal_targets_rejected(self):
        for path in (self.root / "../outside.json", self.root / "main.py", self.root / "backups/arbitrary.json"):
            with self.subTest(path=path), self.assertRaises(store.JsonStoreError):
                with self.operation():
                    store.save_json_safe(path, {})

    def test_invalid_json_is_rejected_before_staging(self):
        before = self.a.read_bytes()
        with self.assertRaises(ValueError):
            with self.operation():
                store.save_json_safe(self.a, {"nan": float("nan")})
        self.assertEqual(self.a.read_bytes(), before)

    def test_invalid_journal_hash_is_rejected_before_rollback(self):
        with self.assertRaises(SimulatedCrash):
            self._interrupt(SimulatedCrash)
        path = self.root / f"backups/json_operations/{self.operation_id}/transaction.json"
        journal = json.loads(path.read_text(encoding="utf-8"))
        journal["documents"][0]["revision"] = "0" * 64
        self.write(path, journal)
        current = self.a.read_bytes()
        with self.assertRaises(store.JsonReadError):
            rollback_json_operation(self.operation_id, self.root)
        self.assertEqual(self.a.read_bytes(), current)

    def test_unknown_journal_schema_or_status_blocks_new_operations(self):
        with self.operation():
            store.save_json_safe(self.a, {"value": "after"})
        path = self.root / f"backups/json_operations/{self.operation_id}/transaction.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for updates in ({"status": "unknown"}, {"schema_version": 99}):
            self.write(path, {**original, **updates})
            with self.assertRaises(store.JsonReadError):
                pending_json_operations(self.root)
            with self.assertRaises(store.JsonReadError):
                with atomic_json_operation(root=self.root, kind="other"):
                    pass

    def test_invalid_status_types_reject_read_recovery_and_new_save_without_changes(self):
        with self.operation():
            store.save_json_safe(self.a, {"value": "after"})
        path = self.root / f"backups/json_operations/{self.operation_id}/transaction.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for status in ([], {}, ["prepared"], None, True):
            with self.subTest(status=status):
                self.write(path, {**original, "status": status})
                before = {str(item): item.read_bytes() for item in self.root.rglob("*.json") if item.is_file()}
                with self.assertRaises(store.JsonReadError):
                    pending_json_operations(self.root)
                with self.assertRaises(store.JsonStoreError):
                    rollback_json_operation(self.operation_id, self.root)
                with self.assertRaises(store.JsonReadError):
                    with atomic_json_operation(root=self.root, kind="other"):
                        pass
                self.assertEqual({str(item): item.read_bytes() for item in self.root.rglob("*.json") if item.is_file()}, before)

    def test_edit_snapshot_survives_rerun_and_missing_file(self):
        missing = self.root / "settings/user_settings.json"
        state = {}
        store.load_json_safe(self.a, {})
        captured = capture_edit_snapshot(state, "edit", [self.a, missing])
        self.assertIsNone(captured[missing])
        self.write(self.a, {"value": "new"})
        store.load_json_safe(self.a, {})
        self.assertEqual(capture_edit_snapshot(state, "edit", [self.a]), captured)
        clear_edit_snapshot(state, "edit")
        self.assertNotEqual(capture_edit_snapshot(state, "edit", [self.a])[self.a], captured[self.a])


if __name__ == "__main__":
    unittest.main()
