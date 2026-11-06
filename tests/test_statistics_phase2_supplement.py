from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def half_up(value, decimals):
    """Independent expected-value oracle; do not call the production helper."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))

from fractions import Fraction
import json
import math
import unittest
from unittest.mock import patch

import numpy as np

from app_core.statistics_practice_service import grade_problem
from app_core.statistics_practice_supplement import (
    SUPPLEMENT_SPECS,
    SYNTHETIC_LABEL,
    _kaplan_meier_steps,
)


class _ForbiddenContext:
    def __getattribute__(self, name):
        raise AssertionError(f"Synthetic builder accessed personal context: {name}")


class StatisticsPhase2SupplementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builders = {spec[0]: spec[3] for spec in SUPPLEMENT_SPECS}

    def build(self, template_id: str, seed: int = 11) -> dict:
        return self.builders[template_id](_ForbiddenContext(), np.random.default_rng(seed))

    @staticmethod
    def answers(payload: dict) -> dict:
        return {part["key"]: part["expected"] for part in payload["answer_parts"]}

    def test_six_unique_templates_have_expected_topics(self) -> None:
        self.assertEqual(len(SUPPLEMENT_SPECS), 6)
        self.assertEqual(len(self.builders), 6)
        self.assertEqual({spec[1] for spec in SUPPLEMENT_SPECS}, {"logistic", "survival", "clinical"})

    def test_builders_never_load_or_inspect_personal_records(self) -> None:
        with patch("app_core.statistics_practice_service._load_context", side_effect=AssertionError("read")), patch(
            "app_core.statistics_practice_service.load_all_study_sessions", side_effect=AssertionError("read")
        ), patch("app_core.statistics_practice_service.build_daily_feature_frame", side_effect=AssertionError("read")):
            for template_id in self.builders:
                with self.subTest(template=template_id):
                    payload = self.build(template_id)
                    self.assertEqual(payload["data_origin"], "synthetic")
                    self.assertEqual(payload["data_origin_label"], SYNTHETIC_LABEL)
                    self.assertIn(SYNTHETIC_LABEL, payload["prompt"])
                    self.assertTrue(payload["source_key"].startswith("synthetic:"))
                    self.assertTrue(payload["calculation_spec"])
                    self.assertIn("実ログを加工したデータでも", payload["assumptions"][0])

    def test_seed_is_deterministic_and_all_variants_are_json_safe(self) -> None:
        for template_id in self.builders:
            serialized_variants = set()
            for seed in range(40):
                with self.subTest(template=template_id, seed=seed):
                    payload = self.build(template_id, seed)
                    self.assertEqual(payload, self.build(template_id, seed))
                    serialized_variants.add(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True))
                    self.assertGreaterEqual(len(payload["hints"]), 2)
                    self.assertTrue(payload["explanation"])
                    keys = {column["key"] for column in payload["data_columns"]}
                    for row in payload["data_rows"]:
                        self.assertEqual(set(row), keys)
            self.assertGreater(len(serialized_variants), 1)

    def test_correct_answers_and_partial_credit_are_supported_for_every_template(self) -> None:
        for template_id in self.builders:
            with self.subTest(template=template_id):
                payload = self.build(template_id)
                answers = self.answers(payload)
                grade = grade_problem(payload, answers)
                self.assertTrue(grade["all_correct"])
                self.assertEqual(grade["score"], 100.0)
                first = next(iter(answers))
                partial = grade_problem(payload, {first: answers[first]})
                self.assertGreater(partial["score"], 0.0)
                self.assertLess(partial["score"], 100.0)

    def test_logistic_probability_uses_only_presented_coefficients(self) -> None:
        for seed in range(40):
            payload = self.build("logistic_probability", seed)
            row = payload["data_rows"][0]
            logit = row["intercept"] + row["beta_x"] * row["x"] + row["beta_z"] * row["z"]
            expected = self.answers(payload)
            self.assertEqual(expected["logit"], half_up(logit, 2))
            self.assertEqual(expected["odds"], half_up(math.exp(logit), 3))
            self.assertEqual(expected["probability"], half_up(1.0 / (1.0 + math.exp(-logit)), 3))
            self.assertGreater(expected["probability"], 0.0)
            self.assertLess(expected["probability"], 1.0)

    def test_odds_ratio_uses_exponentiated_coefficient_and_change(self) -> None:
        signs = set()
        for seed in range(40):
            payload = self.build("logistic_odds_ratio", seed)
            row = payload["data_rows"][0]
            signs.add(row["beta"] > 0)
            expected = self.answers(payload)
            self.assertEqual(expected["or_one_unit"], half_up(math.exp(row["beta"]), 3))
            self.assertEqual(expected["or_change"], half_up(math.exp(row["beta"] * row["delta"]), 3))
            self.assertIn("確率の倍率とは限らない", expected["interpretation"])
        self.assertEqual(signs, {False, True})

    def test_kaplan_meier_ties_use_pre_time_risk_set(self) -> None:
        rows = [
            {"time": 1, "status": "イベント"},
            {"time": 2, "status": "打切り"},
            {"time": 3, "status": "イベント"},
            {"time": 3, "status": "打切り"},
            {"time": 4, "status": "イベント"},
            {"time": 5, "status": "打切り"},
            {"time": 6, "status": "イベント"},
            {"time": 7, "status": "打切り"},
        ]
        steps = _kaplan_meier_steps(rows)
        tied = next(step for step in steps if step["time"] == 3)
        self.assertEqual(tied["at_risk"], 6)
        self.assertEqual(tied["events"], 1)
        self.assertEqual(tied["censored"], 1)
        self.assertAlmostEqual(tied["survival"], 35 / 48)
        self.assertAlmostEqual(steps[-1]["survival"], 35 / 128)
        self.assertEqual(steps[-2]["survival"], steps[-1]["survival"])
        self.assertEqual(steps, _kaplan_meier_steps(list(reversed(rows))))

    def test_kaplan_meier_all_events_or_all_censoring_are_finite(self) -> None:
        for status, expected in (("イベント", 0.0), ("打切り", 1.0)):
            rows = [{"time": 1, "status": status} for _ in range(4)]
            steps = _kaplan_meier_steps(rows)
            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[-1]["survival"], expected)
            json.dumps(steps, allow_nan=False)

    def test_kaplan_meier_questions_and_chart_match_exact_fraction_calculation(self) -> None:
        reached_zero = False
        remained_positive = False
        for seed in range(50):
            payload = self.build("kaplan_meier", seed)
            rows = payload["data_rows"]
            specification = payload["calculation_spec"]
            focus_time = specification["focus_time"]
            risk_set = sum(row["time"] >= focus_time for row in rows)
            events_at_focus = sum(row["time"] == focus_time and row["status"] == "イベント" for row in rows)
            survival = Fraction(1)
            by_time = {0: 1.0}
            for time in sorted({row["time"] for row in rows}):
                risk = sum(row["time"] >= time for row in rows)
                events = sum(row["time"] == time and row["status"] == "イベント" for row in rows)
                survival *= Fraction(risk - events, risk)
                by_time[time] = float(survival)
            expected = self.answers(payload)
            self.assertEqual(expected["at_risk"], risk_set)
            self.assertEqual(expected["conditional_survival"], half_up((risk_set - events_at_focus) / risk_set, 3))
            self.assertEqual(expected["survival"], half_up(float(survival), 3))
            reached_zero |= survival == 0
            remained_positive |= survival > 0
            self.assertEqual(payload["result_chart"]["kind"], "survival")
            chart = payload["result_chart"]["rows"]
            self.assertEqual(chart[0], {"time": 0, "survival": 1.0})
            for point in chart:
                self.assertAlmostEqual(point["survival"], by_time[point["time"]])
            self.assertTrue(all(before["survival"] >= after["survival"] for before, after in zip(chart, chart[1:])))
            self.assertTrue(all("survival" not in row for row in payload["data_rows"]))
            self.assertEqual(specification["ties"], "events_before_censoring")
        self.assertTrue(reached_zero)
        self.assertTrue(remained_positive)

    def test_diagnostic_accuracy_uses_correct_denominators(self) -> None:
        for seed in range(40):
            payload = self.build("diagnostic_accuracy", seed)
            positive, negative = payload["data_rows"]
            tp, fp = positive["disease_positive"], positive["disease_negative"]
            fn, tn = negative["disease_positive"], negative["disease_negative"]
            expected = self.answers(payload)
            self.assertEqual(expected["sensitivity"], half_up(tp / (tp + fn) * 100, 2))
            self.assertEqual(expected["specificity"], half_up(tn / (tn + fp) * 100, 2))
            self.assertEqual(expected["ppv"], half_up(tp / (tp + fp) * 100, 2))
            self.assertEqual(expected["npv"], half_up(tn / (tn + fn) * 100, 2))
            self.assertTrue(all(part["unit"] == "%" for part in payload["answer_parts"]))

    def test_risk_ratio_odds_ratio_and_absolute_difference_are_distinct(self) -> None:
        for seed in range(40):
            payload = self.build("risk_ratio_odds_ratio", seed)
            exposed, control = payload["data_rows"]
            a, b = exposed["events"], exposed["no_events"]
            c, d = control["events"], control["no_events"]
            expected = self.answers(payload)
            p1, p0 = a / (a + b), c / (c + d)
            self.assertEqual(expected["risk_ratio"], half_up(p1 / p0, 3))
            self.assertEqual(expected["odds_ratio"], half_up(a * d / (b * c), 3))
            self.assertEqual(expected["risk_difference"], half_up((p1 - p0) * 100, 2))
            self.assertNotEqual(expected["risk_ratio"], expected["odds_ratio"])
            self.assertIn("因果関係は断定できない", expected["interpretation"])

    def test_trial_design_keeps_original_allocation_with_complete_outcomes(self) -> None:
        for seed in range(20):
            payload = self.build("trial_design", seed)
            first, second = payload["data_rows"]
            self.assertGreater(first["changed"], 0)
            self.assertEqual(first["randomized"], second["randomized"])
            self.assertEqual(first["randomized"], first["observed"])
            self.assertEqual(second["randomized"], second["observed"])
            expected = self.answers(payload)
            self.assertIn("最初に無作為割付された群", expected["itt"])
            self.assertIn("登録前", expected["allocation"])
            self.assertTrue(payload["calculation_spec"]["outcomes_complete"])


if __name__ == "__main__":
    unittest.main()
