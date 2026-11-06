from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from app_core import app_clock, json_store, settings_service
from app_core import statistics_practice_service as service


class StatisticsGradingContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-stat-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root), f"Non-fixture JSON read: {path}")
            return original_read(path)

        for patcher in (patch.object(json_store, "_read", side_effect=isolated_read),
                        patch.object(json_store, "PROJECT_ROOT", self.root),
                        patch.object(app_clock, "DEMO_MODE_PATH", self.root / "settings/demo_mode.json"),
                        patch.object(settings_service, "USER_SETTINGS_PATH", self.root / "settings/user_settings.json")):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def part(expected=1.125, *, decimals=2, domain="real", key="value", unit=""):
        return service._numeric_part(key, "テスト値", expected, decimals=decimals, domain=domain, unit=unit)

    def problem(self, part):
        context = service._DataContext(date(2026, 9, 1), date(2026, 9, 1), pd.DataFrame(), pd.DataFrame())
        return service._finalize_problem(service._TEMPLATES[0], {"answer_parts": [part], "prompt": "固定値を答えてください。"}, context, 11)

    def correct(self, part, answer):
        return service.grade_problem({"answer_parts": [part]}, {part["key"]: answer})["all_correct"]

    def test_independent_decimal_half_ties_match_expected_text_and_grading(self):
        for value, expected in ((1.125, "1.13"), (-1.125, "-1.13"), (2.675, "2.68"),
                                (1.005, "1.01"), (-0.005, "-0.01")):
            with self.subTest(value=value):
                part = self.part(value)
                self.assertEqual(part["expected_text"], expected)
                self.assertEqual(part["expected"], float(expected))
                self.assertEqual(service.format_decimal_answer(value, 2), expected)
                self.assertTrue(self.correct(part, value))
                self.assertTrue(self.correct(part, expected))

    def test_rounding_cell_boundaries_are_directional_not_symmetric_tolerance(self):
        for value, trials in (
            (1.13, {"1.124999999": False, "1.125": True, "1.125000001": True, "1.134999999": True, "1.135": False}),
            (-1.13, {"-1.135": False, "-1.134999999": True, "-1.125": True, "-1.124999999": False}),
            (0, {"-0.005": False, "-0.004999999": True, "0": True, "0.004999999": True, "0.005": False}),
        ):
            for answer, correct in trials.items():
                with self.subTest(value=value, answer=answer):
                    self.assertEqual(self.correct(self.part(value), answer), correct)

    def test_mean_builder_uses_displayed_values_and_the_same_explanation_rounding(self):
        # Five displayed values sum exactly to 5.625; their mean is 1.125.
        rows = pd.DataFrame({"date": ["2026-09-01"] * 5, "value": [1, 1, 1, 1, 1.625]})
        source = {"rows": rows, "key": "study_hours", "label": "時間", "unit": "時間", "observation_unit": "日"}
        with patch.object(service, "_choose_numeric", return_value=source), patch.object(service, "_sample_frame", return_value=rows):
            payload = service._mean_problem(self.context(), np.random.default_rng(0))
        self.assertEqual([row["value"] for row in payload["data_rows"]], [1, 1, 1, 1, 1.625])
        self.assertEqual(payload["answer_parts"][0]["expected"], 1.13)
        self.assertIn("平均は1.13時間", payload["explanation"])

    def test_generation_does_not_round_binary_error_below_exact_decimal_half(self):
        # Independent arithmetic: 34.075 / 5 = 6.815, not 6.8149999999999995.
        values = [5.241, 3.48, 7.796, 8.533, 9.025]
        rows = pd.DataFrame({"date": ["2026-09-01"] * len(values), "value": values})
        source = {"rows": rows, "key": "study_hours", "label": "時間", "unit": "時間", "observation_unit": "日"}
        for builder in (service._mean_problem, service._z_score_problem, service._mean_ci_problem):
            with self.subTest(builder=builder.__name__), patch.object(service, "_choose_numeric", return_value=source), \
                    patch.object(service, "_sample_frame", return_value=rows):
                payload = builder(self.context(), np.random.default_rng(0))
                mean = next(part for part in payload["answer_parts"] if part["key"] == "mean")
                self.assertEqual(mean["expected_text"], "6.82")
                self.assertTrue(self.correct(mean, "6.82"))
        # Sum of squared deviations=.1; divide by n-1=4 -> .025 -> .03.
        variance_rows = pd.DataFrame({"date": ["2026-09-01"] * 5, "value": [1, 1.1, 1.2, 1.3, 1.4]})
        with patch.object(service, "_choose_numeric", return_value=source), patch.object(service, "_sample_frame", return_value=variance_rows):
            payload = service._sample_sd_problem(self.context(), np.random.default_rng(0))
        self.assertEqual(payload["answer_parts"][0]["expected_text"], "0.03")
        self.assertIn("不偏分散は0.03", payload["explanation"])

    def test_binomial_moments_keep_exact_fraction_until_final_rounding(self):
        # n=10, p=11/20 gives 10*11*9/400=2.475, hence 2.48.
        rows = pd.DataFrame({"date": ["2026-09-01"] * 20, "category": ["A"] * 11 + ["B"] * 9})
        source = {"rows": rows, "key": "synthetic", "label": "分類", "observation_unit": "件"}

        class Choices:
            calls = iter((0, 2))  # choose A, then trial_options[2] == 10

            def integers(self, *_args):
                return next(self.calls)

        with patch.object(service, "_choose_categorical", return_value=source), patch.object(service, "_sample_frame", return_value=rows):
            payload = service._binomial_moments_problem(self.context(), Choices())
        expected = {part["key"]: part["expected_text"] for part in payload["answer_parts"]}
        self.assertEqual(expected, {"p_percent": "55.00", "expected_count": "5.50", "variance": "2.48"})
        self.assertIn("Var[X]=2.48", payload["explanation"])

    def test_regression_and_risk_set_use_exact_displayed_rational_values(self):
        # y=.125*x+.125 has exact slope and intercept ties -> .13, .13.
        x = np.arange(1, 9, dtype=float)
        y = np.array([.25, .375, .5, .625, .75, .875, 1, 1.125])
        rows = [{"no": i + 1, "date": "2026-09-01", "x": a, "y": b} for i, (a, b) in enumerate(zip(x, y))]
        source = {"x_label": "X", "y_label": "Y", "x_unit": "", "y_unit": "", "key": "test", "observation_unit": "件"}
        with patch.object(service, "_choose_correlation", return_value=source), \
                patch.object(service, "_sample_pair_rows", return_value=(rows, x, y)):
            payload = service._regression_problem(self.context(), np.random.default_rng(0))
        self.assertEqual([part["expected_text"] for part in payload["answer_parts"]], ["0.13", "0.13"])
        from app_core.statistics_practice_supplement import _kaplan_meier_steps
        from fractions import Fraction
        steps = _kaplan_meier_steps([{"time": 1, "status": "イベント"}, {"time": 1, "status": "打切り"},
                                    {"time": 2, "status": "イベント"}, {"time": 3, "status": "打切り"}], exact=True)
        self.assertEqual(steps[-1]["survival"], Fraction(3, 8))

    def test_counts_reject_nonintegers_before_rounding(self):
        count = self.part(5, domain="integer_count", decimals=0)
        for answer in (5, "5", "5.0", "５．００", "5e0"):
            self.assertTrue(self.correct(count, answer))
        for answer in (5.49, 4.51, "5.0000000001", "-0.0001"):
            self.assertFalse(self.correct(count, answer))
        # An expected frequency is not an observed integer count.
        self.assertTrue(self.correct(self.part(5.49, domain="nonnegative", unit="件"), "5.49"))

    def test_probability_percentage_and_correlation_domains_precede_rounding(self):
        for domain, lower, upper in (("probability", 0, 1), ("percentage", 0, 100), ("correlation", -1, 1),
                                     ("percentage_difference", -100, 100)):
            with self.subTest(domain=domain):
                self.assertTrue(self.correct(self.part(lower, domain=domain), lower))
                self.assertTrue(self.correct(self.part(upper, domain=domain), upper))
                self.assertFalse(self.correct(self.part(lower, domain=domain), lower - 0.000001))
                self.assertFalse(self.correct(self.part(upper, domain=domain), upper + 0.000001))

    def test_ratios_and_unclipped_confidence_limits_are_not_probabilities(self):
        self.assertTrue(self.correct(self.part(4.5, domain="nonnegative"), "4.50"))
        self.assertTrue(self.correct(self.part(-3.5, domain="real", unit="%"), "-3.50"))
        self.assertTrue(self.correct(self.part(103.5, domain="real", unit="%"), "103.50"))
        self.assertFalse(self.correct(self.part(0, domain="nonnegative"), "-0.001"))
        self.assertFalse(self.correct(self.part(0.5, domain="probability"), "0.5%"))
        self.assertTrue(self.correct(self.part(50, domain="percentage", unit="%"), "５０％"))

    def test_boolean_nonfinite_and_invalid_answers_never_receive_credit(self):
        part = self.part(1)
        old = {key: value for key, value in part.items() if key not in {"expected_text", "contract_version", "rounding", "comparison", "input_domain"}}
        old["tolerance"] = 0.5
        for answer in (True, False, np.bool_(True), float("nan"), float("inf"), "NaN", "Infinity", "-inf", "abc", 10**400):
            for target in (part, old):
                with self.subTest(answer=str(answer), legacy=target is old):
                    self.assertFalse(self.correct(target, answer))

    def test_invalid_new_contract_never_falls_back_to_legacy_tolerance(self):
        for updates in ({"contract_version": 3}, {"rounding": "HALF_EVEN"}, {"tolerance": 0.5},
                        {"decimals": True}, {"expected_text": "1.12"}, {"input_domain": {}}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.correct({**self.part(), **updates}, "1.13")
        for options in ({"expected": True}, {"expected": float("nan")}, {"expected": 5.49, "decimals": 0, "domain": "integer_count"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.part(**options)

    def test_pvalue_decision_boundary_filter_uses_the_same_half_up_cells(self):
        from app_core.statistics_practice_tests import _check_result
        for value in (0.04995, 0.05, 0.050049999):
            with self.subTest(value=value), self.assertRaises(service._TemplateUnavailable):
                _check_result(1.0, value)
        for value in (0.049949999, 0.05005, 0.050050001):
            with self.subTest(value=value):
                _check_result(1.0, value)
                self.assertNotEqual(self.part(value, decimals=4, domain="probability")["expected_text"], "0.0500")

    def test_legacy_snapshot_retains_original_tolerance_and_is_not_mutated(self):
        old_part = {"key": "value", "label": "人数", "kind": "numeric", "expected": 5.0,
                    "decimals": 0, "tolerance": 0.500000000001, "unit": "人", "weight": 1}
        problem = {"answer_parts": [old_part], "snapshot": {"schema_version": 1, "answer_key": [deepcopy(old_part)]}}
        before = deepcopy(problem)
        self.assertTrue(service.grade_problem(problem, {"value": 5.49})["all_correct"])
        self.assertTrue(service.grade_problem(problem, {"value": 5.5})["all_correct"])
        self.assertFalse(service.grade_problem(problem, {"value": 5.51})["all_correct"])
        self.assertEqual(problem, before)
        self.assertIn("旧問題", service.numeric_answer_rule(old_part))
        self.assertIn("再丸めしません", service.numeric_answer_rule(old_part))

    def test_reading_history_preserves_stored_scores_and_bytes(self):
        path = self.root / "statistics_practice/2026/09/20260901.json"
        path.parent.mkdir(parents=True)
        history = {"schema_version": 1, "date": "2026-09-01", "attempts": [{
            "attempt_id": "old", "date": "2026-09-01", "score": 37.0,
            "grade": {"score": 37.0}, "problem_snapshot": {"answer_parts": [{"key": "n", "kind": "numeric", "expected": 5, "tolerance": 0.5}]}}]}
        path.write_text(json.dumps(history, indent=3), encoding="utf-8")
        before = path.read_bytes(), path.stat().st_mtime_ns
        with patch.object(service, "grade_problem", side_effect=AssertionError("history must not be regraded")):
            loaded = service.load_attempts(root=self.root)
        self.assertEqual(loaded[0], history["attempts"][0])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    @staticmethod
    def context():
        start = date(2020, 1, 1)
        dates = pd.date_range(start, periods=40)
        daily = pd.DataFrame({"date": dates, "study_hours": [1 + i % 7 * .3 for i in range(40)],
                              "sleep_hours": [5 + i % 9 * .2 for i in range(40)], "focus_score": [1 + i % 5 for i in range(40)],
                              "fatigue": [1 + i * 2 % 5 for i in range(40)], "running_distance_km": [2 + i % 7 * .45 for i in range(40)],
                              "running_duration_min": [15 + i % 8 * 4 for i in range(40)], "is_weekend": dates.dayofweek >= 5})
        rows = [{"date": stamp.date().isoformat(), "session_minutes": 20 + (i * 3 + offset) % 17 * 5,
                 "environment": ("自宅", "カフェ", "図書館")[(i + offset) % 3],
                 "io_type": ("インプット", "アウトプット", "両方")[(i * 2 + offset) % 3],
                 "study_category": ("医学", "英語", "IT")[(i + 2 * offset) % 3]}
                for i, stamp in enumerate(dates) for offset in range(2)]
        return service._DataContext(start, start + timedelta(days=39), daily, pd.DataFrame(rows))

    def test_all_24_templates_freeze_explicit_semantic_contracts(self):
        expected_domains = {
            "descriptive_mean": {"mean": "real"},
            "descriptive_sample_sd": {"sample_variance": "nonnegative", "sample_sd": "nonnegative"},
            "category_proportion": {"event_count": "integer_count", "event_percent": "percentage"},
            "conditional_probability": {"denominator": "integer_count", "numerator": "integer_count", "conditional_percent": "percentage"},
            "binomial_moments": {"p_percent": "percentage", "expected_count": "nonnegative", "variance": "nonnegative"},
            "z_score": {"mean": "real", "sample_sd": "nonnegative", "z_score": "real"},
            "mean_se_ci": {"mean": "real", "standard_error": "nonnegative", "ci_lower": "real", "ci_upper": "real"},
            "proportion_ci": {"sample_percent": "percentage", "standard_error_pp": "nonnegative", "ci_lower_percent": "real", "ci_upper_percent": "real"},
            "contingency_expected": {"row_percent": "percentage", "expected_count": "nonnegative"},
            "pearson_correlation": {"pearson_r": "correlation"}, "simple_regression": {"slope": "real", "intercept": "real"},
            "correlation_interpretation": {"pearson_r": "correlation"},
            "one_sample_t_test": {"t_statistic": "real", "p_value": "probability"},
            "welch_t_test": {"t_statistic": "real", "p_value": "probability"},
            "paired_t_test": {"t_statistic": "real", "p_value": "probability"},
            "chi_square_independence": {"chi_square": "nonnegative", "p_value": "probability"},
            "fisher_exact_test": {"observed_probability": "probability", "p_value": "probability"},
            "wilcoxon_signed_rank": {"w_statistic": "nonnegative", "p_value": "probability"},
            "logistic_probability": {"logit": "real", "odds": "nonnegative", "probability": "probability"},
            "logistic_odds_ratio": {"or_one_unit": "nonnegative", "or_change": "nonnegative"},
            "kaplan_meier": {"at_risk": "integer_count", "conditional_survival": "probability", "survival": "probability"},
            "diagnostic_accuracy": {key: "percentage" for key in ("sensitivity", "specificity", "ppv", "npv")},
            "risk_ratio_odds_ratio": {"risk_ratio": "nonnegative", "odds_ratio": "nonnegative", "risk_difference": "percentage_difference"},
            "trial_design": {},
        }
        self.assertEqual(len(service._TEMPLATES), 24)
        context = self.context()
        for template in service._TEMPLATES:
            with self.subTest(template=template.template_id):
                payload = None
                for seed in range(20):
                    try:
                        payload = service._build_payload(template, context, seed)
                        break
                    except service._TemplateUnavailable:
                        continue
                self.assertIsNotNone(payload)
                problem = service._finalize_problem(template, payload, context, seed)
                parts = [part for part in problem["answer_parts"] if part["kind"] == "numeric"]
                self.assertEqual({part["key"]: part["input_domain"]["kind"] for part in parts}, expected_domains[template.template_id])
                self.assertEqual(problem["snapshot"]["grading_contract_version"], 2)
                for part in parts:
                    frozen = next(item for item in problem["snapshot"]["answer_key"] if item["key"] == part["key"])
                    for key in ("decimals", "tolerance", "rounding", "comparison", "input_domain", "contract_version"):
                        self.assertEqual(frozen[key], part[key])
                        self.assertEqual(problem["snapshot"]["rounding"][part["key"]][key], part[key])
                    self.assertEqual(part["rounding"], "ROUND_HALF_UP")
                    self.assertEqual(part["tolerance"], 0)
                    self.assertIn(part["expected_text"], problem["explanation"])
                self.assertTrue(service.grade_problem(problem, {part["key"]: part["expected"] for part in problem["answer_parts"]})["all_correct"])
                if parts:
                    self.assertIn("ROUND_HALF_UP", problem["prompt"])
                json.dumps(problem, allow_nan=False)

    def page_app(self, problem):
        from streamlit import config
        from streamlit.testing.v1 import AppTest
        config.get_config_options(options_from_flags={"global.developmentMode": False, "browser.gatherUsageStats": False})
        source = (Path(__file__).resolve().parents[1] / "pages/05_高度AI分析.py").read_text(encoding="utf-8-sig")
        source = source.replace("ROOT = Path(__file__).resolve().parents[1]", f"ROOT = Path({str(self.root)!r})")
        source = source.replace("import prolog_ui.components as ab", "class _Decoration:\n    def __getattr__(self, name):\n        return lambda *args, **kwargs: None\nab = _Decoration()")
        app = AppTest.from_string(source, default_timeout=30)
        app.session_state["statistics_practice_current_problem"] = problem
        app.run()
        self.assertEqual(len(app.exception), 0)
        return app

    def test_real_ui_displays_new_rule_preserves_raw_input_and_saves_contract(self):
        problem = self.problem(self.part(5, decimals=0, domain="integer_count", unit="人"))
        app = self.page_app(problem)
        key = f"statistics_answer_{problem['problem_id']}_value"
        self.assertTrue(any("整数のみ" in item.value for item in app.caption))
        self.assertTrue(any("ROUND_HALF_UP" in item.value for item in app.markdown))
        app.text_input(key=key).set_value("5.49")
        next(button for button in app.button if button.label == "採点する").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertFalse(app.session_state["statistics_practice_current_grade"]["all_correct"])
        self.assertEqual(app.text_input(key=key).value, "5.49")
        results = [table.value for table in app.dataframe if "あなたの解答" in table.value.columns]
        self.assertTrue(any("5.49" in str(value) for value in results[0]["あなたの解答"]))
        self.assertEqual(service.load_attempts(root=self.root)[0]["problem_snapshot"], problem)

    def test_real_ui_uses_decimal_half_up_and_labels_legacy_tolerance(self):
        problem = self.problem(self.part(1.125))
        app = self.page_app(problem)
        key = f"statistics_answer_{problem['problem_id']}_value"
        self.assertIn("ROUND_HALF_UP", app.text_input(key=key).proto.help)
        app.text_input(key=key).set_value("1.125")
        next(button for button in app.button if button.label == "採点する").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(app.session_state["statistics_practice_current_grade"]["all_correct"])
        tables = [table.value for table in app.dataframe if "正解" in table.value.columns]
        self.assertEqual(tables[0].iloc[0]["正解"], "1.13")
        old = deepcopy(problem)
        for part in old["answer_parts"]:
            for field in ("contract_version", "rounding", "comparison", "input_domain", "expected_text"):
                part.pop(field)
            part.update(expected=1.12, tolerance=0.005000000001)
        old["snapshot"] = {"schema_version": 1}
        old["prompt"] = "旧問題"
        old_app = self.page_app(old)
        self.assertTrue(any("旧問題の保存済み規約" in item.value for item in old_app.caption))
        self.assertEqual(len(old_app.number_input), 1)

    def test_session_period_and_coercion_use_local_analysis_date(self):
        local = datetime(2026, 9, 1, 0, 15).astimezone()
        timestamp = local.astimezone(timezone.utc).isoformat()
        with patch.object(service, "build_daily_feature_frame", return_value=pd.DataFrame()), \
                patch.object(service, "load_all_study_sessions", return_value=[{"start_at": timestamp, "duration_sec": 60}]):
            context = service._load_context(date(2026, 9, 1), date(2026, 9, 1), self.root)
        self.assertEqual(context.sessions["date"].tolist(), ["2026-09-01"])
        self.assertEqual(service._coerce_date(timestamp, "date"), date(2026, 9, 1))


if __name__ == "__main__":
    unittest.main()
