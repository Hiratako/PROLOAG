from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from app_core import diary_service, substance_service
from app_core.analysis_statistics import bootstrap_interval, summarize_difference, summarize_rate_difference
from app_core.caffeine_timing_service import (
    build_session_timing_frame,
    caffeine_before_session,
    with_caffeine_recording_status,
)
from app_core.daily_analysis_service import build_calendar_study_totals
from app_core.performance_analysis_service import (
    build_daily_feature_frame,
    calculate_performance_delta,
    summarize_high_performance_profile,
)


class AnalysisStatisticsTests(unittest.TestCase):
    def test_paired_point_and_interval_use_median_of_pair_differences(self) -> None:
        left = [0.0, 10.0, 11.0] * 30
        right = [0.0, 1.0, 10.0] * 30
        result = summarize_difference(left, right, paired=True)
        self.assertEqual(result["median_difference"], 1.0)
        self.assertEqual((result["interval_low"], result["interval_high"]), (1.0, 1.0))
        self.assertEqual(result["group_a_n"], 90)

    def test_paired_missing_values_remove_the_same_pair(self) -> None:
        left = [10, None, 30, "invalid"]
        right = [5, 20, None, 8]
        result = summarize_difference(left, right, paired=True)
        self.assertEqual((result["group_a_n"], result["group_b_n"]), (1, 1))
        self.assertEqual(result["median_difference"], 5)
        self.assertEqual(bootstrap_interval(left, right, paired=True), (5, 5))

    def test_unequal_paired_lengths_do_not_invent_pairs(self) -> None:
        result = summarize_difference([1, 2], [1], paired=True)
        self.assertIsNone(result["median_difference"])
        self.assertEqual((result["group_a_n"], result["group_b_n"]), (0, 0))
        self.assertEqual(bootstrap_interval([1, 2], [1], paired=True), (None, None))

    def test_unpaired_median_definition_is_unchanged(self) -> None:
        result = summarize_difference([0, 10, 11, None], [0, 1, 10])
        self.assertEqual(result["median_difference"], 9)
        self.assertEqual((result["group_a_median"], result["group_b_median"]), (10, 1))
        self.assertEqual((result["group_a_n"], result["group_b_n"]), (3, 3))

    def test_binary_recording_rates_use_mean_and_percentage_point_difference(self) -> None:
        frame = pd.DataFrame({
            "outcome": [100.0] * 10 + [0.0] * 30,
            "running_recorded": [True] * 6 + [False] * 4 + [True] * 12 + [False] * 18,
        })
        profile, _ = summarize_high_performance_profile(frame, "outcome")
        rate = profile.iloc[0]
        self.assertEqual(rate["statistic"], "rate")
        self.assertEqual(rate["comparison_label"], "記録率差")
        self.assertAlmostEqual(rate["high_value"], 60)
        self.assertAlmostEqual(rate["other_value"], 40)
        self.assertAlmostEqual(rate["difference"], 20)
        expected_interval = bootstrap_interval(
            frame.loc[:9, "running_recorded"], frame.loc[10:, "running_recorded"],
            seed=20260721, statistic="mean",
        )
        self.assertAlmostEqual(rate["interval_low"], expected_interval[0] * 100)
        self.assertAlmostEqual(rate["interval_high"], expected_interval[1] * 100)

    def test_continuous_profile_values_keep_their_median_definition(self) -> None:
        frame = pd.DataFrame({
            "outcome": [100.0] * 3 + [0.0] * 9,
            "sleep_hours": [0, 10, 11] + [0, 1, 10] * 3,
        })
        profile, _ = summarize_high_performance_profile(frame, "outcome")
        row = profile.iloc[0]
        self.assertEqual(row["comparison_label"], "中央値差")
        self.assertEqual(row["difference"], 9)

    def test_rate_summary_rejects_nonbinary_values(self) -> None:
        with self.assertRaises(ValueError):
            summarize_rate_difference([0, 2], [0, 1])
        result = summarize_rate_difference([], [False])
        self.assertIsNone(result["rate_difference"])
        self.assertEqual((result["interval_low"], result["interval_high"]), (None, None))


