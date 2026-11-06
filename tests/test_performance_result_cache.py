from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from app_core import app_clock, json_store, settings_service
from app_core import performance_analysis_service as service
from app_core.performance_result_cache import PerformanceResultCache
from app_core.study_category_service import default_category_catalog


def synthetic_frame(days: int = 30) -> pd.DataFrame:
    """Numerical fixtures only: never consult the user's records/settings."""
    rng = np.random.default_rng(91017 + days)
    dates = pd.date_range("2026-01-01", periods=days)
    frame = pd.DataFrame({
        "date": dates, "weekday": dates.weekday, "is_weekend": dates.weekday >= 5,
        "diary_recorded": True, "caffeine_imputed_zero": False,
        "caffeine_mg": np.where(np.arange(days) % 2, 100.0, 0.0),
        "running_recorded": np.arange(days) % 3 == 0,
        "running_status_recorded": True, "exercise_log_exists": True,
        "good_seat_status": np.resize(["yes", "no", "so_so", None], days),
        "study_recorded": True, "health_recorded": True, "plan_recorded": True,
        "_analysis_outcome": rng.uniform(0, 8, days),
        "next_focus_score": rng.uniform(1, 5, days),
        "next_condition_balance": rng.normal(size=days),
    })
    for name in ("sleep_hours", "sleep_quality", "morning_sleepiness", "previous_fatigue",
                 "planned_hours", "prior_7d_study_hours", "previous_study_hours", "avg_effort",
                 "output_ratio", "night_study_hours", "study_hours", "output_hours",
                 "plan_execution_rate", "task_completion_rate", "focus_score"):
        frame[name] = rng.uniform(1, 8, days)
    frame["caffeine_status"] = np.where(frame["caffeine_mg"] > 0, "1-100mg", "0mgまたは初期値")
    return frame


class ComparableDayArrayTests(unittest.TestCase):
    def test_equal_distance_keeps_set_order_and_removes_each_candidate_once(self):
        frame = synthetic_frame(8)
        frame.index = [101, 33, 74, 8, 91, 12, 63, 5]
        frame["date"] = pd.Timestamp("2026-01-01")
        frame["is_weekend"] = False
        frame["caffeine_mg"] = [100] * 4 + [0] * 4
        for column in service.factor_groups(frame, "caffeine")[1]["matching_columns"]:
            frame[column] = [0, 0, 1, 1] * 2
        frame["_analysis_outcome"] = np.arange(8, dtype=float)
        available = set(frame.index[4:])
        expected = []
        for source in frame.index[:4]:
            best = next(index for index in available if frame.at[index, "sleep_hours"] == frame.at[source, "sleep_hours"])
            available.remove(best)
            expected.append(float(frame.at[best, "_analysis_outcome"]))
        result = service.find_comparable_days(frame, "caffeine", "_analysis_outcome")
        self.assertEqual(result["pairs"]["condition_b_value"].tolist(), expected)
        self.assertTrue(result["pairs"]["matching_distance"].eq(0).all())
        self.assertEqual(result["pairs"]["condition_b_value"].nunique(), 4)

    def test_distance_boundary_includes_exactly_one_point_five(self):
        frame = synthetic_frame(6)
        frame["date"] = pd.Timestamp("2026-01-01")
        frame["is_weekend"] = False
        frame["caffeine_mg"] = [100] * 3 + [0] * 3
        for column in service.factor_groups(frame, "caffeine")[1]["matching_columns"]:
            frame[column] = [0, 10, 20, 1.5, 11.5, 21.5]
        with patch.object(service, "robust_scale", return_value=1.0):
            included = service.find_comparable_days(frame, "caffeine", "_analysis_outcome")
            excluded = service.find_comparable_days(frame, "caffeine", "_analysis_outcome",
                                                   maximum_distance=np.nextafter(1.5, 0))
        self.assertEqual(len(included["pairs"]), 3)
        self.assertTrue(included["pairs"]["matching_distance"].eq(1.5).all())
        self.assertTrue(excluded["pairs"].empty)

    def test_empty_missing_features_and_missing_next_condition(self):
        frame = synthetic_frame(30)
        for data in (frame.iloc[:0], frame.assign(caffeine_mg=100)):
            result = service.find_comparable_days(data, "caffeine", "_analysis_outcome")
            self.assertTrue(result["pairs"].empty)
        incomplete = frame.copy()
        for column in service.factor_groups(frame, "caffeine")[1]["matching_columns"]:
            incomplete[column] = np.nan
        self.assertTrue(service.find_comparable_days(incomplete, "caffeine", "_analysis_outcome")["pairs"].empty)
        frame["next_focus_score"] = np.nan
        result = service.find_comparable_days(frame, "caffeine", "_analysis_outcome", next_condition_column="next_focus_score")
        self.assertGreater(len(result["pairs"]), 0)
        self.assertEqual(result["next_summary"]["group_a_n"], 0)


