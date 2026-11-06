from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import app_clock, json_store, settings_service
from app_core import statistics_practice_service as service


PAGE = Path(__file__).resolve().parents[1] / "pages/05_高度AI分析.py"
VIEW = "statistics_practice_view"
PROBLEM = "statistics_practice_current_problem"
GRADE = "statistics_practice_current_grade"
DETAIL = "statistics_history_detail_visible"
PROGRESS = "statistics_history_progress_visible"


class StatisticsLazyUiTests(unittest.TestCase):
    """Real page widgets, fixed synthetic snapshots, no personal or network I/O."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-statistics-lazy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.today = date(2026, 9, 17)
        self.attempts = []
        for name, patcher in (
            ("read", patch.object(json_store, "_read", side_effect=AssertionError("No source JSON reads"))),
            ("settings", patch.object(settings_service, "load_user_settings", return_value=deepcopy(settings_service.DEFAULT_USER_SETTINGS))),
            ("network", patch("socket.socket.connect", side_effect=AssertionError("No network"))),
            ("clock", patch.object(app_clock, "app_today", return_value=self.today)),
            ("load", patch.object(service, "load_attempts", side_effect=lambda **kwargs: deepcopy(self.attempts))),
            ("save", patch.object(service, "save_attempt", return_value={})),
            ("generate", patch.object(service, "generate_problem", wraps=service.generate_problem)),
            ("progress", patch.object(service, "practice_progress", wraps=service.practice_progress)),
            ("summary", patch.object(service, "available_problem_summary", wraps=service.available_problem_summary)),
        ):
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def problem(self, template="kaplan_meier", seed=19):
        return service.generate_problem(self.today, self.today, data_mode="synthetic",
                                        template_id=template, seed=seed, root=self.root)

    @staticmethod
    def answers(problem):
        return {part["key"]: part.get("expected_text", part["expected"])
                for part in problem["answer_parts"]}

    def attempt(self, problem, key="attempt_1"):
        return {"attempt_id": key, "attempted_at": "2026-09-17T10:00:00",
                "template_id": problem["template_id"], "title": problem["title"],
                "topic": problem["topic"], "period": problem["period"],
                "problem_snapshot": problem, "grade": service.grade_problem(problem, self.answers(problem))}

    def app(self, *, problem=None, grade=None, view="演習", source_path=PAGE):
        from streamlit import config
        from streamlit.testing.v1 import AppTest
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        source = source_path.read_text(encoding="utf-8-sig")
        source = source.replace("ROOT = Path(__file__).resolve().parents[1]", f"ROOT = Path({str(self.root)!r})")
        source = source.replace("import prolog_ui.components as ab", """class _Decoration:
    def __getattr__(self, name):
        if name == 'theme_palette':
            return lambda *args, **kwargs: {}
        return lambda *args, **kwargs: None
