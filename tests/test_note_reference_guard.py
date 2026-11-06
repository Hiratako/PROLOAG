from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import json_store as store
from app_core.json_operations import atomic_json_operation
from app_core.note_reference_guard import validate_note_references


class NoteReferenceGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-note-guard-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(store, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.trash_path = self.root / "backups" / "note_trash.json"
        self.notes_path = self.root / "notes_data" / "notebooks.json"
        self.card_path = self.root / "flashcards.json"

    def trash(self, **extra):
        return [{"trash_id": "t1", "note_id": "note_one", "note": {"note_id": "note_one", "id": "legacy_one"}, **extra}]

    def notebooks(self):
        return {"notebooks": [{"id": "nb", "sections": [{"id": "sec", "pages": [{"note_id": "note_one", "id": "legacy_one"}]}]}]}

    def write_trash(self, value=None):
        store.save_json_safe(self.trash_path, self.trash() if value is None else value)

    def test_targets_without_links_and_non_targets_have_no_io(self):
        from app_core import note_reference_guard as guard
        with patch.object(guard, "load_json_safe", side_effect=AssertionError("unexpected read")):
            validate_note_references(self.root, self.card_path, [{"linked_note_ids": [], "source_note_id": ""}])
            validate_note_references(self.root, self.root / "settings" / "user_settings.json", {"source_note_id": "note_one"})
            validate_note_references(self.root, self.root / "diary_entries" / "day.json", {"linked_note_ids": ["note_one"]})
            validate_note_references(self.root, self.root / "daily_plans" / "backup" / "old.json", {"source_note_id": "note_one"})

    def test_deleted_id_and_legacy_alias_are_blocked_in_all_reference_shapes(self):
        self.write_trash()
        cases = [
            ("study_logs/2026/09/20260916.json", [{"linked_note_ids": ["legacy_one"]}]),
            ("study_logs/2026/09/20260916.json", {"sessions": [{"source_note_id": "note_one"}]}),
            ("daily_plans/2026/09/20260916.json", {"review_note_ids": ["note_one"]}),
            ("daily_plans/2026/09/20260916.json", {"tasks": [{"linked_note_ids": ["legacy_one"]}]}),
            ("flashcards.json", [{"source_note_id": "note_one"}]),
            ("reminders.json", [{"review_note_ids": ["legacy_one"]}]),
        ]
        for relative, data in cases:
            with self.subTest(relative=relative, data=data):
                with self.assertRaises(store.JsonConflictError):
                    validate_note_references(self.root, self.root / relative, data)

    def test_unknown_ids_remain_compatible_and_do_not_read_notebooks(self):
        from app_core import note_reference_guard as guard
        self.write_trash()
        real_read = guard.load_json_safe
        reads = []
        def read(path, default):
            reads.append(Path(path))
            self.assertNotEqual(Path(path), self.notes_path)
            return real_read(path, default)
        with patch.object(guard, "load_json_safe", side_effect=read):
            validate_note_references(self.root, self.card_path, [{"linked_note_ids": ["legacy_unknown"]}])
        self.assertEqual(reads, [self.trash_path])

    def test_restored_marker_or_live_note_allows_links(self):
        self.write_trash(self.trash(restored_at="2026-09-16T12:00:00"))
        validate_note_references(self.root, self.card_path, [{"source_note_id": "legacy_one"}])
        self.write_trash()
        store.save_json_safe(self.notes_path, self.notebooks())
        validate_note_references(self.root, self.card_path, [{"linked_note_ids": ["note_one", "legacy_one"]}])

    def test_invalid_trash_structures_fail_closed(self):
        from app_core import note_reference_guard as guard
        for broken in ({}, [1], [{"note_id": "one"}], [{"note": {}}], self.trash(restored_at=True)):
            with self.subTest(broken=broken), patch.object(guard, "load_json_safe", return_value=broken):
                with self.assertRaises(store.JsonReadError):
                    validate_note_references(self.root, self.card_path, [{"source_note_id": "note_one"}])

    def test_normal_save_cannot_resurrect_reference_after_deletion(self):
        store.save_json_safe(self.card_path, [{"card_id": "c1", "linked_note_ids": []}])
        self.write_trash()
        before = self.card_path.read_bytes()
        with self.assertRaises(store.JsonConflictError):
            store.save_json_safe(self.card_path, [{"card_id": "c1", "source_note_id": "legacy_one"}])
        self.assertEqual(self.card_path.read_bytes(), before)

    def test_atomic_restore_uses_final_staged_state_independent_of_order(self):
        self.write_trash()
        with atomic_json_operation(root=self.root, kind="test_restore", operation_id=uuid4().hex):
            # References are staged before notes/trash, as in a real restore.
            store.save_json_safe(self.card_path, [{"card_id": "c1", "linked_note_ids": ["legacy_one"]}])
            store.save_json_safe(self.notes_path, self.notebooks())
            store.save_json_safe(self.trash_path, self.trash(restored_at="2026-09-16T12:00:00"))
        self.assertEqual(store.load_json_safe(self.card_path, [])[0]["linked_note_ids"], ["legacy_one"])

    def test_atomic_deleted_reference_rejects_before_any_live_write(self):
        self.write_trash()
        with self.assertRaises(store.JsonConflictError):
            with atomic_json_operation(root=self.root, kind="test_bad_link", operation_id=uuid4().hex):
                store.save_json_safe(self.root / "diary_entries" / "2026" / "09" / "20260916.json", {"body": "must not commit"})
                store.save_json_safe(self.card_path, [{"source_note_id": "note_one"}])
        self.assertFalse(self.card_path.exists())
        self.assertFalse((self.root / "diary_entries" / "2026" / "09" / "20260916.json").exists())


if __name__ == "__main__":
    unittest.main()