class PerformanceResultCacheTests(unittest.TestCase):
    def setUp(self):
        self.frame = synthetic_frame()
        self.state = {}
        self.root = Path(tempfile.gettempdir()) / "synthetic-analysis-root"
        self.context = {"as_of_date": date(2026, 9, 17), "period": ("start", "end"),
                        "missing_as_zero": False, "metric": "output_hours", "next_metric": "next_focus_score",
                        "mode": "raw", "filter": "all"}
        self.compute = Mock(return_value={"summary": {"difference": 1.0}, "pairs": self.frame[["date"]]})

    def result(self, *, frame=None, context=None, state=None, root=None, settings=None):
        cache = PerformanceResultCache(self.state if state is None else state,
            self.frame if frame is None else frame, root=self.root if root is None else root,
            context=self.context if context is None else context)
        return cache.get_or_compute("similar_days", settings or {"factor": "caffeine", "distance": 1.5}, self.compute)

    def test_equivalent_rebuilt_frame_hits_and_returned_results_are_isolated(self):
        result = self.result()
        result["summary"]["difference"] = 999
        result["pairs"].iloc[0, 0] = pd.Timestamp("2099-01-01")
        repeated = self.result(frame=self.frame.copy(deep=True))
        self.assertEqual(self.compute.call_count, 1)
        self.assertEqual(repeated["summary"]["difference"], 1)
        self.assertEqual(repeated["pairs"].iloc[0, 0], self.frame["date"].iloc[0])

    def test_content_dtype_index_column_order_and_new_rows_invalidate(self):
        alternatives = []
        changed = self.frame.copy()
        changed.loc[0, "next_focus_score"] += 1
        alternatives.append(changed)
        changed = self.frame.copy()
        changed.loc[0, "caffeine_mg"] = np.nan
        alternatives.extend([changed, self.frame.iloc[::-1], self.frame.rename(index={0: 1000}),
                             self.frame.assign(weekday=self.frame.weekday.astype("float64")),
                             self.frame[self.frame.columns[::-1]], pd.concat([self.frame, self.frame.iloc[:1]], ignore_index=True)])
        for changed in alternatives:
            with self.subTest(shape=changed.shape):
                self.state.clear()
                self.compute.reset_mock()
                self.result()
                self.result(frame=changed)
                self.assertEqual(self.compute.call_count, 2)

    def test_every_context_change_and_result_parameter_invalidates(self):
        for field in self.context:
            with self.subTest(field=field):
                self.state.clear()
                self.compute.reset_mock()
                self.result()
                self.result(context={**self.context, field: "changed"})
                self.assertEqual(self.compute.call_count, 2)
        for setting in ({"factor": "running"}, {"distance": 1.0}, {"seed": 2}, {"iterations": 1000}):
            self.state.clear()
            self.compute.reset_mock()
            self.result()
            self.result(settings=setting)
            self.assertEqual(self.compute.call_count, 2)

    def test_session_root_and_implementation_revision_do_not_share_results(self):
        self.result()
        self.result(state={})
        self.result(root=self.root / "other")
        with patch("app_core.performance_result_cache._CALCULATION_REVISION", "changed-code-seed-or-defaults"):
            self.result()
        self.assertEqual(self.compute.call_count, 4)

    def test_bounded_storage_and_failure_does_not_replace_success(self):
        cache = PerformanceResultCache(self.state, self.frame, root=self.root, context=self.context)
        for index in range(12):
            cache.get_or_compute("test", {"index": index}, self.compute)
        self.assertEqual(len(cache._results), 8)
        with self.assertRaises(ValueError):
            cache.get_or_compute("failure", {}, lambda: (_ for _ in ()).throw(ValueError("synthetic")))
        self.assertEqual(len(cache._results), 8)