ab = _Decoration()""")
        for name in ("render_problem_data", "render_grade", "render_exercise_tab", "render_history_tab", "render_scope_tab"):
            tree = ast.parse(source)
            node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
            lines = source.splitlines()
            lines.insert(node.body[0].lineno - 1, f'    st.session_state["_calls_{name}"] = st.session_state.get("_calls_{name}", 0) + 1')
            source = "\n".join(lines) + "\n"
        app = AppTest.from_string(source, default_timeout=30)
        app.session_state[VIEW] = view
        if problem:
            app.session_state[PROBLEM] = deepcopy(problem)
        if grade:
            app.session_state[GRADE] = deepcopy(grade)
        return self.run_app(app)

    def run_app(self, app):
        app.run(timeout=30)
        self.assertFalse(app.exception, [item.message for item in app.exception])
        self.read.assert_not_called()
        self.network.assert_not_called()
        return app

    def switch(self, app, view):
        app.radio(key=VIEW).set_value(view)
        return self.run_app(app)

    @staticmethod
    def count(app, name):
        return app.session_state.filtered_state.get(f"_calls_{name}", 0)

    @staticmethod
    def button(app, label):
        return next(item for item in app.button if item.label == label)

    def test_only_selected_view_runs_and_closed_history_has_no_details(self):
        problem = self.problem()
        self.attempts = [self.attempt(problem)]
        self.generate.reset_mock()
        app = self.app(problem=problem)
        self.assertEqual(self.count(app, "render_exercise_tab"), 1)
        self.assertEqual(self.count(app, "render_history_tab"), 0)
        self.assertEqual(self.count(app, "render_scope_tab"), 0)
        self.load.assert_not_called()
        self.progress.assert_not_called()
        self.assertEqual(self.count(app, "render_grade"), 0)
        self.switch(app, "学習履歴")
        self.assertEqual(self.load.call_count, 1)
        self.assertEqual(self.count(app, "render_problem_data"), 1)
        self.assertEqual(self.count(app, "render_grade"), 0)
        self.progress.assert_not_called()
        app.checkbox(key=DETAIL).check()
        self.run_app(app)
        self.assertEqual(self.count(app, "render_problem_data"), 2)
        self.assertEqual(self.count(app, "render_grade"), 1)
        self.progress.assert_not_called()
        app.checkbox(key=DETAIL).uncheck()
        app.checkbox(key=PROGRESS).check()
        self.run_app(app)
        self.assertEqual(self.count(app, "render_grade"), 1)
        self.assertEqual(self.progress.call_count, 1)
        before = (self.load.call_count, self.progress.call_count)
        self.switch(app, "収録範囲")
        self.assertEqual((self.load.call_count, self.progress.call_count), before)
        self.assertEqual(self.count(app, "render_scope_tab"), 1)
        self.assertEqual(self.count(app, "render_grade"), 1)
        self.generate.assert_not_called()
        self.summary.assert_not_called()
        self.save.assert_not_called()

    def test_numeric_choice_text_and_legacy_drafts_survive_before_grading(self):
        problem = self.problem()
        problem["answer_parts"] = [
            service._numeric_part("number", "数値", 1.25, domain="real"),
            {"key": "choice", "kind": "choice", "label": "選択", "options": ["A", "B"], "expected": "B"},
            {"key": "text", "kind": "text", "label": "説明", "expected": "下書き"},
            {"key": "legacy", "kind": "numeric", "label": "旧形式", "decimals": 2, "expected": 2.5},
        ]
        app = self.app(problem=problem)
        prefix = f"statistics_answer_{problem['problem_id']}_"
        app.text_input(key=prefix + "number").set_value("1.2500")
        app.selectbox(key=prefix + "choice").set_value("B")
        app.text_input(key=prefix + "text").set_value("採点前の下書き")
        app.number_input(key=prefix + "legacy").set_value(2.5)
        # Submit widget state with the navigation interaction, without grading.
        self.switch(app, "学習履歴")
        self.switch(app, "収録範囲")
        self.switch(app, "演習")
        self.assertEqual(app.text_input(key=prefix + "number").value, "1.2500")
        self.assertEqual(app.selectbox(key=prefix + "choice").value, "B")
        self.assertEqual(app.text_input(key=prefix + "text").value, "採点前の下書き")
        self.assertEqual(app.number_input(key=prefix + "legacy").value, 2.5)
        self.assertEqual(app.session_state[PROBLEM], problem)
        self.assertNotIn(GRADE, app.session_state.filtered_state)
        self.save.assert_not_called()

    def test_dates_and_generation_conditions_survive_switch_without_generating(self):
        app = self.app()
        app.date_input(key="statistics_practice_start_date").set_value(date(2026, 1, 2))
        app.date_input(key="statistics_practice_end_date").set_value(date(2026, 8, 3))
        self.switch(app, "収録範囲")
        self.switch(app, "演習")
        app.selectbox(key="statistics_practice_data_mode").set_value("synthetic")
        self.run_app(app)
        app.selectbox(key="statistics_practice_topic").set_value("survival")
        self.run_app(app)
        app.selectbox(key="statistics_practice_template").set_value("kaplan_meier")
        app.selectbox(key="statistics_practice_selection").set_value("weak")
        self.switch(app, "収録範囲")
        self.switch(app, "演習")
        for key, expected in {"data_mode": "synthetic", "topic": "survival", "template": "kaplan_meier", "selection": "weak"}.items():
            self.assertEqual(app.selectbox(key=f"statistics_practice_{key}").value, expected)
        self.assertEqual(app.date_input(key="statistics_practice_start_date").value, date(2026, 1, 2))
        self.assertEqual(app.date_input(key="statistics_practice_end_date").value, date(2026, 8, 3))
        self.generate.assert_not_called()
        self.summary.assert_not_called()
        self.save.assert_not_called()
        self.button(app, "問題を生成").click()
        self.run_app(app)
        self.assertEqual(self.generate.call_count, 1)
        self.assertEqual(app.session_state[PROBLEM]["template_id"], "kaplan_meier")
        self.save.assert_not_called()

    def test_grade_answers_and_history_selection_survive_switch_without_resaving(self):
        problem = self.problem()
        self.attempts = [self.attempt(problem, "first"), self.attempt(self.problem(seed=27), "second")]
        app = self.app(problem=problem)
        answers = self.answers(problem)
        for part in problem["answer_parts"]:
            widget = app.selectbox if part["kind"] == "choice" else app.text_input
            widget(key=f"statistics_answer_{problem['problem_id']}_{part['key']}").set_value(answers[part["key"]])
        self.button(app, "採点する").click()
        self.run_app(app)
        expected_grade = service.grade_problem(problem, answers)
        self.assertEqual(app.session_state[GRADE], expected_grade)
        self.save.assert_called_once_with(problem, answers, expected_grade, root=self.root)
        self.switch(app, "学習履歴")
        app.checkbox(key=DETAIL).check()
        self.run_app(app)
        app.selectbox(key="statistics_history_detail").set_value("second")
        self.run_app(app)
        self.switch(app, "収録範囲")
        self.switch(app, "学習履歴")
        self.assertTrue(app.checkbox(key=DETAIL).value)
        self.assertEqual(app.selectbox(key="statistics_history_detail").value, "second")
        self.switch(app, "演習")
        self.assertEqual(app.session_state[GRADE], expected_grade)
        self.assertEqual(app.session_state[PROBLEM], problem)
        self.assertEqual(app.session_state["statistics_practice_current_answers"], answers)
        self.assertEqual(self.save.call_count, 1)

    def test_history_reconciles_removed_selection_and_empty_identifiers(self):
        problem = self.problem()
        self.attempts = [self.attempt(problem, "first"), self.attempt(problem, "second")]
        app = self.app(view="学習履歴")
        app.checkbox(key=DETAIL).check()
        self.run_app(app)
        app.selectbox(key="statistics_history_detail").set_value("second")
        self.run_app(app)
        self.attempts.pop()
        self.run_app(app)
        self.assertEqual(app.selectbox(key="statistics_history_detail").value, "first")
        self.attempts[0].pop("attempt_id")
        self.run_app(app)
        self.assertTrue(any("詳細を表示できる" in item.value for item in app.info))
        self.save.assert_not_called()

    def test_new_problem_clears_previous_drafts_but_keeps_conditions(self):
        problem = self.problem()
        app = self.app(problem=problem)
        prefix = f"statistics_answer_{problem['problem_id']}_"
        app.text_input(key=prefix + problem["answer_parts"][0]["key"]).set_value("0.125")
        app.selectbox(key="statistics_practice_data_mode").set_value("synthetic")
        self.run_app(app)
        app.selectbox(key="statistics_practice_template").set_value("kaplan_meier")
        self.button(app, "問題を生成").click()
        self.run_app(app)
        self.assertNotEqual(app.session_state[PROBLEM]["problem_id"], problem["problem_id"])
        self.assertFalse(any(key.startswith(prefix) for key in app.session_state.filtered_state))
        self.assertEqual(app.selectbox(key="statistics_practice_template").value, "kaplan_meier")
        self.save.assert_not_called()

    def test_answer_edits_are_fragment_scoped_not_page_scoped(self):
        # AppTest currently performs full runs; inspect the production fragment
        # boundary rather than claiming browser fragment dispatch was measured.
        tree = ast.parse(PAGE.read_text(encoding="utf-8-sig"))
        answer_ui = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_exercise_answers")
        self.assertIn("st.fragment", [ast.unparse(item) for item in answer_ui.decorator_list])
        calls = [ast.unparse(node.func) for node in ast.walk(answer_ui) if isinstance(node, ast.Call)]
        self.assertIn("render_answer_form", calls)
        self.assertIn("render_grade", calls)
        self.assertNotIn("render_problem_data", calls)
        self.assertNotIn("load_attempts", calls)

    def test_next_problem_resets_grade_and_draft_only_after_button(self):
        problem = self.problem()
        grade = service.grade_problem(problem, self.answers(problem))
        app = self.app(problem=problem, grade=grade)
        app.selectbox(key="statistics_practice_data_mode").set_value("synthetic")
        self.run_app(app)
        app.selectbox(key="statistics_practice_template").set_value("kaplan_meier")
        self.run_app(app)
        self.assertEqual(app.session_state[PROBLEM], problem)
        self.assertEqual(app.session_state[GRADE], grade)
        self.button(app, "同じ条件で次の問題").click()
        self.run_app(app)
        self.assertNotIn(GRADE, app.session_state.filtered_state)
        self.assertNotEqual(app.session_state[PROBLEM]["problem_id"], problem["problem_id"])
        self.assertFalse(any(key.startswith(f"statistics_answer_{problem['problem_id']}_")
                             for key in app.session_state.filtered_state))
        self.save.assert_not_called()

    def test_baseline_tables_explanation_and_grade_are_unchanged(self):
        baseline = PAGE.parents[1] / "backups/page_performance_phase2_20260917_084422/pages/05_高度AI分析.py"
        if not baseline.exists():
            self.skipTest("Phase-two baseline source is not present in this checkout")
        problem = self.problem()
        grade = service.grade_problem(problem, self.answers(problem))
        self.attempts = [self.attempt(problem)]
        old = self.app(problem=problem, grade=grade, source_path=baseline)
        old_tables = [item.value for item in old.dataframe]
        current = self.app(problem=problem, grade=grade)
        for table in current.dataframe:
            self.assertTrue(any(table.value.equals(previous) for previous in old_tables))
        self.assertIn(problem["explanation"], [item.value for item in current.markdown])
        self.switch(current, "学習履歴")
        current.checkbox(key=DETAIL).check()
        current.checkbox(key=PROGRESS).check()
        self.run_app(current)
        for table in current.dataframe:
            self.assertTrue(any(table.value.equals(previous) for previous in old_tables))
        self.assertIn(problem["explanation"], [item.value for item in current.markdown])


if __name__ == "__main__":
    unittest.main()
