from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import exercise_service as exercise
from app_core import health_service as health
from app_core import life_health_service as life


class HealthReadEfficiencyTests(unittest.TestCase):
    start = date(2026, 9, 1)
    end = date(2026, 9, 3)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for module, attribute, folder in (
            (health, "HEALTH_LOGS_DIR", "health"),
            (health, "STUDY_LOGS_DIR", "study"),
            (health, "DAILY_PLANS_DIR", "plans"),
            (health, "NOTES_DATA_DIR", "notes"),
            (health, "FLASHCARDS_PATH", "cards.json"),
            (exercise, "EXERCISE_LOGS_DIR", "exercise"),
            (exercise, "STUDY_LOGS_DIR", "study"),
        ):
            patcher = patch.object(module, attribute, self.root / folder)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.data = {
            self.day_path("health", 1): {
                "date": "1999-01-01",  # The file date remains authoritative.
                "sleep": {"sleep_duration_min": 390, "sleep_quality": 4},
                "condition": {"physical_score": 5, "fatigue": 2, "memo": "fixture"},
            },
            self.day_path("health", 3): {},
            self.day_path("study", 1): [
                {"duration_sec": "3600"}, {"duration_sec": 90.5}, None,
                {"duration_sec": "invalid"},
            ],
            self.day_path("study", 2): {"schema_version": 0, "unknown_metadata": {"keep": True}, "sessions": [
                {"duration_sec": 600}, {"duration_sec": 1200},
            ]},
            self.day_path("study", 3): [{"duration_sec": 0}, {"duration_sec": 120.7}],
            self.day_path("plans", 1): {"tasks": [{"status": "done"}, {"status": "pending"}, None]},
            self.day_path("plans", 3): {"tasks": [{"status": "completed"}, {"status": "complete"}]},
            self.root / "notes" / "notebooks.json": {"notebooks": [
                {"sections": [{"pages": [
                    {"created_at": "2026-09-01T10:00:00", "updated_at": "2026-09-03"},
                    {"updated_at": "2026-09-03T12:00:00"},
                ]}]},
            ]},
            self.root / "cards.json": [
                {"created_at": "2026-09-01", "updated_at": "2026-09-03"},
                {"updated_at": "2026-09-03"}, "invalid",
            ],
            self.day_path("exercise", 1): {"date": "1999-01-01", "records": [
                {"exercise_id": "run", "type": "running", "duration_min": 30,
                 "distance_km": 5, "intensity": 7, "fatigue_after": 4, "source": "diary"},
                {"exercise_id": "walk", "type": "walking", "duration_min": 20,
                 "distance_km": 2, "intensity": 3, "fatigue_after": 2, "source": "daily_hub"},
                None,
            ]},
            self.day_path("exercise", 3): {"records": "invalid"},
        }
        self.original = deepcopy(self.data)
        self.reads: Counter[Path] = Counter()

        def read(path, default=None):
            path = Path(path)
            self.assertTrue(path.is_relative_to(self.root), "Only synthetic data may be read")
            self.reads[path] += 1
            return self.data.get(path, default)

        for patcher in (
            patch.object(health, "load_json_safe", side_effect=read),
            patch.object(exercise, "load_json_safe", side_effect=read),
            patch.object(Path, "exists", lambda path: path in self.data),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.assertEqual(self.data, self.original, "Read-only summaries must not mutate source records")

    def day_path(self, kind: str, day: int) -> Path:
        return self.root / kind / "2026" / "09" / f"202609{day:02}.json"

    def assert_daily_read_count(self, kind: str, expected: int) -> None:
        for day in range(1, 4):
            self.assertEqual(self.reads[self.day_path(kind, day)], expected, (kind, day))

    def expected_health_rows(self):
        rows = []
        for day, duration, count in ((1, 3690.5, 3), (2, 1800, 2), (3, 120.7, 2)):
            present = day != 2
            row = {
                "date": f"2026-09-0{day}", "bed_time": "23:30", "wake_time": "06:30",
                "sleep_duration_min": (390 if day == 1 else 420) if present else 0,
                "sleep_quality": (4 if day == 1 else 3) if present else 0,
                "morning_sleepiness": 3 if present else 0, "midnight_awakenings": 0,
                "nap_minutes": 0, "physical_score": (5 if day == 1 else 3) if present else 0,
                "fatigue": (2 if day == 1 else 3) if present else 0,
                "stress": 3 if present else 0, "focus_score": 3 if present else 0,
                "mood_score": 3 if present else 0, "memo": "fixture" if day == 1 else "",
                "study_duration_sec": duration, "study_hours": round(duration / 3600, 3),
                "has_health_log": present, "study_session_count": count,
                "completed_task_count": {1: 1, 2: 0, 3: 2}[day],
                "task_count": 0 if day == 2 else 2,
                "task_completion_rate": {1: 50.0, 2: None, 3: 100.0}[day],
                "note_count": 0 if day == 2 else 1, "card_count": 0 if day == 2 else 1,
            }
            rows.append(row)
        return rows

    def test_joined_values_and_daily_reads_include_missing_and_legacy_data(self):
        self.assertEqual(health.build_health_study_joined_summary(self.start, self.end), self.expected_health_rows())
        self.assert_daily_read_count("study", 1)
        self.assert_daily_read_count("plans", 1)
        self.assertEqual(self.reads[self.root / "cards.json"], 1)
        self.assertEqual(self.reads[self.root / "notes" / "notebooks.json"], 1)
        self.assertEqual(self.reads[self.day_path("health", 1)], 1)
        self.assertEqual(self.reads[self.day_path("health", 2)], 0)
        self.assertEqual(self.reads[self.day_path("health", 3)], 1)

    def test_health_page_reuses_summaries_without_changing_inputs_or_columns(self):
        study = health.build_study_summary_by_date(self.start, self.end)
        summary = health.build_health_summary(self.start, self.end, study_summaries=study)
        original = deepcopy((study, summary))
        joined = health.build_health_study_joined_summary(
            self.start, self.end, health_summary=summary, study_summaries=study,
        )
        self.assertEqual(joined, self.expected_health_rows())
        self.assertEqual((study, summary), original)
        self.assertNotIn("study_session_count", summary[0])
        self.assert_daily_read_count("study", 1)
        self.assertEqual(self.reads[self.day_path("health", 1)], 1)

    def test_supplied_empty_knowledge_and_study_data_do_not_trigger_fallback_reads(self):
        self.assertEqual(health._knowledge_counts(self.start, [], []), {"note_count": 0, "card_count": 0})
        summary = health.build_health_summary(self.start, self.end, study_summaries={})
        self.assertTrue(all(row["study_duration_sec"] == 0 for row in summary))
        self.assert_daily_read_count("study", 0)
        self.assertEqual(self.reads[self.root / "cards.json"], 0)

    def test_exercise_page_reuses_records_and_keeps_summary_results(self):
        expected = exercise.build_exercise_study_joined_summary(self.start, self.end)
        self.reads.clear()
        records = exercise.load_exercise_logs_range(self.start, self.end)
        original = deepcopy(records)
        actual = exercise.build_exercise_study_joined_summary(self.end, self.start, exercise_records=records)
        self.assertEqual(actual, expected)
        self.assertEqual(actual[0], {
            "date": "2026-09-01", "record_count": 2, "exercise_duration_min": 50,
            "distance_km": 7.0, "avg_intensity": 5.0, "avg_fatigue_after": 3.0,
            "study_duration_sec": 3690.5, "study_hours": 1.025, "has_exercise_log": True,
        })
        self.assertFalse(actual[2]["has_exercise_log"])
        self.assertEqual(actual[1]["study_duration_sec"], 1800)
        self.assertEqual(actual[1]["study_hours"], 0.5)
        self.assertEqual(records, original)
        self.assertEqual(self.reads[self.day_path("exercise", 1)], 1)
        self.assertEqual(self.reads[self.day_path("exercise", 3)], 1)
        self.assert_daily_read_count("study", 1)

    def test_dashboard_reads_each_source_once_and_keeps_details_and_source_values(self):
        rows = life.build_life_health_dashboard_summary(self.start, self.end)
        self.assert_daily_read_count("study", 1)
        self.assert_daily_read_count("plans", 1)
        self.assertEqual(self.reads[self.day_path("exercise", 1)], 1)
        self.assertEqual(self.reads[self.day_path("exercise", 3)], 1)
        self.assertEqual(self.reads[self.root / "cards.json"], 1)
        expected = {
            "date": "2026-09-01", "has_health_log": True, "sleep_duration_min": 390,
            "sleep_quality": 4, "morning_sleepiness": 3, "fatigue": 2, "stress": 3,
            "focus_score": 3, "mood_score": 3, "physical_score": 5,
            "study_duration_sec": 3690.5, "study_hours": 1.025, "study_session_count": 3,
            "completed_task_count": 1, "task_count": 2, "task_completion_rate": 50.0,
            "note_count": 1, "card_count": 1, "has_exercise_log": True,
            "exercise_record_count": 2, "exercise_duration_min": 50, "exercise_distance_km": 7.0,
            "exercise_avg_intensity": 5.0, "exercise_avg_fatigue_after": 3.0,
            "exercise_types": "ランニング / ウォーキング", "exercise_pace_min_per_km": 8.0,
        }
        self.assertEqual(rows[0], expected)
        self.assertFalse(rows[1]["has_health_log"])
        self.assertEqual(rows[1]["study_duration_sec"], 1800)
        self.assertEqual(rows[1]["study_session_count"], 2)
        self.assertEqual(rows[2]["study_duration_sec"], 120.7)
        self.assertAlmostEqual(life.calculate_life_health_kpis(rows)["total_study_sec"], 5611.2)
        self.assertEqual(rows[1]["task_completion_rate"], None)
        self.assertEqual(rows, life.build_life_health_dashboard_summary(self.end, self.start))

    def test_explicit_empty_exercise_inputs_avoid_all_reads(self):
        rows = exercise.build_exercise_summary(
            self.end, self.start, exercise_records=[], study_duration_by_date={},
        )
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(not row["has_exercise_log"] and row["study_duration_sec"] == 0 for row in rows))
        self.assertEqual(self.reads, Counter())


if __name__ == "__main__":
    unittest.main()