class CalendarAverageTests(unittest.TestCase):
    def test_sparse_records_are_filled_on_calendar_days_in_memory(self) -> None:
        source = pd.DataFrame({
            "date": [date(2026, 8, 1), date(2026, 8, 14)],
            "duration_sec": [7 * 3600, 7 * 3600],
            "money": [700, 700],
        })
        before = source.copy(deep=True)
        result = build_calendar_study_totals(source, date(2026, 8, 1), date(2026, 8, 14))
        self.assertEqual(len(result), 14)
        self.assertEqual(result.iloc[-1]["hours_ma"], 1)
        self.assertEqual(result.iloc[-1]["money_ma"], 100)
        self.assertEqual(result.iloc[-1]["cum_hours"], 14)
        self.assertEqual(result["sessions"].sum(), 2)
        self.assertEqual(result.iloc[1]["study_hours"], 0)
        pd.testing.assert_frame_equal(source, before)

    def test_empty_calendar_and_reversed_dates_are_supported(self) -> None:
        result = build_calendar_study_totals(pd.DataFrame(), date(2026, 8, 3), date(2026, 8, 1))
        self.assertEqual(result["date"].dt.date.tolist(), [date(2026, 8, day) for day in (1, 2, 3)])
        self.assertTrue(result["hours_ma"].eq(0).all())


class TemporaryAnalysisDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def write_daily(self, folder: str, target: date, payload: object) -> Path:
        path = self.root / folder / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def write_study(self, target: date, hours: float) -> None:
        self.write_daily("study_logs", target, [{
            "start_at": f"{target.isoformat()}T10:00:00",
            "duration_sec": hours * 3600,
            "condition": {"category": "医学", "io_type": "アウトプット"},
        }])

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*.json")}

    def test_feature_context_preserves_previous_28_days_and_next_observed_day(self) -> None:
        target = date(2026, 8, 15)
        for offset in range(1, 29):
            self.write_study(target - timedelta(days=offset), 2)
        self.write_study(target, 5)
        self.write_study(target + timedelta(days=1), 4)
        self.write_daily("health_logs", target + timedelta(days=1), {
            "sleep": {"sleep_duration_min": 450},
            "condition": {"focus_score": 4, "fatigue": 2},
        })
        before = self.snapshot()
        frame = build_daily_feature_frame(target, target, self.root, as_of_date=date(2026, 8, 20))
        self.assertEqual(frame["date"].dt.date.tolist(), [target])
        row = frame.iloc[0]
        self.assertEqual(row["previous_study_hours"], 2)
        self.assertEqual(row["prior_7d_study_hours"], 2)
        self.assertEqual(row["next_study_hours"], 4)
        self.assertEqual(row["next_sleep_hours"], 7.5)
        self.assertEqual(row["next_focus_score"], 4)
        self.assertEqual(calculate_performance_delta(frame, "study_hours", "直近28日中央値差").iloc[0], 3)
        self.assertEqual(calculate_performance_delta(frame, "study_category_医学_hours", "直近28日中央値差").iloc[0], 3)
        wider = build_daily_feature_frame(target - timedelta(days=3), target, self.root, as_of_date=date(2026, 8, 20))
        self.assertEqual(wider.iloc[-1]["prior_28d_study_hours_median"], row["prior_28d_study_hours_median"])
        self.assertEqual(wider.iloc[-1]["next_study_hours"], row["next_study_hours"])
        self.assertEqual(self.snapshot(), before)

    def test_future_stored_defaults_are_not_next_day_observations(self) -> None:
        target = date(2026, 8, 20)
        self.write_study(target, 3)
        future = target + timedelta(days=1)
        self.write_daily("study_logs", future, [])
        self.write_daily("health_logs", future, {
            "sleep": {"sleep_duration_min": 0},
            "condition": {"focus_score": 3, "fatigue": 3},
        })
        frame = build_daily_feature_frame(target, future, self.root, as_of_date=target)
        self.assertTrue(pd.isna(frame.iloc[0]["next_study_hours"]))
        self.assertTrue(pd.isna(frame.iloc[0]["next_sleep_hours"]))
        self.assertTrue(pd.isna(frame.iloc[0]["next_condition_balance"]))
        self.assertFalse(frame.iloc[1]["study_recorded"])
        self.assertTrue(pd.isna(frame.iloc[1]["study_hours"]))

    def test_missing_days_remain_missing_without_creating_records(self) -> None:
        target = date(2026, 8, 20)
        self.write_study(target, 3)
        before = self.snapshot()
        frame = build_daily_feature_frame(target, target, self.root, as_of_date=date(2026, 8, 22))
        self.assertTrue(pd.isna(frame.iloc[0]["next_study_hours"]))
        self.assertTrue(pd.isna(frame.iloc[0]["previous_study_hours"]))
        self.assertTrue(pd.isna(calculate_performance_delta(frame, "study_hours", "直近28日中央値差").iloc[0]))
        self.assertEqual(self.snapshot(), before)

    def test_session_timing_reads_the_day_before_the_display_period(self) -> None:
        target = date(2026, 8, 2)
        previous = target - timedelta(days=1)
        self.write_daily("diary_entries", previous, {
            "date": previous.isoformat(),
            "caffeine_mg": 200,
            "caffeine_untimed_mg": 0,
            "caffeine_records": [{
                "caffeine_id": "cross_midnight",
                "taken_at": f"{previous.isoformat()}T23:00:00",
                "amount_mg": 200,
            }],
        })
        self.write_daily("study_logs", target, [{
            "start_at": f"{target.isoformat()}T01:00:00", "duration_sec": 1800,
        }])
        before = self.snapshot()
        with patch.object(diary_service, "DIARY_ENTRIES_DIR", self.root / "diary_entries"), patch.object(
            substance_service, "SUBSTANCE_PROFILES_PATH", self.root / "settings" / "substance_profiles.json",
        ):
            frame = build_session_timing_frame(target, target, self.root)
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["minutes_since_last_intake"], 120)
        self.assertEqual(frame.iloc[0]["caffeine_6h_mg"], 200)
        self.assertEqual(frame.iloc[0]["timing_band"], "91-180分")
        self.assertEqual(self.snapshot(), before)


