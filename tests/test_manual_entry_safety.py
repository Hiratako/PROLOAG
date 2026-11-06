from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import json_store
from app_core.study_entry_service import MANUAL_SUBMISSION_KEY, save_manual_entry
from app_core.study_log_service import load_study_sessions_by_date


class ManualEntrySafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_manual_test_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(json_store, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = {}
        self.payload = {"start_at": "2026-09-16T09:00:00", "end_at": "2026-09-16T10:00:00",
                        "duration_sec": 3600, "hourly_rate": 1000, "memo": "synthetic",
                        "money_policy": {"overtime_enabled": True, "threshold_hours": 8, "multiplier": 1.25}}

    def rows(self):
        return load_study_sessions_by_date("2026-09-16", self.root)

    def test_repeat_is_noop_even_when_preview_amount_changes(self):
        _, replayed = save_manual_entry(self.payload, self.state, self.root)
        self.assertFalse(replayed)
        second = {**self.payload, "money": 1234}
        _, replayed = save_manual_entry(second, self.state, self.root)
        self.assertTrue(replayed)
        self.assertEqual(len(self.rows()), 1)

    def test_explicit_repeat_and_new_input_get_new_operation(self):
        save_manual_entry(self.payload, self.state, self.root)
        self.state.pop(MANUAL_SUBMISSION_KEY)
        save_manual_entry(self.payload, self.state, self.root)
        save_manual_entry({**self.payload, "memo": "changed"}, self.state, self.root)
        self.assertEqual(len(self.rows()), 3)

    def test_failure_retains_identity_and_retry_is_single_record(self):
        original = json_store.save_json_safe
        def failure(path, data, **options):
            if "study_logs" in Path(path).parts and json_store._active_operation.get() is None:
                raise OSError("synthetic disk failure")
            return original(path, data, **options)
        with patch.object(json_store, "save_json_safe", side_effect=failure):
            with self.assertRaises(json_store.JsonStoreError):
                save_manual_entry(self.payload, self.state, self.root)
        pending = deepcopy(self.state[MANUAL_SUBMISSION_KEY])
        self.assertTrue(pending["session_id"])
        self.assertEqual(self.rows(), [])
        save_manual_entry(self.payload, self.state, self.root)
        self.assertEqual(self.rows()[0]["session_id"], pending["session_id"])
        self.assertEqual(len(self.rows()), 1)
