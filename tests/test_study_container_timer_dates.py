from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import data_utils
from app_core import json_store, study_log_service
from app_core.study_entry_service import save_manual_entry
from tests import test_study_append_atomic as timer_tests


class StudyContainerPreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_container_test_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(json_store, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = study_log_service.get_study_log_path("2026-01-01", self.root)

    def session(self, identity="old", start="09:00:00"):
        return {"session_id": identity, "start_at": f"2026-01-01T{start}",
                "end_at": "2026-01-01T12:00:00", "duration_sec": 3600,
                "hourly_rate": 1000, "custom": {"keep": [1, 2]}}

    def raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_append_preserves_envelope_metadata_unknown_rows_and_record_fields(self):
        envelope = {"schema_version": 7, "custom_metadata": {"keep": True},
                    "sessions": [None, self.session(), "unrecognized", 42]}
        json_store.save_json_safe(self.path, envelope)
        study_log_service.append_study_session(self.session("new", "11:00:00"), self.root)
        result = self.raw()
        self.assertEqual(result["custom_metadata"], envelope["custom_metadata"])
        self.assertEqual(result["schema_version"], 7)
        self.assertEqual([result["sessions"][i] for i in (0, 2, 3)], [None, "unrecognized", 42])
        self.assertEqual(result["sessions"][1]["custom"], envelope["sessions"][1]["custom"])
        self.assertEqual([row["session_id"] for row in result["sessions"] if isinstance(row, dict)], ["old", "new"])

    def test_list_unknown_rows_are_preserved(self):
        json_store.save_json_safe(self.path, [None, self.session(), "unknown"])
        study_log_service.append_study_session(self.session("new", "11:00:00"), self.root)
        self.assertIsNone(self.raw()[0])
        self.assertEqual(self.raw()[2], "unknown")
        self.assertEqual(self.raw()[3]["session_id"], "new")

    def test_single_record_stays_single_on_update_and_promotes_on_append(self):
        json_store.save_json_safe(self.path, self.session())
        rows = data_utils.load_study_sessions_file(self.path)
        rows[0]["memo"] = "edited"
        data_utils.save_study_sessions_file(self.path, rows)
        self.assertIsInstance(self.raw(), dict)
        self.assertEqual(self.raw()["memo"], "edited")
        study_log_service.append_study_session(self.session("new", "11:00:00"), self.root)
        self.assertIsInstance(self.raw(), list)
        self.assertEqual(self.raw()[0]["custom"], {"keep": [1, 2]})
        self.assertEqual(len(self.raw()), 2)

    def test_normalization_writeback_keeps_envelope(self):
        json_store.save_json_safe(self.path, {"owner": "synthetic", "sessions": [self.session(), False]})
        data_utils.load_study_sessions_file(self.path, write_back=True, backup=False)
        self.assertEqual(self.raw()["owner"], "synthetic")
        self.assertIs(self.raw()["sessions"][1], False)
        self.assertIn("linked_note_ids", self.raw()["sessions"][0])

    def test_unsupported_or_malformed_document_is_not_replaced(self):
        for malformed in ({}, {"schema_version": 7}, {"sessions": {}}, {"sessions": None}, None, 12):
            with self.subTest(malformed=malformed):
                json_store.save_json_safe(self.path, malformed)
                before = self.path.read_bytes()
                with self.assertRaises(json_store.JsonReadError):
                    study_log_service.append_study_session(self.session("new", "11:00:00"), self.root)
                self.assertEqual(self.path.read_bytes(), before)

    def test_internal_container_reload_does_not_approve_concurrent_change(self):
        json_store.save_json_safe(self.path, {"metadata": "old", "sessions": [self.session()]})
        rows = data_utils.load_study_sessions_file(self.path)
        # Simulate an uncooperative editor without refreshing this caller's token.
        self.path.write_text(json.dumps({"metadata": "remote", "sessions": [self.session()]}), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(json_store.JsonConflictError):
            data_utils.save_study_sessions_file(self.path, rows)
        self.assertEqual(self.path.read_bytes(), before)

    def test_manual_submission_replay_preserves_container_without_duplicates(self):
        json_store.save_json_safe(self.path, {"metadata": {"keep": 1}, "sessions": [self.session()]})
        state = {}
        payload = self.session("new", "11:00:00")
        _, replayed = save_manual_entry(payload, state, self.root)
        self.assertFalse(replayed)
        before = self.path.read_bytes()
        _, replayed = save_manual_entry(payload, state, self.root)
        self.assertTrue(replayed)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.raw()["metadata"], {"keep": 1})
        self.assertEqual(len(self.raw()["sessions"]), 2)

    def test_corrupt_json_is_not_treated_as_empty(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"sessions": [', encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(json_store.JsonReadError):
            study_log_service.append_study_session(self.session(), self.root)
        self.assertEqual(self.path.read_bytes(), before)

    def test_new_document_still_uses_list_format(self):
        study_log_service.append_study_session(self.session(), self.root)
        self.assertIsInstance(self.raw(), list)
        self.assertEqual(len(self.raw()), 1)


class TimerOwnedDateTests(unittest.TestCase):
    """Use the existing timer's isolated fixture, not a copied save routine."""

    setUpClass = classmethod(timer_tests.TimerAtomicSaveTests.setUpClass.__func__)
    setUp = timer_tests.TimerAtomicSaveTests.setUp
    save = timer_tests.TimerAtomicSaveTests.save
    live_files = timer_tests.TimerAtomicSaveTests.live_files

    def test_midnight_month_and_year_boundaries_update_only_owned_plan(self):
        for owner, today in (("2026-01-01", "2026-01-02"), ("2026-01-31", "2026-02-01"), ("2026-12-31", "2027-01-01")):
            with self.subTest(owner=owner):
                data_utils.save_daily_plan(self.root, {"date": owner, "tasks": [{"task_id": "task_synthetic", "title": "owner", "linked_session_ids": []}]})
                data_utils.save_daily_plan(self.root, {"date": today, "tasks": [{"task_id": "task_today", "title": "today", "linked_session_ids": []}]})
                today_path = data_utils.daily_plan_path(self.root, today)
                today_before = today_path.read_bytes()
                self.ui.session_state.pending_session = {"start_at": f"{owner}T23:50:00", "end_at": f"{today}T00:10:00", "duration_sec": 1200, "hourly_rate": 1000,
                                                         "linked_task_id": "task_synthetic", "linked_task_date": owner}
                self.ui.session_state.selected_plan_task_date = owner
                self.ui.session_state.selected_plan_task_id = "task_synthetic"
                with patch.object(data_utils, "app_today", return_value=date.fromisoformat(today)):
                    self.save()
                self.assertEqual(self.ui.errors, [])
                record = study_log_service.load_study_sessions_by_date(owner, self.root)[-1]
                task = data_utils.load_daily_plan(self.root, owner)["tasks"][0]
                self.assertEqual(record["linked_task_date"], owner)
                self.assertIn(record["session_id"], task["linked_session_ids"])
                self.assertEqual(task["status"], "in_progress")
                self.assertEqual(today_path.read_bytes(), today_before)

    def test_deleted_or_inactive_selected_task_stops_all_writes_and_keeps_input(self):
        for tasks in ([], [{"task_id": "task_synthetic", "is_active": False}]):
            with self.subTest(tasks=tasks):
                data_utils.save_daily_plan(self.root, {"date": "2026-01-01", "tasks": tasks})
                before = self.live_files()
                self.ui.session_state.pending_session = deepcopy(self.pending)
                self.save()
                self.assertEqual(self.live_files(), before)
                self.assertIsNotNone(self.ui.session_state.pending_session)
                self.assertEqual(self.ui.session_state.pending_memo, "keep this memo")
                self.assertTrue(any("削除・無効化" in error for error in self.ui.errors))

    def test_explicit_unselect_clears_pending_task_and_allows_save(self):
        self.ui.session_state.pending_session.update(linked_task_id="deleted", linked_task_date="2025-12-31")
        self.ui.session_state.selected_plan_task_id = ""
        self.ui.session_state.selected_plan_task_date = ""
        before = data_utils.daily_plan_path(self.root, "2026-01-01").read_bytes()
        self.save()
        self.assertEqual(self.ui.errors, [])
        record = study_log_service.load_study_sessions_by_date("2026-01-01", self.root)[0]
        self.assertEqual(record["linked_task_id"], "")
        self.assertEqual(record["linked_task_date"], "")
        self.assertEqual(data_utils.daily_plan_path(self.root, "2026-01-01").read_bytes(), before)

    def test_invalid_owned_day_does_not_fall_back_to_today(self):
        before = self.live_files()
        with self.assertRaises(json_store.JsonStoreError):
            data_utils.link_session_to_plan_task(self.root, "task_synthetic", "session_1", target_date="2026-99-99", require_existing=True)
        self.assertEqual(self.live_files(), before)

    def test_mismatched_document_day_never_redirects_into_other_plan(self):
        owner = data_utils.daily_plan_path(self.root, "2026-01-01")
        other = data_utils.daily_plan_path(self.root, "2026-01-02")
        json_store.save_json_safe(owner, {"date": "2026-01-02", "tasks": [
            {"task_id": "task_synthetic", "title": "owner task", "linked_session_ids": []}], "owner_unknown": True})
        json_store.save_json_safe(other, {"date": "2026-01-02", "tasks": [
            {"task_id": "other_task", "title": "other day task"}], "other_unknown": {"keep": True}})
        before = self.live_files()
        for strict in (False, True):
            with self.subTest(require_existing=strict):
                with self.assertRaises(json_store.JsonConflictError):
                    data_utils.link_session_to_plan_task(
                        self.root, "task_synthetic", "new_session", target_date="2026-01-01", require_existing=strict)
                self.assertEqual(self.live_files(), before)
        # The timer's staged multi-file save must stop as a whole, too.
        self.save()
        self.assertEqual(self.live_files(), before)
        self.assertIsNotNone(self.ui.session_state.pending_session)
        self.assertEqual(self.ui.session_state.pending_memo, "keep this memo")
        self.assertTrue(any("日付が一致しません" in error for error in self.ui.errors))

    def test_freeze_pending_keeps_selected_owner_instead_of_current_day(self):
        self.ui.session_state.update(start_at=datetime(2026, 1, 1, 23, 50), accumulated_sec=1200,
                                     display_adjust_sec=0, selected_plan_task_date="2026-01-01")
        self.scope["freeze_pending"]()
        self.assertEqual(self.ui.session_state.pending_session["linked_task_id"], "task_synthetic")
        self.assertEqual(self.ui.session_state.pending_session["linked_task_date"], "2026-01-01")
        self.ui.session_state.selected_plan_task_id = ""
        self.ui.session_state.selected_plan_task_date = ""
        self.scope["freeze_pending"]()
        self.assertEqual(self.ui.session_state.pending_session["linked_task_id"], "")
        self.assertEqual(self.ui.session_state.pending_session["linked_task_date"], "")

    def test_selection_app_test_retains_owner_after_midnight_then_can_unselect(self):
        from streamlit import config
        from streamlit.testing.v1 import AppTest

        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        page = next((Path(__file__).resolve().parents[1] / "pages").glob("01_*.py"))
        tree = ast.parse(page.read_text(encoding="utf-8-sig"))
        selection = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_plan_task_selection")
        source = ("from pathlib import Path\nfrom datetime import date\nfrom typing import Any\nimport streamlit as st\n"
                  "from data_utils import load_daily_plan\n"
                  f"ROOT=Path({str(self.root)!r})\n"
                  "app_today=lambda: date.fromisoformat(st.session_state.get('fixture_today','2026-01-01'))\n"
                  + ast.unparse(selection) + "\nrender_plan_task_selection()\n")
        app = AppTest.from_string(source, default_timeout=15).run()
        selected_label = next(option for option in app.selectbox[0].options if "[task_synthetic]" in option)
        app.selectbox[0].select(selected_label).run()
        self.assertEqual(app.session_state.selected_plan_task_date, "2026-01-01")
        app.session_state.fixture_today = "2026-01-02"
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state.selected_plan_task_id, "task_synthetic")
        self.assertEqual(app.session_state.selected_plan_task_date, "2026-01-01")
        # A subsequent deletion must remain a visible selection, not become an
        # implicit unlinked save. Explicit "none" is still available.
        data_utils.save_daily_plan(self.root, {"date": "2026-01-01", "tasks": []})
        app.run()
        self.assertIn("削除・無効", app.selectbox[0].value)
        app.selectbox[0].select("選択しない").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state.selected_plan_task_id, "")
        self.assertEqual(app.session_state.selected_plan_task_date, "")


if __name__ == "__main__":
    unittest.main()