class PerformancePageCacheUiTests(unittest.TestCase):
    """Real page controls, in-memory synthetic features, no JSON/network access."""

    def setUp(self):
        import streamlit as st

        self.frame = synthetic_frame(30)
        self.today = date(2026, 9, 17)
        self.page_settings = {"analysis": {"caffeine_missing_as_zero": False}}
        for name, patcher in (
            ("read", patch.object(json_store, "_read", side_effect=AssertionError("No personal JSON reads"))),
            ("categories", patch("app_core.study_category_service.load_study_category_catalog",
                                 side_effect=lambda *args, **kwargs: default_category_catalog())),
            ("network", patch("socket.socket.connect", side_effect=AssertionError("No network"))),
            ("clock", patch.object(app_clock, "app_today", side_effect=lambda: self.today)),
            ("settings", patch.object(settings_service, "load_user_settings",
                                      side_effect=lambda: st.session_state.get("_test_settings", {}))),
            ("load", patch.object(service, "build_daily_feature_frame", side_effect=lambda *args, **kwargs: self.frame.copy())),
            ("profile", patch.object(service, "summarize_high_performance_profile", wraps=service.summarize_high_performance_profile)),
            ("evidence", patch.object(service, "build_evidence_table", wraps=service.build_evidence_table)),
            ("similar", patch.object(service, "find_comparable_days", wraps=service.find_comparable_days)),
        ):
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def page(self):
        from streamlit import config
        from streamlit.testing.v1 import AppTest
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        source = (Path(__file__).resolve().parents[1] / "pages/17_学習パフォーマンス・条件分析.py").read_text(encoding="utf-8-sig")
        source = source.replace("import prolog_ui.components as ab", """class _Decoration:
    def __getattr__(self, name):
        if name == 'theme_palette':
            return lambda **kwargs: {key: '#445566' for key in ('ink', 'primary', 'green', 'amber', 'red', 'muted', 'panel_2', 'line', 'card', 'text', 'background')}
        if name == 'sidemenu':
            return lambda *args, **kwargs: st.session_state.get('_test_settings', {})
        return lambda *args, **kwargs: None
ab = _Decoration()""")
        source = source.replace('st.page_link("pages/16_カフェイン・運動分析.py", label="成分リズム・コンディション分析を詳しく見る")', 'st.caption("offline navigation")')
        app = AppTest.from_string(source, default_timeout=30)
        app.session_state["_test_settings"] = self.page_settings
        return app

    def run_app(self, app):
        app.run(timeout=30)
        self.assertFalse(app.exception, [item.message for item in app.exception])
        self.read.assert_not_called()
        self.network.assert_not_called()
        return app

    def reset_counts(self):
        for mock in (self.load, self.profile, self.evidence, self.similar):
            mock.reset_mock()

    def test_ui_only_rerun_and_minimum_days_execute_zero_heavy_calculations(self):
        app = self.run_app(self.page())
        self.assertEqual([self.profile.call_count, self.evidence.call_count, self.similar.call_count], [1, 1, 1])
        self.reset_counts()
        self.run_app(app)
        self.assertEqual(self.load.call_count, 1)
        self.assertEqual([self.profile.call_count, self.evidence.call_count, self.similar.call_count], [0, 0, 0])
        app.number_input(key="performance_minimum_days").set_value(30)
        self.run_app(app)
        self.assertEqual([self.profile.call_count, self.evidence.call_count, self.similar.call_count], [0, 0, 0])
        self.assertIn("最低日数を満たす比較なし", " ".join(item.value for item in app.markdown))
        self.reset_counts()
        app.selectbox(key="performance_factor").set_value(service.FACTOR_LABELS["running"])
        self.run_app(app)
        self.assertEqual([self.profile.call_count, self.evidence.call_count, self.similar.call_count], [0, 0, 1])

    def test_data_date_missing_policy_and_next_metric_refresh_real_page(self):
        app = self.run_app(self.page())
        def edit_data():
            self.frame.loc[:, "output_hours"] = self.frame.output_hours + 0.5
        changes = (
            edit_data,
            lambda: setattr(self, "today", self.today + timedelta(days=1)),
            lambda: app.session_state.__setitem__("_test_settings", {"analysis": {"caffeine_missing_as_zero": True}}),
            lambda: app.selectbox(key="performance_next").set_value("翌日集中しやすさ"),
        )
        for change in changes:
            self.reset_counts()
            change()
            self.run_app(app)
            self.assertEqual([self.profile.call_count, self.evidence.call_count, self.similar.call_count], [1, 1, 1])


if __name__ == "__main__":
    unittest.main()
