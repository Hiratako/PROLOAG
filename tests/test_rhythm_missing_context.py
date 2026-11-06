from __future__ import annotations

import ast
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from app_core import diary_service, substance_service
from app_core.analysis_dates import parse_analysis_datetime
from app_core.caffeine_timing_service import build_session_timing_frame, parse_record_datetime, with_caffeine_recording_status
from app_core.daily_analysis_service import analysis_context_window
from app_core.json_store import load_json_safe
from app_core.performance_analysis_service import _exercise_summary, _health_summary, _plan_summary, factor_groups, summarize_study_records, load_study_analysis_context, build_daily_feature_frame, caffeine_daily_measurement
from app_core.study_log_service import get_study_log_path
from app_core.study_record_reader import unpack_study_document


PAGE = Path(__file__).resolve().parents[1] / "pages" / "16_カフェイン・運動分析.py"
FUNCTIONS = {
    "date_range", "num", "seconds_label", "minutes_label", "caffeine_band", "daily_path",
    "load_study_context", "load_running_summary", "load_plan_summary", "caffeine_timing_summary",
    "load_daily_row", "build_dashboard_frame", "build_caffeine_event_frame", "grouped_metric_summary",
    "render_group_table", "render_condition_group_table", "render_lag_table", "display_table", "measured_label",
    "build_comments", "observed_mean_label", "correlation_text",
}


class RhythmMissingContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proloag-rhythm-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = date(2025, 12, 31)
        self.reads = Counter()

        def read(path, default):
            path = Path(path)
            self.assertTrue(path.resolve().is_relative_to(self.root.resolve()))
            self.reads[path] += 1
            return load_json_safe(path, default)

        for patcher in (
            patch("app_core.performance_analysis_service.load_json_safe", side_effect=read),
            patch.object(diary_service, "app_now", return_value=datetime(2026, 1, 10, 12)),
            patch.object(diary_service, "substance_profile_map", return_value={"caffeine": diary_service.LEGACY_CAFFEINE_PROFILE}),
            patch.object(substance_service, "SUBSTANCE_PROFILES_PATH", self.root / "settings" / "substance_profiles.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.page = dict(globals(), ROOT=self.root, app_today=lambda: date(2026, 1, 10),
                         load_json_safe=read, normalize_diary_entry=diary_service.normalize_diary_entry,
                         load_substance_profiles=lambda **kwargs: [diary_service.LEGACY_CAFFEINE_PROFILE])
        tree = ast.parse(PAGE.read_text(encoding="utf-8-sig"))
        selected = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS], type_ignores=[])
        exec(compile(selected, str(PAGE), "exec"), self.page)

    def write(self, folder, target, payload):
        path = self.root / folder / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def frame(self, start=None, end=None, **kwargs):
        return self.page["build_dashboard_frame"](start or self.target, end or start or self.target, **kwargs)

    def snapshot(self):
        return {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*.json")}

    def test_missing_files_are_not_zero_or_no_running(self):
        row = self.frame().iloc[0]
        for column in ("study_hours", "study_session_count", "sleep_hours", "fatigue", "running_count", "running_distance_km", "planned_hours"):
            self.assertTrue(pd.isna(row[column]), column)
        self.assertEqual(row["running_state"], "未記録")
        self.assertFalse(row["study_recorded"])
        self.assertFalse(row["health_recorded"])

    def test_partial_health_and_running_do_not_import_form_defaults(self):
        self.write("health_logs", self.target, {"sleep": {"sleep_quality": 0}, "condition": {"focus_score": 0}})
        self.write("exercise_logs", self.target, {"records": [{"type": "running", "distance_km": 0, "fatigue_after": 0}]})
        row = self.frame().iloc[0]
        for column in ("sleep_hours", "fatigue", "stress", "morning_sleepiness", "running_avg_intensity", "running_duration_min"):
            self.assertTrue(pd.isna(row[column]), column)
        for column in ("sleep_quality", "focus_score", "running_distance_km", "running_avg_fatigue_after"):
            self.assertEqual(row[column], 0, column)
        self.assertEqual(row["running_state"], "ランニングあり")

    def test_explicit_empty_records_and_zero_measurements_remain_valid(self):
        self.write("study_logs", self.target, [])
        self.write("exercise_logs", self.target, {"records": []})
        first = self.frame().iloc[0]
        self.assertEqual(first["study_hours"], 0)
        self.assertEqual(first["study_session_count"], 0)
        self.assertEqual(first["running_state"], "ランニングなし")
        self.write("study_logs", self.target, {"sessions": [{"start_at": f"{self.target}T10:00", "duration_sec": 0, "condition": {"effort": 0}}], "custom": 123})
        second = self.frame().iloc[0]
        self.assertEqual(second["avg_effort"], 0)
        self.assertEqual(second["study_hours"], 0)
        self.assertEqual(second["study_session_count"], 1)

    def test_missing_duration_never_counts_as_zero_study(self):
        self.write("study_logs", self.target, {"start_at": f"{self.target}T10:00", "condition": {"effort": 0}})
        row = self.frame().iloc[0]
        self.assertTrue(pd.isna(row["study_hours"]))
        self.assertEqual(row["avg_effort"], 0)
        self.assertEqual(row["study_session_count"], 1)

    def test_end_only_legacy_records_keep_duration_and_top_level_classification(self):
        self.write("study_logs", self.target, [{"end_at": f"{self.target}T20:00", "duration_sec": 3600.5, "category": "IT", "io_type": "output"}, None])
        row = self.frame().iloc[0]
        self.assertEqual(row["study_hours"], 3600.5 / 3600)
        self.assertEqual(row["output_hours"], 3600.5 / 3600)
        self.assertEqual(row["study_category_IT_hours"], 3600.5 / 3600)
        self.assertTrue(pd.isna(row["night_study_hours"]))
        other = build_daily_feature_frame(self.target, self.target, self.root, as_of_date=date(2026, 1, 10)).iloc[0]
        self.assertEqual(other["study_hours"], row["study_hours"])

    def test_missing_or_invalid_caffeine_field_is_not_explicit_zero(self):
        for payload in ({}, {"caffeine_mg": None}, {"caffeine_mg": ""}, {"caffeine_mg": "invalid"}):
            with self.subTest(payload=payload):
                self.write("diary_entries", self.target, payload)
                row = self.frame().iloc[0]
                self.assertTrue(row["has_diary_log"])
                self.assertFalse(row["has_caffeine_log"])
                self.assertFalse(row["has_no_caffeine_record"])
                self.assertTrue(pd.isna(row["caffeine_mg"]))
                other = build_daily_feature_frame(self.target, self.target, self.root, as_of_date=date(2026, 1, 10)).iloc[0]
                self.assertTrue(pd.isna(other["caffeine_mg"]))

    def test_future_plan_is_visible_but_completion_is_unobserved(self):
        future = self.target + timedelta(days=1)
        self.write("daily_plans", future, {"tasks": [{"planned_minutes": 60, "status": "pending"}]})
        for row in (self.frame(future, future, as_of_date=self.target).iloc[0],
                    build_daily_feature_frame(future, future, self.root, as_of_date=self.target).iloc[0]):
            self.assertEqual(row["planned_hours"], 1)
            self.assertTrue(pd.isna(row["task_completion_rate"]))
            self.assertTrue(pd.isna(row["completed_task_count"]))

    def test_group_means_have_metric_specific_denominators(self):
        for offset in range(3):
            target = self.target - timedelta(days=offset)
            self.write("diary_entries", target, {"caffeine_mg": 0})
        self.write("study_logs", self.target, [])
        self.write("health_logs", self.target - timedelta(days=1), {"condition": {"focus_score": 4}})
        frame = self.frame(self.target - timedelta(days=2), self.target)
        group = self.page["render_group_table"](frame).iloc[0]
        self.assertEqual(group["日数"], 3)
        self.assertEqual(group["平均勉強時間"], 0)
        self.assertEqual(group["平均勉強時間 有効n"], 1)
        self.assertEqual(group["平均集中しやすさ"], 4)
        self.assertEqual(group["平均集中しやすさ 有効n"], 1)
        self.assertTrue(pd.isna(group["平均睡眠時間"]))
        self.assertEqual(group["平均睡眠時間 有効n"], 0)
        self.assertEqual(group["ランニング"], "未記録")
        condition = self.page["render_condition_group_table"](frame).iloc[0]
        self.assertEqual(condition["平均勉強時間 有効n"], 1)

    def test_single_day_context_is_invariant_at_month_and_year_boundaries(self):
        for target in (date(2025, 11, 30), date(2025, 12, 31)):
            with self.subTest(target=target):
                before, after = target - timedelta(days=1), target + timedelta(days=1)
                self.write("diary_entries", before, {"caffeine_mg": 100})
                self.write("diary_entries", target, {"caffeine_mg": 0})
                self.write("health_logs", after, {"sleep": {"sleep_duration_min": 420, "morning_sleepiness": 0}, "condition": {"focus_score": 4}})
                self.write("study_logs", before, [{"start_at": f"{before}T19:00", "duration_sec": 3600}])
                narrow = self.frame(target, target)
                wide = self.frame(before, after)
                common = wide.loc[wide["date"] == pd.Timestamp(target)].reset_index(drop=True)
                pd.testing.assert_frame_equal(narrow, common)
                self.assertEqual(narrow.iloc[0]["next_sleep_hours"], 7)
                self.assertEqual(narrow.iloc[0]["next_morning_sleepiness"], 0)
                self.assertEqual(narrow.iloc[0]["previous_caffeine_mg"], 100)
                self.assertEqual(narrow.iloc[0]["previous_night_study_hours"], 1)
                lag = self.page["render_lag_table"](narrow).iloc[0]
                self.assertEqual(lag["翌日睡眠時間 有効n"], 1)
                pd.testing.assert_frame_equal(self.frame(after, before), wide)

    def test_future_default_files_are_not_actual_observations(self):
        future = self.target + timedelta(days=1)
        self.write("health_logs", future, {"sleep": {"sleep_duration_min": 0}, "condition": {"focus_score": 3}})
        self.write("study_logs", future, [])
        self.write("exercise_logs", future, {"records": []})
        self.write("diary_entries", future, {"caffeine_mg": 0})
        frame = self.frame(self.target, future, as_of_date=self.target)
        self.assertTrue(pd.isna(frame.iloc[0]["next_sleep_hours"]))
        row = frame.iloc[1]
        self.assertFalse(row["health_recorded"])
        self.assertFalse(row["study_recorded"])
        self.assertFalse(row["has_diary_log"])
        self.assertEqual(row["running_state"], "未記録")

    def test_adjacent_offset_owner_file_is_grouped_by_local_start_date(self):
        target = date(2026, 1, 1)
        local = datetime(2026, 1, 1, 0, 30).astimezone()
        value = local.astimezone(timezone(timedelta(hours=-12)))
        if value.date() == target:
            local = datetime(2026, 1, 1, 23, 30).astimezone()
            value = local.astimezone(timezone(timedelta(hours=14)))
        self.assertNotEqual(value.date(), target)
        self.write("study_logs", value.date(), {"sessions": [{"start_at": value.isoformat(), "duration_sec": 3600}], "opaque": {"keep": True}})
        narrow = self.frame(target, target)
        wide = self.frame(target - timedelta(days=3), target + timedelta(days=3))
        self.assertEqual(narrow.iloc[0]["study_hours"], 1)
        performance = build_daily_feature_frame(target, target, self.root, as_of_date=date(2026, 1, 10))
        self.assertEqual(performance.iloc[0]["study_hours"], narrow.iloc[0]["study_hours"])
        pd.testing.assert_frame_equal(narrow, wide.loc[wide["date"] == pd.Timestamp(target)].reset_index(drop=True))

    def test_cached_timing_and_event_paths_do_not_reread_daily_files_or_modify_json(self):
        for offset in (-1, 0, 1):
            target = self.target + timedelta(days=offset)
            self.write("diary_entries", target, {"caffeine_mg": 100, "caffeine_records": [{"taken_at": f"{target}T08:00", "amount_mg": 100}]})
            self.write("health_logs", target, {"condition": {"focus_score": 0}})
            self.write("exercise_logs", target, {"records": []})
            self.write("study_logs", target, [{"start_at": f"{target}T09:00", "duration_sec": 0, "condition": {"effort": 0}}])
        before = self.snapshot()
        diaries, studies = {}, {}
        self.frame(diary_cache=diaries, study_cache=studies)
        reads = self.reads.copy()
        self.assertTrue(all(count == 1 for count in reads.values()), reads)
        events = self.page["build_caffeine_event_frame"](self.target, self.target, diary_cache=diaries)
        with patch("app_core.caffeine_timing_service.load_diary_entry", side_effect=AssertionError("reread diary")), patch("app_core.caffeine_timing_service.load_study_sessions_by_date", side_effect=AssertionError("reread study")):
            timing = build_session_timing_frame(self.target, self.target, self.root, preloaded_diaries=diaries, preloaded_study_by_date=studies)
        self.assertEqual(len(events), 1)
        self.assertEqual(timing.iloc[0]["duration_min"], 0)
        self.assertEqual(timing.iloc[0]["effort"], 0)
        self.assertEqual(self.reads, reads)
        self.assertEqual(self.snapshot(), before)

    def test_display_and_comments_do_not_present_nan_or_missing_as_zero(self):
        self.write("diary_entries", self.target, {"caffeine_mg": 100})
        self.write("exercise_logs", self.target, {"records": [{"type": "running"}]})
        frame = self.frame()
        table = self.page["display_table"](frame)
        self.assertEqual(table.iloc[0]["勉強時間"], "未記録")
        self.assertEqual(table.iloc[0]["ラン距離"], "未記録")
        self.assertNotIn("nan", " ".join(self.page["build_comments"](frame)))

    def test_running_factor_excludes_unknown_but_retains_recording_rate_semantics(self):
        frame = pd.DataFrame({"running_recorded": [True, False, False], "running_status_recorded": [True, True, False]})
        groups, metadata = factor_groups(frame, "running")
        self.assertEqual(groups.iloc[:2].tolist(), ["A", "B"])
        self.assertTrue(pd.isna(groups.iloc[2]))
        self.assertIn("未記録", metadata["exclusion_note"])
        self.assertEqual(frame["running_recorded"].sum(), 1)

    def test_caffeine_missing_setting_changes_comparison_only_and_never_future(self):
        future = self.target + timedelta(days=1)
        self.write("diary_entries", self.target, {})
        self.write("diary_entries", future, {"caffeine_mg": 0})
        before = self.snapshot()
        for setting in (False, True):
            with self.subTest(missing_as_zero=setting):
                frame = self.frame(self.target - timedelta(days=1), future,
                    as_of_date=self.target, caffeine_missing_as_zero=setting)
                performance = build_daily_feature_frame(self.target - timedelta(days=1), future, self.root,
                    as_of_date=self.target, caffeine_missing_as_zero=setting)
                for index in (0, 1):
                    row = frame.iloc[index]
                    self.assertFalse(row["caffeine_recorded"])
                    self.assertFalse(row["caffeine_field_recorded"])
                    self.assertEqual(bool(row["caffeine_imputed_zero"]), setting)
                    self.assertEqual(bool(row["has_no_caffeine_record"]), setting)
                    self.assertEqual(bool(row["has_diary_log"]), index == 1)
                    if setting:
                        self.assertEqual(row["caffeine_mg"], 0)
                        self.assertEqual(performance.iloc[index]["caffeine_mg"], 0)
                    else:
                        self.assertTrue(pd.isna(row["caffeine_mg"]))
                        self.assertTrue(pd.isna(performance.iloc[index]["caffeine_mg"]))
                    self.assertTrue(pd.isna(row["study_hours"]))
                    self.assertTrue(pd.isna(row["sleep_hours"]))
                    self.assertEqual(row["running_state"], "未記録")
                for current in (frame, performance):
                    self.assertTrue(pd.isna(current.iloc[2]["caffeine_mg"]))
                    self.assertFalse(current.iloc[2]["caffeine_imputed_zero"])
                groups, _ = factor_groups(performance, "caffeine")
                self.assertEqual(groups.iloc[:2].eq("B").sum(), 2 if setting else 0)
                self.assertTrue(pd.isna(groups.iloc[2]))
        self.assertEqual(before, self.snapshot())

    def test_intake_details_without_mirror_have_identical_caffeine_totals_without_profile_reads(self):
        legacy = {"caffeine_id": "shared", "taken_at": f"{self.target}T08:00", "amount_mg": 80}
        generic = {"substance_record_id": "shared", "substance_id": "caffeine", "taken_at": f"{self.target}T08:00", "amount_mg": 80}
        for payload, expected in (
            ({"caffeine_records": [legacy]}, 80),
            ({"caffeine_records": [legacy], "caffeine_untimed_mg": 20}, 100),
            ({"caffeine_records": [legacy], "caffeine_mg": 150}, 150),
            ({"caffeine_records": [legacy], "substance_records": [generic]}, 80),
            ({"substance_records_schema_version": 1, "substance_records": [generic]}, 80),
            ({"caffeine_mg": None, "caffeine_cups": 2}, 200),
        ):
            with self.subTest(payload=payload):
                self.write("diary_entries", self.target, payload)
                before = self.snapshot()
                with patch.object(substance_service, "substance_profile_map", side_effect=AssertionError("unexpected profile read")):
                    row = self.frame(caffeine_missing_as_zero=True).iloc[0]
                    other = build_daily_feature_frame(self.target, self.target, self.root,
                        as_of_date=date(2026, 1, 10), caffeine_missing_as_zero=True).iloc[0]
                for current in (row, other):
                    self.assertEqual(current["caffeine_mg"], expected)
                    self.assertTrue(current["caffeine_recorded"])
                    self.assertFalse(current["caffeine_imputed_zero"])
                self.assertEqual(before, self.snapshot())

    def test_legacy_list_diary_uses_last_entry_without_mutation(self):
        self.write("diary_entries", self.target, [{"caffeine_mg": 200}, {
            "caffeine_records": [{"taken_at": f"{self.target}T08:00", "amount_mg": 80}],
            "custom": {"preserve": True},
        }])
        before = self.snapshot()
        for setting in (False, True):
            first = self.frame(caffeine_missing_as_zero=setting).iloc[0]
            second = build_daily_feature_frame(self.target, self.target, self.root,
                as_of_date=date(2026, 1, 10), caffeine_missing_as_zero=setting).iloc[0]
            for row in (first, second):
                self.assertEqual(row["caffeine_mg"], 80)
                self.assertTrue(row["caffeine_recorded"])
                self.assertFalse(row["caffeine_imputed_zero"])
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