class CaffeineTimingTests(unittest.TestCase):
    def test_six_hour_boundary_excludes_future_and_nonpositive_events(self) -> None:
        start = datetime(2026, 8, 2, 1, 0)
        records = [
            {"taken_at": "2026-08-01T19:00:00", "amount_mg": 50},
            {"taken_at": "2026-08-01T18:59:59", "amount_mg": 80},
            {"taken_at": "2026-08-01T23:00:00", "amount_mg": 200.5},
            {"taken_at": "2026-08-02T00:30:00", "amount_mg": 0},
            {"taken_at": "2026-08-02T02:00:00", "amount_mg": 100},
            {"taken_at": "invalid", "amount_mg": 100},
        ]
        result = caffeine_before_session(records, start)
        self.assertEqual(result["last_intake_at"], datetime(2026, 8, 1, 23, 0))
        self.assertEqual(result["minutes_since_last_intake"], 120)
        self.assertEqual(result["caffeine_6h_mg"], 250.5)

    def test_untimed_amount_is_never_assigned_an_intake_time(self) -> None:
        result = caffeine_before_session([], datetime(2026, 8, 2, 1), has_untimed=True)
        self.assertIsNone(result["last_intake_at"])
        self.assertIsNone(result["minutes_since_last_intake"])
        self.assertEqual(result["timing_band"], "時刻不明のみ")

    def test_absent_diary_is_not_part_of_the_recorded_zero_group(self) -> None:
        frame = pd.DataFrame({
            "has_diary_log": [False, True, True],
            "caffeine_mg": [0, 0, 200],
            "timed_caffeine_mg": [0, 0, 200],
            "caffeine_untimed_mg": [0, 0, 0],
            "caffeine_after_18_mg": [0, 0, 0],
            "caffeine_record_count": [0, 0, 1],
            "study_hours": [9, 1, 3],
        })
        before = frame.copy(deep=True)
        result = with_caffeine_recording_status(frame)
        self.assertEqual(result["has_no_caffeine_record"].tolist(), [False, True, False])
        self.assertTrue(pd.isna(result.iloc[0]["caffeine_mg"]))
        self.assertTrue(pd.isna(result.iloc[0]["timed_caffeine_mg"]))
        self.assertEqual(result.loc[result["has_no_caffeine_record"], "study_hours"].mean(), 1)
        self.assertEqual(result.iloc[1]["caffeine_mg"], 0)
        pd.testing.assert_frame_equal(frame, before)


if __name__ == "__main__":
    unittest.main()
