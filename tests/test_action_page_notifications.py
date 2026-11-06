from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock


PAGE = Path(__file__).resolve().parents[1] / "pages/05_高度AI分析.py"


class AnswerUI:
    def __init__(self) -> None:
        self.session_state: dict[str, Any] = {}
        self.submitted = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def container(self):
        return self

    def markdown(self, *_args, **_kwargs):
        pass

    def caption(self, *_args, **_kwargs):
        pass

    def text_input(self, *_args, **_kwargs):
        return "keep answer draft"

    def button(self, *_args, **_kwargs):
        return self.submitted


class ActionPageNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(PAGE.read_text(encoding="utf-8-sig"))
        wanted = {"render_answer_form", "request_problem", "clear_problem_state"}
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        cls.code = compile(ast.Module(body=functions, type_ignores=[]), str(PAGE), "exec")

    def setUp(self):
        self.ui = AnswerUI()
        self.feedback = SimpleNamespace(
            show_action_message=Mock(), show_action_error=Mock(), show_action_toast=Mock(),
        )
        self.grade = {"all_correct": True, "score": 100}
        self.grader = Mock(return_value=self.grade)
        self.save = Mock(return_value={"attempt_id": "synthetic"})
        self.scope = {
            "st": self.ui, "ab": self.feedback, "Any": Any, "date": date,
            "ROOT": Path("synthetic-root"), "grade_problem": self.grader,
            "save_attempt": self.save, "generate_problem": Mock(side_effect=ValueError("synthetic missing condition")),
            "randbelow": lambda _upper: 1,
            **{name: name for name in (
                "CURRENT_GRADE_KEY", "CURRENT_ANSWERS_KEY", "CURRENT_PROBLEM_KEY", "CURRENT_ERROR_KEY",
                "SUMMARY_KEY", "SUMMARY_ERROR_KEY", "DATA_MODE_KEY", "TEMPLATE_KEY", "SELECTION_KEY",
            )},
        }
        exec(self.code, self.scope)
        self.problem = {"problem_id": "synthetic", "answer_parts": [{"key": "answer", "kind": "text"}]}

    def answer(self):
        self.scope["render_answer_form"](self.problem)

    def test_success_notifies_in_button_event_without_queued_or_extra_rerun(self):
        self.answer()
        self.save.assert_called_once()
        self.feedback.show_action_message.assert_called_once_with("採点結果を学習履歴に保存しました。")
        self.feedback.show_action_toast.assert_not_called()
        self.feedback.show_action_error.assert_not_called()
        self.ui.submitted = False
        self.answer()
        self.save.assert_called_once()
        self.feedback.show_action_message.assert_called_once()
        self.assertEqual(self.ui.session_state["CURRENT_ANSWERS_KEY"]["answer"], "keep answer draft")

    def test_separate_submissions_of_same_answer_each_receive_notice(self):
        self.answer()
        self.answer()
        self.assertEqual(self.save.call_count, 2)
        self.assertEqual(self.feedback.show_action_message.call_count, 2)

    def test_history_save_failure_keeps_grade_and_warns_once(self):
        self.save.side_effect = OSError("synthetic save failure")
        self.answer()
        notice = self.feedback.show_action_message.call_args
        self.assertEqual(notice.kwargs["kind"], "warning")
        self.assertEqual(notice.kwargs["toast_message"], "採点は完了しましたが、学習履歴を保存できませんでした。")
        self.assertIn("synthetic save failure", notice.args[0])
        self.assertEqual(self.ui.session_state["CURRENT_GRADE_KEY"], self.grade)
        self.ui.submitted = False
        self.answer()
        self.feedback.show_action_message.assert_called_once()
        self.save.assert_called_once()

    def test_grading_failure_notifies_without_history_save(self):
        self.grader.side_effect = ValueError("synthetic invalid answer")
        self.answer()
        self.feedback.show_action_error.assert_called_once()
        self.feedback.show_action_message.assert_not_called()
        self.save.assert_not_called()
        self.assertNotIn("CURRENT_GRADE_KEY", self.ui.session_state)

    def test_invalid_grade_notifies_without_history_save(self):
        self.grader.return_value = None
        self.answer()
        self.feedback.show_action_error.assert_called_once()
        self.save.assert_not_called()

    def test_problem_generation_failure_has_popup_and_preserves_persistent_notice(self):
        self.scope["request_problem"](date(2026, 10, 3), date(2026, 10, 3), "mixed")
        self.feedback.show_action_toast.assert_called_once()
        self.feedback.show_action_message.assert_not_called()
        self.assertEqual(self.ui.session_state["CURRENT_ERROR_KEY"], "synthetic missing condition")
        self.assertNotIn("CURRENT_PROBLEM_KEY", self.ui.session_state)


