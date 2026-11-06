from __future__ import annotations

import ast
from contextvars import Context
from copy import deepcopy
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import json_store
from app_core.daily_task_service import save_task_edit
from data_utils import daily_plan_path, load_daily_plan, save_daily_plan


PROJECT = Path(__file__).resolve().parents[1]


class DailyTaskSafetyTests(unittest.TestCase):
    target = date(2026, 9, 16)

    @classmethod
    def setUpClass(cls):
        from streamlit import config
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag_task_safety_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = daily_plan_path(self.root, self.target)
        plan = load_daily_plan(self.root, self.target)
        plan.update(custom={"retain": True}, consultation_memos=[{"custom": "untouched"}])
        plan["tasks"] = [{"task_id": f"t{i}", "title": f"task {i}", "subject": "old",
                          "status": "not_started", "custom": {"keep": i},
                          "linked_session_ids": [f"session{i}"]} for i in (1, 2)]
        save_daily_plan(self.root, plan)
        self.revision = json_store.observed_json_revision(self.path)
        self.operation_id = uuid4().hex

    def edit(self, updates, task_id="t1", revision=None, operation_id=None):
        save_task_edit(self.root, self.target, task_id, updates,
                       expected_revisions={self.path: revision or self.revision},
                       operation_id=operation_id or self.operation_id)

    def remote(self, callback):
        def write():
            plan = load_daily_plan(self.root, self.target)
            callback(plan)
            save_daily_plan(self.root, plan)
        Context().run(write)

    def test_success_preserves_other_fields_and_duplicate_retry(self):
        original = load_daily_plan(self.root, self.target)
        self.edit({"title": "changed"})
        saved = load_daily_plan(self.root, self.target)
        self.assertEqual(saved["custom"], original["custom"])
        self.assertEqual(saved["consultation_memos"], original["consultation_memos"])
        self.assertEqual(saved["tasks"][1], original["tasks"][1])
        self.assertEqual(saved["tasks"][0]["custom"], original["tasks"][0]["custom"])
        self.assertEqual(saved["tasks"][0]["linked_session_ids"], ["session1"])
        before = self.path.read_bytes()
        self.edit({"title": "changed"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_edits_addition_deletion_and_plan_memo_block_stale_save(self):
        changes = [lambda p: p["tasks"][0].update(status="done", subject="remote"),
                   lambda p: p["tasks"].append({"task_id": "t3", "title": "added"}),
                   lambda p: p["tasks"].pop(0),
                   lambda p: p["consultation_memos"].append({"custom": "new"})]
        for change in changes:
            with self.subTest(change=change):
                self.remote(change)
                before = self.path.read_bytes()
                for updates in ({"title": "stale"}, None):
                    with self.assertRaises(json_store.JsonConflictError):
                        self.edit(updates)
                    self.assertEqual(self.path.read_bytes(), before)

    def test_removed_task_is_not_recreated_from_a_current_revision(self):
        self.remote(lambda p: p["tasks"].pop(0))
        load_daily_plan(self.root, self.target)
        before = self.path.read_bytes()
        with self.assertRaises(json_store.JsonStoreError):
            self.edit({"title": "revive"}, revision=json_store.observed_json_revision(self.path))
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_failure_rolls_back(self):
        before = self.path.read_bytes()
        original = json_store.save_json_safe
        def fail(path, value, **kwargs):
            if Path(path) == self.path and json_store._committing_operation.get() is not None:
                raise OSError("synthetic write failure")
            return original(path, value, **kwargs)
        with patch.object(json_store, "save_json_safe", side_effect=fail):
            with self.assertRaises(json_store.JsonStoreError):
                self.edit({"title": "failed"})
        self.assertEqual(self.path.read_bytes(), before)
        self.edit({"title": "retry"})
        self.assertEqual(load_daily_plan(self.root, self.target)["tasks"][0]["title"], "retry")

    def test_unrelated_edit_preserves_inactive_or_unknown_classification(self):
        self.remote(lambda p: p["tasks"][0].update(project_id="inactive", subject_id="missing"))
        load_daily_plan(self.root, self.target)
        self.edit({"title": "renamed", "project_id": "inactive", "subject_id": "missing"},
                  revision=json_store.observed_json_revision(self.path))
        task = load_daily_plan(self.root, self.target)["tasks"][0]
        self.assertEqual((task["project_id"], task["subject_id"]), ("inactive", "missing"))

    def test_invalid_classification_change_is_rejected(self):
        before = self.path.read_bytes()
        with self.assertRaises(json_store.JsonStoreError):
            self.edit({"project_id": "missing"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_committed_request_replay_accepts_identical_canonical_payload_only(self):
        newly_saved = save_task_edit(self.root, self.target, "t1", {"title": "committed", "subject": "same"},
                                     expected_revisions={self.path: self.revision}, operation_id=self.operation_id)
        self.assertTrue(newly_saved)
        before = self.path.read_bytes()
        acknowledged = save_task_edit(self.root, self.target, "t1", {"subject": "same", "title": "committed"},
                                       expected_revisions={self.path: self.revision}, operation_id=self.operation_id)
        self.assertFalse(acknowledged)
        self.assertEqual(self.path.read_bytes(), before)
        for changed in ({"title": "changed later", "subject": "same"}, None):
            with self.subTest(changed=changed), self.assertRaises(json_store.JsonConflictError):
                save_task_edit(self.root, self.target, "t1", changed,
                               expected_revisions={self.path: self.revision}, operation_id=self.operation_id)
            self.assertEqual(self.path.read_bytes(), before)

    def test_committed_operation_id_cannot_be_reused_for_another_task_or_date(self):
        self.edit({"title": "committed"})
        another_date = date(2026, 9, 17)
        other_path = daily_plan_path(self.root, another_date)
        other_plan = load_daily_plan(self.root, another_date)
        other_plan["tasks"] = [{"task_id": "t1", "title": "another day"}]
        save_daily_plan(self.root, other_plan)
        another_revision = json_store.observed_json_revision(other_path)
        before = self.path.read_bytes(), other_path.read_bytes()
        for target, identifier, path, revision in ((self.target, "t2", self.path, self.revision),
                                                    (another_date, "t1", other_path, another_revision)):
            with self.subTest(target=target, task_id=identifier), self.assertRaises(json_store.JsonConflictError):
                save_task_edit(self.root, target, identifier, {"title": "committed"},
                               expected_revisions={path: revision}, operation_id=self.operation_id)
            self.assertEqual((self.path.read_bytes(), other_path.read_bytes()), before)

    def app_source(self, *, with_plan_actions=False):
        page = PROJECT / "pages/09_今日の学習プラン.py"
        tree = ast.parse(page.read_text(encoding="utf-8-sig"))
        ui_nodes = next(node.body for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "render_daily_inputs")
        names = {"plan_task_ids", "task_form_baseline", "visible_task_drafts", "master_options", "selected_label",
                 "master_selects", "note_titles", "session_rows", "sec_to_hms", "yen",
                 "save_plan_and_advance_tasks", "split_csv"}
        functions = [ast.unparse(node) for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        nodes, active = [], False
        for node in ui_nodes:
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "subheader"
                    and node.value.args and isinstance(node.value.args[0], ast.Constant)):
                if node.value.args[0].value == "今日のタスク一覧":
                    active = True
                elif node.value.args[0].value == "今日復習すべきカード":
                    break
            if active:
                nodes.append(ast.unparse(node))
        nodes.append(ast.unparse(next(node for node in ui_nodes
                                      if isinstance(node, ast.If) and "task_list_rerun" in ast.unparse(node.test))))
        if with_plan_actions:
            action_nodes = []
            in_base_and_carryover = False
            for node in ui_nodes:
                if isinstance(node, ast.With):
                    call = node.items[0].context_expr
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.args and isinstance(call.args[0], ast.Constant):
                        if call.func.attr == "expander" and call.args[0].value == "基本計画":
                            in_base_and_carryover = True
                        if call.func.attr == "form" and call.args[0].value == "new_task_form":
                            action_nodes.append(ast.unparse(node))
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "summary_cols" for target in node.targets):
                    in_base_and_carryover = False
                if in_base_and_carryover:
                    action_nodes.append(ast.unparse(node))
            nodes = action_nodes + nodes
        header = f'''
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from types import SimpleNamespace
import pandas as pd
import streamlit as st
from app_core.daily_task_service import save_task_edit
from app_core.form_snapshot import advance_form_snapshots_after_own_patch, discard_form_snapshot, form_snapshot
from app_core.json_store import observed_json_revision, resolved_json_path
from app_core.master_snapshot import MasterSnapshot
from data_utils import daily_plan_path, generate_id, load_daily_plan, load_all_study_sessions, save_daily_plan
ROOT=Path({str(self.root)!r})
target_date=date(2026,9,16)
plan=load_daily_plan(ROOT,target_date)
plan_revision=observed_json_revision(daily_plan_path(ROOT,target_date))
plan_action_rerun=False
target_minutes=int(plan.get("target_total_minutes",0))
MASTER_SNAPSHOT=MasterSnapshot({{"projects":[],"subjects":[],"materials":[],"units":[]}})
STATUS_LABELS={{"not_started":"未着手","in_progress":"進行中","done":"完了","pending":"保留"}}
LABEL_TO_STATUS={{v:k for k,v in STATUS_LABELS.items()}}
notes_by_id={{}}
sessions_by_id={{session.get("session_id"): session for session in load_all_study_sessions(ROOT) if session.get("session_id")}}
session_display_rows={{task.get("task_id", ""): {{"session_ids": tuple(task.get("linked_session_ids", [])),
                      "rows": [], "actual_seconds": 0}} for task in plan.get("tasks", [])}}
note_options={{}}
note_label_by_id={{}}
card_options={{}}
card_label_by_id={{}}
ab=SimpleNamespace(queue_success=lambda text: st.success(text),
                   show_action_error=lambda message, **k:st.error(message),
                   show_action_message=lambda message, kind="success", **k:getattr(st, kind)(message))
'''
        return header + "\n".join(functions + nodes)

    def app(self, *, with_plan_actions=False):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_string(self.app_source(with_plan_actions=with_plan_actions)).run(timeout=30)
        self.assertFalse(app.exception, str(app.exception))
        return app

    @staticmethod
    def field(app, prefix, task_id):
        return next(w for w in app.text_input if str(w.key).startswith(prefix) and f"_{task_id}_" in str(w.key))

    def test_actual_ui_blocks_stale_submit_and_preserves_input_until_reload(self):
        app = self.app()
        self.field(app, "title_", "t1").input("local title")
        self.remote(lambda p: p["tasks"][0].update(status="done", subject="remote"))
        before = self.path.read_bytes()
        for _ in range(2):
            next(w for w in app.button if w.label == "タスクを保存").click().run()
            self.assertFalse(app.exception, str(app.exception))
            self.assertTrue(app.error)
            self.assertEqual(self.field(app, "title_", "t1").value, "local title")
            self.assertEqual(self.path.read_bytes(), before)
        next(w for w in app.button if w.label == "このタスクの入力を破棄して最新データを再読込").click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(self.field(app, "subject_", "t1").value, "remote")

    def test_actual_ui_own_save_preserves_sibling_draft_and_advances_its_revision(self):
        app = self.app()
        self.field(app, "title_", "t1").input("first")
        self.field(app, "title_", "t2").input("second")
        [w for w in app.button if w.label == "タスクを保存"][0].click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(app.error, str(app.error))
        self.assertEqual(self.field(app, "title_", "t2").value, "second")
        [w for w in app.button if w.label == "タスクを保存"][1].click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(app.error, str(app.error))
        self.assertEqual([t["title"] for t in load_daily_plan(self.root, self.target)["tasks"]], ["first", "second"])

    def test_actual_ui_committed_retry_does_not_approve_remote_changed_sibling(self):
        from app_core import daily_task_service

        app = self.app()
        self.field(app, "title_", "t1").input("first local")
        self.field(app, "title_", "t2").input("second local")
        original_save = daily_task_service.save_task_edit

        def commit_then_lose_ack(*args, **kwargs):
            original_save(*args, **kwargs)
            raise OSError("synthetic acknowledgement loss after commit")

        with patch.object(daily_task_service, "save_task_edit", side_effect=commit_then_lose_ack):
            [w for w in app.button if w.label == "タスクを保存"][0].click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(app.error)
        self.assertEqual(load_daily_plan(self.root, self.target)["tasks"][0]["title"], "first local")
        self.remote(lambda p: p["tasks"][1].update(status="done", subject="remote"))
        after_remote = self.path.read_bytes()
        [w for w in app.button if w.label == "タスクを保存"][0].click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(app.error, str(app.error))
        self.assertEqual(self.path.read_bytes(), after_remote)
        self.assertEqual(self.field(app, "title_", "t2").value, "second local")
        [w for w in app.button if w.label == "タスクを保存"][1].click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(app.error)
        self.assertEqual(self.path.read_bytes(), after_remote)
        self.assertEqual(self.field(app, "title_", "t2").value, "second local")

    def _ui_after_committed_save_loses_ack(self):
        from app_core import daily_task_service

        app = self.app()
        self.field(app, "title_", "t1").input("first committed value")
        original_save = daily_task_service.save_task_edit

        def commit_then_lose_ack(*args, **kwargs):
            original_save(*args, **kwargs)
            raise OSError("synthetic acknowledgement loss after commit")

        with patch.object(daily_task_service, "save_task_edit", side_effect=commit_then_lose_ack):
            [item for item in app.button if item.label == "タスクを保存"][0].click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(app.error)
        self.assertEqual(load_daily_plan(self.root, self.target)["tasks"][0]["title"], "first committed value")
        return app

    def test_actual_ui_changed_payload_after_lost_ack_is_rejected_with_input_intact(self):
        app = self._ui_after_committed_save_loses_ack()
        key = f"daily_task:{self.target.isoformat()}:t1"
        operation = app.session_state[f"form_baseline:{key}"]["operation_id"]
        revision = deepcopy(app.session_state[key])
        before = self.path.read_bytes()
        self.field(app, "title_", "t1").input("new unsaved edit after acknowledgement loss")
        [item for item in app.button if item.label == "タスクを保存"][0].click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(app.error)
        self.assertFalse(app.success)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.field(app, "title_", "t1").value, "new unsaved edit after acknowledgement loss")
        self.assertEqual(app.session_state[f"form_baseline:{key}"]["operation_id"], operation)
        self.assertEqual(app.session_state[key], revision)

    def test_actual_ui_changed_delete_action_after_lost_ack_is_rejected_without_false_success(self):
        app = self._ui_after_committed_save_loses_ack()
        key = f"daily_task:{self.target.isoformat()}:t1"
        operation = app.session_state[f"form_baseline:{key}"]["operation_id"]
        before = self.path.read_bytes()
        self.field(app, "title_", "t1").input("draft still visible after rejected delete")
        next(item for item in app.checkbox if "delete_confirm_" in str(item.key) and "_t1_" in str(item.key)).check()
        [item for item in app.button if item.label == "タスクを削除"][0].click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(app.error)
        self.assertFalse(app.success)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.field(app, "title_", "t1").value, "draft still visible after rejected delete")
        self.assertEqual(app.session_state[f"form_baseline:{key}"]["operation_id"], operation)

    def test_actual_ui_identical_payload_after_lost_ack_acknowledges_without_second_write(self):
        app = self._ui_after_committed_save_loses_ack()
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        [item for item in app.button if item.label == "タスクを保存"][0].click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(app.error, str(app.error))
        self.assertTrue(app.success)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
        self.assertEqual(self.field(app, "title_", "t1").value, "first committed value")

    def test_actual_ui_remote_deleted_task_keeps_draft_without_resurrection(self):
        app = self.app()
        self.field(app, "title_", "t1").input("keep me")
        self.remote(lambda p: p["tasks"].pop(0))
        next(w for w in app.button if w.label == "タスクを保存").click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(app.error)
        self.assertEqual(self.field(app, "title_", "t1").value, "keep me")
        self.assertEqual([t["task_id"] for t in load_daily_plan(self.root, self.target)["tasks"]], ["t2"])

    def test_actual_ui_unconfirmed_delete_keeps_later_task_draft_without_writes(self):
        app = self.app()
        self.assertEqual(load_daily_plan(self.root, self.target)["tasks"][0]["linked_session_ids"], ["session1"])
        self.field(app, "title_", "t2").input("later task draft")
        before = self.path.read_bytes()
        before_files = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*.json")}
        [w for w in app.button if w.label == "タスクを削除"][0].click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertTrue(any("確認チェック" in item.value for item in app.warning))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual({str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*.json")}, before_files)
        self.assertEqual(self.field(app, "title_", "t2").value, "later task draft")
        app.run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual({str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*.json")}, before_files)
        self.assertEqual(self.field(app, "title_", "t2").value, "later task draft")

    def check_plan_action_preserves_draft(self, action):
        if action == "未完了タスクを今日に繰り越す":
            save_daily_plan(self.root, {"date": "2026-09-15", "tasks": [{"task_id": "previous_task", "title": "previous", "status": "not_started"}]})
        app = self.app(with_plan_actions=True)
        self.field(app, "title_", "t1").input("draft kept across action")
        next(w for w in app.button if w.label == action).click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(app.error, str(app.error))
        self.assertEqual(self.field(app, "title_", "t1").value, "draft kept across action")
        next(w for w in app.button if w.label == "タスクを保存").click().run()
        self.assertFalse(app.exception, str(app.exception))
        self.assertFalse(app.error, str(app.error))
        current = load_daily_plan(self.root, self.target)
        self.assertEqual(current["tasks"][0]["title"], "draft kept across action")
        if action != "基本計画を保存":
            self.assertEqual(len(current["tasks"]), 3)
            added_id = current["tasks"][-1]["task_id"]
            self.field(app, "title_", added_id).input("edit new task")
            [w for w in app.button if w.label == "タスクを保存"][-1].click().run()
            self.assertFalse(app.exception, str(app.exception))
            self.assertFalse(app.error, str(app.error))
            self.assertEqual(load_daily_plan(self.root, self.target)["tasks"][-1]["title"], "edit new task")

    def test_actual_ui_basic_plan_save_preserves_task_draft(self):
        self.check_plan_action_preserves_draft("基本計画を保存")

    def test_actual_ui_add_preserves_draft_and_binds_new_task_revision(self):
        self.check_plan_action_preserves_draft("タスクを追加")

    def test_actual_ui_carryover_preserves_draft_and_binds_new_task_revision(self):
        self.check_plan_action_preserves_draft("未完了タスクを今日に繰り越す")


if __name__ == "__main__":
    unittest.main()
