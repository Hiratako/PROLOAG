"""Corrupted legacy study-edit journals must never perform a partial rollback."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app_core import json_store, study_log_editor
from tests import test_study_log_editor as fixtures


class StudyRecoveryValidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.StudyLogEditorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def interrupted_commit(self, plan):
        save = study_log_editor.save_json_safe

        def interrupt_completion(path, payload, **kwargs):
            if Path(path).name == "transaction.json" and payload.get("status") == "committed":
                raise KeyboardInterrupt("synthetic interruption after all record writes")
            return save(path, payload, **kwargs)

        with patch.object(study_log_editor, "save_json_safe", side_effect=interrupt_completion):
            with self.assertRaises(KeyboardInterrupt):
                study_log_editor.commit_session_change(plan, self.root)
        operation_id = study_log_editor.pending_session_changes(self.root)[0]["operation_id"]
        path = self.root / "backups/study_log_edits" / operation_id / "transaction.json"
        return operation_id, path, self.fixture.read_json(path)

    def interrupted_delete(self):
        self.fixture.seed([self.fixture.session(linked_note_ids=["note_a"], linked_task_id="task_a")])
        self.fixture.seed_links()
        return self.interrupted_commit(self.fixture.prepare_delete())

    def assert_rejected_unchanged(self, operation_id, path, journal, error=json_store.JsonReadError):
        self.fixture.write_json(path, journal)
        before = self.fixture.json_snapshot()
        with self.assertRaises(error):
            study_log_editor.rollback_pending_session_change(operation_id, self.root)
        self.assertEqual(self.fixture.json_snapshot(), before)
        self.assertEqual(self.fixture.read_json(path)["status"], "prepared")

    def test_changed_before_raw_cannot_silently_lose_original_study_record(self):
        operation_id, path, journal = self.interrupted_delete()
        journal["documents"][0]["before_raw"] = base64.b64encode(b"[]").decode("ascii")
        self.assert_rejected_unchanged(operation_id, path, journal)
        self.assertEqual(len(study_log_editor.pending_session_changes(self.root)), 1)

    def test_invalid_base64_in_later_target_prevents_any_earlier_restore(self):
        operation_id, path, journal = self.interrupted_delete()
        # Recovery runs in reverse order. A bad first entry previously left the
        # other targets already rolled back before failing on this last entry.
        journal["documents"][0]["before_raw"] = "invalid base64!"
        self.assertGreater(len(journal["documents"]), 2)
        self.assert_rejected_unchanged(operation_id, path, journal)

    def test_invalid_last_entry_is_checked_before_any_target_is_restored(self):
        operation_id, path, journal = self.interrupted_delete()
        journal["documents"][-1]["revision"] = "0" * 64
        self.assert_rejected_unchanged(operation_id, path, journal)

    def test_consistent_hash_but_mismatched_structured_before_is_rejected(self):
        operation_id, path, journal = self.interrupted_delete()
        journal["documents"][0]["before"] = []
        self.assert_rejected_unchanged(operation_id, path, journal)

    def test_invalid_json_duplicate_keys_and_nonfinite_before_images_are_rejected(self):
        operation_id, path, original = self.interrupted_delete()
        for raw in (b"not json", b'{"a":1,"a":2}', b'[NaN]', b'[1e999]', b'\xff'):
            with self.subTest(raw=raw):
                journal = deepcopy(original)
                journal["documents"][0].update(
                    before_raw=base64.b64encode(raw).decode("ascii"),
                    revision=hashlib.sha256(raw).hexdigest(),
                )
                self.assert_rejected_unchanged(operation_id, path, journal)

    def test_missing_raw_image_or_revision_stays_pending_for_manual_inspection(self):
        operation_id, path, original = self.interrupted_delete()
        for key in ("before_raw", "revision", "before", "after"):
            with self.subTest(key=key):
                journal = deepcopy(original)
                journal["documents"][0].pop(key)
                self.assert_rejected_unchanged(operation_id, path, journal)

    def test_invalid_or_duplicate_targets_are_rejected_before_rollback(self):
        operation_id, path, original = self.interrupted_delete()
        for target in ("../outside.json", "master_data/master.json", original["documents"][0]["path"]):
            with self.subTest(target=target):
                journal = deepcopy(original)
                journal["documents"][-1]["path"] = target
                self.assert_rejected_unchanged(operation_id, path, journal)

    def test_malformed_document_list_is_not_marked_recovered(self):
        operation_id, path, original = self.interrupted_delete()
        for documents in (None, {}, [], [None], [{"path": "study_logs/2026/08/20260828.json"}]):
            with self.subTest(documents=documents):
                journal = {**original, "documents": documents}
                self.assert_rejected_unchanged(operation_id, path, journal)

    def test_mismatched_operation_id_is_rejected(self):
        operation_id, path, journal = self.interrupted_delete()
        journal["operation_id"] = "0" * 32
        self.assert_rejected_unchanged(operation_id, path, journal)
        with self.assertRaises(json_store.JsonReadError):
            study_log_editor.pending_session_changes(self.root)

    def test_invalid_status_types_are_user_facing_and_block_new_saves_read_only(self):
        from app_core import json_operations, study_log_editor_ui

        operation_id, path, original = self.interrupted_delete()
        for status in ([], {}, ["prepared"], None, True, "unknown"):
            with self.subTest(status=status):
                self.fixture.write_json(path, {**original, "status": status})
                before = self.fixture.json_snapshot()
                with self.assertRaises(json_store.JsonReadError):
                    study_log_editor.pending_session_changes(self.root)
                with self.assertRaises(ValueError):
                    study_log_editor.rollback_pending_session_change(operation_id, self.root)
                with self.assertRaises(json_store.JsonReadError):
                    json_operations.assert_no_pending_operations(self.root)
                with patch.object(study_log_editor_ui.st, "error") as error:
                    self.assertTrue(study_log_editor_ui._render_pending_changes(self.root, {}))
                error.assert_called_once()
                self.assertEqual(self.fixture.json_snapshot(), before)

    def test_later_reformatted_file_is_not_overwritten_even_if_content_matches(self):
        operation_id, path, journal = self.interrupted_delete()
        target = self.root / journal["documents"][1]["path"]
        target.write_text(json.dumps(journal["documents"][1]["after"]), encoding="utf-8")
        self.assert_rejected_unchanged(operation_id, path, journal, json_store.JsonConflictError)

    def test_valid_existing_journal_restores_byte_exact_before_images(self):
        target = self.fixture.seed([self.fixture.session()])
        original = b"\xef\xbb\xbf" + target.read_bytes() + b"\n\n"
        target.write_bytes(original)
        plan = self.fixture.prepare_edit({"memo": "new synthetic memo"})
        operation_id, _, _ = self.interrupted_commit(plan)
        study_log_editor.rollback_pending_session_change(operation_id, self.root)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(study_log_editor.pending_session_changes(self.root), [])

    def test_valid_move_to_previously_missing_file_can_be_rolled_back(self):
        source = self.fixture.seed([self.fixture.session()])
        original = source.read_bytes()
        plan = self.fixture.prepare_edit({"start_at": "2026-08-29T09:00:00", "end_at": "2026-08-29T10:00:00"})
        operation_id, _, journal = self.interrupted_commit(plan)
        missing_targets = [self.root / item["path"] for item in journal["documents"] if item["before_raw"] is None]
        self.assertTrue(missing_targets)
        self.assertTrue(all(target.exists() for target in missing_targets))
        study_log_editor.rollback_pending_session_change(operation_id, self.root)
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(all(not target.exists() for target in missing_targets))


if __name__ == "__main__":
    unittest.main()