class SaveActionNotificationTests(unittest.TestCase):
    def setUp(self):
        self.state = {"unsubmitted_draft": "keep this input"}
        self.ui = SimpleNamespace(session_state=self.state, button=Mock(return_value=True),
                                  form_submit_button=Mock(return_value=True), rerun=Mock())
        self.feedback = SimpleNamespace(show_action_error=Mock(), show_action_message=Mock(), queue_success=Mock())
        self.target = date(2026, 10, 3)

    @staticmethod
    def action_branch(page_name, action_name):
        page = PAGE.parent / page_name
        tree = ast.parse(page.read_text(encoding="utf-8-sig"))
        candidates = [node for node in ast.walk(tree) if isinstance(node, ast.If) and any(
            isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == action_name
            for child in ast.walk(node)
        )]
        branch = min(candidates, key=lambda node: node.end_lineno - node.lineno)
        return compile(ast.Module(body=[branch], type_ignores=[]), str(page), "exec")

    def scope(self, action_name, action):
        return {
            "st": self.ui, "ab": self.feedback, "target_date": self.target, "submitted": True,
            action_name: action, "bed_time": time(23, 30), "wake_time": time(6, 30),
            "sleep_duration_min": 420, "sleep_quality": 3, "morning_sleepiness": 2,
            "midnight_awakenings": 0, "nap_minutes": 0, "physical_score": 3, "fatigue": 2,
            "stress": 2, "focus_score": 4, "mood_score": 4, "memo": "keep memo",
            "exercise_type": "running", "title": "keep exercise title", "start_time": time(7),
            "duration_min": 30, "distance_km": 4.0, "calculated_pace": 7.5,
            "intensity": 3, "fatigue_after": 2, "record": {"exercise_id": "synthetic"}, "index": 0,
        }

    def test_health_and_exercise_failures_notify_without_success_rerun_or_draft_clear(self):
        for page, name in (
            ("12_睡眠・体調ログ.py", "save_health_log"),
            ("13_運動ログ.py", "add_exercise_record"),
            ("13_運動ログ.py", "delete_exercise_record"),
        ):
            for exception in (OSError("synthetic write failure"), ValueError("synthetic revision conflict")):
                with self.subTest(action=name, failure=type(exception).__name__):
                    action = Mock(side_effect=exception)
                    self.feedback.show_action_error.reset_mock()
                    exec(self.action_branch(page, name), self.scope(name, action))
                    action.assert_called_once()
                    self.feedback.show_action_error.assert_called_once()
                    self.feedback.queue_success.assert_not_called()
                    self.ui.rerun.assert_not_called()
                    self.assertEqual(self.state, {"unsubmitted_draft": "keep this input"})

    def test_health_and_exercise_success_queue_notice_only_after_service_completion(self):
        for page, name, result in (
            ("12_睡眠・体調ログ.py", "save_health_log", None),
            ("13_運動ログ.py", "add_exercise_record", {"exercise_id": "synthetic"}),
            ("13_運動ログ.py", "delete_exercise_record", True),
        ):
            with self.subTest(action=name):
                self.feedback.queue_success.reset_mock()
                self.ui.rerun.reset_mock()
                action = Mock(return_value=result)
                exec(self.action_branch(page, name), self.scope(name, action))
                action.assert_called_once()
                self.feedback.queue_success.assert_called_once()
                self.ui.rerun.assert_called_once()
                self.feedback.show_action_error.assert_not_called()

    def test_missing_exercise_delete_warns_without_success_notice(self):
        action = Mock(return_value=False)
        exec(self.action_branch("13_運動ログ.py", "delete_exercise_record"), self.scope("delete_exercise_record", action))
        self.feedback.show_action_message.assert_called_once()
        self.assertEqual(self.feedback.show_action_message.call_args.kwargs["kind"], "warning")
        self.feedback.queue_success.assert_not_called()
        self.ui.rerun.assert_not_called()

    def test_failed_task_addition_restores_display_plan_and_retains_draft(self):
        page = PAGE.parent / "09_今日の学習プラン.py"
        tree = ast.parse(page.read_text(encoding="utf-8-sig"))
        branch = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                      and isinstance(node.test, ast.Call) and isinstance(node.test.func, ast.Attribute)
                      and node.test.func.attr == "form_submit_button" and node.test.args
                      and isinstance(node.test.args[0], ast.Constant) and node.test.args[0].value == "タスクを追加")
        original = {"date": "2026-10-03", "tasks": []}
        scope = {"st": self.ui, "ab": self.feedback, "deepcopy": deepcopy, "plan": deepcopy(original),
                 "generate_id": lambda *_args: "new_synthetic", "title": "keep task title",
                 "subject": "keep subject", "material": "keep material", "planned_minutes": 60,
                 "selected_note_labels": [], "note_options": {}, "new_master_values": {},
                 "save_plan_and_advance_tasks": Mock(side_effect=ValueError("synthetic conflict")),
                 "plan_revision": "before", "plan_action_rerun": False}
        exec(compile(ast.Module(body=[branch], type_ignores=[]), str(page), "exec"), scope)
        self.assertEqual(scope["plan"], original)
        self.assertEqual(scope["title"], "keep task title")
        self.assertEqual(scope["plan_revision"], "before")
        self.assertFalse(scope["plan_action_rerun"])
        self.feedback.show_action_error.assert_called_once()
        self.feedback.queue_success.assert_not_called()

    def reminder_action(self, result=None, *, error=None):
        page = PAGE.parent / "08_忘却曲線リマインダー.py"
        tree = ast.parse(page.read_text(encoding="utf-8-sig"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "save_reminder_action")
        scope = {"st": self.ui, "ab": self.feedback, "Any": Any}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(page), "exec"), scope)
        clear = Mock()
        scope["save_reminder_action"](Mock(return_value=result, side_effect=error), "復習予定を更新しました。", on_success=clear)
        return clear

    def test_reminder_failure_preserves_draft_without_success_notice(self):
        clear = self.reminder_action(error=OSError("synthetic failure"))
        clear.assert_not_called()
        self.feedback.show_action_error.assert_called_once()
        self.feedback.queue_success.assert_not_called()
        self.ui.rerun.assert_not_called()

    def test_missing_reminder_warns_and_preserves_draft(self):
        clear = self.reminder_action(result=False)
        clear.assert_not_called()
        self.feedback.show_action_message.assert_called_once()
        self.feedback.queue_success.assert_not_called()
        self.ui.rerun.assert_not_called()

    def test_successful_reminder_clears_draft_then_queues_notice(self):
        clear = self.reminder_action(result={"reminder_id": "synthetic"})
        clear.assert_called_once()
        self.feedback.queue_success.assert_called_once_with("復習予定を更新しました。")
        self.ui.rerun.assert_called_once()
        self.feedback.show_action_error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
