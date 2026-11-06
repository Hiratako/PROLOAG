"""Selected rhythm panels and fresh, isolated residual controls."""
from __future__ import annotations

import ast
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

import pandas as pd


PAGE = Path(__file__).resolve().parents[1] / "pages/16_カフェイン・運動分析.py"
VIEWS = ["カフェイン比較", "摂取タイミング", "勉強・体調", "翌日ラグ", "ランニング", "日別データ"]


class RhythmLazyAnalysisTests(TestCase):
    def setUp(self):
        self.tree = ast.parse(PAGE.read_text(encoding="utf-8-sig"))

    def panel(self, selected, *, show_tasks=True):
        start = next(i for i, node in enumerate(self.tree.body)
                     if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "analysis_view" for t in node.targets))
        nodes = self.tree.body[start:start + 2]
        self.assertIsInstance(nodes[1], ast.If)
        fake = Mock()
        fake.radio.return_value = selected
        fake.columns.side_effect = lambda count: [nullcontext() for _ in range(count)]
        frame = pd.DataFrame({"value": [1.0]})
        namespace = dict(st=fake, df=frame, start_date=date(2026, 9, 1), end_date=date(2026, 9, 17),
                         ROOT=Path("synthetic"), diary_context={"diary": []}, study_context={"study": []},
                         show_task_metrics=show_tasks, CYAN="cyan", GREEN="green", RED="red", PURPLE="purple", AMBER="amber")
        functions = (
            "render_band_compare", "render_group_table", "build_caffeine_event_frame", "build_session_timing_frame",
            "render_timing_analysis", "metric_card", "correlation_text", "render_caffeine_condition_scatter",
            "render_study_mix_chart", "render_condition_group_table", "render_lag_table", "render_running_scatter",
            "display_table", "to_csv_bytes",
        )
        for name in functions:
            namespace[name] = Mock(return_value=frame)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(PAGE), "exec"), namespace)
        self.assertEqual(fake.radio.call_args.args[1], VIEWS)
        self.assertEqual(fake.radio.call_args.kwargs["key"], "rhythm_analysis_view")
        return namespace

    def test_only_selected_panel_builds_its_data_and_graphs(self):
        expected = {
            "カフェイン比較": {"render_band_compare", "render_group_table"},
            "摂取タイミング": {"build_caffeine_event_frame", "build_session_timing_frame", "render_timing_analysis"},
            "勉強・体調": {"render_caffeine_condition_scatter", "render_study_mix_chart", "render_condition_group_table"},
            "翌日ラグ": {"render_lag_table"}, "ランニング": {"render_running_scatter"},
            "日別データ": {"display_table", "to_csv_bytes"},
        }
        functions = set().union(*expected.values())
        for selected in VIEWS:
            with self.subTest(selected=selected):
                state = self.panel(selected)
                for name in functions:
                    self.assertEqual(state[name].call_count, int(name in expected[selected]), name)

    def test_timing_reuses_current_diary_and_study_context(self):
        state = self.panel("摂取タイミング")
        self.assertIs(state["build_caffeine_event_frame"].call_args.kwargs["diary_cache"], state["diary_context"])
        call = state["build_session_timing_frame"].call_args
        self.assertIs(call.kwargs["preloaded_diaries"], state["diary_context"])
        self.assertIs(call.kwargs["preloaded_study_by_date"], state["study_context"])

    def test_task_visibility_is_forwarded_to_selected_condition_and_table(self):
        for selected, names in (("勉強・体調", ("render_study_mix_chart", "render_condition_group_table")),
                                ("日別データ", ("display_table",))):
            state = self.panel(selected, show_tasks=False)
            for name in names:
                self.assertFalse(state[name].call_args.kwargs["show_task_metrics"])
            if selected == "勉強・体調":
                labels = [call.args[0] for call in state["metric_card"].call_args_list]
                self.assertNotIn("カフェイン×達成率", labels)

    def residual_prefix(self):
        original = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_substance_residual")
        self.assertEqual([ast.unparse(node) for node in original.decorator_list], ["st.fragment"])
        # Run the real clock/day guard and profile loading; stop before UI setup.
        end = next(i for i, node in enumerate(original.body) if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                   and ast.unparse(node.value.func) == "st.subheader")
        copied = ast.FunctionDef(name=original.name, args=original.args, body=original.body[:end], decorator_list=[], returns=original.returns)
        state = dict(date=date, st=Mock(), app_now=Mock(), load_substance_profiles=Mock(return_value=[]))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[copied], type_ignores=[])), str(PAGE), "exec"), state)
        return state

    def test_residual_fragment_refreshes_time_and_profiles_each_interaction(self):
        state = self.residual_prefix()
        day = date(2026, 9, 17)
        state["app_now"].side_effect = [datetime(2026, 9, 17, 10), datetime(2026, 9, 17, 11)]
        for _ in range(2):
            state["render_substance_residual"](day)
        self.assertEqual(state["app_now"].call_count, 2)
        self.assertEqual(state["load_substance_profiles"].call_count, 2)
        state["st"].rerun.assert_not_called()

    def test_residual_day_rollover_requests_whole_page_refresh(self):
        state = self.residual_prefix()
        state["app_now"].return_value = datetime(2026, 9, 18, 0, 1)
        state["st"].rerun.side_effect = RuntimeError("rerun")
        with self.assertRaisesRegex(RuntimeError, "rerun"):
            state["render_substance_residual"](date(2026, 9, 17))
        state["load_substance_profiles"].assert_not_called()

    def test_heavy_daily_builders_are_outside_residual_fragment(self):
        function = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_substance_residual")
        calls = {ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)}
        self.assertFalse(calls & {"build_dashboard_frame", "build_session_timing_frame", "render_correlation_heatmap", "render_timeline"})
        top_calls = [node for node in self.tree.body if isinstance(node, (ast.Assign, ast.Expr))]
        eager = {ast.unparse(node.func) for item in top_calls for node in ast.walk(item) if isinstance(node, ast.Call)}
        self.assertFalse(eager & {"build_caffeine_event_frame", "build_session_timing_frame", "display_table"})
