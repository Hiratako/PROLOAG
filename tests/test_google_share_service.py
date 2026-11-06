from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import google_share_service as service
from app_core.google_share_snapshot import build_share_snapshot
from app_core.google_sheets_client import GoogleShareError
from app_core import json_store


class FakeClient:
    def __init__(self):
        self.publication = {}
        self.writes = []
        self.error = None
        self.commit_before_error = False
        self.after_publish_read_error = False

    def create_spreadsheet(self, title):
        return {"spreadsheet_id": "fixture-book", "url": "https://docs.google.com/spreadsheets/d/fixture-book/edit"}

    def read_publication(self, book):
        if self.after_publish_read_error and self.writes:
            raise GoogleShareError("認証期限", code="reauthorize")
        return deepcopy(self.publication)

    def publish(self, book, snapshot):
        if self.error and not self.commit_before_error:
            raise self.error
        self.writes.append(deepcopy(snapshot))
        self.publication = {"snapshot_id": snapshot["snapshot_id"], "payload_hash": service.payload_hash(snapshot)}
        if self.error:
            raise self.error


class GoogleShareServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_google_service_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.snapshot = build_share_snapshot(self.root, date(2026, 9, 10), date(2026, 9, 16))
        self.client = FakeClient()
        original_read = json_store._read
        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root.resolve()))
            return original_read(path)
        reader = patch.object(json_store, "_read", side_effect=isolated_read)
        reader.start()
        self.addCleanup(reader.stop)

    def send(self, snapshot=None, **kwargs):
        return service.publish_share(self.client, snapshot, root=self.root, **kwargs)

    def test_success_stores_history_and_no_pending(self):
        result = self.send(self.snapshot)
        self.assertFalse(result["reconciled"])
        self.assertEqual(len(self.client.writes), 1)
        self.assertIsNone(service.load_pending_share(self.root))
        self.assertEqual(service.load_share_state(self.root)["last_success"], result)
        self.assertIn("理解度", service.consultation_prompt(result))
        self.assertIn(result["snapshot_id"], service.consultation_prompt(result))

    def test_changed_destination_is_not_published(self):
        with self.assertRaises(service.ShareStateError):
            self.send(self.snapshot, expected_spreadsheet_id="a-different-book")
        self.assertEqual(self.client.writes, [])
        self.assertIsNone(service.load_pending_share(self.root))

    def test_lost_response_reconciles_without_second_write(self):
        self.client.error = GoogleShareError("応答なし", code="transport", uncertain=True)
        self.client.commit_before_error = True
        with self.assertRaises(GoogleShareError):
            self.send(self.snapshot)
        pending = service.load_pending_share(self.root)
        self.assertEqual(pending["status"], "uncertain")
        result = self.send(check_only=True)
        self.assertTrue(result["reconciled"])
        self.assertEqual(len(self.client.writes), 1)
        self.assertIsNone(service.load_pending_share(self.root))

    def test_uncertain_retry_pins_original_snapshot(self):
        self.client.error = GoogleShareError("応答なし", code="transport", uncertain=True)
        with self.assertRaises(GoogleShareError):
            self.send(self.snapshot)
        modified = deepcopy(self.snapshot)
        modified["tables"]["overview"].append(["相談", "違う内容"])
        with self.assertRaises(service.ShareStateError):
            self.send(modified)
        with self.assertRaises(service.ShareStateError):
            service.discard_failed_share(self.root)
        self.client.error = None
        self.send()
        self.assertEqual(self.client.writes, [self.snapshot])

    def test_definitive_failure_can_be_discarded(self):
        self.client.error = GoogleShareError("権限なし", code="reauthorize")
        with self.assertRaises(GoogleShareError):
            self.send(self.snapshot)
        self.assertEqual(service.load_pending_share(self.root)["status"], "failed")
        service.discard_failed_share(self.root)
        self.assertIsNone(service.load_pending_share(self.root))

    def test_expected_operation_rejects_replacement_or_missing_before_remote_access(self):
        self.client.error = GoogleShareError("未送信")
        with self.assertRaises(GoogleShareError):
            self.send(self.snapshot)
        original = service.load_pending_share(self.root)
        replacement = deepcopy(original)
        replacement["operation_id"] = "new-operation"
        service._save_pending(self.root, replacement)
        path = self.root / "backups/google_share/pending.json"
        before = path.read_bytes()
        for action in (lambda: self.send(expected_operation_id=original["operation_id"]),
                       lambda: self.send(check_only=True, expected_operation_id=original["operation_id"]),
                       lambda: service.discard_failed_share(self.root, expected_operation_id=original["operation_id"])):
            with patch.object(self.client, "read_publication", side_effect=AssertionError("Must reject before network")):
                with self.assertRaises(service.ShareStateError):
                    action()
            self.assertEqual(path.read_bytes(), before)
        service.discard_failed_share(self.root, expected_operation_id=replacement["operation_id"])
        with self.assertRaises(service.ShareStateError):
            self.send(expected_operation_id=replacement["operation_id"])
        with self.assertRaises(service.ShareStateError):
            service.discard_failed_share(self.root, expected_operation_id=replacement["operation_id"])

    def test_success_followed_by_read_failure_remains_uncertain(self):
        self.client.after_publish_read_error = True
        with self.assertRaises(GoogleShareError):
            self.send(self.snapshot)
        self.assertEqual(service.load_pending_share(self.root)["status"], "uncertain")
        self.client.after_publish_read_error = False
        self.assertTrue(self.send()["reconciled"])

    def test_check_only_never_publishes(self):
        self.client.error = GoogleShareError("応答なし", uncertain=True)
        with self.assertRaises(GoogleShareError):
            self.send(self.snapshot)
        self.assertFalse(self.send(check_only=True)["confirmed"])
        self.assertEqual(self.client.writes, [])

    def test_history_failure_after_remote_commit_recovers(self):
        original = json_store.save_json_safe
        def fail_history(path, data, **kwargs):
            if Path(path).name == "state.json":
                raise OSError("disk unavailable")
            return original(path, data, **kwargs)
        with patch.object(json_store, "save_json_safe", side_effect=fail_history):
            with self.assertRaises(OSError):
                self.send(self.snapshot)
        self.assertEqual(service.load_pending_share(self.root)["status"], "uncertain")
        self.assertTrue(self.send()["reconciled"])
        self.assertEqual(len(self.client.writes), 1)

    def test_clear_pending_failure_does_not_duplicate_history(self):
        original = json_store.save_json_safe
        def fail_clear(path, data, **kwargs):
            if Path(path).name == "pending.json" and data is None:
                raise OSError("disk unavailable")
            return original(path, data, **kwargs)
        with patch.object(json_store, "save_json_safe", side_effect=fail_clear):
            with self.assertRaises(OSError):
                self.send(self.snapshot)
        self.send()
        self.assertEqual(len(service.load_share_state(self.root)["history"]), 1)

    def test_pending_hash_tampering_stops_before_network(self):
        self.client.error = GoogleShareError("応答なし", uncertain=True)
        with self.assertRaises(GoogleShareError):
            self.send(self.snapshot)
        pending = service.load_pending_share(self.root)
        pending["snapshot"]["counts"]["study_sessions"] = 999
        json_store.save_json_safe(self.root / "backups/google_share/pending.json", pending)
        with self.assertRaises(service.ShareStateError):
            self.send()
        self.assertEqual(self.client.writes, [])

    def test_preferences_are_allowlisted_and_no_read_writes(self):
        before = list(self.root.rglob("*.json"))
        self.assertEqual(service.load_share_state(self.root), {})
        self.assertIsNone(service.load_pending_share(self.root))
        self.assertEqual(before, list(self.root.rglob("*.json")))
        service.save_share_preferences({"days": 30, "include_content": False, "token": "NEVER_STORE"}, self.root)
        state = service.load_share_state(self.root)
        self.assertNotIn("NEVER_STORE", str(state))
        with self.assertRaises(service.ShareStateError):
            service.save_share_preferences({"days": 1}, self.root)


if __name__ == "__main__":
    unittest.main()
