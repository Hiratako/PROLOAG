from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from app_core import study_log_editor_ui as ui


class Rerun(BaseException):
    pass


class FakeUI:
    def __init__(self, *, clicked: str = "") -> None:
        self.session_state = {"study_log_edit_selected_id": "session_test", "draft_memo": "修正中"}
        self.clicked = clicked
        self.choice = None
        self.successes = []
        self.errors = []
        self.toasts = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def expander(self, *_args, **_kwargs):
        return self

    def columns(self, spec):
        return [self] * (spec if isinstance(spec, int) else len(spec))

    def button(self, label, **_kwargs):
        return label == self.clicked

    def checkbox(self, *_args, **_kwargs):
        return True

    def selectbox(self, _label, _options, *, key, **_kwargs):
        if self.choice is not None:
            self.session_state[key] = self.choice
        return self.session_state[key]

    def radio(self, _label, options, **_kwargs):
        return options[0]

    def success(self, message):
        self.successes.append(message)

    def error(self, message):
        self.errors.append(message)

    def toast(self, message, **kwargs):
        self.toasts.append((message, kwargs))

    def rerun(self):
        raise Rerun()

    def noop(self, *_args, **_kwargs):
        pass

    caption = subheader = markdown = dataframe = info = warning = code = noop


class StudyLogEditorFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("unused-feedback-test-root")
        self.record = {"session_id": "session_test", "memo": "保存済み", "duration_sec": 4500}
        self.state = {
            "selected_id": "session_test", "generation": 4, "mode": "修正",
            "snapshot": {"session": {**self.record, "memo": "変更前"}},
            "plan": {"kind": "edit", "before": {**self.record, "memo": "変更前"},
                     "after": self.record, "changed_files": ["study_logs/test.json"]},
        }

    def save(self, fake, *, error=None):
        with patch.object(ui, "st", fake), patch.object(ui, "commit_session_change", side_effect=error,
                return_value={"backup_path": "operation-backup"}) as commit, patch.object(ui.ab, "st", fake), \
                patch.object(ui.ab, "queue_success") as queue, patch.object(ui.ab, "queue_toast") as toast_queue:
            if error:
                ui._show_plan(self.state, self.root, "test")
            else:
                with self.assertRaises(Rerun):
                    ui._show_plan(self.state, self.root, "test")
            commit.assert_called_once()
            if error or self.state.get("save_notice") is None:
                toast_queue.assert_not_called()
            else:
                toast_queue.assert_called_once_with("勉強記録を保存しました。")
            return queue.call_count

    def test_success_defers_selector_reset_and_keeps_backup(self):
        fake = FakeUI(clicked="変更を保存する")
        self.assertEqual(self.save(fake), 0)
        self.assertEqual(fake.session_state["study_log_edit_selected_id"], "session_test")
        self.assertTrue(self.state["clear_selection_after_save"])
        self.assertIsNone(self.state["snapshot"])
        self.assertIsNone(self.state["plan"])
        self.assertEqual(self.state["generation"], 5)
        self.assertEqual(self.state["last_backup_path"], "operation-backup")

    def test_next_render_is_empty_even_if_saved_record_left_filter(self):
        for records in ([self.record], []):
            with self.subTest(records=bool(records)):
                self.setUp()
                fake = FakeUI(clicked="変更を保存する")
                self.save(fake)
                fake.clicked = ""
                with patch.object(ui, "st", fake), patch.object(ui, "load_editable_session") as load, patch.object(ui, "_edit_inputs") as inputs:
                    ui._render_record_editor(records, self.state, self.root, {})
                    ui._render_record_editor(records, self.state, self.root, {})
                self.assertEqual(fake.session_state["study_log_edit_selected_id"], "")
                self.assertEqual(self.state["selected_id"], "")
                self.assertNotIn("clear_selection_after_save", self.state)
                self.assertNotIn("mode", self.state)
                load.assert_not_called()
                inputs.assert_not_called()
                self.assertEqual(len(fake.successes), 2)
                self.assertIn("編集フォームをクリア", fake.successes[0])

    def test_reselect_loads_saved_record_with_new_widget_generation(self):
        fake = FakeUI(clicked="変更を保存する")
        self.save(fake)
        fake.clicked = ""
        with patch.object(ui, "st", fake):
            ui._render_record_editor([self.record], self.state, self.root, {})
        fake.choice = "session_test"
        snapshot = {"session": self.record, "source_file": "study_logs/test.json"}
        with patch.object(ui, "st", fake), patch.object(ui, "load_editable_session", return_value=snapshot) as load, patch.object(ui, "_edit_inputs") as inputs:
            ui._render_record_editor([self.record], self.state, self.root, {})
        load.assert_called_once_with("session_test", root=self.root)
        self.assertEqual(inputs.call_args.args[0]["session"], self.record)
        self.assertTrue(inputs.call_args.args[-1].endswith("_6"))
        self.assertNotIn("save_notice", self.state)

    def test_save_failure_preserves_selection_preview_and_draft(self):
        for error in (OSError("保存失敗"), ui.JsonStoreError("別の更新があります")):
            with self.subTest(error=type(error).__name__):
                fake = FakeUI(clicked="変更を保存する")
                before = deepcopy(self.state)
                self.assertEqual(self.save(fake, error=error), 0)
                self.assertEqual(self.state, before)
                self.assertEqual(fake.session_state["study_log_edit_selected_id"], "session_test")
                self.assertEqual(fake.session_state["draft_memo"], "修正中")
                self.assertTrue(fake.errors)
                self.assertEqual(len(fake.toasts), 1)
                self.assertIn("更新を完了できませんでした", fake.toasts[0][0])

    def test_cancelling_preview_keeps_selection_and_draft(self):
        fake = FakeUI(clicked="修正入力へ戻る")
        with patch.object(ui, "st", fake), patch.object(ui, "commit_session_change") as commit:
            with self.assertRaises(Rerun):
                ui._show_plan(self.state, self.root, "test")
        commit.assert_not_called()
        self.assertIsNone(self.state["plan"])
        self.assertIsNotNone(self.state["snapshot"])
        self.assertNotIn("clear_selection_after_save", self.state)
        self.assertEqual(fake.session_state["draft_memo"], "修正中")

    def test_delete_and_restore_keep_existing_feedback_behavior(self):
        for kind, label in (("delete", "ごみ箱へ移動"), ("restore", "記録を復元する")):
            with self.subTest(kind=kind):
                self.setUp()
                self.state["plan"]["kind"] = kind
                fake = FakeUI(clicked=label)
                self.assertEqual(self.save(fake), 1)
                self.assertNotIn("clear_selection_after_save", self.state)
                self.assertNotIn("save_notice", self.state)


if __name__ == "__main__":
    unittest.main()
