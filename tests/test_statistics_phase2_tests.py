from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def half_up(value, decimals):
    """Independent expected-value oracle; do not call the production helper."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))

from datetime import date, timedelta
import copy
import json
import math
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from scipy import stats

from app_core import statistics_practice_service as service
from app_core import statistics_practice_tests as testing


class HypothesisTestingExerciseTests(unittest.TestCase):
    def _context(self, values=None, sessions=None, dates=None):
        values = list(values if values is not None else [6, 7, 8, 9, 10])
        if dates is None:
            dates = [date(2020, 1, 1) + timedelta(days=index) for index in range(len(values))]
        daily = pd.DataFrame({"date": pd.to_datetime(dates), "sleep_hours": values})
        session_frame = pd.DataFrame(sessions or [], columns=["date", "session_minutes", "environment", "io_type", "study_category"])
        return service._DataContext(date(2020, 1, 1), date(2020, 3, 1), daily, session_frame)

    def _table_context(self, table):
        sessions = []
        for i, j in np.ndindex(2, 2):
            sessions.extend({"date": "2020-01-01", "session_minutes": 30,
                             "environment": ["自宅", "カフェ"][i], "io_type": ["インプット", "アウトプット"][j],
                             "study_category": None} for _ in range(table[i][j]))
        return self._context(sessions=sessions)

    def _answers(self, payload):
        return {part["key"]: part["expected"] for part in payload["answer_parts"]}

    def test_one_sample_t_uses_displayed_values_and_fixed_reference(self):
        problem = testing.one_sample_t_problem(self._context(), np.random.default_rng(1))
        answers = self._answers(problem)
        self.assertEqual(problem["parameters"]["null_mean"], 7)
        self.assertAlmostEqual(answers["t_statistic"], half_up(math.sqrt(2), 3))
        self.assertEqual(answers["p_value"], 0.2302)
        self.assertEqual(answers["decision"], testing._RETAIN)
        self.assertIn("独立", problem["prompt"])
        self.assertIn("正規", problem["prompt"])

    def test_t_rejects_constant_and_rounded_constant_data(self):
        for values in ([7] * 8, [7.00001 + index * 0.000001 for index in range(8)]):
            with self.subTest(values=values):
                with self.assertRaises(service._TemplateUnavailable):
                    testing.one_sample_t_problem(self._context(values), np.random.default_rng(2))

    def test_welch_supports_session_only_data_and_preserves_group_direction(self):
        sessions = [{"date": "2020-01-01", "session_minutes": value, "environment": environment,
                     "io_type": None, "study_category": None}
                    for environment, values in [("自宅", [10, 20, 30, 40]), ("カフェ", [15, 20, 25, 30])]
                    for value in values]
        context = self._context(values=[], sessions=sessions)
        problem = testing.welch_t_problem(context, np.random.default_rng(8))
        parameters = problem["parameters"]
        groups = {group: np.array([row["value"] for row in problem["data_rows"] if row["group"] == group])
                  for group in [parameters["group_a"], parameters["group_b"]]}
        a, b = groups[parameters["group_a"]], groups[parameters["group_b"]]
        va, vb = a.var(ddof=1)/len(a), b.var(ddof=1)/len(b)
        statistic = (a.mean()-b.mean())/math.sqrt(va+vb)
        df = (va+vb)**2/(va**2/(len(a)-1)+vb**2/(len(b)-1))
        answers = self._answers(problem)
        self.assertEqual(answers["t_statistic"], half_up(statistic, 3))
        self.assertEqual(answers["p_value"], half_up(float(2*stats.t.sf(abs(statistic), df)), 4))
        self.assertAlmostEqual(parameters["df"], df)
        self.assertEqual([row["no"] for row in problem["data_rows"]], list(range(1, 9)))

    def test_pairing_never_bridges_missing_days_or_reuses_dates(self):
        source = {"rows": pd.DataFrame({"date": ["2020-01-01", "2020-01-03", "2020-01-04", "2020-01-05", "2020-01-06", "2020-01-08"],
                                        "value": [1, 2, 3, 4, 5, 6]})}
        rows = testing._adjacent_pairs(source).to_dict("records")
        self.assertEqual([(row["date_before"], row["date_after"]) for row in rows],
                         [("2020-01-03", "2020-01-04"), ("2020-01-05", "2020-01-06")])
        used = [row[key] for row in rows for key in ("date_before", "date_after")]
        self.assertEqual(len(used), len(set(used)))

    def test_paired_t_uses_within_pair_differences_and_rejects_constant_differences(self):
        context = self._context([5, 6, 6, 9, 7, 8, 8, 12, 9, 14])
        problem = testing.paired_t_problem(context, np.random.default_rng(12))
        differences = np.array([round(row["after"]-row["before"], 3) for row in problem["data_rows"]])
        statistic = differences.mean()/(differences.std(ddof=1)/math.sqrt(len(differences)))
        answers = self._answers(problem)
        self.assertEqual(answers["t_statistic"], half_up(statistic, 3))
        self.assertEqual(problem["parameters"]["df"], 4)
        self.assertIn("介入前後", problem["prompt"])
        with self.assertRaises(service._TemplateUnavailable):
            testing.paired_t_problem(self._context([5, 6, 6, 7, 7, 8, 8, 9, 9, 10]), np.random.default_rng(1))

    def test_signed_rank_exact_matches_known_values_and_handles_ties_zeros(self):
        w, p, ranks = testing.exact_signed_rank(np.array([1, -2, 3, -4, 5]))
        self.assertEqual(w, 6)
        self.assertEqual(p, 0.8125)
        np.testing.assert_array_equal(ranks, [1, 2, 3, 4, 5])
        w, p, ranks = testing.exact_signed_rank(np.array([1, 1, 2, 0]))
        self.assertEqual((w, p), (0, 0.25))
        np.testing.assert_array_equal(ranks, [1.5, 1.5, 3])
        w, p, _ = testing.exact_signed_rank(np.array([1, 1, -2, 0]))
        self.assertEqual((w, p), (3, 1))
        for invalid in (np.array([0, 0]), np.ones(13), np.array([1, np.nan])):
            with self.assertRaises(ValueError):
                testing.exact_signed_rank(invalid)

    def test_wilcoxon_matches_displayed_rounded_differences(self):
        context = self._context([5, 6, 6, 7, 7, 9, 8, 8, 9, 8])
        problem = testing.wilcoxon_problem(context, np.random.default_rng(2))
        differences = np.array([round(row["after"]-row["before"], 3) for row in problem["data_rows"]])
        statistic, pvalue, _ = testing.exact_signed_rank(differences)
        answers = self._answers(problem)
        self.assertEqual(answers["w_statistic"], statistic)
        self.assertEqual(answers["p_value"], half_up(pvalue, 4))
        self.assertEqual(problem["parameters"]["effective_n"], 4)
        self.assertIn("平均順位", problem["prompt"])
        self.assertIn("0差", problem["prompt"])
        self.assertIn("対称", problem["prompt"])

    def test_chi_square_uses_no_yates_correction_and_rejects_sparse_expected(self):
        problem = testing.chi_square_problem(self._table_context([[20, 10], [10, 20]]), np.random.default_rng(3))
        answers = self._answers(problem)
        self.assertEqual(answers["chi_square"], 6.667)
        self.assertEqual(answers["p_value"], 0.0098)
        self.assertEqual(answers["decision"], testing._REJECT)
        self.assertFalse(problem["parameters"]["correction"])
        self.assertTrue(np.all(np.array(problem["parameters"]["expected"]) >= 5))
        with self.assertRaises(service._TemplateUnavailable):
            testing.chi_square_problem(self._table_context([[8, 2], [1, 5]]), np.random.default_rng(3))

    def test_fisher_two_sided_probability_ordering_and_zero_cell(self):
        problem = testing.fisher_exact_problem(self._table_context([[8, 2], [1, 5]]), np.random.default_rng(4))
        answers = self._answers(problem)
        self.assertEqual(answers["p_value"], 0.035)
        self.assertEqual(answers["decision"], testing._REJECT)
        expected_observed = math.comb(9, 8)*math.comb(7, 2)/math.comb(16, 10)
        self.assertEqual(answers["observed_probability"], half_up(expected_observed, 4))
        zero = testing.fisher_exact_problem(self._table_context([[5, 0], [0, 5]]), np.random.default_rng(4))
        self.assertEqual(self._answers(zero)["p_value"], half_up(2/math.comb(10, 5), 4))
        json.dumps(zero, ensure_ascii=False, allow_nan=False)

    def test_pvalue_rounding_boundary_is_not_offered(self):
        for pvalue in (0.04996, 0.05, 0.05004):
            with self.assertRaises(service._TemplateUnavailable):
                testing._test_parts("t", "t", 2, pvalue)
        self.assertEqual(testing._test_parts("t", "t", 2, 0.0499)[-1]["expected"], testing._REJECT)
        self.assertEqual(testing._test_parts("t", "t", 2, 0.0501)[-1]["expected"], testing._RETAIN)

    def test_one_sample_t_skips_boundary_sample_and_uses_other_source(self):
        context = self._context([6.963, 7.963, 8.963, 9.963, 10.963])
        context.daily["study_hours"] = [1, 2, 3, 4, 5]
        sources = testing._sources(context, minimum=5)
        sources.sort(key=lambda source: source["key"] != "sleep_hours")
        with patch.object(testing, "_sources", return_value=sources):
            problem = testing.one_sample_t_problem(context, np.random.default_rng(0))
        self.assertEqual(problem["source_key"], "study_hours")
        self.assertEqual(self._answers(problem)["p_value"], 0.2302)

    def test_all_builders_try_later_candidates_after_invalid_test_result(self):
        context = self._context([5, 6, 6, 9, 7, 8, 8, 12, 9, 14] * 4)
        context.daily["study_hours"] = context.daily["sleep_hours"] * 0.5
        context.sessions = self._table_context([[20, 10], [10, 20]]).sessions
        context.sessions["study_category"] = context.sessions["environment"].map({"自宅": "医学", "カフェ": "英語"})
        original_check = testing._check_result
        builders = [testing.one_sample_t_problem, testing.welch_t_problem, testing.paired_t_problem,
                    testing.chi_square_problem, testing.fisher_exact_problem, testing.wilcoxon_problem]
        for builder in builders:
            with self.subTest(builder=builder.__name__):
                calls = []

                def reject_first(statistic, pvalue):
                    calls.append(pvalue)
                    if len(calls) == 1:
                        raise service._TemplateUnavailable("first candidate is unusable")
                    return original_check(statistic, pvalue)

                with patch.object(testing, "_check_result", side_effect=reject_first):
                    problem = builder(context, np.random.default_rng(7))
                self.assertGreaterEqual(len(calls), 3)
                self.assertTrue(problem["answer_parts"])

    def test_rare_numeric_variation_does_not_hide_one_sample_template(self):
        context = self._context([7] * 99 + [8])
        for seed in range(20):
            problem = testing.one_sample_t_problem(context, np.random.default_rng(seed))
            self.assertGreater(len({row["value"] for row in problem["data_rows"]}), 1)
            self.assertTrue(any("無作為抽出とはみなしません" in item for item in problem["assumptions"]))
        self.assertEqual(testing.one_sample_t_problem(context, np.random.default_rng(0)),
                         testing.one_sample_t_problem(context, np.random.default_rng(0)))

    def test_rare_within_group_variation_does_not_hide_welch_template(self):
        sessions = [{"date": "2020-01-01", "session_minutes": value, "environment": group,
                     "io_type": None, "study_category": None}
                    for group, values in [("自宅", [30]*99+[31]), ("カフェ", [60]*99+[61])]
                    for value in values]
        context = self._context(values=[], sessions=sessions)
        for seed in range(10):
            problem = testing.welch_t_problem(context, np.random.default_rng(seed))
            for group in ("自宅", "カフェ"):
                self.assertGreater(len({row["value"] for row in problem["data_rows"] if row["group"] == group}), 1)

    def test_rare_pair_variation_and_nonzero_differences_remain_available(self):
        t_values = [7] * 199 + [8]
        w_values = [7] * 194 + [7, 8, 7, 9, 7, 10]
        for seed in range(10):
            problem = testing.paired_t_problem(self._context(t_values), np.random.default_rng(seed))
            differences = {round(row["after"]-row["before"], 3) for row in problem["data_rows"]}
            self.assertEqual(differences, {0, 1})
            problem = testing.wilcoxon_problem(self._context(w_values), np.random.default_rng(seed))
            self.assertEqual(problem["parameters"]["effective_n"], 3)
            self.assertEqual(self._answers(problem)["p_value"], 0.25)

    def test_builders_are_deterministic_read_only_and_registered(self):
        context = self._context([5, 6, 6, 9, 7, 8, 8, 12, 9, 14] * 4)
        table_context = self._table_context([[20, 10], [10, 20]])
        context.sessions = table_context.sessions
        daily_before, sessions_before = context.daily.copy(deep=True), context.sessions.copy(deep=True)
        identifiers = {"one_sample_t_test", "welch_t_test", "paired_t_test", "chi_square_independence", "fisher_exact_test", "wilcoxon_signed_rank"}
        templates = [template for template in service._TEMPLATES if template.template_id in identifiers]
        self.assertEqual(len(templates), 6)
        for template in templates:
            with self.subTest(template=template.template_id):
                first = template.builder(context, np.random.default_rng(7))
                second = template.builder(context, np.random.default_rng(7))
                self.assertEqual(first, second)
                frozen = service._finalize_problem(template, first, context, 7)
                stored = json.loads(json.dumps(frozen, ensure_ascii=False, allow_nan=False))
                answer = self._answers(stored)
                self.assertEqual(service.grade_problem(stored, answer)["score"], 100)
                copy_snapshot = copy.deepcopy(stored)
                context.daily.loc[0, "sleep_hours"] = 99
                self.assertEqual(stored, copy_snapshot)
                context.daily.loc[0, "sleep_hours"] = daily_before.loc[0, "sleep_hours"]
        pd.testing.assert_frame_equal(context.daily, daily_before)
        pd.testing.assert_frame_equal(context.sessions, sessions_before)


if __name__ == "__main__":
    unittest.main()
