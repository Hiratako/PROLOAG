from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch

import data_utils
from app_core import json_store, study_log_service


class StudyAppendRepricingTests(unittest.TestCase):
    policy = {"overtime_enabled": True, "threshold_hours": 8.0, "multiplier": 1.25}
    day = "2026-01-01"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_append_test_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(json_store, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, session_id, start, hours=1, **updates):
        return {
            "session_id": session_id, "start_at": f"{self.day}T{start}",
            "end_at": f"{self.day}T23:00:00", "duration_sec": hours * 3600,
            "hourly_rate": 1000, "money_policy": deepcopy(self.policy),
            "memo": "synthetic", "unknown_field": {"keep": [1, 2]}, **updates,
        }

    def rows(self):
        return study_log_service.load_study_sessions_by_date(self.day, self.root)

    def append(self, record):
        return study_log_service.append_study_session(record, self.root)

    def test_backdated_insert_reprices_later_record(self):
        self.append(self.session("later", "18:00:00"))
        before = self.rows()[0]
        self.append(self.session("earlier", "09:00:00", 8))
        later = self.rows()[0]
        self.assertEqual(later["money"], 1250)
        self.assertEqual(later["normal_money"], 0)
        self.assertEqual(later["overtime_money"], 1250)
        for key in ("session_id", "start_at", "end_at", "memo", "created_at", "unknown_field", "money_policy"):
            self.assertEqual(later[key], before[key])

    def test_insert_uses_each_later_records_saved_policy_and_rate(self):
        custom = {"overtime_enabled": True, "threshold_hours": 4.0, "multiplier": 2.0}
        self.append(self.session("later", "18:00:00", hourly_rate=2000, money_policy=custom))
        self.append(self.session("earlier", "09:00:00", 8))
        self.assertEqual(self.rows()[0]["money"], 4000)
        self.assertEqual(self.rows()[0]["money_policy"], custom)

    def test_unchanged_overtime_allocation_preserves_later_record_exactly(self):
        self.append(self.session("later", "18:00:00"))
        before = self.rows()[0]
        self.append(self.session("earlier", "09:00:00"))
        self.assertEqual(self.rows()[0], before)

    def test_disabled_overtime_record_is_not_repriced(self):
        self.append(self.session("later", "18:00:00", money_policy={**self.policy, "overtime_enabled": False}))
        before = self.rows()[0]
        self.append(self.session("earlier", "09:00:00", 8))
        self.assertEqual(self.rows()[0], before)

    def test_unaffected_legacy_record_is_preserved(self):
        record = self.session("earlier", "09:00:00", money=999)
        record.pop("money_policy")
        self.append(record)
        before = self.rows()[0]
        self.append(self.session("later", "18:00:00"))
        self.assertEqual(self.rows()[0], before)

    def test_affected_legacy_policy_stops_insert_without_modifying_day(self):
        record = self.session("later", "18:00:00", money=1000)
        record.pop("money_policy")
        path = self.append(record)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "残業設定が未保存"):
            self.append(self.session("earlier", "09:00:00", 8))
        self.assertEqual(path.read_bytes(), before)

    def test_affected_invalid_rate_stops_insert_without_modifying_day(self):
        record = self.session("later", "18:00:00")
        path = self.append(record)
        rows = self.rows()
        rows[0].pop("hourly_rate")
        study_log_service.save_study_sessions_by_date(self.day, rows, self.root)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "時給が未記録"):
            self.append(self.session("earlier", "09:00:00", 8))
        self.assertEqual(path.read_bytes(), before)

    def test_midday_insert_reprices_all_affected_later_records(self):
        self.append(self.session("morning", "09:00:00", 6))
        self.append(self.session("later1", "18:00:00"))
        self.append(self.session("later2", "20:00:00"))
        self.append(self.session("middle", "16:00:00", 2))
        rows = {row["session_id"]: row for row in self.rows()}
        self.assertEqual(rows["later1"]["money"], 1250)
        self.assertEqual(rows["later2"]["money"], 1250)
        self.assertEqual(rows["morning"]["money"], 6000)

    def test_write_failure_keeps_previous_amount_and_no_insert(self):
        path = self.append(self.session("later", "18:00:00"))
        before = path.read_bytes()
        with patch.object(study_log_service, "save_study_sessions_by_date", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.append(self.session("earlier", "09:00:00", 8))
        self.assertEqual(path.read_bytes(), before)

    def test_direct_append_waits_for_inventory_sensitive_operation(self):
        from app_core.json_operations import atomic_json_operation

        entered = threading.Event()
        def append_from_another_thread():
            entered.set()
            return self.append(self.session("later", "18:00:00"))

        with ThreadPoolExecutor(max_workers=1) as executor:
            with atomic_json_operation(root=self.root, kind="inventory_test") as operation:
                operation.watch_inventory(self.root / "study_logs", [])
                future = executor.submit(append_from_another_thread)
                self.assertTrue(entered.wait(timeout=2))
                with self.assertRaises(FutureTimeout):
                    future.result(timeout=0.1)
                self.assertEqual(self.rows(), [])
            self.assertTrue(future.result(timeout=3).exists())


class _State(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


class _Rerun(Exception):
    pass


class _TimerUI:
    def __init__(self, pending):
        self.session_state = _State(
            running=False, start_at=None, pending_session=deepcopy(pending),
            hourly_rate=1000, overtime_enabled=True, overtime_threshold=8.0,
            overtime_multiplier=1.25, pending_memo="keep this memo",
            selected_plan_task_id="task_synthetic",
        )
        self.errors = []

    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def expander(self, *_args, **_kwargs): return self
    def columns(self, count, **_kwargs): return [self] * count
    def button(self, label, **_kwargs): return label == "保存する"
    def caption(self, *_args, **_kwargs): pass
    def info(self, *_args, **_kwargs): pass
    def text_area(self, *_args, **_kwargs): return self.session_state.pending_memo
    def multiselect(self, *_args, **_kwargs): return []
    def checkbox(self, *_args, **_kwargs): return True
    def text_input(self, *_args, **_kwargs): return "synthetic session note"
    def error(self, message): self.errors.append(message)
    def rerun(self): raise _Rerun()


class TimerAtomicSaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        page = next((Path(__file__).resolve().parents[1] / "pages").glob("01_*.py"))
        tree = ast.parse(page.read_text(encoding="utf-8-sig"))
        functions = [deepcopy(node) for node in tree.body if isinstance(node, ast.FunctionDef)]
        for function in functions:
            function.decorator_list = []
        cls.code = compile(ast.Module(body=functions, type_ignores=[]), str(page), "exec")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_timer_atomic_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(json_store, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        clock_patcher = patch.object(data_utils, "app_today", return_value=date(2026, 1, 1))
        clock_patcher.start()
        self.addCleanup(clock_patcher.stop)
        data_utils.save_daily_plan(self.root, {
            "date": "2026-01-01", "tasks": [
                {"task_id": "task_synthetic", "title": "synthetic task", "linked_session_ids": []}
            ],
        })
        self.initial_live = self.live_files()
        self.pending = {
            "start_at": "2026-01-01T09:00:00", "end_at": "2026-01-01T10:00:00",
            "duration_sec": 3600, "hourly_rate": 1000,
        }
        self.ui = _TimerUI(self.pending)
        self.scope = {
            "Any": Any, "Path": Path, "date": date, "datetime": datetime,
            "st": self.ui, "ROOT": self.root, "STUDY_LOGS_DIR": self.root / "study_logs",
            "ab": SimpleNamespace(metric_block=Mock(), queue_success=Mock(),
                                  show_action_error=lambda message, **_kwargs: self.ui.error(message)),
            "calculate_session_money_with_daily_total": study_log_service.calculate_session_money_with_daily_total,
            "reserved_study_session_ids": study_log_service.reserved_study_session_ids,
            "append_study_session": study_log_service.append_study_session,
            **{name: getattr(data_utils, name) for name in (
                "load_all_notes", "generate_id", "load_study_sessions_file", "create_note_from_session",
                "link_notes_to_session", "link_session_to_plan_task",
            )},
        }
        exec(self.code, self.scope)
        # The real page initializes session state before rendering save controls.
        # Use that initializer so this isolated UI fixture keeps new timer fields
        # (including mini-window control revisions) without duplicating defaults.
        self.scope["init_state"]()

    def save(self):
        try:
            self.scope["render_pending_save"]({}, {})
        except _Rerun:
            pass

    def live_files(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*.json")
                if "backups" not in path.relative_to(self.root).parts}

    def test_final_append_failure_rolls_back_created_note_and_retains_input(self):
        self.scope["append_session"] = Mock(side_effect=OSError("synthetic final append failure"))
        self.save()
        self.assertEqual(self.live_files(), self.initial_live)
        self.assertTrue(self.ui.errors)
        self.assertEqual(self.ui.session_state.pending_memo, "keep this memo")
        pending = self.ui.session_state.pending_session
        self.assertTrue(pending["session_id"])
        self.assertTrue(pending["_save_operation_id"])

    def test_retry_reuses_operation_and_session_ids_without_duplicate_note(self):
        append = self.scope["append_session"]
        self.scope["append_session"] = Mock(side_effect=OSError("synthetic failure"))
        self.save()
        self.assertEqual(self.ui.session_state.miniwindow_control_revision, 0)
        identity = deepcopy(self.ui.session_state.pending_session)
        self.scope["append_session"] = append
        self.save()
        rows = study_log_service.load_study_sessions_by_date("2026-01-01", self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], identity["session_id"])
        self.assertNotIn("_save_operation_id", rows[0])
        notes = data_utils.load_note_store(self.root)["notebooks"][0]["sections"][0]["pages"]
        created = [note for note in notes if note["title"] == "synthetic session note"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["linked_study_session_ids"], [identity["session_id"]])
        plan = data_utils.load_daily_plan(self.root, "2026-01-01")
        self.assertEqual(plan["tasks"][0]["linked_session_ids"], [identity["session_id"]])
        self.assertIsNone(self.ui.session_state.pending_session)
        self.assertEqual(self.ui.session_state.miniwindow_control_revision, 1)

    def test_committed_operation_replay_does_not_duplicate_writes(self):
        append = self.scope["append_session"]
        captured = []
        def capture_and_append(session):
            captured.append(deepcopy(self.ui.session_state.pending_session))
            return append(session)
        self.scope["append_session"] = capture_and_append
        self.save()
        before = self.live_files()
        self.ui.session_state.pending_session = captured[0]
        self.scope["append_session"] = Mock(side_effect=AssertionError("replay must skip body"))
        self.save()
        self.assertEqual(self.live_files(), before)
        self.assertIsNone(self.ui.session_state.pending_session)

    def test_task_link_failure_does_not_publish_note_or_session(self):
        self.scope["link_session_to_plan_task"] = Mock(side_effect=json_store.JsonConflictError("synthetic task conflict"))
        self.save()
        self.assertEqual(self.live_files(), self.initial_live)
        self.assertTrue(self.ui.errors)
        self.assertIsNotNone(self.ui.session_state.pending_session)

    def test_live_study_write_failure_restores_note_and_plan_then_retries(self):
        original_save = json_store.save_json_safe
        target = study_log_service.get_study_log_path("2026-01-01", self.root)
        failed = False

        def injected_save(path, payload, **options):
            nonlocal failed
            if Path(path) == target and json_store._active_operation.get() is None and not failed:
                failed = True
                raise OSError("synthetic failure after note and plan writes")
            return original_save(path, payload, **options)

        with patch.object(json_store, "save_json_safe", side_effect=injected_save):
            self.save()
        self.assertTrue(failed)
        self.assertEqual(self.live_files(), self.initial_live)
        self.assertTrue(self.ui.errors)
        pending = deepcopy(self.ui.session_state.pending_session)
        self.save()
        rows = study_log_service.load_study_sessions_by_date("2026-01-01", self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], pending["session_id"])

    def test_note_deleted_after_options_load_stops_save_without_dropping_link(self):
        from app_core import json_operations

        store = data_utils.default_note_store()
        pages = store["notebooks"][0]["sections"][0]["pages"]
        pages[0].update(note_id="note_old", id="note_old", title="old")
        pages.append({**deepcopy(pages[0]), "note_id": "note_keep", "id": "note_keep", "title": "keep"})
        data_utils.save_note_store(self.root, store)
        self.ui.multiselect = lambda *a, **options: [next(label for label in options["options"] if "[note_old]" in label)]
        self.ui.checkbox = lambda *a, **options: False
        original_operation = json_operations.atomic_json_operation
        after_delete = {}

        @contextmanager
        def finish_concurrent_delete_before_operation(**options):
            current = data_utils.load_note_store(self.root)
            section = current["notebooks"][0]["sections"][0]
            section["pages"] = [page for page in section["pages"] if page["note_id"] != "note_old"]
            data_utils.save_note_store(self.root, current)
            after_delete.update(self.live_files())
            with original_operation(**options) as operation:
                yield operation

        with patch.object(json_operations, "atomic_json_operation", finish_concurrent_delete_before_operation):
            self.save()
        self.assertEqual(self.live_files(), after_delete)
        self.assertEqual(study_log_service.load_study_sessions_by_date("2026-01-01", self.root), [])
        self.assertTrue(any("削除" in error for error in self.ui.errors))
        self.assertIsNotNone(self.ui.session_state.pending_session)
        self.assertEqual(self.ui.session_state.pending_memo, "keep this memo")

    def test_existing_selected_note_and_session_links_commit_together(self):
        store = data_utils.default_note_store()
        note = store["notebooks"][0]["sections"][0]["pages"][0]
        note.update(note_id="note_selected", id="note_selected")
        data_utils.save_note_store(self.root, store)
        self.ui.multiselect = lambda *a, **options: [options["options"][0]]
        self.ui.checkbox = lambda *a, **options: False
        self.save()
        self.assertEqual(self.ui.errors, [])
        record = study_log_service.load_study_sessions_by_date("2026-01-01", self.root)[0]
        saved_note = data_utils.load_note_store(self.root)["notebooks"][0]["sections"][0]["pages"][0]
        self.assertEqual(record["linked_note_ids"], ["note_selected"])
        self.assertEqual(saved_note["linked_study_session_ids"], [record["session_id"]])


if __name__ == "__main__":
    unittest.main()
