from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def half_up(value, decimals):
    """Independent expected-value oracle; do not call the production helper."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

from app_core.statistics_practice_service import (
    TOPIC_LABELS,
    NoAvailableProblemError,
    available_problem_summary,
    generate_problem,
    grade_problem,
    load_attempts,
    save_attempt,
    template_catalog,
)


class StatisticsPracticeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="proloag_statistics_test_")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.start = date(2020, 1, 1)
        self.end = self.start + timedelta(days=39)
        self._seed_rich_fixture()

    def _write_daily(self, folder: str, target: date, payload: object) -> None:
        path = self.root / folder / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _seed_rich_fixture(self) -> None:
        environments = ("自宅", "カフェ", "図書館")
        io_types = ("インプット", "アウトプット", "両方")
        categories = ("医学", "英語", "IT")
        for offset in range(40):
            target = self.start + timedelta(days=offset)
            first_minutes = 35 + (offset % 7) * 8
            second_minutes = 25 + ((offset * 3) % 8) * 6
            sessions = [
                {
                    "session_id": f"session_{target:%Y%m%d}_a",
                    "start_at": f"{target.isoformat()}T08:00:00",
                    "end_at": f"{target.isoformat()}T09:30:00",
                    "duration_sec": first_minutes * 60,
                    "money": 999999,
                    "memo": "この自由記述は統計問題へ出してはいけない",
                    "condition": {
                        "environment": environments[offset % 3],
                        "io_type": io_types[(offset * 2) % 3],
                        "category": categories[offset % 3],
                    },
                },
                {
                    "session_id": f"session_{target:%Y%m%d}_b",
                    "start_at": f"{target.isoformat()}T15:00:00",
                    "end_at": f"{target.isoformat()}T16:30:00",
                    "duration_sec": second_minutes * 60,
                    "money": 888888,
                    "memo": "秘密のメモ",
                    "condition": {
                        "environment": environments[(offset + 1) % 3],
                        "io_type": io_types[(offset + 1) % 3],
                        "category": categories[(offset + 2) % 3],
                    },
                },
            ]
            self._write_daily("study_logs", target, sessions)
            self._write_daily(
                "health_logs",
                target,
                {
                    "sleep": {
                        "sleep_duration_min": 350 + (offset % 9) * 17,
                        "sleep_quality": 1 + offset % 5,
                        "morning_sleepiness": 1 + (offset * 2) % 5,
                    },
                    "condition": {
                        "focus_score": 1 + (offset * 3) % 5,
                        "fatigue": 1 + (offset * 2) % 5,
                        "stress": 1 + (offset * 4) % 5,
                    },
                },
            )
            self._write_daily(
                "exercise_logs",
                target,
                {
                    "records": [
                        {
                            "type": "running",
                            "duration_min": 15 + (offset % 8) * 4,
                            "distance_km": 2 + (offset % 7) * 0.45,
                            "intensity": 1 + offset % 5,
                            "fatigue_after": 1 + (offset * 2) % 5,
                        }
                    ]
                },
            )

    def _source_snapshot(self) -> dict[str, tuple[str, int]]:
        result: dict[str, tuple[str, int]] = {}
        for folder in ("study_logs", "health_logs", "exercise_logs", "diary_entries"):
            directory = self.root / folder
            if not directory.exists():
                continue
            for path in directory.rglob("*.json"):
                raw = path.read_bytes()
                result[str(path.relative_to(self.root))] = (hashlib.sha256(raw).hexdigest(), path.stat().st_mtime_ns)
        return result

    def _correct_answers(self, problem: dict) -> dict:
        return {part["key"]: part["expected"] for part in problem["answer_parts"]}

    def test_summary_exposes_all_twelve_templates_and_generation_is_read_only(self) -> None:
        before = self._source_snapshot()
        summary = available_problem_summary(self.start, self.end, self.root)
        personal_catalog = template_catalog(data_mode="personal")
        original_ids = {
            "descriptive_mean", "descriptive_sample_sd", "category_proportion",
            "conditional_probability", "binomial_moments", "z_score", "mean_se_ci",
            "proportion_ci", "contingency_expected", "pearson_correlation",
            "simple_regression", "correlation_interpretation",
        }
        self.assertTrue(original_ids.issubset({item["template_id"] for item in personal_catalog}))
        self.assertEqual(summary["available_count"], len(personal_catalog))
        self.assertEqual(summary["total_templates"], len(personal_catalog))
        personal_topics = {item["topic"] for item in personal_catalog}
        self.assertTrue(personal_topics.issubset(summary["topic_counts"]))
        self.assertTrue(personal_topics.issubset(TOPIC_LABELS))
        self.assertTrue(all(item["available"] for item in summary["templates"]))
        self.assertGreaterEqual(summary["data_summary"]["session_rows"], 80)

        required = {
            "problem_id",
            "template_id",
            "topic",
            "topic_label",
            "title",
            "prompt",
            "observation_unit",
            "period",
            "data_columns",
            "data_rows",
            "answer_parts",
            "hints",
            "explanation",
            "assumptions",
            "snapshot",
        }
        for position, topic in enumerate(sorted(personal_topics)):
            problem = generate_problem(self.start, self.end, topic, seed=200 + position, root=self.root)
            self.assertTrue(required.issubset(problem))
            self.assertEqual(problem["topic"], topic)
            self.assertEqual(problem["data_rows"], problem["snapshot"]["data_rows"])
            self.assertEqual(problem["period"], problem["snapshot"]["period"])
            json.dumps(problem, ensure_ascii=False, allow_nan=False)
            serialized = json.dumps(problem, ensure_ascii=False)
            self.assertNotIn("秘密のメモ", serialized)
            self.assertNotIn("この自由記述", serialized)
            self.assertNotIn("999999", serialized)
            self.assertNotIn("888888", serialized)
        self.assertEqual(self._source_snapshot(), before)

    def test_same_seed_freezes_selection_values_answer_key_and_problem_id(self) -> None:
        first = generate_problem(self.start, self.end, "mixed", seed=71234, root=self.root)
        second = generate_problem(self.start, self.end, "mixed", seed=71234, root=self.root)
        self.assertEqual(first, second)
        self.assertEqual(first["snapshot"]["seed"], 71234)
        self.assertTrue(first["snapshot"]["rounding"])
        self.assertEqual(
            {part["key"]: part["expected"] for part in first["answer_parts"]},
            {part["key"]: part["expected"] for part in first["snapshot"]["answer_key"]},
        )

    def test_grade_is_deterministic_and_awards_part_credit(self) -> None:
        problem = {
            "answer_parts": [
                {
                    "key": "mean",
                    "label": "平均",
                    "kind": "numeric",
                    "expected": 12.34,
                    "tolerance": 0.005,
                    "decimals": 2,
                    "unit": "時間",
                    "options": [],
                    "weight": 2,
                },
                {
                    "key": "interpretation",
                    "label": "解釈",
                    "kind": "choice",
                    "expected": "相関は因果を示さない",
                    "options": ["相関は因果を示さない", "相関は因果を証明する"],
                    "weight": 1,
                },
            ]
        }
        full = grade_problem(problem, {"mean": "１２．３４", "interpretation": "相関は因果を示さない"})
        self.assertEqual(full["score"], 100)
        self.assertTrue(full["all_correct"])
        partial = grade_problem(problem, {"mean": 99, "interpretation": "相関は因果を示さない"})
        self.assertAlmostEqual(partial["score"], 33.3)
        self.assertEqual(partial["correct_count"], 1)
        self.assertEqual(partial["earned_points"], 1)
        self.assertEqual(partial["max_points"], 3)
        self.assertEqual(grade_problem(problem, {"mean": 99, "interpretation": "相関は因果を示さない"}), partial)

    def test_saved_attempts_use_separate_daily_store_atomic_generations_and_filters(self) -> None:
        problem = generate_problem(self.start, self.end, "correlation", seed=30, root=self.root)
        answers = self._correct_answers(problem)
        grade = grade_problem(problem, answers)
        before = self._source_snapshot()
        first_now = datetime(2026, 8, 30, 12, 34, 56)
        second_now = datetime(2026, 8, 30, 13, 0, 0)
        with patch("app_core.statistics_practice_service.app_now", return_value=first_now):
            first = save_attempt(problem, answers, grade, self.root)
        with patch("app_core.statistics_practice_service.app_now", return_value=second_now):
            second = save_attempt(problem, answers, grade, self.root)

        path = self.root / "statistics_practice" / "2026" / "08" / "20260830.json"
        self.assertTrue(path.exists())
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["schema_version"], 1)
        self.assertEqual(stored["date"], "2026-08-30")
        self.assertEqual(len(stored["attempts"]), 2)
        self.assertEqual(first["score"], 100)
        self.assertEqual(second["max_score"], 100)
        self.assertEqual(first["problem_snapshot"], problem)
        generations = list((path.parent / "backups" / "auto_save" / path.name).glob("*.json"))
        self.assertEqual(len(generations), 1)

        loaded = load_attempts(date(2026, 8, 30), date(2026, 8, 30), self.root)
        self.assertEqual([datetime.fromisoformat(item["attempted_at"]) for item in loaded], [second_now, first_now])
        self.assertEqual(load_attempts(date(2026, 8, 31), date(2026, 8, 31), self.root), [])
        self.assertEqual(self._source_snapshot(), before)

    def test_empty_root_reports_reasons_and_does_not_create_files(self) -> None:
        empty = self.root / "empty"
        summary = available_problem_summary(self.start, self.end, empty)
        self.assertEqual(summary["available_count"], 0)
        self.assertTrue(summary["issues"])
        with self.assertRaises(NoAvailableProblemError):
            generate_problem(self.start, self.end, root=empty, seed=1)
        self.assertFalse(empty.exists())

    def test_frozen_formula_answers_match_displayed_values(self) -> None:
        from app_core import statistics_practice_service as service

        context = service._load_context(self.start, self.end, self.root)
        by_id = {template.template_id: template for template in service._TEMPLATES}

        mean_problem = service._finalize_problem(
            by_id["descriptive_mean"],
            service._build_payload(by_id["descriptive_mean"], context, 42),
            context,
            42,
        )
        values = np.array([row["value"] for row in mean_problem["data_rows"]], dtype=float)
        self.assertAlmostEqual(mean_problem["answer_parts"][0]["expected"], half_up(float(values.mean()), 2))

        regression_problem = service._finalize_problem(
            by_id["simple_regression"],
            service._build_payload(by_id["simple_regression"], context, 99),
            context,
            99,
        )
        x = np.array([row["x"] for row in regression_problem["data_rows"]], dtype=float)
        y = np.array([row["y"] for row in regression_problem["data_rows"]], dtype=float)
        slope = float(np.sum((x - x.mean()) * (y - y.mean())) / np.sum((x - x.mean()) ** 2))
        intercept = float(y.mean() - slope * x.mean())
        answers = {part["key"]: part["expected"] for part in regression_problem["answer_parts"]}
        self.assertEqual(answers["slope"], half_up(slope, 2))
        self.assertEqual(answers["intercept"], half_up(intercept, 2))

    def test_ordinal_scores_and_repeated_time_series_have_explicit_assumptions(self) -> None:
        from app_core import statistics_practice_service as service

        numeric = service._numeric_assumptions({"key": "focus_score", "observation_unit": "日"})
        correlation = service._correlation_assumptions(
            {"x_key": "sleep_hours", "y_key": "focus_score", "observation_unit": "日"}
        )
        self.assertTrue(any("順序尺度" in item and "等間隔" in item for item in numeric))
        self.assertTrue(any("順序尺度" in item and "等間隔" in item for item in correlation))
        self.assertTrue(any("時系列相関" in item for item in numeric))

    def test_pair_sampling_rejects_constant_or_rounded_constant_subsamples(self) -> None:
        from app_core import statistics_practice_service as service

        source = {
            "rows": None,
            "x_label": "x",
            "x_unit": "",
            "y_label": "y",
            "y_unit": "",
        }
        constant = service.pd.DataFrame(
            {"date": [f"2020-01-{day:02d}" for day in range(1, 9)], "x": [1.0] * 8, "y": [2.0] * 8}
        )
        with patch.object(service, "_sample_frame", return_value=constant):
            with self.assertRaises(service._TemplateUnavailable):
                service._sample_pair_rows(source, np.random.default_rng(1))

    def test_save_rejects_mismatched_grade_before_creating_history(self) -> None:
        problem = generate_problem(self.start, self.end, "descriptive", seed=9, root=self.root)
        answers = self._correct_answers(problem)
        grade = grade_problem(problem, answers)
        grade["score"] = 0
        with self.assertRaisesRegex(ValueError, "採点結果"):
            save_attempt(problem, answers, grade, self.root)
        self.assertFalse((self.root / "statistics_practice").exists())

    def test_corrupt_history_is_never_overwritten(self) -> None:
        problem = generate_problem(self.start, self.end, "descriptive", seed=10, root=self.root)
        answers = self._correct_answers(problem)
        grade = grade_problem(problem, answers)
        now = datetime(2026, 8, 29, 10, 0, 0)
        path = self.root / "statistics_practice" / "2026" / "08" / "20260829.json"
        path.parent.mkdir(parents=True)
        corrupt = b'{"schema_version":1,"attempts":['
        path.write_bytes(corrupt)
        with patch("app_core.statistics_practice_service.app_now", return_value=now):
            with self.assertRaises(ValueError):
                save_attempt(problem, answers, grade, self.root)
        self.assertEqual(path.read_bytes(), corrupt)

    def test_wrong_history_schema_is_never_repaired_during_append(self) -> None:
        problem = generate_problem(self.start, self.end, "descriptive", seed=10, root=self.root)
        answers = self._correct_answers(problem)
        grade = grade_problem(problem, answers)
        now = datetime(2026, 8, 29, 10, 0, 0)
        path = self.root / "statistics_practice" / "2026" / "08" / "20260829.json"
        path.parent.mkdir(parents=True)
        original = {"schema_version": 999, "date": "2026-08-29", "attempts": []}
        path.write_text(json.dumps(original), encoding="utf-8")
        with patch("app_core.statistics_practice_service.app_now", return_value=now):
            with self.assertRaisesRegex(ValueError, "スキーマ"):
                save_attempt(problem, answers, grade, self.root)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_concurrent_attempts_are_serialized_without_lost_updates(self) -> None:
        problem = generate_problem(self.start, self.end, "probability", seed=11, root=self.root)
        answers = self._correct_answers(problem)
        grade = grade_problem(problem, answers)
        now = datetime(2026, 8, 28, 9, 0, 0)
        errors: list[Exception] = []
        barrier = threading.Barrier(5)

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                save_attempt(problem, answers, grade, self.root)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch("app_core.statistics_practice_service.app_now", return_value=now):
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        loaded = load_attempts(now.date(), now.date(), self.root)
        self.assertEqual(len(loaded), 5)
        self.assertEqual(len({item["attempt_id"] for item in loaded}), 5)


if __name__ == "__main__":
    unittest.main()
