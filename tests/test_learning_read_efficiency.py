from __future__ import annotations

import ast
from collections import Counter, defaultdict
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from app_core import analytics_service as analytics
from app_core import json_store
from app_core.analysis_dates import parse_analysis_date, parse_analysis_datetime
from app_core.json_store import JsonReadError
from app_core.master_snapshot import MasterSnapshot
from app_core.study_log_service import load_all_study_sessions


PAGE = Path(__file__).resolve().parents[1] / "pages" / "11_学習蓄積ビジュアライザー.py"


def page_aggregations(root: Path) -> dict[str, Any]:
    """Load the real pure page functions without executing the Streamlit page."""
    names = {
        "parse_day", "matches_master_filters", "session_condition_value",
        "normalize_io_type", "build_category_io_summary", "most_studied_hour",
    }
    tree = ast.parse(PAGE.read_text(encoding="utf-8-sig"))
    module = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[])
    namespace = {
        "date": date, "datetime": datetime, "Any": Any,
        "defaultdict": defaultdict, "ROOT": root,
        "load_all_study_sessions": load_all_study_sessions,
        "parse_analysis_date": parse_analysis_date, "parse_analysis_datetime": parse_analysis_datetime,
        "IO_INPUT": "インプット", "IO_OUTPUT": "アウトプット",
        "IO_BOTH": "両方", "IO_UNSET": "未設定",
    }
    exec(compile(module, str(PAGE), "exec"), namespace)
    return namespace


class LearningReadEfficiencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(analytics, "app_today", return_value=date(2026, 9, 16)))
        original_read = json_store._read

        def isolated_read(path):
            self.assertTrue(Path(path).resolve().is_relative_to(self.root.resolve()), f"Out-of-fixture JSON read: {path}")
            return original_read(path)

        self.stack.enter_context(patch.object(json_store, "_read", side_effect=isolated_read))
        for constant, relative in {
            "STUDY_LOGS_DIR": "study_logs",
            "DAILY_PLANS_DIR": "daily_plans",
            "NOTES_DATA_DIR": "notes_data",
            "FLASHCARDS_PATH": "flashcards.json",
            "REMINDERS_PATH": "reminders.json",
        }.items():
            self.stack.enter_context(patch.object(analytics, constant, self.root / relative))
        self.put("study_logs/2026/09/20260905.json", [
            {"start_at": "2026-09-05T08:00:00", "duration_sec": 3600, "money": 1000,
             "project_id": "p", "subject_id": "s", "condition": {"category": "計算", "io_type": "both"}},
            {"start_at": "2026-09-06T09:00:00", "duration_sec": "1800", "money": "500.5",
             "project_id": "p", "subject_id": "s", "category": "計算", "io_type": "output"},
            {"end_at": "2026-09-06T10:00:00", "duration_sec": 60, "money": 1,
             "project_id": "other", "subject_id": "missing"},
            {"start_at": "not-a-date", "duration_sec": 9999}, "ignore-non-record",
        ])
        self.put("study_logs/2026/09/20260901.json", [
            {"start_at": "2026-09-01T12:00:00", "duration_sec": 7200, "money": 2000,
             "project_id": "p", "subject_id": "s"},
        ])
        self.put("study_logs/2026/09/20260904.json", {"sessions": [
            {"start_at": "2026-09-05T20:00:00", "duration_sec": 14400,
             "project_id": "p", "subject_id": "s", "category": "旧形式", "io_type": "input"},
        ]})
        self.put("study_logs/backups/ignored.json", [{"start_at": "2026-09-05", "duration_sec": 99999}])
        self.put("daily_plans/20260905.json", {"date": "2026-09-05", "tasks": [
            {"project_id": "p", "subject_id": "s", "planned_minutes": 90},
            {"project_id": "other", "planned_minutes": 10},
        ]})
        self.put("notes_data/notebooks.json", {"notebooks": [{"sections": [{"pages": [
            {"created_at": "2026-09-05", "project_id": "p", "subject_id": "s"},
            {"updated_at": "2026-09-01", "project_id": "p", "subject_id": "s"},
        ]}]}]})
        self.put("flashcards.json", [{"created_at": "2026-09-06", "project_id": "p", "subject_id": "s"}])
        self.put("reminders.json", [
            {"scheduled_date": "2026-09-05", "project_id": "p", "subject_id": "s", "status": "done"},
            {"registered": "2026-09-06", "project_id": "p", "subject_id": "s", "status": "pending"},
        ])
        self.master = MasterSnapshot({"subjects": [{"subject_id": "s", "name": "統計学"}]})
        self.start, self.end = date(2026, 9, 5), date(2026, 9, 6)
        self.filters = {"project_id": "p"}
        self.page = page_aggregations(self.root)

    def put(self, relative: str, value: Any) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def fingerprints(self) -> dict[str, str]:
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.rglob("*.json")}

    def test_shared_input_matches_all_views_including_legacy_sessions(self) -> None:
        before = self.fingerprints()
        args = (self.start, self.end, self.filters)
        expected_daily = analytics.build_daily_learning_summary(*args)
        expected_subject = analytics.build_subject_growth_summary(*args, master=self.master)
        expected_curve = analytics.build_cumulative_learning_curve(*args)
        expected_month = analytics.build_monthly_forest_data(2026, 9, self.filters)
        expected_category = self.page["build_category_io_summary"](*args)
        expected_hour = self.page["most_studied_hour"](*args)
        snapshot = analytics.LearningDataSnapshot.load()
        snapshot_before = deepcopy(snapshot)

        daily = analytics.build_daily_learning_summary(*args, snapshot=snapshot)
        self.assertEqual(daily, expected_daily)
        self.assertEqual(analytics.build_subject_growth_summary(*args, snapshot=snapshot, master=self.master), expected_subject)
        self.assertEqual(analytics.cumulative_curve_from_daily(daily), expected_curve)
        self.assertEqual(analytics.build_monthly_forest_data(2026, 9, self.filters, snapshot=snapshot), expected_month)
        self.assertEqual(self.page["build_category_io_summary"](*args, sessions=snapshot.activity_sessions), expected_category)
        self.assertEqual(self.page["most_studied_hour"](*args, sessions=snapshot.activity_sessions), expected_hour)
        self.assertEqual(expected_hour, "20:00台")
        self.assertEqual(sum(row["total_duration_sec"] for row in daily), 19800)
        self.assertEqual(sum(row["session_count"] for row in daily), 3)
        self.assertEqual(sum(row["duration_sec"] for row in expected_subject), 19800)
        self.assertEqual(sum(row["session_count"] for row in expected_category), 3)
        self.assertEqual(sum(row["total_hours"] for row in expected_category), 5.5)
        self.assertEqual(expected_subject[0]["subject_name"], "統計学")
        self.assertEqual(expected_subject[0]["note_count"], 1)
        self.assertEqual(expected_subject[0]["done_reminder_count"], 1)
        self.assertEqual(expected_subject[0]["pending_reminder_count"], 1)
        self.assertEqual(snapshot, snapshot_before)
        self.assertEqual(self.fingerprints(), before)

    def test_each_file_is_read_once_for_all_six_views(self) -> None:
        with patch.object(analytics, "load_json_safe", wraps=analytics.load_json_safe) as reader:
            snapshot = analytics.LearningDataSnapshot.load()
            reads_after_load = reader.call_count
            args = (self.start, self.end, self.filters)
            daily = analytics.build_daily_learning_summary(*args, snapshot=snapshot)
            analytics.build_subject_growth_summary(*args, snapshot=snapshot, master=self.master)
            analytics.cumulative_curve_from_daily(daily)
            analytics.build_monthly_forest_data(2026, 9, self.filters, snapshot=snapshot)
            with patch.dict(self.page, {"load_all_study_sessions": unittest.mock.Mock(side_effect=AssertionError("unexpected reread"))}):
                self.page["build_category_io_summary"](*args, sessions=snapshot.activity_sessions)
                self.page["most_studied_hour"](*args, sessions=snapshot.activity_sessions)
            self.assertEqual(reader.call_count, reads_after_load)
            self.assertEqual(reads_after_load, 7)
            self.assertTrue(all(count == 1 for count in Counter(call.args[0] for call in reader.call_args_list).values()))

    def test_month_remains_independent_of_selected_period_and_reverse_dates(self) -> None:
        snapshot = analytics.LearningDataSnapshot.load()
        selected = analytics.build_daily_learning_summary(self.end, self.start, self.filters, snapshot=snapshot)
        self.assertEqual([row["date"] for row in selected], ["2026-09-05", "2026-09-06"])
        month = analytics.build_monthly_forest_data(2026, 9, self.filters, snapshot=snapshot)
        self.assertEqual(len(month), 30)
        self.assertEqual(month[0]["total_duration_sec"], 7200)
        self.assertEqual(sum(row["total_duration_sec"] for row in month), 27000)
        self.assertEqual(month[0]["note_count"], 1)

    def test_empty_supplied_snapshot_does_not_reload_and_curve_copies_rows(self) -> None:
        snapshot = analytics.LearningDataSnapshot([], [], [], [], [], [])
        with patch.object(analytics, "load_json_safe", side_effect=AssertionError("unexpected read")):
            daily = analytics.build_daily_learning_summary(self.start, self.end, snapshot=snapshot)
            self.assertEqual(sum(row["session_count"] for row in daily), 0)
            self.assertEqual(analytics.build_subject_growth_summary(self.start, self.end, snapshot=snapshot, master=self.master), [])
        before = deepcopy(daily)
        curve = analytics.cumulative_curve_from_daily(daily)
        self.assertEqual(daily, before)
        curve[0]["total_duration_sec"] = 999
        self.assertEqual(daily, before)

    def test_snapshot_keeps_strict_json_errors(self) -> None:
        (self.root / "flashcards.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(JsonReadError):
            analytics.LearningDataSnapshot.load()


if __name__ == "__main__":
    unittest.main()
